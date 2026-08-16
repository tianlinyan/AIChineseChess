"""qwen3.8 走子工具调用压力测试（定位"4 轮失败"的真实环节）。

复用 AIWorker._agentic_loop 真实逻辑 + 复刻 controller._start_llm_request
的提示词构建，直连 llama-server 端点连跑 N 次，统计：
  - 成功率 / 失败率
  - 失败形态：无 tool_calls / arguments 坏 / 坐标非法 / 4 轮耗尽
  - 每次的请求/响应结构（AI_DEBUG=1 时输出到 stderr）

用法：
  python tests/llm_pressure_test.py [次数] [--with-engine-hint] [--ai-debug]
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.prompts import (
    get_system_prompt, build_move_prompt, format_legal_moves,
    DEFAULT_TOOLS,
)
from domain.evaluation import compute_material
from ai.worker import AIWorker

N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
WITH_HINT = '--with-engine-hint' in sys.argv
MIDGAME = '--midgame' in sys.argv
if '--ai-debug' in sys.argv:
    os.environ['AI_DEBUG'] = '1'

import requests
from services.models import ModelManager

# 加载 qwen3.8（黑方模型）
mm = ModelManager()
mm.load(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'models.json'))
model = next((m for m in mm.models if m.id == 'qwen3.8-p2'), None)
if model is None:
    print('[FAIL] models.json 中未找到 qwen3.8-p2')
    sys.exit(1)
print(f'模型: {model.name} ({model.model}, {model.type}) '
      f'端点: {model.endpoint}')


def build_prompt():
    import random as _rnd
    g = ChineseChessGame()
    if MIDGAME:
        # 随机走 10 步（红黑交替）构造中局局面，贴近真实对局
        _rnd.seed()
        for _ in range(10):
            pl = g.current_player
            mv = _rnd.choice(g.get_all_legal_moves(pl))
            g.move_piece(*mv)
            if g.game_over:
                break
    player = g.current_player  # 当前走子方（红/黑交替）
    board_str = g.get_board_state_string()
    history = g.format_move_history(max_items=24)
    in_check = g.is_in_check(player)
    opponent_in_check = g.is_in_check(3 - player)
    move_count = len(g.moves) // 2 + 1
    legal_moves = g.get_all_legal_moves(player)
    repetition_moves = g.find_repetition_moves(legal_moves)
    legal_moves_str = format_legal_moves(
        legal_moves, g.board, player, repetition_moves=repetition_moves)
    red_mat, black_mat, _, _ = compute_material(g.board)
    mine, theirs = (black_mat, red_mat) if player == 2 else (red_mat, black_mat)
    material_str = (f'子力对比（单位=兵）：你 {mine:g} : {theirs:g} 对手'
                    f'（正值=你领先）')
    engine_hint = ''
    if WITH_HINT:
        # 真实场景：引擎推荐对当前走子方必然合法（从合法列表取第一个）
        if legal_moves:
            r, c, tr, tc = legal_moves[0]
            hint_move = (f"{chr(65 + c)}{r + 1}→{chr(65 + tc)}{tr + 1}")
        else:
            hint_move = '（无合法走法）'
        engine_hint = ('## 🔍 Pikafish 参考走法\n\nPikafish 深度搜索完成，'
                       f'推荐：\n**{hint_move}**\n\n'
                       'Pikafish 是顶级战术引擎，其推荐经过深度分析，'
                       '应作为**重要参考优先考虑**。')
    prompt = build_move_prompt(
        player, board_str, history, in_check=in_check,
        opponent_in_check=opponent_in_check, move_count=move_count,
        legal_moves_str=legal_moves_str, vision_mode=False,
        engine_hint=engine_hint, material_str=material_str)
    system_prompt = get_system_prompt(include_analysis_tools=True)
    return g, player, prompt, system_prompt


def main():
    print(f'压力测试: {N} 次 × 黑方走子（引擎参考={WITH_HINT}）\n')
    ok = 0
    fail_modes = {}
    total_time = 0.0
    session = requests.Session()
    session.trust_env = False

    for i in range(1, N + 1):
        g, player, prompt, system_prompt = build_prompt()
        w = AIWorker(model, prompt, None, '黑方', version=0, cancel_version=0,
                     system_prompt=system_prompt, tools=DEFAULT_TOOLS,
                     game=g, current_player=player)
        t0 = time.time()
        try:
            fc, tc, full = w._agentic_loop(session)
        except Exception as e:
            fc = tc = None
            full = f'异常: {e}'
        dt = time.time() - t0
        total_time += dt
        if fc and tc:
            ok += 1
            print(f'[{i:2d}] ✓ {fc}→{tc}  ({dt:.1f}s)')
        else:
            if 'ERROR:' in full:
                mode = full[:full.index('\n')] if '\n' in full else full
                # 归类失败形态
                if '4 轮' in full:
                    mode = '4轮耗尽'
                fail_modes[mode] = fail_modes.get(mode, 0) + 1
            else:
                mode = f'异常: {full[:120]}'
                fail_modes[mode] = fail_modes.get(mode, 0) + 1
            print(f'[{i:2d}] ✗ {mode}  ({dt:.1f}s)')
        # 慢端点：避免连续请求被限流
        time.sleep(0.5)

    session.close()
    print(f'\n结果: {ok}/{N} 成功，平均耗时 {total_time / N:.1f}s')
    if fail_modes:
        print('失败形态分布:')
        for mode, cnt in sorted(fail_modes.items(), key=lambda x: -x[1]):
            print(f'  {cnt:2d}× {mode}')
        sys.exit(1)
    print('全部成功')


if __name__ == '__main__':
    main()
