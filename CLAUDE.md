# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run the app

```bash
python main.py
```

Requires: `PyQt6`, `requests`. No test suite or build system exists.

## Architecture

Five-layer dependency chain — higher layers import from lower, never reverse:

```
domain/  ──→  ai/  ──→  app/  ──→  ui/  ──→  main.py
                      services/ ──┘
```

### `domain/` — pure game logic, zero framework imports

- **`game.py`** — `ChineseChessGame`: 10×9 board. Uppercase = Red, lowercase = Black. Core move/checkmate logic.
- **`evaluation.py`** — Static evaluation from Red's perspective. Detects Chinese Chess patterns: 卧槽马/挂角马, 双车错/重炮, 当头炮.
- **`search.py`** — `SearchEngine`: Alpha-Beta + quiescence + check extensions. ~3000 nodes/sec.
- **`openings.py`** — 19 standard opening lines (中炮对屏风马, 顺炮, etc.).
- **`models.py`** — `ModelInfo` dataclass. `constants.py` — Board size, search config, AI constants.
- **`prompts.py`** — Tool-calling prompt builder with legal moves, few-shot examples. Tool calling always enforced.

### `ai/` — AI API interaction

- **`worker.py`** — Runs in raw `threading.Thread` (cancellable via `requests.Session.close()`). Sends OpenAI-compatible tool-calling requests. Auto-detects LM Studio for think param handling.
- **`manager.py`** — Worker lifecycle + stale-response rejection via version gating.

### `app/` — orchestration

- **`controller.py`** — `GameController`: central state machine. **Three AI modes**: `hybrid` (LLM + search, default), `search_only`, `llm_only`. Fallback chain: LLM failure → search engine. Retry: up to 3 (non-retryable errors skip). Version-gated to reject stale responses.
- **`protocols.py`** — `MainWindowProtocol`: structural typing for `GameController.main`, breaks circular dependency.

### `services/` + `ui/` — config loading, logging, PyQt6 GUI

### AI Decision Flow (Hybrid Mode — default)

```
Controller.make_ai_move()
  ├─ 1. Opening book lookup → if found, play immediately (saves tokens)
  ├─ 2. If search_only mode → MCTS search → best move
  │    └─ _mcts_search()                  # app/controller.py
  ├─ 3. Otherwise (hybrid/llm_only):
  │    ├─ format_legal_moves()          # domain/prompts.py
  │    ├─ build_move_prompt()           # domain/prompts.py (legal moves included)
  │    ├─ AIWorker(model, prompt, ...)  # ai/worker.py
  │    └─ threading.Thread(worker.run)
  │         └─ requests.post(endpoint)  # OpenAI-compatible API
  │              └─ signals.finished.emit(...)
  └─ 4. Controller.on_ai_finished()
       ├─ Hybrid mode: MCTS 验证 LLM 走法 → 最优走法
       ├─ Parse coords → game.move_piece()
       ├─ If illegal → MCTS search fallback (_fallback_to_search)
       ├─ If legal → update UI → schedule next turn via QTimer
       └─ If LLM total failure → _fallback_to_search()
```

### MCTS Search Engine Flow (主要搜索引擎)

```
MCTSEngine.search(game, player, priors)
  ├─ 构建根节点，展开所有合法走法
  ├─ 应用 LLM 先验（虚拟访问次数字段）
  ├─ 主循环（600次模拟或5秒上限）
  │    ├─ 1. Selection — 沿 UCB1 最大路径下降
  │    ├─ 2. Expansion  — 访问0次的叶节点展开子节点
  │    ├─ 3. Simulation — 用评估函数快速评分（无走法生成）
  │    └─ 4. Backprop   — 沿路径回传价值
  └─ 返回访问次数最多的走法
```

### Alpha-Beta Search Engine Flow（LLM 工具调用用）

```
SearchEngine.search(game, player)
  ├─ Iterative deepening (depth 1 → max_depth)
  │    └─ For each root move:
  │         ├─ _make_move (fast board update)
  │         ├─ _alpha_beta(depth-1, alpha, beta)
  │         │    ├─ Leaf (depth≤0) → _quiescence (captures only)
  │         │    │    └─ _fast_eval (no move generation)
  │         │    ├─ get_all_legal_moves → _order_moves
  │         │    └─ Recurse with alpha-beta pruning
  │         └─ _unmake_move (restore board)
  └─ Return best move found
```

## Key design decisions

- **Hybrid AI**: LLM provides strategic understanding; MCTS search provides tactical verification and precision. On LLM failure, MCTS takes over (no more random moves).
- **Legal moves in prompt**: All legal moves are formatted in the prompt grouped by piece type. LLM selects from the list instead of guessing coordinates, drastically reducing error rate.
- **Opening book**: 19 standard opening lines cover the first 4-6 half-moves, saving tokens and ensuring strong opening play.
- **Always tool calling**: No UI toggle. Prompts only instruct function calling. Parser exists only as fallback for models that return text despite receiving tools.
- **Thread not threadpool**: AIWorker uses raw `threading.Thread` so `requests.Session.close()` can abort in-flight HTTP from `cancel()`.
- **Version gating**: Every AI request carries `game_version`. Reset/pause increments it. Stale-response rejection prevents race conditions from overlapping AI calls.
- **Chinese UI**: All UI text and prompts are in Chinese. No i18n infrastructure.
- **No persistence**: Game state is in-memory only. `QSettings` used only for left-panel collapsed state.
