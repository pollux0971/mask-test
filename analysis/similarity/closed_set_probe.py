"""D21 — 快速閉集合相似度探針。

使用者原話（2026-08-26，`ad` 轉述）：「給一段錄音，算它跟 8 個候選詞
各自有多像——一個探針，不是分類器」「我希望只要做到訊號驗證就好，
不需要訓練分類器」。

**這裡不重新發明距離轉分數這件事**——`D06`（`scoring.py`）的
`class_distances()`/`normalize_distances()`/`softmax_scores()` 已經是
「不訓練、每類最近距離、正規化後 softmax」這一整套，`probe_closed_set()`
只是在上面加兩件事：(1) 強制「閉集合，最多 8 個候選詞」這個使用場景
的邊界，不是開放式辨識；(2) 每個候選詞只吃**一筆**參考向量（不是
`RecognitionService` 那種「每類多筆樣板 + LOOCV/ROC 校準拒識門檻」），
因為這是訊號驗證階段的快速探針，不是正式 enrollment。

## 🔴 沒有 VAD 裁切，這個探針一定會「看起來沒有訊號」

一次錄音（hold-to-record）常常是 3.5 秒左右，但一個詞的實際發音只佔
400–600 ms（約 14%）。不裁切的話，固定長度重採樣（D03，T=24）取的 24
個幀裡，大部分落在「按著錄音鍵但已經講完話」的靜音區間——兩筆不同詞
的向量在這些幀上幾乎相同（都是靜音），真正承載語意差異的幀只佔一小
部分,被 86% 的靜音稀釋掉。結果是 8 個候選詞的分數會全部擠在
`1/8 ≈ 12.5%` 附近，看起來像「完全沒有判別力」——**這不是訊號真的
沒有，是裁切窗選錯了**（`reports/ALIGNMENT_MISMATCH.md`「按住多久會
不會洩漏」章節量過同一個機制）。

修法是 `host/features/live_pipeline.py` 的 `compute_speech_window()` +
`assemble_query_from_aligned_frames(..., speech_window=...)`
（`esp-mask-test-8f` 剛完成）——這裡直接用，不重寫一套裁切邏輯。
`build_probe_vector()` 就是「一筆 trial → 一個（可選是否裁切的）探針
向量」這個接線，跟 `build_templates_from_session.build_template_vector()`
同一條 Aligner 路徑，只是多了 `trim` 這個開關。
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from analysis.similarity.build_templates_from_session import _aligner_for_trial
from analysis.similarity.cosine_baseline import cosine_dist
from analysis.similarity.scoring import DEFAULT_TAU, class_distances, normalize_distances, softmax_scores
from host.features.live_pipeline import assemble_query_from_aligned_frames, compute_speech_window

MAX_CANDIDATES = 8  # 「閉集合」——使用者原話「8 個候選詞」


@dataclass(frozen=True)
class ProbeResult:
    """一次探針的結果。**不是分類判定**——沒有拒識、沒有信心門檻，
    呼叫端自己看分數判斷；跟 `RecognitionService.recognize()` 回傳的
    `TriResult` 刻意不同名、不同形狀，避免被誤用成正式辨識結果。
    """
    candidates: List[str]   # 候選詞順序，跟 d_raw/d_norm/scores 一一對應
    d_raw: np.ndarray       # 未正規化距離（dist_fn 自己的尺度）
    d_norm: np.ndarray      # normalize_distances() 之後的距離
    scores: np.ndarray      # softmax_scores(d_norm)，總和為 1
    top1: str
    trim: Optional[dict] = None  # compute_speech_window() 結果的 to_dict()；None = 這次呼叫沒有嘗試裁切

    def ranked(self):
        """依分數高到低排序，回傳 `[(word, score), ...]`。"""
        order = np.argsort(-self.scores)
        return [(self.candidates[i], float(self.scores[i])) for i in order]

    def score_spread(self):
        """`max(scores) - min(scores)`——判別力的一個簡單摘要：越接近 0
        代表分數越平（8 個候選詞幾乎分不出來，見模組說明的靜音稀釋陷阱），
        越大代表有明顯的贏家。不是驗收條件本身，是給人看報告用的摘要數字。
        """
        return float(self.scores.max() - self.scores.min())


def probe_closed_set(query_vec, candidate_vecs, dist_fn=cosine_dist, tau=DEFAULT_TAU, trim=None):
    """`query_vec`：一筆 (T,104) 特徵向量（例如 `build_probe_vector()` 的
    輸出）。`candidate_vecs`：`{候選詞: (T,104) 向量}`，每詞恰好一筆
    參考向量——**不訓練、不校準**，跟 `RecognitionService` 不是同一件事。

    `trim`：可選，呼叫端自己組出來的 `SpeechWindowResult`（給
    `query_vec`/`candidate_vecs` 用的裁切依據），這裡只是原樣存進
    `ProbeResult.trim`（`.to_dict()` 過）方便報告使用，不影響任何計算
    ——真正的裁切發生在向量組裝那一步（`build_probe_vector()`），
    這個函式吃的已經是組好的向量。
    """
    if not candidate_vecs:
        raise ValueError("candidate_vecs 不能是空的——沒有候選詞就無從比較")
    if len(candidate_vecs) > MAX_CANDIDATES:
        raise ValueError(
            f"閉集合探針最多支援 {MAX_CANDIDATES} 個候選詞，收到 {len(candidate_vecs)} 個"
            "——超過這個數字代表這其實是開放式辨識，不是這個工具的設計範圍"
        )

    templates_by_class = {word: [vec] for word, vec in candidate_vecs.items()}
    classes, d_raw = class_distances(query_vec, templates_by_class, dist_fn)
    d_norm = normalize_distances(d_raw)
    scores = softmax_scores(d_norm, tau=tau)
    top1 = classes[int(np.argmax(scores))]

    return ProbeResult(
        candidates=classes, d_raw=d_raw, d_norm=d_norm, scores=scores,
        top1=top1, trim=trim.to_dict() if trim is not None else None,
    )


def trial_speech_window(trial, **kwargs):
    """從 `session_loader.Trial.attrs`（HDF5 原始 attrs，`SessionWriter
    .write_trial()` 寫入的 VAD 時間戳沒有被提升成具名欄位，只在這裡）
    組出 `compute_speech_window()` 要的 segments，算出這筆 trial 的
    `SpeechWindowResult`。

    唇動（A/B）各自的起點 + 語音起點，取聯集（見 `compute_speech_window()`
    的理由：唇動比出聲早是這個專案要量的東西，不能用交集切掉）；三個
    來源共用同一個 `vad_end_us` 當終點——`write_trial()` 只存一個整體的
    語音結束時間，沒有分唇動/語音各自的結束時間。任何一個來源缺席
    （`None`）會被 `compute_speech_window()` 自動濾掉，不用在這裡先擋。
    """
    end = trial.attrs.get("vad_end_us")
    segments = [
        ("lip_A", trial.attrs.get("lip_onset_us_A"), end),
        ("lip_B", trial.attrs.get("lip_onset_us_B"), end),
        ("voice", trial.attrs.get("voice_onset_us"), end),
    ]
    return compute_speech_window(segments, **kwargs)


def build_probe_vector(session, trial, trim=False, **speech_window_kwargs):
    """一筆 `session_loader.Trial` → 一個 (T,104) 探針用向量，跟
    `build_templates_from_session.build_template_vector()` 同一條
    `Aligner` + `assemble_query_from_aligned_frames()` 路徑，唯一差別是
    `trim` 這個開關。

    `trim=False`（預設）：不裁切，整段錄音都用——**這是刻意保留的對照
    組**，用來示範不裁切會發生什麼（見模組說明的靜音稀釋陷阱），不是
    因為這是比較好的預設。
    `trim=True`：用 `trial_speech_window(trial, **speech_window_kwargs)`
    算出的視窗裁切後再組向量。

    回傳 `(vector, speech_window_result_or_None)`——`trim=False` 時第二個
    元素是 `None`（沒有嘗試裁切，見 `QueryAssembly.trim` 的說明：`None`
    跟「嘗試過但退回整段」是兩件不同的事）。
    """
    mu_A, sigma_A = session.baseline("A")
    mu_B, sigma_B = session.baseline("B")
    if mu_A is None or mu_B is None:
        raise ValueError(f"{trial.key}: session 缺 baseline mu/sigma，無法計算 ToF 特徵")

    aligner = _aligner_for_trial(trial)
    t_start = int(min(trial.tof_t_us[0], trial.mel_t_us[0]))
    t_end = int(max(trial.tof_t_us[-1], trial.mel_t_us[-1]))
    frames = list(aligner.frames(t_start, t_end))

    speech_window = trial_speech_window(trial, **speech_window_kwargs) if trim else None
    query = assemble_query_from_aligned_frames(
        frames, mu_A, sigma_A, mu_B, sigma_B, speech_window=speech_window,
    )
    return query.data, speech_window
