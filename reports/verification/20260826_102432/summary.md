# 驗證報告 summary

> ⚠️ **合成資料，不是真實結論。** 真實結論待 `E05`。

## 通過矩陣

| 實驗 | 指標 | 實測 | 標準 | 判定 |
|---|---|---|---|---|
| C0 串擾 🔒 | Δ_dist | — | < 2 mm | — SKIPPED |
| A 逐 zone SNR 🔒 | SNR_L / SNR_R | — | > 3 | — SKIPPED |
| B 跨次戴 CV | CV_between | — | < 30% | — SKIPPED |
| C Silhouette 🔒 | Silhouette | — | > 0.15 | — SKIPPED |
| E Viseme 敏感度 | 擦音 Mel > ToF | — | 有模式 | — SKIPPED |

🔒 = 必通過項目（失敗時整份報告的數字都不可信）。

## 跨實驗一致性

六個實驗從不同角度量同一件事，**它們對得上才可信**。

### ⚠️ 第二顆 ToF 是否帶來額外資訊

來源：

**這個交叉檢查目前只有 0 個來源**（（無）），至少要 2 個才比得出矛盾。`D13` 的互補性、`D16` 的資訊增益、`D19` 的消融應該三者都在——缺的那幾個請確認有沒有跑起來。**「沒有資料」不等於「沒有矛盾」。**

### ⚠️ 有實驗沒有跑

來源：C0、A、B、C、E

C0 串擾、A 逐 zone SNR、B 跨次戴 CV、C Silhouette、E Viseme 敏感度 因資料不足而未執行。**「沒跑」不是「通過」也不是「失敗」**——在補齊資料之前，這份報告對這幾項沒有任何結論。

## 已知限制

* **本報告的所有數字來自合成資料，不是真實結論。** 真實結論待 `E05` 蒐集資料後重跑。
* **舌音（viseme E）未涵蓋。** `config/vocab.json`（§6）的八個詞不含任何 /t,d,k/ 類的詞，因此 `D14` 的舌音那一列永遠是空的——本系統**無法驗證**該類別的表現。這是詞彙集的設計取捨，不是量測失敗。
* **zone 佈局 row-major 仍是未驗證假設**（ASSUMED, unverified — see A track/E01）。所有逐 zone 的空間結論（`D11` 的活躍 zone、`D14` 的熱力圖）在佈局確認前**方向可能是反的**。

---

Session：`/tmp/claude-1000/-home-pollux-Desktop-esp-mask-test/2122ec03-74bb-4eeb-bd05-fcca1b8fcbb9/scratchpad/sessions_verify/verify_test.h5`
執行時間：0.5 秒

## 執行備註

* 特徵序列不足，`D16`/`D19` 未執行；「第二顆 ToF 有沒有用」的三方投票沒有任何來源
