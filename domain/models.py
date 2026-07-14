from dataclasses import dataclass


@dataclass
class ModelInfo:
    """AI 模型配置 — 纯数据对象，无依赖。"""
    id: str = ''
    name: str = ''
    type: str = ''
    endpoint: str = ''
    model: str = ''
    api_key: str = ''
    tools_choice: str = 'auto'
    system_prompt: str = ''
    options: dict = None

    def __post_init__(self):
        if self.options is None:
            self.options = {}

    @classmethod
    def from_dict(cls, data: dict) -> 'ModelInfo':
        return cls(
            id=data.get('id', ''),
            name=data.get('name', ''),
            type=data.get('type', ''),
            endpoint=data.get('endpoint', ''),
            model=data.get('model', ''),
            api_key=data.get('api_key', ''),
            tools_choice=data.get('tools_choice', 'auto'),
            system_prompt=data.get('system_prompt', ''),
            options=dict(data.get('options', {})),
        )
