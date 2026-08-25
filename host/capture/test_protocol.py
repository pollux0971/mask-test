"""`host/capture/protocol.py` 的單元測試。

測資直接抄 `CONTRACTS.md` §1.1 的 7 行真實範例（含極端值：`-1` 無效
zone、`0mm` 最小值、`4000mm` 最大值、`32767` 滿刻度），不自己另編一套，
免得測試通過但跟契約對不上。

放在 `host/capture/` 而不是 story 草稿寫的 `tests/test_parse.py`：
本 repo 既有慣例是測試檔與模組同目錄（見 `host/features/test_*.py`），
且 `host/**` 是 B 軌獨佔，根目錄 `tests/` 不在本 story 的可修改範圍內。
"""
import pytest

from host.capture.protocol import (
    MAX_LINE_LEN,
    READLINE_BUFFER_MIN,
    N_MELS,
    PROTO_VERSION,
    ProtocolParser,
    parse_line,
)

# ---------------------------------------------------------------- §1.1 範例

# 4x4，感測器 A，第 105 幀：zone 3 無效（-1/-1），其餘含最小值 0mm 與最大值 4000mm
TOF_A = (
    "$T,A,105,1737863421123456,16,"
    "120,0,4000,-1,880,340,210,995,60,1200,3400,77,15,600,2200,88,"
    "300,120,58,-1,60,45,88,102,19,7,140,55,66,44,20,90"
)
# 4x4，感測器 B，同一時刻：全部有效
TOF_B = (
    "$T,B,105,1737863421125102,16,"
    "300,450,600,750,80,120,90,60,900,1100,1300,1500,1600,1700,1800,1900,"
    "110,95,130,75,88,70,60,50,45,40,35,30,20,15,10,5"
)
MIC_QUIET = "$M,3150,1737863421124800,12,340"          # 接近靜音
MIC_CLAP = "$M,3151,1737863421126400,28901,32767"      # 拍手，16-bit 滿刻度
HB_OK = "$H,1737863421130000,0,0,0,142300,42"          # 一切正常
HB_DROP = "$H,1737863431130000,3,0,0,18200,58"         # ToF-A 掉 3 幀、heap 偏低
STATUS_V2 = "$STATUS,res=4,proto=2,fw=a1b2c3d"

CONTRACT_EXAMPLES = [TOF_A, TOF_B, MIC_QUIET, MIC_CLAP, HB_OK, HB_DROP, STATUS_V2]


def mel_line(seq=7, t_us=1737863421130000, value=1234):
    return "$F," + ",".join([str(seq), str(t_us)] + [str(value)] * N_MELS)


# ------------------------------------------------------- 1. 四種行都解得出來


def test_contract_examples_all_parse():
    """§1.1 的 7 行範例一行都不能漏。"""
    for line in CONTRACT_EXAMPLES:
        assert parse_line(line) is not None, line


def test_parse_tof_a_extreme_values():
    e = parse_line(TOF_A)
    assert e["type"] == "tof"
    assert e["sensor"] == "A"
    assert e["seq"] == 105
    assert e["t_us"] == 1737863421123456
    assert e["dim"] == 16
    assert len(e["distance"]) == 16 and len(e["signal"]) == 16
    assert e["distance"][1] == 0          # 最小值 0mm 是有效值，不是缺值
    assert e["valid"][1] is True
    assert e["distance"][2] == 4000       # 最大值
    assert e["signal"][0] == 300
    assert e["n_valid"] == 15


def test_parse_tof_b_all_valid():
    e = parse_line(TOF_B)
    assert e["sensor"] == "B"
    assert e["n_valid"] == 16
    assert all(e["valid"])
    assert None not in e["distance"] and None not in e["signal"]


def test_parse_mic_full_scale():
    e = parse_line(MIC_CLAP)
    assert e == {
        "type": "mic",
        "seq": 3151,
        "t_us": 1737863421126400,
        "rms": 28901,
        "peak": 32767,
    }


def test_parse_mic_quiet():
    e = parse_line(MIC_QUIET)
    assert e["rms"] == 12 and e["peak"] == 340
    assert isinstance(e["rms"], int)      # §1.1 變更：rms 是 i16 不是浮點


def test_parse_heartbeat():
    e = parse_line(HB_DROP)
    assert e["type"] == "heartbeat"
    assert (e["drop_A"], e["drop_B"], e["drop_M"]) == (3, 0, 0)
    assert e["heap"] == 18200
    assert e["temp_c"] == 58


def test_parse_status_v2():
    e = parse_line(STATUS_V2)
    assert e["res"] == 4                  # 邊長
    assert e["dim"] == 16                 # zone 數，對應 `$T` 的 dim 欄位
    assert e["proto"] == PROTO_VERSION
    assert e["fw"] == "a1b2c3d"
    assert e["compatible"] is True


def test_parse_mel_frame():
    e = parse_line(mel_line(value=-250))
    assert e["type"] == "mel"
    assert len(e["mel_q"]) == N_MELS
    assert e["mel_q"][0] == -250
    assert e["log_mel"][0] == pytest.approx(-2.5)   # §3.1 int16 = log_mel*100


def test_parse_record_line_is_recognised():
    """`$REC` 不是 B01 的四種資料行，但也不能被算成畸形行。"""
    e = parse_line("$REC,start,5")
    assert e == {"type": "record", "state": "recording", "seconds": 5}


def test_parse_8x8_frame_length_not_hardcoded():
    """8×8 = 64 zones、128 個值，長度靠行內的 dim 推，不寫死。"""
    line = "$T,A,9,1000,64," + ",".join(["4000"] * 64 + ["200"] * 64)
    e = parse_line(line)
    assert e["dim"] == 64 and e["n_valid"] == 64
    assert e["distance"][63] == 4000 and e["signal"][63] == 200
    # §1.3：8×8 + signal 是那種幾百 bytes 的長行，readline buffer 要 ≥1024
    assert 500 < len(line) < READLINE_BUFFER_MIN


# ------------------------------------------- 2. `-1` 無效值：d 與 s 成對處理


def test_invalid_zone_drops_distance_and_signal_together():
    """§1.1：`-1` 時距離與 signal 是同一組缺值，要一起跳過。"""
    e = parse_line(TOF_A)
    assert e["distance"][3] is None
    assert e["signal"][3] is None       # 不可以只判斷距離就留下 signal
    assert e["valid"][3] is False
    assert e["raw_distance"][3] == -1   # 原始值仍保留給要自己判斷的下游
    assert e["raw_signal"][3] == -1
    assert e["pair_violations"] == 0


def test_half_invalid_zone_is_treated_as_invalid_and_counted():
    """只有一邊是 -1 → 違反契約。整個 zone 當無效，並計數。"""
    line = "$T,A,1,1000,16," + ",".join(["-1"] + ["100"] * 15 + ["50"] * 16)
    e = parse_line(line)
    assert e["valid"][0] is False
    assert e["distance"][0] is None and e["signal"][0] is None
    assert e["pair_violations"] == 1
    assert e["n_valid"] == 15


def test_all_zones_invalid():
    line = "$T,B,2,1000,16," + ",".join(["-1"] * 32)
    e = parse_line(line)
    assert e["n_valid"] == 0
    assert not any(e["valid"])


# --------------------------------------------------- 3. 畸形輸入不拋例外


MALFORMED = [
    pytest.param("$T,A,105,1737863421123456,16,120,0,4000", id="tof-truncated"),
    pytest.param("$T,A,105,1737863421123456,16," + ",".join(["1"] * 33), id="tof-one-extra"),
    pytest.param("$T,C,1,1000,16," + ",".join(["1"] * 32), id="tof-bad-sensor"),
    pytest.param("$T,A,1,1000,32," + ",".join(["1"] * 64), id="tof-bad-dim"),
    pytest.param("$T,A,1,-5,16," + ",".join(["1"] * 32), id="tof-negative-t_us"),
    pytest.param("$T,A,1,1000,16," + ",".join(["1"] * 31 + ["x"]), id="tof-nan-value"),
    pytest.param("$T,A,1,1000,16," + ",".join(["1"] * 31 + ["40000"]), id="tof-i16-overflow"),
    pytest.param("$T,A,4294967296,1000,16," + ",".join(["1"] * 32), id="tof-u32-overflow"),
    pytest.param("$M,3150,1737863421124800,12", id="mic-missing-field"),
    pytest.param("$M,3150,1737863421124800,12,340,99", id="mic-extra-field"),
    pytest.param("$M,3150,1737863421124800,12.5,340", id="mic-float-rms"),
    pytest.param("$M,,1737863421124800,12,340", id="mic-empty-seq"),
    pytest.param("$H,1737863421130000,0,0,0,142300", id="hb-missing-temp"),
    pytest.param("$H,1737863421130000,0,0,0,142300,999", id="hb-temp-out-of-i8"),
    pytest.param("$F,7,1000," + ",".join(["1"] * 39), id="mel-39-coeffs"),
    pytest.param("$F,7,1000," + ",".join(["1"] * 41), id="mel-41-coeffs"),
    pytest.param("$STATUS,res=5,proto=2,fw=abc", id="status-bad-res"),
    pytest.param("$STATUS,proto=2,fw=abc", id="status-no-res"),
    pytest.param("$STATUS,res=4,protoo", id="status-token-without-eq"),
    pytest.param("$REC,start", id="rec-missing-seconds"),
    pytest.param("$", id="bare-dollar"),
    pytest.param("$T", id="tof-header-only"),
    pytest.param("$WAT,1,2,3", id="unknown-line-type"),
    pytest.param("$T,A,1,1000,16," + ",".join(["1"] * 32) + "$T,B,1,1000,16,1", id="two-lines-glued"),
    pytest.param("$T,A,1,1000,16," + ",".join(["9"] * (MAX_LINE_LEN // 2)), id="over-max-line-len"),
    pytest.param("$T,A,1,1000,16,���", id="utf8-garbage"),
]


@pytest.mark.parametrize("line", MALFORMED)
def test_malformed_returns_none_never_raises(line):
    assert parse_line(line) is None


def test_malformed_bytes_input_never_raises():
    """serial 直出的 bytes，含壞掉的 UTF-8 與 NUL，也不能炸。"""
    assert parse_line(b"$T,A,1,1000,16,\xff\xfe\x00garbage") is None
    assert parse_line(b"\x00\x00\x00") is None


def test_non_protocol_lines_return_none():
    for line in ["", "   ", "I (123) main: booting", "BEGIN_WAV_B64 rate=16000", "END_WAV_B64"]:
        assert parse_line(line) is None


def test_bytes_input_parses_like_str():
    assert parse_line(MIC_CLAP.encode()) == parse_line(MIC_CLAP)


def test_crlf_and_whitespace_tolerated():
    assert parse_line("  " + MIC_QUIET + "\r\n") == parse_line(MIC_QUIET)


# ------------------------------------------ 4. ProtocolParser：計數與統計


def test_parser_counts_malformed_separately_from_device_logs():
    p = ProtocolParser()
    p.feed(STATUS_V2)
    p.feed(TOF_A)
    p.feed("I (123) main: booting")     # 裝置 log，不算畸形
    p.feed("$M,bad,line")               # 畸形
    p.feed("$T,A,1,1000,16,1")          # 畸形
    assert p.stats.parsed == 2
    assert p.stats.malformed == 2
    assert p.stats.ignored == 1
    assert p.stats.lines == 5
    assert p.stats.malformed_by_prefix == {"$M": 1, "$T": 1}
    assert p.stats.malformed_rate == pytest.approx(0.5)
    assert len(p.malformed_samples) == 2


def test_heartbeat_event_carries_host_malformed_count():
    """story：畸形行要計數並在 `$H` 事件裡帶出去。"""
    p = ProtocolParser()
    p.feed(STATUS_V2)
    p.feed("$M,x,y,z,w")
    e = p.feed(HB_OK)
    assert e["type"] == "heartbeat"
    assert e["host"]["malformed"] == 1
    assert e["host"]["malformed_rate"] > 0


def test_parser_accumulates_pair_violations():
    p = ProtocolParser()
    p.feed(STATUS_V2)
    bad = "$T,A,1,1000,16," + ",".join(["-1"] + ["100"] * 15 + ["50"] * 16)
    p.feed(bad)
    p.feed(bad)
    assert p.stats.pair_violations == 2


def test_parser_tracks_resolution_change():
    p = ProtocolParser()
    p.feed(STATUS_V2)
    assert p.dim == 16
    p.feed(TOF_A)
    assert p.stats.resolution_changes == 0
    p.feed("$T,A,10,2000,64," + ",".join(["500"] * 128))
    assert p.dim == 64
    assert p.stats.resolution_changes == 1


def test_parser_feed_many_preserves_order():
    p = ProtocolParser()
    events = p.feed_many(CONTRACT_EXAMPLES)
    assert [e["type"] for e in events] == [
        "tof", "tof", "mic", "mic", "heartbeat", "heartbeat", "status",
    ]


# ------------------------------------------------------- 5. 版本協商 §1.1


def test_version_mismatch_stops_parsing_all_data_lines():
    p = ProtocolParser()
    e = p.feed("$STATUS,res=4,proto=3,fw=deadbee")
    assert e["compatible"] is False
    assert "韌體版本不符" in e["mismatch_reason"]
    assert p.version_mismatch is True
    assert p.proto_confirmed is False

    # 之後所有 `$` 資料行一律不解析——不嘗試向下相容。
    for line in (TOF_A, MIC_CLAP, HB_OK, mel_line(), "$REC,start,5"):
        assert p.feed(line) is None
    assert p.stats.dropped_version_mismatch == 5
    assert p.stats.malformed == 0       # 被版本擋掉，不是畸形


def test_v1_status_without_proto_is_a_mismatch():
    """舊韌體的 `$STATUS,res=4` 沒有 proto 欄位 → 視為版本不符。"""
    p = ProtocolParser()
    e = p.feed("$STATUS,res=4")
    assert e["proto"] is None
    assert e["compatible"] is False
    assert p.version_mismatch is True
    assert p.feed(TOF_A) is None


def test_parser_recovers_when_correct_firmware_reappears():
    """裝置重開機／換韌體會重發 `$STATUS`，主機要能自己恢復。"""
    p = ProtocolParser()
    p.feed("$STATUS,res=4,proto=1,fw=old")
    assert p.feed(TOF_A) is None
    p.feed(STATUS_V2)
    assert p.version_mismatch is False
    assert p.proto_confirmed is True
    assert p.feed(TOF_A) is not None


def test_data_before_status_is_parsed_but_not_confirmed():
    """主機可能是中途接上序列埠，還沒看到 `$STATUS` 就有資料進來。"""
    p = ProtocolParser()
    assert p.proto_confirmed is False
    assert p.feed(TOF_A) is not None
    p.feed(STATUS_V2)
    assert p.proto_confirmed is True


# ------------------------------- 6. 錄音 dump 期間：殘行、亂碼、不能崩


def test_survives_recording_dump_noise():
    """§1.4：錄音 dump 吃掉 92% 頻寬，ToF 必然掉幀，UART 會出現殘行與
    base64 payload 夾雜。整條資料流不能被打斷。"""
    p = ProtocolParser()
    p.feed(STATUS_V2)
    stream = [
        "$REC,start,5",
        "BEGIN_WAV_B64 rate=16000 bits=16 channels=1 bytes=160000",
        "AAAA//8AAP//AAA=" * 20,                 # base64 payload
        TOF_A[:60],                              # 被截斷的殘行
        MIC_CLAP,
        "$T,A,106,17378634211" + TOF_A,          # 兩行黏在一起
        "\x00\x00" + HB_OK,                      # 前面掛了 NUL，`$` 前綴被吃掉
        "END_WAV_B64",
        TOF_B,
    ]
    events = p.feed_many(stream)
    types = [e["type"] for e in events]
    assert "mic" in types and "tof" in types and "record" in types
    assert p.stats.malformed == 2        # 被截斷的殘行 + 兩行黏在一起
    assert p.stats.malformed_by_prefix == {"$T": 2}
    # `$` 前綴本身被 NUL 蓋掉的行，跟裝置的一般 log 長得一樣，分不出來，
    # 只能算進 ignored。這是已知的限制，不是 bug。
    assert p.stats.ignored == 4          # BEGIN / base64 / NUL 汙染行 / END
    assert p.stats.parsed == 1 + 3       # $STATUS + $REC + $M + $T(B)
    assert p.version_mismatch is False   # 雜訊不該讓版本狀態亂掉


def test_five_minutes_of_synthetic_traffic_never_raises():
    """30 fps × 2 感測器 × 5 分鐘 ≈ 18000 幀，混 2% 畸形行，全程不拋例外。"""
    p = ProtocolParser()
    p.feed(STATUS_V2)
    frames = 9000
    for seq in range(frames):
        p.feed(f"$T,A,{seq},{1000 + seq * 33333},16," + ",".join(["500"] * 16 + ["70"] * 16))
        p.feed(TOF_B)
        if seq % 50 == 0:
            p.feed(HB_OK)
        if seq % 50 == 7:
            p.feed("$T,A,garbage")
    assert p.stats.parsed > 18000
    assert p.stats.malformed == frames // 50 + (1 if frames % 50 > 7 else 0)
    assert p.version_mismatch is False
