# CONTRACTS — 跨軌介面契約

> **狀態：第 1／2／3／5 章已凍結（2026-08-26，由 `T01` `T02` `T03` `T06` 完成）。
> 第 4 章 HTTP／SSE 介面待 `B09` `B18` `B19` `D09` 凍結。**
>
> 這份文件是平行開發的單一事實來源。A/B/C/D 四條軌道都只對著它開發，
> 不需要互相詢問。**任何改動都必須通知全體軌道。**

| 章節 | 負責 story | 狀態 |
|---|---|---|
| 1. 序列埠協定 v2 | `T01` | ✅ 已凍結 |
| 2. HDF5 Session Schema | `T02` | ✅ 已凍結 |
| 3. 特徵向量規格 | `T03` | ✅ 已凍結 |
| 4. HTTP / SSE 介面 | `B09`, `B18`, `B19`, `D09` | ⬜ 待凍結 |
| 5. 檔案所有權 | `T06` | ✅ 已凍結 |

## 變更紀錄

| 日期 | 章節 | 變更 | 影響的 story | 已通知 |
|---|---|---|---|---|
| 2026-08-26 | 1. 序列埠協定 v2 | `A15`：`$H` 新增 `bw_bytes_since_last:u32`（第 7 個資料欄），韌體端在 `uart_out.c` 累計、`tof_print_heartbeat()` 每次回報自上次 `$H` 以來送出的位元組數。**⚠️ 破壞性變更且尚未修好**：`host/capture/protocol.py` 的 `_parse_heartbeat()` 目前硬性要求 `len(parts) == 7`（對應舊格式），本變更讓每行 `$H` 變成 8 段，在該函式更新（放寬成 `>= 7` 或改成 key=value）之前，**每一行 `$H` 都無法解析**，不只是新欄位讀不到。`B03`/`dropwatch.py` 的掉幀判定不受影響（純靠 `seq` 缺口，不吃 `$H`），但 heap／溫度／新頻寬欄位在此之前對主機端全部不可見 | `A15`, `B01`, `B03`, `C04` | ⬜ 待通知（見完成回報） |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.2 明訂 SSE 的 `mel.bands` 是**已解碼的浮點 log10 值**（約 -10~0），**不是**線上 `$F` 的 `int16 = round(log_mel×100)`（約 -1000~0）——parser 在轉發前就除回浮點了。`C08` 照 §3.1 的線上格式寫色階，結果整張瀑布圖純黃一片、看不出頻譜結構，**而程式邏輯完全正確，只有真的打開瀏覽器看才會發現**。通則：§1 是線上格式、§4.2 是解碼後格式，消費端一律以 §4.2 為準 | `B01`, `B19`, `C08`, `C19` | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.2 `status` 事件新增 **`source`**（`live`/`mock`/`replay-log`/`replay-session`），由 bridge 的 CLI 旗標明示，**不可推測**（pty 與真實 UART 分辨不出）。原因：`E05` 要錄 4 小時，對著 mock 錄會產出「裝滿合成資料卻標記成真實量測」的 HDF5，而 `D13`/`D16`/`D17`/`D19` 會拿它跑出漂亮的結論且**沒有任何一層會發現**。`source`（連線層級）與 `replay: true`（單一事件層級）是不同層，不可互相取代 | `T04`, `T05`, `B17`, `B19`, `C04`, `E05` | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.2 更正 `CONFIRM` 的端點路徑為 `POST /trial/confirm` 與 `/trial/discard`（原寫成 `/trial/confirm/keep`、`/trial/confirm/discard`，與實作不符）。理由：所有 trial action 是同一層的兄弟，對應狀態機方法名；把 `discard` 放進 `confirm/` 底下語意不通。另新增 `POST /trial/reject {"trial_idx":N}`（`C14`：棄用**已存檔**的 trial，與 `abort`/`discard` 不同層——棄用不是刪除，資料留著只改 `quality`，`D12` 需要知道錄壞了幾次） | `B11`, `B19`, `C12`, `C14`, `D12` | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.2 新增 `POST /trial/confirm/keep` 與 `/trial/confirm/discard`（`CONFIRM` 狀態原本只定義了語意沒有端點，`C12` 實作時提案）。前端鍵盤慣例 Enter=keep／ESC=discard——ESC 在整個錄製畫面上一律代表「不要、丟掉」，不另外發明規則 | `B12`, `B19`, `C12`, `C14` | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.2 `trial` 事件新增 `next_label`（只在 `IDLE`/`REST`/`SAVE` 出現）。原因：Hold-to-Record 在使用者按下之前，前端完全不知道下一個要念哪個詞，只能盲按（`C12` 實作時發現）。詞指標改在 `_do_save()` 之前前進，`SAVE`/`REST` 才能 peek 到「下一個」。`abort` 讓它前進、`redo` 不會。另 `TrialStateMachine` 新增 `first_trial_idx` 參數，避免與 baseline 的 `trial_000` 撞號 | `B11`, `B12`, `B19`, `C12`, `C13` | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.2 定義 `session` 事件在 `state:"baseline"` 時的 `progress` 形狀（`elapsed_s`/`remaining_s`/`duration_s`/`live_sigma_A|B`，完成時帶 `outcome`），並新增 `POST /session/baseline/retry`。原本 `progress` 只是佔位符沒有形狀，`C11` 實作時提案。前端須用 `elapsed_s` 重新對時；`live_sigma_*` 為 null 時必須明示「倒數是本地估計」不可假裝正常 | `B10`, `B19`, `C11`, `C12` | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.2 `trial` 事件補上 `CONFIRM` 狀態（`B12` Hold-to-Record 專用：按住時間超出 0.3–5 s 範圍時，資料算好但**不落盤**，等使用者決定保留或跳過）。與 `B11` 「放棄的 trial 完全不落盤」一致 | `B12`, `B19`, `C12`, `C14` | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.2 `trial` 事件補上 `IDLE` 狀態與 `seed` 欄位；明訂 `abort`（跳過此詞）與 `redo`（保留同詞重試）語意不同，兩者都不寫入 HDF5 與 manifest；明訂 `quality` 值域凍結為 `{ok, low, rejected}`（棄用＝`rejected`，不新增第四個值）與 `B11` 的暫定門檻 0.7／0.3，並標註該門檻無實測依據、待 `E01`/`E03` 校準 | `B11`, `B12`, `B19`, `C12`, `C14`, `D12` | 是 |
| 2026-08-26 | 6. 詞彙集 | 明訂實驗 A（逐 zone SNR）的對照詞為 `五`（round）vs `一`（spread）。`D11` 的公式用了 round／spread 但契約沒規定是哪兩個詞，`D15` 的 `run_all` 只能自己對應 | `D11`, `D15`, `E03` | 是 |
| 2026-08-26 | 2. HDF5 Session Schema | `/meta` 新增 `sensors_enabled`（`"AB"`/`"A"`/`"B"`）。原因：`D10`（C₀ 串擾）要比較單顆開 vs 兩顆開，但 schema 沒記錄擷取當下的感測器開關狀態——`D15` 的 `run_all` 因此**永遠無法自動配對 solo/dual**，`C0` 恆為 SKIPPED。值來自 `SENS:` 指令，但要照實記錄「那是主機端記的指令、非裝置確認狀態」的限制（§4.1.2） | `B07`, `B18`, `D10`, `D15`, `E02` | 是 |
| 2026-08-26 | 6. 詞彙集 | 裁決：**接受 viseme E（舌音）為空列，不加詞**。`D14` 的預期表有 E 但 `vocab.json` 沒有任何舌音詞、且有預期表沒列的 G 應用（三個詞）。加詞會連鎖影響 `C15`–`C21`、`D22` 的樣板數、`E05` 的錄音量，收益只是驗證一個預期本來就是「三模態都弱」的類別。「本系統不涵蓋舌音」本身是可寫進論文的誠實限制。G 標 `no_expectation` 不硬套猜的預期 | `D14`, `D15`, `E05` | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.3 更正融合拒識公式：必須用**原始（未正規化）距離** `d_tof_raw`/`d_mel_raw`，並把這兩個列為 JSON 的必要欄位。原因：`normalize_distances()` 強制減去最小值，`d_tof.min()` 恆為 0，任何正門檻都不會被超過 → **`reject_fused` 永遠回 False 且無任何錯誤訊息**，只會表現成「這個系統從不拒識」。用原始距離也正是兩端退化性質成立的原因（`D07` 實作時發現） | `D07`, `D09`, `C16`, `C17`, `C18` | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.3 定義**融合軌的拒識**：`theta_reject_fused(w) = w·theta_tof + (1-w)·theta_mel`，由前端即時算不存進 `TriResult`（它隨 `w` 改變，存下來會跟滑桿不同步）。線性內插的理由：融合距離本身就是同一組權重的線性組合，且在兩端恰好退化成單模態門檻——**拖滑桿到底時不會出現行為跳變**，那正是 Demo 第 2 步要做的事。另明訂「拒識不是分歧是沉默」：判斷三軌一致性時被拒識的那軌不列入比較（`C16` 實作時發現缺口） | `D06`, `D07`, `D09`, `C16`, `C17`, `C18` | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.3 拒識門檻校準的**預設方法改為 `D22` 的雙邊 ROC**。誤拒率（真實規模 104 維 T=24）：n=30 時 ToF 由 **32% 降到 0%**、Mel 由 44% 降到 0%；樣板數不平衡時新方法全程 < 1.1%、舊方法 30–54%。舊方法保留為對照不刪除。代價：校準耗時約 17 倍（隨樣板數平方成長），n≤100 時仍遠低於 `E06` 的 30 秒預算 | `D06`, `D07`, `D08`, `D09`, `D22`, `C17`, `C18`, `E06` | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | §4.3 `TriResult` 的 `theta_reject` 拆成 `theta_reject_tof` / `theta_reject_mel` 兩個獨立欄位。原因：兩模態的原始距離尺度與 `_reject` 樣板距離分布都不同，共用一個閾值必有一邊校準不準，而且失準會安靜地表現為「全部拒識」或「完全不拒識」（`D07` 實作時發現） | `D06`, `D07`, `D09`, `C17`, `C18` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | §1.1.2 `$STATUS` 補 `amb=<0|1>` 欄位，與既有的 `mel=` 對稱。原因：`A16` 加了 `AMB:<0|1>` 開關，但主機端沒有任何方式查詢它目前的狀態——`B18` 的控制端點與 `C04` 狀態列都需要。缺欄位時主機端一律回 `None`（§1.1.2） | `A16`, `B01`, `B18`, `C04` | 是 |
| 2026-08-26 | 1 + 2 | 新增 §1.1.3 `$A` ambient 幀（第五條獨立串流）與 §1.2 的 `AMB:<0|1>` 指令，HDF5 對應 `tof_ambient_A/B (Ta,16)` + `tof_ambient_t_us (Ta,)`。原因：`D10` 明訂 `ambient_per_spad` 是 crosstalk 最靈敏的指標，但該欄位從韌體到 schema **整條管線都不存在**。設計成獨立行 + 預設關閉 + 1 Hz，而非塞進 `$T`：ambient 變化慢，塞進 `$T` 每幀會讓 8×8 多約 190 bytes/行，而 §1.4 顯示 8×8 開 Mel 已達 70%。韌體實作見新增的 `A16` | `A16`, `B01`, `B07`, `D10`, `E02` | 是 |
| 2026-08-26 | 3. 特徵向量規格 | §3.2 重整為 §3.2.1／3.2.2／3.2.3：σ 下限一律 `1/√12`（該通道的傳輸單位）不是 `1e-3`；**每一個**除以 σ 的地方都要有守衛，`D11.md` 原文的 SNR 分母是裸的。後果鏈：剛性表面 zone → σ≈0 → SNR=inf → 活躍 zone 排名第一 → 主宰特徵向量 → 汙染 DTW → `D05`/`D06`/`D07` 全部失真，**全程無錯誤訊息**。註：`exp_a_snr.py` 已有守衛但常數是 `1e-3`，`max(0.026, 0.001)=0.026` 等於沒作用 | `D01`, `D05`, `D06`, `D07`, `D11`, `D13`, `D19`, `D21`, `D22` | 是 |
| 2026-08-26 | 3. 特徵向量規格 | §3.2 加註：`sigma` 下限 `1e-3` 只適用於**已正規化的特徵**；直接對原始整數 mm 距離 z-score 時下限應為量化尺度 `1/√12 ≈ 0.289 mm`。原因：`$T` 距離是整數 mm，靜止時回波穩定的 zone 幾乎每幀同一個整數 → σ→0 → z 爆到 10⁵ 量級，單一 zone 主宰整條訊號且**無任何錯誤訊息**（`B16` 實測 9.4×10⁵）。同理 MAD 在高度量化資料上會剛好回 0 | `B15`, `B16`, `D01`, `D11` | 是 |
| 2026-08-26 | 2. HDF5 Session Schema | 明訂 `vad_start_us`/`vad_end_us`/`lip_onset_us`/`voice_onset_us` 偵測不到時填 **`None`**，不可填 0 或 capture 視窗邊界——後者會讓「完全沒偵測到」看起來像「整段都在動」，下游不報錯只會安靜算錯 | `B07`, `B11`, `B15`, `B16`, `D14` | 是 |
| 2026-08-26 | 2. HDF5 Session Schema | `/trial_NNN` attrs 新增 `speaking_mode`（`normal`/`whisper`/`silent`）與 `vad_confidence`。原因：`B15` 實作時發現契約的 `mode` 是 session／面板模式（`quiz` 之類），**完全沒有定義說話模式**，而 `D13`/`D17` 的「三種 mode 各跑一組」分析依賴它。共用一個欄位會讓 `"quiz"` 與 `"whisper"` 混在一起 | `B07`, `B11`, `B15`, `B16`, `D13`, `D17` | 是 |
| 2026-08-26 | 2. HDF5 Session Schema | `mel` 的時間軸由 `(M, 40)` 改為 **`(F, 40)`**，並新增成對的 `mel_t_us (F,) int64`。原因：`A14` 之後 `$F` 62.5 Hz、`$M` 31.25 Hz，兩者幀數不相等（§1.1.1），原 schema 是 §1.1.1 凍結**之前**的假設。`B11` 實作時撞到 `session_writer.py` 的「mel 幀數必須等於 mic_t_us 長度」檢查才發現。**寫入端必須移除該檢查**，且不可用內插把 mic 湊到 mel 網格（那是捏造未量測的數值） | `B07`, `B11`, `B17`, `D02`, `D03` | 是 |
| 2026-08-26 | 2. HDF5 Session Schema | `/meta` 新增時鐘漂移欄位：`clock_drift_us` / `clock_drift_ppm` / `clock_sync_span_us` / `clock_sync_confirmed`，以及 session 首尾各三個校時欄位（`device_us` / `host_us` / `rtt_min_us`，重算漂移所需的最小集合）。原因：`B05` 的驗收要求「漂移量寫進 metadata」，但 §2 凍結時沒有任何漂移欄位。註：兩點法漂移（`B05`）與回歸法 slope（`B04`）互為獨立檢查，`B07` 寫入時應比對，差太多要標 quality | `B04`, `B05`, `B07`, `D12` | 是 |
| 2026-08-26 | 2. HDF5 Session Schema | §2.2 補充：`n_frames` 明訂為 **ToF 幀數**（`tof_A.shape[0]`，即 `T`），因為 ToF 與麥克風長度不同而原欄位名沒指明（`B08` 實作時才發現）；`session_path` 明訂為「相對於某個 `root`」，且增量與重建必須用同一個 `root` | `B08`, `B19`, `D12` | 是 |
| 2026-08-26 | 2. HDF5 Session Schema | 明訂無效 zone 在 `tof_A`/`tof_B` 數值陣列填 **`NaN`**（不是 `-1` 也不是 `0`）。原 §2 只說「不要塞 -1」，沒講具體填什麼，`B07` 實作時必須自己決定。選 `NaN` 的理由：算術會正確傳染錯誤逼讀取端注意到，而 `-1`/`0` 會被當成合理的近距離值悄悄算進統計 | `B07`, `B17`, `D01`, `D10`, `D13` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | `$H` 尾端新增 `bw_bytes_since_last:u32`（自上次 `$H` 以來送出的 bytes，供 `A14` 的「總頻寬 < 70%」驗收與 `C04` 狀態列使用）。**同時新增 §1.1「前向相容規定」**：所有 `$` 資料行的解析一律用「至少 N 段」而非 `len(parts) != N`，多餘尾端欄位忽略不判畸形。起因：`host/capture/protocol.py:259` 的 `_parse_heartbeat()` 寫死 `!= 7`，韌體加一欄後**整條 `$H` 事件消失**（連 `heap`/`drop_*` 一起丟掉）且無任何錯誤訊息 | `A06`, `A15`, `B01`, `B19`, `C04` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | §1.1.2 補充解析規定：選用欄位缺漏時主機端一律回 `None` **不可填預設值**（「沒送」與「送了是 512」是兩件事，填預設會讓舊韌體看起來像新韌體，而 §1.1.2 的唯一目的就是分辨版本）；選用欄位格式壞掉只讓該欄位為 `None`，**不可讓整行 `$STATUS` 解析失敗**（那行扛著版本協商）；「未知欄位忽略」指不因此報錯而非丟棄，建議保留原文 dict 供日後新欄位的下游使用 | `B01`, `B02`, `B19`, `C04` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | 新增 §1.3.1：`A14` 之後 `$M` 必須用與 `$F` 相同的 512 樣本窗算 RMS，取得連續不重疊的完整涵蓋，**不可只對新讀進的 256 個樣本算**。原因：後者會讓一半音訊不進入任何 RMS 幀，短促爆音對 `B15` 的 VAD 完全隱形且不報錯 | `A03`, `A14`, `B15`, `B16` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | 新增 §1.1.2：`$STATUS` 增加 `sr` / `mel` / `mel_win` / `mel_hop` / `mic_hop` 五個自我描述欄位。原因：`A14` 之後同一個 `$F` 行格式代表不同東西（31.25 vs 62.5 Hz），主機端無從得知接的是哪一版。**解析一律 key=value、順序無關、未知欄位忽略**，不可用固定位置切分 | `A07`, `A13`, `B01`, `B02`, `C04` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | 新增 §1.1.1：明文規定 `$T`(A)/`$T`(B)/`$M`/`$F` 為四條獨立串流，各自維護 `seq`（= 已送出行數），**跨模態對齊一律靠 `t_us` 不靠 `seq`**。原因：`A14` 讓 `$F` 62.5 Hz、`$M` 31.25 Hz，一一對應在物理上不成立。這是 `A12` 與 `A14` 兩份 story 之間的矛盾，非實作偏差。`B03` 從設計之初就是四條串流各自追蹤 `last_seq`，此變更讓文件回歸 `B03` 的既有預期，不要求 `B03` 改動 | `A12`, `A14`, `B01`, `B03`, `B06` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | T04：新增 `ssi-backlog/tools/mock_device.py`（pty 假裝置，產生 `$T`/`$M`/`$H`/`$STATUS`）。預設 `--proto v2` 照本章格式；`--proto v1` 保留凍結前舊格式，讓 mock 現在就能對接 unmodified `bridge_server.py`，也作為 `B02` 雙協定相容測試的夾具 | `B01`, `B02`, `C01`, `C05` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | 調度決議：`$H` 的 `drop_A`/`drop_B`/`drop_M` 改為**自開機起算**，`$STATUS` **不再重置**掉幀計數。原因：§1.1 同時規定「每次收到 `PING` 都要重發 `$STATUS`」與「`drop_*` 自上次 `$STATUS` 起算」，兩者相乘的後果是 `B05` 的「連續 100 次 PING」校時流程會把計數器清 100 次 —— 健康指標在最需要它的時候恆為 0，`B03` 也無法拿主機端 `seq` 缺口與韌體端計數交叉驗證。u32 累積計數本來就不需要中途歸零，改為與 `seq` 一致的 session 語意 | `A05`, `A06`, `A09`, `B03`, `B05` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | 調度決議：`REC:<seconds>` 上限由草案的 60 s 改為 **30 s**，與韌體 `uart_cmd.c` 既有實作一致。理由：dump 期間佔 92% 頻寬（§1.4），60 s 錄音需約 56 s 傳輸、ToF 全程掉幀，而所有 trial 都是單詞級發音，30 s 遠超所需。改契約而非改韌體，避免為沒有需求的能力付出可靠度代價 | `A08`, `B01`, `B11`, `C11` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | 調度決議：`A01`、`A02` 合併為一個實作單元。原因：§1 凍結後 `$T` 行的 `d0..dN,s0..sN` 是同一組欄位契約，且「無效值語意」要求距離與 signal 在無效 zone 必須同時回 `-1`（不能只有其中一個），中間切開會產生違反此規則、也沒有消費者能正確解析的過渡格式。實作已在 A01 完成，A02 內容併入 | `A01`, `A02`, `B01` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | 凍結協定 v2：補齊每種 `$` 行的欄位型別/單位/範圍表、7 行真實範例（含極端值）、`$STATUS` 版本協商流程、`-1` 無效值的適用情境。**破壞性變更**：`$M` 的 `rms` 從草案佔位 `f1`（浮點文字）改為 `i16` 定點整數（16-bit PCM 原始振幅），以符合「浮點格式一律定點整數」的既有決定 | `A01`, `A03`, `B01`, `T04` | ⬜ 待通知（見完成回報） |
| 2026-08-26 | 5. 檔案所有權 | 建立目錄骨架並凍結；依 T03 決議新增 `ssi-backlog/tools/reference_mel.py`（T 軌獨佔，A/B/D 唯讀引用） | A, B, D, T03 | 是 |
| 2026-08-26 | 2. HDF5 Session Schema | 凍結 schema；新增 `ssi-backlog/tools/schema_example.py` 產生結構正確的空 HDF5 檔供 D 軌開發 | B, D, B07, B17, D01, D10 | 是 |
| 2026-08-26 | 3. 特徵向量規格 | 凍結規格（3.1–3.4）；`reference_mel.py` 路徑由草案的 `analysis/` 移至 `ssi-backlog/tools/`（調度決議，理由：跨軌唯讀契約產物，不是 D 軌分析程式碼）；新增可執行的 `ssi-backlog/tools/reference_mel.py` | A, B, D, T03, T06 | 是 |
| 2026-08-26 | 3. 特徵向量規格 | B14 補上 3.1 遺漏的 `STFT center=False` 決定（T03 當時已在 `reference_mel.py` 實作但正文沒寫進去）；並新增 `tools/compare_mel.py`（B14 產出，B 軌維護，`tools/OWNER.md` 已加註） | A11, A12, D, T03 | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | B09：補上 §4.1.1 `session/start`\|`end`\|`current`\|`prefill` 的請求/回應形狀；新增 `GET /session/prefill`（原表沒有預填的端點）。目標配戴幾何（`target_distance_mm`/`target_angle_deg`）在 `config/session_targets.json`（新增，`E01` 量測前一律 `null`）未設定時，`target_check` 回 `"not_configured"` 且 `warnings` 為空——不捏造沒有依據的偏離警告 | B18, B19, C11, D09, D12, E01 | 是 |
| 2026-08-26 | 2. HDF5 Session Schema | `host/storage/session_writer.py` 同步到最新 schema（B05/B11 追加的欄位落地）：①`REQUIRED_META_KEYS` 補時鐘漂移七欄位，`session_end_*` 三欄位改由新方法 `SessionWriter.finalize_session_end()` 於 session 結束時補寫，不列入建構必填；②`write_trial()` 移除「`mel` 幀數必須等於 `mic_t_us` 長度」的錯誤檢查，改收 `mel_t_us` 參數、與 `mel` 成對驗證；③新增 `tof_ambient_A`/`tof_ambient_B`/`tof_ambient_t_us` 三選填參數，全有或全無，各自時間軸，無效填 NaN；④新增 `/meta` 的 `clock_cross_check_ppm_diff`/`clock_cross_check_ok`（本輪新增的欄位名，見上方 §2 說明），實作 B05 交叉檢查建議，門檻沿用 `host/clock/align.py` 的 `SLOPE_TOLERANCE_PPM`（±200ppm） | B05, B07, B11, B19, D, T02 | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | B18：補上 §4.1.2 裝置控制端點形狀（`SENS`/`MEL`/`AMB` 執行期指令 vs `/switch` 重燒狀態機的區分、`flashing` 中三者一致回 409、`/device/state` 回應形狀）；端點表新增 `POST /ambient?on=0`（`A16` 對應，原表沒有）。明訂 `sensor_a_enabled`/`sensor_b_enabled` 是主機端記錄的上次指令而非裝置確認狀態（`$STATUS` 沒有 `sens_a=`/`sens_b=` 自我描述欄位），`sensor_state_confirmed` 欄位標出這個落差 | B19, C04, C22 | 是 |
| 2026-08-26 | 4. HTTP / SSE 介面 | D09：§4.3 的 JSON 範例補上前一輪（D07）已在文字說明但沒改到範例本身的 `theta_reject_tof`/`theta_reject_mel`（範例仍寫舊的單一 `theta_reject`，跟上面的文字說明不一致）；新增 `dist_method` 欄位（記錄該次辨識實際用的距離函式，`D09` 預設 `"cosine"`——批次餘弦 0.147ms vs DTW 8-12ms，且 `D05` 合成資料 LOOCV 顯示 DTW 較差，`E05` 後應複驗）；`latency_ms` 的鍵從寫死的 `"dtw"` 改成 `"dist"`（距離函式可換，鍵名不該綁死一種） | D07, D09, C16, C17, C18 | 是 |

---

## 1. 序列埠協定 v2

**負責：`T01`** ｜ 凍結日期：`2026-08-26`

> **FROZEN 2026-08-26**：本章節（1.1–1.4）已凍結。後續修改需在文件頂端
> 「變更紀錄」加一行並通知 A/B/C/D 全體軌道，不可直接改字。
>
> `ssi-backlog/tools/mock_device.py --proto v1` 保留本章凍結前的舊格式
> （`$TOF`/`$MIC`/`$STATUS,res=`），供 `B02` 雙協定相容測試使用；預設
> `--proto v2` 才是本章的協定。

### 1.1 裝置 → 主機

```
$T,<A|B>,<seq:u32>,<t_us:i64>,<dim>,<d0>..<dN>,<s0>..<sN>
$M,<seq:u32>,<t_us:i64>,<rms:i16>,<peak:i16>
$F,<seq:u32>,<t_us:i64>,<m0>..<m39>
$H,<t_us:i64>,<drop_A:u32>,<drop_B:u32>,<drop_M:u32>,<heap:u32>,<temp_c:i8>,<bw_bytes_since_last:u32>
$A,<A|B>,<seq:u32>,<t_us:i64>,<dim>,<a0>..<aN>
$STATUS,res=<dim>,proto=2,fw=<git_sha>
$REC,start,<seconds>
BEGIN_WAV_B64 rate=.. bits=.. channels=.. bytes=..
END_WAV_B64
```

**版本協商**：主機連上序列埠後，讀取第一行有效的 `$STATUS`，比對
`proto=` 是否等於主機認得的版本（目前 `2`）。不符時主機**必須**停止解析
所有 `$` 資料行並顯示「韌體版本不符」，不嘗試向下相容舊協定——本協定
不保證跨版本相容。裝置每次開機、每次收到 `PING`、以及每次
`SENS`/`MEL`/`switch` 改變輸出組態後都要重發一次 `$STATUS`，讓主機隨時
能重新確認版本與目前解析度。

#### 1.1.3 `$A`（ambient 幀，crosstalk 分析用）

```
$A,<A|B>,<seq:u32>,<t_us:i64>,<dim>,<a0>..<aN>
```

| 欄位 | 型別 | 單位 | 範圍 | 說明 |
|---|---|---|---|---|
| `a0..aN` | i16 | `ambient_per_spad / 100` | `-1`（無效）或 ≥0 | 環境光子率，`dim` 個（zone 數） |

其餘欄位語意與 `$T` 相同（`seq` 為本串流已送出的行數，`t_us` 取樣點同 `$T`）。
**`$A` 是第五條獨立串流**，`seq` 不與 `$T` 共用（§1.1.1）。

**預設關閉，由 `AMB:<0|1>` 開啟**（§1.2）。開啟時建議 **1 Hz**，不要跟著 `$T` 走：

> **為什麼要獨立成一行而且預設關閉**：`ambient_per_spad` 是 crosstalk 最靈敏的
> 指標（比距離偏移更早顯現，`D10`），但它**變化很慢**，不需要 30 Hz。
> 若塞進 `$T` 每一幀，8×8 會多約 190 bytes/行——§1.4 顯示 8×8 開 Mel 已經到 **70%**，
> 再加就爆了。而 `D10`／`E02` 是**專門的短時實驗**，不是持續監測，
> 開一個旗標跑一段就好。

**HDF5**：對應 `/trial_NNN/tof_ambient_A`、`tof_ambient_B`，形狀 `(Ta, 16) float32`，
配一個 `tof_ambient_t_us (Ta,) int64`。**`Ta` 是自己的時間軸**，
不與 `tof_t_us` 共用——理由同 §2 的 `mel`／`mel_t_us`（不同取樣率不可假設等長）。
無效 zone 一律 `NaN`（§2）。

**前向相容規定（所有 `$` 資料行）**：主機端解析器一律以「**至少** N 段」
檢查欄位數，**多出來的尾端欄位必須忽略，不可判定為畸形行**；
建議把多餘欄位原封不動收進一個 `extra` 串列，讓還不認得它的下游仍看得到。
**不可**用 `len(parts) != N` 這種等號檢查。

> **為什麼**：韌體加一個欄位是常態（`A15` 就為了頻寬量測在 `$H` 尾端加了
> `bw_bytes_since_last`）。等號檢查會讓**整行事件消失**——不是「新欄位讀不到」，
> 而是 `$H` 連同 `heap`、`drop_*` 一起被當成雜訊丟掉，而且不會有任何錯誤訊息，
> 只會看到心跳莫名其妙不見了。這比讀不到新欄位嚴重得多。
>
> 用「至少 N 段」仍然抓得到**截斷**（傳輸損壞最常見的形態），
> 只是不再把「比我新的韌體」誤判成壞掉的韌體。
> `$T` 雖然行內自帶 `dim`，值的個數檢查（`2 × dim`）一樣要放寬——
> A 軌哪天在 `$T` 尾端加欄位，ToF 資料會整批消失。`$M` / `$F` / `$H` 同理。

**唯一的例外：協定 v1 的 `$TOF`。** 那裡**必須保持等號檢查**，理由有二：
1. v1 是**已凍結的歷史格式**，不會再長新欄位——多出來的段只可能是傳輸損壞。
2. 更關鍵：v1 `$TOF` 的**值數本身兼任方言判別**
   （`zones` 個 = 只有距離、`2 × zones` 個 = 距離＋signal）。
   放寬成「至少」的話，一條**被截斷的「距離＋signal」行會被誤讀成一條完整的
   「只有距離」行**——那是**靜默地接受壞資料**，比誤判成畸形嚴重得多。

通則的目的是「不要把比我新的韌體誤判成壞掉的韌體」。
沒有未來版本的格式，就沒有套用它的理由。

**無效值語意**：ToF 的 `d`（距離）與 `s`（signal）欄位在該 zone
`target_status ∉ {5, 9}` 時，兩者**一律**回 `-1`；主機看到 `-1` 要把
距離＋signal 當成同一組缺值一起跳過，不可只判斷其中一個。`$M` / `$F` /
`$H` 目前沒有定義哨兵值——若某次讀取失敗，裝置該幀直接不送，靠 `seq`
不連續（配合 `$H` 的 `drop_*` 計數）讓主機偵測掉幀，而不是送一個假數字。

#### 1.1.1 `seq` 的語意：四條獨立串流

`$T`(A) / `$T`(B) / `$M` / `$F` 是**四條各自獨立的串流**，各自維護自己的 `seq`。

- `seq` = 該串流**已送出的行數**，不是「應送出的幀數」。送出成功才 `++`。
- **跨模態對齊一律靠 `t_us`，不靠 `seq`。** 任何「`$F.seq == $M.seq`」或
  「兩者成固定比例」的假設都是錯的。

> **為什麼**：`A14` 把 Mel 的 hop 改成 256（`$F` → 62.5 Hz），而 `$M` 維持
> 31.25 Hz。兩者頻率不同，物理上不可能一一對應。這是 `A12` 與 `A14` 兩份
> story 規格之間的矛盾，不是實作偏差。`t_us` 從 `T01` 凍結起就是為了跨模態
> 對齊而存在（見 1.3 兩種模態的取樣點定義），靠 `seq` 對齊會讓整套設計失去意義。

#### 1.1.2 `$STATUS` 的音框參數欄位（韌體自我描述）

```
$STATUS,res=<dim>,proto=2,fw=<sha>,sr=16000,mel=<0|1>,amb=<0|1>,mel_win=<n>,mel_hop=<n>,mic_hop=<n>
```

| 欄位 | 型別 | 說明 |
|---|---|---|
| `sr` | u32 | 音訊取樣率 Hz，目前 `16000` |
| `mel` | u8 | `$F` 串流開關目前狀態（`MEL:<0\|1>`，見 1.2） |
| `amb` | u8 | `$A` 串流開關目前狀態（`AMB:<0\|1>`，見 1.1.3／1.2） |
| `mel_win` | u16 | FFT 窗長 samples，目前 `512` |
| `mel_hop` | u16 | Mel 幀間距 samples，`A14` 前 `512`、之後 `256` |
| `mic_hop` | u16 | `$M` 幀間距 samples，目前 `512` |

> **為什麼**：`A14` 之後，**同一個 `$F` 行格式在改動前後代表不同東西**
> （31.25 Hz vs 62.5 Hz、幀間距 32 ms vs 16 ms），但行本身長得一模一樣，
> 主機端無從得知自己接的是哪一版。韌體必須自我描述。

**解析規定**：`$STATUS` 一律以 **key=value 解析、順序無關、未知欄位忽略**，
**不可**用固定位置切分。日後還會再加欄位，用位置切分的解析器會壞。

**選用欄位缺漏時，主機端一律回 `None`，不可填預設值。**
「沒送這個欄位」（舊韌體）與「送了而且值是 512」是兩件不同的事，
填預設值會讓舊韌體看起來像新韌體——而 §1.1.2 存在的唯一目的就是分辨版本。
同理，選用欄位**格式壞掉**（如 `mel_hop=abc`）時只讓該欄位為 `None`，
**不可讓整行 `$STATUS` 解析失敗**——那一行還扛著版本協商，比任何參數都重要。
未知欄位「忽略」指的是**不因此報錯**，不是丟棄：建議原文保留一份 key=value
dict，讓日後新增、目前解析器還不認得的欄位，下游仍看得到。
（`host/capture/protocol.py` 的 `_parse_status()` 已符合此規定。）

**各 `$` 行欄位型別／單位／範圍**

#### `$T`（ToF 幀，A/B 各一行）
| 欄位 | 型別 | 單位 | 範圍 | 說明 |
|---|---|---|---|---|
| `sensor` | char | — | `A`\|`B` | 感測器 ID |
| `seq` | u32 | 幀序號 | 0–4294967295 | 溢位處理見 1.3 |
| `t_us` | i64 | µs | ≥0 | 取樣點定義見 1.3 |
| `dim` | u8 | zone 數 | `16`\|`64` | 4×4=16、8×8=64 |
| `d0..dN` | i16 | mm | `-1`（無效）或 0–4000 | 上限為感測器規格值，實際依環境待 A 軌實測校正 |
| `s0..sN` | i16 | `signal_per_spad/100` | `-1`（無效）或 ≥0 | 無理論上限，實務值多落在 0–200 |

#### `$M`（麥克風統計幀）
| 欄位 | 型別 | 單位 | 範圍 | 說明 |
|---|---|---|---|---|
| `seq` | u32 | 幀序號 | 0–4294967295 | |
| `t_us` | i64 | µs | ≥0 | 音框第一個 sample 的時間 |
| `rms` | i16 | 16-bit PCM 原始振幅 | 0–32767 | 見上方變更紀錄：改自草案 `f1` |
| `peak` | i16 | 16-bit PCM 原始振幅 | 0–32767 | 該幀樣本絕對值最大者 |

#### `$F`（Mel 幀，佔位）
```
$F,<seq:u32>,<t_us:i64>,<m0>..<m39>
```
`m0..m39` 的型別、單位、正規化方式由 `T03`（特徵向量規格）決定；此處
只凍結**幀格式**：固定 40 個係數，`seq`/`t_us` 語意與 `$T`/`$M` 相同。

#### `$H`（心跳）
| 欄位 | 型別 | 單位 | 範圍 | 說明 |
|---|---|---|---|---|
| `t_us` | i64 | µs | ≥0 | |
| `drop_A` / `drop_B` / `drop_M` | u32 | 累積掉幀數 | ≥0 | **自開機起算**，不被 `$STATUS` 重置（見變更紀錄：PING 會重發 `$STATUS`，若重置會讓 B05 校時期間指標失效）。與 `seq` 同為 session 計數，重開機歸零 |
| `heap` | u32 | bytes | ≥0 | 目前可用 heap |
| `temp_c` | i8 | °C | -40–125 | 晶片溫度 |
| `bw_bytes_since_last` | u32 | bytes | ≥0 | **A15 新增**：自「上一行 `$H`」以來送出的位元組數，不是固定的「最近一秒」——用兩行 `$H` 各自的 `t_us` 相減換算成真正的秒數再除。目前只計入 `$T`/`$STATUS`/`$H` 本身（`uart_out_add_bytes()` 的 opt-in 呼叫者），`$M`/`$F`/錄音 dump 尚未加入，是頻寬的下界不是總量。**⚠️ 已知相容性缺口**：`host/capture/protocol.py` 的 `_parse_heartbeat()` 目前硬性要求 `len(parts) == 7`（對應舊的 6 欄格式），這個新欄位讓 `$H` 變成 8 段，在該函式更新前**每一行 `$H` 都會被判成畸形行、整行事件消失**，不只是新欄位讀不到。掉幀率判定不受影響（`B03`/`dropwatch.py` 完全靠 `seq` 缺口，不吃 `$H`），但 heap／`bw` 相關的下游（`C04`?）在此之前拿不到任何 `$H` 事件 |

#### `$STATUS`
| 欄位 | 型別 | 單位 | 範圍 | 說明 |
|---|---|---|---|---|
| `res` | u8 | zone 邊長 | `4`\|`8` | |
| `proto` | u8 | — | 目前 `2` | 版本協商依據，見上 |
| `fw` | string | — | git short sha | 例：`a1b2c3d` |

#### `$REC` / WAV 標頭
| 欄位 | 型別 | 單位 | 範圍 | 說明 |
|---|---|---|---|---|
| `seconds`（`$REC,start,<seconds>`） | u16 | 秒 | 1–30 | 韌體硬上限，見 1.4 頻寬預算與變更紀錄 |
| `rate` | u32 | Hz | 例 16000 | |
| `bits` | u8 | bit | 例 16 | |
| `channels` | u8 | 聲道數 | 例 1 | |
| `bytes` | u32 | bytes | ≥0 | Base64 解碼前的 PCM 位元組數 |

**真實範例（含極端值）**

```
# 4x4，感測器 A，第 105 幀：zone 3 無效（-1/-1），其餘含最小值 0mm 與最大值 4000mm
$T,A,105,1737863421123456,16,120,0,4000,-1,880,340,210,995,60,1200,3400,77,15,600,2200,88,300,120,58,-1,60,45,88,102,19,7,140,55,66,44,20,90

# 4x4，感測器 B，同一時刻：全部有效
$T,B,105,1737863421125102,16,300,450,600,750,80,120,90,60,900,1100,1300,1500,1600,1700,1800,1900,110,95,130,75,88,70,60,50,45,40,35,30,20,15,10,5

# 麥克風幀：接近靜音
$M,3150,1737863421124800,12,340

# 麥克風幀：拍手瞬間，接近 16-bit 滿刻度
$M,3151,1737863421126400,28901,32767

# 心跳：一切正常（含 A15 新增的 bw_bytes_since_last）
$H,1737863421130000,0,0,0,142300,42,3840

# 心跳：ToF-A 掉了 3 幀、heap 偏低（接近告警）
$H,1737863431130000,3,0,0,18200,58,3712

# 開機／PING 回應，proto 版本協商用
$STATUS,res=4,proto=2,fw=a1b2c3d
```

### 1.2 主機 → 裝置

```
REC:<seconds>            錄音並 dump
SENS:<A|B>=<0|1>         感測器開關（真的 stop_ranging，不只跳過輸出）
MEL:<0|1>                Mel 串流開關
AMB:<0|1>                ambient 串流開關（$A，預設關閉，見 1.1.3）
PING                     立即回一行 $H
```

| 指令 | 參數型別 | 範圍 | 說明 |
|---|---|---|---|
| `REC:<seconds>` | u16 | 1–30 | 錄音並在結束後以 `$REC,start,<seconds>` + `BEGIN_WAV_B64`…`END_WAV_B64` 回傳。上限 30 s：dump 期間吃掉 92% 頻寬，30 s 錄音約需 28 s 傳輸，期間 ToF 幾乎全掉幀 |
| `SENS:<A\|B>=<0\|1>` | char + bool | `A`/`B`，`0`/`1` | `1`=開啟；`0`=真的呼叫 `stop_ranging()`，不只是跳過輸出 |
| `MEL:<0\|1>` | bool | `0`/`1` | Mel 串流開關 |
| `PING` | 無參數 | — | 觸發立即回一行 `$H`，延遲見 1.3「PING 回應延遲」 |

**範例**
```
REC:5
SENS:B=0
MEL:1
PING
```

### 1.3 已凍結的決定事項

| 項目 | 決定 |
|---|---|
| `t_us` 取樣點（ToF） | `check_data_ready()` 回 true 的當下，**在 `get_ranging_data()` 之前** |
| `t_us` 取樣點（Mic） | **音框第一個 sample 的時間** = 讀取回傳時刻 − n/rate |
| signal rate 縮放 | `signal_per_spad / 100`，四捨五入 |
| 無效值 | `-1`（距離與 signal 皆是），適用情境見 1.1「無效值語意」 |
| 有效判定 | `target_status ∈ {5, 9}` |
| `seq` 溢位 | `uint32`，以 `$STATUS` 為 session 邊界（重開機會歸零，不特別處理溢位） |
| `t_us` 溢位 | `esp_timer_get_time()` 為 int64 µs，約 29 萬年才溢位，不處理 |
| 行長上限 | 8×8 + signal ≈ 780 bytes → 主機 readline buffer ≥ 1024。@460800 baud 單行約 17 ms，8×8 為 10 Hz（100 ms/幀）夠用，但兩顆同時會吃掉約 34% 頻寬，見 1.4 |
| 浮點格式 | 一律定點整數，避免 locale 與 printf 浮點成本（`$M` 已依此改為 `i16`） |
| PING 回應延遲 | 含最多 2 ms 排隊延遲，主機端統計時用最小值而非平均值 |

#### 1.3.1 `$M` 的 RMS 涵蓋範圍

`A14`（hop 256）之後，`$M` 在**偶數次迭代**送出，且**必須使用與 `$F` 相同的
512 樣本窗**，讓連續的 `$M` 幀取得**不重疊且完整**的音訊涵蓋。

**不可只對剛讀進來的 256 個新樣本算 RMS。**

> **為什麼**：只算新的 256 個，會讓**一半的音訊不進入任何 RMS 幀**。
> 短促爆音（子音起始、拍手）若剛好落在被跳過的那半，對 `B15` 的音訊 VAD
> **完全隱形，而且不會報任何錯**——這是最難 debug 的一種缺陷。

`$M` 與同一次迭代送出的 `$F` 共用同一個 `t_start`（兩者描述同一個 ring 快照）。

### 1.4 頻寬預算（460800 baud ≈ 46 KB/s）

| 組態 | `$T` ×2 | `$M` | `$F` | 合計 | 使用率 |
|---|---|---|---|---|---|
| 4×4 @30Hz, Mel 512 hop | 8.7 KB/s | 0.5 | 7.8 | 17.0 | 37% |
| 4×4 @30Hz, Mel 256 hop | 8.7 | 0.5 | 15.6 | 24.8 | **54%** |
| 8×8 @10Hz, Mel 256 hop | 16.0 | 0.5 | 15.6 | 32.1 | **70%** |
| 錄音 dump 期間 | — | — | — | +42.7 | **⚠ 超載** |

**錄音 dump 會吃掉 92% 頻寬**，期間 ToF 必然掉幀。B01 要能容忍這段，
C04 要顯示「錄音傳輸中」而非「連線異常」。

---

## 2. HDF5 Session Schema

**負責：`T02`** ｜ `schema_version = 1` ｜ 凍結日期：`2026-08-26`

空的、結構正確的範例檔可用 `ssi-backlog/tools/schema_example.py` 產生，
D 軌可直接對著它開發讀取程式，不需要等真實資料。

```
/meta (attrs)
    schema_version, subject, session_date, wear_id, mode,
    distance_mm, angle_deg, ambient, notes,
    fw_sha, proto_version, tof_dim,
    sensors_enabled,        # "AB" | "A" | "B" —— 擷取當下哪幾顆在 ranging
    clock_slope, clock_offset, clock_residual_p95,
    clock_drift_us, clock_drift_ppm, clock_sync_span_us, clock_sync_confirmed,
    session_start_device_us, session_start_host_us, session_start_rtt_min_us,
    session_end_device_us,   session_end_host_us,   session_end_rtt_min_us,
    clock_cross_check_ppm_diff, clock_cross_check_ok,
    baseline_mu_A (32,), baseline_sigma_A (32,),
    baseline_mu_B (32,), baseline_sigma_B (32,),
    noise_floor_mu, noise_floor_sigma

/trial_NNN
    tof_A       (T, 32) float32    [0:16] 距離 mm, [16:32] signal/100
    tof_B       (T, 32) float32
    tof_t_us    (T,)    int64
    tof_valid_A (T, 16) bool
    tof_valid_B (T, 16) bool
    mic_rms     (M,)    float32
    mic_peak    (M,)    int16
    mic_t_us    (M,)    int64
    tof_ambient_A    (Ta, 16) float32  選填  ambient_per_spad/100，無效填 NaN
    tof_ambient_B    (Ta, 16) float32  選填
    tof_ambient_t_us (Ta,)    int64    選填  與 tof_ambient_* 成對
    mel         (F, 40) float32    選填  ⚠ 軸是 F 不是 M，見下方說明
    mel_t_us    (F,)    int64      選填  與 mel 成對，缺一不可
    audio       (N,)    int16      選填
    audio_t0_us         int64
  (attrs)
    label, trial_idx, wear_id, mode,
    speaking_mode,          # normal|whisper|silent —— 與上面的 mode 是兩條不同的軸
    vad_confidence,         # f32，B15 的端點偵測信心度；silent 模式為 None
    valid_zone_ratio, drop_count,
    vad_start_us, vad_end_us, lip_onset_us, voice_onset_us,
    quality ∈ {ok, low, rejected}
```

**無效 zone 在 `tof_A` / `tof_B` 數值陣列裡一律填 `NaN`。**

上游（`host/capture/protocol.py`、`host/align/aligner.py`）用 Python `None`
標記無效值，但 HDF5 的 `float32` dataset 存不了 `None`。填法是 **`NaN`，不是 `-1`、
也不是 `0`**：任何算術碰到 `NaN` 都會讓結果變 `NaN`，**逼讀取端注意到**；
而 `-1` 或 `0` 會被當成一個合理的近距離值悄悄算進統計，錯了也不會有人發現。
有效與否仍以 `tof_valid_A` / `tof_valid_B`（`(T,16) bool`）為準，
`NaN` 是給「沒讀 valid 就直接算」的人的安全網。

> **VAD 四個時間欄位「偵測不到」的表示法**
> （`vad_start_us` / `vad_end_us` / `lip_onset_us` / `voice_onset_us`）：
>
> | 層 | 表示法 |
> |---|---|
> | Python API（`to_trial_attrs()`） | **`None`** |
> | HDF5 attrs（`B07` 寫入） | **整個 attr 不寫入** |
>
> **不可填 `0`、`-1`、或 capture 視窗邊界。** 填視窗邊界最糟：
> 「完全沒偵測到」會長得像「整段都在動」——
> 下游算統計時不會報錯，**只會安靜地算出錯的答案**。
>
> 屬性缺席時 `grp.attrs["lip_onset_us"]` 會拋 `KeyError`——
> **大聲失敗，而不是安靜地給出錯誤答案**。消費端用 `.attrs.get()`。
>
> ⚠️ **`silent` 模式下 `voice_onset_us` 必然缺席，但 `lip_onset_us` 仍應存在。**
> **不可假設四個欄位同時存在或同時缺席。**
>
> `B11` 在 `B15`/`B16` 完成前的佔位值（視窗邊界）必須換掉。
>
> **⚠ `mel` 的時間軸是 `F`，不是 `M`。**
> `A14` 之後 `$F` 是 62.5 Hz、`$M` 是 31.25 Hz——**兩者幀數不相等**（§1.1.1：
> 四條獨立串流，各自維護 `seq`，對齊一律靠 `t_us`）。
> 原本 schema 把 `mel` 寫成 `(M, 40)`，是 §1.1.1 凍結**之前**留下的假設。
>
> 所以 `mel` 必須有自己的時間軸 `mel_t_us (F,) int64`，
> **寫入端不可以檢查「`mel` 幀數 == `mic_t_us` 長度」**，
> 也**不可以**把 `mic_rms` 內插到 mel 的網格上來湊——
> 那是在沒有實際量測的時間點捏造數值，違反 §2.1「無效值用 mask 不要填造的值」。
>
> `mel` 與 `mel_t_us` **成對出現，缺一不可**：只有陣列沒有時間軸，
> 下游就只能猜取樣率，而那正是 §1.1.2 加自我描述欄位要解決的問題。

> **`session_end_*` 三個欄位，`SessionWriter` 建構時沒有、`close()` 前才補寫。**
> `session_end_device_us`/`session_end_host_us`/`session_end_rtt_min_us`
> 要等 session 真正結束才量得到，不能列進建構時必填欄位（`REQUIRED_META_KEYS`）
> ——那樣的話連第一個 trial 都寫不成。`host/storage/session_writer.py` 用
> `SessionWriter.finalize_session_end(...)` 補寫；沒呼叫也不報錯，只是
> `/meta` 少這三個欄位，不影響已寫入的 trial。

> **`clock_cross_check_ppm_diff` / `clock_cross_check_ok`：`B04`（回歸法
> slope）與 `B05`（兩點法 drift）的交叉驗證結果，`SessionWriter` 在寫
> `/meta` 時自動算好寫入，呼叫端不用自己比對。**
> `clock_cross_check_ppm_diff = |((clock_slope − 1) × 1e6) − clock_drift_ppm|`，
> 門檻沿用 `B04` 驗收條件的 ±200ppm（`host/clock/align.py` 的
> `SLOPE_TOLERANCE_PPM`，同一個「多準才算準」的定義只寫一處）。超過門檻
> `clock_cross_check_ok = False`，但**不會**擋 session 寫入或改動任何
> trial 的 `quality`——兩個獨立方法對不上是「這個 session 的時鐘可疑，
> 下游該知道」的訊號，不是「這筆資料一定是錯的」，是否要因此下修
> quality 由讀取端（`D` 軌／`B19` 儀表板）決定。

> **`sensors_enabled` 記錄擷取當下哪幾顆感測器在 ranging**（`"AB"`／`"A"`／`"B"`）。
>
> `D10`（實驗 C₀ 串擾）要比較「單顆開」與「兩顆開」的兩次錄製，
> 但 schema 原本**沒有記錄這件事**——所以 `D15` 的 `run_all` 從 session 檔
> **永遠無法自動配對 solo/dual**，`C0` 恆為 `SKIPPED`（`D15` 實作時發現）。
>
> 值的來源：`B` 軌的 `SENS:<A|B>=<0|1>` 指令本來就知道這個狀態
> （`host/control/device_state.py` 的 `sensor_a_enabled`/`sensor_b_enabled`）。
> ⚠️ 但 §4.1.2 註明那兩個是「主機端記的上次指令」不是裝置確認狀態
> （`$STATUS` 沒有 `sens_a=`/`sens_b=`）——**寫進 `/meta` 時要照實記錄這個限制**。
>
> **`mode` 與 `speaking_mode` 是兩條不同的軸，不可共用一個欄位。**
> - `mode`：**session／面板模式**（`quiz`、`record`…），關於**流程**
> - `speaking_mode`：**說話模式**（`normal` / `whisper` / `silent`），關於**受試者**
>
> `B15` 實作時發現契約完全沒有定義後者。讓 `"quiz"` 與 `"whisper"` 出現在同一個
> 欄位會讓 `D13`/`D17` 的「三種 mode 各跑一組」分析無法正確分群。
>
> ⚠️ **沒有 `speaking_mode` 就分不出「一筆漏偵是因為人沒出聲，還是門檻設錯」**——
> 那是 `E05` 之後回頭 debug 時唯一的線索。

### 2.1 兩個不可妥協的設計決定

**① 無效值用獨立布林陣列，不要把 `-1` 塞進數值裡。**
`-1` 混在距離資料裡，任何一個忘記過濾的 `mean()` 都會靜默產出錯誤結果，
而且不會報錯。幾週後才發現時，已經分不清哪些結論受影響了。

**② baseline 必須是同一次戴上時錄的。**
跨次戴的 baseline 會把戴法差異混進正規化，讓所有下游數字失真。
`B10` 強制在 session 開始時自動錄製，無法跳過。

### 2.2 manifest.csv

> **`n_frames` = ToF 幀數（`tof_A.shape[0]`，即 schema 裡的 `T`）。**
> ToF 與麥克風是不同長度（`T` vs `M`），欄位名沒有指明是哪一個。
> 取 ToF 的理由：ToF 是驅動 trial 切分、`valid_zone_ratio` 與 `quality`
> 判定的主模態。**不要假設成 `M`。**
>
> **`session_path` 一律存「相對於某個 `root` 的路徑」**，
> 增量更新（`add_session()`）與完整重建（`rebuild_manifest()`）
> **必須用同一個 `root`**——否則兩條路徑產生的 manifest 不一致，
> 而 `D12` 是靠 manifest 分組的，不一致會安靜地污染實驗分組。

```
session_path, trial_idx, label, wear_id, mode, quality,
n_frames, valid_zone_ratio, drop_count, session_date
```

manifest 是**衍生資料**，永遠可從 HDF5 重建（`rebuild_manifest.py`）。
這個設計讓 `B07` 不需要處理「寫 trial 成功但寫 manifest 失敗」的一致性問題。

---

## 3. 特徵向量規格

**負責：`T03`** ｜ 凍結日期：`2026-08-26`

### 3.1 音框與 Mel

```
窗長        512 samples (32 ms) @ 16 kHz
hop         256 samples (16 ms) → 62.5 Hz     [A14 之前為 512]
窗函數      Hann，週期性（periodic），非對稱
STFT        center=False（裝置端無前後 padding，逐幀比對用）
n_mels      40
fmin/fmax   80 Hz / 8000 Hz
mel 尺度    Slaney            ⚠ 不是 HTK
濾波器      三角形，面積正規化（norm='slaney'）
log         log10(max(power, 1e-10))
裝置端傳輸  int16 = round(log_mel * 100)
```

> **⚠ Slaney vs HTK 是最容易踩的坑。**
> librosa 預設 Slaney、多數 C 實作預設 HTK，兩者在 1 kHz 以上有可見差距。
> `A12` 的驗收條件是「裝置端與 librosa 相關係數 > 0.95」——公式不一致就永遠達不到。
> 對應參數：`librosa.filters.mel(..., htk=False, norm='slaney')`

> **⚠ `center=False`，不是 librosa 預設的 `center=True`。**
> 裝置端即時分幀沒有前後 padding 可用，`center=True` 會讓幀邊界對不上，
> 逐幀比對（`A12`、`tools/compare_mel.py`）就沒有意義。
> 對應參數：`librosa.stft(..., center=False)`。

### 3.2 ToF 特徵

#### 3.2.1 σ 下限一律是量化尺度，不是 `1e-3`

```python
SIGMA_FLOOR = 1 / math.sqrt(12)   # ≈ 0.28868，單位 = 該通道的傳輸單位
sigma_safe = np.maximum(sigma, SIGMA_FLOOR)
```

- 距離通道 → 單位是 **mm**
- signal 通道 → 單位是 **`signal_per_spad/100`**

**`$T` 的所有通道都是整數。** 一個量化到 1 個單位的量測，其雜訊 σ
**不可能有意義地小於量化本身**——均勻量化誤差的 σ 是 `Δ/√12`。

> **實測後果**（`B16`）：一個貼著剛性表面的 zone（真實 17.0 mm、
> 類比雜訊 0.15 mm），量化後 `baseline_sigma ≈ 0.026 mm`。
> 讀到 18 mm 那一幀：
>
> | 守衛 | z |
> |---|---|
> | `1e-3` | **39** ← 其他 zone 都在 ±3 |
> | `1/√12` | **3.47** ← 合理 |
>
> ⚠️ **這不是只有 mock 會遇到。** 真機上任何一個貼著硬表面、
> 或視野內沒有目標而回傳固定值的 zone 都會踩到，
> **而且不會有任何錯誤訊息**。

`1e-3` 只擋得住**除以零**，擋不住**「小到沒有意義」**——那才是真正的問題。

#### 3.2.2 任何除以 σ 的地方都要有守衛，`D11` 的 SNR 尤其

`D11.md` 原文：
```
snr_zone = |Δ_round - Δ_spread| / sigma_baseline
                                  ^^^^^^^^^^^^^^ 裸的，沒有守衛
```

**這是整條分析鏈裡最嚴重的一個洞**，因為 `D11` 的活躍 zone 索引
**直接餵給 `D01`**：

```
剛性表面 zone → σ≈0 → SNR = inf → 活躍 zone 排名第一
             → 主宰特徵向量 → 汙染 DTW 距離
             → D05 / D06 / D07 的辨識結果全部失真
```

**全程沒有任何錯誤訊息。** 加上 `1/√12` 守衛後，同一個 zone 的 SNR 是 3.26，
排名回到正常位置。

> 註：`exp_a_snr.py` 的實作**已經有守衛**（`np.maximum(..., SIGMA_FLOOR)`），
> 只是常數是 `1e-3`。**「有守衛但常數錯」與「完全沒守衛」的後果一樣**——
> `max(0.026, 0.001) = 0.026`，守衛等於沒作用。

#### 3.2.3 檢查清單

改動任何用到 σ 的程式碼時，逐一確認：

- [ ] σ 的下限是 `1/√12`（該通道的傳輸單位），不是 `1e-3`
- [ ] **每一個**除以 σ／std 的地方都有 `np.maximum` 守衛
- [ ] 若有 `inf`／`NaN` 流出，**是報錯不是繼續算**

### 3.3 串接順序（104 維／幀）

```
[  0: 32]  tof_A
[ 32: 64]  tof_B
[ 64:104]  mel
```

> 原 v3.3 簡報為 105 維（含 1 維 sEMG）。**本專案無 sEMG，改為 104 維。**

### 3.4 標準答案

`ssi-backlog/tools/reference_mel.py` 用 librosa 依上述參數產生標準答案，
供 `A12` 的裝置端實作比對。

> **路徑決議（2026-08-26）：** 原草案寫 `analysis/reference_mel.py`，
> 已改為 `ssi-backlog/tools/reference_mel.py`。理由：這是 A/D 兩軌
> 互驗用的**契約產物**，不是 D 軌的分析程式碼；放進 D 軌獨佔的
> `analysis/` 會變成別軌要依賴一個自己不能讀寫的目錄，且 D 軌沒有
> story 負責維護它。此檔為 **T 軌所有，A/B/D 三軌唯讀引用**，
> 不得各自複製修改——複製出去的版本會逐漸漂移，等於重造
> 「規格不一致」的原始問題。要改參數，先改本節，再回頭改程式。

---

## 4. HTTP / SSE 介面

**負責：`B09` `B18` `B19` `D09`** ｜ 凍結日期：`____`

### 4.1 端點

| 方法 | 路徑 | 負責 | 用途 |
|---|---|---|---|
| GET | `/` | `C01` | panel 靜態檔 |
| GET | `/events` | 既有 | SSE 事件流 |
| POST | `/record?seconds=N` | 既有 | 錄音 |
| POST | `/switch?res=4\|8` | `B18` | 切解析度（重燒，非執行期） |
| POST | `/sensor?id=A&on=0` | `B18` | 感測器開關（執行期，立即生效） |
| POST | `/mel?on=0` | `B18` | Mel 串流開關（執行期，立即生效） |
| POST | `/ambient?on=0` | `B18` | Ambient 串流開關（`A16`，執行期，立即生效） |
| GET | `/device/state` | `B18` | 裝置目前狀態 |
| POST | `/session/start` | `B09` | 開始 session |
| POST | `/session/end` | `B09` | 結束 session |
| GET | `/session/current` | `B09` | 當前 session（未開始回 204） |
| GET | `/session/prefill` | `B09` | 上次設定（`wear_id` 已 +1），供表單預填 |
| POST | `/session/baseline?seconds=N` | `B10`／`B19` | 擷取 baseline（預設 30 s，取自緩衝區「過去 N 秒」） |
| GET | `/baseline` | `B10`／`B19` | 目前 session 的 baseline 統計（未擷取回 204） |
| GET | `/pca?model=tof_only\|enrollment` | `C10`／`B19` | 已擬合的 PCA 模型（沒有模型回 204） |
| POST | `/trial/hold/start` \| `/stop` | `B12` | Hold-to-Record |
| POST | `/trial/abort` \| `/redo` | `B11` | 放棄 / 重錄 |
| POST | `/recognize` | `D09` | 辨識，回 TriResult |
| GET | `/templates` | `D09` | 已載入的樣板組 |

#### 4.1.1 Session metadata（`B09`）

邏輯層是 `host/storage/session_registry.py` 的 `SessionRegistry`；下面是
HTTP wiring（`B19`）要對應的形狀，例外型別已經對好該轉成哪個狀態碼。

**`POST /session/start`** — body 是 metadata JSON：

```json
{"subject": "s01", "wear_id": 3, "mode": "quiz",
 "distance_mm": 30.0, "angle_deg": 0.0, "ambient": "quiet room", "notes": ""}
```

必填：`subject` / `mode` / `distance_mm` / `angle_deg` / `ambient`。
`wear_id` **不是必填**——沒給就自動用「上次 +1」；同次戴上繼續錄時，
前端明確傳入跟上次相同的值即可覆寫自動遞增（不是「不能改」，是「預設 +1，
可覆寫」）。`distance_mm`/`angle_deg` 為 `0` 是合法值，不可用 `if not value`
判斷缺欄位（`SessionRegistry` 用 `in (None, "")` 判斷，只有真的沒給或空字串才算缺）。

成功（200）：

```json
{"session_id": "2026-09-01_S01", "subject": "s01", "wear_id": 4, "mode": "quiz",
 "distance_mm": 30.0, "angle_deg": 0.0, "ambient": "quiet room", "notes": "",
 "started_at": "2026-09-01T10:00:00",
 "warnings": ["距離 47 mm 偏離目標 17 mm"],
 "target_check": "warning", "note": ""}
```

`target_check` ∈ `{"not_configured", "ok", "warning"}`——**`config/session_targets.json`
的目標配戴幾何在 `E01` 上機量測前全是 `null`，此時一律回 `"not_configured"`、
`warnings` 是空陣列，不捏造警告**（沒有目標值就沒有依據判斷偏離，假的「距離
正常」比沒有檢查更危險）。`note` 在完全未設定、或只設定了距離/角度其中一個
軸時，說明原因；都設定齊全時是空字串。

錯誤對應：`MissingFieldsError` → **400**，body 帶缺的欄位名（例如
`{"error": "缺少必填欄位: distance_mm"}`）；`SessionAlreadyActiveError` → **409**。

**`POST /session/end`** — 無 body。成功回目前這個 session 的完整資訊（同
`/session/start` 的成功回應形狀）。沒有進行中的 session 時是
`NoActiveSessionError`，狀態碼由 `B19` 決定（story 沒有規定這條的驗收條件，
不像 `start` 的 409 那樣是硬性要求）。

**`GET /session/current`** — 有進行中的 session 回 200 + 同上形狀；
沒有則回 **204**（無 body）。

**`GET /session/prefill`** — 一律 200，回：

```json
{"subject": "s01", "wear_id": 4, "mode": "quiz",
 "distance_mm": 30.0, "angle_deg": 0.0, "ambient": "quiet room", "notes": "",
 "session_id": "2026-09-01_S01", "started_at": "...", "warnings": [...],
 "target_check": "warning", "note": ""}
```

（`wear_id` 已經 +1；其餘欄位原封不動來自 `config/last_session.json`。）
沒有歷史紀錄（第一次用）就回 `{}`。

#### 4.1.1a Baseline 與 PCA（`B10` / `C10` 的 HTTP wiring，`B19`）

**`POST /session/baseline?seconds=N`**（預設 30）——從 bridge 的對齊緩衝區取
「過去 N 秒」跑 `host/storage/baseline.py` 的品質檢查。沒有進行中的 session
或緩衝區資料不足 → **409**；品質檢查**沒過** → **422**，
且**不寫任何檔案、`baseline_done` 維持 `false`**（trial 仍然被擋住）。
通過 → **200**，呼叫 `mark_baseline_recorded()`，並廣播 `session`（`state:"baseline"`）
與 `baseline` 兩個 SSE 事件。

**`GET /baseline`** —— 回目前 session 已擷取的 baseline；還沒擷取回 **204**。
兩者的 body 形狀相同：

```json
{"source":"session", "ok":true, "reason":null, "captured_at_us":...,
 "mu_A":[32], "sigma_A":[32], "mu_B":[32], "sigma_B":[32],
 "noise_floor_mu":.., "noise_floor_sigma":.., "valid_zone_ratio":..,
 "unstable_zones":{"A":[..],"B":[..]},
 "no_signal_zones":{"A":[..],"B":[..]},
 "suspect_zero_variance_zones":{"A":[..],"B":[..]},
 "quality":{"A":{..},"B":{..}}}
```

> **⚠ `NaN` 一律序列化成 `null`，不是 `0`。** 沒有訊號的 zone 其 `mu`/`sigma`
> 本來就是 `NaN`；`json.dumps` 預設會吐出裸的 `NaN` 字面值——**那不是合法 JSON**，
> 瀏覽器的 `JSON.parse` 會整條訊息拋錯。轉成 `null` 表示「沒有數值」，
> **三個 zone 旗標陣列才是權威**（前端據此顯示 `N/A` 而不是假裝有個 0.0 mm 的穩定讀數）。
> 這條同樣適用於**所有 SSE 事件**。
>
> zone 索引是**每顆感測器各自 0–15**，所以三個旗標按 `A`/`B` 分開給——
> 攤平成一個陣列會變成「zone 3 有問題」卻沒說是哪顆的 zone 3。

**`GET /pca?model=tof_only|enrollment`** —— 回已擬合的 PCA 模型：

```json
{"source":"tof_only", "dims":64, "mean":[N], "components":[[N],[N]],
 "explainedVarianceRatio":[f,f]}
```

`model` 不是這兩個值 → **400**。**目前沒有任何流程會產生模型檔**
（`analysis/features/feature_assembly.py` 有 `fit_pca`/`save_pca`，但沒有人呼叫），
所以現在一律回 **204**。

> **為什麼回 204 而不是給一個 stub**：`C10` 自己已經有 placeholder 並且每 10 秒重試，
> 而且它只在 `source`/`dims` 改變時清空軌跡。一個現場即時擬合的模型會維持同一個
> `source` 標籤卻讓座標軸在既有的點底下轉動——**看起來像真的，但軌跡沒有意義**。
> 模型檔路徑約定為 `models/pca_<source>.joblib`，**還沒有指定由誰產生**。

#### 4.1.2 裝置控制（`B18`）

邏輯層是 `host/control/`：`commands.py`（指令字串組裝，§1.2 是唯一事實
來源，不要在別處重拼字串）、`resolution.py`（`ResolutionController` 狀態
機）、`device_state.py`（`build_device_state()`）。HTTP wiring 一樣是
`B19` 的事。

**`SENS`/`MEL`/`AMB` 是同一類：執行期指令，立即生效，跟 `/switch` 完全
不同。** `POST /sensor?id=A&on=0`、`POST /mel?on=0`、`POST /ambient?on=0`
都直接 `serial_write_lock` + `ser.write()`，成功就回 202（跟既有
`/record` 一樣，不用等裝置確認——確認靠下一行 `$STATUS`／SSE）。
`flashing.is_set()`（`ResolutionController.is_busy`）時三者跟 `/switch`
一樣回 **409**：燒錄期間序列埠被 build/flash 子行程獨占，寫指令沒有意義。

**`POST /switch?res=4|8` 是重燒，不是執行期指令。** ULD 驅動的 grid size
不能執行期切換——`bridge_server.py` 現有的 `do_switch_resolution()` 已經
是這個流程（editing → building → flashing → done|error），`B18` 只是把它
包成明確的狀態機（`ResolutionController`）供 `/device/state` 查詢進度，
沒有改變既有的 202 立即回應 + 之後用 SSE `flash` 事件通知結果這個模式。

> **⚠ 重燒期間 `$STATUS` 會重新出現、`seq` 會歸零**（§1.3：`seq` 以
> `$STATUS` 為 session 邊界）。`B03` 的掉幀偵測看到 `seq` 倒退判定
> 「重開機」是對的行為，但前端／`C04` 不該把它顯示成故障——`resolution_change_in_progress`
> 為真時看到 `seq` 歸零，代表的是「重燒中，預期行為」，用這個旗標抑制
> 誤判，不要另外發明一套判斷。

**`GET /device/state`** 回：

```json
{"resolution": 8, "proto_version": 2, "fw_sha": "a1b2c3d",
 "mel_enabled": true, "ambient_enabled": false,
 "sensor_a_enabled": true, "sensor_b_enabled": true,
 "sensor_state_confirmed": false,
 "resolution_change_in_progress": false, "resolution_change_state": "idle"}
```

> **⚠ `sensor_a_enabled`/`sensor_b_enabled` 是主機端記的「上次送出的
> `SENS` 指令」，不是裝置確認過的狀態。** `$STATUS` 有 `mel=`/`amb=`
> 自我描述欄位（§1.1.2），但**沒有**對應的 `sens_a=`/`sens_b=`——裝置
> 收到 `SENS` 只會重發 `$STATUS`，不會在裡面說「A 現在是關的」。
> `sensor_state_confirmed: false` 就是把這個落差明講出來，避免前端把
> 五個欄位當成同等可信。若之後想讓它變 `true`，韌體端要在 `$STATUS`
> 補 `sens_a=<0|1>`/`sens_b=<0|1>`——這是本 story 發現、還沒決定要不要做
> 的事，見完成回報「CONTRACTS.md 的疑問」。

`ambient_enabled` 目前恆為 `null`：`host/capture/protocol.py` 的
`_parse_status()` 還沒解析 `$STATUS` 的 `amb=` 欄位（只有
`sr`/`mel`/`mel_win`/`mel_hop`/`mic_hop` 五個，§1.1.2 的 `amb=` 是後來才
加的）。`build_device_state()` 已經在讀 `event.get("amb")`，`protocol.py`
補上解析後這裡不用改。

### 4.2 SSE 事件型別

```
{"type":"tof",     "sensor":"A", "seq":.., "t_us":.., "dist":[..], "signal":[..], "valid":[..]}
{"type":"mic",     "seq":.., "t_us":.., "rms":.., "peak":..}
{"type":"mel",     "seq":.., "t_us":.., "bands":[..]}
{"type":"quality", "t":.., "metrics":{"drop_rate":{"value":..,"level":"green","hint":".."}, ...}}
{"type":"trial",   "state":"PROMPT|COUNTDOWN|CAPTURE|CONFIRM|SAVE|REST|IDLE", "label":"..", "idx":.., "seed":.., "next_label":".."}
{"type":"session", "state":"started|baseline|ended", "progress":{..}}
{"type":"status",  "protocol_version":2, "degraded":false, "recording_allowed":true,
                   "warning":null, "dim":16, "fw":"..", "sr":.., "mel":.., "mel_win":..,
                   "mel_hop":.., "mic_hop":.., "stats":{..}}
{"type":"heartbeat","drop_A":.., "drop_B":.., "drop_M":.., "heap":.., "temp_c":..}
{"type":"link",    "state":"up|down"}
{"type":"record",  "state":"recording|receiving|done|error", ...}
{"type":"flash",   "state":"editing|building|flashing|done|error", ...}
{"type":"mfcc",    "state":"computing|done|error", "file":"..", ...}
```

**所有事件都可能帶 `"replay": true`**（`B17`）。前端必須顯眼標示，
否則會拿回放資料當即時資料——這在 Demo 時會出大問題。

**缺欄位就是缺欄位**（§1.1.2 的延伸）：韌體沒送的欄位在 SSE 事件裡
**不出現**，不補 `0`／`512`／`null` 之類的預設值。協定 v1 的 `tof`／`mic`
事件因此**沒有** `seq` 與 `t_us`，並額外帶 `"proto":1, "has_timestamp":false`
讓前端能明確標示降級資料，不會跟 v2 的即時資料混在一起。

#### `status`（`B02` / `B19`）

直接轉發 `host/capture/protocol.py` 的 `ProtocolParser.state()`，不重新組裝。
`recording_allowed` 為 `false` 時前端**必須**停用錄音鈕（v1 沒有 `t_us`，
錄下來的 session 無法做時間對齊也無法驗證）；`warning` 非 `null` 時必須顯示。

橋接端**連上序列埠後會立刻送一次 `PING`**：板子開機通常遠早於 bridge 啟動，
開機那行 `$STATUS` 早就過去了，不主動問就永遠協商不到版本、也拿不到 §1.1.2 的
音框參數。§1.1 規定「每次收到 `PING` 都要重發 `$STATUS`」正是為了這個情境。

#### `quality`（`B19`，1 Hz）

六個指標：`drop_rate`、`valid_zones`、`symmetry`、`clock_resid`（秒）、
`noise_floor`（16-bit PCM 振幅）、`bandwidth`（鏈路使用率 0–1）。
每個是 `{"value":.., "level":.., "hint":".."}`，`hint` **只在非綠燈時出現**。

`level` 有四種：`green`／`yellow`／`red`／**`unknown`**。
`unknown` 代表「還沒有資料」或「這個指標沒有設定門檻」——**刻意不是綠燈**，
沒人設定過的指標不等於通過。

門檻與 hint 文字都放 `config/quality_thresholds.json`，
bridge 每秒比一次 mtime，**改完存檔即時生效，不需重啟**。

事件可能額外帶 **`"alarms":[..]`**：主機端由 `seq` 缺口算出的掉幀數
**超過**裝置端 `$H` 回報的 `drop_*` 時觸發。主機只能靠「兩個收到的幀之間的缺口」
推算，所以正常情況下**永遠落後**裝置端（差額 = `$H` 當下的連續掉幀長度）。
差額為正代表幀在兩個計數器之間遺失——**那是傳輸層故障的訊號，不是誤差**，
因此獨立成警報而非放寬容忍度。前端應顯著標示，不要當成掉幀率的一部分。

> **`IDLE` 是合法狀態，必須廣播。** 它出現在 `REST` 結束、以及 `abort`／`redo` 之後，
> 意思是「可以開始下一個 trial 了」。前端沒有它就只能靠逾時猜，而猜錯的代價是
> 使用者對著一個不會反應的畫面等——`E05` 要錄 4 小時，這種摩擦會累積成真實成本。
>
> 🔴 **SSE 的 `mel.bands` 是「已解碼的浮點 log10 值」，不是線上的 `int16`。**
>
> | 層 | 值 | 範圍 |
> |---|---|---|
> | 線上（`$F`，§3.1） | `int16 = round(log_mel × 100)` | 約 **-1000 ~ 0** |
> | SSE（`{"type":"mel","bands":[...]}`） | **浮點 `log_mel`** | 約 **-10 ~ 0** |
>
> `host/capture/protocol.py` 的 parser **在轉發之前就已經除回浮點**，
> `to_sse_event()` 轉發的是 `event["log_mel"]`。
>
> ⚠️ **`C08` 照 §3.1 的線上格式假設 `-1000 ~ 0` 寫色階，
> 結果整張瀑布圖是純黃色一片、完全看不出頻譜結構**——
> 而程式邏輯完全正確，只有真的打開瀏覽器看才會發現。
>
> 其餘 SSE 欄位同理：**§1 是線上格式，§4.2 是解碼後的格式，兩者不同。**
> 消費端一律以 §4.2 為準，不要照 §1 推。
>
> 🔴 **`status` 事件必須帶 `source`，值域 `live | mock | replay-log | replay-session`。**
>
> `bridge_server.py` **分辨不出** pty 與真實 UART，**也不該猜**——
> 由操作者用 CLI 旗標明示（例如 `--source live|mock|replay-log`），預設 `live`。
>
> **為什麼這是硬性要求**：`E05` 要錄 4 小時。如果有人不小心對著 mock 錄，
> 產出的 HDF5 會**裝滿合成資料卻標記成真實量測**——
> `D13`/`D16`/`D17`/`D19` 全部會拿它跑出漂亮的結論，
> **而且沒有任何一層會發現**。
>
> 附帶效果：`C04` 的狀態列可以顯示資料來源，
> Demo 現場觀眾也分得出「這是即時」還是「這是回放」。
>
> ⚠️ `source` 與 `replay: true` 是**不同層**的東西，不可互相取代：
> - `source`（`status` 事件）= **這條連線接的是什麼**，整個 session 不變
> - `replay: true`（每個資料事件）= **這一筆事件是重播出來的**（`B17` 的 HDF5 回放）
>
> `T05`（序列埠 log 重播）產出的 pty 對 bridge 而言與 `T04` 的合成 pty 無異，
> **所以只能靠 `source` 標記，不會有 `replay: true`**——那是正確的分層。
>
> **`next_label`（下一個要念的詞）只出現在 `IDLE` / `REST` / `SAVE`。**
> `PROMPT`／`COUNTDOWN`／`CAPTURE`／`CONFIRM` **沒有**——
> 那幾個狀態下「當下」的詞才是重點，不是下一個。
>
> 詞序是**循環**的（`E05` 的重複次數遠超過 8 個詞），所以正常情況永遠有值；
> 只有 `_order` 本身是空的（建構時已擋）才回 `None`。
>
> ⚠️ **`abort` 會讓 `next_label` 前進、`redo` 不會**——這是兩者語意差異的直接後果。
> 詞指標在 `_do_save()` **之前**就前進，所以 `SAVE`／`REST` 事件 peek 到的是
> 「下一個」而不是剛存好的這個；`label`（顯示用）維持到真的進 `IDLE` 才清，
> 所以 `REST` 畫面還能顯示「剛才錄的是哪個」。
>
> **`session` 事件在 `state:"baseline"` 時的 `progress` 形狀**（`C11` 提案，已採納）：
> ```json
> // 進行中
> {"type":"session","state":"baseline","progress":{
>    "elapsed_s":12.5,"remaining_s":17.5,"duration_s":30,
>    "live_sigma_A":[16 個 float]|null, "live_sigma_B":[...]|null}}
> // 完成
> {"type":"session","state":"baseline","progress":{
>    "done":true, "outcome": <BaselineOutcome.to_dict()，見 host/storage/baseline.py>}}
> ```
> 前端用 `elapsed_s` **重新對時**（本地倒數只是估計，會漂）。
> `live_sigma_*` 為 `null` 時前端必須顯示「尚未收到伺服器事件、倒數是本地估計」，
> **不可以假裝正常**。
>
> 另有 `POST /session/baseline/retry`（重錄用，`C11` 的重新擷取按鈕）。
>
> **棄用「已存檔」的 trial**（`C14`）：
> ```
> POST /trial/reject {"trial_idx": N}   → mark_current_trial_saved_quality(..., "rejected")
> ```
> ⚠️ 與 `abort`／`discard` **不同層**：那兩個是「錄製當下、還沒落盤」，
> 這個是「**已經寫進 HDF5 之後**才決定不要」。
> **棄用不是刪除**——資料留著，只改 `quality` attr，
> 因為 `D12` 的 CV 實驗需要知道「這次戴的時候錄壞了幾次」。
>
> **`CONFIRM` 狀態的兩個端點**（`C12` 提案，已採納）：
> ```
> POST /trial/confirm   → confirm_keep()     仍然要存
> POST /trial/discard   → discard_pending()  跳過此詞（語意同 abort）
> ```
>
> ⚠️ **原本寫成 `/trial/confirm/keep` 與 `/trial/confirm/discard` 是錯的**，
> 已改成與實作一致。理由：`start` / `hold/start` / `hold/stop` /
> `confirm` / `discard` / `abort` / `redo` **全部是同一層的兄弟**，
> 對應狀態機的方法名。把 `discard` 放進 `confirm/` 命名空間底下語意不通——
> **「丟棄」不是「確認」的一種**。
> 前端鍵盤慣例：**Enter = keep、ESC = discard**。
> ESC 在整個錄製畫面上一律代表「不要、丟掉」（一般狀態下是 `abort`），
> **不另外發明規則**。
>
> **`CONFIRM` 是 Hold-to-Record（`B12`）專用的狀態**：按住時間短於 0.3 s
> 或超過 5 s 時進入。此時資料**已算好但尚未落盤**，等
> `confirm_keep()`（還是要存）或 `discard_pending()`（跳過此詞）決定。
> 這與 `B11` 「放棄的 trial 完全不落盤，不要寫了再刪」一致——
> **不是「先存再標記警告」，是「先不存，決定要留才存」。**
>
> **`abort` 與 `redo` 的語意不同**（`B11` 實作時定義，兩個端點存在的意義本來就該不一樣）：
> - **`abort`**：**跳過**這個詞，往下一個詞走。用於「這個詞今天念不好，先跳過」。
> - **`redo`**：**保留同一個詞**再試一次。用於「剛才咳嗽／手滑了」。
>
> 兩者都**不寫入 HDF5 也不進 manifest**——不是寫一筆再標記，是從來沒存在過。
> 事後要棄用**已存檔**的 trial 是另一回事，用 `quality="rejected"`（見下）。
>
> **`quality` 的值域凍結為 `{ok, low, rejected}`**，不要發明第四個值。
> 「棄用」對應 `rejected`。目前的判定門檻（`B11` 的暫定值）：
> `valid_zone_ratio >= 0.7 且 drop_count == 0` → `ok`；`>= 0.3` → `low`；其餘 `rejected`。
> ⚠️ **這組門檻沒有實測依據**，`E01`／`E03` 之後要回頭校準——
> 它直接決定 `D12` 的 CV 分組裡有多少資料被排除。

### 4.3 TriResult（`D07` / `D09`）

> **融合軌的拒識：門檻隨 `w` 線性內插。**
>
> ```
> theta_reject_fused(w) = w · theta_reject_tof + (1 - w) · theta_reject_mel
> reject_fused(w)       = (min(w·d_tof_raw + (1-w)·d_mel_raw) > theta_reject_fused(w))
> ```
>
> 🔴 **必須用「原始（未正規化）距離」`d_tof_raw` / `d_mel_raw`，
> 不能用 `d_tof` / `d_mel`。**
>
> `normalize_distances()` 會**強制減去最小值**，所以 `d_tof.min()` 恆為 `0`——
> 任何正的門檻都不會被超過，**`reject_fused` 會永遠回傳 `False`，完全沒用**。
> 而且不會有任何錯誤訊息，只會表現成「這個系統從不拒識」。
>
> 用原始距離也**正是兩端退化性質成立的原因**：
> `reject_tof` / `reject_mel` 本來就是用原始距離算出來、事先存好的。
>
> → **`d_tof_raw` 與 `d_mel_raw` 是 §4.3 JSON 的必要欄位**，
> 前端沒有它們就算不出融合拒識。
>
> `TriResult` **不存 `reject_fused` 這個布林值**——它隨 `w` 改變，
> 存下來就會跟滑桿不同步。**由前端用上式即時算**，
> 跟 `fuse(w)` 一樣不需要重打伺服器。
>
> **為什麼是線性內插**：融合距離本身就是 `w·d_tof + (1-w)·d_mel`
> （`D07`，兩邊都已正規化到可比尺度），門檻用同一組權重才自洽。
> 而且它在兩端**恰好退化成單模態的門檻**：
> `w=1` 時等於 `theta_reject_tof`、`w=0` 時等於 `theta_reject_mel`——
> **拖滑桿到底時不會出現行為跳變**，那正是 Demo 第 2 步要做的事。
>
> ⚠️ **拒識不是分歧，是沉默。** 判斷「三軌是否一致」時，
> **被拒識的那一軌不列入比較**——它沒有意見，不是有不同的意見。
>
> **拒識門檻的校準方法：預設為 `D22` 的雙邊 ROC，不是 `D06` 的單邊 LOO。**
>
> `D09` 證明單邊 LOO 方法有結構性缺陷：它只用 `_reject` 類別自己的
> leave-one-out 距離分布校準，而「LOO 最近距離」與「真詞查詢到自己樣板的
> 最近距離」**在統計上是同一種量**，樣板數增加時兩者一起縮小，門檻不會相對變寬。
>
> `D22` 改成同時用**兩邊**的分布產出 ROC，讓門檻是一個**明示的取捨**：
>
> | 每類樣板數 | 舊（單邊 LOO） | 新（雙邊 ROC） |
> |---|---|---|
> | 10 | ToF 32% / Mel 52% | ToF 3.3% / Mel 0.6% |
> | 30 | 32% / 44% | **0% / 0%** |
> | 100 | 37% / 45% | **0% / 0%** |
>
> 樣板數不平衡時（word:reject 從 1:0.3 到 1:3）新方法誤拒率全程 < 1.1%，
> 舊方法全程 30–54%。
>
> **舊方法保留為對照**（`enrollment.py` 的 `fit_reject_threshold`），
> 不刪除——它是這個發現的證據，真實資料上結論可能不同。
>
> ⚠️ **代價**：新方法校準耗時約 **17 倍**（n=100 時 1.5 s vs 0.09 s），
> 因為它對**每個詞類別**各做一次 LOOCV，**隨樣板數平方成長**。
> `E06` 的預算是 30 秒，n≤100 時仍遠低於門檻。
> **若 enrollment 樣板數大幅增加、或改用 DTW 距離，要重新評估。**
>
> **`theta_reject` 拆成 `theta_reject_tof` 與 `theta_reject_mel` 兩個獨立欄位。**
> ToF 與 Mel 的原始距離尺度不同（32 維 z-score vs 40 維 CMN），
> `_reject` 樣板的距離分布也不同——**共用一個閾值，其中一個模態一定校準不準**，
> 而且不準的那一邊會安靜地要嘛全部拒識、要嘛完全不拒識。
> 融合後的拒識判定由 `D07` 依 `w` 決定，不在序列化層合併。

```json
{"classes": ["八","五","一","啊","四","好","停","不要"],
 "d_tof": [2.1, 0.3, 1.8, ...],
 "d_mel": [1.9, 1.1, 2.4, ...],
 "reject_tof": false, "reject_mel": false,
 "tau": 0.5, "theta_reject_tof": 3.2, "theta_reject_mel": 1.7,
 "dist_method": "cosine",
 "latency_ms": {"feature": 12, "dist": 1.0, "total": 13}}
```

> **`dist_method` 記錄這次辨識實際用的距離函式（`"cosine"` 或 `"dtw"`）。**
> `D09` 預設 `"cosine"`：批次餘弦實測 0.147 ms，DTW 實測 8-12 ms，量級差
> 兩個數量級；而且 `D05` 的合成資料 LOOCV 顯示 DTW 準確率反而較差
> （56.2% vs 37.5%）。沒有證據支持 DTW 更準時，選較快的當預設對 Demo
> 反應速度更有利，`E05` 真實資料齊全後應該重新驗證。
>
> `latency_ms` 的 `"dist"` 鍵取代原本寫死的 `"dtw"`——距離函式本身可換
> （cosine 或 dtw），鍵名不應該綁死其中一種。

> **回傳的是正規化後的距離向量，不是分數。**
> 前端拿到就能自己算任意 `w` 的融合結果——這是 `C17` 滑桿
> 「拖動不需重念」的前提。兩個模態的距離必須各自正規化後才能加權，
> 否則量級差 10 倍時 `w=0.5` 實際上等於「幾乎只用其中一個」。

---

## 5. 檔案所有權

**負責：`T06`** ｜ 凍結日期：`2026-08-26`

| 目錄／檔案 | 擁有者 | 備註 |
|---|---|---|
| `vl53l7cx_test/main/**` | **A 軌獨佔** | 韌體 |
| `vl53l7cx_test/monitor/bridge_server.py` | **B 軌獨佔** | |
| `host/**` | **B 軌獨佔** | capture / storage / clock |
| `vl53l7cx_test/monitor/panel/**` | **C 軌獨佔** | 一模式一檔 |
| `analysis/**` | **D 軌獨佔** | |
| `tools/**` | **T 軌** | mock device |
| `ssi-backlog/tools/reference_mel.py` | **T 軌獨佔（契約產物）** | Mel 標準答案；A / B / D 三軌唯讀引用，不得各自複製或修改（見 3.4） |
| `config/*.json` | 共用（純資料） | 衝突僅為 JSON 合併 |
| `CONTRACTS.md` | **共用，需協調** | 改動通知全體 |

`monitor/panel/js/modes/` 下**一個模式一個檔案**：
`monitor.js`（C05–C10）、`record.js`（C11–C14）、`quiz.js`（C15–C21）、
`validate.js`（C22–C23）、`replay.js`（C24）。

這不是美學選擇，是平行化的必要條件——C05 與 C16 才能同時開發。

---

## 6. 詞彙集（`config/vocab.json`）

> **實驗 A（逐 zone SNR）的對照詞：`五`（`wu`，B 圓唇）vs `一`（`yi`，C 展唇）。**
>
> `D11` 的公式是 `|Δ_round − Δ_spread| / σ_baseline`，但**契約原本沒有規定
> 哪兩個詞是 round／spread 的對照組**（`D15` 實作時只能自己對應）。
> 明訂於此，`D15` 的 `run_all` 才能自動配對；用別的詞錄的話它會 `SKIPPED`
> 並列出實際看到的標籤。



> 🔴 **已知限制：本詞彙集不涵蓋舌音（viseme E）。**
>
> `D14` 的預期表列了 A／B／C／D／**E 舌音**／F 六類，
> 但 `config/vocab.json` 實際是 A／B／C／D／F／**G 應用**——
> **E 一個詞都沒有（永遠不會有樣本），G 有三個詞（好／停／不要）卻不在預期表裡。**
>
> **裁決：接受 E 為空列，不加詞。**
>
> 加一個舌音詞會連鎖影響 `C15`–`C21` 的版面、`D22` 的 enrollment 樣板數、
> `E05` 的錄音量——而收益只是驗證一個**預期本來就是「三個模態都弱」**的類別。
>
> **「本系統的詞彙集不涵蓋舌音，因此無法驗證該類別」本身就是一句誠實、
> 可寫進論文的限制陳述。** `D14`／`D15` 的報告必須明確寫出這條。
>
> ⚠️ **G 應用不硬套一個猜的預期**——猜出來的預期一旦寫進報告，
> 下一個人會把它當成原始論述引用。標 `no_expectation` 就好。
>
> 若日後真的要加舌音詞（「他」/t/、「大」/d/、「開」/k/），
> 程式已經支援：viseme 類別是從 `vocab.json` 讀的，加詞就會自動出現。



```json
{"words": [
  {"id":"ba",    "text":"八",   "viseme":"A 雙唇", "expect":"ToF"},
  {"id":"wu",    "text":"五",   "viseme":"B 圓唇", "expect":"ToF"},
  {"id":"yi",    "text":"一",   "viseme":"C 展唇", "expect":"ToF"},
  {"id":"a",     "text":"啊",   "viseme":"D 開口", "expect":"雙模態"},
  {"id":"si",    "text":"四",   "viseme":"F 擦音", "expect":"音訊"},
  {"id":"hao",   "text":"好",   "viseme":"G 應用", "expect":"雙模態"},
  {"id":"ting",  "text":"停",   "viseme":"G 應用", "expect":"雙模態"},
  {"id":"buyao", "text":"不要", "viseme":"G 應用", "expect":"ToF"}
],
 "reject": {"id":"_reject", "text":"靜止／其他"}}
```

**「五」與「四」的組合是這個詞彙集的設計核心。**
「五」ToF 強／音訊弱，「四」ToF 弱／音訊強。Demo 時把 `C17` 的融合權重
滑到純 ToF，「五」還認得出來但「四」認不出來——這個現象本身就是最好的
多模態論證。

詞彙集由設定檔驅動，所以 `E08` 的「降到 4 選項」備援只需要改這一個 JSON。
