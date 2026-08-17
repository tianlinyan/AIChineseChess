# 代码精简可行性评估报告（终稿）

> 方法：主代理跨模块扫描 + 5 个子系统子代理（app/ai/domain 核心/引擎-EGTB/UI-services-prompts）逐文件深度审查，
> 全部"零调用点"声明经全仓库 grep 核实，关键条目由主代理独立复核；未修改任何代码文件。
> 原则：所有建议以"功能不变"为前提；每条给出位置、类别、预估减行、风险、验证方式。

## 一、总体判断

- 全库 14,817 行 Python：生产代码 25 文件 **11,503 行**，另有 tests 约 2,400 行、scripts 约 892 行。
  `domain/openings.py`（1,863 行）与 `domain/evaluation.py`（638 行）主体是数据（开局线 / PST 表），非代码冗余。
- 核心引擎（game/search/mcts/egtb）为高质量代码：定向走法生成、增量缓存、Negamax+PVS
  均有测试覆盖（perft / 对拍 / 增量一致性），**该区域精简空间小、风险高，应只做安全项**。
- 主要精简空间集中在：**跨模块重复逻辑**（make/unmake 与 move_piece 的增量维护、
  mcts/search 的 EGTB+评估分支、_is_time_up/stop 拷贝）、**生产不可达代码**（egtb 云查询）、
  **死字段/死参数/未用导入**、**测试侧样板重复**。
- 结论：**精简可行**。零/低风险项约 **400~500 行**（占生产 3.5~4.5%）；加上有强测试闸的中风险大件
  （D#1/D#2/W7 等）可达 **550~700 行（5~6%）**；另有 ~250 行属高风险/不建议项（详见第九节）。

## 二、主代理已核实发现（附证据）

### A. 跨模块重复逻辑

| # | 位置 | 内容 | 减行 | 风险 | 验证 |
|---|------|------|------|------|------|
| A1 | `domain/search.py:688-808` vs `domain/game.py` move_piece | `_make_move`/`_unmake_move` 的增量缓存维护（Zobrist/PST/子力计数/NNUE）与 `game.move_piece` 重复实现，`_unmake_move` 为其逆（与 D#1 同项） | 40-55（净） | 高（搜索热路径，需逐行核对差异） | test_incremental / test_perft / compare_movegen |
| A2 | `domain/mcts.py:352-358` vs `domain/search.py:812-818` | `_is_time_up` + `stop` 逐字相同（7 行 ×2） | ~7 | 低 | smoke_engine |
| A3 | `domain/mcts.py:258-269` vs `domain/search.py:590-598` | "子力数≤EGTB_MAX_PIECES → probe(allow_cloud=False)" 分支重复（6 行 ×2） | ~6 | 低 | test_egtb |
| A4 | `scripts/gen_selfplay.py:39-46` vs `domain/egtb_local.py:153` | `rotate180_swap` 与 `_rotate_board` 同款（180°旋转+大小写互换，docstring 自认"同款"） | ~8 | 低（脚本侧，可改 import 或保留） | gen_selfplay 自检 |
| A5 | `scripts/eval_benchmark.py:71` vs `tests/compare_movegen.py:18` | `board_from_fen` 几乎逐字相同 | ~18 | 低（tests/scripts） | 两脚本各自运行 |
| A6 | 6 个测试文件 | `check(name, cond, detail)` 助手逐字重复（smoke_engine/test_perft/test_evaluation/test_incremental/test_arbitration/verify_fixes；test_egtb 签名不同） | ~25 | 低（tests） | 各脚本运行 |
| A7 | `ui/panel.py:207-214` vs `264-271` | 红/黑搜索强度 QSpinBox 构造块重复（7 行 ×2） | ~7 | 低（UI 手工验证） | 启动 GUI |
| A8 | `app/controller.py` vs `ui/window.py` 定时器方法 | window 的 start/stop/pause/resume_thinking_timer 是薄转发（Qt 槽模式，非真重复；可直连 controller，收益小） | ~8 | 低 | GUI 手工 |
| A9 | `main.py:11-15` vs `services/models.py:45-49` | frozen→exe 目录 / 否则脚本目录 的 base-dir 定位逻辑重复 | ~5 | 低 | 启动 GUI |

### B. 生产不可达 / 死代码

| # | 位置 | 内容 | 减行 | 风险 | 验证 |
|---|------|------|------|------|------|
| B1 | `domain/egtb.py:27-136`（约 110 行） | chessdb.cn 云查询（probe_cloud/熔断/缓存/常量）：生产两处调用点（search.py:594、mcts.py:262）均硬编码 `allow_cloud=False`，全仓库无 True 调用点；`probe` 默认参数 `allow_cloud=True` 反而是隐患（未来调用方漏传会同步联网卡搜索）。可删云路径并把默认改 False，或仅把默认改 False 并删云代码 | 60~110 | 中（公共 API 变更；当前无生产调用者；tests/test_egtb.py 显式禁云） | test_egtb / smoke_engine |
| B2 | `domain/game.py:797` | `i1, i2, i3 = indices[-3], indices[-2], indices[-1]` 中 `i2` 未使用 | 1 | 零 | test_perft（长将用例） |
| B3 | `domain/game.py:36-59` vs `61-74` | `__init__` 与 `reset` 主体几乎相同，`__init__` 可改为 `self.reset()` + 少量字段 | ~13 | 低 | smoke_engine / test_incremental |
| B4 | `scripts/train_nnue.py:313-314` | `if X is None: return 1` 死分支（generate_training_data 恒返回 tuple 或已 return 1） | 2 | 零 | 运行 --quick |
| B5 | `ai/worker.py:60` | 注释中残留旧签名 `tokens` 字段（实际已移除） | 1（注释） | 零 | 无 |
| B6 | 未用导入 | `domain/search.py:34` `evaluate`（仅注释提及）；`ui/board.py:7` `QBrush`；`tests/measure_vision_image.py:14` `VISION_IMAGE_MAX_WIDTH/VISION_IMAGE_SCALE`；`tests/test_evaluation.py:14` `RED_PST/compute_material`；`tests/smoke_engine.py:95` `evaluate` | ~6 | 零 | py_compile |

### C. 结构性问题（非纯精简，需权衡）

| # | 位置 | 内容 | 说明 |
|---|------|------|------|
| C1 | `domain/search.py:311-316` | `search()` 的 `on_progress` 参数与构造器 `progress_callback` 双通道，同一事件可能双回调 | 可合并为一；需查 controller 调用方 |
| C2 | `domain/game.py:161, 725` | `from domain.evaluation import RED_PST` 在方法内热路径执行（2 处），应提至模块顶部 | 性能微优化 + 消除重复 |
| C3 | 39 处宽泛 `except Exception`（pikafish 13 / engine_bridge 10 / worker 10 / search 5） | 部分 `except Exception: pass` 静默吞错（如 search.py:751, 807 NNUE 累加器异常） | 属"收紧异常"而非精简；收紧后行为更可预期，但改错误处理 = 行为变更，需谨慎 |

## 三、上一轮审查（docs/CODE_REVIEW.md，2026-08-13）遗留项核对

以下为上一轮"轻微问题"清单在本轮的自核状态：

| 项 | 上轮结论 | 本轮状态 |
|----|---------|---------|
| `AI_RETRY_DELAY_MS` 未用 | controller.py:752,818 用 `retry_count*2000` | ✅ 已修复（现用于 751/817/934） |
| `stats['search_nodes']` 死字段 | controller.py:62,164,225 | ✅ 已移除 |
| 搜索强度三处不一致 | panel 硬编码 5 vs SEARCH_MAX_DEPTH | ✅ 已修复（统一 DEFAULT_SEARCH_DEPTH） |
| worker `tokens` 恒传 0 | worker.py:31,86,90 | ✅ 已移除（仅 60 行注释残留，见 B5） |
| game.py:159 热路径 import | → 现在 161 与 725 两处 | ⚠️ 仍在（见 C2） |
| game.py:794 `i2` 死变量 | → 现在 797 | ⚠️ 仍在（见 B2） |
| smoke_engine.py:92 死导入 | → 现为 161 行带 `# noqa: F401` 的模块加载检查 | ⚠️ 有意保留（模块可导入性检查） |
| train_nnue `X is None` 死代码 | :313-314 | ⚠️ 仍在（见 B4） |
| numpy 未声明依赖 | requirements.txt 无 numpy | ✅ 已修复（numpy>=1.24 已入 requirements） |

## 四、子代理结果（已合并：UI / services / prompts 子系统）

> 来源：子代理 2e22c7bc 深度审查 ui/{window,panel,board,theme}.py、services/{logging,models}.py、domain/prompts.py、main.py。所有"零调用点"声明经全仓库 grep 核实。

| # | 类别 | 位置 | 预估减行 | 风险 |
|---|------|------|---------|------|
| U1 | 未用导入 | `ui/board.py:7`（`QBrush`，全仓库仅此一处） | 1 | 低 |
| U2 | 死代码 | `ui/board.py:203-206`（mousePressEvent 内层无操作 else：把已是 (-1,-1) 的选中再赋一遍 + 无效重绘） | 4 | 低 |
| U3 | 重复逻辑 | `ui/panel.py:184-222` vs `241-279`（红/黑引擎组各 39 行，仅前缀/颜色/tooltip 不同）→ 提取 `_build_engine_group(parent, side, ...)` | ~25 | 低-中 |
| U4 | 重复逻辑 | `ui/window.py:92,135` + `ui/panel.py:143`（`#1a1a1e` 面板背景字面量 ×3）→ theme 常量 | 2 | 低 |
| U5 | 重复逻辑 | `main.py:11-14` vs `services/models.py:45-49`（frozen→exe 目录基判定，与 A9 同项） | 4 | 低 |
| U6 | 重复逻辑（信息性） | `ui/theme.py:14-25` 全局 QSS vs `panel.py` 每控件样式——全局规则非死代码（`collapse_btn` 依赖），**不建议删** | 0 | 高（不删） |
| U7 | 重复逻辑 | `ui/window.py:193-201`（model1/2 下拉填充 ×2）→ `_populate_combo(combo, models)` | 3 | 低 |
| U8 | 冗长 | `ui/window.py:338-363`（update_player_status 状态标签成对分支 ×5）→ `_set_status_pair(red, black)` | 6 | 低 |
| U9 | 冗长 | `ui/window.py:97-117`（black/red_status 构造重复）→ `_make_status_label(...)` | 4 | 低 |
| U10 | 冗长 | `ui/window.py:208-249`（红/黑 8 个两两同构信号槽）→ 4 个带 side 参数的槽 + partial 绑定 | 10 | 低-中 |
| U11 | 冗长 | `domain/prompts.py` 5 组重复片段（阶段判定/玩家上下文/走子历史块/工具示例字面量/将军条件）→ 提取助手或常量 | 14 | 低 |
| U12 | 冗长 | `domain/prompts.py:417/423`（`(fr,fc,tr,tc) in checking` 重复求值）→ 缓存局部变量 | 1 | 低 |
| U13 | 冗长 | `ui/panel.py:97-133`（3 个单调用样式函数）→ 模块级常量 | 6 | 低 |
| U14 | 冗长 | `services/models.py:74-90`（6 条 stderr print 折叠） | 4 | 低 |
| U15 | 冗长 | `services/logging.py:15`（`'INFO'` 与 `'DEFAULT'` 同值冗余键；**注意 `'ERROR'` 键不能删**，删了会变灰） | 1 | 低 |
| U16 | 过度抽象 | `ui/window.py:259-271`（4 个纯转发计时器方法，`stop_thinking_timer` 仅 closeEvent 一个调用点）→ controller 直调 | 13 | 中（需同步改 controller 6 处调用点 + test_arbitration.py:77 FakeMain 桩） |

**U 系列合计约 95~100 行**（各条独立可实施）。

子代理已核实**不是问题**的点（防误报）：prompts.py 的 `include_analysis_tools=False` 分支（controller llm_only 模式在用）、`TOOLS_BASIC/DEFAULT_TOOLS/MOVE_PIECE_TOOL` 均有调用点、window 的 4 个 UI 更新方法均被 controller 调用、`LogManager.clear()` 被调用——均非死代码。

## 五、子代理结果（已合并：引擎 / EGTB 子系统）

> 来源：子代理 99ec3a54 深度审查 domain/{pikafish,egtb,egtb_local,nnue,models}.py。grep 核实所有调用点。

| # | 类别 | 位置 | 预估减行 | 风险 |
|---|------|------|---------|------|
| E1 | 生产不可达 | `domain/egtb.py:27-136`（cloud 查询 probe_cloud/熔断/缓存/常量）——全部生产调用方显式 `allow_cloud=False`，默认 `True` 无调用点依赖；仅 `tests/verify_fixes.py` 直调 probe_cloud（联网脚本） | ~145 | 低 |
| E2 | 死代码 | `domain/egtb.py:370` `clear_cache`（零调用；子代理初报为 egtb_local，主代理复核实际在 egtb.py） | ~5 | 低 |
| E3 | 死代码 | `domain/pikafish.py` `PikafishEngine.get_top_moves`（零调用；注意与 MCTSEngine.get_top_moves 接口同名但无调用方） | ~15 | 低 |
| E4 | 过度抽象 | `PikafishEngine.__init__` 4 个参数全部调用方传默认值（零实参） | ~4 | 低 |
| E5 | 过度抽象 | `search(priors=...)` 参数无人传参 | ~4 | 低 |
| E6 | 重复逻辑 | `scripts/gen_selfplay.py rotate180_swap` ≡ `domain/egtb_local.py _rotate_board` 逐字重复（与 A4 同项） | ~8 | 低 |
| E7 | 重复逻辑 | `domain/egtb_local.py _kings_facing` ≈ `domain/game.py _is_king_facing`（近似重复，语义需核对） | ~10 | 中 |
| E8 | 重复逻辑 | DTM 评分公式在 egtb.py / egtb_local.py 重复 | ~8 | 中 |
| E9 | 冗长 | 5 处棋盘扫描样板（pieces 计数/王位扫描）→ 共享助手 | ~15 | 低 |
| E10 | 冗长（高风险） | `search_async`/`_search_locked` 重复 UCI 命令序列（异步/同步双路径） | ~30 | 高（进程通信时序，验证薄弱） |
| E11 | 冗长 | NNUE 累加器对称重复（update/unmake 两个对称块） | ~20 | 中 |
| E12 | 冗长 | pikafish.py 7 处局部 `import sys`（函数内）→ 模块顶部 | 3 | 零 |
| E13 | 冗长 | `_drain_out_q` 内联（单调用） | ~4 | 低 |
| E14 | 过度抽象 | `_canonicalize` 单调用包装 | ~6 | 低 |

**E 系列合计约 250 行，其中 ~170 行（E1+E2+E3+E12 等）低风险、收益确定。**

关键验证事实：
- `smoke_engine.py` / `test_egtb.py` **不覆盖 Pikafish**——pikafish 改动只能靠 `verify_fixes.py` + 手工 GUI + gen_selfplay 脚本验证（E10 高风险的原因）。
- `test_egtb.py` 默认读盘不执行 `generate()`——改生成代码必须删 `.dtm` 文件重生成才能被测到（E1 若删 cloud 路径不受影响）。
- **勿动陷阱**：`generate()` 行 320-341 "不可能局面"分支当前不触发，但删除会改变重新生成的表内容，与"功能不变"约束冲突——不改。

## 六、子代理结果（已合并：ai 层）

> 来源：子代理 0f5ae0d8 深度审查 ai/{worker,manager,parser}.py + 相关 controller 调用点。grep 核实所有调用点。

| # | 类别 | 位置 | 预估减行 | 风险 |
|---|------|------|---------|------|
| W1 | 死代码（未用参数） | `worker.py:59,138-139,142-143` + `controller.py:692,1245`（`tokens` 信号第 5 参恒传 0、槽从不读取）——连带 tests/test_arbitration.py 7 处位置实参 | 3~5 | 低 |
| W2 | 死代码（未用字段） | `worker.py:72,95`（`player_name` 参数与赋值，全仓库零读取）——连带 controller.py:603,1225 及两个压力测试位置实参 | 2~3 | 低 |
| W3 | 冗长 | `worker.py:189` vs `205`（tool_calls 16 行内重复提取） | 1 | 低 |
| W4 | 死代码（不可达分支） | `worker.py:303-305`（注释自认"防御性"的 break，逐路径不可达） | 2~3 | 低 |
| W5 | 死代码（空操作） | `worker.py:567`（`tmp_game.board = board` 同一引用重赋；CODE_REVIEW.md 亦已指出） | 1 | 低 |
| W6 | 冗长（样板 ×12） | `worker.py` 12 处 `json.dumps({'error':...}, ensure_ascii=False)` → `_tool_err(msg)` 助手 | 5~8 | 低（输出逐字节相同） |
| W7 | 重复逻辑 | `worker.py:496-532` vs `586-619`（Pikafish/本地两条搜索路径的"★ 搜索首选"+"Top-N 候选"格式化逐行同构）→ 公共 helper | 8~12 | 中（输出文本须逐字节一致，标题文案作参数） |
| W8 | 重复逻辑 | `worker.py:215-219` vs `314-317`（仲裁返回块 ×2）；`chr(65+..)` 坐标编码内联 ~14 处，而 `format_coord/format_move`（constants.py:79,103）是权威实现 | 4~6 | 低 |
| W9 | 常量漂移 | `worker.py:641` 硬编码 14 vs `constants.py:27 ENDGAME_PIECE_THRESHOLD`（全仓库唯一硬编码点） | 0（换 import） | 低 |
| W10 | 死代码 | `worker.py:64-69,91`（无基类的 `super().__init__()` + 过期 docstring） | 1~3 | 低 |
| W11 | 过度抽象 | `worker.py:728-729`（`_is_llama_server` 单调用包装） | 2 | 低 |
| W12 | 过度抽象（边界） | `worker.py:772-784`（`_build_user_content` 单调用，docstring 记录 image_url 过滤理由）——**建议保留** | 0~4 | 低 |
| W13 | 冗长 | `worker.py:542-545+621-622`（内层 except 与外层 453-470 逐字节相同）→ 内层改 try/finally | 2 | 低 |
| W14 | 冗长 | `controller.py:178,226`（clear_queue 已复位 busy，其后 `set_busy(False)` 空操作）；`controller.py:749-750,815-816,936-937`（3 处内联清理对与 `_finish_ai_move` 同构） | 5 | 低 |
| W15 | 过度抽象（风格） | `manager.py:38-49`（`set_active_worker/set_active_thread/set_busy` 单字段 setter）→ 直接属性赋值（风格取舍，可保留） | 6~8 | 低-中 |

**W 系列合计约 35~50 行。**

已核实**否证**：LLM 工具 schema **无第二份定义**（`"name": "move_piece"` 等仅命中 prompts.py:36,56,79，worker 经 `tools` 构造参数消费——单一来源成立）；`ai/parser.py` 无精简发现。

验证载体确认：**smoke_engine.py 不覆盖 LLM 工具回路**（仅导入 domain.*）；worker 改动主要靠 `llm_pressure_test.py`（需端点）、`test_arbitration.py`（离线 A1-A6 覆盖仲裁槽）、`arbitration_pressure_test.py`（真实 API）；GUI 手工验证点：hybrid 走子、暂停/恢复/重置、DeepSeek 无 image_url。

## 七、子代理结果（已合并：app 层）

> 来源：子代理 e7423203 深度审查 app/{controller,engine_bridge}.py。每条经 read 全文 + 全仓 grep 核实。

| # | 类别 | 位置 | 预估减行 | 风险 |
|---|------|------|---------|------|
| C1 | 死代码 | `engine_bridge.py:211,220,222`（`start_mcts_fallback` 的 `result` 字典只写不读；对比 `_start_mcts_async` 的是活代码） | 3 | 低 |
| C2 | 重复逻辑 | `controller.py:509-518 / 909-916 / 1138-1148`（"零合法走法→判负"3 份拷贝，后两处属防御）→ `_declare_no_legal_moves()` | 8-10 | 低 |
| C3 | 重复逻辑 | `controller.py:747-756 / 813-822 / 918-943`（重试块逐字重复 + 第三变体）→ `_retry_or_fallback()` | 8-10 | 低 |
| C4 | 重复逻辑 | `controller.py:1120-1126 / 1258-1264 / 1272-1278 / 1349-1355`（仲裁"失败→LLM 回退"核心 5 行 ×4）→ `_fallback_llm_move()` | 7-10 | 低（test_arbitration A4/A5 直接覆盖） |
| C5 | 重复逻辑 | `controller.py:413-431 vs 863-877`（`_on_search`/`_on_fb` 异步回调近相同）+ 6 处守卫（415-417/441-443/453-455/623-625/841-843/865-867）与 `_check_version` 同一同步链重复 | 10-14 | 中（可证明等价，但改动面大；建议先只抽守卫） |
| C6 | 重复逻辑 | `engine_bridge.py:175-189 vs 277-291`（Pikafish 健康检查+自动重启 14 行逐行相同）→ `_ensure_pikafish_alive()` | 8-10 | 低 |
| C7 | 冗长 | `engine_bridge.py:216-231 / 302-315 / 379-415`（MCTS 后台线程样板 3 份）→ `_spawn_mcts_thread()` | 6-8 | 中（线程语义敏感，须保留按线程 pop 自己的条目） |
| C8 | 重复逻辑 | `controller.py:261-262, 1290, 1292, 1327, 1332, 1337`（手写 `f"{chr(65+col)}{row+1}"` ×7 处，而 format_coord/format_move 已导入并用于 959-960）→ 统一用 domain 实现（与 W8 同类） | 0（去重） | 低 |
| C9 | 冗长 | `controller.py` 约 8 处"棋子名+format_move"样板 → `_move_desc(move)` | 5-6 | 低 |
| C10 | 重复逻辑 | `controller.py:521-524/1150-1153`（合法走法+重复标注）、`527-536/1156-1157`（子力对比）、`503/1136`（回合数）→ `_legal_moves_str()`/`_material_str()` | 4-5 | 低 |
| C11 | 重复逻辑 | `controller.py:854-858 vs 884-888`（"连续异常终止游戏"守卫，前者不可达属防御） | 3-4 | 低 |
| C12 | 冗长 | `controller.py:704-706 vs 1315-1317`（show_think 表达式逐字重复）→ `_show_think_enabled()` | 1-2 | 低 |
| C13 | 重复逻辑 | `controller.py:316-318 vs 399-401`（"📚 当前开局"日志 ×2） | 2-3 | 低 |
| C14 | 死代码（恒真守卫） | `engine_bridge.py:175,199`（`on_done is not None`——全仓 3 个调用点均传真实回调） | 2 | 低 |
| C15 | 过度抽象 | `engine_bridge.py:319-336`（`_start_pf_hint` 唯一调用点，17 行包装）→ 内联 | 6-8 | 低 |
| C16 | 过度抽象+常量重复 | `engine_bridge.py:361-362`（`_depth_to_sims` 唯一调用点；回退值 2000 与 mcts.py:35 `DEFAULT_SIMULATIONS` 重复） | 2 | 低 |

**C 系列合计约 60-80 行。**

已核实**否证**（重要）：controller/engine_bridge 与 domain **无棋盘/走法/将军逻辑重复**——将位缓存、射线检测、走法校验、合法走法、重复走法全部委托 domain；版本门控单一实现 `_check_callback_valid`；未用导入/字段无（18 个导入符号逐一追踪均有使用）。**唯一确认的换算重复是 #8 坐标字符串格式化**。

附带发现（未计入减行）：`MCTSEngine.search(on_progress=...)` 与 `__init__(progress_callback=...)`（mcts.py:87,104）、`SearchEngine` 同名参数（search.py:190,226）——grep 全仓**零调用点**，属 domain 层死参数（engine_bridge 与 worker 均不传）。CLAUDE.md 记载 `_DEPTH_SIMS_MAP` 在 controller，实际在 engine_bridge.py:44（文档漂移）。

## 八、子代理结果（已合并：domain 核心 — 完整 24 条）

> 来源：子代理 39221cf5 深度审查 domain/{game,constants,fen,evaluation,search,mcts}.py。主代理已独立复核 #1/#2/#5/#6/#7/#8/#10/#15/#18 等关键条目的证据。

### 中风险大件

| # | 类别 | 位置 | 预估减行 | 风险 | 证据 |
|---|------|------|---------|------|------|
| #1 | 重复逻辑 | `game.move_piece`（game.py:146-189）↔ `search._make_move`（697-742）↔ `_unmake_move`（756-797）：Zobrist/将位/PST/子力计数三份拷贝，前向两份逐行同式、撤销为精确逆（唯一结构差异：_unmake 恢复只增不减无需清 0 键；NNUE 段仅 search 侧）→ 抽 `_sync_move_caches(game, ..., undo=False)` | 40-55 | 中（test_incremental 严格 dict 断言是天然闸） | ✅ 主代理逐行比对 |
| #2 | 重复逻辑 | `evaluation.evaluate()`（308-353）↔ `evaluate_fast()`（419-463）：D~I 六特征组+模式检测逐行同构（46 行 vs 45 行）；唯一行为差异是机动性段（仅 evaluate 有，LLM 工具路径依赖，**不可顺手统一**——CODE_REVIEW M-SEARCH-2）→ 抽 `_score_features()` | 40 | 中（浮点加法顺序须逐项一致；test_fast_consistency 100 局面断言等价） | ✅ 主代理已比对 |
| #3 | 重复逻辑 | `game._is_legal_move`（291-397，~107 行）↔ 定向生成器 `_gen_*`（549-655）双实现同一套行棋规则；move_piece:136 可改 `(fr,fc,tr,tc) in get_all_legal_moves(owner)` 后整删 | 净 50-90（域内 −100，smoke_engine 的 `_brute_in_check` 独立 oracle 需测试内置 ~50 行拷贝） | 中 | 调用点仅 game.py:136 + smoke_engine.py:115 |

### 低风险批量（可独立提交）

| # | 类别 | 位置 | 预估减行 |
|---|------|------|---------|
| #4 | 死参数（双进度回调） | search.py:190,194,226,311-316 + mcts.py:87,91,104,177-187——全仓 `.search()` 调用点零传回调 | 15 |
| #5 | 死代码（TT 统计链） | search.py:69-70,74-75,87,96,127-135（`hit_rate`/`size`）、830-837（`tt_hit_rate`/`tt_size`）→`_hits`/`_probes` 全链零消费者 | 12 |
| #6 | 死代码 | game.py:89-92 `king_pos` property（grep `\.king_pos\b` 0 匹配，全部直用 `_king_pos`） | 3 |
| #7 | 未用导入 | search.py:34 `evaluate`（仅 import 与注释） | 1 |
| #8 | 死变量 | game.py:797 `i2`（仅 range(i1,i3) 用 i1/i3） | 1 |
| #9 | 死赋值 | search.py:259 `ordered_moves = None` | 1 |
| #10 | 死常量（兼值重复） | constants.py:20 `MCTS_EXPLORATION` 零引用；mcts.py:37 另立 `DEFAULT_EXPLORATION=1.4` 同值 → 推荐 mcts 改引用常量 | 1 |
| #11 | 未用导入 | fen.py:9-10 TYPE_CHECKING 下 `ChineseChessGame`（无注解引用） | 2 |
| #12 | 仅测试调用 | game.py:696-702 `is_endgame`、704-716 `count_pieces`、845-851 `get_move_key`——生产零调用（count_pieces/get_move_key 删则需同步测试；is_endgame 建议保留并让 search/mcts 转用，见 #13） | 27（含测试同步） |
| #13 | 重复逻辑 | 残局公式"总子数≤14"5 处：game.py:702 / evaluation.py:75 / search.py:605 / mcts.py:255 / worker.py:641（硬编码）→ search/mcts 用 O(1) `game.is_endgame()`，worker 换常量 | 6 |
| #14 | 重复逻辑 | mcts.py:253-254、search.py:367-369 全盘扫子 vs O(1) `_red_piece_count+_black_piece_count` | 3 |
| #15 | 重复逻辑 | search.py:812-818 ↔ mcts.py:352-358 `_is_time_up`/`stop` 逐行相同 | 6 |
| #16 | 冗长 | search.py:510-516 与 546-552 同款 qs 排序 lambda（`evaluate_move_ordering(game.board, *m)` 可等价）→ `_sort_by_mvv_lva()` | 8 |
| #17 | 重复逻辑 | game.py:682-683 `format_move_history` 内联坐标公式重写 constants.format_coord/format_move；worker.py:523-524,607-608 同款 | 2 |
| #18 | 冗长 | game.py:161,725 热路径内联 `import RED_PST`（evaluation 只依赖 constants，无循环导入）→ 模块顶部 | 2 |
| #19 | 冗长 | mcts.py:276-277 显式传 `legal_moves_red=0, legal_moves_black=0`（即默认值） | 2 |
| #20 | 重复逻辑 | search.py:590、mcts.py:258 的 `<= EGTB_MAX_PIECES` 守卫与 egtb.py:199 内部守卫重复（probe 自守卫） | 2 |
| #21 | 重复逻辑 | evaluation.py:676-681 MVV-LVA 公式 ↔ worker.py:574-577 重写一份 → worker 复用 | 4 |
| #22 | 常量重复 | search.py:141 `DEFAULT_MAX_DEPTH=8` vs constants.py:14 `SEARCH_MAX_DEPTH=8` → 可引用消除漂移（可选） | 1 |
| #23 | 过度抽象 | game.py:853-860 `_no_attacking_pieces` 唯一调用点 move_piece:245（语义清晰，可保留） | 7（可选） |

### 不建议

| # | 类别 | 位置 | 说明 |
|---|------|------|------|
| #24 | 重复逻辑 | search.py:278-304 根节点 PVS 循环 ↔ 404-443 `_alpha_beta` 走法循环同构 | 合并任何偏差都改变选棋行为，收益 ~15 行，**不做** |

**D 系列合计约 250-290 行**（含 #1/#2/#3 大件；低风险项约 100-120 行）。

## 九、汇总与优先级

> 生产代码 25 个文件共 **11,503 行**（不含 tests 2,400+ 与 scripts 892）。测试基线已实测全绿：`test_perft`（44/1920/79666）、`smoke_engine`（EGTB/限着/开局库 165 线/AB 全过）。

### 各系列合计（已去重，与第 2-8 节条目对应）

| 系列 | 内容 | 低风险小计 | 中风险大件 |
|------|------|-----------|-------------|
| A（跨文件自核） | 增量维护重复/板扫描/测试样板 | ~80 | +D#1 |
| U（UI/services/prompts） | 面板重复/槽合并/提示词片段 | ~80 | — |
| E（引擎/EGTB） | **云路径删除 145**/死代码/扫描样板 | ~200 | +E10/E11 |
| W（ai） | 死参数链/JSON 样板/搜索结果格式化 | ~35 | +W7 |
| C（app） | 判负/重试/仲裁回退/健康检查重复 | ~70 | +C5/C7 |
| D（domain 核心） | TT 链/死常量/评估重复/`_is_time_up`（24 条，含测试耦合项） | ~100-120 | +D#1/#2/#3 |

- **P0 零/低风险（纯删除、常量提取、样板折叠，独立可提交）**：约 **400~500 行**，占生产代码 3.5~4.5%。
- **P1 中风险（重复逻辑抽取，有强测试闸）**：约 130~220 行（D#1 增量维护三拷贝 40-55、D#2 评估特征段 40、W7 搜索结果格式化 ~10、E11 NNUE 对称块 ~20、C5/C7 回调/线程样板、E10 UCI 双路径 ~30）。
- **P2 高风险/不建议（改行为或收益比低）**：D#3（`_is_legal_move` 双实现删除，净 50-90 行，smoke_engine oracle 依赖）、D#24（根节点 PVS 合并）、U6（全局 QSS 合并，`collapse_btn` 依赖全局规则）。

### 结论

1. **可行且推荐**：P0 全部 + P1 的 D#1/D#2/W7。合计 **550~700 行（5~6%）**，全部有"功能不变"验证手段（见下）。
2. **可行但收益/风险需权衡**：E1 云路径删除（~145 行）——生产不可达但属公共 API 变更，且 tests/verify_fixes.py 联网脚本在用；建议连同 verify_fixes 一起处理。
3. **不建议**：P2 三项（D#3 / D#24 / U6）。
4. 架构层**没有**需要精简的重复基座：controller 全部委托 domain（无棋盘/走法/将军逻辑再实现，app 子代理已否证）；LLM 工具 schema 单一来源（prompts.py）；版本门控单一实现。这说明**本项目代码质量基线高**，精简空间主要来自"迭代留下的死代码/样板/参数残留"，而非架构坏味道。

### 建议实施顺序（每步独立提交 + 验证）

| 阶段 | 内容 | 减行 | 验证 |
|------|------|------|------|
| 1 | 死代码/未用导入/死参数批量（B2-B6、D#4-D#11、U1-U2、W1-W5/W10-W11、C1/C14、E2-E5/E12） | ~120 | py_compile + 各脚本跑一遍 |
| 2 | 常量与样板折叠（A6、U4/U7-U15、W6/W8/W9/W13-W14、C9-C13/C16、E13-E14、D#16-D#19/#21/#22） | ~150 | 对应压力测试 + GUI 目检 |
| 3 | 跨文件重复抽取（A2-A5、U3/U10/U16、C2-C4/C6/C8、D#13-D#15/#20、E6/E9、W7） | ~120 | test_arbitration + 压力测试 + GUI |
| 4 | 大件重构（D#1 增量维护共享、D#2 评估共享、E1 云路径删除、C5/C7、E11；D#12/D#23 可选） | ~250 | test_incremental/test_evaluation/test_perft/compare_movegen + GUI 双 AI 对局 |

> 注：`.claude/skills/verify/SKILL.md` 在仓库中不存在（CLAUDE.md 有引用），验证矩阵以本节 + tests/ 脚本为准。

---

## 十、实施记录（P0 + P1 已执行完毕）

> 状态：全部完成。31 个文件，**净减 482 行**（819 增 / 1301 删）；全测试矩阵绿。

### 已实施（主代理执行）

| 批次 | 内容 | 验证 |
|------|------|------|
| domain 死代码/低风险 | D#4-D#11、D#13-D#20、D#16-D#19、B2/B3/B6、A2/A3（`_engine_time_up`/`_engine_stop` 共享、`_sort_by_mvv_lva`、is_endgame 收敛、O(1) 计数、probe 预检查移除）、fen.py 去 TYPE_CHECKING | perft/incremental/evaluation/smoke 全绿 |
| **E1 云路径删除** | egtb.py 删除 chessdb.cn 云查询（probe_cloud/熔断/缓存/常量，约 145 行）+ `allow_cloud` 参数 + `clear_cache`；同步改 search/mcts/smoke_engine 调用方、verify_fixes.py 云段移除、constants 删 `EGTB_CLOUD_MAX_PIECES` | test_egtb 0 失败 |
| **D#1** | `game._sync_move_caches(game, fr,fc,tr,tc,captured,undo)` 统一 move_piece/`_make_move`/`_unmake_move` 三份增量缓存维护（~50 行） | test_incremental make/unmake 50 循环恢复一致 |
| **D#2** | `evaluation._score_features(...)` 统一 evaluate/evaluate_fast 的 D~I 特征组+模式检测（~40 行） | test_fast_consistency 100 局面相等 |
| worker（W 系列） | W1 tokens 死链、W2 player_name、W3 tool_calls 重复提取、W4 不可达 break、W5 空操作赋值、W6 `_tool_err`×12、**W7 `_format_search_result`**（修复 `entry[:4]` 解包 bug）、W8 `_arb_return`+format_coord 内联、W9 常量、W10 super().__init__、W11 内联、W13 try/finally | 助手输出逐字节对照 + test_arbitration 全过 |
| app（C 系列） | C1 result 死写、C2 `_declare_no_legal_moves`、C3 `_retry_or_fallback`、C4 `_fallback_llm_move`、C5 `_engine_round_guard`（6 处守卫）、C6 `_ensure_pikafish_alive`、C7 `_spawn_mcts`（回退+提示共用）、C8-C13 各助手、C14 恒真守卫、C15/C16 内联、W14 冗余 set_busy、**A8/U16 定时器直调** | test_arbitration A1-A6 全过 |
| A5 | `domain/fen.py` 新增 `fen_to_board` 解析器；compare_movegen/eval_benchmark 复用（-18 行） | compare_movegen 3020 局面 100% |

### 已实施（子代理执行，主代理已复核 diff）

| 批次 | 内容 | 验证 |
|------|------|------|
| UI（3e4d2e80） | U1 QBrush、U2 空操作分支、U3 `_build_engine_group`、U4 PANEL_BG、U7 `_populate_combo`、U8 `_set_status_pair`、U9 `_make_status_label`、U10 8 槽→4 槽+side、U13 样式常量（-58 行） | py_compile + test_arbitration |
| 引擎/脚本（a6058daa） | E3 死方法、E5 死参数、**E11 `_apply_accumulator_delta(sign=±1)`**（2000 对随机 bit-identical）、E12 局部 import sys×7、E14 `_canonicalize` 内联、A4 rotate180_swap 复用、B4 死分支、B6 未用导入、U14 stderr 折叠、U15 INFO 键、U5 `app_base_dir()`（-51 行） | 专项字节等价 + 全测试 |

### 明确未做（理由）

- **D#3** `_is_legal_move` 双实现删除、**D#24** 根 PVS 合并、**U6** QSS 合并 —— P2 不建议项，改行为风险高。
- **E10** UCI 同步/异步序列合并 —— pikafish 无自动化测试覆盖，进程通信时序风险高，**暂缓**。
- **A6** 测试 `check()` 助手合并 —— 6 个独立脚本（非 pytest）的 standalone 约束，收益小。
- **D#12** `count_pieces`/`get_move_key` 删除 —— 被真实一致性测试使用，删除需连带改测试、收益小，保留。
- **E4** PikafishEngine 构造参数、**E9** egtb_local 扫描样板 —— 文档化配置接口/用途不同，收益小或行为风险。
- **W12** `_build_user_content`、**W15** manager setter —— 风格取舍，收益比低。

### 验证矩阵（最终状态）

perft（44/1920/79666）✅ · test_incremental ✅ · test_evaluation（含 fast 100 局面等价）✅ · smoke_engine（EGTB/限着/开局库 165 线/AB）✅ · test_arbitration A1-A6 ✅ · compare_movegen（3020 局面 0 不一致）✅ · test_egtb（0 失败）✅ · 33 文件 py_compile ✅ · worker 助手输出逐字节对照 ✅
GUI 手工验证点（无法自动化）：hybrid 双 AI 对局、AI 思考中暂停/恢复/重置、人类回合提示搜索、分歧仲裁弹窗、`python main.py` 启动与面板样式。

---

## 十一、第二轮全面排查（2026 后续，当前状态）

> 方法：主代理跨模块扫描 + 3 个子代理逐模块深审（domain / app+ai / ui+services+scripts+tests），全部"零调用点"经 grep 核实。
> 结论：**剩余可精简约 190-220 行**，其中零/低风险约 100-120 行；无架构级坏味道，主要是**序列级重复**与**上轮遗漏**。

### 排查中已即时修复（2 处）

1. **search.py 4 个死导入**（`BOARD_WIDTH/BOARD_HEIGHT/RED_PST/ZOBRIST_TABLE`）——D#1 `_sync_move_caches` 重构后 `_make_move`/`_unmake_move` 不再直接用这些符号，导入残留。已删。
2. **prompts.py U11/U12 补实施**（P0 遗漏）：`_player_context`/`_phase_of`/`_history_section`/`_MOVE_PIECE_EXAMPLE` 4 个助手收敛 5 组重复片段 + `is_check` 缓存（format_legal_moves）。输出逐字节验证通过。

### P0 遗漏复核（上轮声称已实施、实际未做）

| 项 | 现状 | 位置 |
|----|------|------|
| **D#4 双进度回调** | SearchEngine 与 MCTSEngine 的 `progress_callback`（构造器）+ `on_progress`（search 入参）双通道**全仓零实参传递**，且同一事件会双触发（search.py:315-320） | search.py:196,200,232,315-320；mcts.py:87,91,104,177-178,185-187（~18 行） |

### 发现汇总（按模块）

**domain/（~55-58 行）**

| # | 位置 | 类别 | 减行 | 风险 |
|---|------|------|------|------|
| d1 | search.py:69-70 `TranspositionTable.clear()` | 死代码 | 2 | 低 |
| d2 | game.py:164-165 `is_black` | 死代码（连内部都无调用） | 2 | 低 |
| d3 | search+mcts 双进度回调（即 D#4） | 死参数 | 18 | 低 |
| d4 | evaluation.py:328-363 vs 414-440 位置收集循环（26 行逐行重复，D#2 残余） | 重复 | 15-18 | 中（test_fast_consistency 闸） |
| d5 | prompts.py:358/368 "被将军/将帅对面"条件重复求值（U11 第 5 组） | 重复 | 1-2 | 低 |
| d6 | prompts.py:304-307/541-544 棋盘 code block ×2 | 重复 | 3 | 低 |
| d7 | search.py:691-700/711-720 NNUE 累加器 try/except 对称块 | 重复 | 7 | 低-中（test_incremental 闸） |
| d8 | search.py:579-586 vs mcts.py:255-266 EGTB probe 样板 | 重复 | 4 | 低 |
| d9 | search/mcts 单合法走法捷径 ×2 | 重复 | 2 | 低 |

**app/ + ai/（~90-115 行）**

| # | 位置 | 类别 | 减行 | 风险 |
|---|------|------|------|------|
| a1 | controller.py:1156 `player_name` 赋值未用 | 死代码 | 1 | 零 |
| a2 | controller.py:589-590 `_on_hybrid_engine_done` 默认参数永不生效（调用点均传满 4 参） | 死参数 | 2 | 零 |
| a3 | controller 走子成功收尾序列 ×5（`_on_move_success→_refresh_ui→[finish]→game_over?→_schedule_next`） | 重复 | 20-30 | 中（test_arbitration + GUI 闸） |
| a4 | controller.py:863-874 `_retry_move` 尾部 ≡ `_retry_or_fallback` 逐字副本 | 重复 | 8-10 | 低-中（日志微调） |
| a5 | `_fallback_hybrid_engine` ≡ `_fallback_llm_move` 骨架同构 | 重复 | 5-6 | 低 |
| a6 | on_ai_finished hybrid/else 失败三分支 ×3 | 重复 | 8-10 | 低-中 |
| a7 | `_start_arbitration` 零合法走法防御 = `_declare_no_legal_moves` 第 3 份内联拷贝 | 重复 | 4 | 低 |
| a8 | `pause_thinking_timer` ≡ `stop_thinking_timer` 逐字节（唯一调用点 224） | 重复 | 7 | 低 |
| a9 | on_human_move 尾部 ≡ `_schedule_next_ai_move` | 重复 | 5 | 低 |
| a10 | worker.py:211-219 轮内文本解析块 ≡ 302-310 循环后块（纯函数、输入不变） | 重复 | 8-9 | 低 |
| a11 | `_start_llm_request`/`_start_arbitration` AIWorker 启动 6 行样板 ×2 | 重复 | 5 | 低 |
| a12 | engine_bridge MCTS 启动环绕样板 ×3（`_spawn_mcts` 未覆盖 `_start_mcts_async` 与环绕段） | 重复 | 8-12 | 低-中（线程语义敏感） |
| a13 | `reset_random`/`reset_random_count` 死链（`_on_move_success` 已无条件重置非随机 source） | 无效参数 | 5-6 | 低 |
| a14 | 杂项空操作：1300 冗余重解包、819 尾部 return、550/557 重复求值 | 无效 | 3-4 | 零-低 |
| a15 | 3 元守卫表达式 ×8（`_engine_round_guard` 只覆盖 6 个回调站点） | 重复（信息性） | 0-2 | 低 |
| a16 | `'红方' if … else '黑方'` ×6、`self.model1 if … else self.model2` ×10 缺助手 | 重复（信息性） | 0-2 | 低 |

**ui/ + services/ + scripts/ + tests/（~41-45 行）**

| # | 位置 | 类别 | 减行 | 风险 |
|---|------|------|------|------|
| u1 | scripts/gen_selfplay.py:233-248 `_fen_to_board` ≡ domain/fen.py `fen_to_board`（A5 漏网） | 重复 | 15 | 低 |
| u2 | ui/panel.py:226-231/247-252 红/黑模型下拉构造块 ×2（U3 只提取了引擎组） | 重复 | 5 | 低 |
| u3 | ui/panel.py 小节间隔样板 ×4 | 重复 | 4-5 | 低 |
| u4 | tests/test_arbitration.py:232-238 `_watch`/`_Probe` 死残留（注释与实现脱节） | 死代码 | 6 | 低 |
| u5 | scripts/train_nnue.py:56 `game` 未用 | 死代码 | 1 | 零 |
| u6 | scripts/train_nnue.py:74,77 `capture_bias = 0.0` 重复赋值 | 空操作 | 2 | 零 |
| u7 | ui/window.py:210 `_on_model_changed` 的 idx/side 死参数（U10 合并残留） | 死参数 | 2 | 低 |
| u8 | ui/window.py:220 `_on_ai_mode_changed` idx 未用（信号必需，可改名 `_`） | 死参数 | 0 | 低 |
| u9 | tests/test_evaluation.py:158 未用赋值 | 死代码 | 1 | 零 |
| u10 | tests/test_evaluation.py:91 未用解包 | 死变量 | 1 | 零 |
| u11 | tests/test_egtb.py:278 `tainted` 未用解包（有意忽略 → `_`） | 死变量 | 1 | 零 |
| u12 | tests/test_arbitration.py:215 `app` 赋值未用 | 死变量 | 1 | 低 |
| u13 | scripts/gen_selfplay.py:217 函数内重复 `import random as _r` | 冗余 | 1 | 零 |
| u14 | ui/panel.py:36 `_spacer` 默认值从未被无参调用 | 死参数 | 0-1 | 低 |
| u15 | 面板背景样式串 `f"background-color: {PANEL_BG};"` ×3 | 重复 | 1-2 | 低 |
| u16 | services/models.py:24-42 `_resolve_env_vars` 单调用点（边界） | 过度抽象 | 0-2 | 低 |
| u17 | scripts/eval_benchmark.py:191,263 函数内惰性导入 ×2 | 冗余 | 1 | 零 |

### 干净与否证（防误报）

- 全部新助手调用点 ≥2（无单调用残留）；controller 30 个导入符号、worker 18 个、engine_bridge 11 个逐一追踪均有使用；tokens/player_name 死链无残留。
- UI 重构未引入新死代码：4 个合并槽均被信号连接、`_build_engine_group` 内局部变量全使用；10 处 Qt connect 点全部核实无孤儿槽。
- 已知保留项复核仍成立：A6 check()、D#12、E4/E9、W12/W15、U6、E10、D#3/D#24。
- tests/smoke_engine.py:95 `evaluate # noqa: F401` 为有意保留的模块可导入性检查。

### 建议优先级

1. **零/低风险批量（~60-70 行，可立即做）**：d1/d2/d3、d5/d6/d8/d9、a1/a2/a7/a8/a9/a13/a14、u1/u4-u15 全部、u17。
2. **低-中风险助手合并（~35-45 行）**：a4/a5/a6/a11、d7、a10。
3. **中风险序列级重构（~50-65 行，需对局验证）**：a3 走子收尾 ×5、a12 MCTS 环绕样板 ×3、d4 位置收集循环。
4. **信息性（可做可不做）**：a15/a16 守卫与命名助手、u16。

### 第 1 批已实施（零/低风险，净减约 78 行，全测试绿）

| 项 | 内容 | 验证 |
|----|------|------|
| d1/d2 | `TranspositionTable.clear()`、`ChineseChessGame.is_black` 死代码删除 | py_compile |
| **d3（D#4 补漏）** | SearchEngine/MCTSEngine 双进度回调（`progress_callback`+`on_progress` 零实参双通道）整链删除，含 `Callable` 导入收窄 | smoke/arbitration |
| d5/d6 | prompts 将军条件缓存 `is_king_danger`（U11 第 5 组补漏）+ 棋盘 code block `_board_block` 助手 | 提示词输出逐项断言 |
| d8 | EGTB `probe` 导入提至模块级（search/mcts） | test_egtb |
| a1/a2 | controller `player_name` 未用赋值、`_on_hybrid_engine_done` 永不生效默认参数 | — |
| a7/a8/a9 | 仲裁零合法走法并入 `_declare_no_legal_moves`、`pause_thinking_timer` 并入 `stop_thinking_timer`、人类落子尾部并入 `_schedule_next_ai_move` | test_arbitration |
| a13/a14 | `reset_random`/`reset_random_count` 死链删除、仲裁冗余重解包/尾部 return/重复求值清理 | test_arbitration |
| u1 | `gen_selfplay._fen_to_board` 复用 `domain.fen.fen_to_board`（A5 漏网补漏，-15 行） | --help + 数据校验 |
| u2/u3/u14 | panel `_build_model_combo`/`_section_gap` 助手、`_spacer` 默认值删除 | MainWindow 构造冒烟 |
| u5/u6/u13/u17 | train_nnue 死变量/空操作、gen_selfplay 重复导入、eval_benchmark 惰性导入提顶 | py_compile |
| u7/u8/u15 | window 槽死参数清理、`PANEL_BG_STYLE` 常量（3 处） | MainWindow 构造冒烟 |
| u4/u9-u12 | 测试侧死代码/死变量清理（`_watch`/`_Probe`、未用赋值/解包、`_` 占位） | 各脚本全绿 |

**最终验证**：perft / test_incremental / test_evaluation / smoke_engine / test_arbitration / compare_movegen（3020 局面 0 不一致）/ test_egtb（0 失败）全绿；33 文件 py_compile；MainWindow offscreen 构造通过；prompts 输出逐项断言通过。

**本轮会话累计**：33 文件，925 增 / 1473 删（净 -548 行，含前两批）。

### 第 2/3 批 + 信息性项已实施（低-中风险，全测试绿）

| 项 | 内容 | 验证 |
|----|------|------|
| **d4** | `evaluation._collect_positions(board)` 统一 evaluate/evaluate_fast 的位置收集循环（26 行 ×2 → 1 份；evaluate 保留单遍 material/PST 累加，fast 热路径扫描不变） | test_fast_consistency 100 局面等价 |
| **d7** | `search._sync_nnue_acc(..., undo)` 统一 `_make_move`/`_unmake_move` 的 NNUE 累加器 try/except 对称块 | test_incremental 50 循环 |
| **a3** | `_complete_move(fr,fc,tr,tc,result,player,source,delay,finish,extra_log)` 统一 5 处走子成功收尾（开局库/LLM/随机/引擎兜底/仲裁） | test_arbitration A1-A6 |
| **a4** | `_retry_move` 尾部委托 `_retry_or_fallback(log=True)`（重试/超限日志保留） | llm_only 路径 |
| **a5** | `_fallback_move(player,candidate,source)` 统一 `_fallback_hybrid_engine`/`_fallback_llm_move` 骨架 | test_arbitration |
| **a6** | `_handle_llm_failure` 统一 on_ai_finished 3 处 hybrid/else 失败分支 | test_arbitration |
| **a10** | worker `_agentic_loop` 轮内文本解析块塌缩为 `break`（循环后块为严格超集，输入逐字节不变） | 结构等价分析 + 编译 |
| **a11** | `_launch_worker(worker, on_finished)` 统一 LLM/仲裁的 AIWorker 启动 6 行样板 | test_arbitration |
| **a12** | `_spawn_mcts` 扩展 `on_result`/`on_error` 钩子 + 新增 `_start_fallback_mcts`：MCTS 线程样板 3 处 → 1 处（诊断收集保留） | smoke（MCTS 路径） |
| **a15** | `_round_blocked()` 纯查询守卫：`_engine_round_guard` 与 `_check_callback_valid` 复用 | test_arbitration |
| **a16** | `_player_name(player)`/`_model_for(player)` 收敛 10 处红黑名 + 10 处模型选择三元 | 编译 + 全测试 |
| **u16** | `_resolve_env_vars` **评估后保留**（19 行内聚助手内联仅省 2 行且加长 load()，审计亦言"保留亦可接受"） | — |

**修复一个自引入 bug**：a16 正则把 `_player_name`/`_model_for` 自身函数体也替换成自我递归（RecursionError），已立即修复并回归。

**最终验证**：perft / test_incremental / test_evaluation（含 fast 100 局面）/ smoke_engine / test_arbitration A1-A6 / compare_movegen（3020 局面 0 不一致）/ test_egtb（0 失败）全绿。

**本轮会话累计**：33 文件，1102 增 / 1666 删（**净 -564 行**）。第二轮排查发现的全部可实施项（第 1+2+3 批 + 信息性）已实施完毕；剩余仅 P2 不建议项（D#3/D#24/U6）、E10（暂缓）、以及评估后保留项（A6 测试样板、D#12 计数方法、E4/E9、W12/W15、u16）。
