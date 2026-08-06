# CLAUDE.md

## Run the app

```bash
pip install -r requirements.txt
python main.py
```

测试脚本（均为普通 Python 脚本，非 pytest）：
```bash
python tests/smoke_engine.py      # 无 GUI 冒烟：走法生成/AB/MCTS/EGTB/开局库/哈希
python tests/compare_movegen.py   # 走法生成与 baseline_movegen.jsonl 对拍
python tests/test_perft.py        # Perft 走法计数基准（黄金标准）
python tests/test_evaluation.py   # 评估函数正确性/对称性/增量等价
python tests/test_incremental.py  # 增量缓存一致性
```

改动后按 `.claude/skills/verify/SKILL.md` 的矩阵选择验证：`domain/`（走法/搜索/评估）→ 冒烟 + 对拍；`domain/openings.py` → 追加开局线逐线合法性；`domain/pikafish.py` → 冒烟 + 引擎启停；`app/ ui/ main.py` → GUI 启动；`ai/` → 冒烟。

## Architecture

依赖方向：`domain/` 是唯一基座。`services/`（Qt 工具 + 模型配置）与 `ai/`（LLM 工作器）只依赖 `domain/`；`app/`（controller）依赖 `domain/` + `ai/`；`ui/` 与 `main.py` 在最上层。上层从下层导入，永不反向。

### 模型配置（models.json）

- 顶层 `models` 数组（`models.json.example` 为模板）。`id` 后缀 `-p1`/`-p2` 决定归属哪一方玩家下拉框；`arbitration` 是仲裁模型，不出现在玩家下拉框。
- `api_key` 支持 `${ENV_VAR}` 占位符；`main.py` 启动时加载 `.env`（PyInstaller 打包后定位 exe 旁，与 `services/models.py` 一致）。
- `type`: `lmstudio`（OpenAI 兼容本地端点）/ `deepseek`（DeepSeek API — **不支持视觉**，控制器/AIWorker 对 deepseek 过滤 `image_url`）。

### 两个搜索引擎 — 角色分明

| 引擎 | 文件 | 使用者 | 角色 |
|------|------|--------|------|
| `MCTSEngine` | `domain/mcts.py` | Controller | 战术验证、引擎兜底 |
| `SearchEngine` | `domain/search.py` | LLM (via `search_best_move`) | LLM 按需深度分析 |
| `PikafishEngine` | `domain/pikafish.py` | Controller | 外部 NNUE，大师级；优先→MCTS 兜底 |

LLM 侧的工具调用（`domain/prompts.py` 的 `DEFAULT_TOOLS`）由 `ai/worker.py` 执行：`move_piece` 提交走法、`search_best_move` 实例化 `SearchEngine` 跑 Alpha-Beta、`evaluate_position` 静态评估。多轮 agentic loop 直到调用 `move_piece` 或达 `MAX_TOOL_TURNS=4`。

### AI 决策流程（Hybrid 模式）

```
开局库命中 → 直接落子
       ↓ 未命中
Pikafish (15s) → MCTS 兜底 → 引擎结果注入 LLM 提示词
       ↓
LLM 分析 + 引擎参考 → 一致→落子 / 分歧→DeepSeek 仲裁
```

`ai_mode`（`app/controller.py`，默认 `hybrid`）三种取值：`hybrid`（引擎先跑，结果注入提示词）、`search_only`（Pikafish 结果即最终走法）、`llm_only`（纯 LLM，无引擎参考，工具仅 `move_piece`）。

## Key design decisions

- **Hybrid AI**: 引擎（Pikafish/MCTS）提供战术精度；LLM 提供战略判断和最终决策权。分歧时 DeepSeek 仲裁。
- **Engine-first in hybrid**: 引擎先于 LLM 运行，引擎推荐立即可用，无需第二轮往返。
- **Legal moves in prompt**: 所有合法走法分组格式化为提示词，LLM 从列表中选择而非猜测坐标，大幅降低错误率。
- **Always tool calling**: 无 UI 开关。提示词指导函数调用。文本解析器（`ai/parser.py`）仅作为兜底。
- **Thread not threadpool**: AIWorker 使用原始 `threading.Thread`。`requests.Session.close()` 无法可靠中止进行中的 HTTP — 陈旧响应通过 `cancel_version` 门控丢弃。
- **Version gating**: 每个 AI 请求携带 `game_version`。重置/暂停递增它。`cancel_version`（来自 `AIManager`）提供第二层。Pikafish/MCTS 回调验证两者，陈旧回调仅记日志（不重置 busy 状态），防止重复走子。
- **Pikafish async via signal relay**: `EngineBridge`（QObject + `pyqtSignal`）桥接 daemon 线程结果到 Qt 主线程。MCTS 兜底也走同一条中继路径（`_start_mcts_async`），永不阻塞 UI。
- **King position cache**: `_king_pos` 缓存双方将/帅位置。`is_in_check`（从将位反向检测：射线/马腿/兵位）和 `_is_king_facing` 使用缓存，失效时自动全盘扫描回退。
- **LRU transposition table**: OrderedDict 淘汰替代旧 O(n) 内存分配。
- **Worker isolation**: `_run_search` 和 `_run_evaluate` 使用快照游戏对象，不触碰 `self.game.board`。
- **Chinese UI**: 全部中文。无 i18n 基础设施。
- **No persistence**: 游戏状态仅内存。`QSettings` 仅用于左侧面板折叠状态。
- **Vision mode**: 棋盘渲染为 QPixmap 以 JPEG base64 发送。**DeepSeek API 不支持 `image_url`**——控制器对 `model.type == 'deepseek'` 自动禁用视觉；AIWorker 对 DeepSeek 模型过滤 `image_url`。
- **Opening book**: 46 条标准开局线（`domain/openings.py`），前缀加权随机选择。
- **EGTB integration**: 本地启发式≤10 子；chessdb.cn cloud 查询≤6 子已实现但未接入生产——所有生产调用者使用 `allow_cloud=False`。
- **MCTS fallback**: 后台线程 MCTS 使用缩减限制。Hybrid 模式 LLM 失败直接用引擎结果或随机，不回落搜索。
- **Prompts**: 紧凑结构化提示词。合法走法带战术标注（×吃子、+将军）；子力平衡每回合透视注入；走子历史截断至最近 24 手。仲裁使用"安全门优先，收益排序"两步式评分。

## Piece Name Convention

权威映射在 `domain/constants.py` 的 `PIECE_SYMBOLS`。大写=红，小写=黑。UI 以红底/黑底圆形区分。

## Search Constants

`SEARCH_MAX_DEPTH` 是 UI 搜索强度 spinbox 的默认值（1~6），**非** Alpha-Beta 深度；它缩放 MCTS 模拟数和 Pikafish 时间（见 controller `_DEPTH_SIMS_MAP`）。
