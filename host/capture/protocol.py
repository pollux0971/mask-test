"""協定 v2 行解析（`CONTRACTS.md` 第 1 章，FROZEN 2026-08-26）。

這裡只放**純函式 + 一個沒有 IO 的狀態機**，故意不 import `serial`、
不碰 HTTP、也不 import `bridge_server`。理由有二：

1. `bridge_server.py` 目前把 serial IO、解析、HTTP 混在同一個檔案裡，
   解析邏輯沒有辦法單獨測試。拆成獨立模組之後 `pytest` 就跑得動。
2. B 軌後續的 story（`B03` 掉幀偵測、`B06` 時間對齊、`B07` HDF5 寫入）
   都要吃同一份解析結果，讓它們 import 這個模組，不要各自再寫一份。

兩層 API：

* `parse_line(line)` —— 無狀態、不拋例外。看得懂就回一個 dict，
  看不懂就回 `None`。
* `ProtocolParser` —— 有狀態：版本協商、目前解析度、畸形行計數，
  並把計數掛到 `$H` 心跳事件上帶給前端。

`B02` 之後也支援舊的協定 v1（`$TOF`/`$MIC`/`$STATUS,res=`），但**必須明確
opt-in**：`ProtocolParser(allow_v1=True)`。預設仍然是 §1.1 規定的嚴格拒絕
——降級不能是預設行為，否則「靜默地用錯協定跑完一整個 session」正是
版本協商要防的事。v1 事件會標明 `proto=1`、`t_us=None`、
`has_timestamp=False`，並帶 `warning` 讓 panel 顯示。

**不包含**（是別的 story 的事，不要在這裡做）：掉幀偵測（`B03`）、
時間對齊（`B06`）。本模組只負責「一行文字 → 結構化事件」，並且忠實地把
`seq` 原封不動暴露出去讓 `B03` 用。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

# 主機認得的協定版本。`$STATUS` 的 `proto=` 不等於這個值就是韌體版本不符，
# 依 §1.1「版本協商」必須停止解析所有 `$` 資料行，不做向下相容。
PROTO_VERSION = 2

# `B02`：主機**有能力**解析的版本。v1 不是「相容」而是「明確標示的降級模式」，
# 只有 `ProtocolParser(allow_v1=True)` 才會啟用。
SUPPORTED_PROTOCOLS = (1, 2)

# v1 沒有 `t_us`。用 v1 錄一整個 session 才發現沒有時間戳、資料不能做時間
# 對齊（`B06`）與驗證分析，是這個 story 要防的具體災難，所以警告文字寫死在
# 這裡當單一事實來源，panel 直接顯示 `status` 事件的 `warning` 欄位即可。
V1_WARNING = "協定 v1 — 無時間戳，資料不可用於驗證分析"

# §1.3 行長上限：8×8 + signal ≈ 780 bytes，所以 readline buffer 至少 1024。
# 這個常數是給 serial reader 設 buffer 用的，本模組自己不做 IO。
READLINE_BUFFER_MIN = 1024

# 超過這個長度就當畸形丟掉。正常最長的 `$T`（64 zones）約 780 bytes，
# 2048 已經是它的兩倍多——會超過只可能是兩行黏在一起或 UART 噴垃圾，
# 與其硬解出一個錯的幀，不如丟掉並計數。
MAX_LINE_LEN = 2048

# `$F` 固定 40 個係數（§1.1 / §3.1 n_mels=40）。
N_MELS = 40

# 裝置端傳輸 int16 = round(log_mel * 100)（§3.1），主機還原時除回去。
MEL_SCALE = 100.0

# §1.1 `$T` 的 `dim` 欄位是 **zone 數**（4×4=16、8×8=64），不是邊長；
# `$STATUS` 的 `res=` 才是邊長（4|8）。兩者容易混淆，這裡分開處理。
VALID_ZONE_COUNTS = (16, 64)

U32_MAX = 4294967295
I16_MAX = 32767
I16_MIN = -32768
I8_MIN, I8_MAX = -128, 127

# 保留最近幾筆畸形行原文，方便人工判斷是 UART 位元錯誤還是格式真的變了。
MALFORMED_SAMPLE_LIMIT = 20


def _to_int(text: str) -> int | None:
    """`int(text)` 但不拋例外。順手拒絕 `int()` 會接受但協定不允許的寫法
    （空字串、`+5`、`0x10`、`1_000`、前後空白以外的雜訊）。"""
    text = text.strip()
    if not text:
        return None
    body = text[1:] if text[0] == "-" else text
    if not body.isdigit():
        return None
    try:
        return int(text)
    except ValueError:  # 理論上到不了，isdigit 已擋掉；保險
        return None


def _u32(text: str) -> int | None:
    v = _to_int(text)
    if v is None or not (0 <= v <= U32_MAX):
        return None
    return v


def _nonneg_i64(text: str) -> int | None:
    v = _to_int(text)
    if v is None or v < 0:
        return None
    return v


def _i16(text: str) -> int | None:
    v = _to_int(text)
    if v is None or not (I16_MIN <= v <= I16_MAX):
        return None
    return v


def _decode(line: str | bytes | bytearray) -> str | None:
    """接受 `bytes`（serial 直出）或 `str`。壞掉的 UTF-8 不拋例外，
    換成 U+FFFD——後面的整數解析自然會失敗，那行就被當畸形丟掉。"""
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8", errors="replace")
    if not isinstance(line, str):
        return None
    # UART 噪訊常見 NUL；strip 掉行尾的 \r\n 與空白。
    return line.replace("\x00", "�").strip()


def _parse_tof(parts: list[str]) -> dict | None:
    """`$T,<A|B>,<seq>,<t_us>,<dim>,<d0>..<dN>,<s0>..<sN>`

    長度不寫死，用行內自帶的 `dim` 推：`d` 與 `s` 各 `dim` 個。
    """
    if len(parts) < 5:
        return None
    sensor = parts[1].strip()
    if sensor not in ("A", "B"):
        return None
    seq = _u32(parts[2])
    t_us = _nonneg_i64(parts[3])
    dim = _to_int(parts[4])
    if seq is None or t_us is None or dim not in VALID_ZONE_COUNTS:
        return None

    values = parts[5:]
    if len(values) != 2 * dim:
        return None  # 被截斷、或兩行黏在一起

    nums = [_i16(v) for v in values]
    if any(n is None for n in nums):
        return None

    raw_d = nums[:dim]
    raw_s = nums[dim:]
    distance, signal, valid, pair_violations = _pair_zones(raw_d, raw_s)

    return {
        "type": "tof",
        "proto": 2,
        "sensor": sensor,
        "seq": seq,
        "t_us": t_us,
        "has_timestamp": True,
        "dim": dim,
        "distance": distance,          # 無效 zone 為 None
        "signal": signal,              # 與 distance 一一對應，同進同出
        "signal_present": True,
        "valid": valid,
        "n_valid": sum(valid),
        "raw_distance": raw_d,         # 原始值（含 -1），給要自己判斷的下游
        "raw_signal": raw_s,
        "pair_violations": pair_violations,
    }


def _pair_zones(raw_d, raw_s):
    """把一幀的距離與 signal 配成對，回 `(distance, signal, valid, violations)`。

    §1.1「無效值語意」：`target_status ∉ {5,9}` 時 d 與 s **一律同時** 回 -1。
    所以主機這邊也把它們當同一組缺值一起丟，不可以只判斷其中一個。
    若只有一邊是 -1，那是裝置端違反契約或 UART 位元錯誤——保守起見整個
    zone 當無效（寧可少一個 zone，也不要餵一個半真半假的樣本給下游），
    並在 `pair_violations` 記一筆讓人看得到發生率。

    `raw_s` 傳 `None` 代表這個版本的線上格式根本沒有 signal 欄位
    （協定 v1 的 `$TOF` 只送距離），此時只看距離判斷有效性——**不是**
    把 signal 當成 0，那會捏造一個裝置從來沒送過的數字。
    """
    distance: list[int | None] = []
    signal: list[int | None] = []
    valid: list[bool] = []
    pair_violations = 0

    if raw_s is None:
        for d in raw_d:
            ok = d >= 0
            distance.append(d if ok else None)
            signal.append(None)
            valid.append(ok)
        return distance, signal, valid, 0

    for d, s in zip(raw_d, raw_s):
        d_bad = d < 0
        s_bad = s < 0
        if d_bad != s_bad:
            pair_violations += 1
        if d_bad or s_bad:
            distance.append(None)
            signal.append(None)
            valid.append(False)
        else:
            distance.append(d)
            signal.append(s)
            valid.append(True)
    return distance, signal, valid, pair_violations


def _parse_mic(parts: list[str]) -> dict | None:
    """`$M,<seq>,<t_us>,<rms:i16>,<peak:i16>`

    注意 `rms` 是 **i16 定點**（16-bit PCM 原始振幅），不是草案的浮點 `f1`
    ——見 CONTRACTS 變更紀錄的破壞性變更那一行。
    """
    if len(parts) != 5:
        return None
    seq = _u32(parts[1])
    t_us = _nonneg_i64(parts[2])
    rms = _i16(parts[3])
    peak = _i16(parts[4])
    if seq is None or t_us is None or rms is None or peak is None:
        return None
    return {
        "type": "mic",
        "proto": 2,
        "seq": seq,
        "t_us": t_us,
        "has_timestamp": True,
        "rms": rms,
        "peak": peak,
    }


def _parse_mel(parts: list[str]) -> dict | None:
    """`$F,<seq>,<t_us>,<m0>..<m39>`（固定 40 個係數）。"""
    if len(parts) != 3 + N_MELS:
        return None
    seq = _u32(parts[1])
    t_us = _nonneg_i64(parts[2])
    if seq is None or t_us is None:
        return None
    coeffs = [_i16(v) for v in parts[3:]]
    if any(c is None for c in coeffs):
        return None
    return {
        "type": "mel",
        "proto": 2,
        "seq": seq,
        "t_us": t_us,
        "has_timestamp": True,
        "mel_q": coeffs,                                   # 線上原始 int16
        "log_mel": [c / MEL_SCALE for c in coeffs],        # §3.1 還原
    }


def _parse_heartbeat(parts: list[str]) -> dict | None:
    """`$H,<t_us>,<drop_A>,<drop_B>,<drop_M>,<heap>,<temp_c:i8>`"""
    if len(parts) != 7:
        return None
    t_us = _nonneg_i64(parts[1])
    drops = [_u32(p) for p in parts[2:5]]
    heap = _u32(parts[5])
    temp_c = _to_int(parts[6])
    if t_us is None or any(d is None for d in drops) or heap is None:
        return None
    if temp_c is None or not (I8_MIN <= temp_c <= I8_MAX):
        return None
    return {
        "type": "heartbeat",
        "proto": 2,
        "t_us": t_us,
        "has_timestamp": True,
        "drop_A": drops[0],
        "drop_B": drops[1],
        "drop_M": drops[2],
        "heap": heap,
        "temp_c": temp_c,
    }


def _parse_status(parts: list[str]) -> dict | None:
    """`$STATUS,res=<dim>,proto=2,fw=<sha>,sr=..,mel=..,mel_win=..,mel_hop=..,mic_hop=..`

    §1.1.2 的硬性規定：**一律以 key=value 解析、順序無關、未知欄位忽略，
    不可用固定位置切分**——日後還會再加欄位。

    缺 `proto=` 也照樣回一個事件（`proto` 為 `None`），因為「沒有 proto
    欄位」正是舊韌體的樣子，版本協商需要看到它才能報「韌體版本不符」，
    不能在這裡就當畸形丟掉。

    §1.1.2 的五個音框參數同理：**缺欄位一律 `None`，不填預設值**。
    下游必須分得出「韌體沒說」與「韌體說了是 512」——填預設值會讓舊韌體
    看起來像新韌體，`B06` 就會用錯的幀間距去對齊。
    """
    fields: dict[str, str] = {}
    for token in parts[1:]:
        key, sep, value = token.partition("=")
        if not sep:
            return None
        fields[key.strip()] = value.strip()
    if "res" not in fields:
        return None

    res = _to_int(fields["res"])
    if res not in (4, 8):
        return None
    proto = _to_int(fields["proto"]) if "proto" in fields else None

    return {
        "type": "status",
        "res": res,                 # 邊長 4|8
        "dim": res * res,           # zone 數 16|64，對應 `$T` 的 dim 欄位
        "proto": proto,             # 舊韌體沒有這欄 → None
        "fw": fields.get("fw"),
        "compatible": proto == PROTO_VERSION,
        # --- §1.1.2 音框參數（韌體自我描述）。沒送就是 None。
        "sr": _opt_uint(fields, "sr"),
        "mel": _opt_bool(fields, "mel"),
        "mel_win": _opt_uint(fields, "mel_win"),
        "mel_hop": _opt_uint(fields, "mel_hop"),
        "mic_hop": _opt_uint(fields, "mic_hop"),
        # 原封不動的 key=value，讓下游看得到韌體到底送了什麼（含日後新增、
        # 本模組還不認得的欄位）。§1.1.2 規定未知欄位忽略，但「忽略」是指
        # 不因此報錯，不是丟掉。
        "fields": fields,
    }


def _opt_uint(fields: dict, key: str) -> int | None:
    """選用的非負整數欄位。沒送 → `None`。送了但不合法 → 也是 `None`
    （不讓一個壞掉的選用欄位害整行 `$STATUS` 解不出來——那行還扛著版本
    協商，比任何一個參數都重要）。"""
    if key not in fields:
        return None
    value = _to_int(fields[key])
    return value if value is not None and value >= 0 else None


def _opt_bool(fields: dict, key: str) -> bool | None:
    """選用的 `0|1` 旗標。沒送 → `None`（不是 `False`）。"""
    value = _opt_uint(fields, key)
    if value not in (0, 1):
        return None
    return bool(value)


def _parse_record(parts: list[str]) -> dict | None:
    """`$REC,start,<seconds>`。不是 B01 的四種資料行，但它是 `$` 開頭，
    不認得就會被算成畸形行、污染錯誤率統計，所以這裡要認得它。"""
    if len(parts) != 3 or parts[1].strip() != "start":
        return None
    seconds = _to_int(parts[2])
    if seconds is None or seconds < 0:
        return None
    # `$REC` 在 v1／v2 格式相同，`proto` 由呼叫端補（`parse_line_v1` 會蓋成 1）。
    return {"type": "record", "proto": 2, "state": "recording", "seconds": seconds}


_HANDLERS = {
    "$T": _parse_tof,
    "$M": _parse_mic,
    "$F": _parse_mel,
    "$H": _parse_heartbeat,
    "$STATUS": _parse_status,
    "$REC": _parse_record,
}

# 版本協商用的控制行。版本不符時只有它還會被解析——其餘所有 `$` 開頭的行
# （含舊協定的 `$TOF`/`$MIC`）一律停掉，見 §1.1。
STATUS_PREFIX = "$STATUS"


# ------------------------------------------------------------------ 協定 v1
#
# v1 是 `T01` 凍結之前的舊格式，**沒有 `seq`，也沒有 `t_us`**：
#
#     $TOF,<A|B>,<dim>,<d0>..<dN>[,<s0>..<sN>]
#     $MIC,<rms:float>,<peak:int>
#     $STATUS,res=<side>
#
# 沒有 `$H`、沒有 `$F`。`$REC` / `BEGIN_WAV_B64` 與 v2 相同。
#
# `$TOF` 有兩種方言，兩種都要吃（差異來源見下）：
#
#   * 真實舊韌體（`fb286d1:vl53l7cx_test/main/vl53l7cx_test.c:135`）送
#     `$TOF,A,<side>,<side²個距離>` —— dim 欄位是**邊長**，且**只有距離、
#     沒有 signal**。
#   * `ssi-backlog/tools/mock_device.py --proto v1` 送
#     `$TOF,A,<zone數>,<距離><signal>` —— dim 欄位是**zone 數**，且**有
#     signal**。
#
# 兩者可以無歧義地分辨：dim 欄位落在 {4,8} 就是邊長、落在 {16,64} 就是
# zone 數（4≠16、8≠64，不會撞）；值的個數等於 zone 數就是只有距離、
# 等於兩倍就是距離＋signal。所以這裡照「行自己說了什麼」解析，不猜。

V1_TOF_SIDES = (4, 8)

# v1 專屬與 v2 專屬的行前綴。`$STATUS` / `$REC` 兩版格式相同，是共用的。
V1_ONLY_PREFIXES = frozenset({"$TOF", "$MIC"})
V2_ONLY_PREFIXES = frozenset({"$T", "$M", "$F", "$H"})
SHARED_PREFIXES = frozenset({"$STATUS", "$REC"})


def _parse_tof_v1(parts: list[str]) -> dict | None:
    """`$TOF,<A|B>,<dim>,<d0>..<dN>[,<s0>..<sN>]`"""
    if len(parts) < 4:
        return None
    sensor = parts[1].strip()
    if sensor not in ("A", "B"):
        return None
    dim_field = _to_int(parts[2])
    if dim_field is None:
        return None
    if dim_field in V1_TOF_SIDES:
        zones = dim_field * dim_field       # 真實舊韌體：dim 欄位是邊長
    elif dim_field in VALID_ZONE_COUNTS:
        zones = dim_field                   # mock v1：dim 欄位是 zone 數
    else:
        return None

    values = parts[3:]
    if len(values) == zones:
        has_signal = False
    elif len(values) == 2 * zones:
        has_signal = True
    else:
        return None

    nums = [_i16(v) for v in values]
    if any(n is None for n in nums):
        return None

    raw_d = nums[:zones]
    raw_s = nums[zones:] if has_signal else None
    distance, signal, valid, pair_violations = _pair_zones(raw_d, raw_s)

    return {
        "type": "tof",
        "proto": 1,
        "sensor": sensor,
        # v1 線上就沒有這兩個欄位。**不捏造**——填 None 讓下游（`B03` 掉幀
        # 偵測、`B06` 時間對齊、`B07` HDF5 寫入）一眼看出資料不完整。
        "seq": None,
        "t_us": None,
        "has_timestamp": False,
        "dim": zones,
        "distance": distance,
        "signal": signal,
        "signal_present": has_signal,
        "valid": valid,
        "n_valid": sum(valid),
        "raw_distance": raw_d,
        "raw_signal": raw_s,
        "pair_violations": pair_violations,
    }


def _parse_mic_v1(parts: list[str]) -> dict | None:
    """`$MIC,<rms:float>,<peak:int>`

    v1 的 `rms` 是浮點文字（舊韌體 `printf("$MIC,%.1f,%d")`）；v2 已改成
    i16 定點。這裡照實回浮點並標 `proto=1`，**不四捨五入成 i16 假裝是 v2**
    ——兩個版本的數值單位一樣（16-bit PCM 振幅），但精度來源不同，混在一起
    之後就分不出哪些樣本是估的。
    """
    if len(parts) != 3:
        return None
    try:
        rms = float(parts[1].strip())
    except ValueError:
        return None
    if rms != rms or rms in (float("inf"), float("-inf")):   # NaN / inf
        return None
    peak = _to_int(parts[2])
    if peak is None or not (I16_MIN <= peak <= I16_MAX):
        return None
    return {
        "type": "mic",
        "proto": 1,
        "seq": None,
        "t_us": None,
        "has_timestamp": False,
        "rms": rms,             # v1 是 float，v2 是 int，用 proto 分辨
        "peak": peak,
    }


_HANDLERS_V1 = {
    "$TOF": _parse_tof_v1,
    "$MIC": _parse_mic_v1,
    "$STATUS": _parse_status,   # 兩版格式相同，version-agnostic
    "$REC": _parse_record,
}


def parse_line(line: str | bytes | bytearray) -> dict | None:
    """把一行協定 v2 文字解析成 dict。**永遠不拋例外。**

    回傳：
      * `dict` —— 認得且欄位合法。`type` 是
        `tof` / `mic` / `mel` / `heartbeat` / `status` / `record`。
      * `None` —— 不是 `$` 開頭（裝置的 log、base64 payload、空行），
        或是 `$` 開頭但畸形（欄位數不對、整數解析失敗、被截斷、亂碼）。

    畸形與「不是協定行」都回 `None` 是刻意的（story 驗收條件如此規定）。
    要把兩者分開計數請用 `ProtocolParser`，它靠「以 `$` 開頭但這裡回 None」
    來判定畸形。
    """
    return _parse_with(_HANDLERS, line)


def parse_line_v1(line: str | bytes | bytearray) -> dict | None:
    """協定 v1 版的 `parse_line()`：認得 `$TOF` / `$MIC` / `$STATUS` / `$REC`。

    回傳的事件形狀與 v2 一致（同樣有 `type` / `distance` / `valid` …），
    差別在 `proto` 是 `1`、`seq` 與 `t_us` 是 `None`、`has_timestamp` 是
    `False`。下游程式碼因此不必寫兩套，但也不會誤以為 v1 有時間戳。
    """
    event = _parse_with(_HANDLERS_V1, line)
    if event is not None and event["type"] != "status":
        event["proto"] = 1
    # `$STATUS` 刻意不蓋：它的 `proto` 欄位代表「裝置自己回報的版本」，
    # 舊韌體根本沒送這個欄位所以是 `None`。蓋成 1 會變成「裝置說它是 v1」，
    # 那是捏造——是**主機推論**出來的，不是裝置講的。推論結果由
    # `ProtocolParser` 放在 `proto_detected`。
    return event


# story B02 的實作草稿寫的就是這張表；`$STATUS` 兩版共用一個
# version-agnostic 的解析器（它是唯一兩版格式相同的行）。
PARSERS = {1: parse_line_v1, 2: parse_line}


def _parse_with(handlers: dict, line) -> dict | None:
    text = _decode(line)
    if not text or not text.startswith("$"):
        return None
    if len(text) > MAX_LINE_LEN:
        return None

    parts = text.split(",")
    handler = handlers.get(parts[0].strip())
    if handler is None:
        return None
    try:
        return handler(parts)
    except Exception:
        # 這裡不該發生，但解析器掛掉會斷掉整條資料流，寧可多這一層。
        return None


@dataclass
class ParserStats:
    """`ProtocolParser` 的累計計數。UART 在 460800 baud 下偶發位元錯誤是
    常態，重點不是「有沒有」而是「發生率多少」，所以每一項都要看得到。"""

    lines: int = 0                    # 餵進來的總行數
    parsed: int = 0                   # 成功解析成事件的行數
    malformed: int = 0                # `$` 開頭但解不出來
    ignored: int = 0                  # 不是 `$` 開頭（log / base64 / 空行）
    dropped_version_mismatch: int = 0  # 版本不符期間被丟掉的 `$` 資料行
    pair_violations: int = 0          # d/s 只有一邊是 -1（違反 §1.1）
    resolution_changes: int = 0       # `$T` 的 dim 與上一次不同
    v1_lines: int = 0                 # 用 v1 解析器解成功的行（`B02` 降級模式）
    v2_lines: int = 0                 # 用 v2 解析器解成功的行
    malformed_by_prefix: dict = field(default_factory=dict)

    @property
    def malformed_rate(self) -> float:
        """畸形行 ÷ 所有 `$` 開頭的行。分母排除裝置 log，才是真的錯誤率。"""
        protocol_lines = self.parsed + self.malformed + self.dropped_version_mismatch
        return self.malformed / protocol_lines if protocol_lines else 0.0

    def as_dict(self) -> dict:
        return {
            "lines": self.lines,
            "parsed": self.parsed,
            "malformed": self.malformed,
            "ignored": self.ignored,
            "dropped_version_mismatch": self.dropped_version_mismatch,
            "pair_violations": self.pair_violations,
            "resolution_changes": self.resolution_changes,
            "v1_lines": self.v1_lines,
            "v2_lines": self.v2_lines,
            "malformed_by_prefix": dict(self.malformed_by_prefix),
            "malformed_rate": round(self.malformed_rate, 6),
        }


class ProtocolParser:
    """有狀態的解析器：版本協商 + 目前解析度 + 畸形行計數。

    一個序列埠連線配一個實例。`feed()` 一次吃一行，回事件或 `None`。

    ## 版本協商（§1.1）

      * 還沒看到任何 `$STATUS` 之前，資料行照解（主機可能是中途接上去的，
        沒辦法先驗證；`proto_confirmed` 會是 `False` 讓呼叫端知道）。
      * 讀到第一行有效 `$STATUS` 就比對 `proto=`。不符 → 進入
        `version_mismatch`，**之後所有 `$` 行一律不解析**（`$STATUS` 除外）。
        不嘗試向下相容舊協定，這是契約明文規定。
      * 裝置重開機／改組態會重發 `$STATUS`，所以之後每一行 `$STATUS`
        都會重新評估——換上對的韌體可以直接恢復，不用重開主機。

    ## v1 降級模式（`B02`，預設關閉）

    `ProtocolParser(allow_v1=True)` 才會啟用。啟用後，偵測到 v1 韌體
    （`$STATUS` 沒有 `proto=`，或直接收到 `$TOF`/`$MIC` 行）時不再報錯，
    改走一條**明確標示的**降級路徑：

      * `protocol_version` 變成 `1`、`degraded` 為 `True`
      * `warning` 是 `V1_WARNING`，`status` 事件也會帶同一份文字給 panel
      * `recording_allowed` 是 `False`——v1 沒有 `t_us`，錄下來的 session
        不能做時間對齊也不能驗證，這個旗標就是給 panel 停用錄音鈕用的
      * 事件本身標 `proto=1` / `has_timestamp=False`，不混進 v2 的事件流

    §1.1 的嚴格語意沒有被推翻：**預設仍然是拒絕**，降級必須有人明確打開。
    """

    def __init__(self, allow_v1: bool = False) -> None:
        self.allow_v1 = allow_v1
        self.stats = ParserStats()
        self.status: dict | None = None        # 最後一次收到的 `$STATUS` 事件
        self.dim: int | None = None            # 目前 zone 數（16|64）
        self.version_mismatch = False
        self.mismatch_reason: str | None = None
        # 主機推論出來的對方版本（不是裝置自己講的，v1 韌體不會講）。
        # `None` = 還沒有任何證據。
        self.protocol_version: int | None = None
        self.malformed_samples: deque[str] = deque(maxlen=MALFORMED_SAMPLE_LIMIT)

    # ------------------------------------------------------------ 對外狀態

    @property
    def proto_confirmed(self) -> bool:
        """是否已經看過一行 `$STATUS` 且版本可用（v2，或允許的 v1）。"""
        return self.status is not None and not self.version_mismatch

    @property
    def degraded(self) -> bool:
        """是否正跑在 v1 降級模式。"""
        return self.protocol_version == 1 and not self.version_mismatch

    @property
    def warning(self) -> str | None:
        """要顯示給使用者看的警告（沒有就是 `None`）。panel 直接顯示它。"""
        if self.version_mismatch:
            return self.mismatch_reason
        if self.degraded:
            return V1_WARNING
        return None

    @property
    def recording_allowed(self) -> bool:
        """v1 沒有 `t_us`，錄下來的 session 不能做時間對齊也不能驗證，
        所以降級模式下錄音必須被停用（story B02 驗收條件）。版本不符時
        當然也不能錄。"""
        return not self.version_mismatch and self.protocol_version != 1

    def state(self) -> dict:
        """一份可以直接丟給 panel／SSE 的狀態快照。"""
        return {
            "protocol_version": self.protocol_version,
            "proto_confirmed": self.proto_confirmed,
            "degraded": self.degraded,
            "version_mismatch": self.version_mismatch,
            "warning": self.warning,
            "recording_allowed": self.recording_allowed,
            "allow_v1": self.allow_v1,
            "dim": self.dim,
            "fw": self.status.get("fw") if self.status else None,
            # §1.1.2 音框參數。`B06` 要靠 `mel_hop` 才知道 `$F` 是 31.25 Hz
            # 還是 62.5 Hz；`None` 代表韌體沒說，下游不可以自己假設。
            "sr": self._status_field("sr"),
            "mel": self._status_field("mel"),
            "mel_win": self._status_field("mel_win"),
            "mel_hop": self._status_field("mel_hop"),
            "mic_hop": self._status_field("mic_hop"),
            "stats": self.stats.as_dict(),
        }

    def _status_field(self, key: str):
        return self.status.get(key) if self.status else None

    # ------------------------------------------------------------ 主要入口

    def feed(self, line: str | bytes | bytearray) -> dict | None:
        """吃一行，回一個事件 dict 或 `None`。**永遠不拋例外**——錄音 dump
        期間頻寬吃到 92%，殘行、亂碼、被截斷的行是預期中的輸入，不能讓
        它們中斷整條資料流。"""
        self.stats.lines += 1
        text = _decode(line)
        if not text or not text.startswith("$"):
            self.stats.ignored += 1
            return None

        prefix = text.split(",", 1)[0].strip()

        # `$STATUS` 永遠要解析：它是唯一兩版格式相同的行，也是唯一能讓
        # 「版本不符」狀態恢復的行（換韌體、裝置重開機）。
        if prefix == STATUS_PREFIX:
            return self._feed_status(text)

        line_version = self._version_of(prefix)

        # 版本不符期間，其餘 `$` 行全丟。這裡刻意不只擋 v2 的那幾種前綴——
        # 舊韌體送的是 `$TOF`/`$MIC`，若讓它們掉進畸形計數，錯誤率會衝到
        # ~100%，`C04` 就會顯示成「連線異常」而不是「韌體版本不符」，
        # 把真正的原因蓋掉。
        if self.version_mismatch:
            self.stats.dropped_version_mismatch += 1
            return None

        # 沒開 v1 相容時收到 v1 專屬的行：這是**可以偵測到的**版本不符，
        # 不是畸形行。直接進入不符狀態並說清楚原因（v1 韌體開機只印一次
        # `$STATUS`，主機中途接上去的話永遠等不到那一行）。
        if line_version == 1 and not self.allow_v1:
            self.protocol_version = 1        # 認出來了，只是不接受
            self._enter_mismatch(
                f"韌體版本不符：收到協定 v1 的 {prefix} 行，主機支援 "
                f"proto={PROTO_VERSION}。已停止解析所有 $ 資料行；"
                f"若確定要用舊韌體，請改用 ProtocolParser(allow_v1=True)。"
            )
            self.stats.dropped_version_mismatch += 1
            return None

        if line_version == 1:
            self._note_version(1)
            parser = parse_line_v1
        elif line_version == 2:
            self._note_version(2)
            parser = parse_line
        else:
            # `$REC` 等共用行：跟著目前談定的版本走，還沒談定就當 v2。
            parser = PARSERS.get(self.protocol_version or PROTO_VERSION, parse_line)

        event = parser(text)
        if event is None:
            self._note_malformed(prefix, text)
            return None

        self.stats.parsed += 1
        if event.get("proto") == 1:
            self.stats.v1_lines += 1
        else:
            self.stats.v2_lines += 1

        if event["type"] == "tof":
            self._on_tof(event)
        elif event["type"] == "heartbeat":
            # story B01：畸形行不要靜默丟棄——計數並在 `$H` 事件裡帶出去。
            event["host"] = self.stats.as_dict()

        return event

    def feed_many(self, lines) -> list[dict]:
        """吃一整批行，回所有解得出來的事件（順序不變）。"""
        events = []
        for line in lines:
            event = self.feed(line)
            if event is not None:
                events.append(event)
        return events

    # ------------------------------------------------------------ 內部細節

    @staticmethod
    def _version_of(prefix: str) -> int | None:
        """這個前綴只屬於哪一版？共用行（`$REC`）回 `None`。"""
        if prefix in V1_ONLY_PREFIXES:
            return 1
        if prefix in V2_ONLY_PREFIXES:
            return 2
        return None

    def _note_malformed(self, prefix: str, text: str) -> None:
        self.stats.malformed += 1
        self.stats.malformed_by_prefix[prefix] = (
            self.stats.malformed_by_prefix.get(prefix, 0) + 1
        )
        self.malformed_samples.append(text[:200])

    def _note_version(self, version: int) -> None:
        """由資料行推論版本。只在還沒有 `$STATUS` 定論時才動——`$STATUS`
        是權威，資料行只是還沒收到它之前的線索。"""
        if self.status is None:
            self.protocol_version = version

    def _enter_mismatch(self, reason: str) -> None:
        self.version_mismatch = True
        self.mismatch_reason = reason

    def _feed_status(self, text: str) -> dict | None:
        event = parse_line(text)          # `$STATUS` 兩版格式相同
        if event is None:
            self._note_malformed(STATUS_PREFIX, text)
            return None
        self.stats.parsed += 1
        # `$STATUS` 兩版格式相同，不歸給任何一版，否則 `v1_lines`/`v2_lines`
        # 就不再是「這條線上跑的是哪一版的資料」的乾淨指標。
        self._on_status(event)
        return event

    def _on_status(self, event: dict) -> None:
        self.status = event
        self.dim = event["dim"]
        reported = event["proto"]

        if reported == PROTO_VERSION:
            self.protocol_version = 2
            self.version_mismatch = False
            self.mismatch_reason = None
        elif reported is None and self.allow_v1:
            # 沒有 `proto=` 欄位 = 凍結前的舊韌體。story B02：「無 `proto=`
            # 時預設 v1」。這是降級，不是相容——所以要標得很明顯。
            self.protocol_version = 1
            self.version_mismatch = False
            self.mismatch_reason = None
        else:
            # 「沒送 proto 欄位」在協定上只有一個可能：凍結前的舊韌體。
            # 即使不接受它，也要把推論結果講出來，`C04` 才能顯示「偵測到
            # 協定 v1 韌體」而不是「未知版本」。`degraded` 與
            # `recording_allowed` 仍然靠 `version_mismatch` 擋住。
            self.protocol_version = 1 if reported is None else reported
            got = (
                "無 proto 欄位（協定 v1 韌體）" if reported is None
                else f"proto={reported}"
            )
            extra = (
                "若確定要用舊韌體，請改用 ProtocolParser(allow_v1=True)。"
                if reported is None else ""
            )
            self._enter_mismatch(
                f"韌體版本不符：主機支援 proto={PROTO_VERSION}，裝置回報 {got}。"
                f"已停止解析所有 $ 資料行，請更新韌體。{extra}"
            )

        # 讓 panel 不必自己判斷——狀態事件把該顯示的東西都帶上。
        event["proto_detected"] = self.protocol_version
        event["degraded"] = self.degraded
        event["mismatch_reason"] = self.mismatch_reason
        event["warning"] = self.warning
        event["recording_allowed"] = self.recording_allowed

    def _on_tof(self, event: dict) -> None:
        self.stats.pair_violations += event["pair_violations"]
        if self.dim is not None and event["dim"] != self.dim:
            # 不算畸形：`$T` 自帶 dim，是自描述的。裝置改解析度後會補一行
            # `$STATUS`，這裡先跟上，順便記一筆讓人看得到切換次數。
            self.stats.resolution_changes += 1
        self.dim = event["dim"]
