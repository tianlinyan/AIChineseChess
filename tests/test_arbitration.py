"""仲裁集成测试：LLM 与引擎意见不一致时仲裁是否正常。

用法：
  python tests/test_arbitration.py            # 分支级：不依赖网络，直接驱动回调
  python tests/test_arbitration.py --live     # 集成级：真实 _start_arbitration +
                                              # AIWorker + DeepSeek（需 DEEPSEEK_API_KEY；
                                              # key 缺失/网络失败时验证"仲裁失败→LLM 回退"路径）

覆盖：
  A. on_arbitration_finished 分支：
     A1 仲裁选 LLM 候选 → 计分 +1、执行 LLM 走法
     A2 仲裁选引擎候选 → 计分 +0、执行引擎走法
     A3 仲裁返回第三走法（合法但非候选）→ 采纳引擎走法
     A4 仲裁 error → 采用 LLM 走法（不计分）
     A5 仲裁坐标解析失败 → 采用 LLM 走法
  B. 集成：分歧状态 → _start_arbitration → AIWorker 线程 → 事件循环 →
     回调 → 走子/回退（live 模式）
"""

import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from domain.game import ChineseChessGame
from domain.prompts import HUMAN_MODEL
from ai.manager import AIManager
from app.controller import GameController

failures = []


def check(name, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name}' + (f' — {detail}' if detail else ''))
    if not cond:
        failures.append(name)


class _FakeWidget:
    def update(self):
        pass


class FakeMain:
    """最小 mock：提供仲裁模型查找与 controller 用到的 UI 面。"""

    class _MM:
        def __init__(self):
            from services.models import ModelManager
            self._inner = ModelManager()
            self._inner.load(os.path.join(os.path.dirname(__file__), '..',
                                          'models.json'))
            self.models = self._inner.models

    def __init__(self):
        self.model_manager = self._MM()
        self.board_widget = _FakeWidget()

    def update_ai_score(self):
        pass

    def update_game_status(self):
        pass

    def update_history_list(self):
        pass

    def update_player_status(self):
        pass

    def start_thinking_timer(self, player):
        pass


def make_controller():
    g = ChineseChessGame()
    am = AIManager()
    c = GameController(g, am)
    c.main = FakeMain()
    c.is_active = True
    c.is_paused = False
    # 双方设为人类：仲裁落子后 _schedule_next_ai_move 不会调度 AI 走子
    # （避免测试事件循环里触发 make_ai_move 依赖真实模型对象）
    c.model1 = HUMAN_MODEL
    c.model2 = HUMAN_MODEL
    return c


def coord_of(mv):
    return f"{chr(65 + mv[1])}{mv[0] + 1}", f"{chr(65 + mv[3])}{mv[2] + 1}"


def setup_disagreement(c):
    """构造分歧状态：红方两个合法候选走法（LLM vs 引擎）+ 第三个合法走法。"""
    legal = c.game.get_all_legal_moves(1)
    assert len(legal) >= 3, '初始局面至少需 3 个合法走法'
    llm_move = legal[0]
    engine_move = legal[1]
    third = legal[2]
    c._arbitration_llm_move = llm_move
    c._arbitration_engine_move = engine_move
    c._arbitration_llm_text = '我方判断：此着法可保持子力协调，为残局做准备。'
    return llm_move, engine_move, third


def reset_round(c):
    """每个分支前重置棋盘与仲裁状态（不清计分，便于累计断言）。"""
    c.game.reset()
    c._arbitration_llm_move = None
    c._arbitration_engine_move = None
    c._arbitration_llm_text = ''


def last_move_of(c):
    return c.game.moves[-1][:4] if c.game.moves else None


# ─────────────────────────────────────────────────────────────────────
# 部分 A：on_arbitration_finished 分支（不依赖网络）
# ─────────────────────────────────────────────────────────────────────
def test_branches():
    print('\n═══ 部分 A：仲裁回调分支（直接驱动） ═══')
    c = make_controller()
    v = c.game_version
    cv = c.ai_manager.cancel_version

    # A1 仲裁选 LLM 候选 → 计分 +1
    reset_round(c)
    llm_move, engine_move, _ = setup_disagreement(c)
    score_before = c.ai_score
    fc, tc = coord_of(llm_move)
    c.on_arbitration_finished(fc, tc, '仲裁分析：A 更优。', '', 0, v, cv)
    check('A1 执行了 LLM 候选', last_move_of(c) == llm_move,
          f'实际={last_move_of(c)}')
    check('A1 计分 +1', c.ai_score == score_before + 1,
          f'{score_before} -> {c.ai_score}')
    # 注：arbitration_count 只在 _start_arbitration 递增（部分 B 验证），
    # A 分支直接驱动回调不经该路径

    # A2 仲裁选引擎候选 → 计分 +0
    reset_round(c)
    llm_move, engine_move, _ = setup_disagreement(c)
    score_before = c.ai_score
    fc, tc = coord_of(engine_move)
    c.on_arbitration_finished(fc, tc, '仲裁分析：B 更优。', '', 0, v, cv)
    check('A2 执行了引擎候选', last_move_of(c) == engine_move,
          f'实际={last_move_of(c)}')
    check('A2 计分 +0', c.ai_score == score_before,
          f'{score_before} -> {c.ai_score}')

    # A3 仲裁返回第三走法（合法但非候选）→ 采纳引擎
    reset_round(c)
    llm_move, engine_move, third = setup_disagreement(c)
    score_before = c.ai_score
    fc, tc = coord_of(third)
    c.on_arbitration_finished(fc, tc, '仲裁分析：我选 C。', '', 0, v, cv)
    check('A3 采纳引擎走法', last_move_of(c) == engine_move,
          f'实际={last_move_of(c)}')
    check('A3 计分 +0（第三走法不算一致）', c.ai_score == score_before,
          f'{score_before} -> {c.ai_score}')

    # A4 仲裁 error → 采用 LLM 走法（不计分）
    reset_round(c)
    llm_move, engine_move, _ = setup_disagreement(c)
    score_before = c.ai_score
    c.on_arbitration_finished('', '', '分析', '客户端错误: xxx', 0, v, cv)
    check('A4 仲裁失败 → LLM 回退', last_move_of(c) == llm_move,
          f'实际={last_move_of(c)}')
    check('A4 失败不计分', c.ai_score == score_before,
          f'{score_before} -> {c.ai_score}')

    # A5 仲裁坐标解析失败 → 采用 LLM 走法（不计分）
    reset_round(c)
    llm_move, engine_move, _ = setup_disagreement(c)
    score_before = c.ai_score
    c.on_arbitration_finished('Z9', 'Q8', '分析', '', 0, v, cv)
    check('A5 坐标解析失败 → LLM 回退', last_move_of(c) == llm_move,
          f'实际={last_move_of(c)}')
    check('A5 失败不计分', c.ai_score == score_before,
          f'{score_before} -> {c.ai_score}')

    # A6 陈旧回调（版本不匹配）→ 丢弃，不落子
    reset_round(c)
    llm_move, engine_move, _ = setup_disagreement(c)
    moves_before = len(c.game.moves)
    fc, tc = coord_of(llm_move)
    c.on_arbitration_finished(fc, tc, '分析', '', 0, v + 99, cv)
    check('A6 陈旧回调被丢弃', len(c.game.moves) == moves_before,
          f'{moves_before} -> {len(c.game.moves)}')


# ─────────────────────────────────────────────────────────────────────
# 部分 B：集成（真实 _start_arbitration + AIWorker + DeepSeek）
# ─────────────────────────────────────────────────────────────────────
def test_live():
    print('\n═══ 部分 B：集成（真实仲裁链路） ═══')
    # 与 main.py 一致：存在 .env 时加载（否则用进程环境变量）
    env_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), '.env')
    if os.path.exists(env_path):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            pass
    app = QApplication.instance() or QApplication([])
    c = make_controller()
    reset_round(c)
    llm_move, engine_move, _ = setup_disagreement(c)
    moves_before = len(c.game.moves)
    arb_count_before = c.arbitration_count

    # 找出仲裁模型是否可配置
    arb = next((m for m in c.main.model_manager.models
                if m.id == 'arbitration'), None)
    if arb is None:
        check('仲裁模型已配置', False, 'models.json 缺 arbitration 条目')
        return
    check('仲裁模型已配置', True, f'{arb.name} ({arb.model})')

    loop = QEventLoop()

    def _watch(*_args):
        # 仲裁回调执行完（on_arbitration_finished 已先于本槽执行走子）
        QTimer.singleShot(50, loop.quit)

    # 先连接收集槽（在 _start_arbitration 内部 connect 之后执行）
    class _Probe:
        pass

    # 我们无法在 _start_arbitration 之前拿到 worker，改用轮询完成标志：
    # on_arbitration_finished 落子后 game.moves 会增加（注意不能用
    # arbitration_count——它在 _start_arbitration 启动时即递增）
    deadline = time.time() + 120
    timer = QTimer()
    timer.timeout.connect(lambda: (loop.quit()
                                   if time.time() > deadline or
                                   len(c.game.moves) > moves_before
                                   else None))
    timer.start(200)

    print(f'  分歧：LLM={coord_of(llm_move)} | 引擎={coord_of(engine_move)}')
    print(f'  启动仲裁 → {arb.name}（{arb.model}，type={arb.type}）')
    c._start_arbitration(1)
    loop.exec()

    moved = len(c.game.moves) > moves_before
    arb_ran = c.arbitration_count > arb_count_before
    check('仲裁已启动（计数递增）', arb_ran,
          f'{arb_count_before} -> {c.arbitration_count}')
    check('仲裁流程完成且已落子', moved,
          f'最后走子={last_move_of(c)}')
    if moved:
        m = last_move_of(c)
        check('落子为候选之一或 LLM 回退',
              m == llm_move or m == engine_move,
              f'实际={m}（LLM={coord_of(llm_move)} 引擎={coord_of(engine_move)}）')
    # 是否真实 API 裁决：计分变化只在"成功且选 LLM"时 +1，失败回退 +0；
    # 这里只报告过程，不做成败断言（取决于 API key/网络）
    print(f'  仲裁计分={c.ai_score} 累计仲裁次数={c.arbitration_count}')


def main():
    live = '--live' in sys.argv
    test_branches()
    if live:
        test_live()
    else:
        print('\n（未加 --live：跳过真实 DeepSeek 调用。'
              '加 --live 可测完整链路，需设置 DEEPSEEK_API_KEY）')
    print()
    if failures:
        print(f'失败 {len(failures)} 项: {failures}')
        sys.exit(1)
    print('仲裁测试全部通过')


if __name__ == '__main__':
    main()
