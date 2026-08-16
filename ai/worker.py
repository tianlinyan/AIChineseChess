import json
import re
import threading
from typing import Optional

import requests
from PyQt6.QtCore import QObject, pyqtSignal

from domain.models import ModelInfo
from ai.parser import parse_coordinates_from_text
from domain.constants import (
    AI_TIMEOUT_SECONDS, AI_CONNECT_TIMEOUT,
    AI_OUTPUT_TRUNCATE_LENGTH, AI_OUTPUT_MIN_TRIM_POSITION,
    BOARD_HEIGHT, BOARD_WIDTH,
    PIECE_SYMBOLS, parse_coord,
)
from domain.prompts import DEFAULT_TOOLS
from domain.evaluation import evaluate, PIECE_VALUE, compute_material
from domain.game import ChineseChessGame
from domain.search import SearchEngine


# ══════════════════════════════════════════════════════════════════════════════
# 多轮工具调用配置
# ══════════════════════════════════════════════════════════════════════════════

MAX_TOOL_TURNS = 4  # 每回合最多工具调用轮数（防止死循环）


def _clamp_int(value, lo: int, hi: int) -> int:
    """将参数钳制到 [lo, hi]，非数值类型抛 TypeError/ValueError。

    弱模型可能传字符串/None/bool（如 {"depth": "4"}），max/min 对这些
    值抛 TypeError 且无法被下游恢复——此处集中校验并给出可恢复的异常。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f'参数应为整数，实际为 {type(value).__name__}')
    return max(lo, min(hi, int(value)))


class AIWorkerSignals(QObject):
    finished = pyqtSignal(str, str, str, str, int, int, int)
    # from_coord, to_coord, full_text, error, tokens, version, cancel_version


class AIWorker:
    """LLM 请求工作器 — 由 controller 在裸 threading.Thread 中运行。

    （不是 QRunnable：继承它只是历史遗留，实际从未进入 QThreadPool；
    用裸线程是为了 cancel() 时能直接中断 requests.Session。）
    """
    def __init__(self, model_info: ModelInfo, prompt: str,
                 image_base64: Optional[str] = None,
                 player_name: str = '', version: int = 0,
                 cancel_version: int = 0,
                 system_prompt: str = '',
                 tools: Optional[tuple] = None,
                 timeout: int = AI_TIMEOUT_SECONDS,
                 # ── 工具执行所需 ──
                 game: Optional[ChineseChessGame] = None,
                 current_player: int = 0,
                 # Pikafish 引擎（evaluate_position 工具的大师级评估源；
                 # 由 controller 注入，None → 回退手工评估）
                 pikafish: Optional['PikafishEngine'] = None) -> None:
        super().__init__()
        self.model_info = model_info
        self.prompt = prompt
        self.image_base64 = image_base64
        self.player_name = player_name
        self.version = version
        self.cancel_version = cancel_version
        self.system_prompt = system_prompt
        self.tools = tools if tools is not None else DEFAULT_TOOLS
        self.timeout = timeout
        self.game = game
        self.current_player = current_player
        self.pikafish = pikafish
        self.signals = AIWorkerSignals()
        self._cancelled = threading.Event()
        self._session: Optional[requests.Session] = None

        # 收集所有工具调用产生的文本
        self._all_texts: list = []
        # 仅存正式 content（不含 reasoning_content / 工具结果）——
        # 兜底文本解析只用此池，排除 DeepSeek 推理文本里"讨论过但未选中"
        # 的坐标干扰（M-AI-5）
        self._content_texts: list = []
        # 在途搜索对象（SearchEngine 或 PikafishEngine，均有 .stop()；
        # cancel() 调用以中止，M-AI-1）
        self._active_search_engine = None
        # search_best_move 每步最多一次（系统提示词约定，此处强制）
        self._search_used = False
        # evaluate_position 每步最多两次（防弱模型刷满轮次导致无走法）
        self._evaluate_count = 0

    def run(self) -> None:
        self._session = requests.Session()
        self._session.trust_env = False
        try:
            from_coord, to_coord, full_text = self._agentic_loop(self._session)
            error = ''
            if (not from_coord or not to_coord) and 'ERROR:' in full_text:
                error = full_text
                full_text = ''
            self.signals.finished.emit(
                from_coord, to_coord, full_text, error, 0,
                self.version, self.cancel_version)
        except Exception as e:
            self.signals.finished.emit(
                '', '', '', str(e), 0,
                self.version, self.cancel_version)
        finally:
            self._session.close()

    # ── 多轮 Agentic Loop ──

    def _agentic_loop(self, session: requests.Session) -> tuple:
        """多轮工具调用循环。

        LLM 可以多次调用 search_best_move / evaluate_position 等工具，
        直到最终调用 move_piece 或达到最大轮数。
        """
        messages = [
            {'role': 'system', 'content': self.system_prompt},
            {'role': 'user', 'content': self._build_user_content()},
        ]

        from_coord = ''
        to_coord = ''

        for turn in range(MAX_TOOL_TURNS):
            if self._cancelled.is_set():
                raise Exception("任务已取消")

            payload = self._build_payload_with_messages(messages)
            resp = self._send_request(session, payload)

            try:
                data = resp.json()
            except Exception as e:
                raise Exception(f"API 响应不是有效的 JSON: {e}")

            # 防御畸形响应：choices 为空 / message 为 null 在内容过滤、
            # 弱本地模型与部分代理上是常态，不应以未分类异常终止循环
            choices = data.get('choices') or []
            if not choices:
                raise Exception(f"API 响应缺少 choices: {str(data)[:200]}")
            message = choices[0].get('message') or {}
            content = (message.get('content') or '').strip()
            reasoning = message.get('reasoning_content', '')

            # 收集文本：_all_texts 用于展示日志（含推理/工具结果）；
            # _content_texts 仅存正式 content，供兜底文本解析（M-AI-5）
            if reasoning:
                self._all_texts.append(reasoning)
            if content:
                self._all_texts.append(content)
                self._content_texts.append(content)

            # ── 提取 tool_calls ──
            tool_calls = message.get('tool_calls', [])
            if not tool_calls:
                # 无 tool_calls → 尝试文本解析（仅从 LLM 正式 content 提取，
                # 排除 [Tool: ...] 工具结果与 reasoning 里的坐标干扰）
                fc, tc = parse_coordinates_from_text(
                    '\n'.join(self._content_texts))
                if fc and tc:
                    return fc, tc, self._build_full_text()
                # 无法解析，继续等下一轮（或最终失败）
                break

            # ── 分类 tool_calls ──
            move_piece_call = None
            other_calls = []

            for tool_entry in tool_calls:
                # function 可能为 null（弱本地模型常见），防御 None
                func = tool_entry.get('function') or {}
                name = func.get('name', '')
                if name == 'move_piece':
                    move_piece_call = tool_entry
                else:
                    other_calls.append(tool_entry)

            # ── 处理 move_piece：合法则直接结束，否则反馈错误让模型自纠 ──
            move_error = None
            if move_piece_call is not None:
                fc, tc = self._extract_move_from_call(move_piece_call)
                if fc and tc:
                    if self._validate_move(fc, tc) is not None:
                        return fc, tc, self._build_full_text()
                    move_error = f'走法 {fc}→{tc} 不合法，请从合法走法列表中选择'
                else:
                    move_error = '坐标参数无效：请用列字母 A-I + 行数字 1-10 的格式'

            # ── 执行其他工具调用，并回应无效 move_piece ──
            if other_calls or move_error is not None:
                # 保留本轮全部 tool_calls（含无效 move_piece，下方追加对应
                # tool 结果）；仅剔除 reasoning_content，因其回传会被严格
                # 校验的部署（DeepSeek/vLLM）拒绝。
                messages.append({
                    'role': 'assistant',
                    'content': message.get('content') or '',
                    'tool_calls': tool_calls,
                })

                for tool_entry in other_calls:
                    func = tool_entry.get('function') or {}
                    name = func.get('name', '')
                    raw_args = func.get('arguments', '{}')
                    try:
                        args = json.loads(raw_args)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    if not isinstance(args, dict):
                        # arguments 解析成 list/标量 → 视为无参，避免 .get 崩溃
                        args = {}

                    result = self._execute_tool(name, args)
                    self._all_texts.append(f"[Tool: {name}]\n{result}")
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tool_entry.get('id', ''),
                        'content': result,
                    })

                # 无效 move_piece 也要给出 tool 结果，否则模型不知道被拒，
                # 会在后续轮次重复同样的坏调用，烧光 MAX_TOOL_TURNS
                if move_error is not None:
                    self._all_texts.append(
                        f"[Tool: move_piece]\n"
                        f"{json.dumps({'error': move_error}, ensure_ascii=False)}")
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': move_piece_call.get('id', ''),
                        'content': json.dumps({'error': move_error},
                                              ensure_ascii=False),
                    })
                # 继续下一轮，让 LLM 基于工具结果（含错误）做决策
                continue

            # tool_calls 非空时分类必然命中 move_piece_call 或 other_calls，
            # 上方分支恒 return/continue，以下不可达（防御性 break）
            break

        # ── 所有轮次结束，最后一次尝试文本解析 ──
        full = self._build_full_text()
        fc, tc = parse_coordinates_from_text('\n'.join(self._content_texts))
        if fc and tc:
            return fc, tc, full
        return '', '', f'ERROR: {MAX_TOOL_TURNS} 轮工具调用后未找到有效走法'

    # ── 工具执行 ──

    def _validate_move(self, from_coord: str, to_coord: str):
        """校验坐标字符串是否为当前局面的合法走法。

        返回 (fr, fc, tr, tc) 或 None（坐标格式错误或走法非法）。
        在 worker 侧校验而非推迟到 controller，避免"格式正确但违规"的
        走法浪费一整轮，也防止把非法走法当成最终结果提交。
        """
        try:
            fr, fc = parse_coord(from_coord)
            tr, tc = parse_coord(to_coord)
        except (ValueError, IndexError, TypeError, AttributeError):
            return None
        if self.game is None:
            return None
        move = (fr, fc, tr, tc)
        # 用快照校验（M-AI-6）：worker 线程不触碰 live game。直接调
        # live game 的 get_all_legal_moves 会在 is_in_check 自愈路径
        # 跨线程写 _king_pos 缓存，与主线程 move_piece 存在读-改-写
        # 窗口；快照完全隔离该竞态。
        tmp = self.game.snapshot()
        tmp.current_player = self.current_player
        return move if move in tmp.get_all_legal_moves(self.current_player) else None

    def _execute_tool(self, name: str, args: dict) -> str:
        """在本地执行 AI 工具调用，返回 JSON 结果字符串。"""
        if self.game is None:
            return json.dumps({'error': '游戏状态不可用，无法执行工具调用'},
                             ensure_ascii=False)

        if name == 'search_best_move':
            if self._search_used:
                return json.dumps(
                    {'error': '本回合已调用过 search_best_move（每步最多一次），请基于已有信息选择走法'},
                    ensure_ascii=False)
            self._search_used = True
            return self._run_search(args)
        elif name == 'evaluate_position':
            if self._evaluate_count >= 2:
                return json.dumps(
                    {'error': '本回合已调用过 2 次 evaluate_position（每步最多 2 次），请基于已有信息选择走法'},
                    ensure_ascii=False)
            self._evaluate_count += 1
            return self._run_evaluate()
        else:
            return json.dumps({'error': f'未知工具: {name}'}, ensure_ascii=False)

    def _run_search(self, args: dict) -> str:
        """执行深度搜索，返回搜索最佳走法及候选列表。

        优先 Pikafish 深搜（MultiPV 主变，大师级战术）——LLM 拿到的
        战术参考与引擎决策同源；Pikafish 不可用/未初始化/搜索失败时
        回退本地 Alpha-Beta（原实现）。
        """
        try:
            # 参数类型防线：弱模型可能传字符串/None，钳制必须在 try 内，
            # 否则 TypeError 冒泡到 run() 兜底 → 整步失败且模型得不到可恢复错误
            depth = _clamp_int(args.get('depth', 3), 2, 8)
            top_n = _clamp_int(args.get('top_n', 3), 1, 5)
        except (TypeError, ValueError):
            return json.dumps(
                {'error': '参数类型错误：depth 应为 2~8、top_n 应为 1~5 的整数'},
                ensure_ascii=False)

        try:
            # 用快照隔离，不修改 self.game.board
            tmp_game = self.game.snapshot()
            tmp_game.current_player = self.current_player

            if self.pikafish is not None:
                result = self._run_search_pikafish(tmp_game, depth, top_n)
                if result is not None:
                    return result
                # Pikafish 失败 → 静默回退本地（不把引擎故障暴露给模型）。
                # 但若已被 cancel（暂停/重置），不再启动全量本地 AB：
                # 否则取消不生效且陈旧 worker 空耗 CPU 至多 30s
                if self._cancelled.is_set():
                    return json.dumps({'error': '任务已取消'}, ensure_ascii=False)
            return self._run_search_local(tmp_game, depth, top_n)
        except Exception as e:
            return json.dumps({'error': f'搜索失败: {e}'}, ensure_ascii=False)

    def _run_search_pikafish(self, tmp_game, depth: int,
                             top_n: int):
        """Pikafish 深搜路径（MultiPV）。返回 JSON 字符串；失败返回 None 回退。"""
        pf = self.pikafish
        player = self.current_player
        # depth 参数映射为思考时限（2~8 → 2.6s~7.4s，封顶 8s）
        movetime = min(8000, 1000 + depth * 800)
        # 注册在途搜索：cancel() 发 UCI stop 尽快中止（M-AI-1 同款机制）
        self._active_search_engine = pf
        try:
            # 持锁原子搜索：一次持锁内完成「切 MultiPV 3 → 搜索 → 恢复 1」
            # 并返回候选快照——消除 set/search/restore 撕裂窗口与
            # _top_moves 跨搜索 TOCTOU（见 PikafishEngine.search_atomic）；
            # 锁超时/引擎失败返回 None → 回退本地
            result = pf.search_atomic(tmp_game, player, movetime,
                                      multipv=3, lock_timeout_ms=5000)
            if result is None:
                return None
            best_move, raw_top = result
            # MultiPV 候选：走子方视角评分 → 统一红方视角（与其余工具口径一致）
            top_moves = []
            for mv, sc in raw_top[:top_n]:
                top_moves.append((mv, sc if player == 1 else -sc))
            best_score = top_moves[0][1] if top_moves else 0.0

            board = tmp_game.board
            lines = []
            lines.append(f"Pikafish 深度搜索完成（时限 {movetime // 1000}s，"
                         f"MultiPV {len(raw_top)} 线）")
            if best_score >= 99990:
                lines.append("搜索最佳评分: 将杀（红方胜定）")
            elif best_score <= -99990:
                lines.append("搜索最佳评分: 将杀（黑方胜定）")
            else:
                lines.append(f"搜索最佳评分: {best_score:+.0f}"
                             f"（正值=红优，负值=黑优）")
            lines.append("")

            bfr, bfc, btr, btc = best_move
            bp = board[bfr][bfc]
            bpn = PIECE_SYMBOLS.get(bp, bp)
            bc = board[btr][btc]
            bcap = f" 吃{PIECE_SYMBOLS.get(bc, bc)}" if bc != '.' else ''
            lines.append(f"★ 搜索首选: {bpn} {chr(65+bfc)}{bfr+1}→"
                         f"{chr(65+btc)}{btr+1}{bcap}")
            lines.append("")

            lines.append(f"候选走法 Top-{len(top_moves)}（MultiPV 主变，按引擎评分排序）：")
            for i, (mv, _score) in enumerate(top_moves, 1):
                fr, fc, tr, tc = mv
                piece = board[fr][fc]
                pn = PIECE_SYMBOLS.get(piece, piece)
                from_c = f"{chr(65+fc)}{fr+1}"
                to_c = f"{chr(65+tc)}{tr+1}"
                cap = board[tr][tc]
                cap_info = f" 吃{PIECE_SYMBOLS.get(cap, cap)}" if cap != '.' else ''
                marker = ' ← 搜索首选' if mv == best_move else ''
                lines.append(f"  {i}. {pn} {from_c}→{to_c}{cap_info}{marker}")

            lines.append("")
            lines.append("请综合考虑搜索建议和你的战略判断，选择最优走法。最终调用 move_piece 提交。")
            return json.dumps({'result': '\n'.join(lines)}, ensure_ascii=False)
        except Exception:
            return None
        finally:
            self._active_search_engine = None

    def _run_search_local(self, tmp_game, depth: int, top_n: int) -> str:
        """回退路径：本地 Alpha-Beta 深搜索 + 全走法静态评估排序（原实现）。"""
        player = self.current_player
        opponent = 3 - player
        try:
            engine = SearchEngine(
                max_depth=depth,
                time_limit=min(30.0, 2.0 + depth * 3.0),
            )
            # 注册在途引擎：cancel() 调 engine.stop() 尽快中止搜索（M-AI-1）
            self._active_search_engine = engine
            best_move = engine.search(tmp_game, player)

            if not best_move:
                return json.dumps({'error': '未找到合法走法'}, ensure_ascii=False)

            board = tmp_game.board  # 快照棋盘，不碰 live board

            # 对所有走法用增强评估排序（MVV-LVA + 局面分 + 将军检测）
            # 使用 tmp_game 隔离，避免工作线程访问 self.game.board 的数据竞争
            all_moves = tmp_game.get_all_legal_moves(player)
            scored = []
            for fr, fc, tr, tc in all_moves:
                piece = board[fr][fc]
                captured = SearchEngine._make_move(tmp_game, fr, fc, tr, tc)
                # 静态局面分（红方视角）
                s = evaluate(board)
                # 将军奖励：走子后对方被将军额外加分
                # 使用 tmp_game 隔离，避免修改 self.game.board（防止与主线程/Pikafish 的数据竞争）
                tmp_game.board = board
                if tmp_game.is_in_check(opponent):
                    s += 50.0 if player == 1 else -50.0
                SearchEngine._unmake_move(tmp_game, fr, fc, tr, tc, captured)
                # 评分统一红方视角（正值=红优，负值=黑优），与 evaluate_position
                # 工具的口径一致——避免黑方走棋时两工具同号含义相反
                # MVV-LVA 吃子加分（红方视角）：红吃子加分，黑吃子减分
                if captured != '.':
                    bonus = (PIECE_VALUE.get(captured.upper(), 0) * 10
                             - PIECE_VALUE.get(piece.upper(), 0))
                    s += bonus if player == 1 else -bonus
                scored.append((fr, fc, tr, tc, s))

            # 红方视角排序：红方走棋高分在前，黑方走棋低分在前
            scored.sort(key=lambda x: x[4], reverse=(player == 1))
            top_moves = scored[:top_n]
            # best_score 本身就是红方视角（search.py 公开接口约定），不再转换
            best_score = engine.best_score

            # 格式化
            lines = []
            lines.append(f"Alpha-Beta 搜索完成（深度={depth}，{engine.nodes_searched} 节点）")
            lines.append(f"搜索最佳评分: {best_score:+.0f}（正值=红优，负值=黑优）")
            lines.append("")

            # 搜索最佳走法高亮
            bfr, bfc, btr, btc = best_move
            bp = board[bfr][bfc]
            bpn = PIECE_SYMBOLS.get(bp, bp)
            bc = board[btr][btc]
            bcap = f" 吃{PIECE_SYMBOLS.get(bc, bc)}" if bc != '.' else ''
            lines.append(f"★ 搜索首选: {bpn} {chr(65+bfc)}{bfr+1}→{chr(65+btc)}{btr+1}{bcap}")
            lines.append("")

            # 候选按战术优先级排序（吃大子>将军>局面评估），只印排名不印分数：
            # 排序键由 MVV-LVA×10 加分与评估分混合构成，量纲不同，数值会误导 LLM 比较
            lines.append(f"候选走法 Top-{len(top_moves)}（战术优先级排序：吃大子、将军优先，其后按局面评估）：")
            for i, (fr, fc, tr, tc, _s) in enumerate(top_moves, 1):
                piece = board[fr][fc]
                piece_name = PIECE_SYMBOLS.get(piece, piece)
                from_c = f"{chr(65+fc)}{fr+1}"
                to_c = f"{chr(65+tc)}{tr+1}"
                captured = board[tr][tc]
                cap_info = ''
                if captured != '.':
                    cap_name = PIECE_SYMBOLS.get(captured, captured)
                    cap_info = f" 吃{cap_name}"
                marker = ' ← 搜索首选' if (fr, fc, tr, tc) == best_move else ''
                lines.append(f"  {i}. {piece_name} {from_c}→{to_c}{cap_info}{marker}")

            lines.append("")
            lines.append("请综合考虑搜索建议和你的战略判断，选择最优走法。最终调用 move_piece 提交。")
            return json.dumps({'result': '\n'.join(lines)}, ensure_ascii=False)

        except Exception as e:
            return json.dumps({'error': f'搜索失败: {e}'}, ensure_ascii=False)
        finally:
            # 无论成功/异常/被 cancel 中止，都解除在途引擎引用
            self._active_search_engine = None

    def _run_evaluate(self) -> str:
        """执行静态局面评估"""
        try:
            # 使用快照隔离，避免工作线程访问 self.game.board 的数据竞争
            tmp_game = self.game.snapshot()
            tmp_game.current_player = self.current_player
            board = tmp_game.board
            red_moves = tmp_game.get_all_legal_moves(1)
            black_moves = tmp_game.get_all_legal_moves(2)
            red_check = tmp_game.is_in_check(1)
            black_check = tmp_game.is_in_check(2)

            total_pieces = sum(1 for r in range(BOARD_HEIGHT)
                              for c in range(BOARD_WIDTH) if board[r][c] != '.')
            endgame = total_pieces <= 14

            score = None
            score_source = '手工评估'
            # Pikafish 大师级评估优先（evaluate_fen：红方视角厘兵，与
            # evaluate() 口径一致）。worker 线程在 LLM 回合中调用，此时
            # hybrid 模式的引擎搜索已完成（LLM 在其后启动），锁空闲；
            # 引擎不可用/超时/未初始化 → None 回退手工评估。
            if self.pikafish is not None:
                try:
                    s = self.pikafish.evaluate_fen(board, self.current_player)
                    if s is not None:
                        score = s
                        score_source = 'Pikafish NNUE'
                except Exception:
                    score = None
            if score is None:
                score = evaluate(
                    board,
                    legal_moves_red=len(red_moves),
                    legal_moves_black=len(black_moves),
                    red_in_check=red_check,
                    black_in_check=black_check,
                    endgame=endgame,
                )

            # 统计子力（共享函数，不含将/帥，单位=兵）
            red_material, black_material, _, _ = compute_material(board)

            lines = []
            lines.append(f"局面评估: {score:+.0f}（正值=红优，负值=黑优，"
                         f"{score_source}）")
            lines.append(f"红方子力: {red_material:g}  |  黑方子力: {black_material:g}")
            material_diff = red_material - black_material
            if material_diff > 0:
                lines.append(f"红方多子 +{material_diff:g}")
            elif material_diff < 0:
                lines.append(f"黑方多子 +{-material_diff:g}")
            else:
                lines.append("子力均等")
            lines.append(f"红方走法: {len(red_moves)} 种  |  黑方走法: {len(black_moves)} 种")
            lines.append(f"局面阶段: {'残局' if endgame else '中局/开局'}")
            if red_check:
                lines.append("⚠️ 红方正在被将军！")
            if black_check:
                lines.append("⚠️ 黑方正在被将军！")
            lines.append(f"总子力: {total_pieces}/32")

            return json.dumps({'result': '\n'.join(lines)}, ensure_ascii=False)

        except Exception as e:
            return json.dumps({'error': f'评估失败: {e}'}, ensure_ascii=False)

    # ── 辅助 ──

    def _build_full_text(self) -> str:
        parts = [t for t in self._all_texts if t]
        full = '\n\n'.join(parts)
        # 智能截断
        if len(full) > AI_OUTPUT_TRUNCATE_LENGTH:
            m = re.search(
                r'.*[。！？!\?\n]',
                full[AI_OUTPUT_MIN_TRIM_POSITION:AI_OUTPUT_TRUNCATE_LENGTH]
            )
            if m:
                full = full[:AI_OUTPUT_MIN_TRIM_POSITION + m.end()]
            else:
                full = full[:AI_OUTPUT_TRUNCATE_LENGTH] + '...'
        return full

    def _extract_move_from_call(self, tool_call: dict) -> tuple:
        """从单个 tool_call 提取 move_piece 坐标"""
        func = tool_call.get('function', {})
        if func.get('name') != 'move_piece':
            return '', ''
        args_data = func.get('arguments', {})
        if isinstance(args_data, str):
            try:
                args = json.loads(args_data)
            except (json.JSONDecodeError, TypeError):
                return '', ''
        else:
            args = args_data
        if not isinstance(args, dict):
            return '', ''  # arguments 为 null/list/标量（弱模型常见）
        from_coord = args.get('from', '')
        to_coord = args.get('to', '')
        # 类型防线：弱模型可能输出非字符串坐标（数字/数组），truthy 判断
        # 无法拦截 → 逃逸到 controller 的 parse_coord 抛 TypeError/AttributeError
        # （其 except 只捕获 ValueError/IndexError），异常进入 Qt 槽导致
        # set_busy(False) 不执行、游戏永久卡死。此处强制字符串类型并归一化。
        if not isinstance(from_coord, str) or not isinstance(to_coord, str):
            return '', ''
        return from_coord.strip().upper(), to_coord.strip().upper()

    # ── API 请求 ──

    def _is_llama_server(self) -> bool:
        return self.model_info.type == 'llama-server'

    def cancel(self) -> None:
        self._cancelled.set()
        # 中止在途的 Alpha-Beta 搜索（M-AI-1）：SearchEngine.stop() 置
        # _stop_flag，_is_time_up 随即返回 True，搜索尽快退出（单次最长
        # ~30s → 通常在毫秒级响应）。属性读写跨线程，GIL 下原子。
        engine = self._active_search_engine
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                pass
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass

    def _build_payload_with_messages(self, messages: list) -> dict:
        """使用完整 message 历史构建 payload（用于多轮对话）"""
        payload = {
            'model': self.model_info.model,
            'messages': messages,
            'stream': False,
            'tools': list(self.tools),
        }
        is_llama_server = self._is_llama_server()
        if self.model_info.tools_choice:
            if is_llama_server:
                payload['tool_choice'] = 'required'
            else:
                payload['tool_choice'] = self.model_info.tools_choice
        # 不传递 think / enable_thinking 参数：think 模式取消，
        # 由 llama-server / DeepSeek 按其默认行为处理
        payload.update(self.model_info.options)
        return payload

    def _build_user_content(self):
        """构建用户消息内容。

        DeepSeek API 不接受 image_url 类型（仅 text），会直接 400。
        安全网：即使 controller 误传了 image_base64，此处也过滤掉。
        """
        if self.image_base64 and self.model_info.type != 'deepseek':
            return [
                {'type': 'text', 'text': self.prompt or "请根据棋盘图像分析局势。"},
                {'type': 'image_url', 'image_url': {
                    'url': f'data:image/jpeg;base64,{self.image_base64}'}}
            ]
        return self.prompt

    def _send_request(self, session: requests.Session,
                      payload: dict) -> requests.Response:
        headers = {}
        if self.model_info.api_key:
            headers['Authorization'] = f'Bearer {self.model_info.api_key}'
        try:
            resp = session.post(
                self.model_info.endpoint, json=payload, headers=headers,
                # 连接/读取超时分离：端点黑洞时连接阶段快速失败，
                # 读取阶段仍允许长思考（每轮请求独立计时）
                timeout=(AI_CONNECT_TIMEOUT, self.timeout))
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout:
            raise Exception("超时: API 请求超时")
        except requests.exceptions.ConnectionError as e:
            raise Exception(f"连接错误: API 连接失败: {e}")
        except requests.exceptions.HTTPError as e:
            error_detail = ''
            try:
                # 截断：防整页 HTML 灌入日志、防网关回显请求头（含 Bearer key）
                error_detail = (resp.text[:500] if resp is not None
                                else '无响应')
            except Exception:
                pass
            status = resp.status_code if resp is not None else 0
            if status in (408, 429):
                # 请求超时/速率限制：最值得退避重试的错误，
                # 不能归入"客户端错误"（controller 对那类直接放弃 LLM）
                raise Exception(f"限流错误: API 请求失败: {e}\n响应: {error_detail}")
            if 400 <= status < 500:
                raise Exception(f"客户端错误: API 请求失败: {e}\n响应: {error_detail}")
            raise Exception(f"服务器错误: API 请求失败: {e}\n响应: {error_detail}")
        except requests.exceptions.RequestException as e:
            raise Exception(f"网络错误: API 请求异常: {e}")
