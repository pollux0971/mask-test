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
    mic_t_us: np.ndarray         # (M,) int64，跟 mic_rms/mic_peak 同軸，必填
    mel: Optional[np.ndarray]    # (F, 40) 或 None（選填）
    # §2 選填：`tof_ambient_A/B` 是 (Ta, Z)，無效 zone 已經是 NaN。
    # `D10` 明訂 ambient 是 crosstalk 最靈敏的指標。
    ambient_a: Optional[np.ndarray] = None
    ambient_b: Optional[np.ndarray] = None
    # 之前寫進 HDF5 卻沒有對應欄位可以讀出來的三個 dataset（供需對帳表，
    # SCHEMA_SUPPLY_DEMAND.md 的「結構性讀不到」）。`mel_t_us` 最要緊：
    # §2 規定它跟 `mel` 成對，`mel` 讀得到卻沒有時間軸，任何要對齊 mel
    # 幀與其他串流時間戳的分析都做不到。
    mic_peak: Optional[np.ndarray] = None        # (M,) int16
    mel_t_us: Optional[np.ndarray] = None        # (F,) int64，與 mel 成對
    ambient_t_us: Optional[np.ndarray] = None    # (Ta,) int64，與 ambient_a/b 成對
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
    def sensors_enabled(self):
        """`"AB"` / `"A"` / `"B"`，或 `None`（舊檔沒有這個欄位）。

        ⚠️ **`None` 是「未知」，不是 `"AB"`。** 猜 `AB` 會讓舊資料被錯誤配對
        進 `C0` 的串擾比較，而**結果看起來完全正常**——串擾的訊號本來就小，
        一組配錯的資料只會讓 Δ 看起來偏大或偏小，不會有任何跡象。
        """
        value = self.meta.get("sensors_enabled")
        if value is None:
            return None
        value = str(value).strip().upper()
        return value if value in ("AB", "A", "B") else None

    @property
    def sensors_confirmed(self):
        """感測器狀態是否經裝置確認過。**幾乎永遠是 `False`，而且那是刻意的。**

        `$STATUS` 有 `mel=`/`amb=` 但**沒有** `sens_a=`/`sens_b=`，所以
        `sensors_enabled` 的值是**主機端記的上次指令**，不是裝置回報的狀態
        （§4.1.2）。寫入端刻意寫死 `False` 來逼下游不能忽略這件事。

        欄位不存在時回 `False`——**「沒寫」不能當成「確認過」**。
        """
        return bool(self.meta.get("sensors_enabled_confirmed", False))

    @property
    def wear_ids(self):
        ids = {t.wear_id for t in self.trials if t.wear_id is not None}
        if self.meta.get("wear_id") is not None:
            ids.add(int(self.meta["wear_id"]))
        return sorted(ids)

    @property
    def labels(self):
        return sorted({t.label for t in self.trials if t.label})

    def stacked_tof(self, sensor="A"):
        """整個 session 所有 trial 的 ToF 串起來，回 `(distance, valid)`。

        `D10` 比的是**兩次錄製**的平均，不是逐筆 trial——crosstalk 是持續
        存在的干擾，不是某一個詞的性質。只取距離通道（前半）。
        """
        attr = "tof_a" if sensor == "A" else "tof_b"
        valid_attr = "tof_valid_a" if sensor == "A" else "tof_valid_b"
        blocks, valids = [], []
        for trial in self.trials:
            data = getattr(trial, attr)
            if data.size == 0:
                continue
            blocks.append(data[:, :trial.n_zones])
            valids.append(getattr(trial, valid_attr))
        if not blocks:
            return None, None
        return np.concatenate(blocks, axis=0), np.concatenate(valids, axis=0)

    def stacked_ambient(self, sensor="A"):
        """整個 session 的 ambient 串起來，或 `None`（§2 選填欄位）。"""
        attr = "ambient_a" if sensor == "A" else "ambient_b"
        blocks = [getattr(t, attr) for t in self.trials
                  if getattr(t, attr) is not None and getattr(t, attr).size]
        return np.concatenate(blocks, axis=0) if blocks else None

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
                mic_t_us=np.asarray(group["mic_t_us"], dtype=np.int64),
                mic_peak=np.asarray(group["mic_peak"], dtype=np.int64),
                mel=(np.asarray(group["mel"], dtype=np.float64)
                     if "mel" in group else None),
                mel_t_us=(np.asarray(group["mel_t_us"], dtype=np.int64)
                          if "mel_t_us" in group else None),
                ambient_a=(np.asarray(group["tof_ambient_A"], dtype=np.float64)
                           if "tof_ambient_A" in group else None),
                ambient_b=(np.asarray(group["tof_ambient_B"], dtype=np.float64)
                           if "tof_ambient_B" in group else None),
                ambient_t_us=(np.asarray(group["tof_ambient_t_us"], dtype=np.int64)
                              if "tof_ambient_t_us" in group else None),
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


@dataclass
class CrosstalkPair:
    """`C0` 串擾實驗要的一組對照：同一次戴上、一顆開 vs 兩顆開。"""

    wear_id: Optional[int]
    solo: SessionData          # sensors_enabled 是 "A" 或 "B"
    dual: SessionData          # sensors_enabled 是 "AB"
    confirmed: bool            # 兩邊的感測器狀態是否都經裝置確認過

    @property
    def solo_sensor(self):
        return self.solo.sensors_enabled

    def to_dict(self) -> dict:
        return {
            "wear_id": self.wear_id,
            "solo_path": str(self.solo.path),
            "dual_path": str(self.dual.path),
            "solo_sensor": self.solo_sensor,
            "confirmed": self.confirmed,
        }


def crosstalk_pairs(sessions):
    """從一批 session 裡配出 `C0` 需要的 (solo, dual) 組合。

    回傳 `(pairs, diagnosis)`。`diagnosis` 是一份**數得出來的**盤點，配不到
    時要靠它才知道該補錄什麼——「資料不足」這種訊息幫不上任何忙。

    ## 🔴 兩邊必須是同一個 `wear_id`

    跨次戴的兩組資料拿來比 crosstalk，會把**戴法差異**算成**串擾**。
    串擾的訊號本身就小（門檻是 2 mm），而重新戴一次造成的距離差可以輕易
    超過它——**配錯的結果看起來完全正常，只是 Δ 偏大**，沒有任何跡象。

    （`D12` 那邊有同構的教訓：跨詞的距離量的是「不同的詞長得不一樣」，
    跟戴法重複性無關。）

    ## `confirmed=False` 不擋，但要傳下去

    `sensors_enabled` 是主機端記的指令、不是裝置確認的狀態（§4.1.2），
    所以 `sensors_enabled_confirmed` 幾乎永遠是 `False`。**因此不能拿它當
    配對條件**——那會讓 `C0` 永遠跑不了。但也**不能靜默忽略**：旗標會跟著
    `CrosstalkPair` 傳到報告，讓讀的人知道這組配對建立在未經確認的狀態上。
    """
    by_wear = {}
    counts = {"AB": 0, "A": 0, "B": 0, "unknown": 0}
    for session in sessions:
        state = session.sensors_enabled
        counts[state if state else "unknown"] += 1
        if state is None:
            continue
        # 用 `/meta` 的 wear_id；沒有就用 trial 的（同一個 session 內應該一致）
        wear_id = _opt_int(session.meta.get("wear_id"))
        if wear_id is None:
            ids = session.wear_ids
            wear_id = ids[0] if len(ids) == 1 else None
        by_wear.setdefault(wear_id, {"AB": [], "solo": []})
        by_wear[wear_id]["AB" if state == "AB" else "solo"].append(session)

    pairs = []
    for wear_id, groups in sorted(by_wear.items(), key=lambda kv: (kv[0] is None, kv[0])):
        if wear_id is None:
            # wear_id 不明的 session 不配對：無法確認「同一次戴上」這個前提。
            continue
        for dual in groups["AB"]:
            for solo in groups["solo"]:
                pairs.append(CrosstalkPair(
                    wear_id=wear_id, solo=solo, dual=dual,
                    confirmed=solo.sensors_confirmed and dual.sensors_confirmed,
                ))

    diagnosis = {
        "n_sessions": len(sessions),
        "counts": counts,
        "n_pairs": len(pairs),
        "wear_ids_with_dual": sorted(w for w, g in by_wear.items()
                                     if w is not None and g["AB"]),
        "wear_ids_with_solo": sorted(w for w, g in by_wear.items()
                                     if w is not None and g["solo"]),
        "unpaired_wear_ids": sorted(
            w for w, g in by_wear.items()
            if w is not None and bool(g["AB"]) != bool(g["solo"])),
        "any_unconfirmed": any(not p.confirmed for p in pairs),
    }
    return pairs, diagnosis


def describe_crosstalk_gap(diagnosis) -> str:
    """配不到時，講清楚**現在有什麼、還缺什麼**。

    「資料不足」幫不上任何忙——使用者要的是「我該補錄什麼」。
    """
    counts = diagnosis["counts"]
    parts = [
        f"共 {diagnosis['n_sessions']} 個 session："
        f"兩顆都開（AB）{counts['AB']} 個、"
        f"只開一顆（A/B）{counts['A'] + counts['B']} 個、"
        f"沒有 sensors_enabled 欄位（未知）{counts['unknown']} 個"
    ]
    if counts["unknown"]:
        parts.append(
            f"⚠️ 那 {counts['unknown']} 個舊檔**不會被當成「兩顆都開」**——"
            "猜錯的話串擾結果會看起來完全正常但其實是錯的"
        )
    if diagnosis["unpaired_wear_ids"]:
        parts.append(
            f"wear_id {diagnosis['unpaired_wear_ids']} 只有其中一種組態，"
            "**同一次戴上要各錄一次**（solo 與 dual）才配得起來"
        )
    if not diagnosis["n_pairs"]:
        parts.append(
            "尚無可用配對。請在**同一次戴上**分兩段錄："
            "一段 `SENS:B=0`（只開 A）、一段兩顆都開"
        )
    return "；".join(parts) + "。"


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

    # C0 串擾：靠 `/meta` 的 `sensors_enabled`（§2）配對 solo/dual。
    pairs, crosstalk_diagnosis = crosstalk_pairs(sessions)
    missing["C0"] = None if pairs else describe_crosstalk_gap(crosstalk_diagnosis)

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
