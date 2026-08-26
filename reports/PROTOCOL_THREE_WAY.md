# 序列埠協定三方對帳：CONTRACTS.md §1 / 韌體 / host/capture/protocol.py

**這輪只對帳，沒有改任何程式或契約。**

比對範圍：`ssi-backlog/CONTRACTS.md` §1（lines 77-353，一字不漏讀完）
vs `vl53l7cx_test/main/*.c`（唯讀）vs `host/capture/protocol.py`（唯讀）。

不在範圍：`§1.2` host→device 指令（`REC`/`SENS`/`MEL`/`AMB`/`PING`）的送端在
`bridge_server.py`，這次邊界不含它（`ed` 的檔案），所以指令表沒有三方對帳，
只對了韌體收指令那一半（`uart_cmd.c`）。

---

## 🔴 headline：`$A`（ambient）韌體有送、契約有規定，`protocol.py` 完全沒解析

`vl53l7cx_test.c:225-240` 的 `print_ambient_line()` 會送 `$A,<A|B>,<seq>,<t_us>,<dim>,<a0>..<aN>`
（`AMB:1` 開啟後，約 1Hz）。`CONTRACTS.md` §1.1.3（lines 109-133）完整規定了這個格式。

但 `host/capture/protocol.py` 的 `_HANDLERS`（line 394-401）只有
`$T`/`$M`/`$F`/`$H`/`$STATUS`/`$REC` 六種，**沒有 `$A`**。也不在
`V1_ONLY_PREFIXES`/`V2_ONLY_PREFIXES`（line 434-435）任何一邊裡。

實際發生的事（追過 `ProtocolParser.feed()` 的路徑，line 729-796）：
1. `$A` 不是 `$STATUS`，往下走。
2. `_version_of("$A")` 兩個集合都沒命中 → 回 `None`（當成「共用行」）。
3. 用 `parse_line`（v2）解析 → `_HANDLERS.get("$A")` 是 `None` → `_parse_with()` 直接回 `None`。
4. `feed()` 把它記成 **畸形行**（`_note_malformed("$A", text)`），`malformed_by_prefix["$A"]` 累加。

也就是說：**只要打開 `AMB:1`，protocol.py 就會把每一行 `$A` 都當成解析失敗**，
既拿不到 ambient 資料，也會拉高 `malformed_rate`（`C04` 的健康指標會顯示錯誤率
上升，但看不出原因是「一種正常的行完全沒被實作」，只會以為 UART 在噴垃圾）。

沒有任何測試涵蓋這個路徑（`test_protocol.py` 全檔案沒有 `$A` 或 `ambient` 字樣）。
這是解析器缺漏，不是契約或韌體的問題——歸類：**解析器錯**。

---

## 🟡 `$H`：契約文字是舊的，程式碼已經修好了（`sensors_seen` 之外今天第一個「契約落後於程式碼」的例子）

`CONTRACTS.md` 有兩處（line 23、line 244）用現在式／警告語氣寫：

> ⚠️ 已知相容性缺口：`host/capture/protocol.py` 的 `_parse_heartbeat()` 目前
> 硬性要求 `len(parts) == 7`……在該函式更新前每一行 `$H` 都會被判成畸形行

但實際讀 `protocol.py:277-307`：

```python
def _parse_heartbeat(parts: list[str]) -> dict | None:
    if len(parts) < 7:          # ← 已經是「至少」，不是 == 7
        return None
    ...
    bw = _u32(parts[7]) if len(parts) > 7 else None   # ← 第 8 段已經在讀了
```

韌體端 `vl53l7cx_test.c:299` 送的正是 7 個數值欄位（`t_us,drop_A,drop_B,drop_M,heap,temp_c,bw`）
= 8 個 part（含 `$H` 本身），跟 parser 期待的欄位順序、型別逐一對上，沒有落差。

**歸類：契約錯（過時）。** 這個 bug 已經修好，契約裡兩處「尚未修好」的敘述需要更新成
「已修好」，否則下一個人看到會誤以為 `$H` 現在還是壞的，去重工一次已經解決的問題。

---

## 逐行對帳

### `$T`（ToF 幀）—— ✅ 三方一致
| 項目 | 契約 | 韌體 | 解析器 |
|---|---|---|---|
| 欄位順序/型別 | `sensor,seq:u32,t_us:i64,dim,d0..dN,s0..sN` | `vl53l7cx_test.c:204` 逐項相符 | `_parse_tof` 逐項相符 |
| `dim` 是 zone 數不是邊長 | 明文（line 218） | `dim = TOF_GRID_DIM*TOF_GRID_DIM`（line 200，且行內註解引用契約） | `VALID_ZONE_COUNTS=(16,64)` |
| `-1` 配對語意 | d/s 必須同進同出 | `target_status∉{5,9}` 時兩者一起填 `-1`（line 206-211） | `_pair_zones` 正確配對，且對「只有一邊 -1」的違規多做了 `pair_violations` 計數防禦（比契約要求更保守，非 bug） |
| `seq` 只在送出成功時 `++` | 明文（§1.1.1） | `s_seq[i]++` 只在 `print_tof_line()` 之後（`vl53l7cx_test.c:429-430`）；I2C 讀失敗（`status!=0`）或 `drop_next`（不穩定的第一幀）都不印也不 `++`（line 425-443） | 忠實暴露 `seq`，不做假設 |

### `$M`（麥克風統計幀）—— ✅ 一致，一個沒寫進契約的細節
欄位、型別、`seq`/`t_us` 語意三方一致（`bone_mic.c:390` vs `_parse_mic`）。

**契約沒寫但實際存在的行為**：§1.3.1「偶數次送出」的規則，實際上是
「**成功讀取**的偶數次」，不是「第幾個 hop」。`bone_mic.c:324-330` 在
`i2s_channel_read` 失敗時會 `continue`，而 `emit_m_this_hop` 的 toggle
（line 378）在 `continue` 之後才執行——所以一次讀取失敗會讓下一次成功的
hop 沿用同一個 parity，不會「補上」被跳過的那次。對下游影響很小（`$M`
本來就是靠 `t_us` 對齊不是靠計數），但契約目前的講法（「偶數次」）沒有
說明這是以成功讀取為分母。

### `$F`（Mel 幀）—— ✅ 三方一致
固定 40 係數、`seq`/`t_us` 語意同 `$T`/`$M`，`bone_mic.c:393` 與 `_parse_mel` 相符。

### `$H`（心跳）—— 見上方 headline（契約錯：過時警告）；欄位本身三方一致
欄位順序 `t_us,drop_A,drop_B,drop_M,heap,temp_c,bw_bytes_since_last`
三方逐項相符（`vl53l7cx_test.c:299-306` vs `_parse_heartbeat`）。

**契約沒寫但實際存在的行為**：`temp_c` 只取 `s_results[0].silicon_temp_degc`
（`vl53l7cx_test.c:305`），也就是**永遠是感測器 A 的晶片溫度**，即使 B 也在跑。
契約的欄位表（line 240-243）只寫「晶片溫度」，沒說是哪一顆——如果 A 掛掉但
B 還在跑，`$H` 的溫度讀數仍然只反映 A（可能是舊值或無效值），這點下一個要用
溫度做健康判斷的人容易誤讀成「整機溫度」。

**已驗證為真、非新發現**：`uart_out_lock()`（`uart_out.c:14-19`）只包住
`printf("$...")` 那幾行，`ESP_LOGx` 完全不經過它——`uart_cmd.c`/`bone_mic.c`/
`vl53l7cx_test.c` 裡所有 `ESP_LOGI`/`ESP_LOGE`/`ESP_LOGW`/`ESP_LOGD` 呼叫都
沒有前後夾 `uart_out_lock()`。這確認了 dispatcher 訊息裡提到的懷疑，契約裡
目前沒有任何一處提到這件事。

**已驗證為真、非新發現**：`$H` 在 `PING` 回應時確實排在 `$STATUS` 前面——
`uart_cmd.c:256-257`：`tof_print_heartbeat(); tof_print_status();`，順序寫死
在原始碼裡，不是這次抽驗才成立的巧合。開機序列（`app_main`，
`vl53l7cx_test.c:311-324`）則是只送 `$STATUS`，開機當下不送 `$H`。

### `$A`（ambient）—— 🔴 見 headline（解析器錯：完全沒實作）
契約 §1.1.3、韌體 `print_ambient_line()` 都完整存在且彼此相符（欄位順序、
`-1` 語意、`seq` 是第五條獨立串流都對得上），唯獨 `protocol.py` 沒有
`_parse_ambient`，也不在任何 `_HANDLERS` 表裡。

### `$STATUS`—— ✅ 規則一致；`amb=` 缺口是契約已知且仍然為真
`_parse_status()`（line 310-355）驗證過：key=value、順序無關、未知欄位忽略、
缺欄位一律 `None`、單一選用欄位壞掉不拖垮整行——全部符合 §1.1.2
（line 198-208），契約 line 208 的「已符合此規定」正確。

`sr`/`mel`/`mel_win`/`mel_hop`/`mic_hop` 五個具名欄位三方一致。
契約 line 945-946 已經明文記錄「`_parse_status()` 還沒解析 `amb=`」——
這次確認**現在仍然是真的**（`amb` 只會躺在通用的 `fields` dict 裡，
沒有像 `mel`/`sr` 一樣被拉成具名欄位），不是新問題，契約已經追蹤過了，
不需要再開一條。

### `$REC`—— ✅ 三方一致
`bone_mic.c:154`：`printf("$REC,start,%u\n", seconds)`；
`_parse_record`（line 376-391）檢查 `parts[1]=="start"` 與 `seconds>=0`，
與契約 line 97/253-256 相符。

---

## `seq`/`t_us` 語意總結（§1.1.1 的五個問題）

`$T(A)`/`$T(B)`/`$M`/`$F` 四條獨立串流，`seq` 各自維護、只在**送出成功**時
`++`——四個增量點（`vl53l7cx_test.c:430`、`bone_mic.c:391`、`bone_mic.c:395`
附近的 `f_seq++`）全部確認只在對應的 `have_m`/`have_mel`/成功讀取分支內執行，
讀取失敗或第一幀丟棄都不會讓 `seq` 空跳號——**契約「seq 不連續 = 真的掉幀」
這個假設在韌體端成立，三方一致**。

跨模態對齊靠 `t_us` 不靠 `seq`：`protocol.py` 沒有任何地方拿 `$F.seq` 去對
`$M.seq`，忠實遵守契約 line 171-172 的規定。

---

## 小結：三類問題

- **契約錯（過時）**：`$H` 的 `len(parts)==7` 警告已經修好，contract line 23、244
  該更新為「已修好」。
- **解析器錯**：`$A` 完全沒有解析支援，AMB 開啟時整條串流被算成畸形行。
- **契約沒寫但實際存在的行為**：
  1. `$H.temp_c` 只來自感測器 A，不是整機溫度。
  2. `$M` 的「偶數次」cadence 分母是「成功讀取次數」不是「hop 次數」。
  3. `uart_out_lock()` 不保護 `ESP_LOG`（已驗證，非新發現）。
  4. `$H` 在 PING 回應中排在 `$STATUS` 之前是寫死的行為（已驗證，非新發現）。

其餘（`$T`/`$M`/`$F`/`$REC`、`$STATUS` 的 key=value 規則、`seq` 語意）
**對過了，一致**。
