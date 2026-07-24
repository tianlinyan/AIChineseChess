"""中国象棋游戏常量与AI配置常量"""

# ── 棋盘尺寸 ──
BOARD_WIDTH = 9
BOARD_HEIGHT = 10

# ── AI 配置 ──
AI_RETRY_LIMIT = 3
AI_TIMEOUT_SECONDS = 300
AI_CONNECT_TIMEOUT = 15        # API 连接超时（秒）：与读取超时分离，端点黑洞快速失败
ARBITRATION_TIMEOUT_SECONDS = 180  # 仲裁超时（秒），比正常 LLM 调用更短
AI_OUTPUT_TRUNCATE_LENGTH = 1000
AI_OUTPUT_MIN_TRIM_POSITION = 500
AI_RETRY_DELAY_MS = 3000
LM_STUDIO_DEFAULT_PORT = 1234

# ── 搜索引擎配置 ──
SEARCH_MAX_DEPTH = 5             # 搜索强度 (1-6)：MCTS模拟次数 500~3000，Pikafish时限 depth×3s（封顶 MCTS_TIME_LIMIT=15s）
SEARCH_TIME_LIMIT = 20.0         # 搜索时间上限（秒）
SEARCH_QUIESCENCE_DEPTH = 4      # 静态搜索额外深度
SEARCH_BLUNDER_CHECK_DEPTH = 2   # LLM 走法验证用浅搜索深度

# ── MCTS 配置 ──
MCTS_SIMULATIONS = 2000          # 默认模拟次数（真搜索后单次模拟≈0.1~0.5ms，主要耗在叶评估）
MCTS_TIME_LIMIT = 15.0           # MCTS / Pikafish 时间上限（秒）
MCTS_EXPLORATION = 1.4           # UCB1 探索参数
MCTS_PRIOR_STRENGTH = 50         # LLM走法先验强度（虚拟访问次数乘数）
MCTS_FALLBACK_SIMULATIONS = 500  # 回退搜索模拟次数（后台线程执行，不阻塞 UI）
MCTS_FALLBACK_TIME_LIMIT = 5.0   # 回退搜索时间上限（秒）

# ── 残局库配置 ──
EGTB_MAX_PIECES = 10             # 残局库查询的最大子力数
EGTB_CLOUD_MAX_PIECES = 6        # 云库查询的最大子力数
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
VISION_IMAGE_MAX_WIDTH = 300     # 图片最大宽度 (px)，0=不限制

# ── 棋子符号映射 ──
# 红黑棋子名称：車馬炮红黑通用，帅/将、仕/士、相/象、兵/卒一一对应
PIECE_SYMBOLS = {
    'K': '帥', 'A': '仕', 'B': '相', 'N': '馬', 'R': '車', 'C': '炮', 'P': '兵',
    'k': '将', 'a': '士', 'b': '象', 'n': '馬', 'r': '車', 'c': '炮', 'p': '卒'
}


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
