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
| 2026-08-26 | 1. 序列埠協定 v2 | T04：新增 `ssi-backlog/tools/mock_device.py`（pty 假裝置，產生 `$T`/`$M`/`$H`/`$STATUS`）。預設 `--proto v2` 照本章格式；`--proto v1` 保留凍結前舊格式，讓 mock 現在就能對接 unmodified `bridge_server.py`，也作為 `B02` 雙協定相容測試的夾具 | `B01`, `B02`, `C01`, `C05` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | 調度決議：`A01`、`A02` 合併為一個實作單元。原因：§1 凍結後 `$T` 行的 `d0..dN,s0..sN` 是同一組欄位契約，且「無效值語意」要求距離與 signal 在無效 zone 必須同時回 `-1`（不能只有其中一個），中間切開會產生違反此規則、也沒有消費者能正確解析的過渡格式。實作已在 A01 完成，A02 內容併入 | `A01`, `A02`, `B01` | 是 |
| 2026-08-26 | 1. 序列埠協定 v2 | 凍結協定 v2：補齊每種 `$` 行的欄位型別/單位/範圍表、7 行真實範例（含極端值）、`$STATUS` 版本協商流程、`-1` 無效值的適用情境。**破壞性變更**：`$M` 的 `rms` 從草案佔位 `f1`（浮點文字）改為 `i16` 定點整數（16-bit PCM 原始振幅），以符合「浮點格式一律定點整數」的既有決定 | `A01`, `A03`, `B01`, `T04` | ⬜ 待通知（見完成回報） |
| 2026-08-26 | 5. 檔案所有權 | 建立目錄骨架並凍結；依 T03 決議新增 `ssi-backlog/tools/reference_mel.py`（T 軌獨佔，A/B/D 唯讀引用） | A, B, D, T03 | 是 |
| 2026-08-26 | 2. HDF5 Session Schema | 凍結 schema；新增 `ssi-backlog/tools/schema_example.py` 產生結構正確的空 HDF5 檔供 D 軌開發 | B, D, B07, B17, D01, D10 | 是 |
| 2026-08-26 | 3. 特徵向量規格 | 凍結規格（3.1–3.4）；`reference_mel.py` 路徑由草案的 `analysis/` 移至 `ssi-backlog/tools/`（調度決議，理由：跨軌唯讀契約產物，不是 D 軌分析程式碼）；新增可執行的 `ssi-backlog/tools/reference_mel.py` | A, B, D, T03, T06 | 是 |

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
$H,<t_us:i64>,<drop_A:u32>,<drop_B:u32>,<drop_M:u32>,<heap:u32>,<temp_c:i8>
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

**無效值語意**：ToF 的 `d`（距離）與 `s`（signal）欄位在該 zone
`target_status ∉ {5, 9}` 時，兩者**一律**回 `-1`；主機看到 `-1` 要把
距離＋signal 當成同一組缺值一起跳過，不可只判斷其中一個。`$M` / `$F` /
`$H` 目前沒有定義哨兵值——若某次讀取失敗，裝置該幀直接不送，靠 `seq`
不連續（配合 `$H` 的 `drop_*` 計數）讓主機偵測掉幀，而不是送一個假數字。

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
| `drop_A` / `drop_B` / `drop_M` | u32 | 累積掉幀數 | ≥0 | 自上次 `$STATUS` 起算，見 1.3 `seq` 溢位 |
| `heap` | u32 | bytes | ≥0 | 目前可用 heap |
| `temp_c` | i8 | °C | -40–125 | 晶片溫度 |

#### `$STATUS`
| 欄位 | 型別 | 單位 | 範圍 | 說明 |
|---|---|---|---|---|
| `res` | u8 | zone 邊長 | `4`\|`8` | |
| `proto` | u8 | — | 目前 `2` | 版本協商依據，見上 |
| `fw` | string | — | git short sha | 例：`a1b2c3d` |

#### `$REC` / WAV 標頭
| 欄位 | 型別 | 單位 | 範圍 | 說明 |
|---|---|---|---|---|
| `seconds`（`$REC,start,<seconds>`） | u16 | 秒 | 1–60 | 實務上限，見 1.4 頻寬預算 |
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

# 心跳：一切正常
$H,1737863421130000,0,0,0,142300,42

# 心跳：ToF-A 掉了 3 幀、heap 偏低（接近告警）
$H,1737863431130000,3,0,0,18200,58

# 開機／PING 回應，proto 版本協商用
$STATUS,res=4,proto=2,fw=a1b2c3d
```

### 1.2 主機 → 裝置

```
REC:<seconds>            錄音並 dump
SENS:<A|B>=<0|1>         感測器開關（真的 stop_ranging，不只跳過輸出）
MEL:<0|1>                Mel 串流開關
PING                     立即回一行 $H
```

| 指令 | 參數型別 | 範圍 | 說明 |
|---|---|---|---|
| `REC:<seconds>` | u16 | 1–60 | 錄音並在結束後以 `$REC,start,<seconds>` + `BEGIN_WAV_B64`…`END_WAV_B64` 回傳 |
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
    clock_slope, clock_offset, clock_residual_p95,
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
    mel         (M, 40) float32    選填
    audio       (N,)    int16      選填
    audio_t0_us         int64
  (attrs)
    label, trial_idx, wear_id, mode,
    valid_zone_ratio, drop_count,
    vad_start_us, vad_end_us, lip_onset_us, voice_onset_us,
    quality ∈ {ok, low, rejected}
```

### 2.1 兩個不可妥協的設計決定

**① 無效值用獨立布林陣列，不要把 `-1` 塞進數值裡。**
`-1` 混在距離資料裡，任何一個忘記過濾的 `mean()` 都會靜默產出錯誤結果，
而且不會報錯。幾週後才發現時，已經分不清哪些結論受影響了。

**② baseline 必須是同一次戴上時錄的。**
跨次戴的 baseline 會把戴法差異混進正規化，讓所有下游數字失真。
`B10` 強制在 session 開始時自動錄製，無法跳過。

### 2.2 manifest.csv

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

```
通道 0-15    距離 mm
通道 16-31   signal_per_spad / 100
正規化       per-zone z-score，用 /meta 的 baseline_mu / baseline_sigma
             σ 加守衛：np.maximum(sigma, 1e-3)
無效 zone    z 空間填 0（= 等於基線，最中性的假設）
活躍篩選     只保留 SNR > 2 的 zone（門檻可調，由 D11 提供索引）
```

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
| POST | `/switch?res=4\|8` | 既有 | 切解析度（重燒錄） |
| POST | `/sensor?id=A&on=0` | `B18` | 感測器開關 |
| POST | `/mel?on=0` | `B18` | Mel 串流開關 |
| GET | `/device/state` | `B18` | 裝置目前狀態 |
| POST | `/session/start` | `B09` | 開始 session |
| POST | `/session/end` | `B09` | 結束 session |
| GET | `/session/current` | `B09` | 當前 session（未開始回 204） |
| POST | `/trial/hold/start` \| `/stop` | `B12` | Hold-to-Record |
| POST | `/trial/abort` \| `/redo` | `B11` | 放棄 / 重錄 |
| POST | `/recognize` | `D09` | 辨識，回 TriResult |
| GET | `/templates` | `D09` | 已載入的樣板組 |

### 4.2 SSE 事件型別

```
{"type":"tof",     "sensor":"A", "seq":.., "t_us":.., "dist":[..], "signal":[..], "valid":[..]}
{"type":"mic",     "seq":.., "t_us":.., "rms":.., "peak":..}
{"type":"mel",     "seq":.., "t_us":.., "bands":[..]}
{"type":"quality", "t":.., "metrics":{"drop_rate":{"value":..,"level":"green","hint":".."}, ...}}
{"type":"trial",   "state":"PROMPT|COUNTDOWN|CAPTURE|SAVE|REST", "label":"..", "idx":..}
{"type":"session", "state":"started|baseline|ended", "progress":{..}}
{"type":"link",    "state":"up|down"}
{"type":"record",  "state":"recording|receiving|done|error", ...}
{"type":"flash",   "state":"editing|building|flashing|done|error", ...}
```

**所有事件都可能帶 `"replay": true`**（`B17`）。前端必須顯眼標示，
否則會拿回放資料當即時資料——這在 Demo 時會出大問題。

### 4.3 TriResult（`D07` / `D09`）

```json
{"classes": ["八","五","一","啊","四","好","停","不要"],
 "d_tof": [2.1, 0.3, 1.8, ...],
 "d_mel": [1.9, 1.1, 2.4, ...],
 "reject_tof": false, "reject_mel": false,
 "tau": 0.5, "theta_reject": 3.2,
 "latency_ms": {"feature": 12, "dtw": 47, "total": 61}}
```

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
