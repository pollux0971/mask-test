"""實驗 C：六種模態組合的 Silhouette 分數比較。

規格見 `stories/D-analysis/D13.md`。回答的問題（`ssi-backlog/README.md`
「三個絕對不能省的 Story」那節講得最白）：

    ToF 到底有沒有帶來資訊，還是全部準確率都來自麥克風？

輸入是 `D03`（`analysis/features/feature_assembly.py`）組裝好的
`FeatureSeq.data`（T=24 固定長度版，104 維：[0:32] tof_A、[32:64] tof_B、
[64:104] mel，見 CONTRACTS.md §3.3）。本模組不重算任何特徵，只在已組裝
好的序列上做 StandardScaler -> PCA(50) -> silhouette_score 這條標準流程。

**「ToF_L」「ToF_R」的左右對應是報告用的標籤，不是硬體事實。** CONTRACTS.md
沒有規定 tof_A/tof_B 誰是左誰是右——這裡假設 A=L、B=R 純粹是為了讓報告
可讀，數學上兩者對稱，標反了不影響任何分數或判定，只影響文字說明指向
哪個實體感測器。若之後 E 軌確認方向相反，只需要改本檔的 TOF_L/TOF_R
兩個 slice 對應，其餘全部不用動。

「開發完成不等於有真實結果」（README 同節）：這支腳本用假資料開發並測過
就是完整交付；真實結論要等 `E05` 蒐集資料後才能填。`format_report()` 的
輸出會明確標示 `is_synthetic`，避免假資料的分數被誤讀成真實結論。
"""
import numpy as np

MODALITIES = {
    "tof_l": slice(0, 32),
    "tof_r": slice(32, 64),
    "tof_combined": slice(0, 64),
    "mel": slice(64, 104),
    "all": slice(0, 104),
}

DEFAULT_PCA_COMPONENTS = 50
DEFAULT_COMPLEMENTARITY_MARGIN = 0.05

# 判定門檻（照簡報）：> 0.5 強分離 / 0.3-0.5 標準通過 / 0.15-0.3 邊緣可分 / < 0.15 失敗
_VERDICT_THRESHOLDS = (
    (0.5, "strong"),
    (0.3, "standard_pass"),
    (0.15, "marginal"),
    (float("-inf"), "fail"),
)


def verdict_for_score(score):
    """把一個 Silhouette 分數對應到四級判定字串。"""
    for threshold, label in _VERDICT_THRESHOLDS:
        if score >= threshold:
            return label
    raise AssertionError("unreachable: 最後一個門檻是 -inf，一定會匹配到")


def stack_modality(feature_seqs, modality):
    """把多筆 `FeatureSeq.data`（每筆 (T, 104)）攤平堆疊成單一模態的矩陣。

    feature_seqs: 長度 N 的序列，每個元素是 (T, 104) 的陣列（同一個 T，
                  因為都是 D03 重採樣後的固定長度版）
    modality: `MODALITIES` 的其中一個 key

    回傳 (N, T * D_modality) 矩陣，每列是一筆 trial 攤平後的向量。
    """
    if modality not in MODALITIES:
        raise ValueError(f"未知的 modality '{modality}'，應為 {sorted(MODALITIES)} 其中之一")
    sl = MODALITIES[modality]

    rows = []
    t_ref = None
    for i, fs in enumerate(feature_seqs):
        arr = np.asarray(fs, dtype=np.float64)
        if arr.shape[-1] != 104:
            raise ValueError(f"feature_seqs[{i}] 最後一維應為 104，收到 {arr.shape[-1]}")
        if t_ref is None:
            t_ref = arr.shape[0]
        elif arr.shape[0] != t_ref:
            raise ValueError(
                f"feature_seqs[{i}] 的 T={arr.shape[0]} 與前面的 T={t_ref} 不一致——"
                "六種組合要在同一批固定長度 trial 上比較，T 必須一致"
            )
        rows.append(arr[:, sl].reshape(-1))

    return np.stack(rows, axis=0)


def silhouette_for_modality(feature_seqs, labels, modality, n_pca_components=DEFAULT_PCA_COMPONENTS):
    """StandardScaler -> PCA -> silhouette_score，單一模態組合。

    PCA 的實際維度會被夾在 [1, n_pca_components] 內，同時不超過
    `min(n_samples - 1, n_features)`（資料量不足 50 維可用時的正常情況，
    尤其是本模組自己的假資料測試）。**實際用掉的維度會記在回傳值的
    `n_components` 裡**——D13 規格明講這個值要記錄，因為它會影響結果。

    回傳 dict: {"score", "n_components", "n_samples", "n_classes"}
    """
    from sklearn.decomposition import PCA
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler

    X = stack_modality(feature_seqs, modality)
    labels = np.asarray(labels)
    if labels.shape[0] != X.shape[0]:
        raise ValueError(f"labels 長度 {labels.shape[0]} 與 trial 數 {X.shape[0]} 不一致")

    n_samples, n_features = X.shape
    n_classes = len(np.unique(labels))
    if n_samples < 3:
        raise ValueError(f"至少需要 3 筆 trial 才能算 silhouette，收到 {n_samples}")
    if n_classes < 2:
        raise ValueError(f"至少需要 2 個類別才能算 silhouette，收到 {n_classes}")

    X_scaled = StandardScaler().fit_transform(X)
    n_components = max(1, min(n_pca_components, n_samples - 1, n_features))
    X_pca = PCA(n_components=n_components).fit_transform(X_scaled)

    score = float(silhouette_score(X_pca, labels))
    return {
        "score": score,
        "n_components": n_components,
        "n_samples": n_samples,
        "n_classes": n_classes,
    }


def silhouette_table(feature_seqs, labels, n_pca_components=DEFAULT_PCA_COMPONENTS):
    """單一 mode（normal/whisper/silent 其中一種）的五種模態組合分數。

    回傳 dict: modality -> silhouette_for_modality() 的結果。
    """
    return {
        modality: silhouette_for_modality(feature_seqs, labels, modality, n_pca_components)
        for modality in MODALITIES
    }


def complementarity_check(table, margin=DEFAULT_COMPLEMENTARITY_MARGIN):
    """雙矩陣互補性判定：S(Combined) > max(S(L), S(R)) + margin。

    table: `silhouette_table()` 的回傳值（單一 mode）。
    """
    s_l = table["tof_l"]["score"]
    s_r = table["tof_r"]["score"]
    s_combined = table["tof_combined"]["score"]
    passed = s_combined > max(s_l, s_r) + margin
    return {
        "s_l": s_l,
        "s_r": s_r,
        "s_combined": s_combined,
        "margin": margin,
        "passed": bool(passed),
    }


def tof_vs_mel_gap(table):
    """ToF_Combined 與 Mel 的分數差距（第六種「組合」：不是新的模態子集，
    是既有兩個分數的比較——見本檔開頭與 D13.md「六種組合」的第 6 項）。

    正值代表 ToF 比 Mel 更能分開類別；這正是「準確率是不是全部來自麥克風」
    這個問題的量化版本。
    """
    return table["tof_combined"]["score"] - table["mel"]["score"]


def silhouette_report(trials_by_mode, n_pca_components=DEFAULT_PCA_COMPONENTS,
                       margin=DEFAULT_COMPLEMENTARITY_MARGIN, is_synthetic=True):
    """完整報告：三種 mode 各一份分數表 + 互補性判定 + ToF vs Mel 差距。

    trials_by_mode: dict，key 是 mode 名稱（例如 "normal"/"whisper"/"silent"），
                     value 是 (feature_seqs, labels) tuple。

    is_synthetic: True 時報告會明確標示「假資料，非真實結論」（README「開發
    完成不等於有真實結果」）——`E05` 資料到位、換成真實 trial 之前，呼叫端
    應該保持 True，不要為了「看起來像有結論」就標 False。

    回傳 dict:
        {"is_synthetic": bool,
         "modes": {mode: {"table": ..., "complementarity": ..., "tof_vs_mel_gap": ...}},
         "verdicts": {mode: {modality: verdict_str}}}
    """
    modes = {}
    verdicts = {}
    for mode, (feature_seqs, labels) in trials_by_mode.items():
        table = silhouette_table(feature_seqs, labels, n_pca_components)
        modes[mode] = {
            "table": table,
            "complementarity": complementarity_check(table, margin),
            "tof_vs_mel_gap": tof_vs_mel_gap(table),
        }
        verdicts[mode] = {m: verdict_for_score(r["score"]) for m, r in table.items()}

    return {"is_synthetic": is_synthetic, "modes": modes, "verdicts": verdicts}


def format_report(report):
    """把 `silhouette_report()` 的結果轉成人類可讀的 Markdown 字串。

    silent 模式的 ToF_Combined 分數會被特別標示（D13 驗收條件：「silent
    模式的 ToF 分數明確標示並討論」）——不出聲時 Mel 幾乎無資訊，此時的
    Silhouette 完全來自 ToF，是裝置能否用於「無聲介面」的直接證據。
    """
    lines = []
    if report["is_synthetic"]:
        lines.append("> ⚠️ **假資料（synthetic）產生的分數，不是真實結論。**"
                      " 真實結論待 `E05` 資料蒐集後重跑本模組取得。")
        lines.append("")

    lines.append("## Silhouette 分數表")
    lines.append("")
    modalities_order = ["tof_l", "tof_r", "tof_combined", "mel", "all"]
    header = "| 組合 | " + " | ".join(report["modes"].keys()) + " |"
    lines.append(header)
    lines.append("|---|" + "---|" * len(report["modes"]))
    for modality in modalities_order:
        row = [modality]
        for mode in report["modes"]:
            r = report["modes"][mode]["table"][modality]
            v = report["verdicts"][mode][modality]
            row.append(f"{r['score']:.3f} ({v})")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lines.append("## 雙矩陣互補性（S(Combined) > max(S(L), S(R)) + margin）")
    lines.append("")
    for mode, data in report["modes"].items():
        c = data["complementarity"]
        status = "通過" if c["passed"] else "未通過"
        lines.append(
            f"- **{mode}**: S(L)={c['s_l']:.3f}, S(R)={c['s_r']:.3f}, "
            f"S(Combined)={c['s_combined']:.3f}, margin={c['margin']} -> {status}"
        )
    lines.append("")

    lines.append("## ToF vs Mel 差距")
    lines.append("")
    for mode, data in report["modes"].items():
        lines.append(f"- **{mode}**: {data['tof_vs_mel_gap']:+.3f}")
    lines.append("")

    if "silent" in report["modes"]:
        s = report["modes"]["silent"]["table"]["tof_combined"]["score"]
        m = report["modes"]["silent"]["table"]["mel"]["score"]
        v = report["verdicts"]["silent"]["tof_combined"]
        lines.append("## silent 模式討論（最純粹的 ToF 測試）")
        lines.append("")
        lines.append(
            f"不出聲時 Mel 幾乎無資訊（S(Mel)={m:.3f}）。此時 S(ToF_Combined)={s:.3f}"
            f"（判定：{v}）完全來自 ToF——這是裝置能否用於「無聲介面」的直接證據。"
        )
        lines.append("")

    return "\n".join(lines)
