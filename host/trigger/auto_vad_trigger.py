"""B13 — Auto-VAD 觸發：即時呼叫 `host/trial/state_machine.py`（B11/B12，
只 import 不編輯）的 `hold_start()`/`hold_stop()`，取代人工按住/放開麥克風
按鈕。測驗模式的體驗因此是「看到題目 → 直接念出來 → 立刻看到結果」，
中間不用插一個按鍵。

**跟 `host/vad/audio_vad.py`（B15）的差異：那是批次端點偵測**（錄完一段
回頭切出詞的起訖，給 DTW 比對用）；**這裡是即時**——每收到一幀 `$M` 就要
決定「現在該不該觸發」，不能等錄完再回頭看。兩者共用同一套「進入/離開
閾值 = 底噪 μ + kσ」計算（`thresholds_for()`，只 import 不重寫），但狀態
機是這裡自己維護的即時版本，跟 B15 的 `_scan_hysteresis()`（批次）是平行
的兩份實作，服務不同的時間軸（離線 vs. 即時）。

**silent 模式沒有音訊可用**——B15 對 `silent` 回 `applicable=False`，
**不是「跑了但沒找到」**，這裡完全比照分開處理：`trigger_source="audio"`
配 `speaking_mode="silent"` 在建構時就直接報錯（沒有退路，不能靜默地
「一直觸發失敗」），要嘛換 `trigger_source="tof"`／`"either"`。ToF 觸發源
餵的是 `B16`（尚未完成）算出來的訊號，這裡只定義 `push_tof_activity()`
這個介面，不管訊號怎麼來的。

**300ms pre-roll、超長/超短段落走 CONFIRM，都是 `host/trial/state_machine.py`
`hold_start()`/`hold_stop()` 內建的行為（B12 的 `HOLD_PRE_ROLL_US`/
`HOLD_MIN_DURATION_S`/`HOLD_MAX_DURATION_S`）——這裡完全不重算，只需要在
正確的時間點呼叫它們，回傳值原樣轉發給呼叫端（可能是 SAVE 事件，也可能是
CONFIRM 事件，取決於段落長度）。「靜音 400ms 才確認結束」是這裡自己的
即時觸發判斷（story 明訂的具體數字），跟 B15 批次版的 `HANGOVER_MS=200`
是兩個不同目的的數字，不要混為一談。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from host.trial.state_machine import TrialState
from host.vad.audio_vad import thresholds_for

TRIGGER_SOURCES = ("audio", "tof", "either")

# story 明訂的具體數字。
DEFAULT_COOLDOWN_MS = 800.0
DEFAULT_SILENCE_CONFIRM_MS = 400.0

# **不是 story 明訂的數字，是這裡自己補的。** B15 的批次版用「進入 3σ」
# 加上事後才可能做到的**置中**平滑（±半視窗看未來）把單幀誤觸機率從
# 0.13% 壓到 0.02%；即時版看不到未來，沒有這個平滑可用。純 3σ 單幀判斷
# 在 31.25Hz 下一分鐘有 ~1875 幀，期望誤觸次數 ≈ 1875 × 0.13% ≈ 2.4
# 次/分鐘，**超過驗收條件的 < 1 次/分鐘**。這裡改成「連續 confirm 這麼久
# 都在閾值以上才算數」（時間門檻，不是幀數——原因跟 B15 的掛延遲一樣：
# `$M` 會掉幀，用幀數在掉幀時會誤判）：兩次獨立誤觸機率 ≈ 0.13%² ≈
# 1.7×10⁻⁶，一分鐘期望誤觸 ≈ 0.003 次，遠低於門檻。
DEFAULT_MIN_ACTIVE_CONFIRM_MS = 64.0


class InvalidTriggerSourceError(ValueError):
    pass


class AudioTriggerNotApplicableError(ValueError):
    """`trigger_source="audio"` 但音訊 VAD 不適用（silent 模式，或缺底噪
    統計），而且沒有 `tof`/`either` 這種退路可以退。跟 B15 的
    `VadResult(applicable=False)` 是同一個精神：不適用就要明確地報出來，
    不要讓呼叫端誤以為「一直沒觸發」是偵測失敗。"""


@dataclass(frozen=True)
class TriggerConfig:
    trigger_source: str = "audio"
    speaking_mode: str = "normal"
    cooldown_ms: float = DEFAULT_COOLDOWN_MS
    silence_confirm_ms: float = DEFAULT_SILENCE_CONFIRM_MS
    min_active_confirm_ms: float = DEFAULT_MIN_ACTIVE_CONFIRM_MS

    def __post_init__(self):
        if self.trigger_source not in TRIGGER_SOURCES:
            raise InvalidTriggerSourceError(
                f"trigger_source 必須是 {TRIGGER_SOURCES} 之一，收到 {self.trigger_source!r}"
            )


class AutoVadTrigger:
    """一次測驗回合（或一個 session）對應一個實例。呼叫端負責：
      * 即時把 `$M` 事件餵進 `push_mic(t_us, rms)`
      * `trigger_source` 是 `"tof"`／`"either"` 時，把 B16 的 ToF VAD 訊號
        餵進 `push_tof_activity(t_us, active)`
      * 把回傳的事件（`None` 或 `hold_start()`/`hold_stop()` 的回傳值）
        轉發成 SSE，跟 B12 手動觸發的事件走同一條路徑

    只有 `state_machine.state == TrialState.IDLE` 時才會真的呼叫
    `hold_start()`——這支自己檢查，不假設呼叫端會保證時機，因為自動觸發
    的訊號（音量/唇動）跟狀態機的節奏是兩條獨立的時間軸，隨時可能撞在一起
    （例如使用者同時也按了手動的 hold 按鈕）。
    """

    def __init__(
        self, state_machine, noise_floor_mu: Optional[float],
        noise_floor_sigma: Optional[float], config: Optional[TriggerConfig] = None,
    ):
        self._sm = state_machine
        self._config = config or TriggerConfig()

        wants_audio = self._config.trigger_source in ("audio", "either")
        audio_unusable = (
            self._config.speaking_mode == "silent"
            or noise_floor_mu is None or noise_floor_sigma is None
        )
        if wants_audio and audio_unusable and self._config.trigger_source == "audio":
            reason = (
                "silent 模式不使用音訊 VAD（B15 對此回 applicable=False）"
                if self._config.speaking_mode == "silent"
                else "缺少 session 底噪統計（/meta 的 noise_floor_mu/sigma）"
            )
            raise AudioTriggerNotApplicableError(
                f"trigger_source='audio' 但音訊觸發不適用：{reason}；"
                "silent 模式請改用 trigger_source='tof' 或 'either'"
            )

        self._audio_applicable = wants_audio and not audio_unusable
        if self._audio_applicable:
            self._enter_thr, self._exit_thr = thresholds_for(
                noise_floor_mu, noise_floor_sigma, speaking_mode=self._config.speaking_mode,
            )
        else:
            self._enter_thr = self._exit_thr = None

        self._audio_active = False
        self._tof_active = False
        self._in_segment = False
        self._pending_active_since_us: Optional[int] = None
        self._pending_silence_since_us: Optional[int] = None
        self._cooldown_until_us: Optional[int] = None

    @property
    def is_active(self) -> bool:
        """目前是否處於「已觸發、尚未結束」的段落中。"""
        return self._in_segment

    def _overall_active(self) -> bool:
        source = self._config.trigger_source
        if source == "audio":
            return self._audio_active
        if source == "tof":
            return self._tof_active
        return self._audio_active or self._tof_active  # either：聯集

    def push_mic(self, t_us: int, rms: float) -> Optional[dict]:
        if not self._audio_applicable:
            return None
        if not self._audio_active and rms > self._enter_thr:
            self._audio_active = True
        elif self._audio_active and rms < self._exit_thr:
            self._audio_active = False
        return self._evaluate(int(t_us))

    def push_tof_activity(self, t_us: int, active: bool) -> Optional[dict]:
        """`active`：B16 對這個時間點的判斷。這支不知道、也不需要知道
        B16 內部怎麼判斷唇動或接近。"""
        if self._config.trigger_source not in ("tof", "either"):
            return None
        self._tof_active = bool(active)
        return self._evaluate(int(t_us))

    def _evaluate(self, t_us: int) -> Optional[dict]:
        active = self._overall_active()

        if not self._in_segment:
            if self._cooldown_until_us is not None and t_us < self._cooldown_until_us:
                self._pending_active_since_us = None
                return None  # 冷卻期內，忽略新的觸發
            if not active:
                self._pending_active_since_us = None
                return None
            if self._pending_active_since_us is None:
                self._pending_active_since_us = t_us
                return None
            if (t_us - self._pending_active_since_us) < self._config.min_active_confirm_ms * 1000:
                return None
            if self._sm.state != TrialState.IDLE:
                # 狀態機正忙（手動觸發或別的流程），不要搶著觸發；狀態機
                # 空出來之後才重新起算 confirm 視窗，避免用很舊的上升沿
                # 時間戳去觸發（那會讓 pre-roll 往回推得離譜）。
                self._pending_active_since_us = t_us
                return None
            onset_us = self._pending_active_since_us
            self._in_segment = True
            self._pending_active_since_us = None
            self._pending_silence_since_us = None
            # 用**確認前**、真正的上升沿起點當觸發時間，不是確認完成的
            # 這一刻——confirm 視窗只是軟體判斷花的時間，捕捉到的資料
            # 範圍（含 hold_stop() 內建的 300ms pre-roll）要對齊真實的
            # 語音起點，不能因為軟體晚確認 64ms 就讓資料也跟著晚 64ms。
            return self._sm.hold_start(device_t_us=onset_us)

        # 已經在段落裡：等靜音確認
        if active:
            self._pending_silence_since_us = None
            return None
        if self._pending_silence_since_us is None:
            self._pending_silence_since_us = t_us
            return None
        if (t_us - self._pending_silence_since_us) < self._config.silence_confirm_ms * 1000:
            return None

        self._in_segment = False
        self._pending_silence_since_us = None
        self._cooldown_until_us = t_us + int(self._config.cooldown_ms * 1000)
        return self._sm.hold_stop(device_t_us=t_us)
