"""D21 — 快速閉集合相似度探針（純函式層；CLI/繪圖見
`analysis/experiments/exp_d21_closed_set_probe.py`）。

規格見 `ssi-backlog/stories/D-analysis/D21.md`。**這是 `E6` 完整流程的
簡化前導版，不是取代它**：每個選項只錄 1–3 筆，不做 LOOCV、不做拒識
校準（那些是 `D08`/`D06`/`E06` 的範圍）——存在的唯一理由是在投入 `E05`
的 4 小時資料蒐集之前，用幾分鐘知道 ToF 到底有沒有攜帶詞彙資訊。

**不重新發明距離轉分數這件事**：`D06`（`scoring.py`）的
`class_distances()`/`normalize_distances()` 已經是這裡要的「每類最近
距離、正規化」，這裡只加「三軌（ToF-only／Mel-only／Fused）分開算」跟
「排名（而不是機率）」這兩件 D21 特有的東西。

## 🔴 沒有 VAD 裁切，這個探針一定會「看起來沒有訊號」

一次錄音 3–4 秒，一個詞的實際發音只佔 400–600 ms（約 14%）。不裁切的話，
固定長度重採樣（D03，T=24）取的幀大部分落在「按著錄音鍵但已經講完話」
的靜音區間——兩筆不同詞的向量在這些幀上幾乎相同，真正承載語意差異的
幀被 86% 的靜音稀釋掉，8 個選項的分數會全部擠在 `1/N` 附近，看起來像
「完全沒有判別力」（`reports/ALIGNMENT_MISMATCH.md`「按住多久會不會
洩漏」章節量過同一個機制）。**所以這裡 `build_probe_vector()` 的
`trim` 預設是 `True`**——跟 `assemble_query_from_aligned_frames()`
本身的預設（`speech_window=None`，不裁切）刻意不同：那個函式是給所有
呼叫端共用的通用管線，不該替所有人決定要不要裁切；但 D21 這個功能
「裁切不是可選的前處理，是能不能成立的前提」（story 原文），所以在
D21 自己這一層把預設反過來。

## SIGMA_FLOOR

驗收條件明訂 `SIGMA_FLOOR = 1/√12`（不是 `1e-3`）。**確認過
`analysis/features/tof_features.py` 現有的 `SIGMA_FLOOR` 本來就是
`1.0 / 12 ** 0.5`**——這裡直接重用 `assemble_query_from_aligned_frames()`
→ `tof_features()` 這條既有路徑就自動滿足這條驗收條件，不需要另外
實作一份 z-score。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from analysis.experiments.exp_c_silhouette import MODALITIES
from analysis.similarity.build_templates_from_session import _aligner_for_trial
from analysis.similarity.cosine_baseline import cosine_dist, modality_cosine_dist
from analysis.similarity.euclidean_baseline import euclidean_dist, modality_euclidean_dist
from analysis.similarity.scoring import NORM_EPS, class_distances, normalize_distances
from host.features.live_pipeline import (
    SpeechWindowResult,
    assemble_query_from_aligned_frames,
    compute_speech_window,
)

DEFAULT_N_CANDIDATES = 8
DEFAULT_FUSE_W = 0.5
SEPARABLE_RATIO_THRESHOLD = 1.5  # story：> 1.5 才算真的可分
DIST_FNS = {"euclidean": euclidean_dist, "cosine": cosine_dist}
MODALITY_DIST_FNS = {
    "euclidean": modality_euclidean_dist,
    "cosine": modality_cosine_dist,
}


class NoSpeechDetectedError(ValueError):
    """兩種 VAD（唇動、語音）都偵測不到起訖時間——驗收條件明訂這裡要
    明確報錯要求重錄，不能靜默把整個錄音視窗當成語音（那正是「沒裁切」
    的稀釋陷阱換一個方式重演）。"""


def trial_speech_window(trial, **kwargs) -> SpeechWindowResult:
    """從 `session_loader.Trial.attrs`（`SessionWriter.write_trial()`
    寫入 HDF5 的 VAD 起訖時間戳，沒有被提升成具名欄位，只在 `.attrs`
    裡）組出 `compute_speech_window()` 要的 segments。

    唇動 A／唇動 B／語音，三者取聯集（唇動比出聲早是這個專案要量的東西，
    見 `compute_speech_window()` 的理由）；三個來源共用同一個
    `vad_end_us` 當終點——`write_trial()` 只存一個整體語音結束時間，
    沒有分唇動/語音各自的結束時間。`silent` 模式下 `voice_onset_us`
    是 `None`（B15/B16、CONTRACTS §2.2 的約定），會被
    `compute_speech_window()` 自動濾掉，等於「只用唇動」——不用在這裡
    特判 `silent` 模式，交集/聯集邏輯本來就會自然得到同樣的結果。
    """
    end = trial.attrs.get("vad_end_us")
    segments = [
        ("lip_A", trial.attrs.get("lip_onset_us_A"), end),
        ("lip_B", trial.attrs.get("lip_onset_us_B"), end),
        ("voice", trial.attrs.get("voice_onset_us"), end),
    ]
    return compute_speech_window(segments, **kwargs)


def require_speech_window(trial, **kwargs) -> SpeechWindowResult:
    """跟 `trial_speech_window()` 一樣，但 `source == "none"`（唇動、語音
    全部偵測不到）時丟 `NoSpeechDetectedError`——驗收條件明訂的「明確
    報錯要求重錄」。`compute_speech_window()` 本身對這個情況回傳
    `trimmed=False` 靜默退回整段（那是給其他呼叫端用的通用行為，`D21`
    自己要求更嚴格，所以在這一層加，不改 `compute_speech_window()`）。

    裁切窗太短（`source != "none"` 但 `window_us is None`）**不會**觸發
    這個例外——那種情況已經有偵測到的訊號，只是窗口不夠長，退回整段是
    合理的容錯，驗收條件也只針對「兩者都偵測不到」這一種情況要求硬性
    報錯。
    """
    result = trial_speech_window(trial, **kwargs)
    if result.source == "none":
        raise NoSpeechDetectedError(
            f"{trial.key}: 唇動（A/B）與語音 VAD 全部偵測不到起訖時間——"
            "無法判斷哪一段是語音本體，必須重錄，不能靜默使用整段錄音"
        )
    return result


def build_probe_vector(session, trial, trim=True, **speech_window_kwargs):
    """一筆 `session_loader.Trial` → 一個 (T,104) 探針用向量，跟
    `build_templates_from_session.build_template_vector()` 同一條
    `Aligner` + `assemble_query_from_aligned_frames()` 路徑，差別只有
    `trim`（見模組說明：D21 預設 `True`，跟其餘呼叫端的預設相反）。

    回傳 `(vector, speech_window_result_or_None)`；`trim=False` 時第二個
    元素是 `None`（沒有嘗試裁切，區別於「嘗試過但退回整段」）。
    """
    mu_A, sigma_A = session.baseline("A")
    mu_B, sigma_B = session.baseline("B")
    if mu_A is None or mu_B is None:
        raise ValueError(f"{trial.key}: session 缺 baseline mu/sigma，無法計算 ToF 特徵")

    aligner = _aligner_for_trial(trial)
    t_start = int(min(trial.tof_t_us[0], trial.mel_t_us[0]))
    t_end = int(max(trial.tof_t_us[-1], trial.mel_t_us[-1]))
    frames = list(aligner.frames(t_start, t_end))

    speech_window = require_speech_window(trial, **speech_window_kwargs) if trim else None
    query = assemble_query_from_aligned_frames(
        frames, mu_A, sigma_A, mu_B, sigma_B, speech_window=speech_window,
    )
    return query.data, speech_window


def pairwise_distance_matrix(vectors: Sequence[np.ndarray], dist_fn) -> np.ndarray:
    """可分性預檢：N 個向量兩兩距離，`(N,N)`，對角線恆為 0。

    story：「這比跑探針更有診斷價值——探針失敗時分不清是探針念得不好
    還是這些選項本來就不可分，預檢把後者單獨隔離出來」。跟哪個模態、
    哪個距離函式無關，呼叫端自己決定要傳整條 104 維還是切過的單一模態。
    """
    n = len(vectors)
    matrix = np.zeros((n, n), dtype=np.float64)
    for i in range(n):
        for j in range(i + 1, n):
            d = dist_fn(vectors[i], vectors[j])
            matrix[i, j] = d
            matrix[j, i] = d
    return matrix


def separability_ratio(templates_by_class: Dict[str, List[np.ndarray]], dist_fn) -> Dict[str, Optional[float]]:
    """每個類別的「可分性比值」= 最近的異類距離 / 同類自距離的中位數。

    story：「> 1.5 才算真的可分，比絕對距離有意義得多——絕對距離的尺度
    隨特徵維度變動，無法跨組態比較」。

    需要每個類別至少 2 筆樣板才能算「同類自距離」；只有 1 筆的類別回傳
    `None`（不是 0 或無限大——那兩個都會被誤讀成「非常可分」或「完全不
    可分」，`None` 才誠實代表「這個類別算不出這個數字」）。
    """
    ratios: Dict[str, Optional[float]] = {}
    for label, templates in templates_by_class.items():
        if len(templates) < 2:
            ratios[label] = None
            continue
        same_class_dists = [
            dist_fn(templates[i], templates[j])
            for i in range(len(templates)) for j in range(i + 1, len(templates))
        ]
        median_self = float(np.median(same_class_dists))

        others = {k: v for k, v in templates_by_class.items() if k != label}
        if not others:
            ratios[label] = None
            continue
        cross_dists = [
            dist_fn(t, o) for t in templates for others_list in others.values() for o in others_list
        ]
        nearest_cross = float(np.min(cross_dists))

        ratios[label] = (nearest_cross / median_self) if median_self > NORM_EPS else float("inf")
    return ratios


@dataclass(frozen=True)
class TrackRanking:
    """單一軌（ToF-only／Mel-only／Fused）、單一距離函式的排名結果。"""
    classes: List[str]
    d_raw: np.ndarray
    ranked: List[Tuple[str, float]] = field(compare=False)  # [(label, d_raw), ...] 由近到遠

    def rank_of(self, label: str) -> Optional[int]:
        """`label` 排第幾名（1-indexed）；不在候選清單裡回 `None`。"""
        for i, (candidate, _) in enumerate(self.ranked, start=1):
            if candidate == label:
                return i
        return None


def probe_three_track(query_vec, templates_by_class: Dict[str, List[np.ndarray]],
                       dist_method: str, w=DEFAULT_FUSE_W) -> Dict[str, TrackRanking]:
    """三軌排名：ToF-only／Mel-only／Fused。`templates_by_class` 每類可以
    是 1 筆或多筆（多筆時 `class_distances()` 內建取 min，對壞樣板穩健，
    見 story「建議每個選項錄 2–3 次」）。

    `dist_method`：`"euclidean"` 或 `"cosine"`（`DIST_FNS` 的 key）——
    **兩者都跑、並列輸出是驗收條件**，這個函式只算一種，
    `analysis.experiments.exp_d21_closed_set_probe` 的 CLI 負責跑兩次。

    融合：兩個模態的距離各自正規化後才加權（`w*tof_norm + (1-w)*mel_norm`）
    ——不正規化的話 `w=0.5` 實際上等於「幾乎只用其中一個」（跟
    `fusion.py`／`reports/DISTANCE_COMPARISON.md` 是同一個道理，D21 自己
    的 story 也把這句話原文寫了一遍）。
    """
    dist_fn = DIST_FNS[dist_method]
    slices = {"tof": MODALITIES["tof_combined"], "mel": MODALITIES["mel"]}

    tracks = {}
    d_by_modality = {}
    classes = None
    for modality in ("tof", "mel"):
        sl = slices[modality]
        wrapped_templates = {
            label: [t[:, sl] for t in templates] for label, templates in templates_by_class.items()
        }
        classes, d_raw = class_distances(query_vec[:, sl], wrapped_templates, dist_fn)
        d_by_modality[modality] = d_raw
        ranked = sorted(zip(classes, d_raw.tolist()), key=lambda kv: kv[1])
        tracks[modality] = TrackRanking(classes=classes, d_raw=d_raw, ranked=ranked)

    d_tof_norm = normalize_distances(d_by_modality["tof"])
    d_mel_norm = normalize_distances(d_by_modality["mel"])
    d_fused = w * d_tof_norm + (1 - w) * d_mel_norm
    ranked_fused = sorted(zip(classes, d_fused.tolist()), key=lambda kv: kv[1])
    tracks["fused"] = TrackRanking(classes=classes, d_raw=d_fused, ranked=ranked_fused)

    return tracks


def expected_random_rank(n_candidates: int) -> float:
    """N 個選項下，隨機猜的期望排名——`(N+1)/2`（story：N=8 時是 4.5）。"""
    return (n_candidates + 1) / 2.0


def format_probe_report(tracks_by_dist_method: Dict[str, Dict[str, TrackRanking]],
                         true_label: Optional[str], n_candidates: int) -> str:
    """把 `probe_three_track()`（對每個距離函式各跑一次的結果）轉成人類
    可讀的文字報告，格式跟 story 的範例對齊：三軌並列 + 排名 + 隨機基準。
    """
    lines = [f"探針結果：N={n_candidates}，隨機基準排名 {expected_random_rank(n_candidates):.1f}"]
    if true_label is not None:
        lines[0] += f"（實際念了「{true_label}」）"
    else:
        lines[0] += "（真實答案未知——只看排名分布，不評對錯）"

    for dist_method, tracks in tracks_by_dist_method.items():
        lines.append(f"\n=== 距離函式：{dist_method} ===")
        for track_name in ("tof", "mel", "fused"):
            track = tracks[track_name]
            lines.append(f"  [{track_name}]")
            for rank, (label, d) in enumerate(track.ranked, start=1):
                marker = " ★" if true_label is not None and label == true_label else ""
                lines.append(f"    {rank}. {label}  d={d:.3f}{marker}")
            if true_label is not None:
                rank = track.rank_of(true_label)
                lines.append(f"    -> 正確答案排名: {rank}/{n_candidates}"
                              f"（隨機基準 {expected_random_rank(n_candidates):.1f}）")
    return "\n".join(lines)
