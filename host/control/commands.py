"""B18 — 主機 → 裝置的執行期控制指令（CONTRACTS.md §1.2，唯一事實來源）。

`SENS:<A|B>=<0|1>` / `MEL:<0|1>` / `AMB:<0|1>` 都是**執行期指令，立即
生效**：寫進序列埠、裝置照做、重發一次 `$STATUS`，就結束了——跟
`_handle_record()` 已經示範的模式（`serial_write_lock` + `ser.write()`）
是同一種操作，沒有中間狀態。

**解析度變更完全不是這一類。** ULD 驅動的 grid size 不能執行期切換，
是「改原始碼、重編、重燒」，中斷資料流數十秒——那是 `host/control/resolution.py`
的事，不要跟這裡的三個指令混在一起想。

**指令字串只在這裡組一次。** `mock_device.py`／`bridge_server.py`／測試
都應該呼叫這裡的函式，不要自己再拼一次字串——`$F` 頻率沒有從 `$STATUS`
廣播的常數推導、mock 對自己說謊，就是同類問題的前車之鑑。
"""
from __future__ import annotations

VALID_SENSORS = ("A", "B")


def sens_command(sensor: str, on: bool) -> str:
    """`SENS:<A|B>=<0|1>`。裝置端（`A08`）收到 `0` 是真的呼叫
    `stop_ranging()`，不只是跳過輸出——這裡不重複那個語意，只負責組字串。
    """
    if sensor not in VALID_SENSORS:
        raise ValueError(f"sensor 必須是 {VALID_SENSORS} 之一，收到 {sensor!r}")
    return f"SENS:{sensor}={1 if on else 0}"


def mel_command(on: bool) -> str:
    """`MEL:<0|1>`。開啟會讓頻寬多 15.6 KB/s（§1.4），由呼叫端自行評估，
    這裡不做頻寬檢查——組字串跟決策是兩件事。"""
    return f"MEL:{1 if on else 0}"


def amb_command(on: bool) -> str:
    """`AMB:<0|1>`（`A16`）。ambient 幀預設關閉、開啟頻寬代價 < 2%
    （`A16` 驗收條件），跟 `SENS`/`MEL` 是同一類「執行期、立即生效」指令。
    """
    return f"AMB:{1 if on else 0}"
