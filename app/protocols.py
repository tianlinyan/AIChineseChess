from typing import Protocol, Any


class MainWindowProtocol(Protocol):
    """MainWindow 暴露给 GameController 的接口（结构类型）"""
    model_manager: Any
    model1_combo: Any
    model2_combo: Any
    start_btn: Any
    pause_btn: Any
    reset_btn: Any
    think_log: Any
    game_status_label: Any
    turn_label: Any
    think_timer_label: Any
    disable_think_check: Any
    think_check: Any
    vision_check: Any

    board_widget: Any
    game: Any

    def update_ui(self) -> None: ...
    def update_game_status(self) -> None: ...
    def update_history_list(self) -> None: ...
    def update_player_status(self) -> None: ...
    def start_thinking_timer(self, player: int) -> None: ...
    def stop_thinking_timer(self) -> None: ...
    def pause_thinking_timer(self) -> None: ...
    def resume_thinking_timer(self) -> None: ...
