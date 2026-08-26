# `first_session_check.py` 獨立審查

> **審查方法**：唯讀 `ssi-backlog/tools/first_session_check.py`、
> `ssi-backlog/tools/test_first_session_check.py`，交叉核對
> `host/storage/session_writer.py`（今天新增的三個 schema 欄位定義）跟
> `analysis/reporting/session_loader.py`（確認欄位讀得到）。跑過
> `pytest ssi-backlog/tools/test_first_session_check.py` 確認目前是綠的
> （14 passed），沒有改任何程式碼。

## 結論先講：**五項乾淨，一項是真的缺口——今天剛做出來、專門對付「中途掉線」的
`sensors_seen` 欄位，這支工具完全沒有讀**

---

## 1. 🔴 唯一的 STOP 條件真的只有結構性問題嗎？——**查過了，是**

逐一核對 `CHECKS` 清單裡每個函式，`v.stop()` 只在四個地方被呼叫：

| 呼叫點 | 觸發條件 | 是不是結構性 |
|---|---|---|
| `check_session()` | `load_session()` 讀檔拋例外 | ✅ 檔案本身壞了 |
| `check_baseline_slot()` | 找不到任何 `label=="_baseline"` 的 trial | ✅ baseline 被蓋掉 |
| `check_monotonic_time()` | `tof_t_us` 出現往回跳 | ✅ 裝置重置過，時間戳不可信 |
| `check_mic_signal_level()` | 整個 session 每一幀 RMS **恆為 0** | 見下方第 2 項 |

其餘五個 check（`check_invalid_zones`／`check_mic_noise`／
`check_clipping`／`check_drop_counts`／`check_vad_chain`）**只呼叫
`v.info()`／`v.warn()`，程式碼裡完全沒有 `v.stop()`**——逐行確認過，
沒有哪個「數字異常」被偷偷接成 STOP。`check_mic_noise()` 甚至在
docstring 跟訊息裡兩次明講「300 這個門檻是照假資料訂的，跳黃/紅燈不代表
裝置有問題」，態度一致。**沒有找到會因為一個沒校準過的門檻讓使用者
被迫停下來的路徑。**

---

## 2. ⚠️ 麥克風那條：判準真的是 `== 0` 而不是某個小門檻嗎？——**查過了，是精確等於 0**

```python
n_zero = int(np.sum(all_rms == 0))
...
if n_zero == all_rms.size:
    v.stop(...)
```

精確的整數相等比較，不是 `< 5` 或某個容忍範圍——跟今天早上驗證過的
「真板子安靜時 RMS 4-6 是正常的」完全不衝突：4-6 遠大於 0，不會被這條
誤傷。

**「只有一筆 trial」時還成立嗎？**——成立，`all_rms` 是把**所有** trial
的 `mic_rms` 串接起來的一維陣列，`n_zero == all_rms.size` 這個相等比較
跟陣列來自幾筆 trial 無關，1 筆或 100 筆邏輯完全一樣。

**「某幾筆是 0、某幾筆不是」時呢？**——`n_zero < all_rms.size`，
不觸發 STOP，正確（某幾幀剛好安靜到量出 0 是完全正常的事，只有**全部**
都是 0 才代表訊號鏈可能真的斷了）。這兩個情境都有測試直接覆蓋：
`test_mic_rms_constantly_zero_is_a_stop`（全 0 → STOP）跟
`test_mic_rms_zero_in_only_some_trials_is_not_a_stop`（部分 0 → 不 STOP），
兩個都通過。

⚠️ **一個沒被明確測到、但邏輯上沒問題的細節**：`all_rms` 的串接**包含
`_baseline` trial 自己的 `mic_rms`**（不像 `check_invalid_zones()` 明確
排除 baseline）。這代表如果只有 baseline 那一筆麥克風是死的、後面真實
錄音的麥克風是活的（或反過來），串接後 `n_zero < size`，**不會觸發
STOP**——這是對的行為（判準本來就是「整個 session 完全沒有任何訊號」，
不是「有一筆是死的」），只是說明這條 STOP 條件的覆蓋範圍是「麥克風從頭到
尾完全沒接上」這個最極端情況，比「某幾筆錄音麥克風斷線」寬鬆，這是設計
選擇不是 bug，但值得知道它抓不到「麥克風中途才斷線」這種情況（跟下面
第 5 點的 `sensors_seen` 缺口是同一類問題，只是這裡是麥克風、那裡是
ToF）。

---

## 3. 🔴 `trial_000` 是 baseline 這個假設——**查過了，用 `label`，不是索引**

`check_baseline_slot()` 的判定邏輯：

```python
baseline_trials = [t for t in session.trials if t.label == BASELINE_LABEL]
if not baseline_trials:
    v.stop(...)
```

**決定 STOP 與否的唯一依據是 `label == "_baseline"`**，docstring 也明講
了為什麼：「這裡用『找不找得到 `_baseline` 這個 label』而不是『`trial_000`
是不是 baseline』來判斷——被蓋掉的那種撞號，結果就是完全沒有 `_baseline`
這個 trial 了」。

**索引（`_trial_index()`）只用在事後的一個 `v.warn()`**：如果找到的
`_baseline` trial 不是在 `trial_000`（`idx != 0`），只是警告「位置不尋常，
值得看一眼但不一定是錯」，**不影響 STOP 判定**。這代表這支工具**不跟
`session_writer` 的索引配置方式綁死**——就算之後 `first_trial_idx` 改成
別的數字，這支工具的核心判斷（有沒有 baseline）不受影響，符合 `4f`
今天實測確認 `first_trial_idx=1` 的情境。

`test_missing_baseline_is_a_stop` 直接測了「整個 session 沒有任何
`_baseline`」這個情境（模擬撞號覆蓋的結果），通過。

---

## 4. `tof_t_us` 往回跳的判定——**查過了，訊息有講清楚原因**

```python
v.stop(
    f"{t.key}（label={t.label!r}）的 {name} 出現 {n_back} 次時間往回跳"
    "——像是裝置中途重置過，這個 trial 的時間戳不可信"
)
```

訊息裡明確講了「像是裝置中途重置過」（`t_us` 往回跳的真正成因），不是只
講「發現異常」四個字讓人自己猜。`test_backward_timestamp_is_a_stop`
測過，通過。

---

## 5. 🔴 今天新增的三個 schema 欄位（`sensors_seen`、`baseline_age_s`、
`lip_onset_us_A/B`）——**兩個完全沒讀，一個沒問題，這是這次審查最重要的發現**

逐一核對：

### `lip_onset_us_A`／`lip_onset_us_B`——**沒直接讀，但不是缺口**

`check_vad_chain()` 讀的是 `lip_onset_us`（**單數、融合後的結果**），
不是 `_A`/`_B` 版本。查過 `session_writer.py:329-335` 的欄位定義：
`lip_onset_us` 本身**仍然維持是融合後的單一結果**，`_A`/`_B` 是「額外
新增」的、各自獨立的唇動時間戳，不是取代原本的 `lip_onset_us`。所以
`check_vad_chain()` 讀的欄位名稱**沒有過期，仍然是對的、有效的欄位**，
不算缺口——只是沒有額外去看兩顆感測器唇動偵測的**個別**狀況，這是可以
做但不是「現在讀的東西已經失效」那種急迫性。

### `baseline_age_s`——**完全沒讀，缺口**

全檔案搜尋 `baseline_age_s`，**零筆命中**。這個欄位的用途是「這筆 trial
錄的當下，baseline 是幾秒前錄的」——`session_writer.py` 的註解明講
`E05` 要錄 4 小時，baseline 一定會過期，`record.js` 目前沒有過期偵測。
`first_session_check.py` 是使用者錄完**第一筆**時跑的工具，這個當下
`baseline_age_s` 通常還很小、不是問題最大的時候，**但它完全不檢查這個
數字，代表如果之後有人想把這支工具也用在錄到一半的健檢，這個欄位一樣會
被忽略**——目前不算緊急缺口（story 本身定位是「第一筆」的健檢），但既然
欄位今天剛做出來，這裡先記下來。

### 🔴 `sensors_seen`——**完全沒讀，而且這正是它今天被做出來要解決的問題**

全檔案搜尋 `sensors_seen`，**零筆命中**。查過 `session_writer.py:351-360`
的完整定義：這個欄位**同時存在於 `/meta`（到 baseline 為止看到什麼）跟
每一筆 trial（這筆 trial 期間看到什麼）**，文件裡明講**兩者不一致，
就是「session 中途掉線」的證據**——`analysis/reporting/session_loader.py`
（第 395-417 行附近）已經有另一段程式碼在讀 `/meta` 的 `sensors_seen`
做跨 session 一致性檢查，代表這個欄位不是紙上談兵，**已經有其他程式碼在
用它偵測異常**，只是 `first_session_check.py` 沒有接上。

**這件事的重要性，直接對應到今天稍早這個協調員自己派工調查過的問題**
（`reports/BOOT_OUTPUT.md` §0.7）：**感測器中途斷線，韌體端完全不會有
任何提示，`$H` 的 `drop_*` 計數器凍結不動，`seq` 直接停止跳動，主機端
`DropTracker` 的 seq-gap 機制也偵測不到——`sensors_seen` 正是為了補上
這個盲區才被做出來的欄位**。`first_session_check.py` 是使用者錄完第一筆
**立刻會跑**的工具，如果他的 B 感測器接觸不良、在錄音途中斷線（今天
`ad` 也實測遇到過這件事——`E01_bringup_checklist.md` §0.5 那次接線
間歇性斷開），**這支工具現在完全看不出來，會印出「✅ 結構檢查通過，
可以繼續錄」，而使用者接下來錄的幾百筆裡，只要感測器又斷過，都不會被
這支工具抓到**。

**這是這次審查裡唯一夠格叫做「有問題」的發現**——不是邏輯錯誤，是一個
剛做出來、專門解決這類問題的資料**還沒被接上使用它的地方**。

**建議（只提案，沒有改程式碼）**：加一個新的 check 函式，比較每筆 trial
的 `attrs.get("sensors_seen")` 跟 `/meta` 的 `sensors_seen`——不一致就是
`v.warn()`（不建議設成 `v.stop()`：`sensors_seen="A"` 只代表「線上看到
A 的資料」，不是「A 這顆實體壞了」，跟 `check_mic_noise()` 的態度一致，
數字異常先當警告不是硬性停止）。

---

## 6. 舊檔案（沒有新欄位）跑這支會怎樣？——**查過了，優雅降級，不會炸**

全部用到選填欄位的地方都是 `.get(key)`（隱含 `None` 預設）加上
`is None` 判斷再決定要不要印警告，例如：

```python
mu = session.meta.get("noise_floor_mu")
if mu is None:
    v.warn("/meta 沒有 noise_floor_mu——baseline 沒算過噪音門檻")
    return
```

`t.attrs.get("comparable") is True`、`t.attrs.get("drop_count")` 等
其餘用法同一個模式——舊檔案缺欄位時 `.get()` 回 `None`，不會拋
`KeyError`，只會被歸類成「這個資訊不存在」印一行 warn 或 info，不影響
其他 check 繼續跑。**由於 `sensors_seen`／`baseline_age_s` 現在根本沒被
讀取，它們也不可能在舊檔案上造成任何例外**——這點跟第 5 項的缺口是
同一件事的兩面：沒讀 = 不會因為它們不存在而出錯，但也代表新舊檔案在這
支工具眼中「看起來一樣安全」，即使新檔案裡明明有一個能抓到問題的欄位。

---

## 總結：有沒有情況會讓它給出「看起來合理但錯誤」的結論？

**五項（1/2/3/4/6）程式碼與測試都乾淨，沒有找到會安靜給錯結論的路徑，
STOP 條件確實只保留給結構性問題，麥克風判準確實是精確的 `==0`，
baseline 判斷確實用 label 不用索引，舊檔案確實會優雅降級。**

**第 5 項是真的問題**：`sensors_seen` 這個今天剛做出來、專門偵測
「感測器中途掉線」的欄位完全沒被這支工具讀取，而**中途掉線**正是這個
專案這幾天反覆撞到的真實硬體問題（`E01_bringup_checklist.md` §0.5/§0.7）
——這代表使用者跑完這支工具看到「✅ 結構檢查通過」時，**沒辦法排除
「錄到一半有顆感測器斷過線」這個具體、已知會發生的情況**。這不是
「看起來合理但錯誤」（工具不會給假的肯定訊號騙人），比較接近「工具的
守備範圍比使用者以為的窄一格」——STOP/WARN 邏輯本身沒有錯，只是少了
一項本來可以做、而且素材已經準備好的檢查。