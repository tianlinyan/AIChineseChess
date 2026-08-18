# AIChineseChess 代码审查报告（第二轮）

- 审查日期：2026-08-17
- 审查基线：`284b31e`（2026-08-14，第一轮审查 `docs/CODE_REVIEW.md` 的修复完成点）→ HEAD `d48789c`（2026-08-17）
- 审查范围：9 个提交，43 个文件，+5814/−2198 行。覆盖 domain / ai / app / ui / services / tests / scripts 全部改动文件
- 审查方法：逐文件通读（含 diff 对照基线）+ 上一轮修复项逐条回归核对 + 规则原文比对（`中国象棋规则.txt` 第二章第六节）+ 边界情况实证 + 全量测试套件运行

## 修复状态

2026-08-17 修复完成（同日提交）：

| 条目 | 修复内容 | 回归验证 |
|------|----------|----------|
| P1 | `domain/search.py` 走法循环截断置 `timed_out` 标志，部分搜索的 best 统一存 `TTFlag.LOWER_BOUND`（probe 的 LOWER_BOUND 分支仅在 score ≥ beta 时用作剪枝，语义安全） | 新增 `tests/test_tt_cutoff.py`（桩化递归确定性触发截断路径 + 完整搜索对照组）✅ |
| P2 | `ai/worker.py:704` 改 `tool_call.get('function') or {}`（键存在但值为 null 时 `.get` 默认值不生效） | 内联验证 null/缺失 function 均安全返回空坐标 ✅ |
| P3 | `ai/worker.py` options 透传过滤保留键（model/messages/stream/tools/tool_choice），附加参数（temperature/max_tokens 等）正常透传 | 内联验证 llama-server/deepseek 两种类型 ✅ |
| P4 | `domain/pikafish.py` `search_async` 的 `_top_moves` 重置移入 daemon 持锁段（与 `_search_locked` 同序） | 新增 `tests/test_pikafish_concurrency.py`（30 轮双局面并发）；旧代码 28/30 轮复现污染，修复后 30/30 通过 ✅ |
| P5 | 不修复（设计性条目：两套评估口径，工具结果已标注来源，LLM 可感知） | — |

修复后全量测试：smoke_engine / compare_movegen（3020 局面 0 不一致）/ test_perft / test_evaluation / test_incremental / test_notation / test_arbitration / test_commentary / test_tt_cutoff 全部通过。

---

## 一、总体结论

**本轮 9 个提交未发现新引入的 Critical/High 缺陷。** 四个新功能（AI 点评、传统棋谱记法、视觉 2x 超采样、显示 AI 思考过程开关）实现质量高，关键路径（阻塞调度、版本门控、暂停恢复、规则一致性）均经逐路径验证。上一轮全部修复项（A1/A2/A3、B1/B2、C1、D1/D2、E1-E3、F1、M-* 批次）逐条回归核对，**全部在位且语义未被本轮改动破坏**。

主要改动性质：
1. **新功能**：AI 点评（阻塞式、仅搜索/人类落子后触发、开局库跳过）、传统棋谱记法（`format_chinese_notation` + 走法元组第 8 字段存档）、视觉截图 2x 超采样（600px）、思考过程显示开关。
2. **Pikafish 全面接入**（d75fe71）：`search_atomic` 持锁原子搜索（消除 MultiPV 切换撕裂窗口）、引擎死亡自动 `restart`、`_stdin_lock` 独立写锁、reader 线程 + 行队列。
3. **代码精简**（611e35c，详见 `docs/SIMPLIFY_REVIEW.md`）：移除云 EGTB（生产本未启用）、死代码（TT 命中率统计、`reload_nnue`、`Accumulator.copy`、进度回调）、红黑引擎组 UI 去重。

---

## 二、上一轮修复项回归核对（全部在位 ✅）

| 条目 | 位置 | 核对结果 |
|------|------|----------|
| A1 非字符串坐标卡死 | `ai/worker.py:719-724`（`isinstance` 防线） | ✅ 在位 |
| A2 depth/top_n 钳制入 try | `ai/worker.py:467-470`（`_clamp_int`） | ✅ 在位 |
| A3 move_piece 校验+错误反馈 | `ai/worker.py:350-379`（`_validate_move` 快照隔离）+ 248-265（附合法示例自纠） | ✅ 在位 |
| B1 引擎死亡假可用 | `pikafish.py:404-417`（`_run` 区分进程死亡）+ `app/engine_bridge.py`（`start_search` 健康检查 `poll()`） | ✅ 在位 |
| B2 残留 bestmove 污染 | `pikafish.py` `_purge_lines`（isready/readyok 握手） | ✅ 在位 |
| C1 TT 杀分 ply 折算 | `search.py:140-162`（`_tt_score_to_relative`） | ✅ 数值再验证自洽：存储 `±(JIANGSHA/KUNBI)` 为 ply 无关常量，probe 按探测 ply 还原，杀/困分均正确 |
| D1 `_active_mcts` 单槽竞态 | `app/engine_bridge.py`（按线程 dict 管理） | ✅ 在位 |
| D2 启动握手阻塞主线程 | `app/engine_bridge.py`（`init_pikafish` 后台线程 + `init_done` 信号） | ✅ 在位 |
| F1 吃将走法防御 | `game.py:190-191` + `_append_if_legal` | ✅ 在位；`egtb_local.py` 生成器已同步改用 `is_in_check` 显式检测不可能局面（不再依赖吃将走法，F1 的语义依赖已解除） |
| E1/E2/E3 测试假通过 | `compare_movegen.py` / `train_nnue.py` / `smoke_engine.py` | ✅ 在位 |
| M-AI-1 取消接入搜索停止 | `worker.py:498,546,740-746`（`_active_search_engine` + `cancel()` 调 `stop()`） | ✅ 在位 |
| M-AI-2 每步总预算 | `worker.py:794-797`（`min(单轮超时, 剩余预算)`） | ✅ 在位 |
| M-AI-4 clear_queue 复位 busy | `ai/manager.py:25-33` | ✅ 在位 |
| M-AI-5 正式回复池 | `worker.py:129,320-328`（`_content_texts` 排除推理/工具结果） | ✅ 在位 |
| M-AI-6 _validate_move 快照 | `worker.py:366-372` | ✅ 在位 |
| M-GAME-3 from_snapshot(None) | `game.py:151`（`dict(king_pos or {})`） | ✅ 在位 |
| M-ENG-2 _top_moves 加锁 | `pikafish.py`（`get_top_moves`/`search_atomic` 持锁） | ✅ 在位 |
| M-ENG-3 車馬/車炮官和 | `egtb.py:199-203` | ✅ 在位 |
| M-UI-2 on_human_move busy 检查 | `controller.py` 入口防御 | ✅ 在位 |
| M-UI-4 多行日志折叠 | `services/logging.py`（escape 后 `\n`→`<br>`，本轮又补 CRLF 归一化） | ✅ 在位 |
| M-TEST-1/2 perft 精确值/陈旧缓存 | `test_perft.py` / `smoke_engine.py` | ✅ 在位 |

---

## 三、新功能逐项验证

### 3.1 AI 点评（d48789c）— ✅ 无问题

- **阻塞语义**：`_after_move_finished`（`controller.py:905-920`）在落子后统一调度——终局 → 点评阻塞 → 下一手。点评未完成不调度下一手，符合"待解说完成后方可进行下一步"。
- **触发条件**（`_needs_commentary`，938-951 行）：仅人类落子或 `search_only` AI 落子；hybrid/llm_only 落子时 LLM 已在思考日志解释，不重复；开局库标准谱着跳过（`source == '开局库'`）。
- **模型选择**（`_commentary_model_for`，953-969 行）：AI 方用己方模型；人类方用对手 AI 模型；双人类用第一个非仲裁模型；无可用模型 → 不阻塞直接继续。
- **失败/取消路径**：点评失败（LLM error）→ 记日志、清状态、恢复调度，**永不卡死对局**；暂停打断 → `on_commentary_finished` 版本门控丢弃旧回调（`worker is not self._commentary_worker` 识别在飞 worker），保留 `_commentary_mover`，恢复后 249-251 行重新触发。
- **worker 侧**：纯文本任务以 `tools=()` 启动，payload 不发送 tools/tool_choice 字段（部分端点不接受空数组），模型若仍返回 tool_calls 直接结束循环取正式文本（`worker.py:230-231`），不烧满 4 轮误报"未找到有效走法"。
- **测试**：`tests/test_commentary.py` 覆盖触发/阻塞/恢复/暂停重触发/开关关闭，全绿。

### 3.2 传统棋谱记法（611e35c）— ✅ 与规则原文逐条一致

对照 `中国象棋规则.txt` 第二章第六节（144-169 行）逐条核对 `domain/constants.py` 的 `format_chinese_notation`：

- 四字结构、红中文/黑阿拉伯（路号与竖走步数）、己方视角进退平、第四字（横走/馬士相记目标路，竖走记步数）——全部一致。
- 同路同类子前/后（士/相除外，154 行）——一致。
- 兵卒消歧四级规则（156-162 行）——一致。特别核对了边界情况：**单兵路 + 另两路各 ≥2 兵**时，规则 160 行"若两条竖线的一方兵数量各≥2"是局面级条件（未排除单兵路），实现按字面要求对单兵路也使用前/后+路号（实证：`前三平四`）。实现忠实于规则原文。
- 4~5 兵路（前兵/二兵/三兵/四兵/后兵）优先于两路条件——规则未显式声明优先级，实现取更具体条件优先，合理。
- **存档定格**：走法元组第 8 字段在落子时定格传统记法（`game.py` `move_notation` 优先返回存档值），前/中/后消歧不随后续棋盘变化漂移——`test_notation.py:310-320` 验证。
- 测试：`tests/test_notation.py`（325 行，含竞赛规则原文棋例）全绿。

### 3.3 视觉 2x 超采样（508f247）— ✅ 无问题

- `VISION_IMAGE_SCALE=2`、`VISION_IMAGE_MAX_WIDTH=600`（原 300）、JPEG 质量 80。
- `board.py` 新增 `y_offset` 纵向居中（固定 590×650 下棋盘垂直居中）；**鼠标命中检测已同步换算**（175/178 行 `(y - y_offset) / cell_size`），无点击偏移风险。

### 3.4 显示 AI 思考过程开关（111af30）— ✅ 无问题

- `show_think_check`（默认关闭）→ `controller._show_think_enabled()`（1237-1241 行）→ 思考日志按开关显示 `full_text`（含推理）或 `content_text`（仅正式回复）。LLM 与仲裁两条日志路径共用同一判断（678/1518 行）。

### 3.5 Pikafish 全面接入与并发加固（d75fe71）— ✅ 无问题

- `search_atomic`（232-277 行）：一次持锁内完成"切 MultiPV → 搜索 → 恢复 1 → 导出候选快照"，消除 set/search/restore 撕裂窗口与 `_top_moves` 跨搜索 TOCTOU；锁超时/引擎失败返回 None → 调用方回退本地 AB。
- `restart`（517 行起）：进程死亡后重新 Popen + 握手 + 棋力配置；守卫检查 `poll()` 真实存活而非仅看 `_available` 标志。
- `_stdin_lock` 独立于 `_lock`：`stop()` 故意不取 `_lock`（搜索线程整个搜索期间持锁），写锁只保护 stdin 写入，不破坏 stop 的"尽快返回"语义。
- `close`/`_kill_proc` 先杀进程再收锁：reader 线程 EOF → 哨兵唤醒持锁的等待方，close 不被挂起搜索拖住。
- LLM 工具路径（`worker._run_search_pikafish`）：depth 2~8 映射 movetime 2.6~7.4s（封顶 8s），MultiPV 候选走子方视角 → 红方视角统一，失败静默回退本地 AB 且已取消时不再启动全量 AB（防陈旧 worker 空耗 CPU）。

### 3.6 EGTB 精简（611e35c）— ✅ 无问题

- 云查询路径整体移除（生产调用者本就全部 `allow_cloud=False`），`probe` 签名保留 `piece_count`/`material_counts` 参数。
- `probe` 内部自守卫子力上限（`egtb.py:35`）——`search.py` `_fast_eval` 移除外层 `EGTB_MAX_PIECES` 判断后语义不变（已核对）。
- `material_counts` O(1) 快速否定（双方均有攻击子 → None）与 `_local_egtb` 的判定范围一致（启发式本就只处理单方攻子构型，100-115 行），不会漏判。
- `egtb_local.py`：不可能局面检测改用 `is_in_check(3 - mover)`（语义宽于旧实现但仅覆盖对局不可达状态，可达局面值与现网表一致）；表加载段加 `_tables_lock`（并发查询安全）；生成期子表缺失由静默判和改为显式 `raise`（防错表持久化）。

### 3.7 其余改动 — ✅ 无问题

- **models.json 加载**（`services/models.py`）：`app_base_dir()` 与 `main.py` 共用（.env/models.json 定位一致）；`utf-8-sig` 兼容 BOM；玩家下拉框回退列表排除仲裁裁判（原 `list(self.models)` 会把仲裁混入）。
- **日志**（`services/logging.py`）：CRLF/CR 归一化后再 escape（防 `\r` 原样入 HTML）；颜色查找链"原始 level → 大写 → DEFAULT"修复 `'red'` 恒回退 DEFAULT 的隐患。
- **左侧面板**（`ui/panel.py`）：红黑引擎组经 `_build_engine_group` 去重（约 120 行重复消除）；合并槽函数 `side` 参数区分（`window.py:219-231`）；旧槽（`on_red_ai_mode_changed` 等）已删除且无残留引用（grep 验证）；`start_btn.clicked` 用 lambda 屏蔽 `clicked(bool)` 的多余参数。
- **引擎类型更名** lmstudio→llama-server：全库无 `lmstudio` 残留引用（grep 验证）；`think` 参数移除，worker 不传递 think/enable_thinking（768-769 行注释说明）。
- **max_tokens 移除**（f02c9d5）：qwen3.8 思考模式推理 5000+ 字符会先耗尽输出上限导致 tool_calls 截断，移除正确；`models.json` 的 `options` 仍可对个别模型显式覆盖。
- **死代码清理**：`reload_nnue`、`Accumulator.copy`、TT 命中率统计、`_canonicalize`（内联为 `_pick_frame` 单调用点）、进度回调——grep 验证无悬挂引用；`nnue` 累加器 make/unmake 收敛为 `_sync_nnue_acc` 共享助手（sign 参数化），调用点签名一致。
- **`is_endgame()` O(1) 收敛**：`game.py:724` 新增，search/mcts 转用，`test_incremental.py:206` 一致性测试覆盖。

---

## 四、遗留问题（基线前已存在，本轮未引入恶化）

以下条目在基线 `284b31e` 前即存在（经 git diff 确认本轮未触及）。**P1–P4 已于 2026-08-17 修复**（见文首"修复状态"），原文保留作为问题记录：

### P1【Medium】时间截止时 TT 存储被污染 — `domain/search.py:419-490` ✅ 已修复

走法循环被时间检查中断（419 行 `break`）后，代码落入 480 行后的统一存储路径：`best`（已搜索子集的最大值）按 `best > orig_alpha ? EXACT : UPPER_BOUND` 存入 TT。但循环被截断时**未搜索的走法可能更优**，真实分值 ≥ best，正确标记应为 LOWER_BOUND（或不存）：

- `best > orig_alpha` 存 EXACT → 后续 probe 拿到偏低的"精确值"；
- `best ≤ orig_alpha` 存 UPPER_BOUND → 声称"真实分值 ≤ best"，与事实相反。

TT 跨步复用（容量 100 万条，搜索间不清空），污染条目会影响后续走法的搜索质量。作者已意识到空循环情形（481 行注释"超时截断，未搜索任何走法 —— 不存 TT，避免污染"），但遗漏了部分搜索的截断情形。

**触发**：任何达到时间上限的搜索（生产搜索普遍如此）的最后一个迭代。
**影响**：仅搜索质量（可能低估某些局面），无崩溃/非法走法风险。
**建议修复**：`break` 时置 `timed_out` 标志；截断后若 `best != -inf` 存 LOWER_BOUND（或跳过存储）。

### P2【Low】`_extract_move_from_call` 缺 null-function 防线 — `ai/worker.py:704` ✅ 已修复

`tool_call.get('function', {})` 在键存在但值为 null 时返回 None（默认值不生效），`func.get('name')` 抛 AttributeError。当前不可达（agentic loop 239 行 `tool_entry.get('function') or {}` 已预过滤），属纵深防御缺口。建议改为 `tool_call.get('function') or {}` 与调用方一致。

### P3【Low】`options` 透传可覆盖关键 payload 键 — `ai/worker.py:770` ✅ 已修复

`payload.update(self.model_info.options)` 在 model/messages/stream/tools/tool_choice 全部设置之后执行。`models.json` 的 `options` 若误含 `"model"`/`"tools"` 等键会静默覆盖程序逻辑。`models.json` 为用户自管文件，非安全漏洞，属配置陷阱。建议在 update 后恢复关键键，或文档注明 `options` 仅用于 temperature 等附加参数。

### P4【Low/Info】`search_async` 与 `search_atomic` 重叠时 `_top_moves` 可能瞬态混杂 — `domain/pikafish.py:373-374 vs 243-244` ✅ 已修复

`search_async` 的 daemon 线程内联搜索步骤（不调用 `_search_locked`），其 `_top_moves_dict` 重置发生在**调用线程**（373 行，锁外、线程启动前）；若期间 `search_atomic` 完成并 finalize，daemon 的 finalize 会把两者条目混入 `_top_moves`。`search_async` 自身不读 `_top_moves`，且每次 `search_atomic`/`search` 在锁内先重置（243 行），故当前无实际误读路径——但依赖"hybrid 流程中引擎搜索先于 LLM 启动"这一时序约定（`worker.py:647-649` 注释）。建议 daemon 改为复用 `_search_locked`（锁内重置），消除对时序约定的隐式依赖。

### P5【Low，第一轮 M-SEARCH-2 延续】两套评估口径

`worker._run_evaluate` 在 Pikafish 不可用时回退 `evaluate()`（含机动性项），与引擎内部 `evaluate_fast` 口径不同。工具结果已标注来源（`Pikafish NNUE`/`手工评估`），LLM 可感知，影响有限。

---

## 五、已验证无问题（本轮重点核查项）

- [x] 版本门控体系（`game_version` + `cancel_version` + `_round_guard`）：点评/仲裁/引擎回调全部路径经 `_check_callback_valid` 统一门控，陈旧回调仅记日志不重置 busy；点评 worker 以对象身份（`worker is not self._commentary_worker`）识别在飞任务，暂停取消旧 worker 后恢复重触发不丢状态。
- [x] 5 条走子路径（开局库/LLM/随机/引擎兜底/仲裁）共用 `_complete_move` 收尾，簿记/刷新/终局/调度无遗漏分支。
- [x] 走法生成/将军检测核心（`game.py` 全量）：马腿/炮架/过河横攻/将帅对面/`_would_be_illegal` 临时移动恢复 `_king_pos` 缓存——与基线一致，`compare_movegen` 3020 局面 0 不一致、perft 黄金值精确通过。
- [x] 增量缓存（Zobrist/PST/子力/NNUE 累加器）make/unmake 配对：search 与 game 共用 `_sync_move_caches`，NNUE 累加器异常静默 + AI_DEBUG 诊断，`test_incremental` 全绿。
- [x] 仲裁解析（`worker.py:380-429`）：候选标签正则 `([AB])\b` 不误匹配坐标列字母；候选坐标字面量取最后出现；`allowed_moves` 限死候选，第三走法由 controller 侧采纳引擎走法（`test_arbitration` A3 分支覆盖）。
- [x] UCI 坐标转换（`_uci_to_tuple`：内部行 = 9 - rank）与 FEN（row 0 = 黑方底线）方向约定一致，`fen_to_board` 与 `board_to_fen` 互逆。
- [x] 安全细节：`trust_env=False`、DeepSeek 过滤 `image_url`（worker 侧安全网 779 行）、HTTP 错误响应截断 500 字符（防 Bearer key 回显入日志）、408/429 归入限流退避而非客户端错误。
- [x] 线程模型：AIWorker 裸线程 + `requests.Session.close()` + 版本门控丢弃陈旧响应；EngineBridge 信号中继；MCTS/Pikafish 回调均验证双版本号。

---

## 六、测试基线（2026-08-17 运行）

| 套件 | 结果 |
|------|------|
| `smoke_engine.py` | ✅ 全部通过 |
| `compare_movegen.py` | ✅ 3020 局面 0 不一致 |
| `test_perft.py` | ✅ All passed（perft(2)=1920、perft(3)=79666 精确值） |
| `test_evaluation.py` | ✅ All passed |
| `test_incremental.py` | ✅ All passed（含 `is_endgame` O(1) 一致性） |
| `test_notation.py`（新增） | ✅ 全部通过 |
| `test_arbitration.py`（新增，分支级） | ✅ 全部通过 |
| `test_commentary.py`（新增） | ✅ ALL PASSED |
| `test_tt_cutoff.py`（新增，P1 回归） | ✅ 全部通过（截断存 LOWER_BOUND + 完整搜索 EXACT 对照） |
| `test_pikafish_concurrency.py`（新增，P4 回归） | ✅ 30 轮并发无 `_top_moves` 污染（旧代码 28/30 轮复现，已验证测试有效性） |
| `test_egtb.py` | ⚠️ 第 7 节 `PermissionError`——**沙箱环境限制**（`tempfile.TemporaryDirectory` 内 chmod 被拒），非代码缺陷；已用预建目录驱动单独运行第 7 节，全部 PASS |

---

## 七、建议

1. ~~P1/P2/P3/P4 修复~~ — 已全部完成（见文首"修复状态"），各附回归测试。
2. P4 的 daemon 仍内联 UCI 通信步骤（未完全复用 `_search_locked`）：因 `_search_locked` 不区分"非法走法/超时/进程死亡"三种失败原因（daemon 的回调错误文案依赖该区分），完整复用需改造其返回契约；锁内重置已消除竞态，剩余重复属可接受的诊断性差异。
3. 新增的 `arbitration_pressure_test.py`/`llm_pressure_test.py` 依赖真实 API（需 key），未纳入本轮无头基线；建议 CI 保持分支级测试（`test_arbitration.py` 默认模式）为准。
4. `docs/SIMPLIFY_REVIEW.md` 记录的精简项与本轮 diff 完全吻合，精简过程未引入行为变化（测试全绿佐证）。

---

*本报告基于 284b31e..d48789c 共 9 个提交的完整 diff 与全量通读；上一轮报告见 `docs/CODE_REVIEW.md`。*
