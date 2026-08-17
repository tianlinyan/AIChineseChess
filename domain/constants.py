"""中国象棋游戏常量与AI配置常量"""

# ── 棋盘尺寸 ──
BOARD_WIDTH = 9
BOARD_HEIGHT = 10

# ── AI 配置 ──
AI_RETRY_LIMIT = 3
AI_TIMEOUT_SECONDS = 600
AI_CONNECT_TIMEOUT = 15        # API 连接超时（秒）：与读取超时分离，端点黑洞快速失败
ARBITRATION_TIMEOUT_SECONDS = 180  # 仲裁超时（秒），比正常 LLM 调用更短
AI_RETRY_DELAY_MS = 3000
# ── 搜索引擎配置 ──
SEARCH_MAX_DEPTH = 8             # 搜索强度 (1-8)：MCTS模拟次数 500~5000，Pikafish时限 depth×3s（封顶 MCTS_TIME_LIMIT=30s）
DEFAULT_SEARCH_DEPTH = min(5, SEARCH_MAX_DEPTH)  # UI 搜索强度默认值（controller 与 panel 共用，避免两处不一致）
SEARCH_TIME_LIMIT = 40.0         # 搜索时间上限（秒）

# ── MCTS 配置 ──
MCTS_TIME_LIMIT = 30.0           # MCTS / Pikafish 时间上限（秒）
MCTS_EXPLORATION = 1.4           # UCB1 探索参数
MCTS_FALLBACK_SIMULATIONS = 800  # 回退搜索模拟次数（后台线程执行，不阻塞 UI）
MCTS_FALLBACK_TIME_LIMIT = 10.0  # 回退搜索时间上限（秒）

# ── 残局库配置 ──
EGTB_MAX_PIECES = 10             # 残局库查询的最大子力数
ENDGAME_PIECE_THRESHOLD = 14     # 残局阶段判定阈值（≤此值切换估值策略）

# ── 开局库配置 ──
OPENING_BOOK_ENABLED = True      # 默认启用开局库
OPENING_BOOK_MAX_MOVES = 12      # 开局库最大走子数（之后退出开局库）

# ── 对局规则 ──
NATURAL_LIMIT_MOVES = 120        # 自然限着：连续未吃子步数上限（达到判和；将杀/困毙优先）

# ── AI 模式配置 ──
# "hybrid": AI + 搜索混合模式（推荐）— AI分析局面，搜索验证并选最优
# "search_only": 纯搜索模式 — 不使用AI，仅引擎搜索
# "llm_only": 仅AI模式 — 不使用搜索，仅AI走子
AI_DEFAULT_MODE = "hybrid"

# ── UI 计时 ──
THINKING_TIMER_INTERVAL = 1000
AI_DELAY_MS = 0
OPENING_DELAY_MS = 2000          # 开局库每步间隔（ms），让玩家看清走子

# ── 日志 ──
LOG_MAX_BLOCKS = 2000            # 思考日志最大块数（超出裁最旧；不限制时长对局会拖慢 QTextEdit）

# ── 提示词配置 ──
PROMPT_HISTORY_MAX_ITEMS = 24    # 提示词中走子历史的最大条数（超长截断，控制 token）

# ── 视觉模式 ──
VISION_IMAGE_QUALITY = 80        # JPEG 质量 (1-100)，越低文件越小
VISION_IMAGE_SCALE = 2           # 截图渲染放大倍数（超采样，放大后仍清晰）
VISION_IMAGE_MAX_WIDTH = 600     # 图片最大宽度 (px)，0=不限制（原 300，放大一倍）

# ── 棋子符号映射 ──
# 红黑棋子名称：車馬炮红黑通用，帅/将、仕/士、相/象、兵/卒一一对应
PIECE_SYMBOLS = {
    'K': '帥', 'A': '仕', 'B': '相', 'N': '馬', 'R': '車', 'C': '炮', 'P': '兵',
    'k': '将', 'a': '士', 'b': '象', 'n': '馬', 'r': '車', 'c': '炮', 'p': '卒'
}

# ── 传统棋谱数字（中国象棋程序竞赛规则 第二章第六节）──
# 竖线序号（路）：红方用中文数字（一~九），黑方用阿拉伯数字（1~9）；
# 竖走步数同理：红方中文、黑方阿拉伯。
CN_DIGITS = '一二三四五六七八九'
AR_DIGITS = '123456789'


def _side_file(col: int, player: int) -> str:
    """竖线序号（路）：红黑各自从【己方视角】右→左数。

    红方：棋盘最右竖线（col=8）为「一」，最左（col=0）为「九」；
    黑方：棋盘最左竖线（col=0）为「1」，最右（col=8）为「9」。
    同一条竖线，红方记「一」时黑方记「9」。
    """
    if player == 1:
        return CN_DIGITS[BOARD_WIDTH - 1 - col]
    return AR_DIGITS[col]


def _pawn_disambig(board: list, col: int, row: int, piece: str,
                   player: int, name: str, src_file: str) -> str:
    """兵（卒）同路多条时的首二字消歧（规则第三条）。

    优先级（后一条仅在前一条不适用时生效）：
    1. 某路有 4~5 个兵 → 该路用 前兵/二兵/三兵/四兵/后兵（如 后兵平五、三兵平五）；
    2. 两条路各 ≥2 个兵（同时存在两个前兵和两个后兵）→ 全部用
       前/中/后 + 路号（如 前三平四、中四平五）；
    3. 单路 3 个兵 → 前兵/中兵/后兵（如 中兵平五）；
    4. 单路 2 个兵 → 前兵/后兵。

    board 为【走子前】局面；piece 为含大小写的己方兵卒符号。
    """
    files: dict = {}
    for r in range(BOARD_HEIGHT):
        for c in range(BOARD_WIDTH):
            if board[r][c] == piece:
                files.setdefault(c, []).append(r)

    rows = sorted(files.get(col, []))
    # 前→后排序：红方「前」= 行号小（接近对手），黑方「前」= 行号大
    if player == 2:
        rows = rows[::-1]
    n = len(rows)
    if row not in rows:      # 防御：board 与走法不一致时退化为普通记法
        return f"{name}{src_file}"
    idx = rows.index(row)

    pa = name                # 兵/卒：规则"兵均包含卒"，记法随己方名称
    if n >= 4:
        labels = ([f'前{pa}', f'二{pa}', f'三{pa}', f'后{pa}'] if n == 4
                  else [f'前{pa}', f'二{pa}', f'三{pa}', f'四{pa}', f'后{pa}'])
        return labels[idx]

    two_plus = [c for c, rs in files.items() if len(rs) >= 2]
    if len(two_plus) >= 2:
        if n == 3:
            return ('前', '中', '后')[idx] + src_file
        return ('前', '后')[idx] + src_file

    if n == 3:
        return (f'前{pa}', f'中{pa}', f'后{pa}')[idx]
    if n == 2:
        return (f'前{pa}', f'后{pa}')[idx]
    return f"{name}{src_file}"


def format_chinese_notation(board: list, fr: int, fc: int, tr: int, tc: int,
                            player: int = 0) -> str:
    """传统棋谱着法（中国象棋程序竞赛规则 第二章第六节）。

    Args:
        board: 【走子前】局面（前/后、中兵等消歧需要该时刻的布子）。
        fr/fc/tr/tc: 起止行列索引。
        player: 0=按 (fr,fc) 棋子自动判定（1=红，2=黑）。

    Returns:
        四字着法，如 '炮二平五'、'馬8进7'、'前炮平五'、'后兵平五'、
        '三兵平五'、'前三平四'、'中四平五'、'車9进1' 等。

    规则要点：
    - 第二个字=所在路，红方中文（右→左 一~九），黑方阿拉伯（右→左 1~9）；
    - 第三个字=方向，从己方视角：接近对手=进，接近己方=退，横走=平；
    - 第四个字：横走或馬/士/相(象)记目标路；竖走记步数。
    """
    piece = board[fr][fc]
    if piece == '.':
        raise ValueError(f'({fr},{fc}) 无棋子，无法记录传统着法')
    if player == 0:
        player = 1 if piece.isupper() else 2
    ptype = piece.upper()
    name = PIECE_SYMBOLS.get(piece, piece)

    # ── 方向：从己方视角，接近对手=进，接近己方=退，横走=平 ──
    if tr == fr:
        direction = '平'
    elif player == 1:      # 红：前进=行号减小（朝黑方）
        direction = '进' if tr < fr else '退'
    else:                  # 黑：前进=行号增大（朝红方）
        direction = '进' if tr > fr else '退'

    src_file = _side_file(fc, player)
    dst_file = _side_file(tc, player)

    # ── 第四字：横走或馬/士/相(象) → 目标路；竖走 → 步数 ──
    if direction == '平' or ptype in ('N', 'A', 'B'):
        fourth = dst_file
    else:
        dist = abs(tr - fr)
        fourth = (CN_DIGITS if player == 1 else AR_DIGITS)[dist - 1]

    # ── 首二字消歧 ──
    if ptype == 'P':
        disambig = _pawn_disambig(board, fc, fr, piece, player, name, src_file)
    elif ptype not in ('A', 'B'):
        # 同路同类棋子（士/相除外）：前=接近对手，后=接近己方
        rows = [r for r in range(BOARD_HEIGHT) if board[r][fc] == piece]
        if len(rows) > 1:
            front = min(rows) if player == 1 else max(rows)
            disambig = ('前' if fr == front else '后') + name
        else:
            disambig = f"{name}{src_file}"
    else:
        disambig = f"{name}{src_file}"

    return f"{disambig}{direction}{fourth}"


def format_duration(seconds: int) -> str:
    """将秒数格式化为中文时间字符串。"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}小时 {minutes}分 {secs}秒"
    elif minutes > 0:
        return f"{minutes}分 {secs}秒"
    else:
        return f"{secs}秒"


def format_coord(row: int, col: int) -> str:
    """将棋盘行列索引转换为坐标字符串，如 (9, 0) → 'A10'。"""
    return f"{chr(65 + col)}{row + 1}"


def parse_coord(s: str) -> tuple:
    """将坐标字符串解析为行列索引，如 'A10' → (9, 0)。

    Raises:
        ValueError: 坐标格式无效或超出棋盘范围。
    """
    if not s or len(s) < 2:
        raise ValueError(f"坐标 '{s}' 格式无效：至少需要列字母+行数字")
    col = ord(s[0].upper()) - 65
    try:
        row = int(s[1:]) - 1
    except ValueError:
        raise ValueError(f"坐标 '{s}' 行号无法解析为数字")
    if not (0 <= col < BOARD_WIDTH and 0 <= row < BOARD_HEIGHT):
        raise ValueError(
            f"坐标 '{s}' 超出棋盘范围（列 A-I，行 1-10）")
    return row, col


def format_move(fr: int, fc: int, tr: int, tc: int) -> str:
    """将走法格式化为 'A1→B2' 形式的字符串。"""
    return f"{format_coord(fr, fc)}→{format_coord(tr, tc)}"
