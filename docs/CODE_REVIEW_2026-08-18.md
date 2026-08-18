# AIChineseChess 代码审查报告（第三轮）

- 审查日期：2026-08-18
- 审查基线：HEAD `d48789c`（2026-08-17）+ 工作区未提交改动（P1–P4 修复，`git diff` 共 3 文件 +22/−7 行）
- 审查方法：未提交 diff 逐项验证 + 全量测试套件运行 + 关键路径通读（TT probe 语义、Pikafish 锁内重置、重复检测/开局库、worker 工具链、engine_bridge 生命周期）
- 前两轮报告：`docs/CODE_REVIEW.md`（2026-08-13）、`docs/CODE_REVIEW_2026-08-17.md`（2026-08-17）

## 一、总体结论

**当前工作区状态健康：未提交的 P1–P4 修复全部正确，全量测试套件（11 个脚本 + 49 文件编译检查）全绿，未发现新引入的 Critical/High 缺陷。**

本轮审查的重点是自第二轮报告以来的唯一增量 —— 工作区中未提交的 P1–P4 修复（与 `docs/CODE_REVIEW_2026-08-17.md` 文首"修复状态"表逐条对应）。逐项核对实现正确性、边界语义与回归测试有效性，结论全部成立；另对核心模块做了新一轮通读，未发现此前两轮未覆盖的新问题。

---

## 二、未提交改动逐项验证（P1–P4）

### P1 — `domain/search.py` 超时截断存 LOWER_BOUND ✅

改动：走法循环新增 `timed_out` 标志；循环后 `timed_out` 时统一存 `TTFlag.LOWER_BOUND`（不再按 `best > orig_alpha` 分 EXACT/UPPER_BOUND）。

验证：
- **probe 侧语义核对**（`search.py:93-95`）：LOWER_BOUND 分支仅在 `score >= beta` 时返回命中，用作剪枝。部分搜索的 best 是真实分值的下界，存 LOWER_BOUND 语义安全；存 EXACT/UPPER_BOUND 才会污染后续 probe。修复方向正确。
- **边界核对**：`best == -inf`（超时未搜任何走法）仍走 `_fast_eval` 且不存 TT（原有行为未变）；`best != -inf` 时 `best_move` 必已赋值（同一代码块更新），store 的 `best_move` 非空；beta 截断提前返回路径不受影响。
- **回归测试**：`tests/test_tt_cutoff.py` 桩化递归确定性触发截断路径，断言 LOWER_BOUND + 分值 -15 + best_move 正确；对照组完整搜索仍存 EXACT。实测全部通过。

### P2 — `ai/worker.py` null function 防御 ✅

改动：`tool_call.get('function', {})` → `tool_call.get('function') or {}`。

验证：键存在但值为 `null` 时 `.get` 默认值确实不生效（返回 None），`or {}` 正确兜底；`func.get('name')` 在 dict 上安全。与下游 `isinstance(args, dict)` 防线（worker.py:716）配合，弱模型畸形输出的整条路径均有防御。✅

### P3 — `ai/worker.py` options 保留键过滤 ✅

改动：`payload.update(self.model_info.options)` → 过滤 `reserved = ('model', 'messages', 'stream', 'tools', 'tool_choice')` 后再合并。

验证：程序逻辑构造的键（model/messages/stream/tools/tool_choice）不再被 `models.json` 的 `options` 误覆盖；附加参数（temperature/max_tokens 等）正常透传。`tool_choice` 由 `model_info.tools_choice` 在 764-768 行显式设置，与过滤逻辑一致，无冲突。✅

### P4 — `domain/pikafish.py` `_top_moves` 锁内重置 ✅

改动：`search_async` 的缓存重置从调用线程锁外移入 daemon 持锁段（与 `_search_locked` 同序：重置 → purge → position → go）。

验证：
- **竞态窗口消除**：锁外重置会让并发的 `search_atomic` 在"重置→搜索"窗口内 finalize 进本搜索的空缓存；锁内重置后该窗口消失。`search_atomic` 的快照在持锁期间拷贝（`list(self._top_moves)`），不受后续 async 重置影响。
- **回归测试**：`tests/test_pikafish_concurrency.py` 30 轮双局面并发（持锁启动 async + 立即 atomic），`_top_moves` 无污染。实测 30/30 一致。

---

## 三、全量测试结果（2026-08-18 实测）

| 测试 | 结果 |
|------|------|
| `tests/test_tt_cutoff.py`（P1 回归） | ✅ 截断 LOWER_BOUND + 完整搜索 EXACT 对照 |
| `tests/test_perft.py` | ✅ perft(1)=44 / (2)=1920 / (3)=79666 精确值 |
| `tests/test_incremental.py` | ✅ 100 局面 + make/unmake 50 轮 + from_snapshot + 计数一致 |
| `tests/test_evaluation.py` | ✅ 对称性 50 局面 + evaluate_fast 100 局面 + 将军/优势/残局 |
| `tests/smoke_engine.py` | ✅ 走法生成 / AB / MCTS / EGTB / 自然限着 / 开局库 165 线全合法 |
| `tests/test_notation.py` | ✅ 传统棋谱记法全用例 |
| `tests/test_arbitration.py` | ✅ A1–A6 分支级（默认无 API 模式） |
| `tests/test_commentary.py` | ✅ 28 项 sanity checks |
| `tests/test_egtb.py` | ✅ 0 失败（61s，含 v4 文件门禁/旋转对称/子表一致性） |
| `tests/test_pikafish_concurrency.py`（P4 回归） | ✅ 30 轮并发无 `_top_moves` 污染 |
| `tests/compare_movegen.py` | ✅ 3020 局面 0 不一致 |
| `py_compile` 全库 49 个 .py | ✅ ALL COMPILED OK |

---

## 四、新发现（均为低优先级）

### N1【流程】文档与提交状态不一致
`docs/CODE_REVIEW_2026-08-17.md` 文首称 P1–P4"2026-08-17 修复完成（同日提交）"，但 `git status` 显示这些改动仍在工作区未提交（HEAD 仍为 `d48789c`）。建议尽快提交，或修正文档表述为"待提交"。

### N2【测试基建】test_commentary 的 QBasicTimer 警告
运行 `tests/test_commentary.py` 时 stderr 出现 5 条 `QBasicTimer::start: current thread's event dispatcher has already been destroyed`。根因：模块级 `QCoreApplication` 未在退出前正确销毁，worker 线程迟到 emit 信号时事件分发器已销毁。测试结果不受影响（28 项全过），属测试基建噪音，非生产缺陷。建议测试结尾显式 `app.quit()` / 等待 worker 线程 join。

### N3【确认】已知暂缓项状态未变（风险均低）
- **M-GAME-1 初始局面重复缺口**：`_position_history` 仍不含初始局面哈希，回到初始布局的三次重复会漏判一次。需对局回到初始布局且无中间局面先触发，实际概率低，维持暂缓合理。
- **M-GAME-2 开局库走法合法性校验**：仍无前置校验，但 controller 中 `move_piece` 失败会优雅降级到引擎/LLM 路径（`controller.py:407-415`），风险低。
- **M-GAME-4 终局 current_player 不切换**：UI 在 `game_over` 时优先显示胜负，不依赖 current_player 显示回合，影响有限。

### N4【微瑕】单走法捷径不设 `_best_score`
`search.py:259-260`：`len(all_moves) == 1` 时直接返回，`_best_score` 保持 0.0。`_run_search_local`（worker.py）在唯一走法时显示"搜索最佳评分: +0"，语义略误导；功能正确（走法即唯一合法走法），可顺手补 `_best_score` 赋值。

---

## 五、建议

1. **提交 P1–P4**（当前工作区改动与文档"修复状态"表一致，验证全绿，具备提交条件）。
2. 修正 `docs/CODE_REVIEW_2026-08-17.md` 的"同日提交"表述，或提交后保持文档与 git 一致。
3. 测试基建：`test_commentary.py` 结尾正确销毁 QCoreApplication（消除 QBasicTimer 噪音）。
4. N4 属可选微优化，可随下次提交顺手处理。
5. 暂缓项（M-GAME-1/2/4）维持暂缓，无新增风险信号。

---

*本报告基于 HEAD `d48789c` + 工作区未提交 diff 的通读与全量测试实证；前两轮报告见 `docs/CODE_REVIEW.md` 与 `docs/CODE_REVIEW_2026-08-17.md`。*
