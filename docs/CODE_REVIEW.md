# AIChineseChess 代码审查报告

- 审查日期：2026-08-13
- 审查范围：全部 37 个 Python 文件（约 11,180 行），覆盖 domain / ai / app / ui / services / tests / scripts
- 审查方法：6 个子系统并行逐行审查 + 交叉核对调用方 + 实证验证（随机局面走法对拍、开局库逐条引擎验证、长将/重复判决、PyQt 线程语义实验）

> **修复状态（2026-08-13）**：以下"立即修复"三项已完成并验证 ✅
> - **A1** 非字符串坐标卡死 — `ai/worker.py`（类型防线 + `app/controller.py` 两处 except 扩宽为纵深防御）
> - **A2** depth/top_n 钳制移入 try — `ai/worker.py`（新增 `_clamp_int`）
> - **B1** Pikafish 死亡后假可用 — `domain/pikafish.py`（`_run` 区分进程死亡并标记不可用）+ `app/engine_bridge.py`（`start_search` 健康检查，与 hint 路径对齐）
>
> **"高优先级"批次已完成（2026-08-13）✅**
> - **F1** 吃将走法防御 — `domain/game.py:539`（`_append_if_legal` 拦截目标为对方将）
> - **E1** 空基线假通过 — `tests/compare_movegen.py`（缺失/空基线 `sys.exit(1)` + 输出 ASCII 化修复 GBK 报错）
> - **E2** 训练不可复现 — `scripts/train_nnue.py`（`--seed` 参数 + `np.random.seed`）
> - **E3** 误导先验测试反相 — `tests/smoke_engine.py`（其余走法显式压低到 0.1，目标走法 0.9）
> - **A3** move_piece 合法性校验 + 错误反馈 — `ai/worker.py`（`_validate_move` 走法校验，非法时追加 tool 错误结果让模型自纠）
> - **B2** 残留 bestmove 污染 — `domain/pikafish.py`（`_purge_lines` 改用 isready/readyok 握手替代固定 50ms sleep）
> - **D1** `_active_mcts` 单槽竞态 — `app/engine_bridge.py`（改为按线程对象管理的 dict，`finally` 只移除自己的条目）
> - **D2** 启动握手阻塞主线程 — `app/engine_bridge.py`（`init_pikafish` 移入后台线程，经 `init_done` 信号回主线程）
> - **C1** TT 杀分折算 — **经数值验证结论修正**：杀分/困毙分 ply 折算本身自洽（Stockfish 方案，子代理误判）；EGTB 分漂移仅 ±max_depth 且不影响决策，未做侵入式修改，仅澄清注释 `domain/search.py:159`
>
> 验证：9 文件语法编译通过；`smoke_engine`/`test_incremental`/`test_perft`/`test_evaluation`/`compare_movegen`（3020 局面 0 不一致）全绿；C1 数值自洽验证 6/6 通过。
> ⚠️ D2 涉及 Qt 线程模型，冒烟测试无 GUI 覆盖，建议在真实 GUI 环境确认启动日志正常显示、引擎就绪后主搜索可用。

> **"中等问题"批次（2026-08-13）已完成 9 项 ✅**
> - **M-GAME-3** `from_snapshot(king_pos=None)` 抛 TypeError — `domain/game.py`（`dict(king_pos or {})` 走全盘扫描自愈）
> - **M-ENG-2** `_top_moves` 跨线程无锁 — `domain/pikafish.py`（`get_top_moves`/`get_top_moves_scores` 加锁）
> - **M-ENG-3** 車馬/車炮 vs 士象全误判必胜 — `domain/egtb.py`（`has_rook` 分支补官和判断，双車/車兵仍判胜）
> - **M-ENG-4** 云查询异常覆盖不全 + 无响应上限 — `domain/egtb.py`（补 `http.client.HTTPException`/`ssl.SSLError` + `CHESSDB_MAX_RESPONSE_BYTES`）
> - **M-AI-4** `clear_queue` 不复位 busy — `ai/manager.py`（显式复位 `ai_move_in_progress`/`_active_thread`）
> - **M-UI-2** `on_human_move` 缺 busy 检查 — `app/controller.py`（入口加 `is_busy()` 防御）
> - **M-UI-4** LLM 多行日志折叠 — `services/logging.py`（`escape` 后 `\n`→`<br>`）
> - **M-TEST-2** `make_game_with_board` 陈旧缓存 — `tests/smoke_engine.py`（补 `_recompute_incremental`）
> - **M-TEST-1** perft 黄金值过松 — `tests/test_perft.py`（perft(2)=1920、perft(3)=79666 钉死精确值）
>
> **暂缓（风险/复杂度 > 收益）**：M-GAME-1（初始局面重复计数，会改变 `_position_history`/`_move_checks` 索引对齐，可能引入长将检测 off-by-one）；M-GAME-2（get_opening_move 合法性校验）；M-GAME-4（判负 current_player 切换，UI 影响待确认）。
> ⚠️ 验证阶段 pwsh 执行环境崩溃（`0xC0000142` STATUS_DLL_INIT_FAILED，纯 PowerShell 命令亦复现），无法运行 `py_compile`/回归测试；修改已通过 `read` 工具逐处核对语法。

---

## 一、总体结论

**架构与核心正确性质量高，未发现合法对局下的确定性崩溃、双走子或棋局损坏 bug。**

- 走法生成/将军检测经 **1838 个随机合法局面实证 0 不一致**（含马腿、炮架、过河横攻、将帅对脸）；165 条开局线逐条在真实引擎上完整落子全部合法。
- 版本门控体系（`game_version` + `cancel_version` + `needs_cleanup`）经逐路径验证，**不存在重复走子或 busy 残留死锁**。
- 增量缓存（Zobrist / PST / 子力计数 / NNUE 累加器）与全量重算逐项一致，并有测试覆盖。
- 增量评估、快照隔离、EGTB 视角约定、UCI 命令注入面、安全细节（`trust_env=False`、`image_url` 过滤）均做得扎实。

**主要问题集中在三类：**

1. **异常防御缺口** —— 弱模型畸形输出（非字符串坐标、坏参数类型）可导致游戏**永久卡死**或整步失败（最紧急）。
2. **资源生命周期** —— Pikafish 引擎死亡后永久"假可用"不重启、MCTS 停止失效导致线程泄漏、启动握手阻塞主线程 10s。
3. **搜索数值语义** —— 置换表杀分/EGTB 大分的 ply 折算缺陷、两套评估函数口径不一致、搜索无重复局面防护。

---

## 二、严重问题（Critical / High）

### A. AI 工作器层（可用性故障）

**A1【Critical】非字符串坐标逃逸 → 游戏永久卡死** — `ai/worker.py:428` + `app/controller.py:736-738`
- `_extract_move_from_call` 原样返回 `args.get('from','')`，弱模型输出 `{"from": 1, "to": 2}` 时 `1` 为 truthy 直接通过；controller 的 `parse_coord`（`domain/constants.py:97` `s[0].upper()`）抛 `TypeError`/`AttributeError`，而 `controller.py:738` 只捕获 `(ValueError, IndexError)`。
- 异常逃逸到 PyQt 槽 → `_finish_ai_move()`（`set_busy(False)`）不执行 → `ai_move_in_progress` 恒 True → 之后所有 AI 走子被拒，**对局只能重置**。仲裁回调（controller.py:1283-1286）同受影响。
- **修复**：worker 侧加 `isinstance(from, str) and isinstance(to, str)` 校验（一行防御）；或 controller 的 except 扩为 `(ValueError, IndexError, TypeError, AttributeError)`。

**A2【High】`depth/top_n` 类型钳制在 try 块之外** — `ai/worker.py:257-258`
- `max(args.get('depth'), 2)` 对字符串/None 抛 `TypeError`，不被 catch，整步走子以晦涩错误失败，agentic loop 直接终止。
- **修复**：钳制移入 try，非法类型返回工具错误让模型自纠。

**A3【High】非法 `move_piece` 调用被静默丢弃，无工具结果反馈、loop 内零合法性校验** — `ai/worker.py:165-169, 177`
- 参数解析失败的调用被从历史剔除且**不追加错误结果**，模型可能重复坏调用烧光 `MAX_TOOL_TURNS=4`；坐标格式良好但非法的走法（吃对方子/违规跳法）直接作为最终走法返回，合法性完全推迟到 controller。
- **修复**：worker 拿到坐标后校验 `in game.get_all_legal_moves(...)`，非法时追加 `[Tool: move_piece]` 错误并继续循环。

### B. Pikafish / 外部引擎层

**B1【High】引擎静默死亡后永久"假可用"，永不重启** — `domain/pikafish.py:279-280, 553` + `app/engine_bridge.py:122-130`
- 引擎进程崩溃（退出码 0 或写管道未报错）时 `_read_bestmove` 因 `poll() is not None` 直接 break 返回 None——**不抛异常**，`except (OSError,...)` 分支不触发，`_available` 保持 True。之后每次 AI 走子都白跑一次死引擎再 MCTS 兜底，棋力永久降级，且日志反复误导。
- `start_hint_search`（engine_bridge.py:216-222）有显式健康检查而 `start_search`（:122-130）没有，两条路径不一致。
- **修复**：`_run` 返回 None 分支也检查 `proc.poll()` 并置 `_available=False`；支持引擎死亡后按需重启。

**B2【High】残留 bestmove 可能被当成本局结果** — `domain/pikafish.py:497-520, 578-598`
- `_purge_lines` 依赖固定 50ms 睡眠覆盖"stop→bestmove 异步延迟窗口"；若引擎卡顿导致上次超时的 bestmove 残留，下一次搜索可能第一行读到并立即返回——**新搜索被提前截断，返回上一局走法**（注释自认"合法性校验拦不住合法但错误的走法"）。
- **修复**：发 `go` 前 `_send('isready')` 等 `readyok`，或把固定 50ms 改为轮询至队列连续为空。

### C. 搜索引擎与评估层

**C1【High】置换表杀/困分与 EGTB 大分的 ply 折算缺陷** — `domain/search.py:164-175, 344-353, 449-471`
- 杀分存表加 `max_depth - depth` 书签 ply，仅在同一局面同一 ply 存取时自洽；迭代加深下 TT 跨迭代复用且不清空，同一条目每迭代漂移 ±1，跨 ply 换位漂移更大——存的实际是 `-JIANGSHA + 2p`，与注释意图不符。
- 另外 EGTB 本地启发胜分（20000~85000）与云库 DTM 分（约 99000+）**全部超过 `MATE_TT_BOUND = 40000`**，会被 `_tt_score_to_relative` 误当作"根距离编码的杀分"做 ply 折算。
- **影响**：mate-in-N 距离报告失真、`_best_score`/PV 迭代间系统性漂移、可能偏好更慢的杀棋线路。不导致漏杀（数值量级仍高于静态分），属经典 TT 存储错误。
- **修复**：采用 Stockfish 方案（存 ply 无关常量，probe 按当前节点 ply 还原）；EGTB 大分 clamp 到 `MATE_TT_BOUND` 以下或加独立标志。

### D. 控制器 / UI 层

**D1【High】`_active_mcts` 单槽竞态 → 在飞 MCTS 无法停止 + 线程泄漏** — `app/engine_bridge.py:149, 159, 296, 307, 173-178`
- 单槽位：reset/pause 期间旧 MCTS 未退出时新搜索启动，旧线程 `finally` 把 `_active_mcts=None` **覆盖新引擎引用** → `stop_all()` 找不到新引擎，daemon 线程烧 CPU 直到 30s time_limit；`shutdown` 只 join 最近一个线程。`start_hint_search` 的裸 MCTS 线程（:231-241）同样不受 `stop_all` 管理。
- 正确性被 `move_piece` 的 owner 校验兜住（无双走子），但资源/CPU 泄漏真实存在。
- **修复**：改 `dict[int, MCTSEngine]` 按线程 id 管理，`finally` 只移除自己的条目。

**D2【High】Pikafish UCI 握手在 Qt 主线程阻塞最长 10 秒** — `ui/window.py:51` + `app/engine_bridge.py:75-96` + `domain/pikafish.py:386-451`
- `singleShot(0)` 只推迟到窗口显示后，握手本身仍同步跑主线程：引擎启动缓慢/挂起时 UI 假死最长 10s。
- **修复**：构造/握手放入后台线程，经 QObject 信号回主线程。

### E. 测试 / 训练脚本层

**E1【High】空基线文件导致"恒真"假通过** — `tests/compare_movegen.py:38-73`
- 基线文件存在但为空时循环不执行，`total=0, mismatches=0` 打印"100% 一致"，退出码 0——走法生成回归完全无覆盖。
- **修复**：`main()` 校验文件存在且 `total > 0`，否则 `sys.exit(1)`。

**E2【High】训练全程无全局种子，不可复现** — `scripts/train_nnue.py:60-103, 175, 201-202`
- 数据生成、切分、洗牌全用未播种的全局 `np.random`；只有权重初始化单独 `RandomState(42)`。两次运行产生不同模型，`--resume` 对比无意义。
- **修复**：`main()` 开头 `np.random.seed(...)`（或 `--seed` 参数）。

**E3【High】"误导先验"测试与注释意图相反，断言近乎空转** — `tests/smoke_engine.py:166-171` vs `domain/mcts.py:137`
- 注释声称"给非吃車走法极高误导性先验"，但传参 `priors={(5,0,0,0): 0.9}` 而 `priors.get(move, 1.0)`——未指定的走法（含吃車）先验 1.0 反而更高，误导走法实际被**压低**。若 PUCT 先验实现被改成"先验越大越优先"，本测试仍会通过。
- **修复**：其余走法显式设 0.1、误导走法设 0.9，或改为无先验 vs 强误导先验的对照实验。

### F. 核心棋规层

**F1【High 防御性】走法生成可产生"吃对方将"的非法走法（仅损坏局面下）** — `domain/game.py:539-548, 389-409`
- `_append_if_legal`/`_would_be_illegal` 未排除目标格为对方将。实证：构造"红车打将、黑将无路"局面后 `get_all_legal_moves` 返回吃将走法；`move_piece` 有防御分支拦住，但**搜索/MCTS/EGTB 直接消费 `get_all_legal_moves` 的路径没有**，选中后会把对方将移出棋盘，破坏哈希/重复检测一致性。
- 合法对局中不会触发（对方将不可能处于被吃状态）；外部直接赋值棋盘、残局导入、编辑器场景会暴露。
- **修复**：`_append_if_legal` 中 `if target != '.' and target.upper() == 'K': return`。

---

## 三、中等问题（Medium）

### AI 工作器
- **M-AI-1** 取消无法中止在途回合：`cancel()` 置标志但 `_run_search` 的 Alpha-Beta（最长 26s）不响应；暂停后新旧两个 worker 并发占 API 计费。→ 把取消接到 `SearchEngine.stop()`。（worker.py:66-67, 112, 435-441）
- **M-AI-2** 单步最坏 ~40 分钟：600s 读取超时 × 最多 4 轮 tool 调用（`tool_choice='required'` 强制每轮调工具）。建议轮间共享总预算。（worker.py:488, constants.py:9）
- **M-AI-3** 仲裁提示词注入清洗只剥反引号/控制字符，对方模型全文（含推理链）原文嵌入仍可文本级注入。建议围栏内声明"证据数据非指令"或摘要化。（prompts.py:587-588）
- **M-AI-4** `AIManager.clear_queue` 不复位 `ai_move_in_progress` 与 `_active_thread`，依赖调用方手动 `set_busy(False)` 的隐式契约。（manager.py:25-29）
- **M-AI-5** 兜底文本解析把 DeepSeek `reasoning_content` 也纳入候选文本，推理结尾的"讨论过但未选中"坐标可能被误取。建议 content 与 reasoning 分池。（worker.py:133-136, 143-145, 220）
- **M-AI-6** worker 持有 live `self.game`，reset 是原地变异：陈旧 worker 在重置后分析新开局局面，浪费整轮 API（结果被版本门控丢弃，不落子）。建议传入快照。（worker.py:63, controller.py:203）

### 搜索引擎与评估
- **M-SEARCH-1** qs 热路径"全量生成合法走法再过滤吃子"，而 `game.py` 定向生成器已支持 captures_only，浪费数倍开销。（search.py:535, game.py:528-535）
- **M-SEARCH-2** `evaluate()`（有机动性 ±320 分）与 `evaluate_fast()`（跳过）不一致，docstring 却声称等价：LLM 工具拿到的评估与引擎内部评估不可直接比较。（evaluation.py:307-310, 420）
- **M-SEARCH-3** 搜索无重复局面/长将防护：Alpha-Beta 与 MCTS 在循环局面来回走子，TT 复用还给重复局面正分，游戏层 `move_piece` 之后才判定，搜索得不到反馈。建议带历史栈检测或根节点对重复走法降权。
- **M-SEARCH-4** MCTS `avg_value` 是**对手视角**（根子节点为对手方）：红方大优时日志/UI 反而显示低值，且与 Pikafish `get_top_moves` 兼容层视角不一致。输出前统一转换。（mcts.py:57, 186, 372）
- **M-SEARCH-5** 残局每个叶节点都做全盘扫描 + 启发式判定，且必胜/必和启发分（单車必胜等）被当"准杀分"注入搜索，启发有误会直接扭曲评估；本地判定无进程级缓存。（search.py:585-593, egtb.py:194-206）
- **M-SEARCH-6** NNUE 累加器更新/撤销异常被 `except Exception: pass` 静默吞掉，累加器永久偏离棋盘且无提示，后续 `_fast_eval` 全部用错值。（search.py:734-742, 790-798）
- **M-SEARCH-7** MCTS `_simulate` 的 EGTB 分支 sigmoid 在 |score|>4000 饱和为 0/1，DTM 步数信息全丢，且与手评分支混用标尺。（mcts.py:258-269）

### Pikafish / EGTB
- **M-ENG-1** 异步路径不标记引擎不可用（与 B1 同源，错误文案"无响应/超时"误导排障）。（pikafish.py:279-280）
- **M-ENG-2** `_top_moves` 跨线程无锁读写（reader 线程持锁 append，调用方无锁迭代拷贝），当前无生产调用者，属潜在隐患。（pikafish.py:297-346）
- **M-ENG-3** 本地启发"多子有車必胜"未考虑士象全防守：車馬/車炮 vs 士象全（官和）被无条件判 +85000 必胜。（egtb.py:319-322）
- **M-ENG-4** 云查询异常覆盖不全（`http.client.HTTPException` 子类不捕获）+ `resp.read()` 无字节上限；`probe()` 对 `probe_cloud` 无 try/except，UI 层直接调用时异常会炸线程。（egtb.py:112-125, 209-212）
- **M-ENG-5** EGTB 模块级全局状态（缓存/熔断计数）无锁，并发时最坏重复查询/计数偏斜（生产路径 `allow_cloud=False` 风险有限）。
- **M-ENG-6** `evaluate_position`/同步 `search()` 异常处理不一致（前者 `pass` 不标记不可用），且 `evaluate_position` 返回**走子方视角**分，与"统一红方视角"约定相反——目前无生产调用者，属潜伏 API。（pikafish.py:162-164 vs 218-221）

### 控制器 / UI
- **M-UI-1** LLM 结果经**非 QObject 接收者**回传（controller.py:639, 1254）：PyQt6 实验证实当前结构（connect 在主线程）下排队到主线程**目前安全**，但属隐式语义，重构即退化。建议显式 `QueuedConnection` 或 QObject 接收者。
- **M-UI-2** `on_human_move` 缺 `is_busy()` 防御：若未来某路径使 busy 残留为 True 且轮到人类，玩家无法走子且无提示（死锁）。（controller.py:264-271）
- **M-UI-3** 提示搜索固定 30s，与主搜索 depth×3s 缩放及 docstring 不一致，人类等参考提示最长 30s。（engine_bridge.py:224-225, 256）
- **M-UI-4** LogManager `insertHtml` 把 LLM 多行思考文本的 `\n` 折叠成空格，多行分析变一长行。（logging.py:46-47）

### 核心棋规
- **M-GAME-1** 初始局面哈希从未记录，含初始局面的三重复被漏计（实证：往返循环回到初始局面时由中间局面先触发掩盖，理论缺口仍在）。→ `__init__`/`reset`/`from_snapshot` 写入初始 hash。（game.py:45, 189, 772-815）
- **M-GAME-2** `get_opening_move` 返回的走法不做合法性校验（当前 165 条线实证全合法，属防御性缺口）。（openings.py:1836-1863）
- **M-GAME-3** `from_snapshot(king_pos=None)` 直接 `dict(None)` 抛 TypeError（实证复现），公开 API 应防御。（game.py:82）
- **M-GAME-4** 将杀/困毙/重复判负时 `current_player` 不切换，UI 若直接读它显示"轮到红方"可能误导。（game.py:241）

### 测试
- **M-TEST-1** perft 黄金值未钉死：perft(2) 只断言 `1900<=n<=2000`（真实 1920）、perft(3) 只断言 `>1000`（几乎恒真）；<5% 计数偏差静默通过，且仅初始局面无中局边缘局面。（test_perft.py:72-80）
- **M-TEST-2** `make_game_with_board` 不重建增量缓存，携带标准棋盘的陈旧缓存——目前测试恰好通过是侥幸而非设计。（smoke_engine.py:35-47）
- **M-TEST-3** EGTB 参考求解器只比对 win/lose/draw 类别，精确 DTM 仅靠 4 个黄金局面钉死；前向一致性判别器是"表自洽"检查，整条线一致的错误值不会被发现。（test_egtb.py:268-319, 375-472）
- **M-TEST-4** `verify_fixes.py` 用相对路径 Popen pikafish，缺失时裸抛 FileNotFoundError，kill 后未 wait。（verify_fixes.py:38-44, 73）
- **M-TEST-5** EGTB 吃子子表抽样的 draw/lose 断言在 probe 未命中时静默跳过（"通过但未验证"）。（test_egtb.py:636-648）
- **M-TEST-6** 评估对称性/一致性测试绕过将军（±50）与机动性（每走法 2.0）项，这两项写成非对称实现时测试全绿。（test_evaluation.py:86-103, 113-150）

---

## 四、轻微问题 / 改进建议（Low，精选）

**跨文件**
- **numpy 未声明**：`domain/nnue.py`、`scripts/train_nnue.py` 使用 numpy，但 `requirements.txt` 无此依赖（本审查独立发现）。
- **搜索强度三处不一致**：`ui/panel.py:211,267` 硬编码 5 vs `SEARCH_MAX_DEPTH=8` vs CLAUDE.md 声称 1~6。
- **重试延迟不一致**：controller.py:752,818 用 `retry_count*2000`，`AI_RETRY_DELAY_MS=3000` 未被使用。
- **39 处宽泛 `except Exception`**，部分路径（如 search.py:734-742）静默吞错无日志（见 M-SEARCH-6）。

**ai/worker.py**
- :288 `tmp_game.board = board` 死代码；:211-216 对 tool_calls 重跑提取不可达；:31,86,90 `tokens` 永远传 0；:88-91 兜底异常无 traceback；:459-460 DeepSeek `think` 参数名需对照当前 API 文档确认；:129 content 为列表时 `.strip()` 抛 AttributeError；:428 坐标未 strip 空白/引号；:82 单边坐标错误语义隐晦。
- `ai/parser.py:5` `[A-I]\d{1,2}` IGNORECASE 会匹配"维生素B2"等正文；建议坐标边界校验。

**domain/prompts.py**
- :517-542 仲裁摘要截断 780 字符时直接 break，**尾部最终结论句被丢掉**（最该保留的部分）；建议截断保 tail 三句。

**domain/search.py**
- :227-231 单走法捷径不设置 `_best_score`/`_nodes_searched`，调用方读到恒 0.0；:552 qs 超时返回 fail-hard alpha 与主搜索 fail-soft 不一致（仅略保守）；:57 `TT_MAX_SIZE=1_000_000` 约 100-300MB 偏大；:87-94 hit_rate 把深度不足计入 miss；:306-311 on_progress 与 progress_callback 双设时重复回调。

**domain/nnue.py**
- :296-306 `get_nnue()` 懒加载无锁（良性竞态），且首次检查失败后 `_nnue_checked=True` 永久返回 None，必须显式 `reload_nnue`；:151-171 NNUE 输入未做红黑镜像，网络需自学颜色对称。

**domain/game.py**
- :159 `from domain.evaluation import RED_PST` 在热路径内执行（建议移模块顶部）；:389-409 `_would_be_illegal` 临时改盘无 try/finally；:794 `i2` 死变量；:194-196 历史截断 500 条与重复检测边界（被 120 手限着先触发掩盖）；:226-232 自然限着按 ply 累进，若规则意图是 240 着则系数差一倍，建议按目标竞赛规则确认。

**domain/fen.py**
- 仅序列化无解析器；维度硬编码；`current_player` 非 1 一律输出 'b'。

**domain/openings.py**
- 权重=剩余步数，语义是"偏好更长书线"而非"常见走法优先"，文档表述需澄清；实际 165 条线 vs 文档 46 条，数据漂移。

**app/controller.py**
- :62,164,225 `stats['search_nodes']` 死字段；`move_count` 只统计 AI 不走人类；:909-912 `_random_move` 失败分支静默停滞；:136-137,151-152 `start_game` 模型解析失败静默 return；:1410-1413 thinking_timer 无父对象不 deleteLater。

**ui/**
- board.py:164-201 无拖拽/键盘操作；AI 思考期间玩家仍可点选 AI 棋子；window.py:51 直接访问 `_engine.init_pikafish` 私有成员；window.py:166-170 `restoreState` 返回值未检查；engine_bridge.py:217-221 直接读写 `pf._proc`/`pf._available` 私有字段。

**tests/scripts**
- test_evaluation.py:166-176 `s2>0`/`s3>-10` 近乎恒真断言；test_perft.py:97 函数内 `import random` 未播种；smoke_engine.py:92 死导入；compare_movegen 基线由当前实现自身生成，只能证明"重写前后等价"（正确性依赖 perft 独立黄金值，而 M-TEST-1 显示黄金值过松）；train_nnue.py:313-314 `X is None` 死代码、:207-210 样本为 0 时除零、:146-161 镜像增强三重循环慢、:250 固定 lr 无调度无早停；test_egtb.py `load_tables()` 全量可能耗时数分钟未标注。

---

## 五、亮点（做得好的地方）

1. **版本门控体系（定海神针）**：`game_version` + `AIManager.cancel_version` 双层门控 + `_check_callback_valid` 的 `needs_cleanup` 分离设计，逐路径验证无重复走子/无 busy 死锁；`_defer_ai_move` 捕获快照版本。
2. **走法生成正确性**：定向生成 + 逐候选 `_would_be_illegal` 过滤，1838 个随机局面与暴力法逐走法一致；`is_in_check` 反向检测与正向移动规则的马腿/过河公式完全互逆；困毙判负、判决优先级（重复→将杀→限着→无子力）正确；长将判定显式记录走子方，双方长将判和。
3. **增量缓存一致性**：`move_piece` 与 `_compute_zobrist`/`_recompute_incremental` 公式逐项一致；`search.py` 的 make/unmake 与 `move_piece` 完全对称；`_king_pos` 缓存失效自动全盘扫描自愈（陈旧缓存注入实测通过）。
4. **Negamax+PVS 实现标准**：零窗试探、LMR 重搜、将军延伸每分支仅一次、qs 被将军禁止 stand_pat 强制应将、超时保留上一完整迭代最佳走法；TT 键含走子方 + null move 改 current_player 天然防串表。
5. **Pikafish 进程工程**：先杀进程再收锁、reader 线程 EOF 哨兵唤醒等待方、`_purge_lines` 二次排空、启动握手 10s 超时带退出码诊断（0xC0000135/0xC000001D）、列表参数无 shell 无注入面、UCI 走法双重合法性校验 + TOCTOU 快照。
6. **EGTB 工程质量**：稠密组合索引无哈希碰撞、文件格式魔数+CRC32+原子落盘、DTM 越界硬报错、黑方攻方 180° 旋转归一化；正/负缓存 TTL+熔断 120s；生产路径统一 `allow_cloud=False` 无同步 HTTP。
7. **MCTS 四阶段正确**：PUCT 对手视角取反、backprop 交替、工作快照 + finally 逆序 unmake；"无合法走法=输"的终局值恰好符合象棋规则。
8. **提示词工程深思熟虑**：合法走法分组紧凑格式化 + 战术标注、"只印排名不印分数"避免量纲误导、仲裁候选随机化 + 对称客观事实包消除信息不对称、畸形 tool_calls 防御成体系（null function/list arguments/非字符串 JSON 均有兜底）。
9. **测试质量整体高**：退出码约定规范、`test_egtb` 多层交叉验证（黄金局面链 + 独立参考求解器 + 前向一致性判别器）、`test_incremental` 测"缓存=全量"不变量、评估对称性行为级断言、smoke_engine 战术局面防伪搜索、EGTB 用例显式禁云防 flaky。
10. **安全细节**：`trust_env=False` 防环境代理泄露、HTTP 错误响应体截断 500 字符防网关回显 key、DeepSeek `image_url` 双重过滤、API key 仅 `${ENV_VAR}` 引用。

---

## 六、修复优先级建议

### 立即修复（可用性故障，一行防御）
1. **A1** — worker 坐标 `isinstance` 校验（游戏卡死）→ `ai/worker.py:428`
2. **A2** — depth/top_n 钳制移入 try → `ai/worker.py:257-258`
3. **B1** — 引擎死亡标记 `_available=False` + 重启 → `pikafish.py` / `engine_bridge.py`

### 尽快修复（棋力/资源/正确性）
4. **C1** — TT 杀分 ply 折算 + EGTB 大分 clamp → `search.py`
5. **D1** — `_active_mcts` 多槽位管理 → `engine_bridge.py`
6. **D2** — Pikafish 握手移出主线程 → `window.py` / `engine_bridge.py`
7. **B2** — `isready`/`readyok` 握手替代固定 50ms purge → `pikafish.py`
8. **A3** — move_piece 合法性校验 + 错误反馈闭环 → `ai/worker.py`
9. **F1** — 吃将走法防御 → `game.py:539-548`
10. **E1/E2/E3** — 测试假通过/不可复现/语义反相 → `compare_movegen.py` / `train_nnue.py` / `smoke_engine.py`

### 后续（Medium 按序）
- M-AI-1（取消接入搜索停止）、M-SEARCH-3（搜索重复防护）、M-SEARCH-2（评估口径统一）、M-GAME-1（初始局面重复计数）、M-UI-1（显式 QueuedConnection）、M-ENG-3（士象全官和）、M-SEARCH-4（视角统一）、M-TEST-1（perft 精确值）
- 独立发现：`numpy` 补入 requirements.txt；搜索强度三处常量统一。

---

*本报告由 6 个子系统并行审查汇总生成；标注"实证"的结论均经代码运行验证。*
