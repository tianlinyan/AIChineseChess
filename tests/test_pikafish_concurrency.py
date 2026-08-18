"""Pikafish 并发搜索竞态回归测试（P4 修复验证）

用法：python tests/test_pikafish_concurrency.py
（需要 engines/pikafish.exe 可用；不可用时跳过并退出 0）

背景：search_async 的 daemon 线程曾在**调用线程锁外**重置 _top_moves 缓存。
若并发的 search_atomic 在"重置→搜索"窗口内完成并 finalize，daemon 的
multipv 行会追加进 atomic 填充的 _top_moves_dict，finalize 后 _top_moves
混杂两次搜索的候选。修复：重置移入 daemon 持锁段（与 _search_locked 同序）。

方法：每轮用两个不同局面（A=初始、B=炮二平五后）：
  1. 测试线程持锁 → 启动 search_async（daemon 阻塞在锁上）→ 释放锁
  2. 测试线程立即 search_atomic（与 daemon 竞争锁，两种顺序都可能）
  3. 断言：_top_moves 要么 == atomic 快照（atomic 后跑），
     要么全部条目都是局面 A 的合法走法（async 后跑，MultiPV=1）。
     旧代码在 async 后跑时会混入 atomic 的 multipv 2/3 候选（局面 B 的
     走法，通常非法于 A）→ 断言失败。
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from domain.game import ChineseChessGame
from domain.pikafish import PikafishEngine

ROUNDS = 30
SEARCH_MS = 300

FAILED = []


def check(name, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name}' + (f' — {detail}' if detail else ''))
    if not cond:
        FAILED.append(name)


def main():
    engine = PikafishEngine()
    if not engine.available:
        print(f'SKIP: Pikafish 不可用（{engine.error_msg}）')
        return

    g_a = ChineseChessGame()
    legal_a = set(g_a.get_all_legal_moves(g_a.current_player))

    g_b = ChineseChessGame()
    g_b.move_piece(7, 7, 7, 4)  # 炮二平五
    legal_b = set(g_b.get_all_legal_moves(g_b.current_player))

    check('两个局面的合法走法集不同（测试前提）', legal_a != legal_b)

    mixed_rounds = 0
    for rnd in range(ROUNDS):
        done = threading.Event()
        async_result = {}

        def _cb(move, error, _r=async_result):
            _r['move'], _r['error'] = move, error
            done.set()

        # 1. 持锁启动 async：daemon 确定阻塞在锁上（重置已发生/将发生）
        with engine._lock:
            engine.search_async(g_a, g_a.current_player, SEARCH_MS, _cb)
        # 2. 释放锁后立即 search_atomic：与 daemon 竞争
        res = engine.search_atomic(g_b, g_b.current_player, SEARCH_MS,
                                   multipv=3, lock_timeout_ms=15000)
        if not done.wait(20):
            check(f'轮 {rnd}: async 回调超时', False)
            break
        if res is None:
            check(f'轮 {rnd}: search_atomic 返回 None',
                  False, async_result.get('error', ''))
            break
        _, atomic_snapshot = res

        # 3. 一致性断言（与谁后跑无关）
        top = list(engine._top_moves)
        if top == list(atomic_snapshot):
            pass  # atomic 后跑：快照即最终值
        else:
            # async 后跑：每个条目都必须是局面 A 的合法走法
            bad = [mv for mv, _s in top if mv not in legal_a]
            if bad:
                mixed_rounds += 1
                check(f'轮 {rnd}: _top_moves 混入其他搜索的候选',
                      False, f'非法于 A 的条目: {bad[:3]}')

    engine.close()

    if not FAILED:
        print(f'\n{ROUNDS} 轮并发搜索全部一致（无 _top_moves 污染）')
        print('全部通过')
    else:
        print(f'\nFAILED {len(FAILED)} 项')
        sys.exit(1)


if __name__ == '__main__':
    main()
