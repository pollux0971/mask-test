# 上機測試清單（`E01` Bring-up 與後續）

> **這份清單由調度員維護**，內容是各 story 開發過程中累積下來、
> **只有真板子才能回答**的問題。使用者決定「程式全部先寫完，最後再一次上機測試」，
> 所以這裡是那一次要跑完的全部項目。
>
> 每一項都註明：**誰在等這個答案**。沒有人在等的項目不該出現在這裡。

---

## 0. 開始之前

- [ ] 確認 `/dev/ttyUSB0` 存在、使用者在 `dialout` 群組
- [ ] `. ~/esp/esp-idf/export.sh`
- [ ] ⚠️ **`cd vl53l7cx_test`**（`CMakeLists.txt` 在這個子目錄，不在 repo
      根目錄——repo 根目錄沒有 `CMakeLists.txt`，直接在根目錄下
      `idf.py build` 會找不到專案）
- [ ] `idf.py -p /dev/ttyUSB0 build flash monitor`
- [ ] ⚠️ **開機後 log 會有一段亂碼，這是正常的，不是燒錯**——ROM 開機
      訊息用的鮑率跟 `idf.py monitor` 讀取用的 460800 對不上，改不掉
      （細節見 `reports/BOOT_OUTPUT.md`）。**看到 `$STATUS` 那一行才是
      我們自己的程式開始跑**，在那之前的亂碼直接忽略。
- [ ] **記下燒進去的 git sha**（`$STATUS` 的 `fw=` 會自報，
      `tools/fw_regression.py` 的報告會用它命名）

---

## 1. 阻擋決策的量測（最優先，會改變後續工作）

### 1.1 `A10` FFT spike → **GO / NO-GO**

**誰在等**：`A11`–`A14`（11 h 的裝置端 Mel 工作）的存廢。

照 `reports/A10_spike.md` 的照抄步驟跑。判準：

| 實測單次 FFT+bitrev 耗時 | 決策 |
|---|---|
| < 500 µs | **GO** |
| 500 µs – 2 ms | 勉強 GO，`A15` 效能回歸要盯 |
| > 2 ms 或 bin ≠ 32±1 | **NO-GO** — 改走 `B14` 主機端路線 |

⚠️ `fft_probe_run()` 沒有呼叫點，要**暫時**在 `app_main()` 加一行，測完拆掉。
**這是獨立的第二次燒錄**——跟上面「§0 開始之前」燒的是同一個 `app_main.c`，
但要先改、重編、重燒，不是燒一次就同時測完全部項目。

**精確步驟（已核對現在的原始碼行號，2026-08-26）**：
1. `vl53l7cx_test/main/vl53l7cx_test.c` 第 16 行（`#include
   "vl53l7cx_test.h"` 之後）加一行：
   ```c
   #include "fft_probe.h"
   ```
2. 同一個檔案第 317 行 `uart_out_init();` 之後加一行：
   ```c
   fft_probe_run();
   ```
   （`fft_probe_run()` 定義在 `vl53l7cx_test/main/fft_probe.c`，
   宣告在 `fft_probe.h`——兩個檔案都已存在，不用新建）
3. `cd vl53l7cx_test && idf.py -p /dev/ttyUSB0 build flash monitor`
4. 看 log 裡 `fft_probe` 這個 tag 的輸出，照下面「怎麼判讀 log」章節
   （`reports/A10_spike.md`）填表
5. **測完把上面兩行加的程式碼拿掉，再重燒一次**——這行不是要留在
   正式版裡的功能碼，只是這次量測的觸發點。

### 1.2 真實 UART 的掉幀率 → 決定 `E05` 能不能開始錄

**誰在等**：`E05`（4 小時主資料集蒐集）的資料品質。

> **更正**：`B19` 一度回報「本機 pty 只用 18% 頻寬就掉 0.7%」。
> `B20` 實測推翻了它——**bridge 沒有掉幀**。18%→92% 頻寬、四個 SSE client
> 同時連著，掉幀率一律 **0.0000%**；裸 `readline()` 在 208% 標稱頻寬下也一幀不掉。
> 那 0.7% 是兩個主機端計數 bug，已修。`bridge_server.py` 一行都沒改。

**但 `B20` 只證明了「bridge 自己不掉幀」，證明不了真實 UART 不掉**：

| 限制 | 為什麼重要 |
|---|---|
| **pty 不是 UART** | 沒有電氣層、沒有真實 baud、沒有 FIFO overrun |
| **92% 那列是高幀率逼近的** | T04 收到 `REC:` 只印 log，**錄音 dump 期間的掉幀尚未被真實模擬** |
| **`$F` 沒參與** | 頻寬靠提高 `$T`/`$M` 幀率湊；真實是「更少但更長的行」，對讀取端更輕 —— 測法偏保守 |
| **每組只跑 10 秒** | 若真實掉幀率是 0.1%，10 秒內一幀都沒掉的機率約 3%。**不是上界為 0** |

- [ ] 跑 `tools/fw_regression.py`，看 `DropTracker.cross_check()` 的 `delta`
- [ ] **`delta > 0` 是傳輸層故障**（主機看到裝置不認為自己造成的損失），不是計數誤差
- [ ] **量測窗要夠長**：30 Hz 下需要**約 4.5 分鐘**連續擷取才有統計意義
      （`B03` 算過：短窗的二項分布標準差太大，10 秒的結果沒有意義）
- [ ] **真的錄一次音**，看 dump 期間 ToF 掉幀是否可歸因於頻寬（裝置沒送）
      而非 bridge（主機沒收）
- [ ] 🔴 **`E05` 開錄前先看一次 `quality` 事件的 `alarms`，空的才開始錄**

**🔴 量到非零掉幀率時，先看 `malformed` 計數，不要直接去查線材/baud/FIFO overrun**
（見 `reports/BOOT_OUTPUT.md` §2、§6）：

`uart_out_lock()` 只保護韌體自己寫的 `$` 行函式，`ESP_LOG*` 完全不走那把鎖，
而 `mic_task`/`uart_cmd_task` 是 priority 5、`app_main`（`$T` 的來源）預設
priority 1——高優先權隨時能在一行 `$T` 的 130+ 次 `printf()` 之間插隊。**這會
被主機端 `protocol.py` 正確拒絕（已驗證，見 `host/capture/test_protocol.py`
新增的 5 個測試），但那一行的 `seq` 會在 `DropTracker` 眼中造出一個缺口
——跟裝置真的沒送出那一幀，長得一模一樣。**

而且裝置自己的 `$H` `drop_*` 計數器不會把這算進去（那一幀確實被印出來了，
韌體不知道它被自己的 log 弄髒），所以 `cross_check()` 的 `delta` 會被推成正的、
`quality` 事件會跳出一則「傳輸途中遺失（UART overrun 或 bridge 跟不上）」的
紅色警示——**這句歸因在這個成因下是誤導的**，不是計數器本身錯，是提示文字
把使用者導去查錯的方向。

⚠️ **`ProtocolParser.stats.malformed`／`malformed_rate` 已經算出來了，但目前
沒有任何地方讀它**——`vl53l7cx_test/monitor/bridge_server.py` 沒有把它接進
`quality` 事件或 panel，純粹是個內部數字。在這條路徑被接上之前，**看到非零
掉幀率但摸不清是不是這個成因時，唯一能確認的辦法是自己讀 `ProtocolParser`
的 `stats.malformed_rate`**（`bridge_server.py` 目前沒有 HTTP endpoint 暴露它，
需要另外接一個除錯用的讀取點，或直接在 Python 互動環境裡查 `protocol_state["parser"].stats`）。

**量級（讀原始碼估的，不是實測，數字有誤差）**：穩態下唯一會週期性觸發這個
race 的來源是 `bone_mic.c` 的 `a15_perf` log（每 10 秒一次，`mic_task`，
priority 5）；`vl53l7cx_test.c` 自己也有一行同名 log，但那是**同一個 task**
印 `$T` 的，同 task 內是循序執行，不會撞到自己。其餘 `ESP_LOG*`（感測器 init
失敗、i2s 錯誤、`SENS`/`AMB`/`MEL`/`REC` 指令回應）都只在開機那一刻或使用者
發指令時才印，不是穩態背景噪音。粗估：一行 `$T`（4×4）在裝完 UART 驅動
（512-byte TX ring buffer）之後，130+ 次 `printf()` 幾乎是背靠背执行完就把
資料塞進緩衝區，真正暴露在搶佔風險下的視窗遠小於這行資料實際上線傳輸所需
的時間；`a15_perf` 一次 10 秒才觸發一次，落在這個窄視窗裡的機率粗估在
**千分之幾量級**，10 分鐘的錄製大概率連一次都不會撞上，但不是零——如果
`fw_regression.py` 跑出來的掉幀率剛好是「非常小、非零」，這是第一個該懷疑
的來源，而不是先去查線材。

---

## 2. 未驗證的假設（錯了會讓分析結論反向）

### 2.1 zone 的物理佈局是不是 row-major？

**誰在等**：`D11`（逐 zone SNR）、`C05`/`C07`（熱力圖）、`D10`（crosstalk）。

ST 的 ULD 標頭**沒有文件化** zone 的物理佈局。目前全鏈路假設
`zone i → (i // 4, i % 4)`，程式與圖上都標了 `ASSUMED, unverified`。

**驗證方法**：`AMB:1` 或看 signal 熱力圖，**遮住感測器的一個角落**，
看熱力圖上哪一格變化。一次就能確認或推翻。

⚠️ **錯了的話熱力圖會上下或左右翻轉**，而 `D11` 的產出是
「感測器該往哪個方向調」——**方向錯了，建議就是反的**。

「錯了會怎樣」的完整追查（會不會報錯／哪些數字看起來對但其實錯／
一分鐘判別法）見 `reports/ASSUMPTION_RISK.md` 「假設 1」。

### 2.2 `ToF_L` / `ToF_R` 對應 `tof_A` / `tof_B` 的哪一個？

**誰在等**：`D12`（戴法 CV）。

⚠️ **更正**：這裡原本也列了 `D13`（雙矩陣互補性）與 `D19`（消融）——
追過程式碼後這是錯的，已在 `reports/ASSUMPTION_RISK.md` 「假設 2」
說明：`D13`/`D16`/`D19` 的「互補性」問的是「合併 A+B 是否比單顆好」，
對哪顆叫 A 哪顆叫 B 是對稱的，交換名字結論不變。**只有 `D12`
（`exp_d12_wear_cv.py` 的 `tof_L_distance`/`tof_R_distance` 標籤）
真的受影響**——底層 CV 數字本身仍然正確，錯的只是「這是左邊還是
右邊」這個標籤，而且沒有任何交叉檢查能自動抓到這個錯位。

目前假設 L=A、R=B，未經確認。**驗證方法**：`SENS:B=0` 之後，
看哪一顆的光點熄滅（手機相機看得到近紅外光）。

### 2.3 `$H` 之後是否真的緊跟一行 `$STATUS`？

**誰在等**：`B05` 的 PING 校時可信度標記。

`B05` 用「後面緊跟 `$STATUS` 的那個 `$H`」來分辨
「PING 回覆」與「1 Hz 週期心跳」——**兩者長得一模一樣**，
認錯會讓 RTT 量到假的最小值，污染整組校時且看起來還特別「好」。

- [ ] 跑一次 PING burst，看 `burst.confirmed` 是不是 `True`
- [ ] 若全是 `False`：不會壞掉，但可信度標記失效，要查韌體的重發順序

已追過韌體端（`uart_cmd.c` 的 `PING` 分支：`tof_print_heartbeat()`／
`tof_print_status()` 無條件依序呼叫）與主機端（`ping_sync.py` 的
`_one_ping()`：不要求 `$STATUS` 緊接下一行，100ms 視窗內容忍中間
插入 `$T`/`$M`/`$F`）兩邊程式碼——完整追查見
`reports/ASSUMPTION_RISK.md` 「假設 3」，包含為什麼實際風險比字面
描述更窄、以及數字被污染時該檢查哪個欄位。

---

## 3. 需要填數字的暫定門檻

這些數字目前是工程判斷，**沒有實測依據**。檔案裡都標了 `[暫定]`。

| 檔案 | 欄位 | 誰在等 |
|---|---|---|
| `config/session_targets.json` | `target_distance_mm` / `target_angle_deg` + 兩個容許誤差（**全部是 `null`**） | `B09` 的配戴警告、`C11` 的表單、`D12` 的資料篩選 |
| `config/quality_thresholds.json` | `noise_floor`（300/1000）、`valid_zones`（0.8/0.5）、`symmetry` | `B19` 品質儀表、`C09` 儀表板 |
| `CONTRACTS.md` §4.2 | `quality` 判定的 `valid_zone_ratio` 0.7 / 0.3 門檻 | `B11` 的 trial 品質、**`D12` 的 CV 分組會排除多少資料** |

**目標配戴幾何的量法**：打開 signal rate 熱力圖（`C07`，按 `S` 切換），
戴上裝置微調距離，看哪個距離的反射最強。那就是 `target_distance_mm`。

⚠️ 填之前 `target_check` 一律回 `"not_configured"`、`warnings` 永遠是空的——
**這是刻意的**。一個假的「距離正常」綠燈比沒有檢查更危險，
會讓人在錯誤的配戴位置錄完整批資料。

---

## 4. 效能與資源（`A15` / `tools/fw_regression.py` 一次收齊）

跑 `tools/fw_regression.py`，四種組態各一次（4×4 / 8×8 × Mel 開 / 關）：

- [ ] ToF 實測幀率：4×4 兩顆皆 **≥ 29 Hz**、8×8 **≥ 9.8 Hz**
- [ ] 幀間隔標準差 **< 3 ms**（用 `$T` 的 `t_us` 差分，不是主機收到的時間）
- [ ] `mic_task` 與 ToF task 的 stack high-water mark，**各留 > 1 KB 餘裕**
      （log 前綴 `a15_perf:`）
- [ ] heap 5 分鐘內下降 **< 2 KB**
- [ ] 總頻寬使用率 **< 70%**（`$H` 的 `bw_bytes_since_last`）
- [ ] FFT+Mel 單幀耗時（把 `bone_mic` 的 log level 調到 DEBUG 收集）

✅ **`bw_bytes_since_last` 現在是真正的總量**（`A16` 補件把錄音 dump 的
四個輸出點也接上了）。`fw_regression.py` 量的是序列埠層實收，
兩者可互相對照——**失效模式不同**：整行遺失兩者都抓得到，
但「**行被截斷**」只有 bytes 對得出來。

---

## 5. 只有戴上才能驗的

- [ ] `A16`：`AMB:1` 後遮住一顆感測器，另一顆的 ambient 讀值要有可觀察的變化
- [ ] `A08`：`SENS:B=0` 後手機相機確認 B 的光點熄滅；A 的 `seq` 不中斷；
      `SENS:B=1` 後 2 秒內恢復
- [ ] `B04`：**拍手測試 ×10**，音訊 peak 與 ToF 事件的時間差 **< 20 ms**
      （把兩邊 `t_us` 丟進 `ClockAligner.to_host_us()` 換算同軸再比）
- [ ] `B05`：20 次 PING 至少 15 次在 10 ms 內（`burst.meets_acceptance`）
      ⚠️ **不要用 mock 驗這條**——mock 主迴圈每輪睡 20 ms，
      量到的是它的排程粒度不是韌體反應速度（`B05` 實測 fast 恆為 7/20，
      與 fps/dim 完全無關）
- [ ] `B05`：跑 ≥ 10 分鐘的 session，`clock_drift_ppm` 落在 ±200 ppm，
      且與 `B04` 的 `clock_slope` **對得上**（兩點法 vs 回歸法互為獨立檢查，
      對不上代表其中一邊有問題）
- [ ] `C10`：圓唇／展唇／靜止三態在 PCA 2D 上肉眼可分離
      ⚠️ 要等**固定基底**模型，即時擬合的 stub 座標軸會漂移，驗不了這條
- [ ] `C25`：當前模式的視覺對比在 **Demo 投影距離**下夠不夠明顯

---

## 6. 錄製時要記得的事

### 🔴 6.0 錄完第一筆，先跑健檢，不要錄到第 400 筆才發現不能用

```bash
python3 ssi-backlog/tools/first_session_check.py sessions/<第一筆檔案>.h5
```

一行結論告訴你這個 session 能不能繼續錄——**結構壞掉**（baseline 被蓋掉、
時間戳往回跳）會明確 STOP；無效 zone 比例、麥克風底噪、掉幀數這類**數字
參考**不會擋你，因為那些門檻是照合成資料訂的，跟預期不同可能只是門檻沒
校準過，不代表裝置壞了。兩者**不要混在一起判斷**（見
`reports/FIRST_REAL_DATA.md`）。

### ⚠️ 錄製途中 Ctrl-C 看起來沒反應是已知現象，不要因此拔電源

實測 6 次按 Ctrl-C，**5 次程式完全沒有停下來、繼續照常錄，看起來像當機**
——這是 Python 內部一個跟這個專案無關的機制（h5py 的 weakref 清理
callback 剛好吞掉了中斷訊號），不是資料壞掉，程式其實還在正常寫入。

- ⚠️ **不要因為 Ctrl-C 沒反應就緊張去拔電源**，也不要因此以為程式當機了
- ✅ **`kill -9`／`kill -15`（含 USB 鬆脫、筆電斷電、關掉終端機）都是安全
  的**——已經實測超過 15 次，只有正在寫的那一筆會乾淨地整筆消失
  （不是殘缺），前面已經寫完的資料全部完好
- 中途要停就直接連按 Ctrl-C 兩三次，或乾脆 `kill -15`／關掉終端機

完整推導見 `reports/SESSION_DURABILITY.md`。

### 🔴 6.1 每個詞要錄幾次？**不能沿用低維度測試調出來的數字**

`D09` 在真實系統規模（**104 維 × T=24**）上驗證拒識時，
用 `D06`/`D07` 驗證過的樣板數比例（20:20）測出 **100% 誤拒率**。

> ⚠️ **更正（第二次）**：一度以為「錄 50+ 次就好」。
> `D09` 的後續掃描（3 組獨立幾何取平均）推翻了它：
> **誤拒率幾乎不隨樣板數改變。**

| n（每類樣板數） | ToF 誤拒率 | Mel 誤拒率 |
|---|---|---|
| 10 | 32% | 53% |
| 20 | 34% | 52% |
| 30 | 33% | 47% |
| 50 | 35% | 42% |
| 100 | 35% | 44% |

**從 10 錄到 100（十倍），ToF 完全沒有下降趨勢。**
`percentile` 從 80 掃到 99 也只有個位數百分比的改善。

### ✅ 已解決：`D22` 的雙邊 ROC 校準

**根因**：舊方法（`D06`）只用 `_reject` 類別自己的 LOO 距離分布校準門檻，
而「LOO 最近距離」與「真詞查詢到自己樣板的最近距離」**在統計上是同一種量**——
樣板數增加時兩者一起縮小，門檻不會相對變寬。

**`D22` 改成同時用兩邊的分布產出 ROC**，讓門檻成為明示的取捨：

| 每類樣板數 | 舊 | **新（已設為預設）** |
|---|---|---|
| 10 | ToF 32% / Mel 52% | 3.3% / 0.6% |
| 30 | 32% / 44% | **0% / 0%** |
| 100 | 37% / 45% | **0% / 0%** |

樣板數不平衡時（word:reject 從 1:0.3 到 1:3）新方法全程 < 1.1%、舊方法 30–54%。

⚠️ **這是合成資料上的結果**，真實資料待 `E05`。舊方法保留為對照。

### 錄製次數的暫定結論

- [ ] **樣板數不是瓶頸**，但仍建議每類 **20–30 次**
      （足夠讓 LOOCV 有統計意義，再多是浪費你的時間）
- [ ] 「詞」與「靜止（`_reject`）」的次數**不必刻意對齊**——`D06` 曾用
      舊校準方法（單邊 LOO）量到不對齊時誤拒率從 7.33% 升到 14.67%，那組
      數字是真的量過的，但只存在於當時的對話回報裡，從未寫進任何檔案
      （見 `HANDOFF.md` §4.4）。`D22` 換成雙邊 ROC 之後，就是上面那張
      「樣板數不平衡時的行為」表——同樣的比例範圍（1:0.3–1:3）新方法
      全程 < 1.1%，這條「要對齊」的建議已經過時：錄成大致 1:1 即可，
      不用刻意配平
- [ ] **錄完立刻跑一次 `D08` 的 LOOCV**，看 `D22` 的新校準在真實資料上的表現
- [ ] ⚠️ 若真實資料的誤拒率仍然很高，**先看是不是校準方法而不是資料**——
      上面那張表就是在合成資料上把這個可能性排除掉的方法
- [ ] **`E04` 的 20 次戴脫**：每次都要真的脫下來再戴回去，
      不是原地調整。`D12` 量的就是這個差異。
- [ ] ✅ **錄完之後，把 session 變成 `/recognize` 吃得下的樣板**：
      ```bash
      python3 -m analysis.similarity.build_templates_from_session \
          --session sessions/<檔案1>.h5 --session sessions/<檔案2>.h5 \
          --out templates/<subject>_<wear_id>.npz \
          --subject <subject> --wear-id <wear_id>
      ```
      （可以給多個 `--session`，同一次戴上分好幾次錄都算）。**這個工具
      刻意跟 `POST /recognize` 走同一條時間對齊路徑**（`Aligner` + `t_us`
      真對齊，不是幀數截斷），理由跟下面「⚠️ 已知風險」一致。存完
      `bridge_server.py` 下次收到請求時會自動讀到，不用重啟。
      ⚠️ **已知風險（還在量，`8f` 正在處理）**：這個工具跟
      `analysis/run_all.py`（離線分析報告用的那條路徑）對「ToF 幀數 ≠
      Mel 幀數」的處理方式不一樣——`run_all.py` 用幀數截斷，這裡用
      `t_us` 真對齊。兩者算出來的特徵理論上可能有小落差，目前還沒有
      真實資料上的量測結果，先知道這件事，不代表現在就有問題。

---

## 6.2 前端檢查

**步驟見 `reports/PANEL_INTEGRATION.md` 第 7 節。**

> ⚠️ 這裡**刻意不複製**那份步驟——這個專案已經三次因為「第二份事實來源
> 腐爛」而出事（`schema_example.py` 漏 `mel_t_us`、`REQUIRED_META_KEYS`
> 沒跟上校時欄位、mock 的 v1 方言與真實韌體不符）。**指過去，不要抄過來。**

⚠️ **兩個會在錄製過程中自然遇到、不代表出事的畫面**（實際讀程式碼確認過）：

- **`monitor` 模式斷線時，畫面會整片變暗（不透明度降到 0.35）並跳出紅色
  橫幅**——這是刻意的降級提示（`monitor.js` 的 `.stale-data` 機制），
  代表「收到的資料不新鮮了」，不是程式當機，重新連線後會自動恢復正常。
- **`record` 模式的 baseline 超過 10 分鐘會顯示一則提示 + 「重新擷取
  baseline」按鈕**——錄 4 小時的主資料集一定會遇到這個，看到就照著按
  就好，不是錯誤。（這則提示目前沒有額外的顏色樣式，純文字，不要因為
  沒有特別醒目的顏色就以為沒看到）

已知的兩項待確認：

- [ ] **`monitor` 模式的 CPU**（`reports/C_monitor_perf.md` 有完整調查）
      
      | 指標 | 修復前 | 修復後 |
      |---|---|---|
      | `TaskDuration`（主執行緒忙碌） | 55.9% | **21.7%** |
      | `LayoutDuration`（同步 reflow） | 28.8% | **7.6%** |
      | `LayoutCount` | 916 次/8s | 517 次/8s |
      
      **根因不是猜測的那個**：不是熱力圖的 DOM style 寫入貴
      （`RecalcStyleDuration` 只佔 0.8%），是 `drawTrail()` 在寫完樣式後
      **立刻讀 `canvas.clientWidth`**，逼瀏覽器跑一次同步 reflow
      才能回答——經典的 layout thrashing。修法是快取 CSS 尺寸。
      
      ⚠️ **`/proc` 的總 CPU 幾乎沒變**（54.4% → 51.6%），
      因為 headless `--disable-gpu` 用軟體合成，那部分成本跟這次修復無關。
      → **在 Demo 用的那台筆電上重量一次**（有 GPU、沒有別的 agent 搶 CPU）
      才是乾淨的數字。若 `TaskDuration` 仍明顯超過 15%，
      Demo 期間建議停在測驗模式（11–13%，而且 Demo 四步全在那裡）。
- [ ] ✅ **更正（第二次）：`validate` 模式現在整條路徑都是通的**——
      `C22` 完成（`validate.js` 486 行、已接進 `main.js`），**後端
      `POST /verify/run`／`GET /verify/state` 也已經接上**
      （`bridge_server.py` 現在有這兩條路由）。**這次不只讀程式碼，
      實際用瀏覽器（headless Chrome + Playwright）點過一次**：造一份
      合成 session（`ssi-backlog/tools/schema_example.py`），在
      `validate` 模式的輸入框填路徑、按「執行」，畫面正確跳出
      「上次執行：…（耗時 0.5 秒）⚠ 合成資料」，不是「尚未串接」。
      ⚠️ **`validate.js:8-13` 開頭的註解仍寫著「confirmed live, both
      currently 404」——那段註解本身過時了，已回報給調度員轉知
      `esp-mask-test-ed`，不影響這條路徑實際能不能用，只是文件說法
      要更新。**

---

## 7. 測完之後要回填的檔案

| 檔案 | 填什麼 |
|---|---|
| `reports/A10_spike.md` | GO / NO-GO 結論 + 原始 log |
| `reports/A04_polling.md` | 實測幀率與幀間隔標準差 |
| `reports/A15_perf.md` | 四種組態的完整對照表 |
| `reports/B20_bridge_throughput.md` | 掉幀率 vs 頻寬曲線 |
| `config/session_targets.json` | 四個 `null` |
| `config/quality_thresholds.json` | 三個 `[暫定]` |
