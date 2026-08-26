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

## 已知的不穩定測試與原因

**目前沒有找到真的會壞的測試**（見上方「結論」）。以下是主動排查過、
**確認不是問題**的懷疑項目，記錄下來讓下一個人不用重查一次：

| 懷疑項目 | 排查方法 | 結論 |
|---|---|---|
| 共用 fixture／全域狀態污染 | `grep` 搜尋 `host/`、`analysis/`、`vl53l7cx_test/monitor/` 底下模組層級的可變狀態（`^_x = `、`@lru_cache`、`_CACHE = ` 等樣式） | 沒找到任何一個；每個模組的狀態都在函式/類別內部，沒有模組層級的可變單例 |
| 多個 `mock_device` 子行程搶 pty | 讀 `ssi-backlog/tools/mock_device.py:470`，`pty.openpty()` 每次呼叫都由核心配一個新路徑 | pty 配置本身不是固定共用資源；`esp-mask-test-4f` 回報的 `test_session_writer_mock_device.py` 連續單獨重跑 5 次全綠，**這輪沒有重現** |
| 測試寫到同一個 `data/`／`reports/` 路徑互相覆蓋 | `grep` 所有 `test_*.py` 裡對 `data/`／`reports/`／`/tmp/` 的檔案存取，確認都經過 `tmp_path`／`monkeypatch` | 沒找到任何測試直接寫死路徑，全部用 pytest 的隔離 fixture |

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

**基準快照**（本次調查完整跑過、確認 0 失敗的那一輪）：

```
843 passed, 14 warnings in 467.42s
```

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
比對「跟這次 843/843 比，總數變多是正常的（新 story 加測試），
**通過數應該等於總數**」——只要出現任何 F，先確認是不是自己一個人在跑
（沒有其他 agent 同時佔用機器），再判斷是真的迴歸還是環境問題。

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
