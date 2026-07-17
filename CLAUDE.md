# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run the app

```bash
pip install -r requirements.txt
python main.py
```

Requires: `PyQt6`, `requests`, `python-dotenv`（可选）。无测试框架。

## Architecture

Five-layer dependency chain — higher layers import from lower, never reverse:

```
domain/  ──→  ai/  ──→  app/  ──→  ui/  ──→  main.py
                      services/ ──┘
```

### `domain/` — pure game logic, zero framework imports

- **`game.py`** — `ChineseChessGame`: 10×9 board. Uppercase = Red, lowercase = Black. Core move/checkmate logic, move validation, position hashing for repetition detection. King position cache (`_king_pos`) for O(1) check detection. `_is_jiangsha()` replaces `_is_checkmated`. Move bounds check and king capture guard added.
- **`evaluation.py`** — Static evaluation from Red's perspective (~40 features). Linear model: material + PST (7 piece types × 2 colors) + mobility + soldier structure + general safety + open columns + piece coordination + center/river control. Detects tactical patterns: 卧槽馬/挂角馬, 双車错/重炮/铁门栓/当头炮. Endgame-aware (switches piece values, general activation bonus at ≤14 pieces). All piece names use Chinese terms (卒/馬/車 etc.).
- **`search.py`** — `SearchEngine`: Alpha-Beta + PVS + quiescence + check extensions + null-move pruning + transposition table (1M-entry LRU via OrderedDict). Iterative deepening with time control. ~3000 nodes/sec. Used as a **tool by LLM** (not directly by controller). Constants: `JIANGSHA_SCORE`, `KUNBI_SCORE`.
- **`mcts.py`** — `MCTSEngine`: Monte Carlo Tree Search with UCB1, prior-guided search (LLM virtual visits), evaluation-function-driven simulation. Used by **controller** for tactical verification and as fallback.
- **`pikafish.py`** — `PikafishEngine`: UCI protocol wrapper for the Pikafish NNUE engine (Stockfish-derived, master-level). Async search via daemon thread + `pyqtSignal` relay to main thread. Falls back silently to MCTS if binary not found. Binary goes in `engines/pikafish.exe` or set `PIKAFISH_PATH` env var.
- **`egtb.py`** — Endgame tablebase: queries chessdb.cn cloud database for ≤4-piece endgames (DTM/win/side). Local heuristic for ≤10 pieces. Cached with 5-min TTL.
- **`openings.py`** — 19 standard opening lines (中炮对屏風馬, 顺炮, etc.), weighted random selection by prefix-matching move history.
- **`fen.py`** — Shared FEN generation (`board_to_fen`, `game_to_fen`). Used by pikafish and egtb. Replaces duplicated `_board_to_fen` in both files.
- **`models.py`** — `ModelInfo` dataclass (`id`, `name`, `type`, `endpoint`, `model`, `api_key`, `tools_choice`, `system_prompt`, `options`).
- **`constants.py`** — Board size, search config, MCTS config, AI timeout/retry settings, vision mode config. Utility functions: `format_duration`, `format_coord`, `parse_coord`, `format_move`.
- **`prompts.py`** — System prompts (full ~1800 chars + lite ~1050 chars), arbitration prompt (~680 chars), user prompt builder, legal-move formatter, tool definitions. All piece names unified: 将/士/相/馬/車/炮/卒 for both sides.

### `ai/` — AI API interaction

- **`worker.py`** — `AIWorker` (extends `QRunnable`). Runs in raw `threading.Thread` (cancellable via `requests.Session.close()`). Implements **multi-turn agentic loop** (up to 4 turns): LLM can call `search_best_move` (runs Alpha-Beta engine locally) or `evaluate_position` (runs static eval), then ultimately `move_piece`. Auto-detects LM Studio for `think` param handling. Fallback text parser if tool-calling fails. Temp game objects sync `_king_pos` cache for performance.
- **`manager.py`** — `AIManager`: worker lifecycle + `cancel_version` gating for stale-response rejection. `shutdown()` sets flag and cancels active worker.
- **`parser.py`** — Regex coordinate parser (`[A-I]\d{1,2}`) as last-resort fallback when LLM returns text without tool calls.

### `app/` — orchestration

- **`controller.py`** — `GameController`: central state machine. Three AI modes: `hybrid` (default), `search_only`, `llm_only`. Manages Pikafish async lifecycle, version gating, retry logic (non-retryable errors skip retries), fallback chain. Key subsystems:
  - **Opening book** — checked first, saves tokens
  - **Hybrid mode** — engine (Pikafish→MCTS) runs first → result injected into LLM prompt as "引擎参考走法" → LLM makes final decision → if LLM disagrees with engine, **DeepSeek arbitration** is triggered
  - **Arbitration** — when LLM and engine disagree in hybrid mode, a third-party DeepSeek model judges which move is better. Scores tracked (`ai_score`): +1 when LLM matches arbitrator, 0 otherwise. On arbitration failure, LLM's move is used as fallback.
  - **Fallback chain**: LLM failure → engine result → random move (capped at 3 consecutive). Hybrid mode no longer falls back to search; uses engine result or random directly.
  - **Helpers**: `_refresh_ui()`, `_schedule_next_ai_move()`, `_mcts_fallback()` eliminate duplicate patterns.
- **`protocols.py`** — `MainWindowProtocol`: structural typing for `GameController.main`, breaks circular dependency.

### `services/` — config & logging

- **`models.py`** — `ModelManager`: loads `models.json`, resolves `${VAR_NAME}` env-var references in API keys, groups models by `-p1`/`-p2` suffix for player-specific assignment.
- **`logging.py`** — `LogManager`: HTML-colored log output to `QTextEdit`, timestamped.

### `ui/` — PyQt6 GUI

`board.py` (board widget + image capture via `render()` not `grab()`), `window.py` (main window), `panel.py` (side panel), `theme.py`.

## Piece Name Convention

红黑双方使用统一的棋子名称，仅通过颜色（红底/黑底圆形）和字母大小写区分：

| 字母 | 红(K) | 黑(k) | 名称 |
|------|-------|-------|------|
| K/k | K | k | 将 |
| A/a | A | a | 士 |
| B/b | B | b | 相 |
| N/n | N | n | 馬 |
| R/r | R | r | 車 |
| C/c | C | c | 炮 |
| P/p | P | p | 卒 |

## AI Decision Flow (Hybrid Mode — default)

```
Controller.make_ai_move()
  ├─ 1. Opening book lookup → if found, play immediately
  ├─ 2. If search_only mode:
  │    └─ Pikafish async → _on_pikafish_search_done (signal relay)
  │         └─ Fallback: MCTS sync if Pikafish unavailable
  ├─ 3. If hybrid mode:
  │    ├─ Engine-first: Pikafish async → MCTS fallback (_mcts_fallback, 5s limit)
  │    ├─ _on_hybrid_engine_done(): validate engine move, format hint
  │    ├─ _start_llm_request(): build prompt (with engine hint), launch worker
  │    ├─ AIWorker._agentic_loop(): multi-turn tool calling
  │    │    └─ LLM may call search_best_move / evaluate_position before move_piece
  │    ├─ on_ai_finished(): parse coords
  │    │    ├─ LLM == engine → execute immediately
  │    │    └─ LLM != engine → _start_arbitration() (DeepSeek judge)
  │    │         └─ on_arbitration_finished(): score (+1/0) + execute winner
  │    └─ LLM failure → execute engine result directly (no search fallback)
  ├─ 4. If llm_only mode:
  │    └─ LLM directly, retry up to 3 times, then MCTS fallback
  └─ 5. Final safety net: _random_move (capped at 3 consecutive)
```

## Two Search Engines — Distinct Roles

| Engine | File | Used By | Role |
|--------|------|---------|------|
| `MCTSEngine` | `domain/mcts.py` | Controller | Tactical verification, hybrid engine-first, search_only mode, all fallbacks |
| `SearchEngine` | `domain/search.py` | LLM (via `search_best_move` tool) | On-demand deep analysis requested by LLM during agentic loop |

Both are distinct from **Pikafish** (`domain/pikafish.py`), the external NNUE engine that is the preferred engine for the controller. Priority: Pikafish → MCTSEngine → (last resort: random).

## Model Configuration (`models.json`)

Models are configured in `models.json` at project root. Key conventions:
- API keys use `${VAR_NAME}` syntax for env-var substitution
- `-p1` suffix on model `id` → shown only in red-side dropdown; `-p2` → black-side only
- A model with `id: "arbitration"` or `type: "deepseek"` is auto-selected as the arbitration judge
- `type: "lmstudio"` triggers LM Studio-specific `think` param handling
- `type: "deepseek"` uses `think` field in API payload
- `system_prompt` field on a model overrides the default system prompt
- `tools_choice` field maps to API `tool_choice` parameter

## Key design decisions

- **Hybrid AI**: Engine (Pikafish/MCTS) provides tactical precision; LLM provides strategic understanding and final authority. On disagreement, DeepSeek arbitrates.
- **Engine-first in hybrid**: Engine runs before LLM, so engine recommendation is available in the prompt immediately — no second round-trip.
- **Legal moves in prompt**: All legal moves are formatted grouped by piece type. LLM selects from the list instead of guessing coordinates, drastically reducing error rate.
- **Multi-turn agentic loop**: LLM can call tools (`search_best_move`, `evaluate_position`) for deeper analysis before committing to `move_piece`. Max 4 turns to prevent infinite loops.
- **Always tool calling**: No UI toggle. Prompts instruct function calling. Text parser exists only as fallback for models that return text despite receiving tools.
- **Thread not threadpool**: AIWorker uses raw `threading.Thread` so `requests.Session.close()` can abort in-flight HTTP from `cancel()`.
- **Version gating**: Every AI request carries `game_version`. Reset/pause increments it. `cancel_version` (from `AIManager`) adds a second layer. Stale-response rejection prevents race conditions from overlapping AI calls.
- **Pikafish async via signal relay**: `_PikafishRelay` (QObject with `pyqtSignal`) bridges daemon thread results to Qt main thread, avoiding UI freezes.
- **King position cache**: `_king_pos` dict in ChineseChessGame tracks both generals' positions. `_is_in_check` and `_is_king_facing` use cache for O(1) lookup, with automatic full-board fallback if cache is stale.
- **LRU transposition table**: OrderedDict-based eviction eliminates the old O(n) memory allocation when the table fills up.
- **Worker isolation**: `_run_search` and `_run_evaluate` use temporary game objects with `_king_pos` sync, never touching `self.game.board`.
- **Chinese UI**: All UI text and prompts are in Chinese. No i18n infrastructure.
- **No persistence**: Game state is in-memory only. `QSettings` used only for left-panel collapsed state.
- **Vision mode**: When enabled, board is rendered to QPixmap via `render()` (not `grab()`) and sent as JPEG base64 to multimodal models. Dual-channel prompt: image for strategic perception, text legal-move list for tactical execution.
- **Opening book**: 19 standard opening lines cover the first 4-6 half-moves, saving tokens and ensuring strong opening play.
- **EGTB integration**: chessdb.cn cloud queries for ≤4-piece endgames with 5-min cache; local heuristic for ≤10 pieces.
- **MCTS fallback**: Callback-based MCTS uses reduced limits (500 sims / 5s) to avoid UI freezes. Hybrid mode LLM failures use engine result or random move directly without search.
- **Prompts**: Compact, structured prompts with quantifiable judging criteria for arbitration. Full version ~1800 chars, lite ~1050 chars, arbitration ~680 chars. All piece names use unified Chinese terms.
- **requirements.txt**: Declares `PyQt6>=6.5`, `requests>=2.28`, `python-dotenv>=1.0`.
