import json
import os
import re
import sys
from typing import Callable, Optional

from domain.models import ModelInfo

# 匹配 ${VAR_NAME} 格式的环境变量引用
_ENV_VAR_PATTERN = re.compile(r'\$\{(\w+)\}')


def _resolve_env_vars(value: str, warned_vars: set = None) -> str:
    """将字符串中的 ${VAR_NAME} 替换为对应的环境变量值。

    若环境变量未设置，记录缺失变量名到 warned_vars（去重），
    由调用方统一输出一次警告。
    """
    if not isinstance(value, str) or '${' not in value:
        return value
    if warned_vars is None:
        warned_vars = set()

    def _replace(match):
        var_name = match.group(1)
        env_val = os.environ.get(var_name, '')
        if not env_val:
            warned_vars.add(var_name)
        return env_val

    return _ENV_VAR_PATTERN.sub(_replace, value)


class ModelManager:
    """模型管理器 — 从 models.json 加载模型配置"""

    def __init__(self) -> None:
        self.models: list[ModelInfo] = []
        self.player1_models: list[ModelInfo] = []  # -p1 后缀
        self.player2_models: list[ModelInfo] = []  # -p2 后缀

    def load(self, models_path: Optional[str] = None,
             on_error: Optional[Callable] = None) -> None:
        if models_path is None:
            if getattr(sys, 'frozen', False):
                base = os.path.dirname(sys.executable)
            else:
                base = os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__)))
            models_path = os.path.join(base, 'models.json')

        try:
            with open(models_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                items = data.get('models', [])
            elif isinstance(data, list):
                items = data
            else:
                raise ValueError(
                    f"models.json 顶层应为对象或数组，实际为 {type(data).__name__}")
            if not isinstance(items, list):
                raise ValueError(
                    f"models.json 中 'models' 字段应为数组，实际为 {type(items).__name__}")
            self.models = [ModelInfo.from_dict(item) for item in items]

            # 解析 api_key 中的环境变量引用 ${VAR_NAME}（去重警告）
            missing_vars: set = set()
            for m in self.models:
                if m.api_key:  # 仅处理非空的 api_key
                    m.api_key = _resolve_env_vars(m.api_key, missing_vars)
            if missing_vars:
                var_list = "、".join(sorted(missing_vars))
                print(
                    f"[提示] 未检测到环境变量: {var_list}",
                    file=sys.stderr)
                print(
                    f"[提示] 请在终端中设置，例如:",
                    file=sys.stderr)
                for v in sorted(missing_vars):
                    print(f"       set {v}=你的密钥    (Windows CMD)", file=sys.stderr)
                    print(f"       $env:{v}='你的密钥'  (PowerShell)", file=sys.stderr)
                print(
                    f"[提示] 或创建 .env 文件，使用 python-dotenv 自动加载。",
                    file=sys.stderr)
                print(
                    f"[提示] 未设置将导致对应模型的 API 调用失败。",
                    file=sys.stderr)
                # 同步到 UI 日志：打包后无控制台，仅 stderr 用户不可见
                if on_error:
                    on_error(
                        f"未检测到环境变量: {var_list} — "
                        f"对应模型的 API 调用将失败（请设置环境变量或创建 .env）")

            # 按 -p1 / -p2 后缀分组；无后缀模型双方下拉框均可见
            # 仲裁裁判不参与对弈，排除出玩家下拉框
            common = [m for m in self.models
                      if not m.id.endswith('-p1')
                      and not m.id.endswith('-p2')
                      and m.id != 'arbitration']
            self.player1_models = common + [
                m for m in self.models if m.id.endswith('-p1')]
            self.player2_models = common + [
                m for m in self.models if m.id.endswith('-p2')]
            # 某侧无可用模型时回退到全量列表（保证下拉框非空）
            if not self.player1_models:
                self.player1_models = list(self.models)
            if not self.player2_models:
                self.player2_models = list(self.models)
        except (FileNotFoundError, json.JSONDecodeError,
                ValueError, TypeError, AttributeError) as e:
            msg = f"加载 models.json 失败: {e}"
            self.models = []
            self.player1_models = []
            self.player2_models = []
            if on_error:
                on_error(msg)
