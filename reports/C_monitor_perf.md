# `monitor` 模式 CPU 超標調查與修復

> 由 `esp-mask-test-7c` 執行，2026-08-26。起因：`esp-mask-test-ca` 在
> `reports/PANEL_INTEGRATION.md` §5 測到 `monitor` 可見時 CPU ~30.65%
> （`C03` 預算 15%），`esp-mask-test-ad` 要求先量清楚是哪個 canvas 在燒，
> 再決定動哪裡。這份報告記錄量測方法、找到的根因、修復，以及修復後的
> 對照數字。**沒有改 `PANEL_INTEGRATION.md` 的原始數字**，只在這裡另開一節。

## 結論摘要

| 項目 | 結果 |
|---|---|
| 30% CPU 主要來源 | ❌ 不是熱力圖 DOM cell 本身貴，❌ 不是 Mel 瀑布圖，✅ **是 `drawTrail()`（PCA 軌跡）每幀讀 `canvas.clientWidth/clientHeight`，跟熱力圖的 DOM style 寫入疊在一起，逼出同步 layout（reflow）** |
| 修復方式 | 把 PCA canvas 的 CSS 尺寸快取起來，只在 init / window resize / onEnter 時重新讀，`drawTrail()` 改讀快取值 |
| 修復效果（同一台機器，headless，前後對照） | 主執行緒 `TaskDuration` 佔牆鐘時間比例：**55.9% → 21.7%**；`LayoutDuration`：**28.8% → 7.6%**；`LayoutCount`（8 秒視窗內強制 layout 次數）：**916 → 517** |
| 視覺是否有變化 | 沒有 —— 截圖比對、resize 事件重量都確認過，`drawTrail` 的 autoscale / 淡出邏輯完全沒動 |
| 剩餘的 517 次 layout | 找到但**沒有動**：`drawSparkline()`（C09 品質儀表板的迷你趨勢圖，`monitor.js` 471 行）有同樣的 `canvas.clientWidth` 每次呼叫都讀的寫法，是另一個獨立的、範圍外的（這次只被要求查熱力圖/PCA/Mel 三個）小型同類問題，記錄在下面「沒有動的問題」 |

---

## 1. 量測方法

延續 `PANEL_INTEGRATION.md` §5 的環境設定（`mock_device.py --proto v2 --mel 1
--scenario round` + `bridge_server.py` + headless Chrome `--disable-gpu
--no-sandbox`），額外用兩種工具把「30% 到底花在哪」拆開：

1. **CDP `Profiler`（V8 取樣式 CPU 分析器）**：`Profiler.start()` → 等 8 秒
   （`monitor` 模式全程可見，資料持續流入）→ `Profiler.stop()`，用回傳的
   call tree 依函式名分桶：
   - `tof_grid` 桶：`renderDistanceChannel`/`renderChannel`/`buildGrid`/
     `computeZoneStats`/`distColor`/`signalColor`/`zscoreColor`/`lerpColor`
   - `pca_trail` 桶：`drawTrail`/`projectPCA`/`fitPCA2Stub`/`powerIteration`/
     `resizePcaCanvas`/`vecSub`/`dot`/`normalize`/`matVec`
   - `mel_waterfall` 桶：`drawMelColumn`/`drawRmsColumn`/`paintMelWaterfall`/
     `shiftMelCanvasLeft`/`melColorRgb`/`drawPendingVadMarks`
2. **CDP `Performance.getMetrics()`**：修復前後各量一次 8 秒視窗的
   `TaskDuration`/`LayoutDuration`/`RecalcStyleDuration`/`ScriptDuration`/
   `LayoutCount` 差值 —— 這組數字直接對應 Chrome DevTools「Performance」
   面板的 bottom-up 分類，比單純的 V8 取樣更能看出「同步 layout」這種
   不算在 JS 執行時間裡、但確實佔用主執行緒的成本。
3. **`/proc/[pid]/stat` 加總**（ca 原本的方法，作為對照）：修 bug 後才發現
   我第一版的 crashpad 行程排除條件寫錯（`"crashpad" in cmdline` 誤判——
   **每個**子行程的 `--crashpad-handler-pid=NNNN` 參數都含有這個子字串，
   不是只有真的 crashpad handler 行程；改成比對執行檔名
   `chrome_crashpad_handler` 才對）。修好後這個數字才有意義，過程記錄在
   下面「方法論踩雷」。

修復前後都用**全新的 headless Chrome + 全新的 profile 目錄**（不重用同一個
分頁/行程），避免前一輪量測留下的狀態污染下一輪。

## 2. 修復前：CPU 分配在哪三個 canvas 上

```
=== CPU profile (V8 sampler, 8.0s window) ===
  2.6%  pca_trail 桶（含 drawTrail 本身 14.6%）
  1.7%  renderDistanceChannel（tof_grid 桶大宗）
  0.3%  mel_waterfall 桶（幾乎可忽略）
 81.7%  other_js（大部分是 idle + native "(program)"）

=== Performance.getMetrics（8s 視窗）===
TaskDuration: 55.9% of wall
LayoutDuration: 28.8% of wall   ← 主執行緒「忙碌時間」裡超過一半在這裡
RecalcStyleDuration: 0.8% of wall  ← style 重算本身其實很便宜
LayoutCount: 916 次（8 秒內，遠超過 60fps 的 rAF 次數）
```

**跟 `esp-mask-test-ca` 原本的猜測（「熱力圖是 30+ 個 DOM 節點每幀改
style，成本可能比 canvas 高」）方向對，但機制不是那個猜測本身**：
`RecalcStyleDuration` 只有 0.8%，代表單純「改 className/style.background」
本身很便宜。真正貴的是 `LayoutDuration`（同步 reflow），而 `drawTrail()`
單一函式在 V8 取樣裡就佔了 14.6%，遠高於熱力圖兩個渲染函式加總（2.5%）
和 Mel 瀑布圖全部（0.3%）。

## 3. 根因：DOM 寫入 + 下一行讀 layout 屬性 = 強制同步 reflow

`paint()`（每個 `requestAnimationFrame` 執行一次，理論上 60Hz）依序呼叫：

```js
renderDistanceChannel(...)  // 寫 A 感測器 16~64 個 cell 的 className/style
renderChannel(...)          // 寫 A 感測器訊號 channel 的 cell
renderDistanceChannel(...)  // 寫 B 感測器
renderChannel(...)          // 寫 B 感測器
...
drawTrail()                 // 第一行就讀 pcaCanvas.clientWidth/clientHeight
```

瀏覽器對 DOM 樣式的寫入通常會延遲到下一次繪製才真的重排（批次處理，很
便宜）——但如果**同一個 JS 任務裡**，寫完樣式後又讀取一個依賴版面配置的
屬性（`clientWidth`/`clientHeight`/`offsetWidth`/`getBoundingClientRect()`
等），瀏覽器沒辦法延遲，必須**立刻**把剛剛的樣式改動跑一次完整的同步
layout 才能給出正確答案——這就是俗稱的「強制同步佈局」（forced synchronous
layout / layout thrashing）。

`drawTrail()` 原本每一幀都執行 `const w = pcaCanvas.clientWidth, h =
pcaCanvas.clientHeight;`，緊接在四次熱力圖 DOM 寫入之後——**這一行讀取
本身就是那 916 次強制 layout 的觸發點**，即使 canvas 的 CSS 尺寸其實
幾乎從不改變（只有視窗真的被拖曳縮放時才會變）。

## 4. 修復

`vl53l7cx_test/monitor/panel/js/modes/monitor.js`：把 PCA canvas 的 CSS
尺寸快取進一個模組層變數 `pcaCanvasCssSize`，只在三個真正需要重新量測
的時機更新（跟 `resizePcaCanvas()` 原本就被呼叫的時機完全一致，沒有新增
呼叫點）：

- `init()` 時呼叫一次
- `window.addEventListener("resize", resizePcaCanvas)` 觸發時
- `onEnter()` 時呼叫一次（因為模式被隱藏時 canvas 可能曾經是 0 尺寸）

`drawTrail()` 改成讀 `pcaCanvasCssSize` 這個純 JS 物件，不再碰
`clientWidth`/`clientHeight`——autoscale、淡出、當前點高亮等**視覺邏輯
完全沒動**，純粹只是資料來源從「每幀強制讀 DOM 版面」換成「讀一個只在
真正需要時更新的快取值」。

## 5. 修復後對照

```
=== CPU profile（同樣 8s 視窗）===
  1.8%  tof_grid 桶
  0.8%  pca_trail 桶（drawTrail 從 14.6% 降到 0.5%）
  0.1%  mel_waterfall 桶

=== Performance.getMetrics ===
TaskDuration:      55.9% → 21.7% of wall   （主執行緒忙碌時間降了約 6 成）
LayoutDuration:     28.8% →  7.6% of wall   （強制 reflow 成本降了約 7 成）
RecalcStyleDuration: 0.8% →  0.5% of wall
LayoutCount:          916 →  517 次（8 秒內）
```

**沒有修好、但也沒被要求修的殘留 517 次**：見下方「沒有動的問題」。

**`/proc` 總 CPU（ca 原本的方法，含 GPU/raster 執行緒）修復前後幾乎沒變
（54.4% → 51.6%）**——這點刻意誠實列出，不隱藏：這台機器目前有其他
agent 同時在跑自己的 `mock_device`/`bridge_server`/測試（跑這份調查時
`ps aux` 確認過至少 4-5 組其他 agent 的行程同時存在），且 headless
`--disable-gpu` 會用 SwiftShader 軟體合成，這部分的 CPU 成本主要跟
GPU/raster 執行緒有關、跟主執行緒的 JS/layout 修復沒有直接關係——這也是
`PANEL_INTEGRATION.md` §5 自己下的警語（headless 的絕對數字不能直接當
真筆電的數字，只有相對比較可信）。**這次真正可信、乾淨、可重現的信號是
`TaskDuration`/`LayoutDuration` 這兩個「主執行緒忙碌比例」指標**——這也是
實際會不會掉幀、UI 會不會卡頓的直接原因，比總 CPU（含背景 GPU 合成
開銷）更貼近使用者體感。**E01 上機當天用真筆電（有 GPU 加速、沒有其他
agent 搶 CPU）重量一次，才能拿到跟這次可比的乾淨數字**——跟
`PANEL_INTEGRATION.md` 原本的建議一致。

## 6. 正確性檢查（沒有為了降 CPU 犧牲行為）

- 截圖比對：修復前後的 PCA 軌跡視覺一致（漸層淡出的點 + 白色外框標示
  最新點 + autoscale），肉眼比對沒有差異
- Resize 行為：手動觸發 viewport resize（1400x1000 → 900x1000），canvas
  backing store 尺寸正確從 1115x164 變成 615x164，過程 console 沒有
  任何錯誤
- 熱力圖更新率、Mel 瀑布圖捲動、PCA 軌跡衰減時間常數（`PCA_TRAIL_MS`）
  **一行都沒改**

## 7. 沒有動的問題

- **`drawSparkline()`（`monitor.js` 471 行，C09 品質儀表板的迷你趨勢圖）
  有同樣的「每次呼叫都讀 `canvas.clientWidth/clientHeight`」寫法**，是
  修復後仍殘留 517 次 layout 的可能來源之一（品質卡片可能有好幾個
  sparkline canvas，每個都這樣讀）。這次的任務範圍明確是「熱力圖 + PCA
  + Mel 瀑布圖」三個，`drawSparkline` 屬於 C09、不在範圍內，**沒有動**，
  留給之後如果還要繼續壓 CPU 預算的人參考。
- 沒有嘗試把熱力圖從 DOM cell 改成 canvas（`esp-mask-test-ca` 原本的猜測
  方向）——量出來的數字顯示這不是主要瓶頸，改了風險（重寫整個 C05/C06/C07
  的渲染邏輯）跟收益不成比例，所以沒做。

## 9. 追加：`drawSparkline()` 的同類 bug ——修了，但**沒有測出效果**

`esp-mask-test-ad` 授權順手修 §7 提到的 `drawSparkline()`（`monitor.js:471`，
C09 品質儀表板），假設它是修復後殘留 517 次 layout 的部分來源。**先量**，
誠實回報：**這個假設沒有成立**，量出來的數字幾乎沒有變化。

### 修法

跟 `drawTrail()` 同一招（快取 CSS 尺寸，不要每次都讀 `clientWidth`），但
換了一個更穩的資料來源：這次不是用 `window resize` 事件手動追蹤（sparkline
canvas 的寬度除了 window resize 以外，`.quality-grid` 的 `auto-fit` 欄位
數、捲軸出現/消失也都可能改變它，手動列舉容易漏），改用
**`ResizeObserver`**——瀏覽器原生 API，專門用來「觀察一個元素的框大小
變化」，回呼是非同步的（layout 跑完之後才通知），讀它回報的快取值不會
逼出同步 layout。6 個 sparkline canvas 共用一個 observer 實例，各自的
最新尺寸存進 `sparkSize[m.key]`，`drawSparkline()` 改吃這個快取值當第
四個參數，不再自己讀 `canvas.clientWidth/clientHeight`。

### 為什麼沒效果（先算一次數量級，量出來的數字對得上）

`drawSparkline()` 只在 `handleQualityEvent()` 裡被呼叫，一次品質事件
（實測 ~1Hz）觸發 6 個指標各呼叫一次——**上限是 8 秒視窗內 ~48 次強制
layout**，相對於 §5 量到的殘留 517 次（8 秒內），連 10% 都不到。修
`drawTrail()` 之所以有感，是因為它掛在 60fps 的 rAF 迴圈裡，量級差了
超過一個數量級；`drawSparkline()` 從一開始數量級就不夠格當主因。

### 前後對照（同機器、同方法，換了一組全新的 port 避免跟前一輪或其他
agent 的 Chrome 撞在一起）

```
                    §5 的「修 drawTrail 後」   這次「再修 drawSparkline 後」
TaskDuration          21.7% of wall              21.0% of wall
LayoutDuration          7.6% of wall               7.5% of wall
LayoutCount            517 次/8s                   510 次/8s
```

**在雜訊範圍內，沒有可信的改善。** 517 次殘留 layout 目前找不到單一
主因——比較可能是「每幀有 DOM 真的變了，瀏覽器本來就要花一次 layout
才能畫下一幀」這種不可避免的正常成本（60fps × 8s = 480，跟殘留的
510-517 已經很接近），不是還有別的強制同步讀取藏在什麼地方。

### 這次修法要不要留著

**留著。** 雖然沒測出效果，但這不是「為了效能犧牲正確性」也不是「無謂的
抽象」——`ResizeObserver` 是處理「元素尺寸變化」這件事本來就該用的標準
作法（比手動監聽 resize 事件更完整、更不會漏掉觸發點），拿掉一個真實存在
的反模式（即使目前規模下影響小到測不出來），視覺行為截圖 + 6 個 canvas
的 backing store 尺寸都確認過沒有變化。如果之後品質事件頻率提高或指標
數量增加，這個修法會開始有感——現在先做掉，之後不用再回頭補。

## 10. 測試環境清理

`mock_device.py`/`bridge_server.py`/headless Chrome 全部用自己挑的空
port（8890 / 9334）起、量完用精確 PID kill，過程中用 `ps aux` 確認過
其他 agent 的 `mock_device`/`bridge_server` 行程全程沒被動到。
