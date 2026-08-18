# AIChineseChess 代码审查报告（第四轮）

- 审查日期：2026-08-18
- 审查基线：HEAD `f51aa93`（第三轮报告 `docs/CODE_REVIEW_2026-08-18.md` 所审 P1–P4 已提交）+ 工作区未提交改动（仅 `CLAUDE.md` 文档修正，无代码变更）
- 审查方法：全部 Python 源文件逐行通读（domain / ai / app / ui / services / scripts / tests，约 9000 行）+ 前三轮修复项回归核对 + 关键路径实证验证（offscreen Qt 像素级采样、EGTB 生产路径对拍）+ 全量测试套件运行
- 前几轮报告：`docs/CODE_REVIEW.md`（08-13）、`docs/CODE_REVIEW_2026-08-17.md`（08-17）、`docs/CODE_REVIEW_2026-08-18.md`（08-18 第三轮）

> **后续变更注记（2026-08-18 同日）**：本轮 R4-1 修复的 `domain/egtb.py`、R4-L1 所在的 `domain/search.py`，以及 `tests/test_egtb.py`/`tests/test_tt_cutoff.py`，已按"精简代码量"决策整体移除（自研 Alpha-Beta 引擎 + 本地 EGTB 残局库一并删除；`SearchEngine` 的 make/unmake/超时工具迁入 `domain/mcts.py`）。下文涉及这些文件的内容为审查时点快照，保留供追溯。

## 修复状态（2026-08-18 本轮）

| 条目 | 修复内容 | 回归验证 |
|------|----------|----------|
| R4-1 | `domain/egtb.py` `probe()` 快速否定的攻击子集合排除兵（`('R','N','C')` / `('r','n','c')`），恢复 KkRp/KkNp/KkCp 三张"攻子对单卒"DTM 表的生产可达性 | 新增 `tests/test_egtb.py` 第 9 节（生产 `probe()` 路径对拍，含 KkRr 对照）；修复前 3 FAIL / 修复后全 PASS ✅ |
| R4-2 | `ui/board.py` `capture_board_image` 目标 pixmap 增加 `setDevicePixelRatio(scale)`，实现真 2× 超采样（棋盘铺满画布，消除黑边） | `tests/measure_vision_image.py` 新增三角落黑边覆盖检查；offscreen 像素采样对照：修复前棋子位置全黑，修复后棋子/木色背景正确 ✅ |
| R4-L1 | `domain/search.py` 单走法捷径：make → `_fast_eval(game, 1)` → unmake，`_best_score` 保持红方视角语义（不再显示误导的 "+0"）；`_best_move` 同步赋值 | `smoke_engine`/`test_tt_cutoff`/`test_evaluation` 全绿；唯一走法局面评分非 0 且棋盘/缓存精确恢复 ✅ |
| R4-L2 | `app/engine_bridge.py` MCTS top-moves 日志改显示走子方视角胜率 `1.0 - val`（val 为子节点=对手视角软胜率；M-SEARCH-4 展示面） | 纯日志文案，无行为变更 ✅ |
| R4-L3 | `domain/search.py` `TT_MAX_SIZE` 1M → 500k（满载常驻约 150-200MB；跨步复用保留，LRU 淘汰对单步命中率影响可忽略） | `smoke_engine` AB 部分 + `test_tt_cutoff` 全绿 ✅ |
| R4-L4 | `tests/test_egtb.py` 第 7 节改用项目内固定临时目录 `tests/.tmp_egtb`（手动 rmtree，无 chmod），消除沙箱环境 `TemporaryDirectory` 清理 `chmod 0o700` 被拒导致的 exit 1 误报；目录已 gitignore | `test_egtb` 全流程 exit 0 ✅ |
| R4-L5 | `domain/prompts.py` + `app/controller.py` 移除 `build_commentary_prompt` 的 `mover_in_check` 死参数与死分支（合法走子后走子方不可能被将军） | `test_commentary` 28 项 sanity 全绿 ✅ |
| R4-D1 | `CLAUDE.md`/`README.md` 测试清单补入 `tests/measure_vision_image.py` | 文档 ✅ |

验证：`test_egtb`（含新第 9 节）、`smoke_engine`、`compare_movegen`（3020 局面）、`test_perft`、`test_evaluation`、`test_incremental`、`test_notation`、`test_tt_cutoff`、`test_pikafish_concurrency`、`measure_vision_image` 全绿（详见第五节）。

---

## 一、总体结论

**核心正确性与并发纪律保持高位：未发现合法对局下的确定性崩溃、双走子或棋盘损坏。** 版本门控（`game_version` + `cancel_version`）、快照隔离、增量缓存、线程中继在前几轮修复后结构完整，本轮逐路径复核全部在位。

**本轮新发现 2 个此前三轮均未覆盖的实质性缺陷**（均经实证）：

1. **R4-1【中】EGTB 生产路径死表** — `probe()` 的 O(1) 快速否定把黑卒当作"攻击子"，导致 KkRp/KkNp/KkCp 三张"车/马/炮 对 单卒"DTM 表在 `search.py`/`mcts.py` 的生产调用中**永远查不到**（精确 DTM 退化为 NNUE/手工评估）。
2. **R4-2【高】视觉截图超采样失效** — `capture_board_image` 把 2× 画布当 1:1 渲染目标，棋盘只落在左上 1/4、其余 3/4 为黑边；视觉模型实际收到的是"小棋盘 + 大面积黑底"，视觉模式（README 卖点功能）实质不可用。

两个缺陷的共性：都发生在**前几轮审查已覆盖过的代码附近**，但缺少端到端的实证验证（EGTB 测试只测 `probe_local`/`table.probe` 直查，从不测生产 `probe()` 入口；视觉截图只量尺寸不验内容覆盖）。

---

## 二、新发现详述

### R4-1【中】`domain/egtb.py:40-44` — 快速否定误杀"攻子对单卒"DTM 表

**现象（实证）**：

```
局面 K(9,4) k(0,3) R(1,0) p(5,0)，红走，4 子
  probe_local 直查            : (99969.0, 3)   ← 车胜，3 步杀
  probe(带 material_counts)   : None           ← 生产路径（search/mcts 同款调用）
  probe(不带 material_counts) : (99969.0, 3)
KkNp / KkCp 同样：直查 (0.0, 255) 精确和棋 → 生产路径 None
```

**根因**：`probe()` 的快速否定条件是 `red_att > 0 and black_att > 0`，其中 `black_att` 统计 `('r','n','c','p')`。但 21 张 DTM 表的规范帧设计是"红=攻方，黑=防守方（a/b/p 或无）"——`KkRp`/`KkNp`/`KkCp` 三张表的黑色额外棋子**正是卒**，且按防守子处理。该否定条件（2026-08-17 精简 `611e35c` 引入）的注释声称"与 `_local_egtb` 判定范围一致"，只核对了启发式一侧（启发式对"双方都有攻击子"确实返回 None），漏掉了 `probe_local` 一侧的三张兵防守表。

**影响面**：`search.py:601`（Alpha-Beta 叶节点 `_fast_eval`）与 `mcts.py:247`（MCTS `_simulate`）两个生产调用点都传 `game._material_counts`，因此受影响。残局车对卒是常见残局：丢失精确 DTM 后，搜索对"马/炮对卒=精确和"等理论结论退化为子力估值（马≈400 厘兵的正分），可能高估进入 NkP/CkP 的价值、失去最快杀线路径指引。无崩溃/非法走法风险，纯棋力损失。

**修复**（2 行）：攻击子集合排除兵 → `('R','N','C')` / `('r','n','c')`。排除后否定条件仍精确：双方都有非兵攻击子时，规范帧黑方必含非防守子，21 张表与启发式均不可能命中，快速否定保持有效（KkRr 对照组实证仍返回 None）。

**为何前三轮漏检**：`test_egtb.py` 全部 8 节只调用 `table.probe()`/`probe_local()`（表层 API），从未调用生产入口 `domain.egtb.probe(board, player, count, material_counts)`；快速否定是精简提交新增的旁路，审查时只与 `_local_egtb` 的判定范围做了口径核对。本轮已补第 9 节回归测试钉死该路径。

### R4-2【高】`ui/board.py:204-228` — 视觉截图 2× 超采样失效（黑边）

**现象（实证，offscreen 像素采样）**：

```
当前实现（1180×1300 画布 → 缩放 600×661 JPEG）：
  红帅位置 (590,1179) : (0,0,0)     黑    ← 棋子位置是黑的
  黑将位置 (590,121)  : (0,0,0)     黑
  右上 (w-5,5)        : (0,0,0)     黑
  左上 (5,5)          : (190,152,103)    ← 只有左上 1/4 有 1:1 棋盘
修复后（setDevicePixelRatio(2)）：
  红帅 (590,1179)     : (161,48,48)  红棋子 ✓
  黑将 (590,121)      : (83,83,83)   黑棋子 ✓
  右上 (w-5,5)        : (190,152,103) 木色背景 ✓
```

**根因**：`pixmap = QPixmap(QSize(w*2, h*2)); self.render(pixmap)` —— `QWidget::render(QPaintDevice*)` 按**控件自身尺寸 1:1** 绘制到目标设备的左上角，不会自动放大。2× 画布只有左上 1/4 是棋盘，其余是透明像素（JPEG 无 alpha → 黑）。随后 `scaled(600px)` 把整张（含黑边）缩小，视觉模型收到的是 600×661 图里左上角约 300×325 的小棋盘 + 3/4 黑底。期间尝试的 `QPainter.scale()` 包裹方案无效——`render()` 会在目标设备上自建 painter，与外部活跃 painter 冲突（实测 "Cannot render with an inactive painter"）。正确做法是 HiDPI 语义：`pixmap.setDevicePixelRatio(scale)` 后 `render()` 按"逻辑尺寸 × 倍率"铺满绘制（Qt 对带 DPR 的 pixmap 目标按逻辑尺寸渲染）。

**影响面**：视觉模式（`vision_check` 勾选 + 非 DeepSeek 模型）发出的每张图片都是畸形的。功能自 508f247（"视觉截图放大一倍 2x 超采样"）引入起即未生效；`tests/measure_vision_image.py` 只打印 JPEG 大小与像素尺寸、从不验证内容覆盖，故三轮均未暴露。

**修复**：1 行（`pixmap.setDevicePixelRatio(scale)`）+ 注释。最终图片仍按 `VISION_IMAGE_MAX_WIDTH=600` 缩放（token 成本不变，清晰度提升：1180px 下采样而非 590px 上采样）。回归检查加在 `measure_vision_image.py`（三角落红通道 >60 防黑边）。

### 其他新观察（低优先级 / 流程）

> 以下各项本轮已全部修复，见文首"修复状态"表 R4-L1~R4-L5 / R4-D1。

- **L1【低，已修复】** `search.py:259-260` 单走法捷径不设置 `_best_score`（保持 0.0），`worker._run_search_local` 在唯一合法走法时显示"搜索最佳评分: +0"。功能正确（走法本身唯一），显示误导。第三轮 N4，仍未处理。
- **L2【低，仍存】** `engine_bridge.py:408-416` MCTS top-moves 日志的 `价值{val:.3f}` 是 `child.avg_value`（**对方**视角，`mcts.py:347-356` 未转换）——与 M-SEARCH-4 同源，仅日志展示问题，不影响走法选择。
- **L3【低，仍存】** `search.py` `TT_MAX_SIZE = 1_000_000`：OrderedDict + NamedTuple + tuple 键，满载约 200–400MB 常驻内存。长对局（多次搜索、TT 跨步复用不清理）下可达上界。可考虑 300k 起步 + 可配置。
- **L4【流程】** `test_egtb.py` 在 DSH 沙箱内运行时 `TemporaryDirectory` 清理阶段 `chmod 0o700` 被沙箱策略拒绝（workspace 内目录亦被拒），脚本以 exit 1 结束——**测试本体全部 PASS**（输出实证：黄金局面/参考求解器 295 局面 0 违规/前向一致性/镜像对称 等全 PASS）。与前两轮记录的环境限制一致；建议第 7 节改用 workspace 固定临时目录或捕获清理异常，避免 CI 误报。
- **L5【微瑕】** `domain/prompts.py` `build_commentary_prompt` 的 `mover_in_check` 分支对合法走子恒为 False（刚走子的一方不可能被将军），属死分支；`app/controller.py:1586` `thinking_timer` 无 parent（单实例、随 controller 销毁，实际无泄漏）。

### 前几轮已知暂缓项（状态未变，均低风险）

M-GAME-1（初始局面重复计数缺口）、M-GAME-2（开局库走法无前置合法性校验，controller 侧 `move_piece` 失败优雅降级）、M-GAME-4（终局 `current_player` 不切换，UI 不依赖）、M-SEARCH-2（两套评估口径，工具结果已标注来源）、M-SEARCH-3/4/7（搜索无重复局面防护 / MCTS 视角 / EGTB sigmoid 饱和）、M-AI-3（仲裁文本注入面，已有反引号+控制字符清洗）。本轮复核无新增风险信号。

---

## 三、前几轮修复项回归核对（抽样）

| 条目 | 位置 | 本轮核对 |
|------|------|----------|
| A1/A2/A3（worker 类型防线/钳制/校验自纠） | `ai/worker.py:724/467/350` | ✅ 在位 |
| B1/B2（引擎死亡标记/readyok 握手） | `domain/pikafish.py` `_run`/`_purge_lines` | ✅ 在位 |
| P1（超时截断 LOWER_BOUND） | `domain/search.py` 走法循环 `timed_out` | ✅ 在位，`test_tt_cutoff` 通过 |
| P4（`_top_moves` 锁内重置） | `domain/pikafish.py` `search_async` | ✅ 在位，`test_pikafish_concurrency` 30/30 通过 |
| D1（`_active_mcts` 按线程 dict） | `app/engine_bridge.py:70` | ✅ 在位 |
| D2（init 后台线程 + shutdown 守卫） | `app/engine_bridge.py:87-100` | ✅ 在位 |
| F1（吃将防御） | `domain/game.py` `_append_if_legal` | ✅ 在位 |
| M-AI-4（`clear_queue` 复位 busy） | `ai/manager.py:25-33` | ✅ 在位 |
| M-GAME-3（`from_snapshot(None)` 自愈） | `domain/game.py:151` | ✅ 在位（本轮第 9 节测试即用 `from_snapshot` 建局） |
| 版本门控双层（`game_version`/`cancel_version`） | controller 各回调入口 | ✅ 逐路径复核：reset/pause/human-move/stale-callback 均无双走子路径 |
| 自然限着 vs 规则原文 | `domain/game.py:254-261` vs `中国象棋规则.txt` 第五节 | ✅ 120 未吃子步=和、将杀/困毙与三局面循环优先（重复检测在限着之前），与规则第 90-94 行一致；"每方将军最多计入十步"的程序化例外未实现，但循环将军先触发长将判决，实际影响≈0 |
| 依赖方向（domain 基座单向依赖） | 全库 import 图 | ✅ 无逆向依赖 |
| 安全细节 | `trust_env=False`、DeepSeek 过滤 `image_url`（controller + worker 双保险）、日志 HTML 转义 + CRLF 归一、`${ENV}` 占位符 | ✅ 在位 |

---

## 四、本轮修复的实证记录

1. **EGTB 死表**：修复前 `tests/test_egtb.py` 第 9 节 3 FAIL（KkRp `(99969.0,3)`/KkNp `(0.0,255)`/KkCp `(0.0,255)` 均被生产路径拒为 None）；修复后 10 项全 PASS，KkRr 对照组（双方非兵攻击子）仍正确否定为 None。
2. **视觉截图**：offscreen 渲染，采样红帅/黑将/红车/右上角四个物理像素点——修复前棋子点全黑 `(0,0,0)`，修复后分别为 `(161,48,48)`/`(83,83,83)`/`(163,48,48)`/木色 `(190,152,103)`；`measure_vision_image.py` 新增三角落覆盖检查 PASS。

---

## 五、全量测试结果（2026-08-18 实测，修复后）

| 测试 | 结果 |
|------|------|
| `tests/test_egtb.py`（含新增第 9 节生产路径） | ✅ 全 PASS（第 7 节清理阶段的沙箱 chmod 拒绝为环境限制，见 L4） |
| `tests/smoke_engine.py` | ✅ 全 PASS（走法/AB/MCTS/EGTB/限着/165 开局线） |
| `tests/compare_movegen.py` | ✅ 3020 局面 0 不一致 |
| `tests/test_perft.py` | ✅ 44 / 1920 / 79666 精确值 |
| `tests/test_evaluation.py` | ✅ 对称性/增量等价/将军奖惩 |
| `tests/test_incremental.py` | ✅ 100 局面 + make/unmake 50 轮 |
| `tests/test_notation.py` | ✅ 传统棋谱全用例 |
| `tests/test_tt_cutoff.py` | ✅ 截断 LOWER_BOUND + 完整 EXACT 对照 |
| `tests/test_pikafish_concurrency.py` | ✅ 30 轮并发无污染 |
| `tests/measure_vision_image.py`（新增覆盖检查） | ✅ 无黑边，尺寸正常 |

---

## 六、建议

1. **（已完成）** R4-1 / R4-2 / R4-L1~L5 / R4-D1 全部修复 + 回归验证，见文首"修复状态"表。
2. 暂缓项（M-GAME-1/2/4、M-SEARCH-2/3/7、M-AI-3）维持暂缓，无新增风险信号。
3. 本轮工作区改动（2 个功能修复 + 5 个低优先级项 + 测试/文档）已全量回归验证，具备提交条件。

---

*本报告基于 HEAD `f51aa93` + 工作区 CLAUDE.md 文档改动的全量逐行审查；两个新缺陷均经像素级/对拍级实证，修复均附回归测试。前几轮报告见 `docs/CODE_REVIEW.md`、`docs/CODE_REVIEW_2026-08-17.md`、`docs/CODE_REVIEW_2026-08-18.md`。*
