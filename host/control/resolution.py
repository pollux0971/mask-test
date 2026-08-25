"""B18 — 解析度變更狀態機（CONTRACTS.md §1.2 / bridge_server.py 既有的
`do_switch_resolution()` 流程：editing → building → flashing → done|error）。

跟 `commands.py` 的三個執行期指令不一樣：ULD 驅動的 grid size **不能
執行期切換**——變更解析度是真的改 `main/vl53l7cx_test.c` 的
`TOF_RESOLUTION_MODE`、重新 `idf.py build`、重新 flash，中斷資料流數十秒。
`bridge_server.py` 的 docstring 講得很清楚，這裡照抄那個判斷：

    ULD 驅動的 grid size 不是執行期可切換的參數，一次解析度變更
    by design 就是一次重燒。

這支模組只管**狀態機本身**（合法的狀態轉移、目前卡在哪一步、能不能接受
新的變更請求）——真正的編譯/燒錄 I/O 是 `bridge_server.py`（`B19`）的事，
繼續呼叫 `idf.py`，這裡不重複那段 subprocess 邏輯。

**重燒期間 `$STATUS` 會重新出現、`seq` 會歸零。** `B03` 的掉幀偵測看到
`seq` 倒退會判定「重開機」，那是對的行為（§1.3：`seq` 以 `$STATUS` 為
session 邊界）——但前端不該把重燒期間的這個現象顯示成故障。
`ResolutionController.is_busy` 就是給前端／`B19` 用來抑制這個誤判的旗標：
`is_busy` 為真的時候看到 `seq` 歸零，代表的是「重燒中，預期行為」。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

VALID_RESOLUTIONS = (4, 8)


class ResolutionState(Enum):
    IDLE = "idle"
    EDITING = "editing"
    BUILDING = "building"
    FLASHING = "flashing"
    RESUMING = "resuming"
    ERROR = "error"


class InvalidResolutionError(ValueError):
    """`res` 不是 4 或 8。"""


class ResolutionChangeInProgressError(RuntimeError):
    """對應 HTTP 409：已經有一個解析度變更在進行中，跟既有 `_handle_switch()`
    對 `flashing.is_set()` 回 409 是同一個判斷，這裡把它做成明確的狀態機
    而不是一顆孤立的旗標。"""


@dataclass
class ResolutionController:
    """一個 bridge_server 行程對應一個實例。純狀態機，不做任何 I/O——
    `request_change()`／`advance_to_*()`／`complete()`／`fail()` 都只是
    合法性檢查 + 狀態轉移，真正的 build/flash 由呼叫端在對應階段執行。
    """

    state: ResolutionState = ResolutionState.IDLE
    current_resolution: int = 4
    pending_resolution: Optional[int] = None
    error_message: Optional[str] = None

    @property
    def is_busy(self) -> bool:
        """`True` 代表正在重燒流程中（不含 `ERROR`——卡在錯誤狀態不算
        「忙碌」，是需要人處理的狀態，見 `reset_error()`）。"""
        return self.state not in (ResolutionState.IDLE, ResolutionState.ERROR)

    def request_change(self, new_resolution: int) -> None:
        if new_resolution not in VALID_RESOLUTIONS:
            raise InvalidResolutionError(f"res 必須是 {VALID_RESOLUTIONS} 之一，收到 {new_resolution}")
        if self.is_busy:
            raise ResolutionChangeInProgressError(
                f"已經有一個解析度變更在進行中（狀態: {self.state.value}）"
            )
        if new_resolution == self.current_resolution and self.state != ResolutionState.ERROR:
            return  # 目標跟目前一樣，不需要重燒；不是錯誤，靜默成功
        self.pending_resolution = new_resolution
        self.state = ResolutionState.EDITING
        self.error_message = None

    def advance_to_building(self) -> None:
        self._require(ResolutionState.EDITING)
        self.state = ResolutionState.BUILDING

    def advance_to_flashing(self) -> None:
        self._require(ResolutionState.BUILDING)
        self.state = ResolutionState.FLASHING

    def advance_to_resuming(self) -> None:
        self._require(ResolutionState.FLASHING)
        self.state = ResolutionState.RESUMING

    def complete(self) -> None:
        self._require(ResolutionState.RESUMING)
        self.current_resolution = self.pending_resolution
        self.pending_resolution = None
        self.state = ResolutionState.IDLE

    def fail(self, message: str) -> None:
        """任何階段都可以失敗（build 失敗、flash 失敗、逾時），
        所以不檢查目前狀態——失敗本來就是打斷正常流程。"""
        self.error_message = message
        self.pending_resolution = None
        self.state = ResolutionState.ERROR

    def reset_error(self) -> None:
        """`ERROR` 之後要能重新接受請求，不然一次失敗就永久卡死整個功能。"""
        if self.state != ResolutionState.ERROR:
            raise ValueError(f"只能從 {ResolutionState.ERROR.value} 重設，目前在 {self.state.value}")
        self.state = ResolutionState.IDLE
        self.error_message = None

    def _require(self, expected: ResolutionState) -> None:
        if self.state != expected:
            raise ValueError(f"狀態機錯誤：預期在 {expected.value}，實際在 {self.state.value}")
