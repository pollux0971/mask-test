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

**不包含**（是別的 story 的事，不要在這裡做）：v1 相容（`B02`）、
掉幀偵測（`B03`）、時間對齊（`B06`）。本模組只負責「一行文字 → 結構化
事件」，並且忠實地把 `seq` 原封不動暴露出去讓 `B03` 用。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

# 主機認得的協定版本。`$STATUS` 的 `proto=` 不等於這個值就是韌體版本不符，
# 依 §1.1「版本協商」必須停止解析所有 `$` 資料行，不做向下相容。
PROTO_VERSION = 2

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

    # §1.1「無效值語意」：`target_status ∉ {5,9}` 時 d 與 s **一律同時** 回 -1。
    # 所以主機這邊也把它們當同一組缺值一起丟，不可以只判斷其中一個。
    # 若只有一邊是 -1，那是裝置端違反契約或 UART 位元錯誤——保守起見整個
    # zone 當無效（寧可少一個 zone，也不要餵一個半真半假的樣本給下游），
    # 並在 `pair_violations` 記一筆讓人看得到發生率。
    distance: list[int | None] = []
    signal: list[int | None] = []
    valid: list[bool] = []
    pair_violations = 0
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

    return {
        "type": "tof",
        "sensor": sensor,
        "seq": seq,
        "t_us": t_us,
        "dim": dim,
        "distance": distance,          # 無效 zone 為 None
        "signal": signal,              # 與 distance 一一對應，同進同出
        "valid": valid,
        "n_valid": sum(valid),
        "raw_distance": raw_d,         # 原始值（含 -1），給要自己判斷的下游
        "raw_signal": raw_s,
        "pair_violations": pair_violations,
    }


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
    return {"type": "mic", "seq": seq, "t_us": t_us, "rms": rms, "peak": peak}


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
        "seq": seq,
        "t_us": t_us,
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
        "t_us": t_us,
        "drop_A": drops[0],
        "drop_B": drops[1],
        "drop_M": drops[2],
        "heap": heap,
        "temp_c": temp_c,
    }


def _parse_status(parts: list[str]) -> dict | None:
    """`$STATUS,res=<dim>,proto=2,fw=<git_sha>`

    key=value 的順序不假設固定；缺 `proto=` 也照樣回一個事件（`proto` 為
    `None`），因為「沒有 proto 欄位」正是舊韌體的樣子，版本協商需要看到它
    才能報「韌體版本不符」，不能在這裡就當畸形丟掉。
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
    }


def _parse_record(parts: list[str]) -> dict | None:
    """`$REC,start,<seconds>`。不是 B01 的四種資料行，但它是 `$` 開頭，
    不認得就會被算成畸形行、污染錯誤率統計，所以這裡要認得它。"""
    if len(parts) != 3 or parts[1].strip() != "start":
        return None
    seconds = _to_int(parts[2])
    if seconds is None or seconds < 0:
        return None
    return {"type": "record", "state": "recording", "seconds": seconds}


_HANDLERS = {
    "$T": _parse_tof,
    "$M": _parse_mic,
    "$F": _parse_mel,
    "$H": _parse_heartbeat,
    "$STATUS": _parse_status,
    "$REC": _parse_record,
}

# `$` 資料行（相對於 `$STATUS` 這種控制行）。版本不符時要停掉的就是這些。
DATA_PREFIXES = frozenset({"$T", "$M", "$F", "$H", "$REC"})


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
    text = _decode(line)
    if not text or not text.startswith("$"):
        return None
    if len(text) > MAX_LINE_LEN:
        return None

    parts = text.split(",")
    handler = _HANDLERS.get(parts[0].strip())
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
            "malformed_by_prefix": dict(self.malformed_by_prefix),
            "malformed_rate": round(self.malformed_rate, 6),
        }


class ProtocolParser:
    """有狀態的解析器：版本協商 + 目前解析度 + 畸形行計數。

    一個序列埠連線配一個實例。`feed()` 一次吃一行，回事件或 `None`。

    版本協商（§1.1）：
      * 還沒看到任何 `$STATUS` 之前，資料行照解（主機可能是中途接上去的，
        沒辦法先驗證；`proto_confirmed` 會是 `False` 讓呼叫端知道）。
      * 讀到第一行有效 `$STATUS` 就比對 `proto=`。不符 → 進入
        `version_mismatch`，**之後所有 `$` 資料行一律不解析**，只計數。
        不嘗試向下相容舊協定，這是契約明文規定。
      * 裝置重開機／改組態會重發 `$STATUS`，所以之後每一行 `$STATUS`
        都會重新評估——換上對的韌體可以直接恢復，不用重開主機。
    """

    def __init__(self) -> None:
        self.stats = ParserStats()
        self.status: dict | None = None        # 最後一次收到的 `$STATUS` 事件
        self.dim: int | None = None            # 目前 zone 數（16|64）
        self.version_mismatch = False
        self.mismatch_reason: str | None = None
        self.malformed_samples: deque[str] = deque(maxlen=MALFORMED_SAMPLE_LIMIT)

    @property
    def proto_confirmed(self) -> bool:
        """是否已經看過一行 `$STATUS` 且版本相符。"""
        return self.status is not None and not self.version_mismatch

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

        # 版本不符：`$STATUS` 還是要看（換韌體要能恢復），資料行全部丟掉。
        if self.version_mismatch and prefix in DATA_PREFIXES:
            self.stats.dropped_version_mismatch += 1
            return None

        event = parse_line(text)
        if event is None:
            self.stats.malformed += 1
            self.stats.malformed_by_prefix[prefix] = (
                self.stats.malformed_by_prefix.get(prefix, 0) + 1
            )
            self.malformed_samples.append(text[:200])
            return None

        self.stats.parsed += 1

        if event["type"] == "status":
            self._on_status(event)
        elif event["type"] == "tof":
            self._on_tof(event)
        elif event["type"] == "heartbeat":
            # story：畸形行不要靜默丟棄——計數並在 `$H` 事件裡帶出去。
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

    def _on_status(self, event: dict) -> None:
        self.status = event
        self.dim = event["dim"]
        if event["compatible"]:
            self.version_mismatch = False
            self.mismatch_reason = None
        else:
            self.version_mismatch = True
            got = event["proto"]
            got_text = "無 proto 欄位（協定 v1 韌體）" if got is None else f"proto={got}"
            self.mismatch_reason = (
                f"韌體版本不符：主機支援 proto={PROTO_VERSION}，裝置回報 {got_text}。"
                f"已停止解析所有 $ 資料行，請更新韌體。"
            )
        event["mismatch_reason"] = self.mismatch_reason

    def _on_tof(self, event: dict) -> None:
        self.stats.pair_violations += event["pair_violations"]
        if self.dim is not None and event["dim"] != self.dim:
            # 不算畸形：`$T` 自帶 dim，是自描述的。裝置改解析度後會補一行
            # `$STATUS`，這裡先跟上，順便記一筆讓人看得到切換次數。
            self.stats.resolution_changes += 1
        self.dim = event["dim"]
