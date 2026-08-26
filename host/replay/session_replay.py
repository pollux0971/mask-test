"""B17 — HDF5 session 回放：把已經寫好的 session 檔案（`B07` 的
`SessionWriter` 產物）重播成一串帶 `replay: true` 標記的 SSE 事件字典，
供離線開發前端與 Demo 硬體失敗備援用（`E08`）。

跟 `T05`（重播原始序列埠 log）的差別：`T05` 重播的是**未解析的行**，
適合測 `B01` 的解析器；這裡重播的是**已結構化**的 session，適合測
`C` 軌的視覺化跟 `D` 軌的辨識——`T05` 需要真板子的 log（`E01` 還沒做），
這裡只需要一個 HDF5 檔（`ssi-backlog/tools/schema_example.py` 產的空檔
就夠開發用），兩者不互相依賴。

**排程用「目標播放時刻」，不是累加 sleep。** `B06` 的作者在 `Aligner`
踩過同樣的問題：累加會讓浮點誤差線性堆積。這裡永遠用

    目標時刻 = 錨點時刻 + (事件 t_us − 錨點 t_us) / 速度

從頭算，不會累積誤差；而且暫停/續播/變速/跳轉都要重新設錨點
（`_rebase()`），不然舊錨點會讓下一批事件在恢復播放的瞬間全部到期。

**這支模組只負責「讀 HDF5 → 排程 → 吐事件」，不碰序列埠也不碰 HTTP。**
`ReplayController.is_active` 是給呼叫端（`bridge_server.py`／`B19`）用的
訊號——回放進行中該不該接受真實序列埠資料是政策決定，不是這支模組能
替呼叫端做主的事，這裡只保證：**只要呼叫端把 `poll()`/`step()` 吐出來的
事件當成事實來源，就不會意外混進真實資料**（因為這支模組壓根不碰序列埠）。

**`speaking_mode` 目前沒有寫進 HDF5。** `B15` 的作者已經指出 CONTRACTS.md
沒有定義「說話模式」這個欄位（跟 `/meta` 的 session `mode` 是不同的軸），
調度員還沒裁決要不要收進 schema。這裡採取防禦性做法：`trial.attrs` 裡
**如果有** `speaking_mode` 就一併重播，沒有就照樣重播其他欄位、
不強制要求也不假裝有——等那個欄位名定案、`B07` 開始寫入之後，
這裡自動就會生效，不需要跟著改。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import h5py
import numpy as np

VALID_SPEEDS = (0.25, 1.0, 4.0)


class NoReplayEventsError(ValueError):
    """整個 session（或指定的 `start_trial_idx` 之後）沒有任何可重播的事件。"""


class TrialNotFoundError(ValueError):
    """`seek_to_trial()` 指定的 trial_idx 在這個 session 裡不存在。"""


@dataclass(frozen=True)
class ReplayEvent:
    t_us: int
    trial_idx: int
    payload: dict  # 不含 "replay" 鍵——由 ReplayController 在吐出時加上


def _floats_to_wire_ints(values) -> List[Optional[int]]:
    """`NaN`（B07 的無效值約定）還原成 `None`；其餘四捨五入回整數
    ——原始線協定的 `d`/`s` 欄位本來就是整數（mm／signal_per_spad/100）。
    """
    return [None if np.isnan(v) else int(round(float(v))) for v in values]


def _trial_events(name: str, grp: h5py.Group) -> List[ReplayEvent]:
    idx = int(name.split("_")[1])
    events: List[ReplayEvent] = []

    tof_t_us = grp["tof_t_us"][:]
    tof_A, tof_valid_A = grp["tof_A"][:], grp["tof_valid_A"][:]
    tof_B, tof_valid_B = grp["tof_B"][:], grp["tof_valid_B"][:]
    n_zones = tof_valid_A.shape[1] if tof_valid_A.ndim == 2 else 0
    for i, t_us in enumerate(tof_t_us):
        t_us = int(t_us)
        for sensor, values, valid in (("A", tof_A, tof_valid_A), ("B", tof_B, tof_valid_B)):
            events.append(ReplayEvent(t_us, idx, {
                "type": "tof", "sensor": sensor, "seq": i, "t_us": t_us,
                "dist": _floats_to_wire_ints(values[i, :n_zones]),
                "signal": _floats_to_wire_ints(values[i, n_zones:]),
                "valid": valid[i].astype(bool).tolist(),
            }))

    mic_t_us = grp["mic_t_us"][:]
    mic_rms, mic_peak = grp["mic_rms"][:], grp["mic_peak"][:]
    for i, t_us in enumerate(mic_t_us):
        events.append(ReplayEvent(int(t_us), idx, {
            "type": "mic", "seq": i, "t_us": int(t_us),
            "rms": float(mic_rms[i]), "peak": int(mic_peak[i]),
        }))

    if "mel" in grp:
        mel_t_us = grp["mel_t_us"][:]
        mel = grp["mel"][:]
        for i, t_us in enumerate(mel_t_us):
            events.append(ReplayEvent(int(t_us), idx, {
                "type": "mel", "seq": i, "t_us": int(t_us),
                "bands": mel[i].tolist(),
            }))

    attrs = grp.attrs
    trial_common = {"label": attrs["label"], "idx": idx, "quality": attrs["quality"]}
    if "speaking_mode" in attrs:  # 見模組 docstring：schema 目前沒有這個欄位
        trial_common["speaking_mode"] = attrs["speaking_mode"]

    data_t_us = [e.t_us for e in events]
    if data_t_us:
        events.append(ReplayEvent(min(data_t_us), idx, {"type": "trial", "state": "CAPTURE", **trial_common}))
        events.append(ReplayEvent(max(data_t_us), idx, {"type": "trial", "state": "SAVE", **trial_common}))

    return events


def read_session_events(h5_path, start_trial_idx: int = 0) -> List[ReplayEvent]:
    """讀一個 `SessionWriter` 寫出來的 HDF5 session，回傳依 `t_us` 排序的
    `ReplayEvent` 列表（`start_trial_idx` 之前的 trial 完全不讀，對應
    驗收條件「可從任意 trial 開始」）。"""
    events: List[ReplayEvent] = []
    with h5py.File(h5_path, "r") as f:
        trial_names = sorted(k for k in f.keys() if k.startswith("trial_"))
        for name in trial_names:
            if int(name.split("_")[1]) < start_trial_idx:
                continue
            events.extend(_trial_events(name, f[name]))

    if not events:
        raise NoReplayEventsError(
            f"{h5_path} 從 trial_idx={start_trial_idx} 開始沒有任何可重播的事件"
        )
    events.sort(key=lambda e: e.t_us)
    return events


class ReplayController:
    """一次回放對應一個實例。純邏輯、不自己起執行緒/計時器——呼叫端
    （`bridge_server.py`）自己跑事件迴圈，週期性呼叫 `poll(now)` 取得
    「已經到期」的事件並發布成 SSE，跟 `TrialStateMachine.tick()` 是
    同一種設計（注入時鐘、回傳事件列表、不管排程本身怎麼被驅動）。
    """

    def __init__(self, events: List[ReplayEvent], clock: Callable[[], float] = None):
        if not events:
            raise NoReplayEventsError("events 不能是空的")
        self._events = sorted(events, key=lambda e: e.t_us)
        self._clock = clock or time.monotonic
        self._speed = 1.0
        self._paused = False
        self._pos = 0
        self._anchor_wallclock: Optional[float] = None
        self._anchor_t_us: Optional[int] = None
        self._rebase()

    def _rebase(self) -> None:
        """重新設定排程錨點：**現在**這一刻對應到目前 `_pos` 那個事件的
        `t_us`。任何會改變時間基準的操作（暫停/續播/變速/跳轉）都必須
        呼叫這個，否則舊錨點會讓累積的事件在恢復播放瞬間全部到期。"""
        self._anchor_wallclock = self._clock()
        self._anchor_t_us = self._events[self._pos].t_us if not self.finished else None

    @property
    def finished(self) -> bool:
        return self._pos >= len(self._events)

    @property
    def is_active(self) -> bool:
        """呼叫端拿這個決定「現在算不算在回放中」——例如據此拒絕真實
        序列埠資料，避免兩條資料流混在一起（story 明訂的災難情境）。"""
        return not self.finished

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def current_trial_idx(self) -> Optional[int]:
        return None if self.finished else self._events[self._pos].trial_idx

    def set_speed(self, speed: float) -> None:
        if speed not in VALID_SPEEDS:
            raise ValueError(f"speed 必須是 {VALID_SPEEDS} 之一，收到 {speed}")
        self._speed = speed
        self._rebase()

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._rebase()

    def seek_to_trial(self, trial_idx: int) -> None:
        idx = next((i for i, e in enumerate(self._events) if e.trial_idx == trial_idx), None)
        if idx is None:
            raise TrialNotFoundError(f"這個 session 裡沒有 trial_idx={trial_idx}")
        self._pos = idx
        self._rebase()

    def step(self) -> Optional[dict]:
        """不管排程時刻到了沒，立刻吐出下一個事件（單步偵錯用）。"""
        if self.finished:
            return None
        event = self._events[self._pos]
        self._pos += 1
        self._rebase()
        return {**event.payload, "replay": True}

    def poll(self, now: Optional[float] = None) -> List[dict]:
        """回傳所有「排程時刻已到」的事件（呼叫間隔比事件間距長時可能
        一次回好幾個）。`paused` 或播放完畢時一律回空列表。"""
        if self._paused or self.finished:
            return []
        now = self._clock() if now is None else now

        due: List[dict] = []
        while not self.finished:
            event = self._events[self._pos]
            target = self._anchor_wallclock + (event.t_us - self._anchor_t_us) / self._speed / 1e6
            if target > now:
                break
            due.append({**event.payload, "replay": True})
            self._pos += 1
        return due
