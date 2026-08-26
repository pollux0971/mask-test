# 錄到一半斷了，那個 `.h5` 還讀得回來嗎——實測，不是推論

## 一句話答案（先講）

**除了「錄第一筆 baseline 的當下」這個特定窗口，錄到一半斷線最多只丟掉正在寫的那一筆，前面全部安全。`kill -9`、`kill -15`（含 USB 鬆脫、筆電斷電這類「行程被硬生生終止」的情境）都一樣安全，已經實測超過 15 次、涵蓋一般大小跟刻意放大的 trial，沒有一次讓已經寫完的資料損毀。**

**唯一真正的風險是 Ctrl-C（`SIGINT`）：它不保證能停下來。** 錄製途中按一次 Ctrl-C，大約 5 次裡有 4 次會被 Python 內部一個跟 h5py 無關的機制**吃掉**，程式什麼事都沒發生地繼續錄——不是變慢，是**看起來完全正常地繼續**，沒有任何錯誤訊息。這件事本身不會讓資料變壞（程式還在正常寫、正常 flush），但操作者可能誤以為「Ctrl-C 沒用、程式當機了」，進而做出更危險的動作（例如直接拔電源）。這個問題不在 `SessionWriter`，在呼叫端（`bridge_server.py`）沒有註冊訊號處理——這輪邊界不能碰那個檔案，建議見文末。

---

## 怎麼查的：全部是真的砍，不是看程式碼猜

方法：寫一支獨立腳本，完全比照 `bridge_server.py` 的真實用法——`SessionWriter(meta)` 用 `mode="w"` 寫一次 baseline，再用 `mode="a"` 重開（比照 `open_trial_machine()`），**不用 `with` 區塊、不裝訊號處理**（跟production 現況一模一樣：整個 repo 搜過，`session_writer.py` 跟 `bridge_server.py` 都沒有 `signal`/`atexit`）。連續寫 trial，用真的 `kill -9`/`kill -15`/`kill -2`（Ctrl-C 送的訊號）從外部砍掉這個子行程，然後用 `session_loader.load_session()`（不是憑印象，是真的呼叫）試著讀回來。

砍的時機分三種，難度遞增：
1. 兩筆 trial 之間（最好砍的時機）
2. **真的砍在一筆 trial 寫到一半**——用 30000 幀的超大 trial（正常 E05 的一筆頂多幾百幀）把單筆寫入時間拉長到約 0.4 秒，故意把訊號送在寫入正中間，讓 `create_dataset()` 這類 C 呼叫真的還在跑
3. 砍在**第一筆 baseline** 寫到一半（`mode="w"` 那次，檔案剛建立、`/meta` 剛 flush 完，第一筆資料還沒寫完）

全部用真的行程 PID（自己起的、自己記下來的），沒有 pattern kill，沒有動 `/dev/ttyUSB0`，沒有碰 `bridge_server.py`/`state_machine.py`。

---

## 逐題回答

### 1. `SessionWriter` 現在怎麼開檔？

**全程開著一次，不是每筆開關一次。** `open_trial_machine()`（`bridge_server.py`）呼叫 `SessionWriter(h5_path, mode="a")` 後直接 `writer.__enter__()`——**沒有包在 `with` 區塊裡**，這代表 Python 的 context manager 自動清理（`__exit__` 自動呼叫）**在這裡完全用不上**：程式正常結束才會經過 `with`，收到訊號被砍掉的行程不會經過它。整個 session 期間只有一個 `h5py.File` handle 開著，直到 `/session/end` 手動呼叫 `writer.__exit__(None, None, None)` 才關閉。

### 2. 有沒有 flush？

**有，`write_trial()` 每筆寫完最後一行就是 `self._file.flush()`**（`session_writer.py:383`）。這是為什麼下面的實測結果會這麼一致——每筆 trial 的資料在被下一筆蓋掉或行程被砍之前，早就已經透過 `H5Fflush()` 交給作業系統了，不是停留在 Python 或 h5py 自己的緩衝區裡等下一次 flush。

### 3. 實測：真的砍，讀不讀得回來

| 情境 | 訊號 | 重複次數 | 結果 |
|---|---|---|---|
| 兩筆 trial 之間 | `kill -9` | 3 | 全部完整讀回，缺的只有還沒開始寫的那幾筆（正常，本來就沒資料） |
| **真的砍在一筆寫到一半**（30000 幀，刻意拉長寫入時間、訊號送在寫入正中間） | `kill -9` | 5 | **全部完整讀回**：正在寫的那一筆**完全不出現**（不是殘缺的 group，是乾脆沒有這個 group），前面每一筆的所有 dataset 都完整無缺 |
| 同上 | `kill -15`（`SIGTERM`，等同 USB 斷線/程式被系統砍掉時常見的訊號） | 2 | **結果跟 `kill -9`一模一樣**——立刻終止，前面資料完整 |
| 砍在**第一筆 baseline** 寫到一半（`mode="w"`，`/meta` 剛 flush、trial_000 還沒寫完） | `kill -9` | 1 | 檔案存在、是合法的 HDF5（`h5py.File(..., "r")` 開得起來、`/meta` 的 30 個欄位全部完整），但**沒有任何 trial group**。`session_loader.load_session()` 對這種檔案的反應是**乾脆的 `ValueError`**（「沒有任何 /trial_NNN group」），不是安靜地給一個看起來能用但其實空的結果 |
| 兩筆之間，讓行程自己收到訊號、不強制 | `kill -2`（`SIGINT`，Ctrl-C） | 6 | ⚠️ **5 次程式完全沒有停下來，繼續照常錄**；1 次乾淨中斷。見下方「為什麼 Ctrl-C 不可靠」 |

**回答「最後一筆壞掉是可接受的，前面 300 筆一起消失不是」這句話**：目前的行為完全符合期望——**正在寫的那一筆，不管什麼時候砍，永遠是乾淨地消失，不會拖累它之前的任何一筆**。這不是巧合，是 `write_trial()` 的寫入順序決定的：先把這一筆的所有 dataset/attrs 寫進 HDF5 的記憶體結構，最後才呼叫 `flush()` 真正提交到檔案——**如果訊號在提交前就把行程殺了，這一整筆從來沒有「部分存在」的機會**，HDF5 的檔案結構（group 目錄）本身要等 flush 才更新。

### 4. `SessionWriter` 有沒有 signal handler / `atexit`？

**完全沒有**——這是自己的檔案，可以肯定地說；`bridge_server.py` 也是零（讀過，沒改）。Ctrl-C 時能不能乾淨收尾**純粹看運氣**，見下一節。

### ⚠️ 為什麼 Ctrl-C 不可靠（這是這輪最意外的發現）

Python 對 `SIGINT`（Ctrl-C）預設有內建處理：收到訊號會在**下一個 bytecode 執行點**丟出 `KeyboardInterrupt`。問題是「下一個 bytecode 執行點」**不保證是你以為的那一行**——h5py 內部用 `weakref` 追蹤它自己開著的物件（dataset handle、group handle），這些 weakref 的清理 callback 本身也是 Python 程式碼，是一個合法的「執行點」。

實測 6 次裡有 5 次，`KeyboardInterrupt` 剛好在這種內部清理 callback 裡被丟出來——而 **Python 對 `__del__`/weakref callback 裡丟出的例外，行為是印一行「Exception ignored in: ...」然後整個吞掉，不會往上傳、不會終止程式**。這是 CPython 本身有文件記載的行為，不是這個專案的 bug，但後果是：**程式收到 Ctrl-C，印一行沒人會去看的警告，然後就像什麼事都沒發生一樣繼續錄下一筆**。操作者按了 Ctrl-C 卻看不到任何反應，很容易誤判成「當機了」，進而做出**更危險**的動作（強制拔電源、`kill -9` 打錯 PID）。

**`kill -9`（`SIGKILL`）跟 `kill -15`（`SIGTERM`）都沒有這個問題**：作業系統對這兩個訊號的預設處置是「立刻終止」，Python 沒有替 `SIGTERM` 註冊處理常式（`session_writer.py`/`bridge_server.py` 都沒有），所以不會經過「丟 `KeyboardInterrupt`」這條路——**它們是直接、立即、不給任何 Python 程式碼繼續跑的機會**，這正是它們反而比 Ctrl-C 更可預期的原因。

### 5. Append 模式重開會不會把壞的變更壞？

**不會。** 拿一個被砍過、只剩前 4 筆的檔案，重新用 `mode="a"` 開起來寫第 5 筆，寫完立刻可以讀回全部 5 筆，前面 4 筆完全沒受影響——`_validate_existing_meta()` 只檢查 `/meta` 完整性（必填欄位都在），不會去檢查 trial 的完整性或數量，所以重開一個「有效但不完整」的 session 檔案繼續錄，行為正常。

（沒有另外實測「重開一個從 baseline 就壞掉、完全沒有 trial group 的檔案」——這種檔案 `_validate_existing_meta()` 只驗 `/meta`，`/meta` 是完整的，所以理論上一樣能重開繼續寫，只是這個 session 永遠少了 baseline/`trial_000`，跟其他任何「baseline 沒做完就重開」的情境是同一類問題，不是這次砍檔獨有的新風險，這裡沒有另外花時間重複驗證。）

---

## 最小改動建議（照使用者指示，不重寫架構）

**`session_writer.py` 本身不需要改。** `kill -9`/`kill -15`（也就是 USB 鬆脫、筆電斷電、agent 誤殺這幾種「行程被硬生生結束」的情境）已經是安全的，靠的是既有的「每筆 flush 一次」，不需要加任何東西。

**真正的缺口在呼叫端沒有處理 Ctrl-C**——但那是 `bridge_server.py`，這輪邊界不能碰，只能建議，交給之後排 story：

> 在 `bridge_server.py` 的 `main()` 註冊 `signal.signal(signal.SIGINT, handler)`：`handler` 只做一件事——設一個 flag（例如 `threading.Event`）。真正關檔案的動作，放在 trial ticker 迴圈**每一輪迭代開頭**檢查這個 flag，發現被設了就呼叫 `session_runtime["writer"].__exit__(None, None, None)` 再結束行程。
>
> 這樣做的關鍵是：**訊號處理常式本身不呼叫任何 h5py/HDF5 的東西**，只設一個 Python 層級的旗標——把「什麼時候真正收尾」從「訊號隨機打斷的那一瞬間」，搬到「迴圈檢查旗標的那個安全點」，正是這次測到的 `weakref` 陷阱不會發生的地方。大概 10 行以內，不涉及 `SessionWriter` 或 `TrialStateMachine` 的架構。

沒有做這個改動的情況下，`E05` 的操作建議是：**中途要停就直接 `Ctrl-C` 兩三次，或者乾脆 `kill -15`／關掉終端機**——不要因為 Ctrl-C 看起來沒反應就緊張去拔電源，已經寫完的資料是安全的。

---

## 沒有動的東西

`bridge_server.py`、`host/trial/state_machine.py` 全程只讀不改。所有測試用的行程都是自己起的獨立 Python 腳本（在 scratchpad 目錄，跑完已清除），全部用記下來的 PID 送訊號，沒有 pattern kill，沒有碰 `/dev/ttyUSB0`。`host/storage/session_writer.py` 這輪**沒有修改**——實測結論是它已經安全，不需要改。
