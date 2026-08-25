"""D16 — 互信息分析：量化每個模態對分類的資訊貢獻（bit）。

規格見 `stories/D-analysis/D16.md`。跟 `D13`（Silhouette）回答的是同一個
問題的不同角度：Silhouette 看幾何分離度，互信息看統計相依性。兩者一致
才可信；不一致通常代表分布形狀不是球狀（Silhouette 用歐氏距離，對非球狀
分布不敏感，互信息不假設分布形狀）。

輸入跟 `D13`/`D18` 一樣是 `D03` 組裝好的 `FeatureSeq.data`。**重用 D13
的 `stack_modality()` 做模態欄位切法，不重寫。**

**要報告的關鍵數字：`I(ToF_Combined; label)` 與 `I(Mel; label)` 的比值。**
如果 ToF 只有 Mel 的 1/10，多模態的論證就站不住——這是 story 原文，也是
整個專案最需要盯著的一個數字。

## 單位：bit，不是 nats

`sklearn.feature_selection.mutual_info_classif` 回傳的是**自然對數（nats）**
單位的估計值，不是 bit——這是開發時實測校準過的：對一個幾乎無雜訊、完全
決定二元標籤的合成特徵，`mutual_info_classif` 回傳 `0.6932`，而
`ln(2) = 0.6931`（平衡二元標籤的熵剛好是 1 bit = ln(2) nats）。story 要求
的單位是 bit（「量化...資訊貢獻（bit）」「至少一模態 > 0.3 bit」），
所以本模組全程把 sklearn 的原始輸出除以 `ln(2)` 轉成 bit 再對外回傳；
`sklearn` 官方文件沒有明講單位，這個轉換係數是實測校準來的，不是猜的。

## 為什麼用「PCA 降維後逐維 MI 加總」而不是一次算多維聯合 MI

`mutual_info_classif` 是**逐特徵**估計（每一維各自對標籤的 MI），不是
一次估計整個特徵向量對標籤的聯合 MI——高維聯合 MI 的非參數估計在樣本數
有限時極不穩定（story 原文：「對高維輸入很慢且不穩定」）。做法是先用
PCA 把模態壓到 20-30 維（story 建議值），再對每一個主成分各自算 MI 後
加總，當作該模態的「資訊貢獻」估計。**這是近似值，會系統性高估真正的
聯合 MI**（加總等於假設各主成分之間對標籤的資訊貢獻互不重疊，PCA 讓
各成分互不相關，但「不相關」不等於「統計獨立」，非線性的共享資訊還是
可能被重複計入）。在模態之間比較相對大小（例如 ToF vs Mel 的比值）時
這個偏誤大致會同向抵消，不影響相對排序的解讀；但不要把加總後的絕對值
當成嚴謹的資訊論上界。

「開發完成不等於有真實結果」：跟 `D13`/`D18` 一樣，本模組用假資料開發並
測過就是完整交付，真實結論待 `E05`。
"""
import numpy as np

DEFAULT_PCA_COMPONENTS = 25  # story 建議 20-30
DEFAULT_RANDOM_STATE = 0
MI_PASS_THRESHOLD_BITS = 0.3  # 驗收條件：至少一模態 > 0.3 bit
_MEL_FLOOR_BITS = 1e-6  # ToF/Mel 比值分母保護


def _nats_to_bits(x):
    return np.asarray(x) / np.log(2)


def modality_mutual_information(feature_seqs, labels, modality,
                                  n_pca_components=DEFAULT_PCA_COMPONENTS,
                                  random_state=DEFAULT_RANDOM_STATE):
    """單一模態對標籤的互信息（bit），PCA 降維 + 逐主成分 MI 加總。

    回傳 dict: {"modality", "mi_bits", "per_component_mi_bits",
                "n_components", "n_samples", "passed"}
    `n_components` 是實際用掉的 PCA 維度（被夾在
    `min(n_pca_components, n_samples-1, n_features)`，理由跟 D13 一樣：
    資料量不足時要記錄實際用掉的值，不能默默改掉）。
    """
    from sklearn.decomposition import PCA
    from sklearn.feature_selection import mutual_info_classif
    from sklearn.preprocessing import StandardScaler

    from analysis.experiments.exp_c_silhouette import stack_modality

    X = stack_modality(feature_seqs, modality)
    y = np.asarray(labels)
    if y.shape[0] != X.shape[0]:
        raise ValueError(f"labels 長度 {y.shape[0]} 與 trial 數 {X.shape[0]} 不一致")
    if len(np.unique(y)) < 2:
        raise ValueError("至少需要 2 個類別才能算互信息")

    n_samples, n_features = X.shape
    n_components = max(1, min(n_pca_components, n_samples - 1, n_features))

    X_scaled = StandardScaler().fit_transform(X)
    X_pca = PCA(n_components=n_components, random_state=random_state).fit_transform(X_scaled)

    per_component_mi_nats = mutual_info_classif(
        X_pca, y, discrete_features=False, random_state=random_state)
    per_component_mi_bits = _nats_to_bits(per_component_mi_nats)
    mi_bits = float(np.sum(per_component_mi_bits))

    return {
        "modality": modality,
        "mi_bits": mi_bits,
        "per_component_mi_bits": per_component_mi_bits,
        "n_components": n_components,
        "n_samples": n_samples,
        "passed": bool(mi_bits > MI_PASS_THRESHOLD_BITS),
    }


def mutual_information_table(feature_seqs, labels,
                               n_pca_components=DEFAULT_PCA_COMPONENTS,
                               random_state=DEFAULT_RANDOM_STATE):
    """五種模態組合各自的互信息（驗收條件）。

    回傳 dict: modality -> `modality_mutual_information()` 的結果。
    """
    from analysis.experiments.exp_c_silhouette import MODALITIES

    return {
        modality: modality_mutual_information(feature_seqs, labels, modality,
                                                n_pca_components, random_state)
        for modality in MODALITIES
    }


def dual_matrix_gain(table):
    """雙矩陣資訊增益：`I(Combined) - max(I(L), I(R))`（驗收條件）。

    table: `mutual_information_table()` 的回傳值。
    """
    i_l = table["tof_l"]["mi_bits"]
    i_r = table["tof_r"]["mi_bits"]
    i_combined = table["tof_combined"]["mi_bits"]
    return {
        "i_l": i_l,
        "i_r": i_r,
        "i_combined": i_combined,
        "gain": i_combined - max(i_l, i_r),
    }


def tof_vs_mel_ratio(table):
    """`I(ToF_Combined; label) / I(Mel; label)`——story 點名的關鍵數字。

    `Mel` 的 MI 若非常接近 0（例如 silent 模式），分母加一個極小值下限，
    避免除以 0；此時比值會是一個很大的數字，這正是「Mel 幾乎無資訊」的
    量化結果，不是計算錯誤。
    """
    i_tof = table["tof_combined"]["mi_bits"]
    i_mel = table["mel"]["mi_bits"]
    return i_tof / max(i_mel, _MEL_FLOOR_BITS)


def format_report(table, is_synthetic=True):
    """把 `mutual_information_table()` 的結果轉成人類可讀的 Markdown 字串。"""
    lines = []
    if is_synthetic:
        lines.append("> ⚠️ **假資料（synthetic）產生的分數，不是真實結論。**"
                      " 真實結論待 `E05` 資料蒐集後重跑本模組取得。")
        lines.append("")

    lines.append("## 互信息分數表（bit）")
    lines.append("")
    lines.append("| 組合 | I(modality; label) [bit] | PCA 維度 | 判定 |")
    lines.append("|---|---|---|---|")
    modalities_order = ["tof_l", "tof_r", "tof_combined", "mel", "all"]
    for modality in modalities_order:
        r = table[modality]
        status = f"> {MI_PASS_THRESHOLD_BITS} bit" if r["passed"] else f"<= {MI_PASS_THRESHOLD_BITS} bit"
        lines.append(f"| {modality} | {r['mi_bits']:.3f} | {r['n_components']} | {status} |")
    lines.append("")

    gain = dual_matrix_gain(table)
    lines.append("## 雙矩陣資訊增益（I(Combined) - max(I(L), I(R))）")
    lines.append("")
    lines.append(
        f"I(L)={gain['i_l']:.3f} bit, I(R)={gain['i_r']:.3f} bit, "
        f"I(Combined)={gain['i_combined']:.3f} bit -> 增益 {gain['gain']:+.3f} bit"
    )
    if gain["gain"] < 0:
        lines.append("")
        lines.append(
            "> ⚠️ **負增益不能直接讀成「雙矩陣無互補性」。** 逐主成分 MI 加總"
            "隱含「各主成分對標籤的資訊互不重疊」的假設，PCA 只保證不相關、"
            "不保證統計獨立。負值可能是方法產物（見模組 docstring）。請以"
            " `D13` 的 Silhouette 互補性判定與 `D19` 的消融實驗交叉驗證，"
            "不要單憑此值下結論。"
        )
    lines.append("")

    ratio = tof_vs_mel_ratio(table)
    lines.append("## ToF vs Mel 比值（story 點名的關鍵數字）")
    lines.append("")
    lines.append(
        f"I(ToF_Combined)/I(Mel) = **{ratio:.2f}**。"
        f"如果這個比值遠小於 1（例如 ToF 只有 Mel 的 1/10），"
        "多模態的論證就站不住；比值 >= 1 代表 ToF 至少跟 Mel 一樣能提供"
        "關於標籤的資訊。"
    )
    lines.append("")

    return "\n".join(lines)
