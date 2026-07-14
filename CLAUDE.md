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

- **`game.py`** — `ChineseChessGame`: 10×9 board as `list[list[str]]`. Uppercase = Red, lowercase = Black, `.` = empty. `STANDARD_BOARD` is the initial position. `PIECE_SYMBOLS` maps piece chars to Chinese display characters. Key methods: `move_piece(fr,fc,tr,tc) → dict`, `get_all_legal_moves(player) → list[tuple]`, `_is_in_check(player)`, `_is_checkmated(player)`, `_has_any_legal_move(player)`. Move rules enforced in `_is_legal_move`. Self-check and king-facing prohibited by `_would_be_illegal`. Helper methods: `get_piece_at()`, `is_endgame()`, `count_pieces()`, `board_hash()`, `get_move_key()`, `get_board_copy()`.
- **`evaluation.py`** — Static board evaluation from Red's perspective (positive = Red advantage). Piece values in centipawns. Piece-Square Tables (PST) for all 7 piece types — positional bonuses based on chess principles (rooks on open files, knights in center, pawns advancing). Mobility scoring via legal move counts. King safety checks. Pattern detection: dangerous knight (卧槽马/挂角马), double rooks/cannons (双车错/重炮), central cannon threat (当头炮). `evaluate_move_ordering()` for search move prioritization (MVV-LVA).
- **`search.py`** — `SearchEngine`: Alpha-Beta search with iterative deepening. Quiescence search (captures only) to prevent horizon effect. Check extensions (+1 depth when king is in check). Move ordering: captures first (MVV-LVA), killer moves, history heuristic. Time-limited search with early cutoff. Fast leaf evaluation (`_fast_eval`) avoids expensive `get_all_legal_moves()` calls on hot path. Default depth 4, ~3000 nodes/sec in Python.
- **`openings.py`** — Opening book with 19 standard Chinese Chess opening lines (中炮对屏风马, 顺炮, 列炮, 仙人指路, 飞相局, 起马局, 过宫炮, 士角炮). Prefix-based lookup with weighted random selection. Covers first 4-6 half-moves. `get_opening_move(history) → Optional[Move]`.
- **`models.py`** — `ModelInfo` dataclass: `id, name, type, endpoint, model, api_key, tools_choice, system_prompt, options`. Factory: `from_dict(data)`.
- **`constants.py`** — Board size, AI timeout/retry/truncation constants, search engine config (depth, time limit, quiescence depth), opening book toggle, AI mode constants.
- **`prompts.py`** — `MOVE_PIECE_TOOL` (OpenAI function-calling dict), `HUMAN_MODEL` sentinel, `get_system_prompt()`, `build_move_prompt()`, `format_legal_moves()`. Tool calling is **always enforced**. Enhanced prompt includes: legal moves list grouped by piece type, few-shot examples (中炮 and 屏风马), 4-step thinking methodology with threat analysis, improved error recovery instructions.

### `ai/` — AI API interaction

- **`manager.py`** — `AIManager`: queue (`deque`), busy flag, active worker tracking, `_cancel_version` for stale-response rejection. `shutdown()` cancels worker + `thread.join(3s)`.
- **`worker.py`** — `AIWorker(QRunnable)`: runs in `threading.Thread` (not QThreadPool) so HTTP session can be cancelled. Uses `requests.Session(trust_env=False)`. Sends OpenAI-compatible `/v1/chat/completions` with `tools` array. Auto-detects LM Studio (port 1234 or `lmstudio` in URL) for think param handling. Adds `Authorization: Bearer {api_key}` header when key is set. Response parsing: tool_calls first, fallback to regex via `parser.py`. `AIWorkerSignals.finished` pyqtSignal delivers results to main thread.
- **`parser.py`** — `parse_coordinates_from_text(text)`: regex `[A-I]\d{1,2}` extracts first two coordinate pairs as `(from, to)`.

### `app/` — orchestration

- **`controller.py`** — `GameController`: central state machine. Owns `game`, `ai_manager`, search engine, opening book, stats, timers. Version-based invalidation (`game_version` incremented on reset; stale AI responses discarded). **Three AI modes**: `hybrid` (LLM + search, default), `search_only` (pure Alpha-Beta), `llm_only` (original behavior). AI move flow: check opening book → if pure search mode, run Alpha-Beta → build enhanced prompt with legal moves → create `AIWorker` → `on_ai_finished` → parse coords → execute move → advance turn. Fallback chain: LLM failure → search engine → random move. Retry: up to 3 with exponential-ish backoff. Non-retryable errors (HTTP 4xx, cancelled, missing endpoint) skip retries → search fallback.
- **`protocols.py`** — `MainWindowProtocol(Protocol)`: structural typing for `GameController.main`, breaks circular dependency.

### `services/` — cross-cutting

- **`models.py`** — `ModelManager.load()`: reads `models.json`, groups by `-p1` / `-p2` suffix. Falls back gracefully.
- **`logging.py`** — `LogManager`: HTML-colored log via `QTextEdit`.

### `ui/` — PyQt6 desktop GUI

- **`window.py`** — `MainWindow`: fixed 1200×900, three-panel splitter layout. Creates all managers + controller, wires signals. Left panel collapsible via `QStackedWidget`. New signal handlers: `on_ai_mode_changed`, `on_search_depth_changed`, `on_opening_book_changed`.
- **`board.py`** — `BoardWidget(QWidget)`: custom-painted board. Human input via `mousePressEvent` → emits `move_made` pyqtSignal. `capture_board_image()` grabs widget as JPEG base64 for vision mode.
- **`panel.py`** — `setup_left_expanded(parent)`: builds model combos, AI engine controls (mode dropdown, search depth spinner, opening book checkbox), AI control checkboxes (vision mode, think/no_think, disable think), action buttons, status/stats labels.
- **`theme.py`** — Window dimensions, splitter ratios, dark QSS stylesheet.

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

## `models.json`

Model pool config. Each entry: `id` (suffixed `-p1` = Red, `-p2` = Black), `name` (display), `type` (`lmstudio`|`deepseek`|anything OpenAI-compatible), `endpoint`, `model`, `api_key`, `tools_choice`, `system_prompt`, `options`. LM Studio entries can omit `model` (uses endpoint default). No `api_key` needed for local LM Studio.

## Key design decisions

- **Hybrid AI**: LLM provides strategic understanding; MCTS search provides tactical verification and precision. On LLM failure, MCTS takes over (no more random moves).
- **Legal moves in prompt**: All legal moves are formatted in the prompt grouped by piece type. LLM selects from the list instead of guessing coordinates, drastically reducing error rate.
- **Opening book**: 19 standard opening lines cover the first 4-6 half-moves, saving tokens and ensuring strong opening play.
- **Always tool calling**: No UI toggle. Prompts only instruct function calling. Parser exists only as fallback for models that return text despite receiving tools.
- **Thread not threadpool**: AIWorker uses raw `threading.Thread` so `requests.Session.close()` can abort in-flight HTTP from `cancel()`.
- **Version gating**: Every AI request carries `game_version`. Reset/pause increments it. Stale-response rejection prevents race conditions from overlapping AI calls.
- **Chinese UI**: All UI text and prompts are in Chinese. No i18n infrastructure.
- **No persistence**: Game state is in-memory only. `QSettings` used only for left-panel collapsed state.
