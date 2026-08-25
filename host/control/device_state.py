"""B18 — `GET /device/state` 的資料模型。

**`$STATUS` 沒有回報每顆感測器的開關狀態。** CONTRACTS.md §1.1.2 的
`$STATUS` 自我描述欄位裡，`mel=<0|1>` 跟 `amb=<0|1>` 是裝置**確認過**
的狀態（韌體自己回報），但 `SENS:<A|B>=<0|1>` 沒有對應的回報欄位——
裝置收到指令後只會重發 `$STATUS`，不會在裡面說「A 現在是關的」。

所以 `sensor_a_enabled`/`sensor_b_enabled` 這兩個欄位**是主機端自己記的
「上一次送出的指令」，不是裝置確認過的狀態**。這跟 `resolution`/
`mel_enabled`/`ambient_enabled`（直接來自最新一次 `$STATUS`）不一樣，
`DeviceState` 用 `sensor_state_confirmed=False` 明確標出這個差異，不要
讓前端誤以為五個欄位一樣可信。

（這個落差已經記錄到 CONTRACTS.md §4.1 疑問區，供之後決定要不要在韌體端
補上 `sens_a=<0|1>`/`sens_b=<0|1>` 自我描述欄位。）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from host.control.resolution import ResolutionController


@dataclass(frozen=True)
class DeviceState:
    resolution: Optional[int]           # 4|8，來自最新 $STATUS 的 res
    proto_version: Optional[int]        # 來自最新 $STATUS 的 proto
    fw_sha: Optional[str]               # 來自最新 $STATUS 的 fw
    mel_enabled: Optional[bool]         # 裝置確認過（$STATUS 的 mel=）
    ambient_enabled: Optional[bool]     # 裝置確認過（$STATUS 的 amb=，A16 之後才有）
    sensor_a_enabled: Optional[bool]    # 主機端記錄的上次指令，非裝置確認
    sensor_b_enabled: Optional[bool]    # 同上
    sensor_state_confirmed: bool        # 見模組 docstring
    resolution_change_in_progress: bool  # ResolutionController.is_busy
    resolution_change_state: str        # ResolutionController.state.value

    def to_dict(self) -> dict:
        return {
            "resolution": self.resolution,
            "proto_version": self.proto_version,
            "fw_sha": self.fw_sha,
            "mel_enabled": self.mel_enabled,
            "ambient_enabled": self.ambient_enabled,
            "sensor_a_enabled": self.sensor_a_enabled,
            "sensor_b_enabled": self.sensor_b_enabled,
            "sensor_state_confirmed": self.sensor_state_confirmed,
            "resolution_change_in_progress": self.resolution_change_in_progress,
            "resolution_change_state": self.resolution_change_state,
        }


def build_device_state(
    latest_status_event: Optional[dict],
    resolution_controller: ResolutionController,
    sensor_a_enabled: Optional[bool] = None,
    sensor_b_enabled: Optional[bool] = None,
) -> DeviceState:
    """`latest_status_event` 是 `host/capture/protocol.py` 的
    `ProtocolParser.status`（一個 `$STATUS` 解析結果，或者 session 一開始
    還沒收到過任何 `$STATUS` 時是 `None`）。`sensor_a_enabled`／
    `sensor_b_enabled` 是呼叫端（`bridge_server.py`）自己維護的「上次送出
    的 `SENS` 指令」，不是這支函式自己去猜。

    解析度優先用 `ResolutionController.current_resolution`（重燒完成後
    立刻更新，不用等下一行 `$STATUS`）；重燒進行中則兩者都可能暫時不一致，
    這時以 controller 的狀態為準，因為它才知道「現在其實正在變」。
    """
    event = latest_status_event or {}

    resolution = resolution_controller.current_resolution
    if not resolution_controller.is_busy and event.get("res") is not None:
        resolution = event["res"]

    return DeviceState(
        resolution=resolution,
        proto_version=event.get("proto"),
        fw_sha=event.get("fw"),
        mel_enabled=event.get("mel"),
        ambient_enabled=event.get("amb"),
        sensor_a_enabled=sensor_a_enabled,
        sensor_b_enabled=sensor_b_enabled,
        sensor_state_confirmed=False,
        resolution_change_in_progress=resolution_controller.is_busy,
        resolution_change_state=resolution_controller.state.value,
    )
