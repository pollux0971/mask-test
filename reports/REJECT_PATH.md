# 拒識路徑端到端驗證 — `quiz.js` 對 CONTRACTS.md §4.3 的實作

> 由 `esp-mask-test-7c` 執行，2026-08-26。起因：Demo 腳本第 4 步（純 ToF
> 念「四」→ 系統顯示「無法辨識」）是整場 Demo 的關鍵一步，但這條路徑
> 從沒有人端到端走過。`esp-mask-test-ad` 特別點名 §4.3 融合拒識公式裡
> `_raw` 這個容易漏掉的字——`normalize_distances()` 會把最小值減掉，
> 用正規化後的距離判斷拒識會讓 `min()` 恆為 0，拒識永遠不會觸發，
> 而且不會有任何錯誤訊息。

## 結論摘要

**查過了，`quiz.js` 這條路徑跟 §4.3 契約完全一致，沒有找到偏差。**
這是誠實的「查了、是對的」結論，不是為了有東西交而找出來的問題。

| 檢查項目 | §4.3 契約要求 | `quiz.js` 實際行為 | 結果 |
|---|---|---|---|
| 拒識判定在前端還是後端算 | 前端即時算（TriResult 不存這個布林值） | `computeFusedReject()`，每次 `renderFusedColumn()` 都重算 | ✅ 一致 |
| 用哪組距離判斷拒識 | 必須用 `d_tof_raw`/`d_mel_raw`，不能用正規化後的 `d_tof`/`d_mel` | `computeFusedReject` 解構的正是 `d_tof_raw`/`d_mel_raw` | ✅ 一致，**沒有踩到 `.min()` 恆為 0 的坑** |
| 門檻是兩個獨立值還是共用一個 | `theta_reject_tof`／`theta_reject_mel` 分開 | 兩個變數分開解構、分開內插，沒有合併 | ✅ 一致 |
| 門檻怎麼隨 `w` 內插 | `w·theta_tof + (1-w)·theta_mel` | 逐字一致 | ✅ 一致 |
| 兩端退化性質 | `w=1` 等於 `reject_tof`、`w=0` 等於 `reject_mel` | 程式碼裡就有這個斷言，**執行期真的驗證，不是只在註解裡寫** | ✅ 一致，見第 3 節 |
| 拖 `w` 滑桿拒識結論會不會變 | 應該會（公式含 `w`） | 實測用一組刻意在 `w=1/3` 翻轉的資料，`T`/`M`/`F` 三個鍵各自對應正確結論 | ✅ 一致，見第 4 節 |
| 視覺上拒識是不是看得出跟「爛答案」不同 | Demo 意義上兩者完全不同 | dashed 邊框＋灰階＋空心圓環＋`--`（拒識）vs 實心 accent 邊框＋填色圓環＋真實詞＋百分比（爛答案），兩者截然不同種類的視覺，不只是程度上更暗 | ✅ 一致，見第 5 節截圖 |

## 1. 前端拿到的 `d_tof`/`d_mel` 是 raw 還是 normalized？

`fuseScores()`（給三軌分數長條圖用）用的是**正規化後**的 `d_tof`/`d_mel`：

```js
function fuseScores(triResult, w) {
  const { d_tof, d_mel, tau } = triResult;
  const combined = d_tof.map((d, i) => w * d + (1 - w) * d_mel[i]);
  return softmax(combined.map((d) => -d / tau));
}
```

`computeFusedReject()`（給拒識判定用）用的是**原始未正規化**的
`d_tof_raw`/`d_mel_raw`：

```js
function computeFusedReject(triResult, w) {
  const { d_tof_raw, d_mel_raw, theta_reject_tof, theta_reject_mel } = triResult;
  const combined = d_tof_raw.map((d, i) => w * d + (1 - w) * d_mel_raw[i]);
  const minDist = Math.min(...combined);
  const thetaFused = w * theta_reject_tof + (1 - w) * theta_reject_mel;
  return minDist > thetaFused;
}
```

**兩組不同的距離陣列，各自用在正確的地方**——這正是 §4.3 那條警告要防的
錯法（拿正規化後的距離去比會讓 `.min()` 恆為 0），`quiz.js` 沒有踩到。
程式碼自己也有註解說明這是刻意的兩份資料、不是該合併的重複：

> 「Two different distance arrays for two different purposes, on purpose --
> not an inconsistency to 'clean up'.」

## 2. 拒識判定在哪裡做？

**前端自己算**，`onRecognizeClick()` 收到 `TriResult` 後不會拿到現成的
`reject_fused`——契約本來就規定不存這個值（隨 `w` 變，存下來會跟滑桿
不同步）。`renderFusedColumn()` 每次被呼叫（包含每次拖 `w` 滑桿）都重新
呼叫 `computeFusedReject(lastTriResult, currentW)`，不是快取結果。

必要欄位缺漏會在收到回應當下就被擋下，不會讓 `computeFusedReject` 拿到
不完整的資料去算出一個看似正常但其實是垃圾的結果：

```js
if (!triResult || !Array.isArray(triResult.classes) || !Array.isArray(triResult.d_tof) || !Array.isArray(triResult.d_mel)
    || !Array.isArray(triResult.d_tof_raw) || !Array.isArray(triResult.d_mel_raw)) {
  throw new Error("malformed TriResult");
}
```

## 3. `theta_reject_tof`/`theta_reject_mel` 有沒有被誤用成同一個？

沒有——兩個獨立解構、獨立參與線性內插，程式碼裡沒有任何地方把兩者合併
成一個共用門檻。

**而且程式碼自己內建了一個執行期的自我檢查**，不是只有我讀程式碼時的
判斷：

```js
if (currentW === 1 && rejectFused !== lastTriResult.reject_tof) {
  console.error("[quiz] reject_fused(w=1) != reject_tof -- formula/backend mismatch", ...);
}
if (currentW === 0 && rejectFused !== lastTriResult.reject_mel) {
  console.error("[quiz] reject_fused(w=0) != reject_mel -- formula/backend mismatch", ...);
}
```

這條斷言在 §4.1 節的實測（見下）裡**真的被觸發執行過**（`w=1` 與 `w=0`
都測過），瀏覽器 console 沒有出現這兩條錯誤訊息，代表這個自我檢查
**通過了**，不是只是程式碼存在但沒被跑到。

## 4. 拖 `w` 滑桿時拒識結論真的會變

用 `page.route()` 攔截 `POST /recognize`，餵一組**刻意設計成在
`w=1/3` 翻轉**的假 `TriResult`：

```
d_tof_raw=[5,6,7,8]   theta_reject_tof=3.0   (index 0 全程是 min)
d_mel_raw=[1,1.5,2,2.5] theta_reject_mel=2.0
theta_fused(w) = 2 + w
combined_min(w) = 1 + 4w
拒識條件：1+4w > 2+w  =>  w > 1/3
```

用 `quiz.js` 自己的鍵盤快速鍵（`T`=w=1、`M`=w=0、`F`=w=0.5，跟拖滑桿
呼叫的是同一個 `renderFusedColumn()`）實測：

| 按鍵 | `w` | 預期 | 實測結果 |
|---|---|---|---|
| `T` | 1.00 | 拒識（`1+4=5 > 2+1=3`） | ✅ `rejected: true`，跟 `reject_tof=true` 一致 |
| `M` | 0.00 | **不**拒識（`1 > 2` 為假） | ✅ `rejected: false`，畫面真的顯示一個詞「b」，跟 `reject_mel=false` 一致 |
| `F` | 0.50 | 拒識（`1+2=3 > 2+0.5=2.5`） | ✅ `rejected: true` |

**同一筆 `TriResult`，只改 `w`，畫面在「顯示一個真實答案」跟「顯示未偵測到」
之間確實會翻轉。** 不是寫死的。

## 5. 視覺上看得出「拒識」跟「爛答案」的差別

截圖對照（`w=0` 接受一個低信心答案 vs `w=0.5` 拒識，同一筆 `TriResult`）：

**接受（`w=0`，信心度 18%，一個真實的低分答案）**：實心琥珀色邊框、
圓環有 18% 填色、大字顯示真實答案「b」、副標「信心度 18% τ=0.5 w=0.00」。

**拒識（`w=0.5`）**：虛線灰色邊框、圓環完全空心、大字顯示「未偵測到」、
副標「系統判定：不認得」、信心度欄位是 `--` 不是任何百分比。

**這兩種狀態在視覺上是不同種類，不只是「同一種畫面但比較暗」**——一個
低信心的爛答案仍然是實心邊框＋填色圓環＋一個真的字；拒識是虛線＋空心＋
文字直接說「未偵測到」。C18.md 的驗收條件（「未偵測到」要跟正常結果
同樣大方，不能是縮小或黯淡的版本）也做到了：兩種狀態字級、版面完全相同，
只有邊框樣式/顏色/圓環填色不同。

另外也測過**必定拒識**的情境（兩個模態的原始距離都遠大於各自門檻，
對應 Demo 第 4 步「純 ToF 念『四』」那種明確拒識的案例），畫面一樣正確
顯示「未偵測到」，三軌分數欄位旁的「拒識」badge（ToF/Mel/Fused 各自
獨立的小徽章）也正確顯示——這個 badge 本身也是刻意設計成中性灰階虛線
（不是紅色警示色），跟 C18 大卡片的「拒識是誠實的結果，不是失敗」一致，
`quiz.css` 裡有一段 `C25` 的註解明講這個理由。

## 6. 順便查到的一個文件小落差（沒有動 CONTRACTS.md）

`CONTRACTS.md` §4.3 的 JSON 範例（凍結區塊本身）**沒有把 `d_tof_raw`/
`d_mel_raw` 放進範例物件裡**，雖然上面的說明文字明確寫「這兩個是必要
欄位」。如果有人照著範例 JSON 字面去實作後端，會產生一個看起來合法、
實際上缺少拒識判定所需欄位的回應——前端這邊有擋（見第 2 節的
malformed check），不會算出錯的結果，但會直接進入「尚未串接」的降級
路徑而不是真的顯示結果。這是文件本身的小落差，不是 `quiz.js` 的問題，
**沒有動 `CONTRACTS.md`**，留給你判斷要不要在範例 JSON 裡補上這兩個
欄位。

## 7. 測試方法與環境清理

`mock_device.py` + `bridge_server.py` + headless Chrome（自己的空 port）
+ `page.route()` 攔截 `/recognize`（跟 `C19`/`C20` 稽核用的同一招），
兩組手工建構的 `TriResult`（見上）各自的原始距離與門檻都是手算過、
會產生已知預期結果的數字，不是隨機生成後才回頭解讀。全程用 `T`/`M`/`F`
鍵盤快速鍵而非滑鼠拖曳座標，操作更穩定可重現。測完 PID 精確 kill，
沒有動到其他 agent 的行程（這輪機器上同時有 12 組其他 agent 的
`mock_device`/`bridge_server`）。
