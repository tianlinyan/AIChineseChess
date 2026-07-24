# AGENTS.md

本文件供 AI 编码代理阅读，假设读者对本项目一无所知。代码注释与 UI 文案均为中文，无 i18n 基础设施。

## 项目概览

中国象棋 AI 对弈桌面程序（PyQt6 GUI）。核心玩法是"LLM + 象棋引擎"混合决策：本地/远程大模型（OpenAI 兼容接口、DeepSeek、LM Studio）负责战略判断，Pikafish NNUE 引擎与自研 MCTS/Alpha-Beta 搜索提供战术精度，两者分歧时由第三方 DeepSeek 模型仲裁。支持人机对弈与 AI 对 AI 对弈，内置开局库、残局库（chessdb.cn 云查询）、多模态"视觉模式"（把棋盘截图发给多模态模型）。

- 入口：`main.py`（加载 `.env` 后启动 `ui.window.MainWindow`）
- 依赖：仅 `PyQt6>=6.5`、`requests>=2.28`、`python-dotenv>=1.0`（见 `requirements.txt`）
- 引擎二进制：`engines/pikafish.exe` + `engines/pikafish.nnue`（也可用环境变量 `PIKAFISH_PATH` 指定路径；找不到时静默回退到 MCTS）
- 纯 Python 项目，无构建步骤、无打包配置提交在仓库中（`*.spec`、`build/`、`dist/` 已在 `.gitignore`，但 `services/models.py` 有 `sys.frozen` 判断，说明曾用 PyInstaller 打包）

## 运行命令

```bash
pip install -r requirements.txt
python main.py
```

运行前需配置 `models.json`（见下文"模型配置"）。API 密钥通过 `.env`（复制 `.env.example`）或系统环境变量注入。

## 架构：五层单向依赖

高层只导入低层，绝不反向：

```
domain/ ──→ ai/ ──→ app/ ──→ ui/ ──→ main.py
                 services/ ──┘
```

修改代码时必须遵守这个依赖方向。`domain/` 是唯一不依赖任何框架（无 PyQt 导入）的纯逻辑层。

### `domain/` — 纯游戏逻辑（零框架依赖）

- `game.py` — `ChineseChessGame`：10×9 棋盘。**定向走法生成**（車/炮沿射线步进、馬日字验蹩腿、相田字验塞眼、士兵将定向分支；与旧 90 格暴力版经 `tests/compare_movegen.py` 3000+ 局面对拍等价）。`_is_in_check` 从将位**反向检测**（車炮射线/馬位/兵位，O(~20)）。Zobrist 增量哈希（`move_piece` 与搜索 make/unmake 自动维护；**外部直接替换 board 后必须调 `recompute_hash()`**）。将杀/困毙共用一次走法生成判定；自然限着（`NATURAL_LIMIT_MOVES`=120 步未吃子判和，将杀/困毙优先，仅对局层判定、搜索不感知）。将/帅位置缓存 `_king_pos`（缓存失效时自动全盘扫描回退）。`get_capture_moves` 为静止搜索专用
- `evaluation.py` — 静态评估（红方视角，约 40 个特征：子力 + PST + 机动性 + 兵结构 + 将帅安全 + 战术模式识别如卧槽馬/铁门栓等）。机动性特征仅在调用方提供走法数时计入（搜索叶节点为速度跳过）。`compute_material(board)` 是共享的子力统计函数，controller 提示词、worker 评估输出、UI 显示三处共用
- `search.py` — `SearchEngine`：Alpha-Beta + PVS（含根节点）+ 静止搜索 + 置换表（OrderedDict 实现的 LRU + Zobrist 键，约 1.2 万节点/秒）。静止搜索被将军时搜全部应将走法（防将杀漏判，额外深度上限 `QS_EVASION_EXTRA_DEPTH`），非将军时只搜 `get_capture_moves` 生成的吃子。迭代加深每层完整重排。NMP 参数 R=2/最小深度 6（默认深度 ≤5 下实际不触发）。**只作为 LLM 的工具**（`search_best_move`）被调用，controller 不直接用
- `mcts.py` — `MCTSEngine`：UCB1 蒙特卡洛树搜索。**真搜索**：Selection 下降在工作局面副本上真实走子（`SearchEngine._make_move`），Expansion/Simulation 作用于叶局面。**由 controller 使用**，负责战术验证、hybrid 模式引擎先行、各类兜底
- `pikafish.py` — `PikafishEngine`：Pikafish UCI 协议封装，守护线程异步搜索 + `pyqtSignal` 回主线程。引擎 stdout 由独立 reader 线程读入行队列，按截止时间 `queue.get(timeout=)` 取行（引擎静默挂起也不会永久阻塞）；close/kill 先杀进程再收锁。controller 的首选引擎，优先级 Pikafish → MCTS → 随机走法
- `egtb.py` — 残局库：≤10 子用本地启发式（`EGTB_MAX_PIECES`）；≤6 子可查 chessdb.cn 云库（`EGTB_CLOUD_MAX_PIECES`，`chessdb.php?action=queryall` API，解析管道文本 note 的 `(W/D/L-M-NNNN)` 胜负与 DTM）。正缓存 5 分钟 TTL（上限 5000）+ **负缓存 60s（上限 5000）+ 连续失败熔断 120s**；`probe(..., allow_cloud=False)` 供搜索/MCTS 叶节点使用（**搜索循环内禁止同步联网**）。注意：**云查询当前未接入生产路径**（搜索/MCTS/兜底全部 `allow_cloud=False`），`probe_cloud` 已实现并实测可用，留待 UI 层按需接线。本地 `_can_win` 结合防守方士象数量（单車vs士象全=和、单馬必胜孤将、双炮必胜孤将、单卒仅过河未到底可胜孤将）
- `openings.py` — 17 条标准开局谱，按走子历史前缀匹配加权随机选取（线名/着法标注按标准路数校正：红 N 路=col(9−N)，黑 N 路=col(N−1)）
- `fen.py` — 共享 FEN 生成（`board_to_fen` / `game_to_fen`），供 pikafish 和 egtb 使用，勿再各自重复实现
- `prompts.py` — 系统提示词（完整版/精简版均接受 `include_analysis_tools` 参数）、仲裁提示词、合法走法格式化（带 ×吃子/+将军 战术标注，将军>吃子>其他 排序）、工具定义（`DEFAULT_TOOLS` 三个工具；`TOOLS_BASIC` 仅 `move_piece`，用于 llm_only 模式和仲裁）
- `constants.py` — 全部可调常量与坐标格式化工具函数。**注意：`SEARCH_MAX_DEPTH`（UI 上的"搜索强度" 1~6）不是 Alpha-Beta 深度**，它映射到 MCTS 模拟次数（500~3000，见 controller `_DEPTH_SIMS_MAP`）和 Pikafish 时限（强度×3 秒，封顶 `MCTS_TIME_LIMIT`=15 秒）
- `models.py` — `ModelInfo` 数据类

### `ai/` — LLM API 交互

- `worker.py` — `AIWorker`：普通类（不再继承 QRunnable——实际从未进 QThreadPool），在裸 `threading.Thread` 中运行。实现最多 4 轮的多轮 agentic 循环：LLM 可先调 `search_best_move`/`evaluate_position` 再调 `move_piece`。429/408 限流按可重试错误处理（"限流错误："前缀），其余 4xx 不可重试。错误前缀统一 `ERROR:`。注意：`cancel()` 的 `Session.close()` 不能可靠中断 in-flight 请求，过期响应靠 `cancel_version` 丢弃兜底
- `manager.py` — `AIManager`：worker 生命周期管理 + `cancel_version` 过期响应拒绝
- `parser.py` — 坐标正则解析（`[A-I]\d{1,2}`），最后兜底手段。优先匹配 `move_piece(...)` 文本样式；否则取**最后**两个坐标（LLM 思考文本常先引用引擎走法再给出自己的决定，取前两个会张冠李戴）。worker 调用时还会排除 `[Tool: ...]` 工具结果中的坐标干扰

### `app/` — 编排层

- `controller.py` — `GameController`：中央状态机。三种 AI 模式：`hybrid`（默认：引擎先行 → 结果注入 LLM 提示词作为"引擎参考走法" → LLM 最终决策 → 不一致时触发 DeepSeek 仲裁）、`search_only`、`llm_only`。负责 Pikafish 异步生命周期、版本门控、重试逻辑（不可重试错误直接跳过）、兜底链（LLM 失败 → 引擎结果 → 随机走法，连续随机上限 3 次）。要点：
  - **引擎链全异步**：Pikafish 不可用时 MCTS 也在后台线程跑（`_start_mcts_async`，先快照棋盘），结果经 `_PikafishRelay` 信号回主线程；主线程无任何同步搜索
  - **双层版本门控**：relay 回调同时校验 `game_version` + `cancel_version`；过期回调只记日志，绝不 `_finish_ai_move()`（防止清掉新对局状态导致同回合双走）
  - **hybrid 决策顺序**：LLM 走法先做合法性预检（非法直接用引擎兜底）→ 再分歧检测 → 仲裁。仲裁候选 A/B 随机化且隐去来源，双方各附依据（LLM 推理摘要 / 引擎匿名依据），结果强制二选一，缺省采纳引擎
  - **引擎信任分级**：Pikafish 参考走法提示"默认采信"，MCTS 兜底降级为"弱参考"
- `protocols.py` — `MainWindowProtocol`：结构化类型，打破 controller 与 UI 的循环依赖

### `services/` — 配置与日志

- `models.py` — `ModelManager`：加载 `models.json`，解析 api_key 中的 `${VAR_NAME}` 环境变量引用（缺失时统一打印一次警告），按 `-p1`/`-p2` 后缀分组到红/黑方下拉框
- `logging.py` — `LogManager`：带时间戳的 HTML 着色日志输出到 `QTextEdit`。块数上限 `LOG_MAX_BLOCKS`（constants.py，超出裁最旧，防长对局拖慢）；消息 HTML 转义（防 LLM 原文破坏渲染）；仅滚动条本在底部时才跟随滚动

### `ui/` — PyQt6 界面

`window.py`（主窗口；走子历史单 `QLabel` 文本刷新；Pikafish 初始化经 `QTimer.singleShot(0, ...)` 推迟到事件循环）、`board.py`（棋盘控件；截图用 `render()` 而非 `grab()`）、`panel.py`（侧面板）、`theme.py`。

## 模型配置（`models.json`）

仓库只提交 `models.json.example`；`models.json` 含密钥，已被 `.gitignore` 排除。约定：

- `api_key` 支持 `${VAR_NAME}` 环境变量引用
- `id` 以 `-p1` 结尾 → 只出现在红方下拉框；`-p2` → 只出现在黑方
- `id: "arbitration"` 或 `type: "deepseek"` 的模型自动选为仲裁裁判
- `type: "lmstudio"` 触发 LM Studio 专属 `think` 参数处理；`type: "deepseek"` 在请求体中使用 `think` 字段
- `system_prompt` 字段覆盖默认系统提示词；`tools_choice` 映射到 API 的 `tool_choice` 参数

## 棋子命名约定

字母大小写区分阵营：大写=红，小写=黑。唯一权威映射在 `domain/constants.py` 的 `PIECE_SYMBOLS`：

| 字母 | 红方 | 黑方 |
|------|------|------|
| K/k | 帥 | 将 |
| A/a | 仕 | 士 |
| B/b | 相 | 象 |
| N/n | 馬 | 馬 |
| R/r | 車 | 車 |
| C/c | 炮 | 炮 |
| P/p | 兵 | 卒 |

## 代码风格约定

- 全部 Python 3，类型标注普遍使用（`Optional[tuple]` 等）
- 模块 docstring、注释、日志、UI 文案一律中文
- 常量集中在 `domain/constants.py`，新增可调参数应放这里而不是散落各处
- 走法坐标对外用 `A0`~`I9` 格式，转换统一走 `format_coord` / `parse_coord` / `format_move`
- 并发纪律：UI 操作只能在 Qt 主线程；worker/Pikafish/MCTS 的结果经 `pyqtSignal`（`_PikafishRelay`）或版本门控回主线程。每个 AI 请求携带 `game_version`（重置/暂停时自增），再加 `cancel_version` 双层防止过期响应竞态——Pikafish/MCTS 的 relay 回调两层都校验
- worker 内做搜索/评估时必须用临时 game 对象：同步 `_king_pos` 缓存，且 board 被直接替换后调 `recompute_hash()` 重建 Zobrist，绝不动 `self.game.board`

## 测试

无测试框架、无 CI。`tests/` 下是纯 python 验证脚本（不依赖 pytest）：

- `python tests/smoke_engine.py` — 无 GUI 冒烟：走法数、Zobrist 增量一致性、将军检测与暴力法交叉对比、Alpha-Beta/MCTS 战术局面、本地 EGTB、开局库、自我对弈。**任何改动后必须通过**
- `python tests/compare_movegen.py` — 走法生成对拍：与 `tests/baseline_movegen.jsonl`（3000+ 局面基线）逐集合比对，改走法生成/将军检测后必须 100% 一致
- `python tests/movegen_baseline.py` — 重新采集基线（仅在确认旧实现正确时才运行）

改动后至少应保证 `python -m py_compile <改动文件>` 通过、`tests/smoke_engine.py` 全绿、程序能启动（`python main.py`）。

## 安全注意事项

- `models.json` 和 `.env` 含 API 密钥，已在 `.gitignore` 中，**绝不提交**；示例文件用占位符
- 密钥只通过 `${VAR_NAME}` 引用间接出现在配置里，代码中不得硬编码密钥
- 程序会向外发起网络请求：LLM API（用户配置的 endpoint）、chessdb.cn 残局云库
- 对局状态仅内存保存，无持久化（`QSettings` 只存左面板折叠状态）

## 相关文件

- `CLAUDE.md` — 面向 Claude Code 的同类指南，含更详细的 AI 决策流程图与设计决策清单，内容与本文件保持同步，修改架构时应一并更新
- `中国象棋程序竞赛规则.html` — 竞赛规则参考文档（本地文件，不入库）
