# `$A`（ambient）從解析到 `C0` 的完整路徑追查

> **唯讀**，追蹤 `host/`、`vl53l7cx_test/monitor/bridge_server.py`、
> `analysis/` 裡跟 ambient 有關的每一段，**沒有跑任何測試**（窗口保護中），
> 沒有改任何程式碼。

## 🔴 裁決（調度員 2026-08-26）：**已知缺口，刻意不補**

**不是遺漏，是評估過三個理由之後主動決定先不做**：

1. **`C0` 的 must-pass 判準不依賴它**——下面第 6 關證明過，`C0` 用
   ToF 距離差判定通過/失敗，ambient 只是加分項，`C0` 現在就能正常跑。
2. 🔴 **要補就得動 `Aligner` 這個核心類別**——`Aligner` 是訓練/推論
   統一之後所有東西的共用地基，**在真實資料還沒進來之前動它，
   風險大於收益**。
3. **使用者現在被硬體卡住**（兩顆 ToF 剛在真板子上同時掉線，見
   `BOOT_OUTPUT.md` §0.7 的實例更新）——**這才是真的擋著他的東西**，
   不是這個缺口。

**⚠️ 什麼時候該補**：當有人真的需要「距離差還沒到 2mm、但 ambient
已經升高」那種**輕微**串擾的偵測能力時——那是 `ambient` 作為 `D10`
講的「最靈敏指標」才用得上的場景，現在的 `C0` 粗判準還用不到。到那
時候要動的是 `Aligner`（加緩衝區/`push_ambient()`/`AlignedFrame`
欄位）+ `bridge_server.py` 兩個分發函式，不是一行 `elif` 能解決的，
規模跟這份報告下面查到的一致。

---

## 結論先講（技術細節）：**第 2、3 關不通，而且比之前五個「白名單濾掉」更根本——
不是被濾掉，是這一段從來沒有被實作過。第 6 關（`C0`）已經預先防住了這個
缺口，不會誤判，但會少一個 `D10` 講的「最靈敏指標」。**

---

## 逐關結果

| 關卡 | 結果 | 摘要 |
|---|---|---|
| 1. `protocol.py` 解析出 `type="ambient"` | ✅ 通 | 這輪修好的 |
| 2. `bridge_server.py` 事件分發 | 🔴 **不通** | 兩個獨立的地方都沒有 `ambient` 分支 |
| 3. SSE 事件 / `to_sse_event()` | 🔴 **不通** | 直接落到 `else: return None`，面板永遠收不到 |
| 4. `SessionWriter` 寫進 HDF5 | 🔴 **沒有東西可寫**（不是寫入端壞了） | schema 支援，但全repo找不到一個真的呼叫點會傳 `tof_ambient_A=` |
| 5. `session_loader` 讀回來 | ✅ 讀取端本身沒問題 | `stacked_ambient()` 正確、對缺資料優雅處理成 `None` |
| 6. `run_all.py` 的 `C0` | ✅ **已經預先防住**，不會誤判 | 主判準不依賴 ambient，缺資料時誠實印警告，不會捏造數字 |

---

## 第 2 關：`bridge_server.py` 的兩個獨立入口都沒有 `ambient`

`handle_parsed_event()`（`bridge_server.py:270`）對每個解析出來的事件做兩件事：

```python
observe_for_quality(parsed)
sse = to_sse_event(parsed)
```

**兩個都沒有處理 `ambient`：**

### 2a. `observe_for_quality()`（`bridge_server.py:327-368`）

```python
if kind in ("tof", "mic", "mel"):
    ...
    session_aligner.push_event(event)
    ...
    trial.push_event(event)
```

`kind in ("tof", "mic", "mel")` 這個判斷式**沒有 `"ambient"`**——一個
`type="ambient"` 的事件進到這個函式，第一個 `if` 就跳過，**`session_aligner`
跟 `trial`（正式錄音時累積資料、最後餵給 `SessionWriter.write_trial()`
的那個狀態機）永遠不會看到它**。往下的 `elif kind == "tof"` /
`"mic"` / `"mel"` / `"heartbeat"` / `"status"` 也沒有 `ambient` 分支，
所以 `drop_tracker`／`quality` 儀表板同樣不會有任何 ambient 相關的統計。

### 2b. `to_sse_event()`（`bridge_server.py:207-267`）

```python
if kind == "tof": ...
elif kind == "mic": ...
elif kind == "mel": ...
elif kind == "heartbeat": ...
elif kind == "status": ...
elif kind == "record": ...
else:
    return None
```

`ambient` 落到最後的 `else: return None`。**這個跟之前 `$H` 的
`host` 統計被白名單濾掉是同一個形狀（有一個明確的分支表，沒在表上的
一律丟），但更徹底**——`$H` 那次是「算出來了，最後一步濾掉」，這裡是
「連分支都沒開」，SSE 面板完全收不到任何 ambient 事件，不會顯示、
不會報錯，安靜消失。

---

## 更深一層：`Aligner` 這個類別本身就沒有 ambient 的概念

**這是比「bridge_server.py 忘了接」更根本的一層**——就算把上面兩個
分支補上，`session_aligner.push_event(event)` 呼叫的
`host/align/aligner.py:Aligner.push_event()` 本身：

```python
def push_event(self, event: dict) -> None:
    etype = event.get("type")
    if etype == "tof": self.push_tof(...)
    elif etype == "mic": self.push_mic(...)
    elif etype == "mel": self.push_mel(...)
```

**也沒有 `elif etype == "ambient"`，而且 `Aligner` 類別裡根本沒有
`self._ambient` 這個緩衝區、沒有 `push_ambient()` 方法**——搜尋整個
`aligner.py`，`ambient` 這個字**零筆命中**。`AlignedFrame`（`frames()`
吐出來的那個 dataclass）**也沒有 `ambient` 欄位**。

**代表就算 `bridge_server.py` 補上分支，`Aligner` 這個底層類別自己
也還需要先加東西（緩衝區、push 方法、`AlignedFrame` 的欄位），不是
改一行 `if` 就能接通的。** 這是為什麼我說這個缺口比之前那五個「同一個
形狀」更根本——那五個是「算好了、最後一關濾掉」，這個是「從最底層的
資料結構開始就沒有這個東西」。

---

## 第 4 關：`SessionWriter` 寫入端本身沒問題，但全 repo 沒有一個真的呼叫點

`host/storage/session_writer.py` 的 `write_trial()` 確實支援
`tof_ambient_A`/`tof_ambient_B`/`tof_ambient_t_us` 三個選填參數
（第 304 行起），也有驗證邏輯（`_validate_ambient_trio()`，三個一起有
或一起沒有）。**這段程式碼本身沒問題**——問題是**全 repo 搜尋
`tof_ambient_A\s*=` 這個呼叫模式，除了 `session_writer.py` 自己的定義
跟它自己的測試，零筆命中**。沒有任何地方（`bridge_server.py`、任何
`analysis/experiments/` 底下的腳本）真的呼叫過
`write_trial(..., tof_ambient_A=..., ...)`。**這跟第 2、3 關的結論
完全對得上**：因為 `Aligner`/`bridge_server.py` 從沒收集過 ambient
資料，自然也沒有東西可以傳給 `SessionWriter`。

---

## 第 5 關：讀取端沒問題，但沒東西可讀

`analysis/reporting/session_loader.py:133-138` 的 `stacked_ambient()`：

```python
def stacked_ambient(self, sensor="A"):
    """整個 session 的 ambient 串起來，或 `None`（§2 選填欄位）。"""
    attr = "ambient_a" if sensor == "A" else "ambient_b"
    blocks = [getattr(t, attr) for t in self.trials
              if getattr(t, attr) is not None and getattr(t, attr).size]
    return np.concatenate(blocks, axis=0) if blocks else None
```

寫得很乾淨：任何 trial 沒有 ambient 資料就跳過，全部沒有就回 `None`，
**不會拋例外、不會補假值**。但因為第 2-4 關從沒真的寫過 `tof_ambient_A`
到任何真實 session 檔，這個函式在真實資料上**永遠回 `None`**——不是
它有問題，是上游沒東西給它讀。

---

## 第 6 關：`C0` 已經預先防住了這個缺口——**這是好消息，糾正一下急件裡的說法**

急件裡說「一個 must-pass 的硬體閘門永遠驗不了」——**查過
`analysis/run_all.py:640-712` 之後，這句話需要修正：`C0` 的
PASS/FAIL 判準跟 ambient 完全無關**：

```python
delta = mod.zone_distance_delta(solo_dist, solo_valid, dual_dist, dual_valid)
verdicts[sensor] = mod.crosstalk_verdict(delta)   # ← C0 的通過/失敗只看這個
...
solo_amb = pair.solo.stacked_ambient(sensor)
dual_amb = pair.dual.stacked_ambient(sensor)
if solo_amb is not None and dual_amb is not None:
    _, rate = mod.zone_ambient_delta(solo_amb, dual_amb)
    ambient_rates[sensor] = rate
...
passed = all(v["passed"] for v in verdicts.values())   # ← 只看 verdicts，不看 ambient_rates
```

**`C0` 用的是 ToF 距離差（`zone_distance_delta`），ambient 只是額外
加分項**——`_crosstalk_markdown()`（第 717 行起）的註解甚至明講了這件事：

> `exp_d10_crosstalk.format_report()` 要求 A/B 兩顆的 verdict **與**
> ambient 變化率**都在**。實務上 ambient（`$A` 幀）是新加的，多數資料
> 還沒有——**不能為了呼叫它而餵零進去**，那是捏造一個「ambient 完全
> 沒變」的結論。

沒有 ambient 資料時，報告只會印一行誠實的警告
（`⚠️ 沒有 ambient 資料（tof_ambient_*，§2 選填）...`），**`passed`
這個布林值完全不受影響**，`C0` 這個硬體閘門**現在就能正常跑、正常
判定通過或失敗，不會被 ambient 缺席卡住**。

**真正的代價**：`D10` 文件裡講 `ambient_per_spad` 是 crosstalk
「最靈敏的指標」——距離差還沒大到 2mm、但 ambient 已經明顯上升的
那種**輕微**串擾，現在偵測不到，只能等距離差真的夠大才會被 `C0` 抓到。
這是**偵測靈敏度打折**，不是「`C0` 完全跑不了」。

---

## 修正一下今天的「第六個最後一關」清單

`ed` 列的五個例子共通點是「算好了、傳到最後一步被濾掉」（白名單、
只掃一層、忘了 passthrough）。**這個 ambient 缺口不是同一個形狀**：

- **不是「算好了被濾掉」**：`Aligner` 這個最底層的類別**從來沒有
  收集過** ambient 資料，連緩衝區都沒有，不是「算出來、最後一關丟掉」，
  是「根本沒有算」。
- **要接通需要三處都補（`Aligner` 加緩衝區+push方法+`AlignedFrame`
  欄位、`bridge_server.py` 兩個分發函式各加一個分支、可能還要在正式
  錄音流程裡開 `AMB:1` 的地方確認有觸發)**，不是加一行 `elif` 就好。
- **但 `C0` 本身沒被卡死**——這點跟原本的擔心不一樣，值得先讓使用者
  安心：`E01` 照計畫做 `C0` 串擾測試，不會因為這個缺口而卡關，只是拿
  不到 `D10` 說的「最靈敏指標」那個加分項。

**要不要現在補上 `Aligner`+`bridge_server.py` 這一段，還是先讓 `C0`
用距離差跑過再說，由你判斷——這次只追蹤，沒有改動任何程式碼。**
