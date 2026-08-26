"""D15 — 從 HDF5 session 讀出各實驗要的東西（CONTRACTS.md §2）。

`run_all.py` 的資料層。刻意跟 `verification_report.py` 分開：報告的正確性
測得動而且不需要檔案，讀檔的部分需要真的檔案（用 `T02` 的
`ssi-backlog/tools/schema_example.py` 產生結構正確的樣本來測）。

## 一個 session 檔給不了全部六個實驗要的資料

這是 story 的 CLI（`--session <file.h5>`）與實驗需求之間的一個**真實缺口**：

| 實驗 | 需要 | 單一 session 有沒有 |
|---|---|---|
| `C0` 串擾 | 單顆開 vs 兩顆開的**兩次不同錄製** | ❌ 沒有，那是兩種擷取組態 |
| `A` SNR | baseline + round trials + spread trials | ⚠️ 要 trial 的 `label` 分得出 round/spread |
| `B` CV | **跨次戴**（多個 `wear_id`） | ⚠️ 單次 session 通常只有一個 `wear_id` |
| `C` Silhouette | 有標籤的 trials | ✅ |
| `E` Viseme | 有標籤的 trials | ✅ |

所以 `--session` 可以給多次，而**資料不足的實驗會被標成 `skipped` 並說明
缺什麼**——不是靜靜地不出現在報告裡。「沒跑」跟「通過」是兩件事，而一列
從報告裡消失之後，沒有人會發現它從來沒跑過。
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np

# §2：`tof_A` 是 (T, 2*Z)，前半距離、後半 signal/100。
DISTANCE_HALF = 0.5


@dataclass
class Trial:
    """一次 trial 的原始資料（§2 的 `/trial_NNN`）。"""

    key: str
    label: str
    wear_id: Optional[int]
    mode: Optional[str]
    speaking_mode: Optional[str]
    quality: Optional[str]
    tof_a: np.ndarray            # (T, 2Z) float，無效值是 NaN
    tof_b: np.ndarray
    tof_valid_a: np.ndarray      # (T, Z) bool
    tof_valid_b: np.ndarray
    tof_t_us: np.ndarray
    mic_rms: np.ndarray
    mel: Optional[np.ndarray]    # (F, 40) 或 None（選填）
    attrs: dict = field(default_factory=dict)

    @property
    def n_zones(self) -> int:
        return self.tof_a.shape[1] // 2


@dataclass
class SessionData:
    """一個 session 檔的內容。"""

    path: Path
    meta: dict
    trials: List[Trial]

    @property
    def wear_ids(self):
        ids = {t.wear_id for t in self.trials if t.wear_id is not None}
        if self.meta.get("wear_id") is not None:
            ids.add(int(self.meta["wear_id"]))
        return sorted(ids)

    @property
    def labels(self):
        return sorted({t.label for t in self.trials if t.label})

    def baseline(self, sensor="A"):
        """`(mu, sigma)`，各 (2Z,)。缺任一個就回 `(None, None)`——**不補值**，
        缺 baseline 就是不能算 z-score，補一個猜的會讓下游安靜地算錯。"""
        mu = self.meta.get(f"baseline_mu_{sensor}")
        sigma = self.meta.get(f"baseline_sigma_{sensor}")
        if mu is None or sigma is None:
            return None, None
        return np.asarray(mu, dtype=np.float64), np.asarray(sigma, dtype=np.float64)


def _as_scalar(value):
    """HDF5 的 attr 可能是 numpy 純量或 bytes，統一成 Python 型別。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value
    return value


def load_session(path):
    """讀一個 HDF5 session。缺 `/meta` 或完全沒有 trial 都會 `ValueError`
    ——那不是「資料不足以跑某個實驗」，那是這個檔案根本不是 session。"""
    import h5py

    path = Path(path)
    with h5py.File(path, "r") as handle:
        if "meta" not in handle:
            raise ValueError(f"{path} 沒有 /meta group，不是一個 session 檔（§2）")
        meta = {k: _as_scalar(v) for k, v in handle["meta"].attrs.items()}

        trials = []
        for key in sorted(k for k in handle.keys() if k.startswith("trial_")):
            group = handle[key]
            attrs = {k: _as_scalar(v) for k, v in group.attrs.items()}
            trials.append(Trial(
                key=key,
                label=str(attrs.get("label", "")),
                wear_id=_opt_int(attrs.get("wear_id")),
                mode=attrs.get("mode"),
                speaking_mode=attrs.get("speaking_mode"),
                quality=attrs.get("quality"),
                tof_a=np.asarray(group["tof_A"], dtype=np.float64),
                tof_b=np.asarray(group["tof_B"], dtype=np.float64),
                tof_valid_a=np.asarray(group["tof_valid_A"], dtype=bool),
                tof_valid_b=np.asarray(group["tof_valid_B"], dtype=bool),
                tof_t_us=np.asarray(group["tof_t_us"], dtype=np.int64),
                mic_rms=np.asarray(group["mic_rms"], dtype=np.float64),
                mel=(np.asarray(group["mel"], dtype=np.float64)
                     if "mel" in group else None),
                attrs=attrs,
            ))

    if not trials:
        raise ValueError(f"{path} 裡沒有任何 /trial_NNN group（§2）")
    return SessionData(path=path, meta=meta, trials=trials)


def _opt_int(value):
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def usable_trials(sessions, *, require_quality=("ok", "low")):
    """把多個 session 的 trial 攤平。

    `quality == "rejected"` 的 trial 預設**排除**——那是人工標記為不可用的
    錄音，混進統計等於把已知的壞資料當好資料用。`require_quality=None`
    可以關掉這個過濾（例如想看被拒的到底長什麼樣）。
    """
    result = []
    for session in sessions:
        for trial in session.trials:
            if require_quality is not None and trial.quality not in require_quality:
                continue
            result.append((session, trial))
    return result


def availability(sessions):
    """哪些實驗跑得動、跑不動的原因是什麼。

    回傳 `{experiment_key: None | "缺什麼的說明"}`。`None` 代表資料齊全。
    """
    trials = usable_trials(sessions)
    labels = sorted({t.label for _, t in trials if t.label})
    wear_ids = sorted({t.wear_id for _, t in trials if t.wear_id is not None})
    has_baseline = all(
        session.baseline(sensor)[0] is not None
        for session in sessions for sensor in ("A", "B")
    ) and bool(sessions)

    missing = {}

    # C0 串擾：需要「單顆開」與「兩顆開」兩種擷取組態的錄製。session schema
    # 沒有記錄「這次錄的時候另一顆是開還是關」，所以單靠 session 檔判斷不了。
    missing["C0"] = (
        "串擾實驗需要「只開一顆」與「兩顆同時開」的兩次錄製對照；"
        "§2 的 session schema 沒有記錄擷取時另一顆感測器的開關狀態，"
        "無法從 session 檔判斷。請用 `exp_d10_crosstalk` 的專用流程單獨執行。"
    )

    if not has_baseline:
        for key in ("A", "C", "E"):
            missing[key] = "缺 /meta 的 baseline_mu_*/baseline_sigma_*（§2），無法算 z-score"

    if "A" not in missing:
        round_like = [x for x in labels if x]
        if len(round_like) < 2:
            missing["A"] = (
                f"SNR 需要兩種對照的唇形錄製（round vs spread），"
                f"目前只有 {len(round_like)} 種標籤：{round_like}"
            )

    if len(wear_ids) < 2:
        missing["B"] = (
            f"跨次戴 CV 需要至少 2 個不同的 wear_id，目前只有 {wear_ids or '0'} 個。"
            "請提供多次戴上錄製的 session（`--session` 可以給多個）。"
        )

    if len(labels) < 2 and "C" not in missing:
        missing["C"] = f"Silhouette 需要至少 2 個類別，目前只有 {labels}"
    if len(labels) < 2 and "E" not in missing:
        missing["E"] = f"Viseme 熱力圖需要至少 2 個詞，目前只有 {labels}"

    return {key: missing.get(key) for key in ("C0", "A", "B", "C", "E")}
