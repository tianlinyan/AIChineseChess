"""仲裁 AI（qwen3.8）压力测试：二选一任务是否也存在"诱导提交非法坐标/4 轮耗尽"。

完全复刻 controller._start_arbitration 的提示词构建与 AIWorker 构造
（tools=TOOLS_BASIC、get_arbitration_system_prompt、build_arbitration_prompt），
直连 llama-server 连跑 N 次，统计结果分类：
  - 二选一命中（LLM/引擎候选）→ 仲裁真成功
  - 第三合法走法（controller 会采纳引擎）→ 可恢复降级
  - 非法坐标提交（controller 走子失败→LLM 回退）→ 失败
  - 4 轮耗尽（无坐标）→ 失败

用法：
  python tests/arbitration_pressure_test.py [次数] [--ai-debug]
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.prompts import (
    get_arbitration_system_prompt, build_arbitration_prompt,
    format_legal_moves, TOOLS_BASIC,
)
from domain.evaluation import compute_material
from ai.worker import AIWorker
from app.controller import GameController
from ai.manager import AIManager

N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 15
MIDGAME = '--midgame' in sys.argv
if '--ai-debug' in sys.argv:
    os.environ['AI_DEBUG'] = '1'

import requests
from services.models import ModelManager

mm = ModelManager()
mm.load(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'models.json'))
model = next((m for m in mm.models if m.id == 'arbitration'), None)
if model is None:
    print('[FAIL] models.json 未找到 arbitration 模型')
    sys.exit(1)
print(f'仲裁模型: {model.name} ({model.model}, {model.type}) '
      f'端点: {model.endpoint}\n')


def build_arb_worker():
    """复刻 controller._start_arbitration：分歧状态 → 提示词 → AIWorker。"""
    import random as _rnd
    g = ChineseChessGame()
    if MIDGAME:
        # 随机走 10 步（红黑交替）构造中局局面
        _rnd.seed()
        for _ in range(10):
            pl = g.current_player
            mv = _rnd.choice(g.get_all_legal_moves(pl))
            g.move_piece(*mv)
            if g.game_over:
                break
    c = GameController(g, AIManager())
    player = g.current_player  # 当前走子方
    legal = g.get_all_legal_moves(player)
    llm_move = legal[0]
    engine_move = legal[1]
    repetition_moves = g.find_repetition_moves(legal)

    board_str = g.get_board_state_string()
    history = g.format_move_history(max_items=24)
    legal_moves_str = format_legal_moves(legal, g.board, player,
                                         repetition_moves=repetition_moves)
    move_count = len(g.moves) // 2 + 1
    red_mat, black_mat, _, _ = compute_material(g.board)
    material_str = f'子力对比（单位=兵）：红 {red_mat:g} : {black_mat:g}'

    c._arbitration_llm_move = llm_move
    c._arbitration_engine_move = engine_move
    c._arbitration_llm_text = '我方判断：此着法保持子力协调，为残局做准备。'

    # 复刻 controller._start_arbitration 的 A/B 顺序 + label_to_move 接线
    import random as _rnd2
    order = [0, 1]
    _rnd2.shuffle(order)
    label_to_move = {
        'A': llm_move if order[0] == 0 else engine_move,
        'B': llm_move if order[1] == 0 else engine_move,
    }

    engine_basis = c._build_candidate_facts(engine_move, repetition_moves)
    llm_basis = c._build_candidate_facts(llm_move, repetition_moves)

    def _coord_str(mv):
        return (f"{chr(65 + mv[1])}{mv[0] + 1}→{chr(65 + mv[3])}{mv[2] + 1}")

    prompt = build_arbitration_prompt(
        player=player, board_str=board_str, history=history,
        legal_moves_str=legal_moves_str,
        llm_move_str=_coord_str(llm_move),
        llm_reasoning=llm_basis + '\n' + c._arbitration_llm_text,
        engine_move_str=_coord_str(engine_move),
        engine_basis=engine_basis,
        in_check=False, opponent_in_check=False, move_count=move_count,
        material_str=material_str,
        candidate_order=order,
    )
    worker = AIWorker(model, prompt, None, version=0, cancel_version=0,
                      system_prompt=get_arbitration_system_prompt(),
                      tools=TOOLS_BASIC, game=g, current_player=player,
                      allowed_moves={llm_move, engine_move},
                      label_to_move=label_to_move)
    return worker, llm_move, engine_move


def main():
    cats = {'二选一命中': 0, '第三合法走法': 0, '非法坐标': 0, '4轮耗尽': 0,
            '异常': 0}
    samples = []
    total_time = 0.0
    session = requests.Session()
    session.trust_env = False

    for i in range(1, N + 1):
        worker, llm_move, engine_move = build_arb_worker()
        g = worker.game
        legal = set(g.get_all_legal_moves(worker.current_player))
        t0 = time.time()
        try:
            fc, tc, full = worker._agentic_loop(session)
        except Exception as e:
            fc = tc = None
            full = f'异常: {e}'
        dt = time.time() - t0
        total_time += dt

        if fc and tc:
            try:
                fr, fcc = __import__('domain.constants', fromlist=['parse_coord']).parse_coord(fc)
                tr, tcc = __import__('domain.constants', fromlist=['parse_coord']).parse_coord(tc)
                mv = (fr, fcc, tr, tcc)
            except Exception:
                mv = None
            if mv == llm_move or mv == engine_move:
                cat = '二选一命中'
            elif mv in legal:
                cat = '第三合法走法'
            else:
                cat = '非法坐标'
        else:
            cat = '4轮耗尽' if '4 轮' in full else '异常'
        cats[cat] += 1
        samples.append((i, cat, fc, tc, dt))
        print(f'[{i:2d}] {cat:6s} {fc or "":>4}→{tc or "":<4} ({dt:.1f}s)')

    session.close()
    print(f'\n结果（N={N}，平均 {total_time / N:.1f}s）：')
    for cat, cnt in cats.items():
        print(f'  {cnt:2d}/{N}  {cat}')
    print(f'\nLLM={samples[0] if samples else ""} 候选：'
          f'二选一命中率={cats["二选一命中"]}/{N}')
    if cats['二选一命中'] < N:
        print('存在降级/失败样本（非法坐标/4轮耗尽 = 与走子相同问题）')
        sys.exit(1)
    print('仲裁全部二选一命中')


if __name__ == '__main__':
    main()
