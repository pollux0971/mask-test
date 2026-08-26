"""B11/B12 -- Trial 狀態機（純邏輯層；HTTP wiring 是 B19 的事，見檔案底部的
「介面形狀」摘要，那份會 SendMessage 給調度員轉給 ed）。

固定時長模式（B11，例如給 B13 auto-VAD 用）：
```
IDLE --start_trial()--> PROMPT(1.5s) --> COUNTDOWN(0.5s) --> CAPTURE(2.0s)
    --> SAVE --> REST(1.5s) --> IDLE
```

Hold-to-record 模式（B12，使用者按住開始、放開結束，取代固定 2.0s）：
```
IDLE --hold_start()--> CAPTURE --hold_stop()--+-- [0.3s,5s] 內 --> SAVE --> REST(1.5s) --> IDLE
                                               +-- 超出範圍 --> CONFIRM --confirm_keep()--> SAVE --> REST --> IDLE
                                                                        --discard_pending()--> IDLE
```
`hold_start()` 跳過 PROMPT/COUNTDOWN：使用者按下就是開始，不需要外部倒數
——提示卡怎麼顯示、顯示多久是前端（C12）自己的事，不需要後端計時器參與。
兩種模式共用同一個 `TrialState.CAPTURE` 值，`tick()` 靠是否曾呼叫過
`hold_start()`（`_hold_start_device_t_us` 是否已設）分辨這次的 CAPTURE
該不該被固定計時器結束——**兩種觸發方式不會、也不應該同時作用在同一次
trial 上**，這是本模組對呼叫端的假設，不是它自己會檢查的東西。

**怎麼觸發下一個 trial 不是這裡的事**（B12 hold-to-record / B13 auto-VAD），
這裡只管「一旦開始了，時序結構要一致」。`start_trial()`/`hold_start()`
由外部呼叫觸發。

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
from host.storage.session_writer import VALID_SPEAKING_MODES
from host.vad.audio_vad import detect_from_events as detect_voice
from host.vad.onset import measure_lip_lead
from host.vad.tof_vad import detect_from_events as detect_lips

PROMPT_S = 1.5
COUNTDOWN_S = 0.5
CAPTURE_S = 2.0
REST_S = 1.5
CAPTURE_RATE_HZ = 30.0

_TOF_VALUE_CHANNELS = 32
_TOF_VALID_CHANNELS = 16

VALID_QUALITY_VALUES = ("ok", "low", "rejected")

# B12: hold-to-record padding and duration guards.
HOLD_PRE_ROLL_US = 300_000    # 回溯：反應時間 ~200ms + 緩衝，見 B12.md
HOLD_POST_ROLL_US = 200_000
HOLD_MIN_DURATION_S = 0.3     # 短於這個通常是誤觸
HOLD_MAX_DURATION_S = 5.0     # 長於這個通常是忘記放開


class TrialState(str, Enum):
    IDLE = "IDLE"
    PROMPT = "PROMPT"
    COUNTDOWN = "COUNTDOWN"
    CAPTURE = "CAPTURE"
    SAVE = "SAVE"
    REST = "REST"
    # B12: hold 放開但時長超出 [HOLD_MIN_DURATION_S, HOLD_MAX_DURATION_S]，
    # 資料留在記憶體、尚未落盤，等 confirm_keep()/discard_pending() 決定。
    CONFIRM = "CONFIRM"


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
        first_trial_idx: int = 0,
        baseline_mu_A: Optional[np.ndarray] = None,
        baseline_sigma_A: Optional[np.ndarray] = None,
        baseline_mu_B: Optional[np.ndarray] = None,
        baseline_sigma_B: Optional[np.ndarray] = None,
        noise_floor_mu: Optional[float] = None,
        noise_floor_sigma: Optional[float] = None,
        energy_mu: Optional[float] = None,
        energy_sigma: Optional[float] = None,
    ):
        """B21：`baseline_mu_*`/`baseline_sigma_*`（ToF，各 32 值）、
        `noise_floor_mu`/`sigma`（麥克風）與 `energy_mu`/`sigma`（ToF 唇動
        偵測的能量門檻，見 `host/storage/baseline.py` 的
        `evaluate_baseline()`）都來自 `B10` 的 `capture_baseline_trial()`，
        已經寫進 `/meta`（呼叫端 -- 目前是 `bridge_server.py` -- 從 session
        的 `/meta` 讀出來傳進來，這裡不自己開 HDF5 讀）。

        `energy_mu`/`sigma` 不給就讓 `detect_lip_activity()` 自己從這筆
        trial 的資料估（`estimate_energy_floor()`，B16 量過會偏嚴約
        23%）——baseline 期間算好的比較準，因為那段保證沒有動作，但沒有
        也不是不能動（只是精度差一點，不是壞掉）。

        全部設 `Optional[None]` 而不是必填：`host.vad.tof_vad`/`audio_vad`
        自己在缺 baseline/底噪時就會回 `applicable=False`（不拋例外），
        沒有理由在這裡重複擋一次；這樣也不用改動任何一個既有測試的
        `_make_sm()` 呼叫。真正執行 `/trial/*` 的 session 一定會有
        baseline（`baseline_done` 旗標擋著，見這個類別頂端的說明），只有
        測試會用 `None`。
        """
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
        self._baseline_mu_A = baseline_mu_A
        self._baseline_sigma_A = baseline_sigma_A
        self._baseline_mu_B = baseline_mu_B
        self._baseline_sigma_B = baseline_sigma_B
        self._noise_floor_mu = noise_floor_mu
        self._noise_floor_sigma = noise_floor_sigma
        self._energy_mu = energy_mu
        self._energy_sigma = energy_sigma

        self._seed = seed if seed is not None else random.SystemRandom().randrange(2**31 - 1)
        self._order = list(self._words)
        random.Random(self._seed).shuffle(self._order)
        self._order_pos = 0

        self.state = TrialState.IDLE
        self._state_entered_at: Optional[float] = None
        self._current_label: Optional[str] = None
        self._capture_start_t_us: Optional[int] = None
        self._capture_end_t_us: Optional[int] = None
        # C12/ed: baseline is written as trial_000 by a separate SessionWriter
        # call (host/storage/baseline.py), so a session with a baseline must
        # start its own trial numbering at 1, not 0, or the first real trial
        # collides with it. Caller passes the right starting index; this
        # class doesn't know whether a baseline was recorded.
        self._next_trial_idx = first_trial_idx
        self._hold_start_device_t_us: Optional[int] = None  # B12: hold-to-record
        # B21: CONTRACTS.md §2 puts speaking_mode on the *trial*, not /meta
        # -- it can change mid-session (README demo: normal -> whisper for
        # one word -> normal). "Sticky" default: a call that doesn't specify
        # it keeps whatever the last trial used, starting from "normal" for
        # the first one -- this is this story's own call (the story text
        # says the caller decides and documents it), matching how a
        # panel toggle naturally behaves: the user set it once, it stays
        # that way until they touch it again, not reset every trial.
        self._speaking_mode: str = "normal"

        # mic/mel 原生取樣率的緩衝，跟 Aligner 內部的環形緩衝分開一份。
        # 原因：Aligner.frames() 只能吐單一共同取樣率，會把 mic/mel 錯誤地
        # 重取樣成 ToF 的 30Hz 網格，違反 schema 對 mic_t_us（M）／mel_t_us
        # （F）要保留各自原生取樣率的要求（CONTRACTS.md §1.1.1/§2）。
        self._mic_events: List[tuple] = []
        self._mel_events: List[tuple] = []

        # B21: raw ProtocolParser events, kept *in addition to* what
        # _aligner already does with them -- Aligner.frames() only hands
        # back AlignedFrame (resampled onto the ToF grid), but
        # host.vad.tof_vad/audio_vad's detect_from_events() need the
        # original per-stream dicts (type/sensor/distance/signal/t_us).
        # Copied on push (see push_event()), not a view into anything else
        # that might get reshaped/trimmed later -- same reasoning as
        # _mic_events/_mel_events above, and the same bug C13 already hit
        # once on the frontend's dataStore ring buffer (a live reference
        # goes stale as soon as the buffer trims past it).
        self._raw_events: List[dict] = []

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

    def peek_next_label(self) -> Optional[str]:
        """C12：唯讀，`start_trial()`/`hold_start()` 沒指定 `label` 時會用
        哪個詞，**不消耗、不改變** `_order_pos`。附在 `IDLE`/`REST`/`SAVE`
        事件裡，讓前端能在使用者按下去之前就先顯示提示卡（Hold-to-Record
        的整個互動前提）。

        詞序是**循環的**（`E05` 一個詞要錄遠超過 8 次，用完整輪詞表會繞回
        第一個），所以正常情況下永遠有值可回。回 `None` 只有 `_order` 本身
        是空的這個防禦性分支——建構時 `words` 已經擋過空輸入，正常不會走到
        這裡；不是「詞序用完了」的意思。呼叫端看到 `None` 不用特別處理成
        錯誤，就是還沒有下一個詞可以預告。
        """
        if not self._order:
            return None
        return self._order[self._order_pos % len(self._order)]

    # -- 即時資料輸入 -------------------------------------------------

    def push_mic(self, t_us: int, rms: float, peak: float) -> None:
        t_us = int(t_us)
        self._mic_events.append((t_us, float(rms), float(peak)))
        cutoff = t_us - int(self._mic_buffer_seconds * 1_000_000)
        while self._mic_events and self._mic_events[0][0] < cutoff:
            self._mic_events.pop(0)

    def push_mel(self, t_us: int, log_mel) -> None:
        t_us = int(t_us)
        self._mel_events.append((t_us, list(log_mel)))
        cutoff = t_us - int(self._mic_buffer_seconds * 1_000_000)
        while self._mel_events and self._mel_events[0][0] < cutoff:
            self._mel_events.pop(0)

    def push_event(self, event: dict) -> None:
        """方便直接餵 `ProtocolParser.feed()` 的輸出：轉發給 `Aligner`（ToF
        用它的 `frames()` 取樣），mic/mel 額外各自進自己的原生取樣率緩衝。"""
        etype = event.get("type")
        if etype == "mic":
            self.push_mic(event["t_us"], event["rms"], event["peak"])
        elif etype == "mel":
            self.push_mel(event["t_us"], event["log_mel"])
        # B21: only tof/mic are what detect_from_events() (host/vad/) reads;
        # v1-protocol events have no t_us to window by (see class docstring
        # on push_event/tick's device_t_us) and get skipped rather than
        # buffered with no way to ever trim or slice them.
        if etype in ("tof", "mic") and event.get("t_us") is not None:
            t_us = int(event["t_us"])
            self._raw_events.append(dict(event))
            cutoff = t_us - int(self._mic_buffer_seconds * 1_000_000)
            while self._raw_events and self._raw_events[0]["t_us"] < cutoff:
                self._raw_events.pop(0)
        self._aligner.push_event(event)

    # -- trial 生命週期 -----------------------------------------------

    def _apply_speaking_mode(self, speaking_mode: Optional[str]) -> None:
        """B21：在真的開始錄之前就驗證，而不是留到 `_do_save()` 才讓
        `write_trial()` 拋 `ValueError`——那個時間點使用者已經在錄音中，
        session 當場中斷。C11 表單的 `mode` 欄位是自由文字，`speaking_mode`
        不是同一個東西（見 CONTRACTS.md §2），這裡是值域真正被守住的地方。
        """
        if speaking_mode is None:
            return
        if speaking_mode not in VALID_SPEAKING_MODES:
            raise ValueError(f"speaking_mode 必須是 {VALID_SPEAKING_MODES} 之一，收到 {speaking_mode!r}")
        self._speaking_mode = speaking_mode

    def start_trial(self, now: Optional[float] = None, label: Optional[str] = None,
                     speaking_mode: Optional[str] = None) -> dict:
        if self.state != TrialState.IDLE:
            raise RuntimeError(f"不能在狀態 {self.state.value} 開始新 trial，必須先回到 IDLE")
        self._apply_speaking_mode(speaking_mode)
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
        if self.state in (TrialState.IDLE, TrialState.SAVE, TrialState.CONFIRM):
            return []
        if self.state == TrialState.CAPTURE and self._hold_start_device_t_us is not None:
            # B12: 這是 hold-to-record 觸發的 CAPTURE（見 hold_start()），
            # 固定時長模式的計時器不適用——時長由使用者放開按鍵決定，
            # 只有 hold_stop() 能結束它，tick() 在這裡永遠是 no-op。
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
            # 詞指標在這裡（資料已經鎖定要落盤的當下）就前進，不是等到
            # REST->IDLE 才動——這樣 SAVE／REST 的事件才能正確 peek 到「下一個」
            # 詞，而不是剛存好的這個。`_current_label`（顯示用）維持不變，
            # 讓 REST 畫面還能顯示「剛才錄的是哪個詞」，直到真的進 IDLE 才清掉。
            self._order_pos += 1
            events.append(self._do_save(now))
            events.append(self._enter(TrialState.REST, now, next_label=self.peek_next_label()))
        else:
            if self.state == TrialState.COUNTDOWN:
                if device_t_us is None:
                    raise ValueError("離開 COUNTDOWN 需要 device_t_us 標記 capture 起點")
                self._capture_start_t_us = int(device_t_us)
            next_state = _NEXT_STATE[self.state]
            if next_state == TrialState.IDLE:
                self._current_label = None
                events.append(self._enter(next_state, now, next_label=self.peek_next_label()))
            else:
                events.append(self._enter(next_state, now))

        return events

    def abort(self, now: Optional[float] = None) -> dict:
        """放棄目前這個 trial，**跳過**這個詞。不寫入任何資料。"""
        return self._cancel(now, advance_word=True)

    def redo(self, now: Optional[float] = None) -> dict:
        """放棄目前這個 trial，但**保留**同一個詞給下一次 `start_trial()`。
        不寫入任何資料。"""
        return self._cancel(now, advance_word=False)

    # -- B12: hold-to-record -----------------------------------------

    def hold_start(self, now: Optional[float] = None, device_t_us: Optional[int] = None,
                    label: Optional[str] = None, speaking_mode: Optional[str] = None) -> dict:
        """`POST /trial/hold/start`。按下就是開始，**沒有 COUNTDOWN**——固定
        時長模式的倒數是給「使用者要等外部節奏」的情境用的，hold-to-record
        本來就是使用者自己決定何時開口，倒數只會讓體感變慢。跳過 PROMPT
        也是同樣的理由：提示卡怎麼顯示是前端（C12）自己的事，不需要後端
        計時器參與，前端想顯示多久都可以，使用者準備好就直接按。

        `device_t_us` 應該是「按下當下最新收到的裝置 t_us」（呼叫端自己追蹤，
        跟 `tick()` 的 `device_t_us` 是同一種東西）。實際回溯 300ms 的
        pre-roll 是在 `hold_stop()` 才計算的，這裡只記錄起點。
        """
        if self.state != TrialState.IDLE:
            raise RuntimeError(f"不能在狀態 {self.state.value} 開始 hold-to-record，必須先回到 IDLE")
        if device_t_us is None:
            raise ValueError("hold_start 需要 device_t_us（按下當下最新收到的裝置 t_us）")
        self._apply_speaking_mode(speaking_mode)
        now = self._clock() if now is None else now
        self._current_label = label if label is not None else self._order[self._order_pos % len(self._order)]
        self._hold_start_device_t_us = int(device_t_us)
        return self._enter(TrialState.CAPTURE, now)

    def hold_stop(self, now: Optional[float] = None, device_t_us: Optional[int] = None) -> dict:
        """`POST /trial/hold/stop`。`device_t_us` 一樣是「放開當下最新收到
        的裝置 t_us」，**不是精確的裝置時間戳**——放開按鍵是主機端事件，
        沒有對應的裝置事件——這個近似會帶有序列傳輸延遲的偏差（量級跟
        `B04` 的 `clock_residual_p95` 差不多）。呼叫端／下游不要把它當成
        跟 ToF/mic 原生時間戳一樣精確。

        時長在 `[0.3, 5.0]` 秒內直接落盤（跟固定視窗模式一樣同步寫檔）。
        超出範圍**不自動存也不自動丟棄**——轉進 `CONFIRM` 狀態，資料留在
        記憶體，由 `confirm_keep()`/`discard_pending()` 決定（B12.md
        「超出範圍時警示並詢問是否保留」）。
        """
        if self.state != TrialState.CAPTURE or self._hold_start_device_t_us is None:
            raise RuntimeError("沒有進行中的 hold-to-record 可以結束")
        if device_t_us is None:
            raise ValueError("hold_stop 需要 device_t_us（放開當下最新收到的裝置 t_us）")
        now = self._clock() if now is None else now
        device_t_us = int(device_t_us)

        hold_duration_s = (device_t_us - self._hold_start_device_t_us) / 1e6
        self._capture_start_t_us = self._hold_start_device_t_us - HOLD_PRE_ROLL_US
        self._capture_end_t_us = device_t_us + HOLD_POST_ROLL_US
        self._hold_start_device_t_us = None

        if HOLD_MIN_DURATION_S <= hold_duration_s <= HOLD_MAX_DURATION_S:
            self.state = TrialState.SAVE
            save_event = self._do_save(now)
            save_event["hold_duration_s"] = hold_duration_s
            return [save_event, self._enter(TrialState.REST, now)]

        reason = "too_short" if hold_duration_s < HOLD_MIN_DURATION_S else "too_long"
        event = self._enter(TrialState.CONFIRM, now)
        event["warning"] = reason
        event["hold_duration_s"] = hold_duration_s
        return event

    def confirm_keep(self, now: Optional[float] = None) -> dict:
        """使用者在 `CONFIRM` 狀態選擇「還是要留」——把 `hold_stop()` 已經
        算好、暫存在記憶體的視窗正式落盤。跟 `tick()` 的 CAPTURE->SAVE 路徑
        一樣，落盤當下就前進詞指標，讓 SAVE/REST 事件能正確 peek 下一個詞。"""
        if self.state != TrialState.CONFIRM:
            raise RuntimeError(f"狀態 {self.state.value} 沒有待確認的 trial")
        now = self._clock() if now is None else now
        self.state = TrialState.SAVE
        self._order_pos += 1
        return [self._do_save(now), self._enter(TrialState.REST, now, next_label=self.peek_next_label())]

    def discard_pending(self, now: Optional[float] = None) -> dict:
        """使用者在 `CONFIRM` 狀態選擇「不要」——完全不落盤，**跳過**這個詞
        （語意比照 `abort()`：這次的嘗試不算數，換下一個）。"""
        if self.state != TrialState.CONFIRM:
            raise RuntimeError(f"狀態 {self.state.value} 沒有待確認的 trial 可以丟棄")
        return self._cancel_confirm(now)

    def mark_current_trial_saved_quality(self, h5_path, trial_idx: int, quality: str) -> None:
        """事後把**已經落盤**的 trial 標成別的 quality（例如使用者事後回看
        覺得這筆不能用）。這不是狀態機本身的一部分——狀態機只管「錄的當下」
        的 abort/redo；`C14`（重錄/棄用 UI）要的是「已存檔之後」還能改標記，
        兩者是不同時間點的操作，所以是獨立的函式，不是狀態機方法。
        """
        _mark_trial_quality(h5_path, trial_idx, quality, self._manifest_path, self._manifest_root)

    # -- 內部 -----------------------------------------------------

    def _enter(self, state: TrialState, now: float, **overrides) -> dict:
        self.state = state
        self._state_entered_at = now
        return self._event(**overrides)

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
        # REST 排除是 C12 這輪補的：REST 代表這個 trial 已經在 tick() 的
        # CAPTURE 分支裡 _do_save() 存過、詞指標也已經前進了——這時候呼叫
        # abort/redo 沒有東西可以「取消」（資料已經是既成事實），而且如果
        # 誤用還會讓詞指標被重複前進一次（`advance_word=True` 時），安靜地
        # 多跳過一個詞。事後要棄用已存檔的 trial 用 `mark_current_trial_saved_quality()`。
        if self.state in (TrialState.IDLE, TrialState.CONFIRM, TrialState.REST):
            reason = {
                TrialState.CONFIRM: "；用 confirm_keep()/discard_pending() 代替",
                TrialState.REST: "；這個 trial 已經存檔了，用 mark_current_trial_saved_quality() 事後改標記",
            }.get(self.state, "")
            raise RuntimeError(f"狀態 {self.state.value} 不能用 abort/redo 取消{reason}")
        return self._cancel_to_idle(now, advance_word)

    def _cancel_confirm(self, now: Optional[float]) -> dict:
        # discard_pending() 已經在呼叫前確認過 state == CONFIRM 了。
        return self._cancel_to_idle(now, advance_word=True)

    def _cancel_to_idle(self, now: Optional[float], advance_word: bool) -> dict:
        now = self._clock() if now is None else now
        aborted_label = self._current_label
        idx = self._next_trial_idx

        self._capture_start_t_us = None
        self._capture_end_t_us = None
        self._hold_start_device_t_us = None
        self._current_label = None
        if advance_word:
            self._order_pos += 1

        event = self._enter(TrialState.IDLE, now, next_label=self.peek_next_label())
        event["idx"] = idx
        event["aborted_label"] = aborted_label
        return event

    def _raw_events_window(self, start_us: int, end_us: int) -> List[dict]:
        """B21：某個 trial 的 capture 視窗內、給 `host.vad.*` 用的原始事件
        （`detect_from_events()` 要吃這個形狀，不是 `AlignedFrame`）。

        還沒有任何呼叫端使用這個——`measure_lip_lead()` 的實際呼叫是 B21
        階段 3（要動 `TrialStateMachine.__init__` 的簽章，需要先跟
        `/trial/*` wiring 協調），這裡先把「保留＋能正確切出某個 trial 的
        視窗」這件事做好、鎖進測試。
        """
        return [e for e in self._raw_events if start_us <= e["t_us"] <= end_us]

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

        mel_window = [
            e for e in self._mel_events
            if self._capture_start_t_us <= e[0] <= self._capture_end_t_us
        ]
        if mel_window:
            mel = np.array([e[1] for e in mel_window], dtype=np.float32)
            mel_t_us = np.array([e[0] for e in mel_window], dtype=np.int64)
        else:
            # mel 是選填的（$F 可能被 MEL:0 關掉），沒收到就不傳——
            # write_trial() 的 mel/mel_t_us 要嘛同時給、要嘛同時省略。
            mel = None
            mel_t_us = None

        idx = self._next_trial_idx
        quality = classify_quality(valid_zone_ratio, drop_count)

        # B21：真的呼叫 B15/B16，餵原始事件串（_raw_events_window()，不是
        # AlignedFrame）與建構時給的 baseline/底噪統計。只用 sensor A 餵唇動
        # 偵測——B21.md 自己給的介面範例只示範了單一感測器，沒有提兩顆怎麼
        # 融合，這裡照給的範例做，不是自己發明一個融合策略。
        #
        # energy_mu/energy_sigma 從 baseline 期間算好傳進來（host/storage/
        # baseline.py 的 evaluate_baseline()，跟這裡用同一組
        # zone_energy()/estimate_energy_floor()，不是抄一份）——沒給
        # （例如舊測試沒傳）就讓 detect_lip_activity() 自己用
        # estimate_energy_floor() 從這筆 trial 的資料估，精度差一點但不會
        # 壞掉（B16 量過：自估比 baseline 算好的偏嚴約 23%）。
        #
        # excluded_zones 也沒有帶 ZoneQualityReport 進來排除已知壞掉的
        # zone——B21.md 的建構子簽章範圍只列了 baseline_mu/sigma，沒有列
        # quality report，這裡沒有跟著多加一個沒被要求的參數。
        raw_window = self._raw_events_window(self._capture_start_t_us, self._capture_end_t_us)
        lips = detect_lips(
            raw_window, self._baseline_mu_A, self._baseline_sigma_A, sensor="A",
            energy_mu=self._energy_mu, energy_sigma=self._energy_sigma,
        )
        voice = detect_voice(
            raw_window, self._noise_floor_mu, self._noise_floor_sigma,
            speaking_mode=self._speaking_mode,
        )
        lead = measure_lip_lead(lips, voice)
        vad_attrs = lead.to_trial_attrs()  # 四個都可能是 None，見 to_trial_attrs() 的文件字串
        # `vad_confidence`：CONTRACTS.md §2 說「B15 的端點偵測信心度；silent
        # 模式為 None」——直接沿用 VadResult.to_dict() 自己的 vad_confidence
        # 定義（primary 是 None 就是 None），不重寫一份可能跟它不一致的邏輯。
        vad_confidence = voice.to_dict()["vad_confidence"]

        self._writer.write_trial(
            idx, label=self._current_label,
            tof_A=tof_A, tof_B=tof_B, tof_t_us=tof_t_us,
            tof_valid_A=tof_valid_A, tof_valid_B=tof_valid_B,
            mic_rms=mic_rms, mic_peak=mic_peak, mic_t_us=mic_t_us,
            # mel/mel_t_us 各自獨立的時間軸（CONTRACTS.md §2，B07 已更新
            # write_trial() 不再要求跟 mic_t_us 等長），沒有 mel 資料
            # （$F 被 MEL:0 關掉，或這個 capture 視窗剛好沒收到）就是 None，
            # quality 判定不看它是否存在（見 classify_quality 的文件字串）。
            mel=mel, mel_t_us=mel_t_us,
            wear_id=self._wear_id, mode=self._mode,
            valid_zone_ratio=valid_zone_ratio, drop_count=drop_count,
            speaking_mode=self._speaking_mode, vad_confidence=vad_confidence,
            quality=quality, **vad_attrs,
        )
        add_session(self._session_h5_path, self._manifest_path, root=self._manifest_root)

        self._next_trial_idx += 1
        return self._event(
            idx=idx, quality=quality, valid_zone_ratio=valid_zone_ratio,
            drop_count=drop_count, n_frames=int(tof_A.shape[0]),
            # 呼叫端（tick()/confirm_keep()）已經在呼叫這裡之前把 _order_pos
            # 前進過了，所以此刻 peek 到的就是「下一個」，不是剛存的這個。
            next_label=self.peek_next_label(),
            # B21：measure_lip_lead() 的 comparable 旗標（兩邊的門檻 σ 不一致
            # 就是 False，"不能拿不可比的數字寫結論"）——CONTRACTS.md 目前
            # 沒有 HDF5 attr 給它落腳（grep 過 session_writer.py 沒有），加
            # 一個是 host/storage/ 的改動，不在這個 story 列的授權路徑內，
            # 而且 session_writer.py 剛被 18 改過。先讓它至少在即時的 trial
            # SSE 事件裡看得到，完成回報裡已標成需要調度員排 session_writer.py
            # 的一個小改動才能落盤保留。
            vad_comparable=lead.comparable,
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
