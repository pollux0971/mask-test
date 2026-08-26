# 距離度量比較：cosine（現行預設） vs euclidean（新加）

**⚠️ 全部合成資料，不是真實準確率或真實延遲。** 真實結論待 `E05` 真實錄音
後重跑 `analysis/experiments/exp_distance_metric_comparison.py`（把
`_write_session()` 換成讀真實 session，其餘流程不用改）。

來源：使用者原話「我希望只要歐式距離不需要訓練」（`ad` 轉述，2026-08-26）。
「不需要訓練」本來就成立——現行是最近鄰比對，沒有任何模型訓練；已跟使用者
說明過。這份報告回答的是「換掉距離函式本身，行為會怎麼變」。

## 已完成（不受此報告的實測結論影響）

- `analysis/similarity/euclidean_baseline.py`：`euclidean_dist`／
  `modality_euclidean_dist`／`batch_euclidean_dist`，跟 `cosine_baseline.py`
  同介面。9 個對稱測試全過（`test_euclidean_baseline.py`）。
- `analysis/similarity/recognition_service.py`：`DIST_FN_BY_NAME` 加了
  `"euclidean"`；`dist_method="euclidean"` 現在可用。**預設值沒有動**
  （還是 `"cosine"`），等這份報告的數字讓使用者確認再定案。
- 全部 `analysis/similarity/` 測試（84 個）仍然通過，沒有動到既有行為。

## 方法

跟 `exp_d05_dtw_vs_cosine.py`（純合成特徵空間）不同，這次刻意走**真實
production 路徑**：`SessionWriter` 寫出真的 HDF5 session → `load_session()`
讀回 → `build_templates_from_session.build_templates()` 建樣板（含 D01
z-score／D02 CMN／`Aligner` 對齊），跟 `E05` 實際的路徑一致，沒有抄近路
直接塞合成特徵向量。

5 個詞 + `_reject`，每類 train 8 筆／test 4 筆（`_reject` train 10／test
5），對 3 組獨立隨機幾何（不同 class 訊號方向）各跑一次，結果取平均——
避開 `D22.md` 記錄的四個合成資料陷阱（維度詛咒、天花板效應、共同常數
+cosine 塌陷、單一幾何抽樣運氣，細節見腳本 docstring）。

## 發現 1：兩個模態的歐式距離尺度差約 5.2 倍

| | ToF-only 距離 | Mel-only 距離 | 比例 |
|---|---|---|---|
| seed=0 | 46.05 | 8.86 | 5.20x |
| seed=1 | 45.62 | 8.86 | 5.15x |
| seed=2 | 45.92 | 8.85 | 5.19x |
| **平均** | | | **5.18x（標準差 0.02，三組幾何幾乎一致）** |

**這不只是這次合成資料湊巧的數字，背後有結構性原因**：D01 的 ToF z-score
會把每個通道除以標準差（強制歸一到單位變異數），但 D02 的 Mel **預設只
做 CMN（減平均），沒有除以標準差**（`mel_features(cvn=False)`）——兩個
模態的正規化程度本來就不對等，加上 ToF 64 維、Mel 40 維的維度差，量級
不同是預期中的結構性結果，不是這次合成資料選值選壞了才出現的假象。
（合成資料裡 Mel 原始振幅的選值——log10 baseline −3、訊號振幅 0.9、雜訊
0.25——會直接影響這個比例的具體數字，但「兩者不對等」這件事本身跟選值
無關。）

## 發現 2：分類準確率打平，拒識正確率被腰斬

| | cosine | euclidean |
|---|---|---|
| top1 準確率（真詞） | 76.7% | 76.7% |
| 誤拒率（真詞卻被拒識） | 38.3% | 38.3% |
| **正確拒識率（`_reject` 確實被拒）** | **66.7%** | **33.3%** |
| theta_reject_tof（校準後） | ~0.94 | ~44.6 |
| theta_reject_mel（校準後） | ~0.89 | ~8.5 |
| 平均單次辨識延遲 | 0.81 ms | 0.69 ms |

**分類準確率、誤拒率兩個方法完全打平**——`d_tof`/`d_mel`（分類用）在
`fusion.py` 裡都是先各自正規化（減 min、除 std）才用權重 `w` 融合，
對兩模態原始尺度不敏感，所以分類本身沒有被這裡的量級差影響，跟
`euclidean_baseline.py` 模組文件的預期一致。

**拒識正確率不一樣，而且是往壞的方向**：`reject_fused(w)` 用的是**原始
未正規化**的 `d_tof_raw`/`d_mel_raw` 加權相加（CONTRACTS §4.3 規定，
正規化過的距離拿去算會讓 `.min()` 恆為 0）。ToF/Mel 原始尺度差 5.18 倍，
`w=0.5` 融合出來的結果幾乎被 ToF 主導——即使 `theta_reject_tof`/
`theta_reject_mel` 各自都有正確重新校準（見下），融合後的拒識行為還是
明顯變差：合成資料上，`_reject`（靜止／無效訊號）被正確攔下的比例從
66.7% 掉到 33.3%。**這正是 `euclidean_baseline.py` 模組文件事先警告的
風險，這次是第一次量出實際影響。**

## 確認：門檻真的有重新校準（不是沿用 cosine 時代的數字）

`theta_reject_tof`/`theta_reject_mel` 换 `dist_method` 後從 ~0.94/~0.89
（cosine 的 [0,2] 尺度）變成 ~44.6/~8.5（euclidean 的原始尺度）——確認
`set_dist_method()`/`load_enrollment()` 換樣板時真的觸發 `_recalibrate_thresholds()`
用新的 `dist_fn` 重跑 ROC 校準，**不是沿用舊數字硬套新距離函式**。這件事
本來就該驗證，不能假設「換了就一定會校準」。

## 延遲：兩者都遠低於預算，不是決策因素

Euclidean 平均反而略快（0.69ms vs 0.81ms，兩者都遠低於 `E06` 30 秒
校準預算跟即時辨識的延遲要求）——延遲不是這兩個方法之間需要考慮的
差異。

## 一句建議 + 代價

**建議：預設值先不要換成 euclidean。** 分類準確率沒有變好，但拒識正確率
在合成資料上腰斬——代價明確、好處目前沒看到。如果使用者仍然想用
euclidean（例如有其他考量），代價是：要嘛接受拒識變不可靠，要嘛額外做
跨模態尺度校正（例如拒識融合前先把 `d_tof_raw`/`d_mel_raw` 各自除以
某個代表性尺度再加權，這需要新的設計與校準，不是「加個距離函式」這麼
輕量），這已經超出這次「加一個可選項」的範圍，需要使用者另外決定要不要
做。

## 邊界確認

沒有改 `DEFAULT_DIST_METHOD`（還是 `"cosine"`）；沒有碰
`bridge_server.py`／`host/trial/`／`js/modes/record.js`；`recognition_service.py`
只加了 import + 一行 registry 註冊 + 一段註解，沒有重構既有邏輯。
