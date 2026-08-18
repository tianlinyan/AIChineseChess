import json
import os
import re
import sys
import threading
import time
from typing import Optional

import requests
from PyQt6.QtCore import QObject, pyqtSignal

from domain.models import ModelInfo
from ai.parser import parse_coordinates_from_text
from domain.constants import (
    AI_TIMEOUT_SECONDS, AI_CONNECT_TIMEOUT,
    PIECE_SYMBOLS, parse_coord, format_coord, format_move,
    format_chinese_notation,
    ENDGAME_PIECE_THRESHOLD,
    MCTS_FALLBACK_TIME_LIMIT,
)
from domain.prompts import DEFAULT_TOOLS
from domain.evaluation import evaluate, compute_material, PIECE_VALUE
from domain.mcts import MCTSEngine, make_move, unmake_move
from domain.game import ChineseChessGame


# ══════════════════════════════════════════════════════════════════════════════
# 多轮工具调用配置
# ══════════════════════════════════════════════════════════════════════════════

MAX_TOOL_TURNS = 4  # 每回合最多工具调用轮数（防止死循环）

# 深度 → MCTS 模拟次数映射（与 app/engine_bridge 的 _DEPTH_SIMS_MAP 一致；
# ai 层不得 import app 层，依赖方向约束，故本地留存一份）
_MCTS_DEPTH_SIMS_MAP = {1: 500, 2: 800, 3: 1200, 4: 1600,
                        5: 2000, 6: 3000, 7: 4000, 8: 5000}

# 仲裁文本中的候选标签：'候选 A' / '选择 B' / '选A' / '采纳 A' 等
# （[AB]\b 的 \b 保证不误匹配 'A1' 这类坐标里的列字母）
_ARB_LABEL_RE = re.compile(
    r'(?:候选|选(?:择|用|取)?|采纳|同意|取)\s*([AB])\b', re.IGNORECASE)

# ── 诊断开关（仅定位用，不影响任何逻辑）──
# 设置环境变量 AI_DEBUG=1 后，每次 LLM 请求/响应/工具校验的关键结构
# 会打印到 stderr，用于排查"工具调用不可靠"的真实环节（请求结构？
# 响应结构？坐标校验？）。生产默认关闭。
_AI_DEBUG = os.environ.get('AI_DEBUG') == '1'


def _debug_log(msg: str) -> None:
    if _AI_DEBUG:
        print(f'[AI-DEBUG] {msg}', file=sys.stderr, flush=True)


def _clamp_int(value, lo: int, hi: int) -> int:
    """将参数钳制到 [lo, hi]，非数值类型抛 TypeError/ValueError。

    弱模型可能传字符串/None/bool（如 {"depth": "4"}），max/min 对这些
    值抛 TypeError 且无法被下游恢复——此处集中校验并给出可恢复的异常。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f'参数应为整数，实际为 {type(value).__name__}')
    return max(lo, min(hi, int(value)))


def _tool_err(msg: str) -> str:
    """工具结果的标准错误 JSON（ensure_ascii=False，输出逐字节与旧版一致）。"""
    return json.dumps({'error': msg}, ensure_ascii=False)


class AIWorkerSignals(QObject):
    finished = pyqtSignal(str, str, str, str, int, int, str)
    # from_coord, to_coord, full_text, error, version,
    # cancel_version, content_text（正式回复，不含推理/工具结果）


class AIWorker:
    """LLM 请求工作器 — 由 controller 在裸 threading.Thread 中运行。

    用裸线程（而非 QRunnable/QThreadPool）是为了 cancel() 时能直接
    中断 requests.Session。
    """
    def __init__(self, model_info: ModelInfo, prompt: str,
                 image_base64: Optional[str] = None,
                 version: int = 0,
                 cancel_version: int = 0,
                 system_prompt: str = '',
                 tools: Optional[tuple] = None,
                 timeout: int = AI_TIMEOUT_SECONDS,
                 # 每步总预算（秒，跨 MAX_TOOL_TURNS 轮共享）。
                 # None → timeout × 2：正常思考（单轮 < timeout）不受影响，
                 # 挂起场景把最坏 4×timeout 收紧到 2×timeout，避免端点
                 # 无响应时整步卡死过久。仲裁（timeout=180）→ 360s。
                 total_timeout: Optional[float] = None,
                 # ── 工具执行所需 ──
                 game: Optional[ChineseChessGame] = None,
                 current_player: int = 0,
                 # Pikafish 引擎（evaluate_position 工具的大师级评估源；
                 # 由 controller 注入，None → 回退手工评估）
                 pikafish: Optional['PikafishEngine'] = None,
                 # ── 仲裁专用：候选走法集合 + A/B 标签映射 ──
                 # 实证：qwen3.8 思考模式（reasoning 5000+ 字符）下 required
                 # 失效，输出 0 tool_calls + 空 content，坐标淹没在推理文本。
                 # 仲裁本质是二选一——模型只需表达"选 A/B"，无需生成坐标。
                 # allowed_moves 限死候选；label_to_move 供"候选 A/B"标签解析
                 # （controller 以 candidate_order 传入提示词顺序并同步构建）。
                 allowed_moves: Optional[set] = None,
                 label_to_move: Optional[dict] = None) -> None:
        self.model_info = model_info
        self.prompt = prompt
        self.image_base64 = image_base64
        self.version = version
        self.cancel_version = cancel_version
        self.system_prompt = system_prompt
        self.tools = tools if tools is not None else DEFAULT_TOOLS
        self.timeout = timeout
        self.total_timeout = (total_timeout if total_timeout is not None
                              else timeout * 2.0)
        # 本步计时起点（run() 开头重设；此处兜底防 _send_request 先于 run）
        self._turn_start = time.time()
        self.game = game
        self.current_player = current_player
        self.pikafish = pikafish
        self.allowed_moves = allowed_moves
        self.label_to_move = label_to_move
        self.signals = AIWorkerSignals()
        self._cancelled = threading.Event()
        self._session: Optional[requests.Session] = None

        # 收集所有工具调用产生的文本
        self._all_texts: list = []
        # 仅存正式 content（不含 reasoning_content / 工具结果）——
        # 兜底文本解析只用此池，排除 DeepSeek 推理文本里"讨论过但未选中"
        # 的坐标干扰（M-AI-5）
        self._content_texts: list = []
        # 最近一次 _validate_move 的合法走法缓存（供 move_error 附示例）
        self._legal_cache: Optional[list] = None
        # 在途搜索对象（PikafishEngine，有 .stop()；cancel() 调用以中止，
        # M-AI-1）
        self._active_search_engine = None
        # search_best_move 每步最多一次（系统提示词约定，此处强制）
        self._search_used = False
        # evaluate_position 每步最多两次（防弱模型刷满轮次导致无走法）
        self._evaluate_count = 0

    def run(self) -> None:
        self._turn_start = time.time()  # 每步总预算计时起点
        self._session = requests.Session()
        self._session.trust_env = False
        try:
            from_coord, to_coord, full_text = self._agentic_loop(self._session)
            error = ''
            if (not from_coord or not to_coord) and 'ERROR:' in full_text:
                error = full_text
                full_text = ''
            # content_text = 正式回复（不含推理/工具结果），供日志"只显示正式回复"
            content_text = '\n\n'.join(t for t in self._content_texts if t)
            self.signals.finished.emit(
                from_coord, to_coord, full_text, error,
                self.version, self.cancel_version, content_text)
        except Exception as e:
            self.signals.finished.emit(
                '', '', '', str(e),
                self.version, self.cancel_version, '')
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
            _debug_log(
                f'轮次{turn + 1} 请求: model={payload.get("model")} '
                f'tools={[t.get("function", {}).get("name") for t in payload.get("tools", [])]} '
                f'tool_choice={payload.get("tool_choice")} '
                f'messages={len(messages)}条 '
                f'最后一条={str(messages[-1])[:200]}')
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
            tool_calls = message.get('tool_calls', []) or []
            _debug_log(
                f'轮次{turn + 1} 响应: message keys={list(message.keys())} '
                f'content长度={len(content)} reasoning长度={len(reasoning)} '
                f'tool_calls={len(tool_calls)}个 '
                + ('首个tool_call=' + str(tool_calls[0])[:300] if tool_calls else ''))

            # 收集文本：_all_texts 用于展示日志（含推理/工具结果）；
            # _content_texts 仅存正式 content，供兜底文本解析（M-AI-5）
            if reasoning:
                self._all_texts.append(reasoning)
            if content:
                self._all_texts.append(content)
                self._content_texts.append(content)

            # ── 提取 tool_calls（192 行已取；此处直接使用）──
            if not tool_calls:
                # 无 tool_calls → 跳出循环，由循环后统一做文本解析兜底
                # （轮内解析与循环后解析输入相同、结果逐字节一致，
                # 只保留一份实现，见 _agentic_loop 末尾）
                break

            # 纯文本任务（AI点评，tools=()）：payload 未声明任何工具，
            # 模型若仍返回 tool_calls 也无工具可执行——直接结束循环，
            # 由循环后逻辑用已收集的正式文本作为结果（不参与走法解析），
            # 避免烧满 MAX_TOOL_TURNS 后误报"未找到有效走法"
            if not self.tools:
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
                    # 附合法走法坐标示例：弱模型（qwen3.8）被引擎参考诱导时
                    # 常提交非法坐标，给出可复制示例能显著提高下一轮改正率
                    sample = ''
                    if self._legal_cache:
                        sample = '、'.join(
                            format_move(r, c, tr, tc2)
                            for r, c, tr, tc2 in self._legal_cache[:5])
                    move_error = (f'走法 {fc}→{tc} 不合法，请从合法走法列表'
                                  f'中选择一步（可选示例：{sample}）。'
                                  if sample else
                                  f'走法 {fc}→{tc} 不合法，请从合法走法列表中选择')
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
                        f"[Tool: move_piece]\n{_tool_err(move_error)}")
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': move_piece_call.get('id', ''),
                        'content': _tool_err(move_error),
                    })
                # 继续下一轮，让 LLM 基于工具结果（含错误）做决策
                continue

        # ── 所有轮次结束 ──
        full = self._build_full_text()
        # 纯文本任务（AI点评，tools=()）：不需要走法。坐标解析/合法性
        # 校验/仲裁兜底都只服务于走子任务——解说 worker 不传 game，
        # _validate_move 恒为 None，旧逻辑会把"解说文本里引用的坐标"
        # 全部判非法，最后误报"未找到有效走法"（且解说文本明明已生成）。
        # 正式回复文本即结果；仅当模型完全没返回文本才报真实错误，
        # 由 controller 记 WARNING 并继续对弈。
        if not self.tools:
            text = '\n\n'.join(t for t in self._content_texts if t).strip()
            if not text:
                return '', '', 'ERROR: 模型未返回解说文本'
            return '', '', text
        # 走子任务：最后一次尝试文本解析。
        # 与下方推理文本循环同一校验口径（M-AI-7）：content 里讨论过但
        # 未选中的坐标（如引用引擎推荐/示例）必须经 _validate_move 过滤，
        # 防止把非法或非最终选择的坐标当成走法提交。
        fc, tc = parse_coordinates_from_text('\n'.join(self._content_texts))
        if fc and tc and self._validate_move(fc, tc) is not None:
            return fc, tc, full
        # 仲裁场景：从全文（含 reasoning_content）解析 '候选 A/B' 标签或
        # 候选坐标字面量（模型只需表达选了哪个候选，无需生成坐标）
        arb_move = self._resolve_arbitration_choice()
        if arb_move:
            return self._arb_return(arb_move, full)
        # 4 轮耗尽且 content 无坐标：qwen3.8 等思考型模型把最终决定写在
        # reasoning_content。取最后一条"模型文本"（排除 [Tool: ...] 工具
        # 结果，其含引擎推荐/错误反馈坐标会污染解析），且必须通过
        # _validate_move 合法性校验才采用——防止提交非法走法。
        for text in reversed(self._all_texts):
            if text.lstrip().startswith('[Tool:'):
                continue
            fc, tc = parse_coordinates_from_text(text)
            if fc and tc and self._validate_move(fc, tc) is not None:
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
            _debug_log(f'坐标解析失败: from={from_coord!r} to={to_coord!r}')
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
        legal = tmp.get_all_legal_moves(self.current_player)
        self._legal_cache = legal  # 供 move_error 附合法示例用
        if move not in legal:
            _debug_log(
                f'走法被拒: {from_coord}→{to_coord} 不在 {len(legal)} 个合法走法中'
                f'（前5个={[str(m) for m in legal[:5]]}）')
            return None
        return move

    def _resolve_arbitration_choice(self) -> Optional[tuple]:
        """仲裁专用兜底：从模型文本中解析所选候选。

        仅当 allowed_moves 非空（仲裁 worker）时启用。两种策略：
        1. '候选 A/B' 标签 → label_to_move 映射（controller 知道
           build_arbitration_prompt 随机化后的 A/B 各对应哪步）；
        2. 候选坐标字面量（如 'H10→G8'）→ 文本中最后出现的候选。

        content 为空（qwen3.8 思考模式把分析写在 reasoning_content）时
        回退到含推理的模型文本（排除 [Tool: ...] 工具结果），白名单
        保证只匹配候选，不受"讨论过但未选中"坐标干扰。
        """
        if not self.allowed_moves:
            return None
        # 文本池：正式 content 优先；content 全空时用含推理的模型文本
        texts = ['\n'.join(self._content_texts)]
        if not self._content_texts:
            non_tool = [t for t in self._all_texts
                        if not t.lstrip().startswith('[Tool:')]
            if non_tool:
                texts.append('\n'.join(non_tool))
        for text in texts:
            if not text.strip():
                continue
            # 策略 1：标签 → 候选（取最后一次出现的标签 = 最终决定）
            if self.label_to_move:
                matches = list(_ARB_LABEL_RE.finditer(text))
                if matches:
                    label = matches[-1].group(1).upper()
                    mv = self.label_to_move.get(label)
                    if mv:
                        return mv
            # 策略 2：候选坐标字面量（允许多种分隔符，取最后出现）
            best = None
            best_pos = -1
            for mv in self.allowed_moves:
                fr, fc, tr, tc = mv
                frm = format_coord(fr, fc)
                tos = format_coord(tr, tc)
                pat = re.compile(
                    re.escape(frm) + r'\s*(?:→|->|至|\.\.|\.|,|，|—|-)?\s*'
                    + re.escape(tos))
                for m in pat.finditer(text):
                    if m.start() > best_pos:
                        best_pos = m.start()
                        best = mv
            if best is not None:
                return best
        return None

    def _arb_return(self, arb_move: tuple,
                    full_text: Optional[str] = None) -> tuple:
        """仲裁兜底命中 → 返回 (from_coord, to_coord, full_text)。"""
        fr, fc, tr, tc = arb_move
        if full_text is None:
            full_text = self._build_full_text()
        return format_coord(fr, fc), format_coord(tr, tc), full_text

    def _execute_tool(self, name: str, args: dict) -> str:
        """在本地执行 AI 工具调用，返回 JSON 结果字符串。"""
        if self.game is None:
            return _tool_err('游戏状态不可用，无法执行工具调用')

        if name == 'search_best_move':
            if self._search_used:
                return _tool_err('本回合已调用过 search_best_move（每步最多一次），请基于已有信息选择走法')
            self._search_used = True
            return self._run_search(args)
        elif name == 'evaluate_position':
            if self._evaluate_count >= 2:
                return _tool_err('本回合已调用过 2 次 evaluate_position（每步最多 2 次），请基于已有信息选择走法')
            self._evaluate_count += 1
            return self._run_evaluate()
        else:
            return _tool_err(f'未知工具: {name}')

    def _run_search(self, args: dict) -> str:
        """执行深度搜索，返回搜索最佳走法及候选列表。

        优先 Pikafish 深搜（MultiPV 主变，大师级战术）——LLM 拿到的战术
        参考与引擎决策同源；Pikafish 缺失/未初始化/搜索失败时由 MCTS
        兜底（原本地 Alpha-Beta 回退路径已随 domain/search.py 移除）。
        """
        try:
            # 参数类型防线：弱模型可能传字符串/None，钳制必须在 try 内，
            # 否则 TypeError 冒泡到 run() 兜底 → 整步失败且模型得不到可恢复错误
            depth = _clamp_int(args.get('depth', 3), 2, 8)
            top_n = _clamp_int(args.get('top_n', 3), 1, 5)
        except (TypeError, ValueError):
            return _tool_err('参数类型错误：depth 应为 2~8、top_n 应为 1~5 的整数')

        try:
            # 用快照隔离，不修改 self.game.board
            tmp_game = self.game.snapshot()
            tmp_game.current_player = self.current_player

            if self.pikafish is not None:
                result = self._run_search_pikafish(tmp_game, depth, top_n)
                if result is not None:
                    return result
                # Pikafish 失败（锁超时/引擎故障）。若已被 cancel（暂停/
                # 重置），直接返回取消错误，避免陈旧 worker 继续空转
                if self._cancelled.is_set():
                    return _tool_err('任务已取消')
            # Pikafish 缺失或搜索失败 → MCTS 兜底
            return self._run_search_mcts(tmp_game, depth, top_n)
        except Exception as e:
            return _tool_err(f'搜索失败: {e}')

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
            # MultiPV 候选：走子方视角评分 → 统一红方视角（与其余工具口径一致）；
            # top_moves 只保留走法元组（_format_search_result 统一按 entry[:4] 解包）
            top_moves = []
            for mv, sc in raw_top[:top_n]:
                top_moves.append(mv)
            best_score = (raw_top[0][1] if player == 1 else -raw_top[0][1]) if raw_top else 0.0

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
            return self._format_search_result(
                board, best_move, top_moves, lines,
                f"候选走法 Top-{len(top_moves)}（MultiPV 主变，按引擎评分排序）：")
        except Exception:
            return None
        finally:
            self._active_search_engine = None

    def _run_search_mcts(self, tmp_game, depth: int, top_n: int) -> str:
        """兜底路径：MCTS 深搜（PUCT）+ 全走法静态评估排序。

        用于 Pikafish 缺失/未初始化/搜索失败时。走法由 MCTS 确定，
        候选按静态评估排序（吃大子、将军优先）；评分统一红方视角，
        与 evaluate_position 工具口径一致。
        """
        player = self.current_player
        opponent = 3 - player
        try:
            engine = MCTSEngine(
                max_simulations=_MCTS_DEPTH_SIMS_MAP.get(depth, 1200),
                time_limit=MCTS_FALLBACK_TIME_LIMIT,
            )
            # 注册在途引擎：cancel() 调 engine.stop() 尽快中止搜索（M-AI-1）
            self._active_search_engine = engine
            best_move = engine.search(tmp_game, player)

            if not best_move:
                return _tool_err('未找到合法走法')

            board = tmp_game.board  # 快照棋盘，不碰 live board

            # 对所有走法用增强评估排序（MVV-LVA + 局面分 + 将军检测），
            # 评分统一红方视角（正值=红优，负值=黑优）
            all_moves = tmp_game.get_all_legal_moves(player)
            scored = []
            for fr, fc, tr, tc in all_moves:
                piece = board[fr][fc]
                captured = make_move(tmp_game, fr, fc, tr, tc)
                # 静态局面分（红方视角）
                s = evaluate(board)
                # 将军奖励：走子后对方被将军额外加分
                if tmp_game.is_in_check(opponent):
                    s += 50.0 if player == 1 else -50.0
                unmake_move(tmp_game, fr, fc, tr, tc, captured)
                # MVV-LVA 吃子加分（红方视角）：红吃子加分，黑吃子减分
                if captured != '.':
                    bonus = (PIECE_VALUE.get(captured.upper(), 0) * 10
                             - PIECE_VALUE.get(piece.upper(), 0))
                    s += bonus if player == 1 else -bonus
                scored.append((fr, fc, tr, tc, s))

            # 红方视角排序：红方走棋高分在前，黑方走棋低分在前
            scored.sort(key=lambda x: x[4], reverse=(player == 1))
            top_moves = scored[:top_n]
            # 首选走法的静态参考分（红方视角）
            best_score = 0.0
            for fr2, fc2, tr2, tc2, s2 in scored:
                if (fr2, fc2, tr2, tc2) == best_move:
                    best_score = s2
                    break

            # 格式化（"★ 搜索首选 + Top-N" 与 Pikafish 路径共用）
            lines = []
            lines.append(f"MCTS 搜索完成（depth={depth}，"
                         f"{engine.simulations} 次模拟）")
            lines.append(f"首选走法参考评分: {best_score:+.0f}"
                         f"（正值=红优，负值=黑优，静态评估口径）")
            lines.append("")
            return self._format_search_result(
                board, best_move, top_moves, lines,
                f"候选走法 Top-{len(top_moves)}（MCTS 首选 + 静态评估排序："
                f"吃大子、将军优先，其后按局面评估）：")

        finally:
            # 无论成功/异常/被 cancel 中止，都解除在途引擎引用
            self._active_search_engine = None

    def _format_search_result(self, board: list, best_move: tuple,
                              top_moves: list, head_lines: list,
                              candidates_title: str) -> str:
        """格式化"★ 搜索首选 + 候选 Top-N"结果（Pikafish/MCTS 两条路径共用）。

        top_moves 元素可为 (move_tuple, ...) 或 (fr, fc, tr, tc, ...)，
        统一取前 4 个为坐标；head_lines 为引擎专属头部行（调用方负责
        补末尾空行分隔）。输出逐字节与旧实现一致。
        """
        lines = list(head_lines)
        bfr, bfc, btr, btc = best_move
        bc = board[btr][btc]
        bcap = f" 吃{PIECE_SYMBOLS.get(bc, bc)}" if bc != '.' else ''
        lines.append(f"★ 搜索首选: "
                     f"{format_chinese_notation(board, bfr, bfc, btr, btc)}"
                     f"({format_move(bfr, bfc, btr, btc)}){bcap}")
        lines.append("")

        lines.append(candidates_title)
        for i, entry in enumerate(top_moves, 1):
            fr, fc, tr, tc = entry[:4]
            cap = board[tr][tc]
            cap_info = f" 吃{PIECE_SYMBOLS.get(cap, cap)}" if cap != '.' else ''
            marker = ' ← 搜索首选' if (fr, fc, tr, tc) == best_move else ''
            lines.append(f"  {i}. "
                         f"{format_chinese_notation(board, fr, fc, tr, tc)}"
                         f"({format_move(fr, fc, tr, tc)}){cap_info}{marker}")

        lines.append("")
        lines.append("请综合考虑搜索建议和你的战略判断，选择最优走法。最终调用 move_piece 提交。")
        return json.dumps({'result': '\n'.join(lines)}, ensure_ascii=False)

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

            total_pieces = (tmp_game._red_piece_count
                            + tmp_game._black_piece_count)
            endgame = total_pieces <= ENDGAME_PIECE_THRESHOLD

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
            return _tool_err(f'评估失败: {e}')

    # ── 辅助 ──

    def _build_full_text(self) -> str:
        # 汇总 LLM 推理/正式回复/工具结果，思考日志完整展示，不截断
        parts = [t for t in self._all_texts if t]
        return '\n\n'.join(parts)

    def _extract_move_from_call(self, tool_call: dict) -> tuple:
        """从单个 tool_call 提取 move_piece 坐标"""
        # `or {}` 而非默认值：键存在但值为 null 时 .get 默认值不生效
        func = tool_call.get('function') or {}
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

    def cancel(self) -> None:
        self._cancelled.set()
        # 中止在途搜索（M-AI-1）：Pikafish 的 stop() 发 UCI stop，MCTS 的
        # stop() 置 _stop_flag，两者都让搜索主循环尽快退出（毫秒级响应）。
        # 属性读写跨线程，GIL 下原子。
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
        }
        # 纯文本任务（如 AI点评，tools=()）不发送 tools 字段：
        # 部分端点不接受空数组；无 tools 时发 tool_choice 也会 400
        if self.tools:
            payload['tools'] = list(self.tools)
        # 不设 max_tokens（旧版行为）：qwen3.8 思考模式推理可达 5000+
        # 字符，任何输出上限都会先耗尽在推理上，导致响应末尾的
        # tool_calls 被截断（0 tool_calls + 空 content → "4 轮工具调用后
        # 未找到有效走法"）。models.json 的 options 仍可对个别模型显式
        # 覆盖 max_tokens。
        is_llama_server = self.model_info.type == 'llama-server'
        if self.model_info.tools_choice and self.tools:
            if is_llama_server:
                payload['tool_choice'] = 'required'
            else:
                payload['tool_choice'] = self.model_info.tools_choice
        # 不传递 think / enable_thinking 参数：think 模式取消，
        # 由 llama-server / DeepSeek 按其默认行为处理
        # options 仅用于附加参数（temperature / max_tokens 等）：
        # 排除程序逻辑构造的键，防止 models.json 误覆盖 payload
        reserved = ('model', 'messages', 'stream', 'tools', 'tool_choice')
        payload.update({k: v for k, v in self.model_info.options.items()
                        if k not in reserved})
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
        # 读取超时 = min(单轮超时, 剩余总预算)：总预算耗尽时整步失败，
        # 防止 4 轮 × 单轮超时 的最坏挂起（端点无响应时卡死过久）
        remaining = self.total_timeout - (time.time() - self._turn_start)
        if remaining <= 0:
            raise Exception("超时: 本步总预算已耗尽")
        read_timeout = min(float(self.timeout), remaining)
        try:
            resp = session.post(
                self.model_info.endpoint, json=payload, headers=headers,
                # 连接/读取超时分离：端点黑洞时连接阶段快速失败，
                # 读取阶段仍允许长思考（每轮请求独立计时）
                timeout=(AI_CONNECT_TIMEOUT, read_timeout))
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
