# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run the app

```bash
pip install -r requirements.txt
python main.py
```

Requires: `PyQt6`, `requests`, `python-dotenv`（可选）。无测试框架；`tests/` 下是纯 python 验证脚本：

```bash
python tests/smoke_engine.py      # 无 GUI 冒烟（改动后必过）
python tests/compare_movegen.py   # 走法生成与 3000+ 局面基线对拍（改生成/将军检测后必过）
```

## Architecture

Five-layer dependency chain — higher layers import from lower, never reverse:

```
domain/  ──→  ai/  ──→  app/  ──→  ui/  ──→  main.py
                      services/ ──┘
```

### `domain/` — pure game logic, zero framework imports

- **`game.py`** — `ChineseChessGame`: 10×9 board. Uppercase = Red, lowercase = Black. Targeted move generation (rook/cannon ray-walking, knight/bishop/advisor/king/pawn directed branches; verified equivalent to the old 90-square brute force via `tests/compare_movegen.py`). `_is_in_check` uses reverse detection from the king square (rays/knight spots/pawn spots). Incremental Zobrist hash maintained by `move_piece` and search make/unmake — call `recompute_hash()` after replacing `board` externally. Checkmate/stalemate share one move generation. King position cache (`_king_pos`) with automatic full-board fallback. `get_capture_moves` for quiescence.
- **`evaluation.py`** — Static evaluation from Red's perspective (~40 features). Linear model: material + PST (7 piece types × 2 colors) + mobility (only when caller supplies move counts; search leaves skip it for speed) + soldier structure + general safety + open columns + piece coordination + center/river control. Detects tactical patterns: 卧槽馬/挂角馬 (leg-checked), 双車错/重炮/铁门栓/当头炮. Endgame-aware (switches piece values, general activation bonus at ≤`ENDGAME_PIECE_THRESHOLD` pieces). `compute_material(board)` is the shared material counter (king excluded, 兵=1 scale) used by controller prompts, worker eval output, and UI display. All piece names use Chinese terms (卒/馬/車 etc.).
- **`search.py`** — `SearchEngine`: Alpha-Beta + PVS (incl. root) + quiescence + check extensions + null-move pruning (R=2, min depth 6 — effectively off at default depths) + transposition table (1M-entry LRU via OrderedDict, Zobrist keys). Iterative deepening with full re-ordering each iteration. Quiescence searches all evasions when in check (no more stand-pat mate blindness), captures-only otherwise via `get_capture_moves`. ~12k nodes/sec. Used as a **tool by LLM** (not directly by controller). Constants: `JIANGSHA_SCORE`, `KUNBI_SCORE`.
- **`mcts.py`** — `MCTSEngine`: Monte Carlo Tree Search with UCB1, prior-guided search (LLM virtual visits), evaluation-function-driven simulation. **Real search**: selection plays moves on a work-board copy (`SearchEngine._make_move`), so expansion/simulation operate on the actual leaf positions. Used by **controller** for tactical verification and as fallback.
- **`pikafish.py`** — `PikafishEngine`: UCI protocol wrapper for the Pikafish NNUE engine (Stockfish-derived, master-level). Async search via daemon thread + `pyqtSignal` relay to main thread. Engine stdout is pumped by a dedicated reader thread into a line queue — all reads are deadline-bounded `queue.get(timeout=)` (a silently-hung engine can no longer deadlock the game or window close); close/kill terminate the process before taking the lock. Falls back silently to MCTS if binary not found. Binary goes in `engines/pikafish.exe` or set `PIKAFISH_PATH` env var.
- **`egtb.py`** — Endgame tablebase: queries chessdb.cn cloud database for ≤6-piece endgames (`EGTB_CLOUD_MAX_PIECES`; DTM/win/side). Local heuristic for ≤10 pieces (`EGTB_MAX_PIECES`) with defender advisor/bishop awareness (lone rook vs full guard = draw, lone knight beats bare king, double cannons beat bare king). Positive cache 5-min TTL + negative cache 60s + circuit breaker (120s after 3 consecutive network failures) + 5000-entry cap. `probe(..., allow_cloud=False)` is mandatory inside search/MCTS leaves — no synchronous HTTP in the search loop; cloud queries are for the UI layer only.
- **`openings.py`** — 19 standard opening lines (中炮对屏風馬, 顺炮, etc.), weighted random selection by prefix-matching move history.
- **`fen.py`** — Shared FEN generation (`board_to_fen`, `game_to_fen`). Used by pikafish and egtb. Replaces duplicated `_board_to_fen` in both files.
- **`models.py`** — `ModelInfo` dataclass (`id`, `name`, `type`, `endpoint`, `model`, `api_key`, `tools_choice`, `system_prompt`, `options`).
- **`constants.py`** — Board size, search config, MCTS config, AI timeout/retry settings, vision mode config. Utility functions: `format_duration`, `format_coord`, `parse_coord`, `format_move`. Note: `SEARCH_MAX_DEPTH` is the default for the UI 搜索强度 spinbox (1~6) — it is **not** Alpha-Beta depth; it scales MCTS simulations and Pikafish time (see controller `_DEPTH_SIMS_MAP`).
- **`prompts.py`** — System prompts (full ~2280 chars + lite ~1270 chars, both take `include_analysis_tools` to strip tool docs for llm_only mode), arbitration prompt (~630 chars), user prompt builder (`engine_hint` injects engine reference, `material_str` injects perspective-relative material balance), legal-move formatter with **tactical annotations** (×子=吃子, +=将军, computed by simulating each move on a board copy; groups sorted 将军>吃子>其他), tool definitions (`DEFAULT_TOOLS` = all 3 tools; `TOOLS_BASIC` = `move_piece` only, used in llm_only mode and arbitration). Piece names follow the red/black convention below (帥/将, 仕/士, 相/象, 兵/卒).

### `ai/` — AI API interaction

- **`worker.py`** — `AIWorker` (plain class; not `QRunnable` — it never entered a QThreadPool). Runs in raw `threading.Thread`. Implements **multi-turn agentic loop** (up to 4 turns): LLM can call `search_best_move` (runs Alpha-Beta engine locally) or `evaluate_position` (runs static eval), then ultimately `move_piece`. Auto-detects LM Studio for `think` param handling. 429/408 rate-limit errors are retryable ("限流错误：" prefix); other 4xx are not. Error prefix unified to `ERROR:`. Fallback text parser if tool-calling fails (excludes `[Tool: ...]` outputs from coordinate extraction). Temp game objects sync `_king_pos` cache and call `recompute_hash()` after board replacement. Note: `cancel()` via `Session.close()` does not reliably abort in-flight HTTP — stale responses are actually rejected by `cancel_version` gating.
- **`manager.py`** — `AIManager`: worker lifecycle + `cancel_version` gating for stale-response rejection. `shutdown()` sets flag and cancels active worker.
- **`parser.py`** — Regex coordinate parser (`[A-I]\d{1,2}`) as last-resort fallback. Prefers `move_piece(...)` text patterns; otherwise takes the **last** two coordinates (LLM reasoning typically quotes the engine's move first and states its own decision last).

### `app/` — orchestration

- **`controller.py`** — `GameController`: central state machine. Three AI modes: `hybrid` (default), `search_only`, `llm_only`. Manages Pikafish async lifecycle, version gating, retry logic (non-retryable errors skip retries), fallback chain. Key subsystems:
  - **Opening book** — checked first (`get_opening_move` returns None outside book), saves tokens
  - **Hybrid mode** — engine (Pikafish→MCTS) runs first → result injected into LLM prompt as "引擎参考走法" (trust-tiered: Pikafish = "默认采信", MCTS fallback = weak reference) → LLM legality pre-check (illegal LLM move uses engine move directly, no pointless arbitration) → divergence check → if LLM disagrees with engine, **arbitration** is triggered
  - **搜索强度 (search_depth, UI 1~6)** — scales engine strength, not Alpha-Beta depth: `_DEPTH_SIMS_MAP` maps it to MCTS simulations (500~3000); Pikafish time limit = depth×3s, capped at `MCTS_TIME_LIMIT` (15s). MCTS hard cap: `MCTS_TIME_LIMIT` (15s).
  - **Arbitration** — when LLM and engine disagree in hybrid mode, a third-party DeepSeek model judges which move is better. Candidates are A/B-randomized with sources hidden; both sides get a basis block (LLM reasoning summary vs anonymized engine basis). Result is enforced to be one of the two candidates (default: engine). Scores tracked (`ai_score`): +1 when LLM matches arbitrator, 0 otherwise. On arbitration failure, LLM's move is used as fallback.
  - **Fallback chain**: LLM failure → engine result → random move (capped at 3 consecutive). Hybrid mode no longer falls back to search; uses engine result or random directly.
  - **Fully async engine chain**: when Pikafish is unavailable, MCTS also runs on a background thread (`_start_mcts_async`, board snapshot first) with results relayed via `_PikafishRelay` — no synchronous search ever blocks the Qt main thread. Relay callbacks verify both `game_version` and `cancel_version`; stale callbacks only log and never `_finish_ai_move()` (prevents double-move races).
  - **Helpers**: `_refresh_ui()`, `_schedule_next_ai_move()`, `_execute_engine_move_or_random()` eliminate duplicate patterns.
- **`protocols.py`** — `MainWindowProtocol`: structural typing for `GameController.main`, breaks circular dependency.

### `services/` — config & logging

- **`models.py`** — `ModelManager`: loads `models.json`, resolves `${VAR_NAME}` env-var references in API keys, groups models by `-p1`/`-p2` suffix for player-specific assignment.
- **`logging.py`** — `LogManager`: HTML-colored log output to `QTextEdit`, timestamped. Block cap (`LOG_MAX_BLOCKS` in constants.py) trims oldest entries so long AI-vs-AI games don't degrade insert/layout speed; messages are HTML-escaped (LLM raw text can't break rendering); auto-scroll only follows when the scrollbar is already at the bottom.

### `ui/` — PyQt6 GUI

`board.py` (board widget + image capture via `render()` not `grab()`), `window.py` (main window; move history is a single `QLabel` rebuilt via `format_move`; Pikafish init deferred via `QTimer.singleShot(0, ...)`), `panel.py` (side panel), `theme.py`.

## Piece Name Convention

棋子名称红黑区分（車/馬/炮双方通用），字母大小写区分阵营：大写=红，小写=黑。UI 以红底/黑底圆形区分颜色。

| 字母 | 红方 | 黑方 |
|------|------|------|
| K/k | 帥 | 将 |
| A/a | 仕 | 士 |
| B/b | 相 | 象 |
| N/n | 馬 | 馬 |
| R/r | 車 | 車 |
| C/c | 炮 | 炮 |
| P/p | 兵 | 卒 |

唯一权威映射在 `domain/constants.py` 的 `PIECE_SYMBOLS`。

## AI Decision Flow (Hybrid Mode — default)

```
Controller.make_ai_move()
  ├─ 1. Opening book lookup → if found, play immediately
  ├─ 2. If search_only mode:
  │    └─ Pikafish async → _on_pikafish_search_done (signal relay)
  │         └─ Fallback: MCTS on background thread if Pikafish unavailable/failed
  ├─ 3. If hybrid mode:
  │    ├─ Engine-first: Pikafish async → MCTS fallback (background thread, 500 sims/5s)
  │    ├─ _on_hybrid_engine_done(): validate engine move, format trust-tiered hint
  │    ├─ _start_llm_request(): build prompt (with engine hint), launch worker
  │    ├─ AIWorker._agentic_loop(): multi-turn tool calling
  │    │    └─ LLM may call search_best_move / evaluate_position before move_piece
  │    ├─ on_ai_finished(): parse coords → legality pre-check
  │    │    ├─ illegal → execute engine move directly
  │    │    ├─ LLM == engine → execute immediately
  │    │    └─ LLM != engine → _start_arbitration() (DeepSeek judge)
  │    │         └─ on_arbitration_finished(): score (+1/0) + execute winner
  │    └─ LLM failure → execute engine result directly (no search fallback)
  ├─ 4. If llm_only mode:
  │    └─ LLM directly (TOOLS_BASIC: move_piece only; system prompt built with
  │       include_analysis_tools=False), retry up to 3 times, then MCTS fallback
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
- **Thread not threadpool**: AIWorker uses raw `threading.Thread`. (Historical note: `requests.Session.close()` from `cancel()` does **not** reliably abort in-flight HTTP — stale responses are actually discarded via `cancel_version` gating.)
- **Version gating**: Every AI request carries `game_version`. Reset/pause increments it. `cancel_version` (from `AIManager`) adds a second layer — Pikafish/MCTS relay callbacks verify **both** and stale callbacks only log (never reset busy state), preventing double-move races.
- **Pikafish async via signal relay**: `_PikafishRelay` (QObject with `pyqtSignal`) bridges daemon thread results to Qt main thread, avoiding UI freezes. MCTS fallback searches use the same relay from a background thread (`_start_mcts_async`).
- **King position cache**: `_king_pos` dict in ChineseChessGame tracks both generals' positions. `_is_in_check` (reverse detection from the king square: rays/knight legs/pawn spots) and `_is_king_facing` use the cache, with automatic full-board fallback if stale. Incremental Zobrist hash maintained alongside; call `recompute_hash()` after external board replacement.
- **LRU transposition table**: OrderedDict-based eviction eliminates the old O(n) memory allocation when the table fills up.
- **Worker isolation**: `_run_search` and `_run_evaluate` use temporary game objects with `_king_pos` sync, never touching `self.game.board`.
- **Chinese UI**: All UI text and prompts are in Chinese. No i18n infrastructure.
- **No persistence**: Game state is in-memory only. `QSettings` used only for left-panel collapsed state.
- **Vision mode**: When enabled, board is rendered to QPixmap via `render()` (not `grab()`) and sent as JPEG base64 to multimodal models. Dual-channel prompt: image for strategic perception, text legal-move list for tactical execution.
- **Opening book**: 19 standard opening lines cover the first 4-6 half-moves, saving tokens and ensuring strong opening play.
- **EGTB integration**: chessdb.cn cloud queries for ≤6-piece endgames (5-min positive cache, 60s negative cache, circuit breaker); local heuristic for ≤10 pieces. Search/MCTS leaves use `allow_cloud=False` — no synchronous HTTP inside the search loop (it used to paralyze endgame search).
- **MCTS fallback**: Background-thread MCTS uses reduced limits (500 sims / 5s). Hybrid mode LLM failures use engine result or random move directly without search.
- **Prompts**: Compact, structured prompts. Full ~2280 chars, lite ~1270 chars, arbitration ~630 chars; both system prompts accept `include_analysis_tools=False` (llm_only mode) which also shortens them by ~220 chars. Legal moves carry tactical annotations (×capture, +check) computed via board-copy simulation; material balance is injected perspective-relative each turn; move history is truncated to the last `PROMPT_HISTORY_MAX_ITEMS` (24) moves for token control. Arbitration uses a "safety gate first, then收益排序" two-step rubric; candidates are A/B-randomized with sources hidden, each side carrying a comparable basis block. Piece names follow the red/black convention above.
- **requirements.txt**: Declares `PyQt6>=6.5`, `requests>=2.28`, `python-dotenv>=1.0`.
