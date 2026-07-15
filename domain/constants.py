"""中国象棋游戏常量与AI配置常量"""

# ── 棋盘尺寸 ──
BOARD_WIDTH = 9
BOARD_HEIGHT = 10

# ── AI 配置 ──
AI_RETRY_LIMIT = 3
AI_TIMEOUT_SECONDS = 300
AI_OUTPUT_TRUNCATE_LENGTH = 300
AI_OUTPUT_MIN_TRIM_POSITION = 150
AI_RETRY_DELAY_MS = 3000
TOKEN_ESTIMATE_DIVISOR = 4
ESTIMATED_TOKENS_PER_IMAGE = 100
LM_STUDIO_DEFAULT_PORT = 1234

# ── 搜索引擎配置 ──
SEARCH_MAX_DEPTH = 5             # Alpha-Beta 最大搜索深度 (1-6) [仅 search_only 模式使用]
SEARCH_TIME_LIMIT = 20.0         # 搜索时间上限（秒）
SEARCH_QUIESCENCE_DEPTH = 4      # 静态搜索额外深度
SEARCH_BLUNDER_CHECK_DEPTH = 2   # LLM 走法验证用浅搜索深度

# ── MCTS 配置 ──
MCTS_SIMULATIONS = 2000          # 默认模拟次数（每次~5ms，2000次≈10s）
MCTS_TIME_LIMIT = 15.0           # MCTS 时间上限（秒）
MCTS_EXPLORATION = 1.4           # UCB1 探索参数
MCTS_PRIOR_STRENGTH = 50         # LLM走法先验强度（虚拟访问次数乘数）
MCTS_LLM_OVERRIDE_THRESHOLD = 0.15  # MCTS价值超过LLM走法此阈值时，用MCTS结果

# ── 开局库配置 ──
OPENING_BOOK_ENABLED = True      # 默认启用开局库
OPENING_BOOK_MAX_MOVES = 12      # 开局库最大走子数（之后退出开局库）

# ── AI 模式配置 ──
# "hybrid": LLM + 搜索混合模式（推荐）— LLM分析局面，搜索验证并选最优
# "search_only": 纯搜索模式 — 不使用LLM，仅Alpha-Beta搜索
# "llm_only": 纯LLM模式 — 不使用搜索，仅LLM走子（原行为）
AI_DEFAULT_MODE = "hybrid"

# ── UI 计时 ──
THINKING_TIMER_INTERVAL = 1000
AI_DELAY_MS = 0
OPENING_DELAY_MS = 2000          # 开局库每步间隔（ms），让玩家看清走子

# ── 视觉模式 ──
VISION_IMAGE_QUALITY = 80        # JPEG 质量 (1-100)，越低文件越小
VISION_IMAGE_MAX_WIDTH = 300     # 图片最大宽度 (px)，0=不限制

# ── 棋子符号映射 ──
PIECE_SYMBOLS = {
    'K': '帥', 'A': '仕', 'B': '相', 'N': '馬', 'R': '車', 'C': '炮', 'P': '兵',
    'k': '將', 'a': '士', 'b': '象', 'n': '馬', 'r': '車', 'c': '炮', 'p': '卒'
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
