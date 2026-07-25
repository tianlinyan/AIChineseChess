"""工具定义、系统提示词、用户提示词 — 中国象棋 AI 的核心沟通层

设计原则：
1. 系统提示词 = 模型的"终身知识"——规则、策略、禁忌只写一次
2. 用户提示词 = "当前局面"——棋盘状态、合法走法、即时反馈
3. 仲裁提示词 = "中立裁决"——仅需评判标准 + 两个候选走法
4. 工具定义遵循：说明用途 / 参数值域 / 保持简洁
5. 提供合法走法列表，让 LLM 专注于"选择"而非"生成坐标"
"""

import random
import re

from domain.game import ChineseChessGame
from domain.constants import BOARD_HEIGHT, BOARD_WIDTH, PIECE_SYMBOLS, format_coord
from domain.evaluation import PIECE_VALUE

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
                "from": {"type": "string", "description": "起始坐标。列字母 A~I，行数字 1~10。如 'H10'。"},
                "to":   {"type": "string", "description": "目标坐标。列字母 A~I，行数字 1~10。如 'G8'。"}
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
            "调用本地战术搜索引擎分析局面，返回评分最高的候选走法。"
            "用途：①验证你的候选走法是否在搜索结果中；"
            "②复杂中局获取战术参考；③检查隐藏的捉双/抽将/杀棋。"
            "注意：战术搜索擅长计算，但缺乏战略视野，请结合你的判断。"
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
            "静态评估当前局面，返回数值评分（通常在 ±800 之间，±100 以内为均势）。"
            "正值=红优，负值=黑优。"
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

def get_system_prompt(*, include_analysis_tools: bool = True) -> str:
    prompt = """# 身份与铁律

你是中国象棋 AI 棋手。首要目标：**不输**——被将死或困毙（无合法走法）均判负。在此基础上：**赢棋**。

提交走法的**唯一方式**是调用 `move_piece(from="列行", to="列行")`。
✅ `move_piece(from="H10", to="G8")`
❌ 文本坐标、"炮二平五"、只分析不调用工具 → 全部无效

# 工具

| 工具 | 用途 | 何时用 |
|------|------|--------|
| `move_piece` | 提交走法 | **每步必须调用**，必须是最后一轮 |"""

    if include_analysis_tools:
        prompt += """
| `search_best_move` | 战术搜索 | 复杂中局/怀疑有杀棋捉双时。**不要**在开局或唯一应将时用 |
| `evaluate_position` | 局面评估 | 兑子决策/残局判断。不必每步都用 |

`search_best_move` 每步最多一次。**你拥有最终决定权**。

**推荐顺序**：①`evaluate_position` 快速评估 → ②如需验证 `search_best_move` → ③`move_piece` 提交。总轮次 ≤4，尽早提交。"""
    else:
        prompt += """

直接调用 `move_piece` 提交走法，无需使用其他工具。"""

    prompt += """

# 一、坐标

9列(A~I)×10行(1~10)。A1=黑底线(左上)，I10=红底线(右下)。行1=顶，行10=底。
红方=大写字母，黑方=小写字母，空位=`.`。黑方九宫:行1~3列D~F，红方九宫:行8~10列D~F。
初始锚点：红底线(行10)从左到右=車馬相仕帥仕相馬車，炮在B8/H8，兵在A7/C7/E7/G7/I7；黑方镜像(行1底线，炮B3/H3，卒行4)。

# 二、棋子规则与估值

| 棋子 | 价值 | 走法 | 关键约束 |
|------|------|------|---------|
| 帥 K / 将 k | ∞ | 九宫内 1 格 | 禁出九宫；禁将帅同列无遮挡 |
| 仕 A / 士 a | 2 | 九宫内斜 1 格 | 禁出九宫 |
| 相 B / 象 b | 2 | 田字对角(横2竖2) | 禁过河；象眼有子不能走 |
| 馬 N / 馬 n | 4 | 日字(横1竖2或横2竖1) | 蹩脚方向有子不能走 |
| 車 R / 車 r | 9 | 直线任意格，不可越子 | — |
| 炮 C / 炮 c | 4.5 | 移动=直线任意格；吃子=隔1子 | 移不越子，吃须隔子 |
| 兵 P / 卒 p | 1→3 | 未过河=只前进；过河=前左右各1格 | 永不后退。红兵朝行1方向、黑卒朝行10方向；过河=红兵行≤5、黑卒行≥6 |

**子力要诀**：
車 — 全场最强控线，双車错基本无解。善于运用車的控线威力，不可轻兑。
炮 — 依赖炮架。开局利器(4.5)，残局缺架贬值(≈3.8)。
馬 — 独立作战。开局受限(4)，残局开阔升值(≈4.2)。残局馬炮价值倒挂，勿以馬兑炮。配車=卧槽馬。
过河卒 — 身价翻倍如大子，残局逼宫定胜负。
相/士 — 最后防线。残局缺→将暴露→极易被杀。勿兑尽。

**兑子**：低价值换高价值。**决不**車换馬/炮（除非杀棋）。优势简化，劣势保留变化。

# 三、致命错误（系统自动拒绝）

1. **送将** — 走子后己方被将军
2. **将帅对面** — 双方将同列且中间无子
3. **规则违规** — 出九宫/相过河/卒后退/車越子/炮吃无架
4. **长将** — 反复将军同一位置。循环中一方步步将军 → 该方直接判负

> **走子原则**：每步应有明确目的，避免纯粹等待性的来回挪动。防守必需的调整（仕相、将的移动）完全合理。

# 四、阶段策略

**开局(~10步)**：快速出子控中心 — 車占肋道(D/F列)，馬跳活位，炮架中路。忌重复动子。
**中局(~11-25步)**：制造复合威胁 — 捉双/抽将/闪击/牵制/卧槽馬。忌贪吃失先。
**残局(~25步+或各≤3大子)**：制造杀棋 — 車控要线，将可助攻，卒逼宫。炮缺架优先用车马。

# 五、思考流程

**1. 安全** — 我被将军？→ 只考虑应将（躲将/垫子/吃子）。对方上一步意图？我的将暴露吗？
**2. 机会** — 能将军/吃大子/捉双/抽将/闪击？候选→对方最强应对→我能否应对？杀棋路线：卧槽馬、铁门栓、重炮、双車错。优势简化，劣势复杂化。
**3. 选择** — 从合法走法中选最优。这步有什么价值？（吃子/将军/改善位置/阻止威胁/为后续铺路）如果仅仅是"移动了一个子"，换一个。多步价值相当时优先出子，突出車的作用。
**4. 对比引擎**（如有）— 提示词中的"引擎参考走法"来自独立强引擎（Pikafish/MCTS），强度高于 search_best_move 本地搜索。独立分析后对比：一致→采纳；不一致→有具体战术理由（长线弃子/残局过渡/阵型缺陷）则坚持己见，否则信赖引擎。
确认不送将、不违规 → `move_piece`

# 六、示例

**兑子决策**（中局）：車(9)兑馬+炮(8.5) → 账面-0.5，但残局有过河卒优势 → 可兑。車单换炮(4.5)或馬(4) → 巨亏，除非直接杀棋。
**残局简化**（残局）：車+卒 vs 馬+炮。車控线远强于馬炮 → 寻找兑子简化，車对单子必胜。

现在分析局面，从合法走法列表中选择最优走法，调用 `move_piece`。"""

    return prompt


# ══════════════════════════════════════════════════════════════════════════════
# 系统提示词 — 精简版（DeepSeek 等强模型，已掌握棋规）
# ══════════════════════════════════════════════════════════════════════════════

def get_system_prompt_lite(*, include_analysis_tools: bool = True) -> str:
    prompt = """# 身份与铁律

你是中国象棋 AI 棋手。首要目标：**不输**——被将死或困毙（无合法走法）均判负。在此基础上：**赢棋**。

提交走法的**唯一方式**是调用 `move_piece(from="列行", to="列行")`。
✅ `move_piece(from="H10", to="G8")`
❌ 文本坐标、"炮二平五"、只分析不调用工具 → 全部无效

# 工具

| 工具 | 用途 | 何时用 |
|------|------|--------|
| `move_piece` | 提交走法 | **每步必须调用**，必须是最后一轮 |"""

    if include_analysis_tools:
        prompt += """
| `search_best_move` | 战术搜索 | 复杂中局/怀疑有杀棋捉双时。**不要**在开局或唯一应将时用 |
| `evaluate_position` | 局面评估 | 兑子决策/残局判断。不必每步都用 |

`search_best_move` 每步最多一次。**你拥有最终决定权**。引擎是参考，有具体战术理由可坚持己见。

**推荐顺序**：①`evaluate_position` → ②如需 `search_best_move` → ③`move_piece`。总轮次 ≤4。"""
    else:
        prompt += """

直接调用 `move_piece` 提交走法，无需使用其他工具。"""

    prompt += """

# 一、坐标

9列(A~I)×10行(1~10)。A1=黑底线(左上)，I10=红底线(右下)。红大写，黑小写，空位=`.`。黑九宫:行1~3列D~F，红九宫:行8~10列D~F。

# 二、致命错误（系统自动拒绝）

送将、将帅对面、规则违规（出九宫/相过河/卒后退/車越子/炮吃无架）、长将（反复将军同一位置 → 判负）。

> **走子原则**：每步应有明确目的，避免纯粹等待性的来回挪动。防守必需的调整完全合理。

# 三、估值与兑子

**估值**：車9 · 炮4.5 · 馬4 · 相/象2 · 仕/士2 · 兵/卒1(未过河)~3(过河)

低价值换高价值，不轻易車换馬/炮。善用車的控线威力。炮残局贬值(缺架)，馬/过河卒残局升值。相/士=最后防线勿兑尽。

# 四、阶段策略

**开局(~10步)**：快速出子，車占肋道(D/F列)，馬跳活位，炮架中路。忌重复动子。
**中局(~11-25步)**：捉双/抽将/闪击/牵制/卧槽馬。注意己方安全。
**残局(~25步+或各≤3大子)**：卒逼宫，将可助攻。車控要线最优。

# 五、思考流程

**安全** → 被将军？应将优先。对方上一步意图？
**机会** → 能将军/吃大子/捉双/抽将？杀棋路线：卧槽馬/铁门栓/重炮/双車错。优势简化，劣势复杂化。
**选择** → 从合法走法中选最优，确认有明确价值。多步等值时优先出子，突出車的作用。
**对比引擎**（如有）→ 引擎参考来自独立强引擎（强于 search_best_move 本地搜索）。一致采纳，有具体理由可坚持己见。不送将不违规 → `move_piece`。

现在分析局面，从合法走法列表中选择最优走法，调用 `move_piece`。"""

    return prompt


# ══════════════════════════════════════════════════════════════════════════════
# 仲裁系统提示词
# ══════════════════════════════════════════════════════════════════════════════

def get_arbitration_system_prompt() -> str:
    return """# 身份与任务

你是中国象棋仲裁裁判。两个 AI 对最佳走法产生分歧，由你裁决。
你将看到棋盘、走子历史、带战术标注的合法走法、两个候选走法（A/B，来源已隐去）。**必须二选一**，调用 `move_piece` 提交。
仅从走法本身的优劣评判，不要揣测候选来源。

# 裁决流程：先排除，后排序

**第 1 步 · 安全门槛（一票否决）**
候选走子后存在以下任一漏洞 → 直接排除：
- 己方将/帥暴露，给对方将军机会
- 白送子力：吃子后被对方吃回，净亏损
- 给对方制造捉双/抽将/闪击机会
两候选都有漏洞 → 选损失更小的（送兵 < 送馬 < 送車）。

**第 2 步 · 收益排序（仅评估存活候选）**
1. **杀棋/致命威胁** — 能直接将杀，或逼对方以重大代价防守 → 直接胜出
2. **子力净收益** — 按对方最强应对计算净赚（車9 > 炮4.5 > 馬4 > 相/象/仕/士2 > 兵/卒1~3）
3. **子力位置** — 車占要道、馬跳活位、炮有炮架；残局看將/卒活跃度
4. **形势匹配** — 优势方宜兑子简化，劣势方宜保留变化
全维度持平 → 选子力位置改善更明显的。

# 约束

- 对比优劣 ≤200 字，说明为何选 A 不选 B
- 确认所选在合法走法列表中（标注：×子=吃子，+=将军）
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
                      vision_mode: bool = False, engine_hint: str = '',
                      material_str: str = '') -> str:
    player_display = '红方' if current_player == 1 else '黑方'
    player_color = '大写字母' if current_player == 1 else '小写字母'

    if move_count <= 10:    phase = '开局'
    elif move_count <= 25:  phase = '中局'
    else:                   phase = '残局'

    parts = []

    # ═══ 1. 棋盘 ═══
    if vision_mode:
        parts.append(f"## 当前棋盘 — {player_display}({player_color}) [{phase}] 第{move_count}回合")
        parts.append("（附棋盘截图）")
        parts.append("")
        parts.append("图像用于感知阵型、子力分布和线路开放情况。")
        parts.append("下方合法走法列表是坐标的**唯一权威来源**，图像与列表不一致时以列表为准。")
        parts.append("从图像获取战略意图 → 在列表中确认对应坐标 → 验证不走入送将/违规 → `move_piece`")
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
        status_items.append("✅ 你正在将军对方 → 检查是否有连杀路线，或借此机会改善子力位置")
    if last_move_str:
        status_items.append(f"对手上一步：{last_move_str}  ← 分析对手意图：出子？进攻？设陷阱？")
    if material_str:
        status_items.append(material_str)
    for item in status_items:
        parts.append(item)
    if status_items:
        parts.append("")

    # ═══ 3. 合法走法 ═══
    if legal_moves_str:
        parts.append("---")
        parts.append("## 你的合法走法（必须从中选择）")
        parts.append(legal_moves_str)
        parts.append("")

    # ═══ 4. 搜索引擎参考（放在合法走法之后，减少锚定效应）═══
    if engine_hint:
        parts.append("---")
        parts.append(engine_hint)
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
    elif engine_hint:
        parts.append("对比你的分析与搜索引擎推荐，采纳或坚持己见，简述理由后调用 move_piece。")
    else:
        parts.append("按思考流程分析，从合法走法中选择最优着法，调用 move_piece。")
    parts.append("move_piece(from=\"H10\", to=\"G8\")  ← 格式示例：列 A~I, 行 1~10")
    parts.append("")

    # ═══ 7. 错误反馈 ═══
    if last_move_error:
        parts.append("---")
        parts.append(f"## ❌ 上一轮走子被拒：{last_move_error}")
        if "送将" in last_move_error:
            parts.append("你走子后己方将被将军。检查是否移开了将的防线，或走子导致对方車/炮/馬直接攻击己方将。")
        elif "将帅对面" in last_move_error:
            parts.append("你走子后双方将同列无遮挡。检查是否移开了中间的子力，导致两将直接照面。")
        elif "规则违规" in last_move_error or "违规" in last_move_error:
            parts.append("走子违反棋子移动规则。检查：相/象是否过河？馬是否蹩脚？炮吃子是否隔了一子？兵/卒是否后退？将/仕是否出九宫？")
        else:
            parts.append("请检查：走法是否在合法列表中？起始位置是否有你的棋子？是否符合该子移动规则？")
        if retry_count > 0:
            parts.append(f"第 **{retry_count}** 次重试。请选与上次**不同**的走法，优先选吃子或将军着法。")
        parts.append("")

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 合法走法格式化
# ══════════════════════════════════════════════════════════════════════════════

def _find_checking_moves(board: list, legal_moves: list, player: int) -> set:
    """在棋盘副本上逐一模拟各走法，返回能将军对方的走法集合。

    性能：~40 走法 × O(90) 将军检测，毫秒级；仅在构建提示词时调用。
    副本的 _king_pos 缓存初始为开局位置，失效时 _is_in_check 自动全盘
    扫描修复；模拟只动己方棋子，对方将位置缓存修复一次后即稳定有效。
    """
    checking = set()
    if player not in (1, 2):
        return checking
    tmp = ChineseChessGame()
    tmp.board = [row[:] for row in board]
    b = tmp.board
    opponent = 2 if player == 1 else 1
    for fr, fc, tr, tc in legal_moves:
        piece = b[fr][fc]
        target = b[tr][tc]
        b[tr][tc] = piece
        b[fr][fc] = '.'
        if tmp.is_in_check(opponent):
            checking.add((fr, fc, tr, tc))
        b[fr][fc] = piece
        b[tr][tc] = target
    return checking


def format_legal_moves(legal_moves: list, board: list, player: int = 0) -> str:
    """格式化合法走法列表，附战术标注：×子=吃子，+=将军。

    每组内按战术价值排序：将军 > 吃子(按价值降序) > 其他，
    让 LLM 一眼看到候选战术手段，而非在 40+ 坐标中盲目搜索。
    player 传入 1/2 时启用将军标注（需在副本上模拟）。
    """
    if not legal_moves:
        return "（无合法走法 — 困毙）"

    checking = _find_checking_moves(board, legal_moves, player)

    groups: dict = {}
    for fr, fc, tr, tc in legal_moves:
        piece = board[fr][fc]
        piece_name = PIECE_SYMBOLS.get(piece, piece)
        key = f"{piece_name} ({piece.upper()})"
        captured = board[tr][tc]
        move_str = f"{format_coord(fr, fc)}→{format_coord(tr, tc)}"
        if captured != '.':
            move_str += f"×{PIECE_SYMBOLS.get(captured, captured)}"
        if (fr, fc, tr, tc) in checking:
            move_str += "+"
        groups.setdefault(key, []).append((move_str, captured,
                                           (fr, fc, tr, tc) in checking))

    def _sort_key(item) -> tuple:
        _, captured, is_check = item
        cap_val = PIECE_VALUE.get(captured.upper(), 0) if captured != '.' else 0
        return (0 if is_check else 1, 0 if captured != '.' else 1, -cap_val)

    piece_order = ['R', 'N', 'C', 'P', 'B', 'A', 'K']
    ordered_keys = sorted(
        groups.keys(),
        key=lambda k: next((i for i, p in enumerate(piece_order) if f"({p})" in k), 99))

    lines = [f"共 {len(legal_moves)} 种合法走法"
             f"（×子=吃子，+=将军；组内按 将军>吃子>其他 排序）："]
    for key in ordered_keys:
        moves = sorted(groups[key], key=_sort_key)
        move_strs = [m[0] for m in moves]
        # 紧凑排列：每行最多 8 个走法
        wrapped = []
        for i in range(0, len(move_strs), 8):
            wrapped.append("  ".join(move_strs[i:i + 8]))
        lines.append(f"**{key}** ({len(moves)}):")
        for ws in wrapped:
            lines.append(f"  {ws}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# 仲裁用户提示词
# ══════════════════════════════════════════════════════════════════════════════

def build_arbitration_prompt(
    player: int, board_str: str, history: str, legal_moves_str: str,
    llm_move_str: str, llm_reasoning: str, engine_move_str: str, engine_name: str,
    in_check: bool = False, opponent_in_check: bool = False, move_count: int = 0,
    engine_basis: str = '',
) -> str:
    player_display = '红方' if player == 1 else '黑方'
    player_color = '大写字母' if player == 1 else '小写字母'

    if move_count <= 10:    phase = '开局'
    elif move_count <= 25:  phase = '中局'
    else:                   phase = '残局'

    llm_summary = llm_reasoning
    if len(llm_summary) > 800:
        # 保留含关键推理词的句子（因为/所以/最佳/选择/威胁/优于/弃子/杀棋）
        sentences = re.split(r'(?<=[。！？\n])', llm_reasoning)
        key_words = ['因为', '所以', '最佳', '选择', '威胁', '优于', '弃子', '杀棋',
                     '捉双', '抽将', '兑子', '优势', '劣势', '简化', '暴露']
        if len(sentences) <= 6:
            # 句数太少时 head/tail 切片会重叠 → 按原序截断，避免重复句子
            combined = sentences
        else:
            head = sentences[:3]  # 前 3 句（通常是局面判断）
            tail = sentences[-3:]  # 后 3 句（通常是最终选择理由）
            # 中间挑含关键词的句子
            mid = [s for s in sentences[3:-3]
                   if any(kw in s for kw in key_words)]
            combined = head + mid + tail
        # 如果挑出的句子总长超限，按顺序截
        llm_summary = ''
        for s in combined:
            if len(llm_summary) + len(s) > 780:
                llm_summary += "\n…(省略)…\n"
                break
            llm_summary += s
        # 摘要过短（如首句即超限）→ 回退头尾截取，保证仲裁能看到实质理由
        if len(llm_summary.strip()) < 50:
            llm_summary = llm_reasoning[:500] + "\n…(省略)…\n" + llm_reasoning[-300:]

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

    # ═══ 走子历史 ═══
    if history and history != "暂无移动":
        parts.append("---")
        parts.append("## 走子历史")
        parts.append(history)
        parts.append("")

    parts.append(legal_moves_str)
    parts.append("")

    parts.append("---")
    parts.append("## 分歧（必须二选一）")
    parts.append("")
    # 候选 A/B 随机化 + 隐去来源：消除"修辞丰富的候选占优"的系统性偏差
    candidates = [
        (llm_move_str, llm_summary if llm_summary.strip() else "（未提供分析）"),
        (engine_move_str,
         engine_basis if engine_basis.strip() else f"{engine_name} 经深度搜索得出此推荐。"),
    ]
    random.shuffle(candidates)
    for label, (move_s, basis_s) in zip(('A', 'B'), candidates):
        parts.append(f"### 候选 {label}：**{move_s}**")
        parts.append("```")
        # 上场模型的原文须清洗后再嵌入：反引号可闭合围栏、
        # 向裁判注入指令（跨模型 prompt-injection 通道）
        safe_basis = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '',
                            basis_s.replace('`', "'"))
        parts.append(safe_basis)
        parts.append("```")
        parts.append("")
    parts.append("---")
    parts.append("## 裁决")
    parts.append("对比优劣（≤200字），选出客观上更优的一步，调用 `move_piece` 提交。**必须二选一。**")
    parts.append("move_piece(from=\"H10\", to=\"G8\")  ← 格式示例：列 A~I, 行 1~10")

    return "\n".join(parts)
