"""D19 — 消融實驗套件：拿掉某個東西，看準確率掉多少。

規格見 `stories/D-analysis/D19.md`。這是唯一能把「相關」變成「有貢獻」的
方法——`D13`（Silhouette）、`D16`（互信息）、`D18`（permutation test）都
是在「原始資料」上量分離度/資訊量/顯著性，D19 反過來問「拿掉某個東西，
效果掉多少」。**「All vs Mel-only 增益 > 5%」是沒有 sEMG 之後的新核心
指標**（取代簡報原本的 sEMG 消融實驗）。

輸入跟 `D13`/`D16`/`D18` 一樣是 `D03` 組裝好的 `FeatureSeq.data`。
**全部重用 `D18` 的 `run_permutation_test()` 做「準確率 + p 值」，不重寫
一份分類器/CV 邏輯**——每個消融組態都有 p 值，這樣「掉了 3 個百分點」
才不會只是雜訊（調度員的建議，合理，照做）。

## 🔴 `groups`：六個檢定跟著 `D18` 一起分組，虛無假設也跟著變

`run_ablation_suite()` 支援 `groups=`（每筆 trial 的 `wear_id`），原封不動
轉傳給底層的六個 `run_permutation_test()` 呼叫（`all`／`mel`／
`tof_combined`／`tof_l`／`tof_r`／雜訊化後的 `all`）——**同一次呼叫裡
六個結果永遠拿到同一個分組狀態，不會出現同一份報告裡有些檢定分組了、
有些沒有這種兩種嚴謹度混在一起的情況**。

這不只是換一種 CV 切法：分組之後 CV 改用 `StratifiedGroupKFold`（同一次
戴上的樣本不會同時落進訓練跟測試集，堵住 `7c` 實測證明過的洩漏——
ToF-only 準確率從隨機切的 0.917 掉到分組切的 0.625，那 0.29 就是模型
靠「認出這是哪一次戴的」拿到的分數），**每個檢定背後的虛無假設也跟著
改變**：標籤是在同一個 group 內部打亂，不是全體打亂，問的問題變成
「在同一次戴上之內，這個模態還分得出詞嗎」——細節與理由見
`d18_permutation_test.py` 的「🔴 分組驗證」章節，這裡不重複一份可能
對不上的說明。

**時間反轉測試（第 4 項）是唯一的例外**：它自己實作 CV，不經過
`run_permutation_test()`，`groups` 對它沒有作用，`format_report()`
會在有分組時明講這件事。

只有一個 group（第一批資料很可能只戴一次）時分組驗證做不到，回傳的
`grouping` 會是 `"ungrouped_single_group"`，**這個狀態在報告裡只出現
一次，不是六個小節各講一次同樣的警語**（那樣讀報告的人反而更容易略過）。

## 本檔實作的五項消融（照 D19.md 驗收條件逐項對應，不多不少）

1. **雙矩陣 vs 單顆**（Sanity #6）：`acc(tof_combined) - max(acc(tof_l), acc(tof_r))`，
   PASS 門檻 > 5 個百分點。這是 `D16` 負增益警示指定的交叉驗證對象——
   `D16` 用互信息量出來的雙矩陣增益可能因為 PCA 加總法而失真，這裡用
   準確率獨立驗證一次。
2. **All vs Mel-only**（新核心指標）：`acc(all) - acc(mel)`，PASS 門檻 > 5 個百分點。
3. **All vs ToF-only**：`acc(all) - acc(tof_combined)`，**只記錄，story 沒有給
   PASS/FAIL 門檻**——回傳值裡 `passed` 是 `None`，不是 `True`/`False`。
4. **時間反轉測試**（Sanity #3）：PASS 門檻 accuracy < 30%（近乎隨機猜——
   8 類詞彙隨機猜是 12.5%，這裡用 30% 當寬鬆上限）。**這裡做的是「反轉」，
   不是「打亂」**——照 story 原文「把序列在時間軸上反轉」，跟單純洗牌
   幀順序是不同操作（反轉保留連續性只是方向相反，洗牌會完全破壞連續性）；
   如果之後還想要洗牌版本的變體，那是本 story 範圍外的新測試，不在這裡加。

   **這一項刻意不重用 `run_permutation_test()`，用自己的訓練/測試切法。**
   開發時實測過：如果直接把整批資料反轉、再用一般 CV「從頭在反轉資料上
   訓練+測試」（前四項的做法），準確率幾乎不會掉——因為分類器是每次重新
   fit 的，反轉後的樣本對它來說就是換一組一樣容易學的新資料，跟「模型有
   沒有真的利用時序方向」這個問題其實沒關係。**真正有意義的作法是：
   用正常方向的資料訓練模型，但拿反轉後的資料當測試集**——如果模型學到
   的是真正的時序/動態結構，方向搞反的輸入應該讓它認錯；如果模型學到的
   只是跟時間順序無關的整體統計量，方向反轉對它來說跟沒反轉一樣。
   `time_reversal_ablation()` 就是照這個邏輯實作的。
5. **亂數通道測試**（Sanity #2）：把某個模態的欄位整段換成同尺度的隨機
   雜訊，PASS 門檻是相對於 `all` baseline 的準確率**相對**下降介於
   10%-50%（story 原文的門檻）。story 另外提到「掉太多（>70%）代表過度
   依賴單一模態」——這是額外的討論用門檻，不是 PASS/FAIL 判定的一部分，
   `format_report()` 會在超過 70% 時額外加一句討論，但驗收條件的
   PASS/FAIL 只看 10%-50% 這個窗口。

## 「結果整合進 D15 的報告」

`D15`（驗證報告產生器）目前還沒開工（前置 `D10`-`D14` 沒有全部完成）。
本模組能做的是：`format_report()` 的輸出跟 `D13`/`D16`/`D18` 用同一套
Markdown 慣例（`is_synthetic` 警示、表格、PASS/FAIL 判定、討論段落），
`D15` 之後可以直接把這段文字接進 `summary.md`，不需要另外轉換格式。

「開發完成不等於有真實結果」：跟 D13/D16/D17/D18 一樣，本模組用假資料
開發並測過就是完整交付，真實結論待 `E05`。
"""
import numpy as np

DEFAULT_N_PERMUTATIONS = 1000  # 跟 D18 一致
DEFAULT_CV_FOLDS = 5
DEFAULT_RANDOM_STATE = 0

DUAL_MATRIX_GAIN_THRESHOLD = 0.05
MEL_ONLY_GAIN_THRESHOLD = 0.05
TIME_REVERSAL_MAX_ACCURACY = 0.30
RANDOM_CHANNEL_MIN_RELATIVE_DROP = 0.10
RANDOM_CHANNEL_MAX_RELATIVE_DROP = 0.50
RANDOM_CHANNEL_OVERRELIANCE_HINT = 0.70  # 描述性門檻，見模組 docstring 第 5 項


def _run(feature_seqs, labels, modality, n_permutations, cv, random_state, n_jobs,
          groups=None):
    from analysis.experiments.d18_permutation_test import run_permutation_test

    return run_permutation_test(feature_seqs, labels, modality,
                                 n_permutations=n_permutations, cv=cv,
                                 random_state=random_state, n_jobs=n_jobs,
                                 groups=groups)


def reverse_time(feature_seqs):
    """把每筆 (T, D) 序列沿時間軸整個反轉——時間反轉測試（Sanity #3）。"""
    return [np.asarray(fs, dtype=np.float64)[::-1, :].copy() for fs in feature_seqs]


def substitute_modality_with_noise(feature_seqs, modality, random_state=DEFAULT_RANDOM_STATE):
    """把某個模態的欄位整段換成同尺度的隨機雜訊——亂數通道測試（Sanity #2）。

    雜訊標準差取自該模態原始資料本身的標準差（不是隨便挑一個固定值），
    這樣「雜訊尺度不合理」不會是這個測試失敗的原因——只有「這個模態
    有沒有被模型用到」才是。
    """
    from analysis.experiments.exp_c_silhouette import MODALITIES

    sl = MODALITIES[modality]
    rng = np.random.default_rng(random_state)
    out = []
    for fs in feature_seqs:
        fs = np.asarray(fs, dtype=np.float64).copy()
        block = fs[:, sl]
        scale = float(np.std(block))
        if scale <= 0:
            scale = 1.0
        fs[:, sl] = rng.normal(0, scale, size=block.shape)
        out.append(fs)
    return out


def time_reversal_ablation(feature_seqs, labels, cv=DEFAULT_CV_FOLDS,
                            random_state=DEFAULT_RANDOM_STATE):
    """時間反轉測試（Sanity #3）：訓練用正常方向，測試用反轉方向。

    自己做 CV（不是 `run_permutation_test()`）：每一折用正常方向的訓練
    樣本 fit 模型，然後**分別**在同一折的正常方向測試樣本、反轉方向測試
    樣本上評分——這樣「正常方向準確率」跟「反轉方向準確率」用的是同一個
    fold 切法、同一個訓練好的模型，差異只來自測試資料方向，才是乾淨的
    對照。見模組 docstring「這一項刻意不重用 run_permutation_test()」。

    回傳 dict: {"forward_accuracy", "reversed_accuracy", "cv", "passed"}
    `cv` 是實際用掉的折數（同樣被夾在最小類別樣本數，理由跟 D18 一致）。
    """
    from sklearn.model_selection import StratifiedKFold

    from analysis.experiments.d18_permutation_test import make_estimator
    from analysis.experiments.exp_c_silhouette import stack_modality

    y = np.asarray(labels)
    if y.shape[0] != len(feature_seqs):
        raise ValueError(f"labels 長度 {y.shape[0]} 與 trial 數 {len(feature_seqs)} 不一致")

    X_forward = stack_modality(feature_seqs, "all")
    X_reversed = stack_modality(reverse_time(feature_seqs), "all")

    min_class_count = int(np.min(np.unique(y, return_counts=True)[1]))
    if min_class_count < 2:
        raise ValueError(f"每個類別至少需要 2 筆樣本才能做 CV，最小類別只有 {min_class_count} 筆")
    n_splits = max(2, min(cv, min_class_count))

    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state)

    forward_scores = []
    reversed_scores = []
    for train_idx, test_idx in skf.split(X_forward, y):
        estimator = make_estimator()
        estimator.fit(X_forward[train_idx], y[train_idx])
        forward_scores.append(estimator.score(X_forward[test_idx], y[test_idx]))
        reversed_scores.append(estimator.score(X_reversed[test_idx], y[test_idx]))

    reversed_accuracy = float(np.mean(reversed_scores))
    return {
        "forward_accuracy": float(np.mean(forward_scores)),
        "reversed_accuracy": reversed_accuracy,
        "cv": n_splits,
        "passed": bool(reversed_accuracy < TIME_REVERSAL_MAX_ACCURACY),
    }


def run_ablation_suite(feature_seqs, labels, n_permutations=DEFAULT_N_PERMUTATIONS,
                        cv=DEFAULT_CV_FOLDS, random_state=DEFAULT_RANDOM_STATE,
                        n_jobs=-1, noise_modality="mel", is_synthetic=True,
                        groups=None):
    """五項消融全部跑一次（驗收條件）。

    `r_all`（"all" 模態的 permutation test 結果）在多個消融裡被重用，
    不會為同一組資料重跑三次一模一樣的 CV+permutation。

    `groups`（每筆 trial 的 `wear_id`，見 `d18_permutation_test.py` 的
    「🔴 分組驗證」說明）：這裡呼叫的六個 `run_permutation_test()`
    （`all`／`mel`／`tof_combined`／`tof_l`／`tof_r`／雜訊化後的 `all`）
    全部原封不動轉傳，同一個 `run_ablation_suite()` 呼叫裡的六個結果
    保證拿到同一個分組狀態——不會有的通過驗證、有的沒有，同一份報告
    出現兩種嚴謹度。傳了 `groups` 之後**不只是換一種 CV 切法**：
    每個檢定的準確率會變得比較誠實（同一次戴上的樣本不會又當訓練又當
    測試），這裡各項消融比的「增益」（`gain`/`relative_drop`）也就跟著
    變得可信；而每個檢定本身背後的虛無假設也跟著改變（組內打亂而非
    全體打亂，細節見 `d18_permutation_test.py`），問的問題變成「在同一次
    戴上之內，這個模態還分得出詞嗎」。

    ⚠️ **`time_reversal`（第 4 項）是例外，不吃 `groups`**：它自己實作
    CV（`time_reversal_ablation()`，見該函式文件字串），不經過
    `run_permutation_test()`，這裡沒有跟著補上分組邏輯——傳了 `groups`
    也不影響它。`format_report()` 在有分組時會明確提醒這件事，不讓它被
    誤讀成「已經比照其他五項處理過」。

    回傳 dict: {"is_synthetic", "dual_matrix_vs_single", "all_vs_mel_only",
                "all_vs_tof_only", "time_reversal", "random_channel",
                "grouping", "n_groups", "grouping_note"}——後三個是六個
    分組結果共用的那個狀態（拿 `all` 那次的結果代表，六者必然相同）。
    每個消融結果都有 "gain"/"relative_drop" 等數字與 "passed"（`None`
    代表 story 沒有要求 PASS/FAIL，只要求記錄）。
    """
    kwargs = dict(n_permutations=n_permutations, cv=cv, random_state=random_state,
                  n_jobs=n_jobs, groups=groups)

    r_all = _run(feature_seqs, labels, "all", **kwargs)
    r_mel = _run(feature_seqs, labels, "mel", **kwargs)
    r_tof = _run(feature_seqs, labels, "tof_combined", **kwargs)
    r_tof_l = _run(feature_seqs, labels, "tof_l", **kwargs)
    r_tof_r = _run(feature_seqs, labels, "tof_r", **kwargs)

    dual_matrix_gain = r_tof["score"] - max(r_tof_l["score"], r_tof_r["score"])
    dual_matrix_vs_single = {
        "name": "dual_matrix_vs_single",
        "tof_l": r_tof_l, "tof_r": r_tof_r, "tof_combined": r_tof,
        "gain": dual_matrix_gain,
        "passed": bool(dual_matrix_gain > DUAL_MATRIX_GAIN_THRESHOLD),
    }

    mel_gain = r_all["score"] - r_mel["score"]
    all_vs_mel_only = {
        "name": "all_vs_mel_only",
        "all": r_all, "mel_only": r_mel,
        "gain": mel_gain,
        "passed": bool(mel_gain > MEL_ONLY_GAIN_THRESHOLD),
    }

    tof_gain = r_all["score"] - r_tof["score"]
    all_vs_tof_only = {
        "name": "all_vs_tof_only",
        "all": r_all, "tof_only": r_tof,
        "gain": tof_gain,
        "passed": None,  # story：只記錄，沒有通過/失敗門檻
    }

    time_reversal = time_reversal_ablation(feature_seqs, labels, cv=cv, random_state=random_state)
    time_reversal["name"] = "time_reversal"

    noised_feats = substitute_modality_with_noise(feature_seqs, noise_modality, random_state)
    r_noised = _run(noised_feats, labels, "all", **kwargs)
    baseline_acc = r_all["score"]
    relative_drop = ((baseline_acc - r_noised["score"]) / baseline_acc
                      if baseline_acc > 0 else float("nan"))
    random_channel = {
        "name": "random_channel",
        "noised_modality": noise_modality,
        "baseline": r_all, "noised": r_noised,
        "relative_drop": relative_drop,
        "passed": bool(RANDOM_CHANNEL_MIN_RELATIVE_DROP <= relative_drop
                        <= RANDOM_CHANNEL_MAX_RELATIVE_DROP),
    }

    return {
        "is_synthetic": is_synthetic,
        "dual_matrix_vs_single": dual_matrix_vs_single,
        "all_vs_mel_only": all_vs_mel_only,
        "all_vs_tof_only": all_vs_tof_only,
        "time_reversal": time_reversal,
        "random_channel": random_channel,
        # 六個 run_permutation_test() 呼叫共用同一個 groups 引數，這裡拿
        # r_all 的結果代表整批——_resolve_grouping() 對同樣的 groups/樣本數
        # 是決定性的，六者必然相同，不需要各自存一份。
        "grouping": r_all.get("grouping", "ungrouped_no_groups_given"),
        "n_groups": r_all.get("n_groups"),
        "grouping_note": r_all.get("grouping_note"),
    }


def _ablation_grouping_lines(suite):
    """分組驗證狀態，**整份報告只講一次**——六個消融背後的六個
    `run_permutation_test()` 呼叫共用同一個 `groups`，個別小節裡再各講
    一次同樣的話只是六句一模一樣的警語洗版，讀報告的人反而更容易略過。

    重用 `d18_permutation_test._grouping_lines()` 的三態文字，不是另外
    寫一份可能對不上的版本。
    """
    from analysis.experiments.d18_permutation_test import _grouping_lines

    fake_report = {"all": {"grouping": suite.get("grouping", "ungrouped_no_groups_given"),
                            "n_groups": suite.get("n_groups"),
                            "grouping_note": suite.get("grouping_note")}}
    lines = _grouping_lines(fake_report)
    if suite.get("grouping") == "grouped":
        lines.append(
            "> ⚠️ **例外：第 4 項「時間反轉測試」不吃 `groups`**——它自己"
            "實作 CV，不經過 `run_permutation_test()`，這裡沒有分組保護，"
            "讀它的數字時仍要記得同一次戴上的洩漏風險存在。"
        )
        lines.append("")
    return lines


def format_report(suite):
    """把 `run_ablation_suite()` 的結果轉成人類可讀的 Markdown 字串。

    驗收條件：「All vs Mel-only 的增益明確標示」「每項都有 PASS/FAIL 與
    討論」——五項各自一個小節，`all_vs_mel_only` 額外加粗標示（新核心指標）。
    """
    def status_text(passed):
        if passed is None:
            return "（只記錄，無 PASS/FAIL 門檻）"
        return "**PASS**" if passed else "**FAIL**"

    lines = []
    if suite["is_synthetic"]:
        lines.append("> ⚠️ **假資料（synthetic）產生的分數，不是真實結論。**"
                      " 真實結論待 `E05` 資料蒐集後重跑本模組取得。")
        lines.append("")

    lines += _ablation_grouping_lines(suite)

    lines.append("## 1. 雙矩陣 vs 單顆（Sanity #6）")
    lines.append("")
    d = suite["dual_matrix_vs_single"]
    lines.append(
        f"acc(L)={d['tof_l']['score']:.3f}, acc(R)={d['tof_r']['score']:.3f}, "
        f"acc(Combined)={d['tof_combined']['score']:.3f} -> "
        f"增益 {d['gain']:+.3f} (門檻 > {DUAL_MATRIX_GAIN_THRESHOLD}) {status_text(d['passed'])}"
    )
    lines.append("")

    lines.append("## 2. All vs Mel-only（**新核心指標**——取代原本的 sEMG 消融）")
    lines.append("")
    m = suite["all_vs_mel_only"]
    lines.append(
        f"acc(All)={m['all']['score']:.3f}, acc(Mel-only)={m['mel_only']['score']:.3f} -> "
        f"**增益 {m['gain']:+.3f}**（門檻 > {MEL_ONLY_GAIN_THRESHOLD}）{status_text(m['passed'])}"
    )
    lines.append("")

    lines.append("## 3. All vs ToF-only（記錄）")
    lines.append("")
    t = suite["all_vs_tof_only"]
    lines.append(
        f"acc(All)={t['all']['score']:.3f}, acc(ToF-only)={t['tof_only']['score']:.3f} -> "
        f"增益 {t['gain']:+.3f} {status_text(t['passed'])}"
    )
    lines.append("")

    lines.append("## 4. 時間反轉測試（Sanity #3）")
    lines.append("")
    r = suite["time_reversal"]
    lines.append(
        f"訓練=正常方向, 測試=正常方向準確率={r['forward_accuracy']:.3f}；"
        f"訓練=正常方向, 測試=反轉方向準確率={r['reversed_accuracy']:.3f} "
        f"(門檻 < {TIME_REVERSAL_MAX_ACCURACY}, cv={r['cv']}) {status_text(r['passed'])}"
    )
    lines.append("")
    if r["passed"]:
        lines.append("反轉後準確率大幅下降，代表模型真的用到了時序資訊——"
                      "30 Hz 取樣、輪詢修正、DTW 都有實際貢獻，不是白做的。")
    else:
        lines.append("⚠️ 反轉後準確率沒有明顯下降，代表模型可能只用了「整體統計量」"
                      "而沒有真的利用時序資訊——這種情況下 DTW 相對於固定長度餘弦"
                      "距離的優勢需要重新檢視。")
    lines.append("")

    lines.append("## 5. 亂數通道測試（Sanity #2）")
    lines.append("")
    n = suite["random_channel"]
    lines.append(
        f"雜訊化模態: `{n['noised_modality']}`；baseline={n['baseline']['score']:.3f}, "
        f"雜訊化後={n['noised']['score']:.3f} -> 相對下降 {n['relative_drop']:.1%} "
        f"(門檻 {RANDOM_CHANNEL_MIN_RELATIVE_DROP:.0%}-{RANDOM_CHANNEL_MAX_RELATIVE_DROP:.0%}) "
        f"{status_text(n['passed'])}"
    )
    if n["relative_drop"] < RANDOM_CHANNEL_MIN_RELATIVE_DROP:
        lines.append(f"下降過少：`{n['noised_modality']}` 這個模態看起來沒有被模型實際用到。")
    elif n["relative_drop"] > RANDOM_CHANNEL_OVERRELIANCE_HINT:
        lines.append(f"下降超過 {RANDOM_CHANNEL_OVERRELIANCE_HINT:.0%}：可能過度依賴 "
                      f"`{n['noised_modality']}` 這個單一模態。")
    lines.append("")

    return "\n".join(lines)
