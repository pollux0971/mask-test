"""D17 — t-SNE 視覺化：各模態組合的 2D 投影圖，肉眼檢視類別是否形成 cluster。

規格見 `stories/D-analysis/D17.md`。輸入跟 `D13`/`D16`/`D18` 一樣是 `D03`
組裝好的 `FeatureSeq.data`。**重用 D13 的 `stack_modality()` 做模態欄位
切法，不重寫。**

**這是給人看的圖，不是統計證據——統計證據是 `D18`（permutation test）。**
Silhouette 0.2 到底長什麼樣，看圖 3 秒就知道；但「看起來分得開」不能拿
來宣稱顯著性，那是 D18 的工作。

## 兩個陷阱（一定要記得）

1. **t-SNE 的簇間距離沒有意義，只有簇內聚集有意義。** t-SNE 為了保留
   局部鄰域結構會扭曲全域距離，兩群在圖上離多遠、離多近都不是可靠的
   量化資訊——**不要**拿這張圖去回答「哪兩個詞比較像」。圖上只能看
   「這一群樣本聚不聚」，不能看「這一群跟那一群隔多遠」。

2. **perplexity 對結果影響極大，小樣本下特別敏感，必須可調且必須把用了
   多少記在圖上。** 這正是本模組每次都畫多個 perplexity 並列的原因
   （story 原文：「並列多個 perplexity 是誠實的做法——如果只有某個特定
   perplexity 才看得出分群，那個分群可能是假的」）。`compute_tsne_embedding()`
   會把 perplexity 依樣本數自動夾住（sklearn 硬性要求 `perplexity < n_samples`），
   **實際用掉的值記在回傳值裡並印在圖標題上**，跟 D13/D16/D18 記錄
   實際用掉的 PCA 維度/CV 折數是同一個理由。

## 圖表文字一律英文

跟 `d16`/`d18` 同一條規則（調度員訊息）：圖出得來、CJK 字型缺字是靜默
失敗（變方塊），跑圖的機器不保證有中文字型。中文討論留在
`reports/*.md`，這裡的圖表文字全部英文。
"""
import numpy as np

DEFAULT_PERPLEXITIES = (15, 30)
DEFAULT_RANDOM_STATE = 0
DEFAULT_DPI = 300


def compute_tsne_embedding(feature_seqs, modality, perplexity, random_state=DEFAULT_RANDOM_STATE):
    """單一模態、單一 perplexity 的 2D t-SNE 投影。

    perplexity 會被夾在 `[1, n_samples - 1]`（sklearn 的硬性要求：
    `perplexity < n_samples`）。**實際用掉的 perplexity 記在回傳值裡。**

    回傳 (embedding, effective_perplexity)：embedding 是 (N, 2) 陣列。
    """
    from sklearn.manifold import TSNE

    from analysis.experiments.exp_c_silhouette import stack_modality

    X = stack_modality(feature_seqs, modality)
    n_samples = X.shape[0]
    if n_samples < 4:
        raise ValueError(f"t-SNE 至少需要 4 筆樣本才有意義，收到 {n_samples}")

    effective_perplexity = max(1, min(perplexity, n_samples - 1))
    tsne = TSNE(n_components=2, perplexity=effective_perplexity,
                random_state=random_state, init="pca")
    embedding = tsne.fit_transform(X)
    return embedding, effective_perplexity


def embedding_silhouette(embedding, labels):
    """2D t-SNE 座標本身的 Silhouette 分數。

    **不是 `D13` 那個 PCA(50) 空間的分數**——這裡量的是「這張圖看起來到底
    分不分得開」的數字化版本，只用來自我驗證視覺化結果符合預期（驗收條件：
    「silent 模式下 Mel 應明顯不分群（驗證預期）」），不能拿來取代 D13
    的 Silhouette 結論或當成統計證據。
    """
    from sklearn.metrics import silhouette_score

    labels = np.asarray(labels)
    if len(np.unique(labels)) < 2:
        raise ValueError("至少需要 2 個類別才能算 silhouette")
    return float(silhouette_score(embedding, labels))


def plot_modality_perplexities(feature_seqs, labels, modality,
                                 perplexities=DEFAULT_PERPLEXITIES,
                                 random_state=DEFAULT_RANDOM_STATE, dpi=DEFAULT_DPI):
    """單一模態，多個 perplexity 並列（一列多張子圖），每詞不同顏色。

    回傳 matplotlib Figure；存檔或顯示交給呼叫端決定
    （`fig.savefig(path, dpi=300)`）。
    """
    import matplotlib.pyplot as plt

    labels = np.asarray(labels)
    unique_labels = np.unique(labels)

    fig, axes = plt.subplots(1, len(perplexities), figsize=(5 * len(perplexities), 5), dpi=dpi)
    if len(perplexities) == 1:
        axes = [axes]

    for ax, perplexity in zip(axes, perplexities):
        embedding, eff_perplexity = compute_tsne_embedding(
            feature_seqs, modality, perplexity, random_state)
        for lbl in unique_labels:
            mask = labels == lbl
            ax.scatter(embedding[mask, 0], embedding[mask, 1], label=str(lbl), s=20)
        ax.set_title(f"perplexity={eff_perplexity}")
        ax.set_xlabel("t-SNE dim 1")
        ax.set_ylabel("t-SNE dim 2")

    axes[0].legend(title="label", fontsize="small")
    fig.suptitle(
        f"{modality}  (t-SNE, random_state={random_state}) -- "
        "inter-cluster distance is NOT meaningful, only intra-cluster tightness is"
    )
    fig.tight_layout()
    return fig


def plot_all_modalities(feature_seqs, labels, perplexities=DEFAULT_PERPLEXITIES,
                         random_state=DEFAULT_RANDOM_STATE, dpi=DEFAULT_DPI):
    """五種模態組合各一張圖（驗收條件）。呼叫端對 normal/silent 各呼叫一次
    這個函式，就是 story 要的「silent 模式另做一組」對照。

    回傳 dict: modality -> Figure。
    """
    from analysis.experiments.exp_c_silhouette import MODALITIES

    return {
        modality: plot_modality_perplexities(feature_seqs, labels, modality,
                                               perplexities, random_state, dpi)
        for modality in MODALITIES
    }
