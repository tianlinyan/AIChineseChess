"""AI点评功能无头自检（普通脚本，非 pytest）。

用法：python tests/test_commentary.py
覆盖：人类落子触发解说并阻塞下一步、解说完成恢复、search_only AI 落子
触发解说、hybrid 不触发、暂停打断→恢复重触发、开关关闭不触发。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PyQt6.QtCore import QCoreApplication

app = QCoreApplication(sys.argv)

from domain.game import ChineseChessGame
from domain.prompts import HUMAN_MODEL
from ai.manager import AIManager
from ai.worker import AIWorkerSignals
from app.controller import GameController


# ── duck typing UI ──
class FakeBoard:
    def update(self):
        pass


class FakeLabel:
    def __init__(self):
        self.text = ''

    def setText(self, t):
        self.text = t


class FakeButton:
    def setText(self, t):
        pass

    def setEnabled(self, e):
        pass


class FakeCheck:
    def __init__(self, checked=True):
        self._c = checked

    def isChecked(self):
        return self._c


class FakeLogManager:
    def __init__(self):
        self.logs = []

    def log(self, t, m='INFO'):
        self.logs.append((t, m))

    def clear(self):
        self.logs.clear()


class FakeModel:
    id, name, type = 'fake-p2', 'FakeAI', 'llama-server'
    model = 'fake-model'
    endpoint = 'http://127.0.0.1:9/v1/chat/completions'
    api_key, options, tools_choice = '', {}, None


class FakeMain:
    def __init__(self):
        self.board_widget = FakeBoard()
        self.think_timer_label = FakeLabel()
        self.pause_btn = FakeButton()
        self.log_manager = FakeLogManager()
        self.model_manager = type('MM', (), {'models': [FakeModel()]})()
        self.ai_commentary_check = FakeCheck(True)

    def update_game_status(self):
        pass

    def update_history_list(self):
        pass

    def update_player_status(self):
        pass

    def update_ai_score(self):
        pass


# ── 假 worker：不发网络请求，由测试手动 emit 完成信号 ──
LAUNCHED = []


class FakeWorker:
    def __init__(self, model, prompt, image, **kw):
        self.model, self.prompt, self.kw = model, prompt, kw
        self.signals = AIWorkerSignals()
        self.cancelled = False
        LAUNCHED.append(self)

    def run(self):
        pass  # 不自动完成，由测试手动 emit

    def cancel(self):
        self.cancelled = True


import app.controller as ctl
ctl.AIWorker = FakeWorker  # controller 模块内引用替换


def pump(ms=100):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()


def check(cond, msg):
    if not cond:
        print(f'FAIL: {msg}')
        sys.exit(1)
    print(f'ok: {msg}')


gc = GameController(ChineseChessGame(), AIManager())
gc.main = FakeMain()
gc.is_active = True
gc.model1 = HUMAN_MODEL
gc.model2 = HUMAN_MODEL
# 屏蔽真实提示搜索（无 Pikafish 会走 MCTS 后台线程）
gc._engine.start_hint_search = lambda *a, **k: None

# ══ 场景 A：人类落子 → 解说阻塞下一步 → 完成后可继续 ══
gc.game.reset()
gc.game.current_player = 1
mv = gc.game.get_all_legal_moves(1)[0]
gc.on_human_move(*mv)
check(len(gc.game.moves) == 1, '红方人类走子成功')
check(gc._commentary_mover == 1, '人类落子触发解说')
check(gc.ai_manager.is_busy(), '解说期间 busy 置位（人类走子守卫依据）')
check(len(LAUNCHED) == 1 and LAUNCHED[0].kw.get('tools') == (),
      '解说 worker 以 tools=() 启动')
check('本步评析' in LAUNCHED[0].prompt and '双方棋势分析' in LAUNCHED[0].prompt,
      '解说提示词含 本步评析/双方棋势分析 结构')

# 解说期间黑方（人类）被阻塞
gc.game.current_player = 2
mvb = gc.game.get_all_legal_moves(2)[0]
n_before = len(gc.game.moves)
gc.on_human_move(*mvb)
check(len(gc.game.moves) == n_before, '解说期间黑方走子被阻塞')

# 解说完成 → 解除阻塞
w = LAUNCHED[-1]
w.signals.finished.emit('', '', '', '', gc.game_version,
                        gc.ai_manager.cancel_version,
                        '局势分析：… 本步评析：… 其他走法：…')
pump()
check(gc._commentary_mover is None, '解说完成，状态清除')
check(not gc.ai_manager.is_busy(), '解说完成，busy 释放')
n_before = len(gc.game.moves)
gc.on_human_move(*mvb)
check(len(gc.game.moves) == n_before + 1, '解说完成后黑方可正常走子')

# ══ 场景 B：search_only AI 落子 → 触发解说；hybrid 不触发 ══
gc.game.reset()
gc.model1 = FakeModel()
gc.red_ai_mode = 'search_only'
gc.game.current_player = 1
mv = gc.game.get_all_legal_moves(1)[0]
res = gc.game.move_piece(*mv)
gc._complete_move(*mv, res, 1, '搜索')
check(gc._commentary_mover == 1, 'search_only AI 落子触发解说')
check('双方棋势分析' in LAUNCHED[-1].prompt, 'search_only 解说提示词正常')
w = LAUNCHED[-1]
w.signals.finished.emit('', '', '', '', gc.game_version,
                        gc.ai_manager.cancel_version, '解说...')
pump()
check(gc._commentary_mover is None, 'search_only 解说完成清除')

gc.game.reset()
gc.red_ai_mode = 'hybrid'
gc.game.current_player = 1
mv = gc.game.get_all_legal_moves(1)[0]
res = gc.game.move_piece(*mv)
gc._complete_move(*mv, res, 1, 'LLM')
check(gc._commentary_mover is None, 'hybrid 模式落子不触发解说')

# ══ 场景 C：暂停打断解说 → 恢复后重新触发 ══
gc.game.reset()
gc.model1 = HUMAN_MODEL
gc.model2 = HUMAN_MODEL
gc.game.current_player = 1
mv = gc.game.get_all_legal_moves(1)[0]
gc.on_human_move(*mv)
check(gc._commentary_mover == 1, '解说触发')
w = LAUNCHED[-1]

gc.toggle_pause()  # 暂停：取消解说 worker
check(w.cancelled, '暂停取消了在飞解说 worker')
# 模拟真实 worker 被取消后返回错误
w.signals.finished.emit('', '', '', '任务已取消', gc.game_version,
                        w.kw['cancel_version'], '')
pump()
check(gc.is_paused, '处于暂停状态')
check(gc._commentary_mover == 1, '暂停后保留待解说状态')

gc.toggle_pause()  # 恢复：重新触发解说
check(gc._commentary_mover == 1, '恢复后重新触发解说')
check(LAUNCHED[-1] is not w, '重触发使用新 worker')
w2 = LAUNCHED[-1]
w2.signals.finished.emit('', '', '', '', gc.game_version,
                         gc.ai_manager.cancel_version, '重新解说...')
pump()
check(gc._commentary_mover is None, '重解说完成后最终解除阻塞')
check(not gc.ai_manager.is_busy(), 'busy 已清除')

# ══ 场景 D：开关关闭 → 不触发、不阻塞 ══
gc.game.reset()
gc.main.ai_commentary_check = FakeCheck(False)
gc.model1 = HUMAN_MODEL
gc.model2 = HUMAN_MODEL
gc.game.current_player = 1
mv = gc.game.get_all_legal_moves(1)[0]
gc.on_human_move(*mv)
check(gc._commentary_mover is None, '开关关闭时不触发解说')

# ══ 场景 F：开局库落子跳过解说（即使 search_only 模式） ══
gc.game.reset()
gc.main.ai_commentary_check = FakeCheck(True)
gc.model1 = FakeModel()
gc.model2 = HUMAN_MODEL
gc.red_ai_mode = 'search_only'
gc.game.current_player = 1
mv = gc.game.get_all_legal_moves(1)[0]
res = gc.game.move_piece(*mv)
gc._complete_move(*mv, res, 1, '开局库')
check(gc._commentary_mover is None, '开局库落子跳过解说')

# ══ 场景 E：真实 AIWorker 纯文本循环（回归：解说不得误报"未找到有效走法"） ══
# 解说 worker 以 tools=() 启动、不传 game；模型返回的解说文本（含坐标引用）
# 必须原样作为结果返回，而不是进入走法解析/校验流程。
from ai.worker import AIWorker as RealWorker

COMMENTARY_TEXT = ('本步评析：炮二平五（C2→E2）抢占中路，佳着。\n'
                   '双方棋势分析：红方多子占先，中炮威力尽显。\n'
                   '其他招式：马八进六（H9→G7）或兵七进一（G3→G4）。')


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def real_worker(content=None, tool_call=False):
    w = RealWorker(FakeModel(), '请解说上一步', None,
                   system_prompt='你是解说员', tools=())
    message = {'content': content, 'tool_calls': []}
    if tool_call:
        message['tool_calls'] = [{
            'id': 'tc-1',
            'function': {'name': 'move_piece',
                         'arguments': '{"from": "C2", "to": "E2"}'}}]
    w._send_request = lambda s, p: FakeResp(
        {'choices': [{'message': message}]})
    return w


w = real_worker(COMMENTARY_TEXT)
fc, tc, out = w._agentic_loop(None)
check((fc, tc) == ('', ''), '纯文本解说不产生走法')
check('未找到有效走法' not in out and out.strip() == COMMENTARY_TEXT,
      '解说文本原样作为结果（回归：不再误报"未找到有效走法"）')

# 模型若仍返回 tool_call（payload 未声明工具，弱模型偶发）：不执行工具、
# 不烧轮次，直接以正式文本为结果
w = real_worker(COMMENTARY_TEXT, tool_call=True)
fc, tc, out = w._agentic_loop(None)
check((fc, tc) == ('', '') and out.strip() == COMMENTARY_TEXT,
      '偶发 tool_call 时纯文本任务直接返回文本')

# 模型完全未返回文本 → 真实错误（controller 记 WARNING 并继续对弈）
w = real_worker('')
fc, tc, out = w._agentic_loop(None)
check(out.startswith('ERROR:'), '无解说文本时报真实错误')

print('\nALL COMMENTARY SANITY CHECKS PASSED')
