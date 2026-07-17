"""工具定义、系统提示词、用户提示词 — 中国象棋 AI 的核心沟通层

设计原则：
1. 系统提示词 = 模型的"终身知识"——规则、策略、禁忌只写一次
2. 用户提示词 = "当前局面"——棋盘状态、合法走法、即时反馈
3. 仲裁提示词 = "中立裁决"——仅需评判标准 + 两个候选走法
4. 工具定义遵循：说明用途 / 参数值域 / 保持简洁
5. 提供合法走法列表，让 LLM 专注于"选择"而非"生成坐标"
"""

from domain.game import ChineseChessGame
from domain.constants import BOARD_HEIGHT, BOARD_WIDTH, PIECE_SYMBOLS, MCTS_TIME_LIMIT, format_coord

# ── 人类玩家 Sentinel ──
class _HumanSentinel:
    id = 'human'
    name = '人类'
    def __eq__(self, other: object) -> bool:
        return isinstance(other, _HumanSentinel) or (
            hasattr(other, 'id') and other.id == 'human')

HUMAN_MODEL = _HumanSentinel()

# ══════════════════════════════════════════════════════════════════════════════
# 工具定义
# ══════════════════════════════════════════════════════════════════════════════

MOVE_PIECE_TOOL = {
    "type": "function",
    "function": {
        "name": "move_piece",
        "description": (
            "提交中国象棋走法。必须调用此工具来走子，"
            "从下方提供的合法走法列表中选择。"
            "不要在文本中输出坐标，走子无效。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "起始坐标。列字母 A~I，行数字 1~10。如 'H10'（红车原位）。"},
                "to":   {"type": "string", "description": "目标坐标。列字母 A~I，行数字 1~10。如 'H8'（车前进一步）。"}
            },
            "required": ["from", "to"]
        }
    }
}

SEARCH_BEST_MOVE_TOOL = {
    "type": "function",
    "function": {
        "name": "search_best_move",
        "description": (
            "调用本地 Alpha-Beta 搜索引擎分析局面，返回评分最高的候选走法。"
            "用途：①验证你的候选走法是否在引擎推荐中；"
            "②复杂中局获取战术参考；③检查隐藏的捉双/抽将/杀棋。"
            "注意：引擎擅长战术计算，但缺乏战略视野，请结合你的判断。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "depth": {"type": "integer", "description": "搜索深度 2~5（默认 3）。越深越准但越慢。"},
                "top_n": {"type": "integer", "description": "返回前 N 个候选走法（默认 3，最大 5）。"}
            }
        }
    }
}

EVALUATE_POSITION_TOOL = {
    "type": "function",
    "function": {
        "name": "evaluate_position",
        "description": (
            "静态评估当前局面，返回数值评分。正值=红优，负值=黑优。"
            "用途：①判断兑子是否划算；②残局判断能否取胜；③确认当前优劣。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []}
    }
}

TOOLS_BASIC = (MOVE_PIECE_TOOL,)
TOOLS = (SEARCH_BEST_MOVE_TOOL, EVALUATE_POSITION_TOOL, MOVE_PIECE_TOOL)
DEFAULT_TOOLS = TOOLS


# ══════════════════════════════════════════════════════════════════════════════
# 系统提示词 — 完整版（本地模型 / 弱模型，需完整棋规）
# ══════════════════════════════════════════════════════════════════════════════

def get_system_prompt() -> str:
    return """# 身份与铁律

你是中国象棋 AI 棋手。目标：**赢棋**。

提交走法的**唯一方式**是调用 `move_piece(from="列行", to="列行")`。
✅ `move_piece(from="H10", to="H8")`
❌ 文本坐标、"炮二平五"、只分析不调用工具 → 全部无效

# 工具

| 工具 | 用途 | 何时用 |
|------|------|--------|
| `move_piece` | 提交走法 | **每步必须调用**，必须是最后一轮 |
| `search_best_move` | 战术搜索 | 复杂中局/怀疑有杀棋捉双时。**不要**在开局或唯一应将时用 |
| `evaluate_position` | 局面评估 | 兑子决策/残局判断。不必每步都用 |

`search_best_move` 每步最多一次。有引擎参考时评估并说明是否采纳，**你拥有最终决定权**。

# 一、坐标

9列(A~I)×10行(1~10)。A=最左，I=最右。行1=顶(黑底线)，行10=底(红底线)。
红方=大写字母，黑方=小写字母，空位=`.`。黑方九宫:行1~3列D~F，红方九宫:行8~10列D~F。

# 二、棋子规则与估值

| 棋子 | 价值 | 走法 | 关键约束 |
|------|------|------|---------|
| 帅 K / 将 k | ∞ | 九宫内 1 格 | 禁出九宫；禁将帅同列无遮挡 |
| 仕 A / 士 a | 2 | 九宫内斜 1 格 | 禁出九宫 |
| 相 B / 象 b | 2 | 田字对角(横2竖2) | 禁过河；象眼有子不能走 |
| 馬 N / 马 n | 4 | 日字(横1竖2或横2竖1) | 蹩脚方向有子不能走 |
| 車 R / 车 r | 9 | 直线任意格，不可越子 | — |
| 炮 C / 砲 c | 4.5 | 移动=直线任意格；吃子=隔1子 | 移不行越子，吃必须隔子 |
| 兵 P / 卒 p | 1→3 | 未过河=只前进；过河=前左右各1格 | 永不后退；过河=红兵行≤5,黑卒行≥6 |

**兑子原则**：低价值换高价值。马(4)换车(9)血赚，炮(4.5)换马(4)微优。**决不**用车换马/炮（除非有杀棋）。残局：炮贬值(缺架)，马升值(少蹩脚)，过河兵大幅升值。
**长将/长捉**：不要反复将军同一位置或反复捉同一个子。中国象棋禁止长将和长捉，会被判负。

# 三、致命错误（系统自动拒绝）

1. **送将** — 走子后己方被将军
2. **将帅对面** — 双方将/帅同列无子遮挡
3. **规则违规** — 出九宫/象过河/兵后退/车越子/炮吃无架
4. **走闲棋** — 来回移动同一子、仕相在九宫内无意义挪动。**每步必须有明确目的**

# 四、阶段策略

| | 开局(≤10) | 中局(11~25) | 残局(>25) |
|---|---|---|---|
| 目标 | 快速出子控中心 | 制造复合威胁夺子 | 制造杀棋 |
| 手段 | 车占肋道(D/F列)，马跳活位，炮架中路 | 捉双/抽将/闪击/牵制/卧槽马 | 车控要线，将帅助攻，兵卒逼宫 |
| 禁忌 | 重复动子、车未出先走边兵 | 贪吃弃子失先手 | 炮缺架优先用车马 |

# 五、思考流程

**1. 安全** — 我被将军？→ 只考虑应将（躲将/垫子/吃子）。对方上一步意图？我的将/帅暴露吗？
**2. 机会** — 能将军/吃大子/捉双/抽将/闪击吗？候选→对方最强应对→我能否应对？杀棋路线：卧槽马、铁门栓、重炮、双车错。**优势时简化局面（兑子），劣势时制造复杂（保留变化）。**
**3. 选择** — 从合法走法中选最优。这步有什么价值？（吃子/将军/改善位置/阻止威胁/为后续铺路）如果仅仅是"移动了一个子"，换一个。**如果多个走法价值相当，优先出子（移动尚未活跃的大子）。**确认不送将、不违规 → `move_piece`

# 六、示例

**中炮开局**：红先。中炮 H8→E8 直接威胁黑中卒，控制中心。
**屏风马应对**：红 H8→E8 后，黑马 B1→C3 保中卒同时出动子力。

现在分析局面，从合法走法列表中选择最优走法，调用 `move_piece`。"""


# ══════════════════════════════════════════════════════════════════════════════
# 系统提示词 — 精简版（DeepSeek 等强模型，已掌握棋规）
# ══════════════════════════════════════════════════════════════════════════════

def get_system_prompt_lite() -> str:
    return """# 身份与铁律

你是中国象棋 AI 棋手。目标：**赢棋**。

提交走法的**唯一方式**是调用 `move_piece(from="列行", to="列行")`。
✅ `move_piece(from="H10", to="H8")`
❌ 文本坐标、"炮二平五"、只分析不调用工具 → 全部无效

# 工具

| 工具 | 用途 | 何时用 |
|------|------|--------|
| `move_piece` | 提交走法 | **每步必须调用**，必须是最后一轮 |
| `search_best_move` | 战术搜索 | 复杂中局/怀疑有杀棋捉双时。**不要**在开局或唯一应将时用 |
| `evaluate_position` | 局面评估 | 兑子决策/残局判断。不必每步都用 |

`search_best_move` 每步最多一次。有引擎参考时评估并说明是否采纳，**你拥有最终决定权**。

# 一、坐标

9列(A~I)×10行(1~10)。A1=黑底线(左上)，I10=红底线(右下)。红大写，黑小写，空位=`.`。黑九宫:行1~3列D~F，红九宫:行8~10列D~F。

# 二、致命错误（系统自动拒绝）

送将、将帅对面、规则违规（出九宫/象过河/兵后退/车越子/炮吃无架）。
**走闲棋**：来回移动同一子、仕相在九宫无意义挪动——每步必须有明确目的。

# 三、估值与兑子

| 车 9 | 炮 4.5 | 马 4 | 相/象 2 | 仕/士 2 | 过河兵 2~3 | 未过河兵 1 |

低价值换高价值（马换车、炮换车）。不轻易用车换马/炮。残局炮贬值(缺架)、马升值(少蹩脚)、过河兵大幅升值。

# 四、阶段策略

**开局(≤10)**：快速出子，车占肋道(D/F列)，马跳活位，炮架中路。忌重复动子。
**中局(11~25)**：捉双/抽将/闪击/牵制/卧槽马。同时注意己方安全。
**残局(>25)**：兵卒逼宫，将帅助攻。车控要线最优。

# 五、思考流程

**安全** → 被将军？应将优先（躲将/垫子/吃子）。对方上一步意图？
**机会** → 能将军/吃大子/捉双/抽将？杀棋路线（卧槽马/铁门栓/重炮/双车错）。优势简化，劣势复杂化。
**选择** → 从合法走法中选最优，确认有明确价值，不走闲棋。多步价值相当时优先出子。不送将不违规 → `move_piece`。

现在分析局面，从合法走法列表中选择最优走法，调用 `move_piece`。"""


# ══════════════════════════════════════════════════════════════════════════════
# 仲裁系统提示词
# ══════════════════════════════════════════════════════════════════════════════

def get_arbitration_system_prompt() -> str:
    return """# 身份与任务

你是中国象棋仲裁裁判。LLM 与引擎对最佳走法产生分歧，由你裁决。
你将看到棋盘、合法走法、两个候选走法（LLM vs 引擎）。**必须二选一**，调用 `move_piece` 提交。

# 评判标准（按权重排序）

**1. 杀棋/致命威胁**（最高权重）
能直接导致将杀、或逼对方付出重大子力代价来防守？能制造不可阻挡的杀棋威胁？

**2. 子力得失**（用估值计算）
能吃子？（车9>炮4.5>马4>相/仕2>兵1~3）能捉双/抽将净赚？是否送子？

**3. 局面安全**
走子后己方将/帅是否暴露？是否给对手将军或捉双的机会？存在明显战术漏洞则**直接否决**该候选。

**4. 子力活跃度**
车占要道？马跳好位？炮有炮架？是否改善了关键子力的位置？

**5. 形势判断**
优势方简化局面（兑子），劣势方制造复杂（保留变化）。

# 平局规则

难分高下 → 选**更安全**的（不给对手留战术机会）。都安全 → 选**改善子力位置**的。

# 候选来源差异

引擎(B)精于战术：捉双/抽将/杀棋，若推得有立即得子，权重高。
LLM(A)拥有战略视野：局面判断/长远规划，若改善结构(出子/占位/防守)即使无立即得子也可能更优。
**冲突时**：战术得子 > 战略改善，除非战略改善带来不可阻挡的攻势。

# 约束

- 确认两个候选均在合法走法列表中
- 对比优劣 ≤200 字，说明为何选 A 不选 B
- 调用 `move_piece` 提交；坐标格式：列 A~I，行 1~10
- 不调用工具 = 裁决无效"""


# ══════════════════════════════════════════════════════════════════════════════
# 用户提示词 — 走子
# ══════════════════════════════════════════════════════════════════════════════

def build_move_prompt(current_player: int, board_str: str, history: str,
                      in_check: bool = False, opponent_in_check: bool = False,
                      move_count: int = 0, last_move_str: str = '',
                      legal_move_count: int = 0, legal_moves_str: str = '',
                      last_move_error: str = '', retry_count: int = 0,
                      vision_mode: bool = False, mcts_suggestions: str = '') -> str:
    player_display = '红方' if current_player == 1 else '黑方'
    player_color = '大写字母' if current_player == 1 else '小写字母'

    if move_count <= 10:    phase = '开局'
    elif move_count <= 25:  phase = '中局'
    else:                   phase = '残局'

    parts = []

    # ═══ 1. 棋盘 ═══
    if vision_mode:
        parts.append("## 双通道决策")
        parts.append("")
        parts.append("### 📷 图像通道 — 战略感知")
        parts.append("从棋盘截图中获取：阵型结构、子力分布、开放线路、将/帅防护、兵卒推进、潜在威胁。")
        parts.append("棋子识别：红=帥(K)仕(A)相(B)馬(N)車(R)炮(C)兵(P) | 黑=將(k)士(a)象(b)馬(n)車(r)砲(c)卒(p)")
        parts.append("方向：上=黑方(行1~5)，下=红方(行6~10)，中=楚河汉界。列A~I，行1~10。")
        parts.append("")
        parts.append("### 📝 文本通道 — 战术执行")
        parts.append("下方合法走法列表是坐标的**权威来源**。图像感知的战略意图，在列表中确认对应坐标后执行。")
        parts.append("如图像与列表不一致，**以列表为准**。")
        parts.append("")
        parts.append("### 🔗 协同")
        parts.append("图像形成战略意图 → 列表定位具体坐标 → 交叉验证（走子后是否安全？）→ `move_piece`")
    else:
        parts.append(f"## 当前棋盘 — {player_display}({player_color}) [{phase}] 第{move_count}回合")
        parts.append("```")
        parts.append(board_str.strip())
        parts.append("```")
    parts.append("")

    # ═══ 2. 状态 ═══
    status_items = []
    if in_check:
        status_items.append("⚠️ 你正在被将军！必须应将——只能走解除将军的着法")
    if opponent_in_check and not in_check:
        status_items.append("✅ 你正在将军对方")
    if last_move_str:
        status_items.append(f"对手上一步：{last_move_str}  ← 分析对手意图：出子？进攻？设陷阱？")
    for item in status_items:
        parts.append(item)
    if status_items:
        parts.append("")

    # ═══ 3. 引擎参考 ═══
    if mcts_suggestions:
        parts.append("---")
        parts.append(mcts_suggestions)
        parts.append("")

    # ═══ 4. 合法走法 ═══
    if legal_moves_str:
        parts.append("---")
        parts.append("## 你的合法走法（必须从中选择）")
        parts.append(legal_moves_str)
        parts.append("")

    # ═══ 5. 走子历史 ═══
    if history and history != "暂无移动":
        parts.append("---")
        parts.append("## 走子历史")
        parts.append(history)
        parts.append("")

    # ═══ 6. 操作 ═══
    parts.append("## 操作")
    if in_check:
        parts.append("你被将军，请从合法走法中选择一个应将着法。")
    elif mcts_suggestions:
        parts.append("参考引擎推荐，说明是否采纳（≤300字），然后调用 move_piece。")
    else:
        parts.append("按思考流程分析，从合法走法中选择最优着法，调用 move_piece。")
    parts.append("move_piece(from=\"Xy\", to=\"Xy\")  列 A~I, 行 1~10")
    parts.append("")

    # ═══ 7. 错误反馈 ═══
    if last_move_error:
        parts.append("---")
        parts.append(f"## ❌ 上一轮走子被拒：{last_move_error}")
        parts.append("请检查：走法是否在合法列表中？起始位置是否有你的棋子？是否符合该子移动规则？是否送将/将帅对面？")
        if retry_count > 0:
            parts.append(f"第 **{retry_count}** 次重试。请选与上次**不同**的走法，优先选吃子或将军着法。")
        parts.append("")

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 合法走法格式化
# ══════════════════════════════════════════════════════════════════════════════

def format_legal_moves(legal_moves: list, board: list) -> str:
    if not legal_moves:
        return "（无合法走法 — 困毙）"

    groups: dict = {}
    for fr, fc, tr, tc in legal_moves:
        piece = board[fr][fc]
        piece_name = PIECE_SYMBOLS.get(piece, piece)
        key = f"{piece_name} ({piece.upper()})"
        if key not in groups:
            groups[key] = []
        groups[key].append(f"{format_coord(fr, fc)}→{format_coord(tr, tc)}")

    piece_order = ['R', 'N', 'C', 'P', 'B', 'A', 'K']
    ordered_keys = sorted(
        groups.keys(),
        key=lambda k: next((i for i, p in enumerate(piece_order) if f"({p})" in k), 99))

    lines = [f"共 {len(legal_moves)} 种合法走法：", ""]
    for key in ordered_keys:
        moves = groups[key]
        move_strs = []
        for i in range(0, len(moves), 8):
            move_strs.append("  ".join(moves[i:i + 8]))
        lines.append(f"**{key}** ({len(moves)}种):")
        for ms in move_strs:
            lines.append(f"  {ms}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 仲裁用户提示词
# ══════════════════════════════════════════════════════════════════════════════

def build_arbitration_prompt(
    player: int, board_str: str, history: str, legal_moves_str: str,
    llm_move_str: str, llm_reasoning: str, engine_move_str: str, engine_name: str,
    in_check: bool = False, opponent_in_check: bool = False, move_count: int = 0,
) -> str:
    player_display = '红方' if player == 1 else '黑方'
    player_color = '大写字母' if player == 1 else '小写字母'

    if move_count <= 10:    phase = '开局'
    elif move_count <= 25:  phase = '中局'
    else:                   phase = '残局'

    llm_summary = llm_reasoning
    if len(llm_summary) > 800:
        llm_summary = llm_summary[:500] + "\n…(省略)…\n" + llm_summary[-300:]

    parts = []

    parts.append(f"## 当前局面：{player_display}({player_color}) · 第{move_count}回合 · {phase}")
    parts.append("```")
    parts.append(board_str.strip())
    parts.append("```")
    parts.append("")

    if in_check:
        parts.append("⚠️ 当前方正在被将军！应将优先。")
    if opponent_in_check:
        parts.append("✅ 当前方正在将军对方。")
    if in_check or opponent_in_check:
        parts.append("")

    parts.append(legal_moves_str)
    parts.append("")

    parts.append("---")
    parts.append("## 分歧（必须二选一）")
    parts.append("")
    parts.append(f"### 候选 A — LLM：**{llm_move_str}**")
    parts.append("```")
    parts.append(llm_summary if llm_summary.strip() else "（LLM 未提供分析）")
    parts.append("```")
    parts.append("")
    parts.append(f"### 候选 B — {engine_name}：**{engine_move_str}**")
    parts.append(f"{engine_name} 经 {MCTS_TIME_LIMIT:.0f}s 深度搜索，精于发现捉双/抽将/杀棋等战术手段。")
    parts.append("")
    parts.append("---")
    parts.append("## 裁决")
    parts.append("对比优劣（≤200字），选出客观上更优的一步，调用 `move_piece` 提交。**必须二选一。**")
    parts.append("move_piece(from=\"Xy\", to=\"Xy\")  列 A~I, 行 1~10")

    return "\n".join(parts)
