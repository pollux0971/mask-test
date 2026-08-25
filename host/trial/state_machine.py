"""B11 -- Trial 狀態機（純邏輯層；HTTP wiring 是 B19 的事，見檔案底部的
「介面形狀」摘要，那份會 SendMessage 給調度員轉給 ed）。

```
IDLE --start_trial()--> PROMPT(1.5s) --> COUNTDOWN(0.5s) --> CAPTURE(2.0s)
    --> SAVE --> REST(1.5s) --> IDLE
```

**怎麼觸發下一個 trial 不是這裡的事**（B12 hold-to-record / B13 auto-VAD），
這裡只管「一旦開始了，時序結構要一致」。`start_trial()` 由外部呼叫觸發。

**兩個時鐘，各管各的**：
  * `clock`（注入，預設 `time.monotonic`）驅動 PROMPT/COUNTDOWN/CAPTURE/REST
    的**節奏**（給人看的 UI 步調），測試用假時鐘快轉，不必真的等待。
  * `device_t_us`（`tick()` 的參數）標記 CAPTURE 視窗的**資料邊界**。
    trial 邊界必須用裝置 `t_us`、不是主機時間——這是調度員在派工時特別
    交代的（避免主機排程抖動讓 CAPTURE 視窗跟實際收到的幀對不齊）。
    `tof_t_us`/`mic_t_us` 這些寫進 HDF5 的時間戳，因此永遠是裝置時間，
    不是 `clock()` 的讀數。

**放棄的 trial 完全不落盤**：`abort()`/`redo()` 在 SAVE 之前呼叫都只是
把記憶體狀態丟掉、回到 IDLE，`SessionWriter.write_trial()` 根本不會被呼叫。
`SAVE` 這一步本身就是唯一會寫檔案的地方，而且是同步執行、寫完才進 REST。

**`abort` 跟 `redo` 的差異**（`B11.md` 只說兩個 endpoint 都要有，沒定義行為
差異——這是本 story 的判斷，已在完成回報裡請調度員確認）：
  * `abort()`：放棄這次，**跳過**這個詞，下一次 `start_trial()` 換下一個詞。
  * `redo()`：放棄這次，但**保留**同一個詞，下一次 `start_trial()` 還是它。
"""
from __future__ import annotations

import random
import time
from enum import Enum
from typing import Callable, List, Optional, Sequence

import numpy as np

from host.storage.manifest import add_session

PROMPT_S = 1.5
COUNTDOWN_S = 0.5
CAPTURE_S = 2.0
REST_S = 1.5
CAPTURE_RATE_HZ = 30.0

_TOF_VALUE_CHANNELS = 32
_TOF_VALID_CHANNELS = 16

VALID_QUALITY_VALUES = ("ok", "low", "rejected")


class TrialState(str, Enum):
    IDLE = "IDLE"
    PROMPT = "PROMPT"
    COUNTDOWN = "COUNTDOWN"
    CAPTURE = "CAPTURE"
    SAVE = "SAVE"
    REST = "REST"


_DURATIONS = {
    TrialState.PROMPT: PROMPT_S,
    TrialState.COUNTDOWN: COUNTDOWN_S,
    TrialState.CAPTURE: CAPTURE_S,
    TrialState.REST: REST_S,
}

_NEXT_STATE = {
    TrialState.PROMPT: TrialState.COUNTDOWN,
    TrialState.COUNTDOWN: TrialState.CAPTURE,
    TrialState.REST: TrialState.IDLE,
}


def classify_quality(valid_zone_ratio: float, drop_count: int) -> str:
    """一階啟發式門檻。`B11.md` 沒有指定確切數字（`config/session_targets.json`
    的目標幾何全是 `null`，本來就不能拿「距離偏離目標」來判定），這裡只用
    `valid_zone_ratio`／`drop_count` 兩個不需要目標幾何的量。**門檻是本
    story 的預設值，不是 CONTRACTS 凍結的數字**——已在完成回報裡請調度員
    ／D 軌確認是否要調整，這會直接影響 D12 的 CV 分組。
    """
    if valid_zone_ratio >= 0.7 and drop_count == 0:
        return "ok"
    if valid_zone_ratio >= 0.3:
        return "low"
    return "rejected"


def _frames_to_tof_arrays(frames, values_attr: str, present_attr: str):
    """把 `Aligner.frames()` 吐出的 `AlignedFrame` 序列轉成 T02 schema 要的
    `(T, 32) float32`／`(T, 16) bool`。無效通道（`TofSample.values` 裡的
    `None`，或整幀沒有樣本 `*_present=False`）填 `NaN`——不是 `-1`、不是
    `0`（CONTRACTS.md §2「無效 zone 在 tof_A/tof_B 數值陣列裡一律填 NaN」）。
    """
    t = len(frames)
    values = np.full((t, _TOF_VALUE_CHANNELS), np.nan, dtype=np.float32)
    valid = np.zeros((t, _TOF_VALID_CHANNELS), dtype=bool)
    for i, frame in enumerate(frames):
        present = getattr(frame, present_attr)
        sample = getattr(frame, values_attr)
        if present and sample is not None:
            values[i, :] = [np.nan if v is None else v for v in sample.values]
            valid[i, :] = sample.valid
    return values, valid


class TrialStateMachine:
    """一個 session 用一個實例。呼叫端負責：
      * 把即時收到的裝置事件（`ProtocolParser.feed()` 的輸出）餵進 `push_event()`
      * 週期性呼叫 `tick()`（多快都可以，只是解析度；沒有內建計時器/執行緒）
      * 把 `start_trial()`/`tick()`/`abort()`/`redo()` 回傳的事件轉發成 SSE
    """

    def __init__(
        self,
        words: Sequence[str],
        aligner,
        session_writer,
        session_h5_path,
        manifest_path,
        *,
        wear_id: int,
        mode: str,
        seed: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
        manifest_root=None,
        mic_buffer_seconds: float = 15.0,
    ):
        if not words:
            raise ValueError("words 不能是空的")
        self._words = list(words)
        self._aligner = aligner
        self._writer = session_writer
        self._session_h5_path = session_h5_path
        self._manifest_path = manifest_path
        self._manifest_root = manifest_root
        self._wear_id = wear_id
        self._mode = mode
        self._clock = clock
        self._mic_buffer_seconds = mic_buffer_seconds

        self._seed = seed if seed is not None else random.SystemRandom().randrange(2**31 - 1)
        self._order = list(self._words)
        random.Random(self._seed).shuffle(self._order)
        self._order_pos = 0

        self.state = TrialState.IDLE
        self._state_entered_at: Optional[float] = None
        self._current_label: Optional[str] = None
        self._capture_start_t_us: Optional[int] = None
        self._capture_end_t_us: Optional[int] = None
        self._next_trial_idx = 0

        # mic 原生取樣率的緩衝，跟 Aligner 內部的環形緩衝分開一份。
        # 原因見本檔案模組層級文件字串「已知限制」段落：Aligner.frames()
        # 只能吐單一共同取樣率，會把 mic 錯誤地重取樣成 ToF 的 30Hz 網格，
        # 違反 schema 對 mic_t_us 要保留原生取樣率（M）的要求。
        self._mic_events: List[tuple] = []

    # -- 唯讀屬性 ---------------------------------------------------

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def order(self) -> List[str]:
        """打散後的詞序（記錄用的 seed 產生），呼叫端可以直接拿去顯示或記錄。"""
        return list(self._order)

    @property
    def current_label(self) -> Optional[str]:
        return self._current_label

    @property
    def next_trial_idx(self) -> int:
        return self._next_trial_idx

    # -- 即時資料輸入 -------------------------------------------------

    def push_mic(self, t_us: int, rms: float, peak: float) -> None:
        t_us = int(t_us)
        self._mic_events.append((t_us, float(rms), float(peak)))
        cutoff = t_us - int(self._mic_buffer_seconds * 1_000_000)
        while self._mic_events and self._mic_events[0][0] < cutoff:
            self._mic_events.pop(0)

    def push_event(self, event: dict) -> None:
        """方便直接餵 `ProtocolParser.feed()` 的輸出：轉發給 `Aligner`（ToF
        用它的 `frames()` 取樣），mic 額外進自己的原生取樣率緩衝。"""
        if event.get("type") == "mic":
            self.push_mic(event["t_us"], event["rms"], event["peak"])
        self._aligner.push_event(event)

    # -- trial 生命週期 -----------------------------------------------

    def start_trial(self, now: Optional[float] = None, label: Optional[str] = None) -> dict:
        if self.state != TrialState.IDLE:
            raise RuntimeError(f"不能在狀態 {self.state.value} 開始新 trial，必須先回到 IDLE")
        now = self._clock() if now is None else now
        self._current_label = label if label is not None else self._order[self._order_pos % len(self._order)]
        return self._enter(TrialState.PROMPT, now)

    def tick(self, now: Optional[float] = None, device_t_us: Optional[int] = None) -> List[dict]:
        """依 `clock` 經過的時間推進狀態機。多呼叫幾次沒關係（狀態沒到期
        就什麼都不做），呼叫頻率只影響狀態轉換被偵測到的解析度。

        `device_t_us` 在從 COUNTDOWN 進 CAPTURE（標記 capture 起點）、以及
        從 CAPTURE 出來（標記 capture 終點）時是必要的——這兩個時刻不給就
        會丟 `ValueError`，因為 trial 邊界規定用裝置時間，沒有裝置時間就
        沒辦法正確定義這個 trial 要落盤的資料範圍。
        """
        now = self._clock() if now is None else now
        if self.state in (TrialState.IDLE, TrialState.SAVE):
            return []
        elapsed = now - self._state_entered_at
        if elapsed < _DURATIONS[self.state]:
            return []

        events: List[dict] = []
        if self.state == TrialState.CAPTURE:
            if device_t_us is None:
                raise ValueError("離開 CAPTURE 需要 device_t_us 標記 capture 終點")
            self._capture_end_t_us = int(device_t_us)
            self.state = TrialState.SAVE
            events.append(self._do_save(now))
            events.append(self._enter(TrialState.REST, now))
        else:
            if self.state == TrialState.COUNTDOWN:
                if device_t_us is None:
                    raise ValueError("離開 COUNTDOWN 需要 device_t_us 標記 capture 起點")
                self._capture_start_t_us = int(device_t_us)
            next_state = _NEXT_STATE[self.state]
            events.append(self._enter(next_state, now))
            if next_state == TrialState.IDLE:
                self._order_pos += 1  # 正常跑完一輪 -> 換下一個詞
                self._current_label = None

        return events

    def abort(self, now: Optional[float] = None) -> dict:
        """放棄目前這個 trial，**跳過**這個詞。不寫入任何資料。"""
        return self._cancel(now, advance_word=True)

    def redo(self, now: Optional[float] = None) -> dict:
        """放棄目前這個 trial，但**保留**同一個詞給下一次 `start_trial()`。
        不寫入任何資料。"""
        return self._cancel(now, advance_word=False)

    def mark_current_trial_saved_quality(self, h5_path, trial_idx: int, quality: str) -> None:
        """事後把**已經落盤**的 trial 標成別的 quality（例如使用者事後回看
        覺得這筆不能用）。這不是狀態機本身的一部分——狀態機只管「錄的當下」
        的 abort/redo；`C14`（重錄/棄用 UI）要的是「已存檔之後」還能改標記，
        兩者是不同時間點的操作，所以是獨立的函式，不是狀態機方法。
        """
        _mark_trial_quality(h5_path, trial_idx, quality, self._manifest_path, self._manifest_root)

    # -- 內部 -----------------------------------------------------

    def _enter(self, state: TrialState, now: float) -> dict:
        self.state = state
        self._state_entered_at = now
        return self._event()

    def _event(self, **overrides) -> dict:
        payload = {
            "type": "trial",
            "state": self.state.value,
            "label": self._current_label,
            "idx": self._next_trial_idx,
            "seed": self._seed,
        }
        payload.update(overrides)
        return payload

    def _cancel(self, now: Optional[float], advance_word: bool) -> dict:
        if self.state == TrialState.IDLE:
            raise RuntimeError("沒有進行中的 trial 可以取消")
        now = self._clock() if now is None else now
        aborted_label = self._current_label
        idx = self._next_trial_idx

        self._capture_start_t_us = None
        self._capture_end_t_us = None
        self._current_label = None
        if advance_word:
            self._order_pos += 1

        event = self._enter(TrialState.IDLE, now)
        event["idx"] = idx
        event["aborted_label"] = aborted_label
        return event

    def _do_save(self, now: float) -> dict:
        frames = list(self._aligner.frames(
            self._capture_start_t_us, self._capture_end_t_us, rate_hz=CAPTURE_RATE_HZ,
        ))
        tof_A, tof_valid_A = _frames_to_tof_arrays(frames, "tof_A", "tof_A_present")
        tof_B, tof_valid_B = _frames_to_tof_arrays(frames, "tof_B", "tof_B_present")
        tof_t_us = np.array([f.t_us for f in frames], dtype=np.int64)

        drop_count = sum(1 for f in frames if not f.tof_A_present and not f.tof_B_present)
        if tof_valid_A.size or tof_valid_B.size:
            valid_zone_ratio = float(np.concatenate([tof_valid_A, tof_valid_B], axis=1).mean())
        else:
            valid_zone_ratio = 0.0

        mic_window = [
            e for e in self._mic_events
            if self._capture_start_t_us <= e[0] <= self._capture_end_t_us
        ]
        if mic_window:
            mic_rms = np.array([e[1] for e in mic_window], dtype=np.float32)
            mic_peak = np.array([e[2] for e in mic_window], dtype=np.int16)
            mic_t_us = np.array([e[0] for e in mic_window], dtype=np.int64)
        else:
            # write_trial() 要求非空且三者長度一致；這個 capture 視窗內沒收
            # 到任何 $M 事件時，退化成一個哨兵幀而不是讓整個 trial 寫不進去
            # （ToF 資料通常才是這個 trial 有沒有價值的關鍵，見 quality 判定）。
            mic_rms = np.zeros(1, dtype=np.float32)
            mic_peak = np.zeros(1, dtype=np.int16)
            mic_t_us = np.array([self._capture_start_t_us], dtype=np.int64)

        idx = self._next_trial_idx
        quality = classify_quality(valid_zone_ratio, drop_count)

        self._writer.write_trial(
            idx, label=self._current_label,
            tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
            tof_valid_A=tof_valid_A, tof_valid_B=tof_valid_B,
            mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
            # mel 刻意不傳：見本檔案模組文件字串「已知限制」，B07 的
            # write_trial() 目前假設 mel 幀數必須等於 mic 幀數，這在 A14
            # 之後的 62.5Hz/31.25Hz 不成立，等 CONTRACTS/B07 這塊確認前
            # 先不寫 mel，audio 同理不在 B11 範圍內。
            wear_id=self._wear_id, mode=self._mode,
            valid_zone_ratio=valid_zone_ratio, drop_count=drop_count,
            # B13（Auto-VAD）尚未實作，暫時用整個 capture 視窗邊界當佔位值——
            # 完成回報裡已標成需要 B13 完成後回頭補正確數字的項目。
            vad_start_us=self._capture_start_t_us, vad_end_us=self._capture_end_t_us,
            lip_onset_us=self._capture_start_t_us, voice_onset_us=self._capture_start_t_us,
            quality=quality,
        )
        add_session(self._session_h5_path, self._manifest_path, root=self._manifest_root)

        self._next_trial_idx += 1
        return self._event(
            idx=idx, quality=quality, valid_zone_ratio=valid_zone_ratio,
            drop_count=drop_count, n_frames=int(tof_A.shape[0]),
        )


def _mark_trial_quality(h5_path, trial_idx: int, quality: str, manifest_path, root=None) -> None:
    if quality not in VALID_QUALITY_VALUES:
        raise ValueError(f"quality 必須是 {VALID_QUALITY_VALUES} 之一，收到 {quality!r}")
    import h5py

    with h5py.File(h5_path, "a") as f:
        group_name = f"trial_{trial_idx:03d}"
        if group_name not in f:
            raise KeyError(f"{h5_path} 裡沒有 {group_name}")
        f[group_name].attrs["quality"] = quality

    add_session(h5_path, manifest_path, root=root)
