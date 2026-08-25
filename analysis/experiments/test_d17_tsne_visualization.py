"""測試 `d17_tsne_visualization.py`。

t-SNE 本身是視覺化工具，不是統計證據（那是 D18），所以測試重點放在：
    1. `compute_tsne_embedding()` 的 shape、perplexity 夾範圍、可重現性。
    2. `embedding_silhouette()`——2D 座標本身的可分性，用來數字化驗證
       「這張圖看起來到底分不分得開」，尤其是驗收條件「silent 模式下
       Mel 應明顯不分群」。
    3. `plot_modality_perplexities()`/`plot_all_modalities()` 真的畫得出圖、
       dpi 對、每個 perplexity 有自己的子圖、圖表文字是英文。
    4. 一個真的走 D01->D02->D03 的整合測試，驗證 silent 模式下 Mel 的
       embedding 分數明顯低於 normal 模式（跟 D13/D16 同一個現象，
       三個獨立指標互相印證）。
"""
import matplotlib
matplotlib.use("Agg")  # 無頭環境，不需要真的顯示視窗

import numpy as np
import pytest

from analysis.experiments.d17_tsne_visualization import (
    DEFAULT_DPI,
    DEFAULT_PERPLEXITIES,
    compute_tsne_embedding,
    embedding_silhouette,
    plot_all_modalities,
    plot_modality_perplexities,
)


# ---------------------------------------------------------------------------
# compute_tsne_embedding
# ---------------------------------------------------------------------------

def _small_dataset(n_per_class=8, seed=0):
    rng = np.random.default_rng(seed)
    feats, labels = [], []
    for cls, center in enumerate([-5.0, 5.0, 0.0]):
        for _ in range(n_per_class):
            fs = rng.normal(0, 0.2, size=(4, 104))
            fs[:, 0:32] += center
            feats.append(fs)
            labels.append(cls)
    return feats, labels


def test_embedding_shape_matches_sample_count():
    feats, labels = _small_dataset()
    embedding, eff_p = compute_tsne_embedding(feats, "all", perplexity=10, random_state=0)
    assert embedding.shape == (len(feats), 2)
    assert eff_p == 10  # 樣本數夠多，不需要夾


def test_embedding_rejects_too_few_samples():
    feats = [np.zeros((3, 104)) for _ in range(3)]
    with pytest.raises(ValueError):
        compute_tsne_embedding(feats, "all", perplexity=5)


def test_perplexity_clamped_to_sample_count_and_recorded():
    """驗收條件關鍵：perplexity 要可調、實際用掉的值要記錄。sklearn 硬性
    要求 perplexity < n_samples，這裡故意要求遠超過樣本數的 perplexity。"""
    feats = [np.random.default_rng(i).normal(size=(4, 104)) for i in range(6)]
    labels = [0, 0, 1, 1, 2, 2]

    embedding, eff_p = compute_tsne_embedding(feats, "all", perplexity=30, random_state=0)

    assert eff_p == 5  # min(30, n_samples-1=5)
    assert eff_p < 30
    assert embedding.shape == (6, 2)


def test_embedding_reproducible_with_fixed_random_state():
    feats, labels = _small_dataset()
    e1, p1 = compute_tsne_embedding(feats, "tof_l", perplexity=8, random_state=42)
    e2, p2 = compute_tsne_embedding(feats, "tof_l", perplexity=8, random_state=42)
    np.testing.assert_array_equal(e1, e2)
    assert p1 == p2


# ---------------------------------------------------------------------------
# embedding_silhouette：2D 座標本身的可分性
# ---------------------------------------------------------------------------

def test_embedding_silhouette_high_for_well_separated_embedding():
    """人工構造的乾淨 2D 座標（不經過 t-SNE，直接測這個函式本身），
    驗證分數計算邏輯正確。"""
    embedding = np.array([[-5.0, 0.0]] * 5 + [[5.0, 0.0]] * 5)
    labels = [0] * 5 + [1] * 5
    score = embedding_silhouette(embedding, labels)
    assert score == pytest.approx(1.0, abs=1e-6)


def test_embedding_silhouette_rejects_single_class():
    embedding = np.zeros((5, 2))
    with pytest.raises(ValueError):
        embedding_silhouette(embedding, [0, 0, 0, 0, 0])


# ---------------------------------------------------------------------------
# 畫圖：shape、dpi、每個 perplexity 有自己的子圖、英文文字
# ---------------------------------------------------------------------------

def test_plot_modality_perplexities_draws_one_subplot_per_perplexity():
    feats, labels = _small_dataset()

    fig = plot_modality_perplexities(feats, labels, "tof_l", perplexities=(8, 12), random_state=0)

    scatter_axes = [ax for ax in fig.axes if ax.collections]
    assert len(scatter_axes) == 2  # 兩個 perplexity 各一張子圖
    assert fig.dpi == DEFAULT_DPI


def test_plot_modality_perplexities_uses_custom_dpi():
    feats, labels = _small_dataset()
    fig = plot_modality_perplexities(feats, labels, "all", perplexities=(8,), dpi=150)
    assert fig.dpi == 150


def test_plot_titles_and_labels_are_english_only():
    """圖表文字一律英文（調度員規則）——這裡直接檢查標題/軸標籤字串裡
    沒有任何 CJK 字元。"""
    feats, labels = _small_dataset()
    fig = plot_modality_perplexities(feats, labels, "mel", perplexities=(8,), random_state=0)

    texts = [fig._suptitle.get_text()] if fig._suptitle else []
    for ax in fig.axes:
        texts.append(ax.get_title())
        texts.append(ax.get_xlabel())
        texts.append(ax.get_ylabel())

    def has_cjk(s):
        return any("一" <= ch <= "鿿" for ch in s)

    for t in texts:
        assert not has_cjk(t), f"圖表文字含 CJK 字元: {t!r}"


def test_plot_all_modalities_returns_five_figures():
    feats, labels = _small_dataset()
    figs = plot_all_modalities(feats, labels, perplexities=(8,), random_state=0)
    assert set(figs) == {"tof_l", "tof_r", "tof_combined", "mel", "all"}
    assert all(fig.dpi == DEFAULT_DPI for fig in figs.values())


# ---------------------------------------------------------------------------
# 整合測試：真的走 D01 -> D02 -> D03 -> D17
# ---------------------------------------------------------------------------

N_WORDS = 4
N_REPEATS = 10
T_RAW = 15


def _make_trials(mode):
    """跟 D13/D16/D18 同一套合成資料手法。"""
    from analysis.features.audio_features import mel_features
    from analysis.features.feature_assembly import assemble_feature_seq
    from analysis.features.tof_features import tof_features

    pattern_rng = np.random.default_rng(7)
    tof_patterns_a = pattern_rng.normal(0, 3.0, size=(N_WORDS, 32))
    tof_patterns_b = pattern_rng.normal(0, 3.0, size=(N_WORDS, 32))
    mel_patterns = pattern_rng.normal(0, 3.0, size=(N_WORDS, 40))
    envelope = np.sin(np.linspace(0, np.pi, T_RAW))

    valid = np.ones((T_RAW, 16), dtype=bool)
    baseline_mu = np.zeros(32)
    baseline_sigma = np.ones(32)
    mode_gain = {"normal": 1.0, "whisper": 0.4, "silent": 0.0}[mode]
    rng = np.random.default_rng(42)

    feats, labels = [], []
    for word_idx in range(N_WORDS):
        for _ in range(N_REPEATS):
            tof_a_raw = rng.normal(0, 0.5, size=(T_RAW, 32)) + tof_patterns_a[word_idx]
            tof_b_raw = rng.normal(0, 0.5, size=(T_RAW, 32)) + tof_patterns_b[word_idx]
            tof_a_z = tof_features(tof_a_raw, valid, baseline_mu, baseline_sigma)
            tof_b_z = tof_features(tof_b_raw, valid, baseline_mu, baseline_sigma)

            mel_raw = (rng.normal(0, 0.3, size=(T_RAW, 40))
                       + envelope[:, None] * mode_gain * mel_patterns[word_idx])
            mel_cmn = mel_features(mel_raw, vad_start=None, vad_end=None, cvn=False)

            t_us = np.arange(T_RAW) * 1000
            data = assemble_feature_seq(tof_a_z, tof_b_z, mel_cmn, t_us, t_fixed=24).data
            feats.append(data)
            labels.append(word_idx)
    return feats, labels


def test_silent_mode_mel_does_not_cluster_but_tof_still_does():
    """驗收條件：silent 模式下 Mel 應明顯不分群（驗證預期）——這是整份
    D13/D16/D17 裡最有說服力的一張圖的數字化版本：氣音時 Mel 糊成一團，
    ToF 仍然分群。（假資料模擬，不是真實結論。）"""
    feats_normal, labels_normal = _make_trials("normal")
    feats_silent, labels_silent = _make_trials("silent")

    emb_mel_normal, _ = compute_tsne_embedding(feats_normal, "mel", perplexity=15, random_state=0)
    emb_mel_silent, _ = compute_tsne_embedding(feats_silent, "mel", perplexity=15, random_state=0)
    emb_tof_silent, _ = compute_tsne_embedding(feats_silent, "tof_combined", perplexity=15, random_state=0)

    sil_mel_normal = embedding_silhouette(emb_mel_normal, labels_normal)
    sil_mel_silent = embedding_silhouette(emb_mel_silent, labels_silent)
    sil_tof_silent = embedding_silhouette(emb_tof_silent, labels_silent)

    assert sil_mel_silent < sil_mel_normal
    assert sil_mel_silent < 0.3       # 幾乎不分群
    assert sil_tof_silent > 0.5       # ToF 不受影響，仍明顯分群
