import json
import re
import threading
from typing import Optional

import requests
from PyQt6.QtCore import QObject, pyqtSignal, QRunnable

from ai.models import ModelInfo
from ai.parser import parse_coordinates_from_text
from domain.constants import (
    AI_TIMEOUT_SECONDS, AI_OUTPUT_TRUNCATE_LENGTH,
    AI_OUTPUT_MIN_TRIM_POSITION,
    BOARD_HEIGHT, BOARD_WIDTH,
    PIECE_SYMBOLS,
)
from domain.prompts import DEFAULT_TOOLS
from domain.evaluation import evaluate, PIECE_VALUE
from domain.game import ChineseChessGame


# ══════════════════════════════════════════════════════════════════════════════
# 多轮工具调用配置
# ══════════════════════════════════════════════════════════════════════════════

MAX_TOOL_TURNS = 4  # 每回合最多工具调用轮数（防止死循环）


class AIWorkerSignals(QObject):
    finished = pyqtSignal(str, str, str, str, int, int, int)
    # from_coord, to_coord, full_text, error, tokens, version, cancel_version


class AIWorker(QRunnable):
    def __init__(self, model_info: ModelInfo, prompt: str,
                 image_base64: Optional[str] = None,
                 player_name: str = '', version: int = 0,
                 cancel_version: int = 0,
                 think: bool = True,
                 system_prompt: str = '',
                 tools: Optional[tuple] = None,
                 timeout: int = AI_TIMEOUT_SECONDS,
                 # ── 工具执行所需 ──
                 game: Optional[ChineseChessGame] = None,
                 current_player: int = 0) -> None:
        super().__init__()
        self.model_info = model_info
        self.prompt = prompt
        self.image_base64 = image_base64
        self.player_name = player_name
        self.version = version
        self.cancel_version = cancel_version
        self.think = think
        self.system_prompt = system_prompt
        self.tools = tools if tools is not None else DEFAULT_TOOLS
        self.timeout = timeout
        self.game = game
        self.current_player = current_player
        self.signals = AIWorkerSignals()
        self._cancelled = threading.Event()
        self._session: Optional[requests.Session] = None

        # 收集所有工具调用产生的文本
        self._all_texts: list = []

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

            choice = data.get('choices', [{}])[0]
            message = choice.get('message', {})
            content = (message.get('content') or '').strip()
            reasoning = message.get('reasoning_content', '')

            # 收集文本
            if reasoning:
                self._all_texts.append(reasoning)
            if content:
                self._all_texts.append(content)

            # ── 提取 tool_calls ──
            tool_calls = message.get('tool_calls', [])
            if not tool_calls:
                # 无 tool_calls → 尝试文本解析
                fc, tc = parse_coordinates_from_text(
                    '\n'.join(self._all_texts))
                if fc and tc:
                    return fc, tc, self._build_full_text()
                # 无法解析，继续等下一轮（或最终失败）
                break

            # ── 分类 tool_calls ──
            move_piece_call = None
            other_calls = []

            for tool_entry in tool_calls:
                func = tool_entry.get('function', {})
                name = func.get('name', '')
                if name == 'move_piece':
                    move_piece_call = tool_entry
                else:
                    other_calls.append(tool_entry)

            # 如果 move_piece 被调用，提取坐标并结束
            if move_piece_call is not None:
                fc, tc = self._extract_move_from_call(move_piece_call)
                if fc and tc:
                    return fc, tc, self._build_full_text()
                # move_piece 参数无效，继续

            # ── 执行其他工具调用 ──
            if other_calls:
                # 将 assistant message（仅含已执行工具）加入历史
                # 若有无效 move_piece 调用，排除它以保持历史一致
                clean_message = dict(message)
                clean_calls = [t for t in tool_calls if t in other_calls]
                clean_message['tool_calls'] = clean_calls
                messages.append(clean_message)

                for tool_entry in other_calls:
                    func = tool_entry.get('function', {})
                    name = func.get('name', '')
                    try:
                        args = json.loads(func.get('arguments', '{}'))
                    except json.JSONDecodeError:
                        args = {}

                    result = self._execute_tool(name, args)
                    self._all_texts.append(f"[Tool: {name}]\n{result}")
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tool_entry.get('id', ''),
                        'content': result,
                    })
                # 继续下一轮，让 LLM 基于工具结果做决策
                continue

            # tool_calls 存在但没有 move_piece 也没有其他已知工具 → 尝试解析
            for tool_entry in tool_calls:
                fc2, tc2 = self._extract_move_from_call(tool_entry)
                if fc2 and tc2:
                    return fc2, tc2, self._build_full_text()
            break

        # ── 所有轮次结束，最后一次尝试文本解析 ──
        full = self._build_full_text()
        fc, tc = parse_coordinates_from_text(full)
        if fc and tc:
            return fc, tc, full
        return '', '', f'错误: {MAX_TOOL_TURNS} 轮工具调用后未找到有效走法'

    # ── 工具执行 ──

    def _execute_tool(self, name: str, args: dict) -> str:
        """在本地执行 AI 工具调用，返回 JSON 结果字符串。"""
        if self.game is None:
            return json.dumps({'error': '游戏状态不可用，无法执行工具调用'},
                             ensure_ascii=False)

        if name == 'search_best_move':
            return self._run_search(args)
        elif name == 'evaluate_position':
            return self._run_evaluate()
        else:
            return json.dumps({'error': f'未知工具: {name}'}, ensure_ascii=False)

    def _run_search(self, args: dict) -> str:
        """执行 Alpha-Beta 搜索，返回引擎最佳走法及快速评估的候选列表。

        使用两步策略：①Alpha-Beta 深搜索找最佳走法；
        ②对所有合法走法用快速静态评估（~0.05ms/步）排序，返回 top-N。
        """
        from domain.search import SearchEngine

        depth = min(max(args.get('depth', 3), 2), 5)
        top_n = min(max(args.get('top_n', 3), 1), 5)

        try:
            # 主搜索 — 用临时 Game 对象隔离，不修改 self.game.board
            board_copy = [row[:] for row in self.game.board]
            engine = SearchEngine(
                max_depth=depth,
                time_limit=min(15.0, 2.0 + depth * 3.0),
            )
            # 创建临时 Game，避免替换 self.game.board（防止 Pikafish 快照读到修改中棋盘）
            from domain.game import ChineseChessGame
            tmp_game = ChineseChessGame()
            tmp_game.board = board_copy
            tmp_game.current_player = self.current_player
            best_move = engine.search(tmp_game, self.current_player)

            if not best_move:
                return json.dumps({'error': '未找到合法走法'}, ensure_ascii=False)

            board = board_copy  # 用副本做评分，不碰 live board
            player = self.current_player
            opponent = 3 - player

            # 对所有走法用增强评估排序（MVV-LVA + 局面分 + 将军检测）
            all_moves = self.game.get_all_legal_moves(player)
            scored = []
            for fr, fc, tr, tc in all_moves:
                piece = board[fr][fc]
                captured = SearchEngine._make_move(board, fr, fc, tr, tc)
                # 静态局面分
                s = evaluate(board)
                # MVV-LVA 吃子加分
                if captured != '.':
                    s += PIECE_VALUE.get(captured.upper(), 0) * 10 - PIECE_VALUE.get(piece.upper(), 0)
                # 将军奖励：走子后对方被将军额外加分
                # 注意：_is_in_check 内部使用 self.game.board，
                # 因此需临时指向已应用候选走法的 board_copy
                self.game.board = board
                try:
                    if self.game._is_in_check(opponent):
                        s += 50.0 if player == 1 else -50.0
                finally:
                    self.game.board = original_board
                SearchEngine._unmake_move(board, fr, fc, tr, tc, captured)
                # 归一化：将评分转为"越高=对当前玩家越有利"
                if player == 2:
                    s = -s
                scored.append((fr, fc, tr, tc, s))

            # 归一化后高分=好棋 → 始终降序排列（最佳走法排最前）
            scored.sort(key=lambda x: x[4], reverse=True)
            top_moves = scored[:top_n]
            best_score = engine.best_score

            # 格式化
            lines = []
            lines.append(f"Alpha-Beta 搜索完成（深度={depth}，{engine.nodes_searched} 节点）")
            lines.append(f"引擎最佳评分: {best_score:+.0f}（正值=红优，负值=黑优）")
            lines.append("")

            # 引擎最佳走法高亮
            bfr, bfc, btr, btc = best_move
            bp = board[bfr][bfc]
            bpn = PIECE_SYMBOLS.get(bp, bp)
            bc = board[btr][btc]
            bcap = f" 吃{PIECE_SYMBOLS.get(bc, bc)}" if bc != '.' else ''
            lines.append(f"★ 引擎首选: {bpn} {chr(65+bfc)}{bfr+1}→{chr(65+btc)}{btr+1}{bcap}")
            lines.append("")

            lines.append(f"候选走法 Top-{len(top_moves)}（静态评估排序）：")
            for i, (fr, fc, tr, tc, s) in enumerate(top_moves, 1):
                piece = board[fr][fc]
                piece_name = PIECE_SYMBOLS.get(piece, piece)
                from_c = f"{chr(65+fc)}{fr+1}"
                to_c = f"{chr(65+tc)}{tr+1}"
                captured = board[tr][tc]
                cap_info = ''
                if captured != '.':
                    cap_name = PIECE_SYMBOLS.get(captured, captured)
                    cap_info = f" 吃{cap_name}"
                marker = ' ← 引擎首选' if (fr, fc, tr, tc) == best_move else ''
                lines.append(f"  {i}. [{s:+.0f}] {piece_name} {from_c}→{to_c}{cap_info}{marker}")

            lines.append("")
            lines.append("请综合考虑引擎建议和你的战略判断，选择最优走法。最终调用 move_piece 提交。")
            return json.dumps({'result': '\n'.join(lines)}, ensure_ascii=False)

        except Exception as e:
            return json.dumps({'error': f'搜索失败: {e}'}, ensure_ascii=False)

    def _run_evaluate(self) -> str:
        """执行静态局面评估"""
        try:
            board = self.game.board
            red_moves = self.game.get_all_legal_moves(1)
            black_moves = self.game.get_all_legal_moves(2)
            red_check = self.game._is_in_check(1)
            black_check = self.game._is_in_check(2)

            total_pieces = sum(1 for r in range(BOARD_HEIGHT)
                              for c in range(BOARD_WIDTH) if board[r][c] != '.')
            endgame = total_pieces <= 14

            score = evaluate(
                board,
                legal_moves_red=len(red_moves),
                legal_moves_black=len(black_moves),
                red_in_check=red_check,
                black_in_check=black_check,
                endgame=endgame,
            )

            # 统计子力
            red_material = 0
            black_material = 0
            for r in range(BOARD_HEIGHT):
                for c in range(BOARD_WIDTH):
                    p = board[r][c]
                    if p == '.':
                        continue
                    if p.isupper():
                        red_material += PIECE_VALUE.get(p, 0)
                    else:
                        black_material += PIECE_VALUE.get(p.upper(), 0)

            lines = []
            lines.append(f"局面评估: {score:+.0f}（正值=红优，负值=黑优）")
            lines.append(f"红方子力: {red_material}  |  黑方子力: {black_material}")
            material_diff = red_material - black_material
            if material_diff > 0:
                lines.append(f"红方多子 +{material_diff}")
            elif material_diff < 0:
                lines.append(f"黑方多子 +{-material_diff}")
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
                r'.*[。！？!?\n]',
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
            except json.JSONDecodeError:
                return '', ''
        else:
            args = args_data
        return args.get('from', ''), args.get('to', '')

    # ── API 请求 ──

    def _is_lmstudio(self) -> bool:
        return self.model_info.type == 'lmstudio'

    def cancel(self) -> None:
        self._cancelled.set()
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
        is_lmstudio = self._is_lmstudio()
        if self.model_info.tools_choice:
            if is_lmstudio:
                payload['tool_choice'] = 'required'
            else:
                payload['tool_choice'] = self.model_info.tools_choice
        if self.think is not None and is_lmstudio:
            payload['chat_template_kwargs'] = {"enable_thinking": self.think}
        if self.think is not None and not is_lmstudio and self.model_info.type == 'deepseek':
            payload['think'] = self.think
        payload.update(self.model_info.options)
        return payload

    def _build_user_content(self):
        """构建用户消息内容"""
        if self.image_base64:
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
                timeout=(self.timeout, self.timeout))
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout:
            raise Exception("超时: API 请求超时")
        except requests.exceptions.ConnectionError as e:
            raise Exception(f"连接错误: API 连接失败: {e}")
        except requests.exceptions.HTTPError as e:
            error_detail = ''
            try:
                error_detail = resp.text if resp is not None else '无响应'
            except Exception:
                pass
            status = resp.status_code if resp is not None else 0
            if 400 <= status < 500:
                raise Exception(f"客户端错误: API 请求失败: {e}\n响应: {error_detail}")
            raise Exception(f"服务器错误: API 请求失败: {e}\n响应: {error_detail}")
        except requests.exceptions.RequestException as e:
            raise Exception(f"网络错误: API 请求异常: {e}")
