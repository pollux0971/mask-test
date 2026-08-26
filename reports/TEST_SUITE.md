# 全庫測試套件 — 執行指南與已知狀態

這份文件回答一個目前為止沒人回答過的問題：**全庫測試到底乾不乾淨？**
至少四個 agent 回報過「幽靈失敗」（連跑兩次紅的檔案不一樣、單獨跑全綠），
但沒有人真的跑過一次乾淨的全庫測試來確認。這份報告記錄調查結果。

> ## 🔴 2026-08-26 更新：幽靈失敗**被重現了**，而且根因不是環境
>
> 這份文件原本的結論是「**沒有重現任何一次幽靈失敗**，最可能的根因是
> 多個 agent 的 pytest 行程同時在跑」。那個推論**對於當時測到的東西是正確
> 的**，但**不完整**：
>
> * 幽靈失敗**在單一 pytest 行程裡就能穩定重現**，不需要其他 agent
> * 根因是一段**明確的程式機制**（見「🔴 根因」那節），不是籠統的「環境抖動」
> * 修好之後 `vl53l7cx_test/monitor/` 從 `3 failed / 53 passed / 63 errors`
>   變成 **`123 passed / 0 failed / 0 errors`**
>
> **當時的調查沒有錯，是它跑的規模還沒大到觸發。** `vl53l7cx_test/monitor/`
> 在原調查時只有 38 個測試（250 秒），現在是 123 個（627 秒）——
> **測試變多本身就是觸發條件**（見根因）。
>
> 下面原本的內容保留，因為那些排查（共用 fixture、pty、檔案路徑）**仍然
> 有效**，省下重查的功夫。

## 結論（原調查）：截至目前為止，序列（單一 pytest 行程、不跟其他 agent 同時跑）
## 執行全庫測試是乾淨的

在**這次調查當下**（下方「總數」是調查完成時的快照，這個數字會持續成長——
D/C 軌還在開發），完整跑過三種方式，全部 0 失敗：

| 執行方式 | 測試數 | 結果 | 耗時 |
|---|---|---|---|
| `analysis/` + `ssi-backlog/tools/` 單獨跑 | 246 | 全綠 | 64s |
| `host/`（不含 mock_device）單獨跑 | 449 | 全綠 | 30s |
| `host/` 的 9 個 `*_mock_device.py` 逐檔單獨跑 | 25 | 全綠 | 累計 ~113s |
| `host/` 的 mock_device 測試合併一起跑 | 28（`-k` 關鍵字比對範圍略有出入，見下方說明） | 全綠 | 113s |
| `host/` 全部一起跑 | 489 | 全綠 | 144s |
| `vl53l7cx_test/monitor/` 全部一起跑 | 38 | 全綠 | 250s |
| **四個目錄一次跑完（單一 pytest 行程）** | **843**（調查當下；本文件完稿前重新 collect 已經漲到 890，其他 agent 還在加測試） | **全綠** | **467s（約 7 分 50 秒）** |
| 特別針對 `esp-mask-test-4f` 回報過的 `test_session_writer_mock_device.py` 連續重跑 5 次 | 1 | 5/5 全綠 | 每次 ~2.2s |

**沒有重現任何一次「幽靈失敗」。** 這強烈指向：之前回報的幽靈失敗，根因是
**多個 agent 的 pytest 行程真的同時在跑**，不是測試本身的邏輯問題（下方「已知
不穩定的原因」有推論）。

## 建議的執行順序與指令

**四個目錄可以在同一台機器上依序（不是同時）各自獨立跑，不需要固定順序**——
彼此之間沒有共用的檔案系統狀態或全域變數（下方有搜過）。實務上建議由快到慢：

```bash
# 1. 最快，先跑，壞了馬上知道（~65s）
.venv/bin/python3 -m pytest analysis/ ssi-backlog/tools/ -q

# 2. 中等（~145s）——host/ 的 mock_device 測試已經證實跟其他 host 測試混在一起跑沒事，
#    不需要特別分開
.venv/bin/python3 -m pytest host/ -q

# 3. 最慢，SSE/心跳類測試有內建的真實時間等待（~250s）
.venv/bin/python3 -m pytest vl53l7cx_test/monitor/ -q

# 或者四個一次跑完（~470s，約 8 分鐘，注意 pytest 預設不會印進度到很細，
# 底下的 --durations=15 可以看出最慢的幾條）
.venv/bin/python3 -m pytest host/ analysis/ ssi-backlog/tools/ vl53l7cx_test/monitor/ -q --durations=15
```

**不建議同時（背景）跑多個目錄。** 不是因為驗證出真的會撞，而是：
1. 沒有 CPU/IO 隔離的話，實測時間敏感的測試（`quality_event_arrives_at_about_1hz`
   這類，見下方「已知不穩定的原因」）失敗機率會提高，跟系統當下有多忙有關。
2. 這正是懷疑中「幽靈失敗」的根因——見下一節。

**多個 agent 要跑測試時**：理想情況是排隊序列跑，不要同時跑。這份報告没有
辦法驗證這件事（我沒辦法真的叫另一個 agent 跟我同時跑來重現），只能從
「單一行程序列跑非常穩」加上「時間敏感測試的存在」推論出最可能的根因。

## 已知的環境需求

- **`.venv`**：`pip install -r requirements.txt`（`pyserial numpy pandas h5py
  librosa scipy pytest matplotlib scikit-learn joblib`）。這份清單本身也在
  持續成長，跑之前先重新 `pip install -r requirements.txt` 一次比較保險。
- **會開子行程的測試**：檔名含 `mock_device` 的全部（`host/` 底下 9 個檔案）
  以及 `vl53l7cx_test/monitor/test_bridge_*.py`（透過 `ssi-backlog/tools/mock_device.py`
  開一個真的 pty + 假裝置子行程）。這些測試會呼叫 `pty.openpty()`
  ——**每次呼叫都拿到核心動態配的全新 pty 路徑**（不是固定路徑），所以
  pty 本身不是共用資源、不會撞路徑。
- **不需要實體硬體**：這四個目錄的測試全部用 mock/合成資料，`vl53l7cx_test/main/`
  （韌體）本身沒有 pytest 測試（那邊用 `idf.py build`，不在這份報告範圍）。
- **matplotlib 後端**：所有畫圖的測試檔開頭都已經 `matplotlib.use("Agg")`，
  無頭環境不需要額外設定。

## 🔴 根因：`hold_duration` 用的是**裝置時間戳**，不是牆上時間

`host/trial/state_machine.py:479`：

```python
hold_duration_s = (device_t_us - self._hold_start_device_t_us) / 1e6
```

`device_t_us` 來自 `bridge_server.py` 的 `device_clock["last_t_us"]`——
**最後一次收到的 `$T`/`$M` 行上面的時間戳**，不是主機的時鐘。

所以一個測試寫：

```python
_request(rig, "POST", "/trial/hold/start", {})
time.sleep(0.8)                                    # ← 測試這邊過了 0.8 秒
assert _request(rig, "POST", "/trial/hold/stop")[1]["state"] == "REST"
```

**測試這邊確實睡了 0.8 秒，但 bridge 那邊的 `last_t_us` 只有在
serial reader 執行緒拿到 CPU、真的讀到新行時才會前進。**
機器忙的時候那個執行緒被餓著，`last_t_us` 幾乎不動——

### 三筆獨立觀測

| 來源 | `hold_duration_s` | 那 0.8 秒裡讀到了什麼 |
|---|---|---|
| 修 helper 時（`test_bridge_trial_api` / `test_e2e_pipeline`） | **`0.128023`** | 讀到**一些**裝置事件，但遠少於實際 |
| `esp-mask-test-ed`（做 `/replay/*`，跑整個 `vl53l7cx_test/monitor/`） | **`0.0`** | **一行都沒讀到** |
| `esp-mask-test-18`（做型別接縫檢查，跑 `test_e2e_pipeline.py`） | **`0.0`** | 同上 |

`0.128023` 那筆的完整事件：
```
'state': 'CONFIRM', 'warning': 'too_short', 'hold_duration_s': 0.128023
```
`0.0` 那兩筆長得一樣，只是數字是 `0.0`。

**0.8 秒的按壓被算成 0.128 秒（或 0.0 秒）**，於是狀態機**正確地**判定
「太短」，走到 `CONFIRM`（問使用者）而不是直接 `SAVE`。
**程式完全正確，是測試的時間假設不成立。**

### 為什麼那兩筆 `0.0` 特別重要

**`0.128` 還看得出一點比例，`0.0` 是比例完全消失**——那 0.8 秒內
`device_clock["last_t_us"]` **一次都沒有前進**。

`0.0` **不是理論極限，是真的發生過，而且發生過兩次**（兩個不同的 agent、
不同的時間、不同的工作內容）。它們是下面「這不是慢了一點」那節最強的證據。

### 為什麼「測試變多」本身就是觸發條件

`vl53l7cx_test/monitor/` 的每個測試都要起**一對子行程**（mock device + bridge）。
原調查時 38 個測試，現在 123 個——**同一個 pytest 行程在 10 分鐘內連續起了
約 90 對行程**。前面測試留下的行程還在收尾，後面測試的 bridge 就在跟它們搶
CPU。**不需要第二個 agent，一個 pytest 行程自己就會把自己餓到。**

這也解釋了為什麼原調查沒重現：**當時的規模還不夠。**

### 這不是「慢了一點」

如果只是慢一點，加大容忍窗就好。但 `last_t_us` **完全不前進**時，
量到的時長跟真實時長之間**沒有比例關係**——sleep 再久也沒用。
所以修法不能是「睡久一點」。

上面那兩筆 `0.0` 就是這句話的證明：**測試那邊睡了 0.8 秒，量到 0；
睡 8 秒也會量到 0。** 加大容忍窗對「沒有比例關係」完全無效。

> 📌 **那兩筆 `0.0` 原本只存在於兩個 agent 的完成回報訊息裡，沒有進任何
> 檔案。** 寫這一節時我只有自己量到的 `0.128`，一度把 `0.0` 寫成「理論極限」
> ——因為**查不到出處的數字不能當觀測寫**。出處補上之後才知道它真的發生過
> 兩次。
>
> 這正是 `reports/HANDOFF.md` §4.4 講的那件事：**寫在訊息裡等於沒寫。**
> 現在它們在這裡了。

## 🔴 守門員清單：這些測試紅了**代表真的有問題**

> **這一節跟下一節的判準表是同一件事**，只是換個方向切：
> 判準表教你**怎麼判斷一條沒見過的測試**；這一節列出**已知的、
> 絕對不可以放寬的那幾條**。
>
> ⚠️ **它們的共同特徵：斷言本身就是題目。** 放寬 = 刪掉它，只是看起來還在。

| 測試 | 它在守什麼 | 放寬的後果 |
|---|---|---|
| `test_grouping_removes_the_wear_leakage`<br>（`test_d18_permutation_test.py`） | 按 `wear_id` 分組之後準確率**必須下降** | 🔴 **這條測試會自己偵測自己失效**——失敗訊息就寫著「洩漏訊號可能沒有生效，這條測試就失去意義了」。放寬它，整個分組 CV 的正當性就沒有證據了 |
| `test_single_group_is_reported_not_silently_downgraded` | 只有一個 group 時**必須明講「分組驗證無法進行」** | **宣稱一個沒做的方法學保證**——比不做還糟 |
| `test_matches_the_c21_worked_example`<br>（`test_effect_size.py`） | Python 的 Wilson CI 與 `quiz.js:602` **算出同一個數字** | **Demo 上的數字與報告上的數字會靜靜漂開** |
| `test_three_of_three_gives_a_very_wide_interval` | 小樣本的 CI **必須很寬**，且解讀文字要說「寬度本身就是結論」 | 有人為了「數字好看」改成 90% 信心水準或常態近似，**而那正是 `C21` 當初拒絕的東西** |
| `test_above_chance_uses_the_ci_lower_bound_not_the_point_estimate` | 「比隨機好」**必須用 CI 下界比** | **算了 CI 卻拿點估計去比基準，等於白算** |
| `test_extras_are_populated_for_the_three_way_vote`<br>（`test_run_all.py`） | 「第二顆 ToF 有沒有用」的三方投票**不可以是空的** | 🔴 **這條的存在就是因為它真的壞過**：讀錯一個鍵名讓 `D13` 那一票永遠是 `None`，交叉檢查永遠只有兩票、永遠不會發現矛盾，**而報告看起來完全正常** |
| `test_ablation_can_be_switched_off_but_says_so` | 關掉 `D19` 時要講出「三方投票少一票」 | **「沒有資料」與「沒有矛盾」變得分不出來** |
| `test_an_in_range_hold_saves_without_asking`<br>（`test_bridge_trial_api.py`） | 正常長度的 hold **不會反問** | 見下一節——**它的名字就是斷言** |
| `test_a_too_short_hold_goes_to_confirm...`<br>`test_confirm_keeps_a_pending_trial`<br>`test_discard_drops_a_pending_trial` | 太短的 hold **必須**落到 `CONFIRM` | 那是「狀態機不猜」這條設計的唯一證據 |
| `test_pure_noise_is_classified_weak_not_medium`<br>（`test_d14_viseme_sensitivity.py`） | 純雜訊**不可以**被判成「中等敏感」 | **整張熱力圖會變成看起來很有內容、實際上量的是雜訊** |
| `test_truncated_heartbeat_is_still_malformed`<br>（`test_protocol.py`） | 放寬前向相容**不可以**放寬到連截斷都吃下去 | 傳輸損壞會被當成正常資料 |

> 🔴 **新增守門員時請加進這張表。** 一條沒有被記錄的守門員，
> 在它紅的那天跟一條普通測試長得一模一樣。

## 🔴 判準：紅燈是「環境」還是「它就是在測這件事」

**這是這份文件最重要的一段。** 碰到這類偽陽性紅燈時，**不要「紅了就放寬」**。

一開始的判準是「**看那個 hold 時長是不是刻意的**」——但那會誤放行一條：

```python
def test_an_in_range_hold_saves_without_asking(rig):   # ← 名字就是斷言
    time.sleep(1.0)                                     # ← 1.0 秒，一點都不「刻意」
    assert "SAVE" in states
    assert body["state"] == "REST"
```

**1.0 秒完全是個普通數字，但這條測試存在的唯一理由就是
「in-range 的 hold 不會問」。** 放寬成接受 `CONFIRM` = 變成
「會存，可能先問一下」= **等於刪掉它，只是看起來還在**。

→ **正確的判準不是「時長是不是刻意的」，是「那個斷言本身是不是題目」。**

| 測試 | hold | 斷言是題目嗎 | 處理 |
|---|---|---|---|
| `_recorded_session` / `_record_one_in_range_trial` helper | 0.8–1.2s | ❌ 只是為了弄到一筆存好的 trial | 放寬 ✅ |
| `test_a_saved_trial_does_not_destroy_the_baseline` | 1.2s | ❌ 題目是「baseline 還在」 | 放寬 ✅ |
| `test_reject_marks_a_saved_trial_without_deleting_it` | 1.2s | ❌ 題目是「拒絕 ≠ 刪除」 | 放寬 ✅ |
| `test_hold_capture_is_not_ended_by_the_ticker` | 2.0s | ❌ 題目是「ticker 不會替你結束」 | 放寬 ✅ |
| **`test_an_in_range_hold_saves_without_asking`** | **1.0s** | 🔴 **是**——名字就是斷言 | **不動** |
| `test_a_too_short_hold_goes_to_confirm_instead_of_guessing` | 0.1s | 🔴 **是** | **不動** |
| `test_confirm_keeps_a_pending_trial` | 0.1s | 🔴 **是**（硬斷言 `CONFIRM`） | **不動** |
| `test_discard_drops_a_pending_trial` | 0.1s | 🔴 **是**（硬斷言 `CONFIRM`） | **不動** |

**「放寬」的具體做法**（只套用在 ✅ 那幾列）：

```python
state = _request(rig, "POST", "/trial/hold/stop")[1]["state"]
if state == "CONFIRM":
    assert _request(rig, "POST", "/trial/confirm")[0] == 200
else:
    assert state == "REST", f"hold/stop 回到未預期的狀態: {state}"
```

落在 `CONFIRM` **不是失敗**——狀態機刻意**不猜**、改問使用者，
確認之後 trial 一樣會存起來，helper 要的東西拿得到。

### 那三條硬斷言 `== "CONFIRM"` 的在高負載下**反而安全**

想一下負載會把結果推向哪個方向：**負載只會讓量到的 hold 更短，
而更短仍然是 `CONFIRM`**。所以它們不需要處理。

**「時間敏感的測試都危險」是錯的直覺**——要看負載把結果推向哪一邊。

## ⚠️ 殘留：有一條刻意不修

`test_an_in_range_hold_saves_without_asking` **在極高負載下仍會偽陽性失敗**。

**而且這是對的**：它分不出「負載讓 hold 變短」與「in-range 的閘門真的壞了」，
**而它的職責就是後者**。放寬它就沒有任何測試在守那個閘門了。

根治要把 hold 時長改成從「收到 HTTP 請求的時刻」起算——但那是
`state_machine.py` 的**語意改動**，與「最小改動」的指示衝突。**刻意留著。**

看到它紅的時候：先確認是不是自己一個人在跑，再判斷。

## ⚠️ 一條通則：fixture setup 失敗會讓一整批測試**從沒被執行過**

修好之前，`test_e2e_pipeline.py` 有 **6 個 `test_session_loader_*`** 卡在
fixture setup（`_record_one_in_range_trial()` 就是 fixture 的一部分），
所以**那 6 個測試的程式碼從來沒有被執行過一次**。

**它們在 pytest 報告裡長得像 `ERROR`，不像「從沒跑過」。** 而 `ERROR` 很容易
被跟 `FAILED` 一起掃過去，或被當成「環境問題，重跑就好」。

修好之後它們**第一次真的跑起來**，而且全過——但
**「跑起來了而且全過」跟「一直都是綠的」是兩回事**，值得分開講。

> **下次看到一批 `ERROR` 集中在同一個檔案時，先問「這些測試到底有沒有被
> 執行過」**，而不是先問「它們為什麼失敗」。

## ⚠️ 待觀察：一次 `ConnectionRefusedError`，沒能重現

整套跑的其中一輪，`test_bridge_verify_api.py::test_report_files_are_served_with_the_right_mime`
出現 `ConnectionRefusedError`（bridge 中途沒回應）。

* **只出現過一次**
* 該檔案單獨跑 17/17 全綠，之後整套再跑一次也全綠
* **沒能重現，所以下面是懷疑方向不是結論**

懷疑方向：`POST /verify/run` 的背景執行緒會 `import` sklearn 與 matplotlib，
**那是 bridge 做過最重的事**。若之後又看到 bridge 中途沒回應，
這個方向值得先查。

## 已知的不穩定測試與原因

**目前沒有找到真的會壞的測試**（見上方「結論」）。以下是主動排查過、
**確認不是問題**的懷疑項目，記錄下來讓下一個人不用重查一次：

| 懷疑項目 | 排查方法 | 結論 |
|---|---|---|
| 共用 fixture／全域狀態污染 | `grep` 搜尋 `host/`、`analysis/`、`vl53l7cx_test/monitor/` 底下模組層級的可變狀態（`^_x = `、`@lru_cache`、`_CACHE = ` 等樣式） | 沒找到任何一個；每個模組的狀態都在函式/類別內部，沒有模組層級的可變單例 |
| 多個 `mock_device` 子行程搶 pty | 讀 `ssi-backlog/tools/mock_device.py:470`，`pty.openpty()` 每次呼叫都由核心配一個新路徑 | pty 配置本身不是固定共用資源；`esp-mask-test-4f` 回報的 `test_session_writer_mock_device.py` 連續單獨重跑 5 次全綠，**這輪沒有重現** |
| 測試寫到同一個 `data/`／`reports/` 路徑互相覆蓋 | `grep` 所有 `test_*.py` 裡對 `data/`／`reports/`／`/tmp/` 的檔案存取，確認都經過 `tmp_path`／`monkeypatch` | 沒找到任何測試直接寫死路徑，全部用 pytest 的隔離 fixture |

> 🔴 **下面這段推論已被上面的「根因」那節超越。** 它猜的方向（時間敏感 +
> CPU 競爭）是對的，但**機制猜錯了**：不是「事件到達延遲變大、超出容忍窗」，
> 而是**裝置時間戳整段不前進，量到的時長跟真實時長沒有比例關係**。
> 兩者的差別很實際——前者可以靠加大容忍窗解決，後者不行。
> 保留原文，因為「猜對方向但猜錯機制」本身值得記著。

**最可能的真正根因（推論，非確診）**：`vl53l7cx_test/monitor/test_bridge_sse.py`
這類測試本質上是**依賴真實時間**的（`quality_event_arrives_at_about_1hz`
從名字就看得出來要等大約 1 秒鐘的真實時間去觀察一個週期性事件）。這類測試在
系統負載輕（只有我一個行程在跑）時很穩，但**如果同時有好幾個 agent 各自的
pytest 行程在搶 CPU**，事件到達的實際延遲會變大，容易超出測試原本抓的容忍窗口
——這是**系統忙碌造成的時序抖動**，不是測試邏輯或共用資源的 bug。
`vl53l7cx_test/monitor/` 這個目錄整體要 250 秒才跑完 38 個測試（平均每個
6.6 秒），時間敏感型測試的比例明顯比其他三個目錄高，這也支持這個推論。

**沒有調整任何斷言去讓測試變綠**——上面全部是排查後認定「這輪沒問題」，
不是放寬門檻讓它們過。

## 總測試數與預期通過數

> ⚠️ 這個數字是移動目標。D/C 軌還在開發，寫報告當下 collect 到的數字
> （下方「基準快照」）到完稿前已經漲了一輪。**不要把這個數字當成寫死的
> 常數比對**——每次要確認有沒有退步，先重新跑一次
> `pytest --collect-only -q` 拿當下的真實總數，再跟上一次的通過數比較
> 才有意義。

**基準快照**（原調查完整跑過、確認 0 失敗的那一輪）：

```
843 passed, 14 warnings in 467.42s
```

### 2026-08-26 修正後的 `vl53l7cx_test/monitor/` 快照

> ⚠️ 同一條「移動目標」警語適用：**這也是快照，不是常數。**
> 這個目錄從原調查的 38 個測試長到 123 個，**而它還在長**。

| 時間點 | 結果 | 耗時 |
|---|---|---|
| 修之前 | **3 failed, 53 passed, 63 errors** | 316s |
| 修前三處 helper 之後 | 1 failed, 122 passed, 0 errors | 651s |
| **修完五處（現況）** | **123 passed, 0 failed, 0 errors** | **627s** |

**怎麼量的**：單一 pytest 行程、`-q`，指令是
```bash
.venv/bin/python3 -m pytest vl53l7cx_test/monitor/ -q
```
⚠️ **量的當下有其他 agent 在同一台機器上活動**，所以耗時（627s）比乾淨環境
下應該更長。**通過數是可信的，耗時只能當量級參考。**

⚠️ **中間那一列的「1 failed」是上面「待觀察」那節的
`ConnectionRefusedError`，不是 helper 的問題**——它在下一輪就消失了。

### ⚠️ 重跑之前先確認機器狀態

`vl53l7cx_test/monitor/` 一輪要 **10 分鐘、起約 90 對子行程**。

**如果使用者正在跑實體量測（`E` 系列，一次連續 4.5 分鐘），或不確定他有沒有
在跑——不要重跑，用這份文件裡已有的數字。** 搶走 CPU 毀掉的是一次沒辦法
重來的實體量測。

（而且，照上面的根因，**重跑本身就會製造它想量的那個問題**。）

**14 個 warning 全部檢查過，都是良性的**（不是本次調查引入，附上一併記錄）：
- `d10_crosstalk` 的 `test_zone_ambient_delta_all_nan_zone_returns_nan`：
  測試本身就在驗證「全 NaN 的 zone 該回傳 NaN」這個邊界情況，
  `np.nanmean` 對全 NaN 輸入示警是預期行為，不是 bug
- `d14_viseme_sensitivity` 的 `cmap.set_bad()`：matplotlib 未來版本的
  API 棄用警告，不影響目前行為
- `d12_wear_cv` 的「開了超過 20 張圖」：測試裡多次呼叫畫圖函式沒有
  逐一 `plt.close()`，純粹是資源提醒，不影響測試結果
- `feature_assembly` 的 `joblib`/numpy 版本相容性警告（PCA 模型存檔重載）

**`E01` 上機當天的檢查建議**：跑一次
`pytest host/ analysis/ ssi-backlog/tools/ vl53l7cx_test/monitor/ -q`，
比對「總數變多是正常的（新 story 加測試），**通過數應該等於總數**」——
只要出現任何 `F` 或 `E`，先照這個順序判斷：

1. **是不是自己一個人在跑？**（其他 agent、使用者的實體量測）
2. **是不是「判準」那節表格裡標 🔴 的那幾條？** 那些紅了**代表真的有問題**，
   不可以放寬
3. **`ERROR` 集中在同一個檔案嗎？** 先問「這些測試有沒有被執行過」，
   而不是「它們為什麼失敗」

## 調查方法（供之後的人重現或延伸）

1. `pytest --collect-only -q` 先拿測試總數，不執行
2. 由快到慢分段跑：`analysis/`+`tools/` → `host/`（先排除 mock_device 再全部）→
   `monitor/` → 四個一起跑，每一段都記錄「乾不乾淨」與耗時
3. 對懷疑的檔案（`esp-mask-test-4f` 點名的 `test_session_writer_mock_device.py`）
   連續重跑 5 次確認穩定性
4. `grep` 排查三類已知的多 agent 共用工作目錄風險：全域可變狀態、
   固定共用檔案路徑、pty/子行程資源配置方式
5. 全部乾淨 → 寫這份報告，記錄基準快照與方法，不去無中生有地「修」
   沒有壞的測試
