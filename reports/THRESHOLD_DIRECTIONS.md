# 品質儀表板：六個指標的 `direction` 逐項核對

> 範圍：一開始只讀 `host/quality/metrics.py`、`config/quality_thresholds.json`
> 做核對，**兩個修法提案經調度員核准後這一輪已經動手做了**（見文末「已實作」
> 一節）；`config/quality_thresholds.json` 的數字**沒有動**，方向/邏輯層
> 修法都不碰 `green`/`yellow` 門檻。原始核對逐項回答三個問題——方向對不對、
> 另一端的極端值代表什麼、有沒有「只有中間才對」的指標。

## 結論先講

- **6 個指標裡，確認有問題的是 1 個（`noise_floor`），高度懷疑但尚未觀察到
  的是 1 個（`bandwidth`）。**
- `drop_rate`／`valid_zones`／`symmetry`／`clock_resid` 這 4 個**已經有
  正確的防護**：完全沒資料時回傳 `None`（→ `level="unknown"`），不是
  一個看起來像「完美」的 0。**調度員原本對 `drop_rate`／`symmetry`／
  `clock_resid` 的懷疑，逐一讀過程式碼後沒有成立**——照實回報沒問題。
- **沒有找到「只有中間才對，單一 `direction` 表達不了」的指標。** 六個
  現有指標的「越極端越好」在邏輯上都是對的；`noise_floor`／`bandwidth`
  的問題不是方向錯，是**極端值的其中一種成因（裝置故障）被誤判成另一種
  成因（真的表現良好）**，兩者用同一個方向、同一個門檻表達不出來。

---

## 逐項表格

| 指標 | `direction` | 低端極值代表什麼 | 高端極值代表什麼 | 方向對嗎 | 有沒有「完美分數其實是故障」的風險 |
|---|---|---|---|---|---|
| `drop_rate` | `lower_better` | 0 ＝真的沒掉幀（**或**完全沒收到任何幀——見下方，已被防護） | 1 ＝全部掉光 | ✅ 對 | ❌ 沒有，`total=0` 時回 `None`，見下 |
| `valid_zones` | `higher_better` | 0 ＝所有 zone 都無效（沒戴好／超出量程） | 1 ＝所有 zone 都有效 | ✅ 對 | ⚠️ 見下方「次要觀察」，不是方向問題 |
| `symmetry` | `lower_better` | 0 ＝左右完全對稱 | 大 ＝左右嚴重不對稱 | ✅ 對，0 真的是理想值，不是「太對稱也不正常」 | ❌ 沒有，兩顆都沒資料時回 `None`，見下 |
| `clock_resid` | `lower_better` | 0 ＝對齊殘差極小（幾乎不可能真的是 0，UART 抖動不會消失） | 大 ＝時鐘對不齊 | ✅ 對 | ❌ 沒有，樣本不足時回 `None`，見下 |
| `noise_floor` | `lower_better` | 0 ＝**沒有下限**，跟「環境極安靜」和「麥克風沒接上」數值上完全相同 | 大 ＝環境很吵 | ✅ 方向本身對（安靜比吵好），🔴 但沒有下限保護 | 🔴 **確認有，見下** |
| `bandwidth` | `lower_better` | 0 ＝鏈路幾乎沒流量，跟「解析度低所以本來就不用多少頻寬」和「裝置完全沒在送資料」數值上難以區分 | 大 ＝鏈路快滿載 | ✅ 方向本身對（頻寬有餘裕比較好） | ⚠️ **高度懷疑，見下** |

---

## 🔴 確認有問題：`noise_floor`

**麥克風完全沒接上（`$M` 的 `rms` 恆為 0）跟環境安靜，在這個指標上是同一個
數字。** `_noise_floor()` 直接對近期 rms 取第 10 百分位，沒有下限檢查：

```python
def _noise_floor(self):
    return _percentile([v for _, v in self._rms], NOISE_FLOOR_PERCENTILE)
```

`direction: lower_better` 加上 `green: 300` 意味著任何 `<= 300` 都是綠燈
——`rms` 恆為 0（麥克風斷路、沒插好、線材斷裂）完全落在這個範圍內，
**而且是這個指標能顯示的最好結果**。跟環境安靜（例如 rms 在 20-50 之間
波動）在儀表板上只差在同樣是綠燈，不會有任何視覺差異提醒操作者去檢查。

**這不是方向錯——「底噪低比底噪高好」這件事本身沒有問題。問題是「低到
完全沒有訊號」跟「低但仍然是麥克風收音」是兩種不同的物理狀態，只用一個
`lower_better` 加一個下限門檻表達不出這個差異。**

### ✅ 已實作（調度員核准後這輪動手做了）

沒有改 `direction`，沒有動 `green`/`yellow` 的數字。加了一個獨立的
存活檢查 `_mic_all_zero()`：這個視窗裡的 `rms` **全部**精確等於 0
才視為死亡，不是「低於某個門檻」——真板子實測過，貼合骨傳導麥克風安靜時
RMS 是 4-6（`22` 查過韌體確認正常，沒有任何縮放/截斷），**任何大於 0 的
下限都會誤殺這個正常值**，所以判準刻意只抓「恆為 0」這一種情況，跟
`ssi-backlog/tools/first_session_check.py` 判斷一筆錄好的 session 用的
是同一個條件（`n_zero == all_rms.size`），不是另外發明一個可能對不上的
門檻。命中時把 `noise_floor` 的等級直接覆寫成 `red`，附一句說明「這是
精確的 0，不是安靜」的提示，做法跟既有的 `_transport_alarms()`／
`stale_streams()` 覆寫 `level` 的機制一致。

測試：`host/quality/test_metrics.py` 新增 4 個——恆為 0 觸發 `red`、
只要有一筆非 0 就不觸發、真實安靜值（4-6）維持 `green` 不受影響、
既有的「低但非 0」案例（`test_noise_floor_is_a_low_percentile_not_the_mean`）
沒有被誤傷。

---

## ⚠️ 高度懷疑但尚未觀察到：`bandwidth`

`_bandwidth()` 已經對「完全沒有任何 bytes」做了防護：

```python
def _bandwidth(self, now):
    if not self._bytes:
        return None
    ...
```

**但這個防護只擋得住「一個 byte 都沒收到」，擋不住「只收到心跳，沒收到
任何 ToF/Mic/Mel 資料」。** `$H` 心跳本身也會消耗頻寬（雖然量很小），
如果裝置只送心跳、完全沒有 ToF／Mic 資料流過來（比 `sensors_seen=""`
更輕微的情境——連線還在，$H 還在跳，但感測資料整個斷了），`_bandwidth()`
會算出一個很低的非零數字，落進 `green`（`<= 0.6`），**看起來完全正常，
跟「解析度設得低、本來就不需要多少頻寬」的健康狀態沒有任何區別**。

跟 `noise_floor` 是同一種形狀：**低頻寬本身不是問題（4×4@30Hz 不開 Mel
本來就只要約 18.7% 頻寬，這是 `B20` 報告量出來的正常值），問題是「低頻寬
因為設定本來就輕量」跟「低頻寬因為裝置根本沒在送有效資料」用同一個數字
表達不出來。**

標成「高度懷疑但尚未觀察到」是因為：這個推論目前只從程式碼邏輯推出來，
沒有像 `noise_floor` 那樣有真板子上的實際案例（`8f` 是先在真板子上看到
麥克風斷路被判成全綠，回頭才查出 `noise_floor` 的成因）。建議：如果之後
`E05` 錄製過程中出現「感測器斷線但 bandwidth 卻是綠燈」的案例，直接對照
這份報告就能定位到這裡，不用重新從頭查起。

### ✅ 已實作（改用 `ed` 剛加的 `stale_streams()`，不是原提案那版）

原提案是拿 `valid_zones`/`noise_floor` 是否為 `None` 來交叉判斷——寫這段
之後發現 `ed` 剛好加了 `stale_streams()`（逐串流的逾時偵測），**這正是
同一件事的更準確版本**：`valid_zones`/`noise_floor` 是 `None` 只代表
「這個視窗完全沒有樣本」，跟「串流已經逾時、確認死亡」不是同一件事
（例如視窗剛清空但下一幀馬上要到），改用 `stale_streams()` 的判斷結果
（加上「這個串流從來沒被看過」的狀態）就不用發明第二套「這條線是不是
死的」邏輯，兩個機制才不會對同一件事給出不同答案。

實作：新增 `_no_payload_flowing()`，檢查 `tof_A`/`tof_B`/`mic` 是否
**全部**都在 `stale_streams()` 回報的名單裡、或從來沒被看過（`heartbeat`
刻意排除——它正是唯一在所有感測器都死掉時還會持續跳動的串流，算進去
會讓這個檢查失效）。三者全部「死或從未出現」時，把 `bandwidth` 的等級
覆寫成 `unknown` 並移除多餘的 hint，不管原本的門檻分類是什麼。

測試：`host/quality/test_metrics.py` 新增 2 個——只剩心跳時 `bandwidth`
被降成 `unknown`；至少一個 payload 串流還活著時維持原本的門檻分類不受
影響。另外跑過 `test_bridge_sse.py` 對真的 mock device 子行程（55 個
測試全過），確認正常情況下（payload 真的在流動）不會被誤判成
`unknown`。

---

## 已經正確防護、調度員原本的懷疑沒有成立的三個指標

### `drop_rate`：**沒有問題**

`host/capture/dropwatch.py` 的 `DropTracker.overall_drop_rate()`：

```python
total = missing + received
return missing / total if total else None
```

`total == 0`（這個視窗裡完全沒收到任何幀，也沒偵測到任何缺口）時明確
回傳 `None`，映射到 `level="unknown"`，**不會**被算成 0 掉幀率。今天在
`sensors_seen` 那一輪已經確認真板子會出現「某顆感測器整段沒有任何 `$T`
行」的情境——這種情況下 `drop_rate` 會正確顯示 `unknown`，不是誤導性的
綠燈。**調度員的懷疑（「恆為 0 可能代表沒在數」）方向是對的直覺，但這個
指標已經照這個直覺做了防護，不需要再修。**

### `symmetry`：**沒有問題，「只有中間才對」的懷疑不成立**

```python
if len(means) < 2:
    return None
```

兩顆感測器只要有一顆完全沒有資料（`_sensor_mean[sensor]` 是空的），就
回傳 `None`，不會算出一個看起來正常的對稱值。而「0 是不是也可能是故障」
這個假設，實際檢視後不成立：`_symmetry()` 需要兩顆感測器**各自都有**
有效資料才算得出數字，一顆死掉不可能產生一個假的 0——它會先變成
`None`。**0（完全對稱）在這個指標的定義下就是單純的物理事實：兩顆感測器
量到的平均距離一樣**，沒有找到「太對稱也是問題」的實際機制，這個指標
不屬於「只有中間才對」那一類。

### `clock_resid`：**沒有問題**

```python
if aligner is None or aligner.n_buckets < MIN_CLOCK_BUCKETS:
    return None
```

樣本不足（還沒校準、或校準用的 bucket 數不夠）時回傳 `None`，不會顯示
一個誤導性的 0。而且實務上，真實 UART 排程抖動幾乎不可能讓 p95 殘差
精確收斂到 0，所以就算防護失效，「看起來完美」的機率也遠低於
`noise_floor`／`bandwidth` 這兩個可以真的卡在物理上的 0 的指標。

---

## 次要觀察（不是方向問題，記錄但不列入上面的核心結論）

`valid_zones` 的 `higher_better`／100% 有效**方向本身沒有錯**：這個指標
量的是「zone 判定為有效目標的比例」，感測器對準且貼合良好時，真實接近
100% 是合理、健康的結果，不像 `noise_floor`／`bandwidth` 那樣有一個
物理上該存在卻被吃掉的下限。**唯一值得記錄、但不算「方向錯」的觀察**：
這個指標量的是韌體回報的 `target_status` 位元，如果感測器卡住、持續回報
同一個「有效」狀態而不是真的在量測（跟 `symmetry` 那類「完全死亡」不同
的、更隱蔽的「假活著」故障），`valid_zones` 本身無法分辨——但這需要
「感測器卡死但持續回報固定的有效讀數」這個具體故障模式，目前沒有證據
（真板子案例、韌體行為）顯示這是真的會發生的情境，記錄下來供之後參考，
不當作這輪的結論。

---

## 這輪動了什麼、沒動什麼

`host/quality/metrics.py`：加了 `_mic_all_zero()`、`_no_payload_flowing()`
兩個方法跟 `snapshot()` 裡對應的兩處覆寫、`_DEAD_MIC_HINT` 訊息常數；
`host/quality/test_metrics.py` 加了 6 個測試。**`config/quality_thresholds.json`
一個數字都沒動**——兩個修法都是邏輯層的存活檢查/交叉核對，`direction`
跟 `green`/`yellow` 完全沒有變。沒有碰 `analysis/`、`panel/**`，也沒有動
`bridge_server.py`（它只是呼叫 `QualityAggregator.snapshot()`，這輪的
改動對它透明，不需要跟著改）。跑過 `host/quality/`（39 個測試）跟
`test_bridge_sse.py`（55 個測試，含真的 mock device 子行程），全過。
