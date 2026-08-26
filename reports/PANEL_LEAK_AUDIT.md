# 五模式反覆切換稽核 — 有沒有東西沒被關掉

> 由 `esp-mask-test-7c` 執行，2026-08-26。起因：`C03` 的模式路由每個模式
> 分開測過，但沒人測過「一直切換」。這類專案反覆出現的失敗型態是「每一層
> 各自正確，接縫上錯，而且沒有任何東西會報錯」——`onLeave` 有沒有真的關掉
> `onEnter` 啟動的東西，正好是一個接縫。

## 結論摘要

| 檢查項目 | 結果 |
|---|---|
| `requestAnimationFrame` 活躍迴圈數（10 圈切換） | ✅ 全程穩定在 1，沒有累加 |
| `setInterval`/`clearInterval` 淨數量（10 圈切換） | ✅ 全程穩定在 3，沒有累加 |
| `window`/`document` 上的 event listener 數量（10 圈切換） | ✅ 全程完全不變 |
| `performance.memory.usedJSHeapSize`（100 秒、跨越 65 秒 retention 邊界） | ✅ 線性成長到 ~65 秒後打平——**這是 ring buffer 填滿到穩態的正常現象，不是洩漏** |
| `monitor.js` 的 `/config/quality_thresholds` 輪詢 | 🔴 **找到真的洩漏，已修**——離開 `monitor` 後仍持續打，10 秒一次 |
| `monitor.js` 的 `/pca?model=tof_only` 輪詢 | ✅ 離開 `monitor` 後仍持續打，**但這是刻意的**（`C10.md` 要求），不是洩漏 |
| `record.js` / `validate.js` / `replay.js` | 只查看，沒有動；本輪用同一套方法掃過，沒發現明顯異常（見第 4 節，範圍不如 `monitor.js`/`quiz.js` 深入） |

---

## 1. 方法

`headless Chrome`（自己的空 port）+ `mock_device.py --mel 1 --scenario round`
+ `bridge_server.py`，`playwright` `connect_over_cdp` 連線後用
`context.add_init_script()` 在**任何 app 程式碼跑之前**注入攔截層：

- 包 `requestAnimationFrame`/`cancelAnimationFrame`，追蹤目前還沒被呼叫也
  沒被取消的 rAF id 數量（穩定的迴圈在任一時間點應該恆為某個小整數，不會
  隨切換次數增加）
- 包 `setInterval`/`clearInterval`，同樣追蹤淨數量
- 包 `EventTarget.prototype.addEventListener`/`removeEventListener`，**只**
  記錄掛在 `window`/`document` 上的（其餘元素的 listener 隨 DOM 節點存活/
  消滅，不是這次要查的洩漏類型）
- 包 `window.fetch`，記錄每次呼叫的 URL 與時間戳——用來直接抓「離開某個
  模式之後，還有什麼在背景打 API」

五個模式依序切換（`monitor → record → quiz → validate → replay`）跑
**10 圈**，每圈結束時（切回 `monitor`）取一次快照。另外用
`/proc/[pid]/stat` 加總（`esp-mask-test-ca`/`C_monitor_perf.md` 同一套
方法）量整個 10 圈跑下來的平均 CPU%，以及用 `window.gc()`
（Chrome 加 `--js-flags=--expose-gc` 啟動）在每次讀 heap 前強制回收，讓
`performance.memory.usedJSHeapSize` 的讀數不被「剛好還沒被 GC」污染。

## 2. rAF / interval / listener：三項全部乾淨

```
                    baseline   lap1   lap2   ...  lap9   lap10
activeRaf              1        1      1     ...    1      1
activeIntervals         3        3      3     ...    3      3
window:hashchange      1        1      1     ...    1      1
document:keydown       4        4      4     ...    4      4
window:resize          1        1      1     ...    1      1
document:keyup         1        1      1     ...    1      1
```

10 圈（= 50 次模式切換）下來，這三類數字**完全沒有變化**。`monitor.js`
自己的 `onEnter`/`onLeave` 正確配對（`rafId` 該啟動時啟動、該取消時取消），
`shell.js` 的 `registerMode`/`enterMode`/`leaveMode` 契約本身也經得起反覆
呼叫（讀過原始碼：`modeEntered`/`modeInitDone` 兩個旗標各自做了 idempotency
guard，`init()` 全程只跑一次，`onEnter`/`onLeave` 每次切換各跑一次，不多
不少）。

## 3. heap 成長：不是洩漏，是 ring buffer 填滿到穩態

**第一輪 10 圈稽核測到 heap 從 3327 KB 漲到 4956 KB**（10 圈約 35-45 秒），
乍看像洩漏。**追出真正的形狀**：拉長到 100 秒、跨越
`bus.js` 的 `RETENTION_MS = 65000`（65 秒）這個邊界，同時每 0.5 秒切一次
模式取樣一次 heap：

```
t= 0.5s   heapKB=3151
t=10.7s   heapKB=3978
t=20.8s   heapKB=4646
t=30.9s   heapKB=5175
t=41.0s   heapKB=5660
t=51.1s   heapKB=6109
t=58.7s   heapKB=6667   ← 接近 65 秒
t=65.0s   heapKB=6393   ← 65 秒邊界附近，成長開始趨緩
t=71.6s   heapKB=6617
t=82.5s   heapKB=6594
t=90.4s   heapKB=6532
t=100.1s  heapKB=6323   ← 100 秒時比 65 秒時還低
```

**65 秒之前線性成長，65 秒之後打平、在 ±300KB 內震盪，不再淨成長。**
這正是 `bus.js` 的四個 ring buffer（`tofA`/`tofB`/`mic`/`mel`）+ `quality`
從空的開始填、填滿 65 秒份的資料後進入「新進一筆、舊的被 `trim()` 踢出
一筆」的穩態——`bus.js` 自己的註解也說了 ring buffer 是刻意設計成**不隨
模式切換清空**的（切回去要看得到歷史）。

**排除了「其實是 mock_device 中途掛掉，資料停了，heap 才停止成長」這個
混淆因素**：另外量過，100 秒測試跑到最後，`monitor` 模式的 Hz 讀數仍是
穩定的 30.0 Hz（不是衰減趨近 0），證明整段測試資料持續在流動，heap 打平
是 ring buffer 到穩態，不是資料源死掉。

**這是有效的「查了、沒有洩漏」的結論**，不是我為了有東西可以修硬找的。

## 4. 找到的真洩漏：`monitor.js` 的 `/config/quality_thresholds` 輪詢

**這是這次 C09 門檻即時抓取（上一輪剛加的功能）自己引入的 bug，在這裡
自己抓到、自己修掉。**

### 根因

```js
// 舊寫法（init() 裡，只跑一次，永遠不會被清掉）
fetchQualityThresholds();
setInterval(fetchQualityThresholds, QUALITY_THRESHOLDS_CHECK_MS);
```

`init()` 全程只執行一次（`registerMode` 的 `modeInitDone` guard），這個
`setInterval` 建立之後**沒有任何地方存它的 handle、也沒有任何地方
`clearInterval` 它**——不管使用者切到哪個模式，它都每 10 秒打一次
`/config/quality_thresholds`，一直打到分頁關掉為止。

### 怎麼抓到的、怎麼確認乾淨

先切到 `replay` 模式，清空 fetch 記錄，等 25 秒（超過兩個 10 秒週期），
**修復前**看到：

```
t=10088ms  /config/quality_thresholds
t=20090ms  /config/quality_thresholds
```

修復後同樣的測試，25 秒視窗內**這個 URL 完全消失**，只剩下 `/pca`（見
下一節，這個是刻意的）跟 `/device/state`（`shell.js` 的 C04 全域狀態列，
本來就設計成跟模式無關、一直在跑）。

### 修法

`monitor.js`：把這個 timer 從 `init()` 移到 `onEnter()`/`onLeave()`，
用跟 `rafId` 一模一樣的守門寫法（存 handle、`onEnter` 啟動、`onLeave`
清掉），並且 `onEnter` 時立刻打一次（不用等 10 秒），讓切回 `monitor`
時 badge 馬上是新的，不會等到下一個 timer tick 才更新。實測切走→等
25 秒→切回，badge 依然正確顯示 `[暫定]`/`[契約]`，console 沒有錯誤。

## 5. 沒有修的東西：`/pca?model=tof_only` 輪詢，因為它是故意的

同一次測試也看到 `/pca?model=tof_only` 在離開 `monitor` 之後仍然每 10 秒
打一次（`SERVER_MODEL_CHECK_MS`）。**一開始以為這也是洩漏，追下去發現
不是**：

`pcaBookkeeping(now)`（`/pca` 輪詢就在這裡面）是從 `onData(evt)` 呼叫的，
而 `onData` 是 `bus.js` 的 `forEachRegisteredMode` 機制——**設計上每個
已註冊的模式，不論可不可見，每一個事件都會收到**。`monitor.js` 這段
呼叫旁邊本來就有註解寫明原因：

> 「Runs every onData call regardless of visibility -- per C05/C07's
> established pattern and C10.md's explicit requirement, trajectory
> history and the live-fit model both keep accumulating while hidden;
> only drawTrail()（in the rAF loop）actually stops.」

也就是說 `C10.md` 明確要求 PCA 軌跡歷史跟即時擬合模型**在隱藏時也要繼續
累積**，這樣切回 `monitor` 時軌跡是連續的、不是從頭開始——這跟 `quality`
門檻 badge 的情況完全不同：badge 純粹是顯示用的，沒有任何「隱藏時累積
起來有價值」的理由，隱藏時純粹是浪費。**同一種輪詢寫法，一個有正當理由
留著跑，一個沒有，所以只修了後者。** 這個判斷差異寫在這裡，供之後review。

## 6. `record.js`/`validate.js`/`replay.js`——只查，沒有修

用同一套注入層跑過 10 圈，rAF/interval/listener 三項數字全程也是穩定的
（沒有隨切換次數增加）。**但這三個模式不是我這輪的檔案，稽核深度不如
`monitor.js`/`quiz.js`**——沒有像 `monitor.js` 那樣逐行讀過每個
`setInterval`/`fetch` 呼叫點去判斷「該不該在隱藏時繼續跑」。如果之後
`4f`/`ed` 要更仔細查自己的檔案，這次量出來的 baseline 數字（活躍 rAF=1、
interval=3、`window`/`document` listener 四項）可以直接拿去對照。

## 7. `quiz.js`——縮圖 canvas 沒有發現洩漏

`C19` 的三張縮圖（ToF/Mel/PCA）是**一次性繪製**（`onRecognizeClick` 觸發
一次，不是每幀重畫的迴圈），沒有自己的 `setInterval`/`rAF`。10 圈切換
測試裡 `quiz` 模式的 heap 讀數（見第 3 節的表）跟其他模式同一個量級、
同一個「65 秒後打平」的形狀，沒有額外異常成長。

## 8. 測試環境清理

`mock_device.py`/`bridge_server.py`/headless Chrome 全部用自己挑的空
port（8895/9341、之後驗證用的 8896/9342）起、測完精確 PID kill。這台
機器這輪跑的時候同時有至少 6-7 組其他 agent 的行程，`ps aux` 確認過
全程沒有動到它們。
