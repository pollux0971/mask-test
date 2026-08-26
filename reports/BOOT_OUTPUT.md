# BOOT_OUTPUT — 開機時序列埠上會出現什麼

**範圍**：純讀 `vl53l7cx_test/main/` 原始碼 + sdkconfig，沒有碰板子、沒有改韌體。
**目的**：給 `reports/FIRST_REAL_DATA.md`（第一筆真實資料會在哪裡噎到）當上游輸入 —
主機端解析器一直只餵過 `mock_device.py` 產生的乾淨 `$` 行，這份文件列出真板子在那之前/之間
會混進哪些格式。

---

## 1. 開機後序列埠上依序會出現什麼

板子的 console UART 是 **UART0，設定死在 sdkconfig 裡是 460800 baud**
（`CONFIG_ESP_CONSOLE_UART_BAUDRATE=460800`，同一個值也用在
`CONFIG_ESPTOOLPY_MONITOR_BAUD`，所以 `idf.py monitor` 會用 460800 開 port）。

依時間順序：

### (a) Mask ROM 開機訊息 —— 🔴 這一段大機率是亂碼
```
rst:0x1 (POWERON_RESET),boot:0x8 (SPI_FAST_FLASH_BOOT)
SPIWP:0xee
mode:DIO, clock div:1
load:0x3fce3810,len:...
...
entry 0x403...
```
這些是晶片 **Mask ROM**（燒錄在矽片裡，不是我們的程式，sdkconfig 完全管不到）印的，
用的是 ROM 自己寫死的固定 baud（ESP32 系列常見是 74880 或 115200，**不是** 460800）。
但 `idf.py monitor` 一開始就用 460800 去讀 port —— **baud 對不上，這幾行在終端機上會是亂碼**
（看起來像一串隨機符號，不是文字）。這是 ESP32 系列的通病，不是這塊板子或這份韌體特有的問題。

### (b) 第二階段 bootloader 的 log —— 應該正常，走 460800
```
I (27) boot: ESP-IDF v6.0.2 2nd stage bootloader
I (27) boot: compile time ...
I (30) boot: chip revision: ...
I (34) boot.esp32s3: Boot SPI Speed : ...
I (39) boot: Partition Table:
I (43) boot:  ## Label ... Usage
...
I (xx) boot: Loaded app from partition at offset 0x10000
```
`CONFIG_BOOTLOADER_LOG_LEVEL=3`（INFO），這段第二階段 bootloader **共用主專案的
`CONFIG_ESP_CONSOLE_UART_BAUDRATE=460800`**，所以理論上這裡開始 baud 就對得上，
文字應該可讀。(a)→(b) 交界處確切在哪一個 byte 沒辦法從原始碼推，只能實測。

### (c) ESP-IDF app 啟動的 log —— 正常，460800，`I (...) tag: ...` 格式
```
I (xx) cpu_start: Pro cpu up.
I (xx) cpu_start: Starting app cpu, entry point is ...
I (xx) cpu_start: Pro cpu start user code
I (xx) cpu_start: cache ...
I (xx) app_init: Application information:
I (xx) heap_init: Initializing. RAM available for dynamic allocation:
I (xx) spi_flash: detected chip: ...
I (xx) main_task: Started on CPU0
I (xx) main_task: Calling app_main()
```
這些全部**在我們的 `app_main()` 被呼叫之前**印出，`CONFIG_LOG_DEFAULT_LEVEL=3`(INFO)，
一行沒被關掉。也就是說：**在使用者自己的程式碼跑起來之前，序列埠已經吐了幾十行
`I (數字) tag: 文字` 格式的東西**，沒有一行是 `$` 開頭。

### (d) `app_main()` 開始跑之後
原始碼 `main/vl53l7cx_test.c:311-343`：

```c
void app_main(void)
{
    uart_out_init();       // 317 -- 只是建立一個 mutex，不碰 UART 硬體
    tof_print_status();    // 324 -- 第一個 $ 行：$STATUS,...
    vTaskDelay(100ms);     // 327
    for (每個感測器) init_bus_and_sensor(i);   // 336-339 -- 期間會印 ESP_LOGI/E
    bone_mic_init();        // 341
    bone_mic_start_monitor(); // 342 -- 生出 mic_task (priority 5)
    uart_cmd_start();       // 343 -- 生出 uart_cmd_task (priority 5)
    while (1) { ... }       // 之後才是穩定狀態：$T/$A/$H 循環
}
```

**確認回答「$STATUS 是感測器 init 之前還是之後印的」**：**之前**。
`uart_out_init()` 之後、感測器 init 迴圈之前就送出 `$STATUS`，這是刻意設計
（見程式碼註解：host 端要盡快拿到 proto/fw 版本做協商，不必等 I2C 韌體上傳跑完）。

**`$STATUS` 是全程第一行 `$` 開頭的輸出**，但它前面已經有 (a)(b)(c) 三段、
少說幾十行非 `$` 格式的東西。

`init_bus_and_sensor()` 內部（`vl53l7cx_test.c:136-183`）在失敗/成功時會印
`ESP_LOGE`/`ESP_LOGI`（例如 `[A] loading firmware into sensor...`、
`[A] ranging started (8x8 @ 15Hz)`，或失敗時 `i2c_new_master_bus failed: ...`）。
這一段跑的時候**只有 app_main 這一個 task 在動**（`bone_mic_start_monitor()`/
`uart_cmd_start()` 還沒被呼叫），所以這幾行 log 不會跟任何 `$` 行搶著印
—— 這段本身是安全的。

---

## 2. 🔴 穩態之後：`$` 行有沒有被切開的風險

**有，而且是結構性的，不是罕見 edge case。**

### 機制
- `uart_out_lock()`/`uart_out_unlock()`（`uart_out.c`）只是一個 FreeRTOS mutex，
  只在**我們自己寫的 `$` 行輸出函式**裡使用（`print_tof_line`/`print_ambient_line`/
  `tof_print_status`/`tof_print_heartbeat`（vl53l7cx_test.c）、
  `bone_mic_record_and_dump`/mic_task 的 `$M`/`$F` 輸出（bone_mic.c)）。
- **`ESP_LOGI`/`ESP_LOGW`/`ESP_LOGE`/`ESP_LOGD` 完全不走這把鎖**——它們底層直接呼叫
  `vprintf` → console UART，跟 `uart_out_lock` 一點關係都沒有。
- 三個會印東西的 task 優先權不對等：
  - `app_main`（主 ToF 迴圈，$T/$A/$H/$STATUS 的來源）：**預設 main task 優先權**
    （`CONFIG_ESP_MAIN_TASK_PRIORITY`，通常是 1，比另外兩個低）
  - `mic_task`（`bone_mic.c:432`）：**priority 5**
  - `uart_cmd_task`（`uart_cmd.c:282`）：**priority 5**

  FreeRTOS 是搶佔式排程：**priority 5 的 task 只要就緒，隨時可以打斷 priority 1 的
  app_main**，不管 app_main 當下正不正在印 `$T` 行、有沒有拿著 `uart_out_lock`。

### 具體會被切開的組合
`print_tof_line()`（vl53l7cx_test.c:197-216）建一行 `$T,...` 要呼叫 **超過 130 次 `printf()`**
（8×8=64 個 zone 的距離、再 64 個訊號值，每個 zone 各一次 `printf`）——這中間有大量
排程器插手的機會。如果就在這 130 次 `printf` 之間，`mic_task` 或 `uart_cmd_task`
（priority 5，會馬上搶到 CPU）剛好呼叫了一行**不在鎖保護內**的 log，比如：

- `bone_mic.c:192` `ESP_LOGW(TAG, "i2s_channel_read failed during recording: %s", ...)`
- `bone_mic.c:422` `ESP_LOGI(TAG, "a15_perf: mic_task stack headroom = %u bytes", ...)`
  （每 10 秒固定會印一次，不是只有出錯才印，**發生機率不算低**）
- `uart_cmd.c:268` `ESP_LOGW(TAG, "unrecognised command: '%s'", cmd)`
- `vl53l7cx_test.c:366-367` 同一個 tof_task 自己的 `a15_perf` log（同 task 不會打斷自己，
  但如果剛好卡在两个 printf 之間被 mic_task/uart_cmd_task 搶走 CPU、那兩個 task 又剛好
  也要印東西，一樣會夾進去）

→ 結果就是 host 端會收到一行**看起來完整但其實是「半行 $T + 一整行 log + 剩下半行 $T」**
拼起來的東西，例如：

```
$T,A,42,1234567,64,120,130,...
I (98765) bone_mic: a15_perf: mic_task stack headroom = 3200 bytes
...,145,-1,138
```

單純用「找 `$` 開頭、找換行」的 parser 會把中間那行 log 當成一行雜訊丟掉沒關係，
但**如果 parser 假設『看到下一個換行就代表這個 `$T` 行結束了』，就會把 `$T` 行從中間
截斷，得到一行欄位數不對、CSV 解析會炸掉的殘缺 `$T`**——這是最危險的情況，
必須確認 host 端 parser 是「數欄位對不對」還是「看到換行就收工」。

### 什麼情況「不會」被切
- `app_main` 前段（感測器 init 期間）：只有一個 task 在跑，安全。
- 兩個 lock 保護的 `$` 行函式**彼此之間**互斥沒問題（這是鎖原本要解決的問題，做到了）。
- 問題只在「**鎖保護的 `$` 輸出**」vs「**沒被鎖保護的 ESP_LOG**」這一種組合。

---

## 3. 把 ESP-IDF log 導去別處，划不划算？（只評估，這一輪沒改程式碼）

`esp_log_set_vprintf()` 是 ESP-IDF 標準 API，可以把所有 `ESP_LOG*` 的輸出攔截到自訂函式
（丟掉、轉去 UART1、寫進一塊記憶體 buffer 都可以），一行 `esp_log_set_vprintf(my_func)`
在 `app_main()` 最前面呼叫即可，**改動量很小**。

⚠️ 但這一輪**不建議現在改**，理由：
1. 使用者現在燒的是**沒有這個改動**的版本，「最小改動優先」是他剛下的指示
   （[[minimal-change-first]]）。
2. 就算做了，也**只能解決「穩態之後的 ESP_LOG」**——(a)(b)(c) 三段開機噪音發生在
   `app_main()` 被呼叫**之前**，`esp_log_set_vprintf()` 再早呼叫也來不及攔到，
   ROM 那段（(a)）更是完全管不到（硬體限制，不是任何 sdkconfig/程式碼能改的）。
3. 治本的做法其實是「host 端 parser 已經預期會有雜訊行，只看 `$` 開頭 + 用欄位數
   驗證完整性」，而不是「板子這邊想辦法印乾淨」——後者永遠堵不完（ROM 那段就堵不了）。

如果之後真的要做，**低成本的部分**只有「把 `ESP_LOG_INFO` 等級的一般訊息關小聲，只留
`ESP_LOGE`」（`esp_log_level_set()` 或调低 `CONFIG_LOG_DEFAULT_LEVEL`），可以砍掉
`a15_perf` 這種週期性噪音，但**出錯時你最需要看到的 `ESP_LOGE` 反而還是會插進 `$` 行中間**
——所以這治標不治本，只是降低頻率，沒有解決結構性問題。

---

## 4. 給使用者的一句話

**燒完開機，你會先看到一小段亂碼（ROM 印的，大約幾行，正常，不用管），
接著一堆 `I (數字) 標籤: 文字` 格式的正常開機訊息，
然後才會看到第一行 `$STATUS,...`——這才是我們自己的程式開始跑。**

之後長時間跑的時候，**偶爾**會在 `$T`/`$M`/`$F` 這類長資料行中間**混進一行不是 `$`
開頭的普通文字**（通常是 `I (...) bone_mic: a15_perf: ...` 或 `W (...) uart_cmd: ...`）
——**這是已知、預期內的行為，不代表板子壞了**，是因為印 log 跟印 `$` 資料行是兩條不同
的路徑，沒有互相排隊。如果看到的是**一行 `$T` 莫名其妙欄位數不對、缺一截**，
才是需要回報的問題（代表被夾斷的那次，剛好夾在 `$T` 行正中間，不是夾在行尾）。

---

## 5. 給 FIRST_REAL_DATA.md 的重點摘要

- 第一份真實資料進 host 端 parser 之前，**前面已經有幾十行非 `$` 格式的開機噪音**
  （其中最前面幾行是**亂碼**，不是普通文字，parser 若對非 UTF-8 位元組沒做防禦
  可能直接噎住）。
- **第一行 `$` 開頭的東西是 `$STATUS`**，在感測器 init 之前送出——如果 parser
  預期「先看到感測器資料才看到 STATUS」，順序是反的。
- **`$T`/`$M`/`$F` 長行有機率被一行 log 從中間切斷**，不是理論風險、是目前程式碼結構
  必然存在的 race，機率取決於 mic_task/uart_cmd_task 的 log 呼叫頻率
  （`a15_perf` 每 10 秒至少一次，是最穩定會發生的來源）。
