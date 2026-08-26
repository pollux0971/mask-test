"""D09 — 即時辨識服務（純邏輯層，不含 HTTP wiring）。

規格見 `stories/D-analysis/D09.md`；輸出形狀對應 CONTRACTS.md §4.3
`TriResult` JSON。**HTTP 層（`POST /recognize`、`GET /templates`）由
`esp-mask-test-ed` 接到 `bridge_server.py`**——這裡只寫背後的邏輯，
`RecognitionService` 的方法命名刻意對應兩個端點，方便串接時一目了然。

## 距離度量預設 cosine（D04），DTW（D05）可選

D04 批次餘弦實測 **0.147 ms**，D05 DTW **8-12 ms**——量級差了兩個數量級。
而且 D05 自己的合成資料 LOOCV 顯示 DTW 反而較差（56.2% vs 37.5%，
`D05` 完成報告已如實記錄這個負結果）。**沒有證據支持 DTW 更準**，
在這個前提下選 `cosine` 當預設對 Demo 反應速度更有利。
`E05` 真實資料齊全後應該重新跑一次 `exp_d05_dtw_vs_cosine.py` 複驗，
屆時如果 DTW 確實更準、且延遲仍在預算內，再考慮切換預設值——
`dist_method` 本來就是可調參數，不是寫死的。

## `w` 改變不重算距離

一次 `recognize()` 呼叫回傳的 `TriResult` 已經含正規化後的
`d_tof`/`d_mel`；前端可以自己對任意 `w` 呼叫 `TriResult.fuse(w)`/`top1(w)`，
不需要再打一次 `/recognize`——這個性質在 `D07` 已經驗證過
（`test_frontend_can_recompute_many_w_without_recomputing_distances`），
這裡的服務層設計不提供「指定 w 才回傳結果」的介面，保持這個性質成立。

## 樣板熱載入

`load_enrollment()` 換掉樣板 + 重新校準 `theta_reject_tof`/`theta_reject_mel`
（校準本身有計算成本，換樣板時做一次、快取起來，`recognize()` 呼叫時
直接用快取值——見 `fusion.compute_tri_result` 的 `precomputed_thresholds`
參數，這是本 story 為了這個目的替 D07 加的擴充，向後相容、不影響
既有呼叫端）。整個服務只是一個 Python 物件，換屬性不需要重啟任何東西，
中間沒有需要特別處理的「服務中斷」窗口。

## 拒識校準預設 `"roc"`（D22），`"loo_single"` 保留為對照

**這裡原本呼叫的是 D06/D08 的單邊 LOO 校準（`enrollment
.calibrate_tri_reject_thresholds`）**——`D09` 完成時 `D22` 還不存在。
`D22` 後來證明單邊 LOO 在真實規模（104 維、T=24）下有結構性缺陷：
誤拒率完全不隨樣板數改善（32%~37%，n=10 到 100 幾乎打平），雙邊 ROC
校準把同樣情境壓到 0%~1%。調度員已把雙邊 ROC 定為系統預設並寫進
CONTRACTS §4.3，這裡的 `reject_calibration_method` 預設改成 `"roc"`
——這是**極值統計量稽核**時發現的落差，不是這次稽核順手做的優化：
即時辨識服務原本仍在用已知有結構性缺陷的舊校準方法，跟系統其餘部分
（`D08`/`D22` 的完成報告、CONTRACTS 的決議）不一致。

`"loo_single"` 沒有刪除，因為 `D22` 自己的原則就是「舊方法保留當對照，
不刪除」——真實資料上的結論可能不同，需要能切換回去比較。
"""
import time

from analysis.similarity.cosine_baseline import cosine_dist
from analysis.similarity.dtw_baseline import dtw_dist
from analysis.similarity.enrollment import calibrate_tri_reject_thresholds
from analysis.similarity.fusion import compute_tri_result
from analysis.similarity.reject_calibration_roc import STRATEGY_EER, calibrate_tri_threshold_roc
from analysis.similarity.scoring import DEFAULT_TAU

DIST_FN_BY_NAME = {"cosine": cosine_dist, "dtw": dtw_dist}
DEFAULT_DIST_METHOD = "cosine"
DEFAULT_REJECT_PERCENTILE = 95.0  # 只有 reject_calibration_method="loo_single" 時使用

REJECT_CALIBRATION_ROC = "roc"
REJECT_CALIBRATION_LOO_SINGLE = "loo_single"
VALID_REJECT_CALIBRATION_METHODS = {REJECT_CALIBRATION_ROC, REJECT_CALIBRATION_LOO_SINGLE}
DEFAULT_REJECT_CALIBRATION_METHOD = REJECT_CALIBRATION_ROC  # D22：系統預設
DEFAULT_ROC_STRATEGY = STRATEGY_EER
DEFAULT_ROC_TARGET = 0.05


class RecognitionService:
    """即時辨識服務：吃已組裝好的特徵序列，回傳 `TriResult` + 分階段延遲。

    `query` 的長度慣例（T=24 固定長度 vs 原始長度）跟著 `dist_method` 走：
    `cosine` 配 D03 `FeatureSeq.data`（固定長度），`dtw` 配 `data_raw`
    （原始長度）——呼叫端（B06 對齊之後、D01-D03 組裝完的那一層）
    要傳對版本，這裡不做長度檢查/轉換，因為兩種距離函式各自的
    shape 要求已經在 D04/D05 自己驗證過了。
    """

    def __init__(self, templates_by_class, reject_templates, slices,
                 subject=None, wear_id=None, dist_method=DEFAULT_DIST_METHOD,
                 tau=DEFAULT_TAU,
                 reject_calibration_method=DEFAULT_REJECT_CALIBRATION_METHOD,
                 reject_percentile=DEFAULT_REJECT_PERCENTILE,
                 roc_strategy=DEFAULT_ROC_STRATEGY, roc_target=DEFAULT_ROC_TARGET):
        if reject_calibration_method not in VALID_REJECT_CALIBRATION_METHODS:
            raise ValueError(
                f"reject_calibration_method 必須是 {VALID_REJECT_CALIBRATION_METHODS} 之一，"
                f"收到 {reject_calibration_method!r}"
            )
        self._slices = slices
        self._tau = tau
        self._reject_calibration_method = reject_calibration_method
        self._reject_percentile = reject_percentile  # 只有 "loo_single" 用
        self._roc_strategy = roc_strategy            # 只有 "roc" 用
        self._roc_target = roc_target                # 只有 "roc" 用
        self._dist_method = None
        self._dist_fn = None
        self.set_dist_method(dist_method, _skip_recalibrate=True)
        self.load_enrollment(templates_by_class, reject_templates, subject=subject, wear_id=wear_id)

    def set_dist_method(self, name, _skip_recalibrate=False):
        """切換距離度量（`"cosine"` 或 `"dtw"`）。換方法後，已快取的
        `theta_reject_tof`/`theta_reject_mel` 是用舊距離函式校準的，
        尺度不再適用，必須重新校準。
        """
        if name not in DIST_FN_BY_NAME:
            raise ValueError(f"dist_method 必須是 {list(DIST_FN_BY_NAME)} 之一，收到 {name!r}")
        self._dist_method = name
        self._dist_fn = DIST_FN_BY_NAME[name]
        if not _skip_recalibrate and getattr(self, "_templates_by_class", None) is not None:
            self._recalibrate_thresholds()

    def load_enrollment(self, templates_by_class, reject_templates, subject=None, wear_id=None):
        """熱載入新的樣板組（不需要重啟服務）。對應「C20 的 enrollment
        完成後直接切換」。"""
        self._templates_by_class = templates_by_class
        self._reject_templates = reject_templates
        self._subject = subject
        self._wear_id = wear_id
        self._recalibrate_thresholds()

    def _recalibrate_thresholds(self):
        """校準方法由 `reject_calibration_method` 決定（見模組 docstring
        「拒識校準預設 roc」）：`"roc"`（D22，系統預設）或 `"loo_single"`
        （D06/D08，保留為對照，不刪除）。兩者回傳形狀不同，這裡統一成
        `_precomputed_thresholds`（餵給 `fusion.compute_tri_result`）與
        `_threshold_warnings`（`loo_single` 才有樣板數失衡警告；`roc`
        對此問題本身更穩健，沒有對應的警告機制，見 D22 完成報告）。
        """
        if self._reject_calibration_method == REJECT_CALIBRATION_ROC:
            thresholds = calibrate_tri_threshold_roc(
                self._templates_by_class, self._reject_templates, self._slices, self._dist_fn,
                strategy=self._roc_strategy, target=self._roc_target,
            )
            warnings = {"tof": None, "mel": None}
        else:
            thresholds = calibrate_tri_reject_thresholds(
                self._templates_by_class, self._reject_templates, self._slices, self._dist_fn,
                percentile=self._reject_percentile,
            )
            warnings = {"tof": thresholds["tof"]["warning"], "mel": thresholds["mel"]["warning"]}

        self._precomputed_thresholds = {
            "tof": thresholds["tof"]["theta"],
            "mel": thresholds["mel"]["theta"],
        }
        self._threshold_warnings = warnings
        self._calibration_stats = thresholds  # 完整資訊（frr/far/calibration_ms 等），供除錯/報告用

    def list_templates(self):
        """對應 `GET /templates`：列出已載入的樣板組。"""
        return {
            "subject": self._subject,
            "wear_id": self._wear_id,
            "dist_method": self._dist_method,
            "reject_calibration_method": self._reject_calibration_method,
            "classes": {label: len(ts) for label, ts in self._templates_by_class.items()},
            "n_reject_templates": len(self._reject_templates),
            "theta_reject_tof": self._precomputed_thresholds["tof"],
            "theta_reject_mel": self._precomputed_thresholds["mel"],
            "threshold_warnings": self._threshold_warnings,
        }

    def recognize(self, query, feature_latency_ms=None):
        """對應 `POST /recognize`。

        query: (T, 104) 完整特徵序列（B06 對齊、D01-D03 組裝完的結果，
               也可以是 B17 回放資料重建出來的同形狀序列——服務層
               不區分資料來源，這就是「可用回放資料測試」的意思）。
        feature_latency_ms: 可選，呼叫端量到的「特徵萃取」耗時（毫秒），
               併入回傳的 `latency_ms["feature"]`；這個函式本身只量測
               距離計算＋融合的耗時（`latency_ms["dist"]`）。

        回傳 (tri_result, latency_ms)。
        """
        t0 = time.perf_counter()
        tri = compute_tri_result(
            query, self._templates_by_class, self._reject_templates, self._slices,
            self._dist_fn, tau=self._tau, precomputed_thresholds=self._precomputed_thresholds,
        )
        dist_ms = (time.perf_counter() - t0) * 1000

        latency_ms = {
            "feature": feature_latency_ms if feature_latency_ms is not None else 0.0,
            "dist": dist_ms,
        }
        latency_ms["total"] = latency_ms["feature"] + latency_ms["dist"]
        return tri, latency_ms
