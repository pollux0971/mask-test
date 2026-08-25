"""B04 — 裝置時鐘 ↔ 主機時鐘的穩健線性對齊模型（CONTRACTS.md §1.1 / §1.3）。

裝置端 `t_us` 是 int64 µs，從開機 `esp_timer_get_time()` 算起；主機端收到
每一行的時刻是主機自己的時鐘。兩者的關係模型化成：

    host_us ≈ slope * device_us + offset_us

`slope` 反映晶振頻率誤差（通常 ~50 ppm），`offset_us` 反映開機時間差
（含常數線路延遲）。

**不能用普通最小平方法直接對全部樣本回歸。** UART 傳輸延遲是單邊的——
資料只會晚到，不會早到——OLS 會被排隊延遲往「晚」的方向拖偏，且偏移量
隨當下匯流排負載變動，不是常數，事後也無法補償。§1.3 對 PING 延遲已經
是同一個道理：「主機端統計時用最小值而非平均值」。

正確做法是「最小延遲估計」（跟 NTP/Chrony 的 minimum-filter 同一個道理）：
把樣本切成固定時間窗（bucket），每個 bucket 只留 `host_us - device_us`
最小的那個點——這個點的排隊延遲最接近 0，最接近「真實」的時鐘關係——
再對這些「最乾淨」的點做線性回歸。
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np

BUCKET_SECONDS = 1.0
SLOPE_TOLERANCE_PPM = 200.0       # 驗收條件：斜率落在 1 ± 200 ppm
RESIDUAL_ANOMALY_US = 20_000.0    # 驗收條件：拍手測試誤差 < 20 ms


@dataclass(frozen=True)
class ClockSample:
    """一組原始配對：裝置回報的 `t_us` 與主機收到這行當下的本地時刻（同單位，µs）。"""
    device_us: int
    host_us: int

    @property
    def delay_us(self) -> int:
        return self.host_us - self.device_us


@dataclass(frozen=True)
class ClockAlignment:
    slope: float
    offset_us: float
    residual_p95_us: float
    n_samples_used: int
    anomaly: bool
    anomaly_reason: Optional[str] = None

    def to_host_us(self, device_us) -> float:
        return self.slope * device_us + self.offset_us

    def to_meta(self) -> dict:
        """T02 schema `/meta` 要的三個欄位（CONTRACTS.md §2）。"""
        return {
            "clock_slope": self.slope,
            "clock_offset": self.offset_us,
            "clock_residual_p95": self.residual_p95_us,
        }


def _min_delay_per_bucket(samples, bucket_seconds=BUCKET_SECONDS):
    """把樣本依 `host_us` 分桶，每桶只留延遲最小的樣本，依桶序排好回傳。"""
    buckets = {}
    bucket_us = bucket_seconds * 1e6
    for s in samples:
        key = int(s.host_us // bucket_us)
        cur = buckets.get(key)
        if cur is None or s.delay_us < cur.delay_us:
            buckets[key] = s
    return [buckets[k] for k in sorted(buckets)]


def _fit_clean_samples(clean, slope_tolerance_ppm, residual_anomaly_us) -> ClockAlignment:
    if len(clean) < 2:
        raise ValueError(f"至少需要 2 個乾淨樣本（跨 2 個 bucket）才能回歸，收到 {len(clean)} 個")

    device_us = np.array([s.device_us for s in clean], dtype=np.float64)
    host_us = np.array([s.host_us for s in clean], dtype=np.float64)

    slope, offset = np.polyfit(device_us, host_us, deg=1)

    predicted = slope * device_us + offset
    residual_p95 = float(np.percentile(np.abs(host_us - predicted), 95))

    slope_ppm_error = abs(slope - 1.0) * 1e6
    anomaly, reason = False, None
    if slope_ppm_error > slope_tolerance_ppm:
        anomaly = True
        reason = f"slope 偏離 1 達 {slope_ppm_error:.1f} ppm（門檻 {slope_tolerance_ppm:.0f} ppm）"
    elif residual_p95 > residual_anomaly_us:
        anomaly = True
        reason = (f"residual_p95={residual_p95:.1f}us 超過門檻 {residual_anomaly_us:.0f}us"
                   "（可能有 clock-jump 之類的不連續，或取樣視窗涵蓋了不只一段連續時鐘關係）")

    return ClockAlignment(
        slope=float(slope), offset_us=float(offset), residual_p95_us=residual_p95,
        n_samples_used=len(clean), anomaly=anomaly, anomaly_reason=reason,
    )


def fit_clock_alignment(samples, bucket_seconds=BUCKET_SECONDS,
                         slope_tolerance_ppm=SLOPE_TOLERANCE_PPM,
                         residual_anomaly_us=RESIDUAL_ANOMALY_US) -> ClockAlignment:
    """一次性擬合：輸入 `ClockSample` 的 iterable，回傳 `ClockAlignment`。

    不會 raise 以外的例外（`ValueError` 之外的任何情況都不代表「擬合有問題」，
    而是模型本身的 bug）——`anomaly` 欄位才是「資料看起來有問題」的訊號，
    這樣呼叫端（B06）可以在 `--fault clock-jump` 這類情境下正常拿到一個
    帶著警告的結果，而不是整條 pipeline 因為一次跳變就崩潰。
    """
    clean = _min_delay_per_bucket(samples, bucket_seconds)
    return _fit_clean_samples(clean, slope_tolerance_ppm, residual_anomaly_us)


class ClockAligner:
    """持續累積 `(device_us, host_us)` 樣本的線上版本。

    每個 bucket 只保留目前看過延遲最小的樣本（O(1) amortized），`fit()`
    隨時可以用目前累積到的乾淨樣本重新擬合，`freeze()` 直接輸出 T02 schema
    `/meta` 要的三個欄位，供之後 B07 寫進 HDF5 session metadata。
    """

    def __init__(self, bucket_seconds=BUCKET_SECONDS):
        self.bucket_seconds = bucket_seconds
        self._buckets = {}

    def add_sample(self, device_us, host_us) -> None:
        sample = ClockSample(device_us=int(device_us), host_us=int(host_us))
        key = int(sample.host_us // (self.bucket_seconds * 1e6))
        cur = self._buckets.get(key)
        if cur is None or sample.delay_us < cur.delay_us:
            self._buckets[key] = sample

    @property
    def n_buckets(self) -> int:
        return len(self._buckets)

    def clean_samples(self):
        return [self._buckets[k] for k in sorted(self._buckets)]

    def fit(self, slope_tolerance_ppm=SLOPE_TOLERANCE_PPM,
            residual_anomaly_us=RESIDUAL_ANOMALY_US) -> ClockAlignment:
        return _fit_clean_samples(self.clean_samples(), slope_tolerance_ppm, residual_anomaly_us)

    def freeze(self) -> dict:
        return self.fit().to_meta()
