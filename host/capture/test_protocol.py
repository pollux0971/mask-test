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
    PARSERS,
    V1_WARNING,
    READLINE_BUFFER_MIN,
    N_MELS,
    PROTO_VERSION,
    ProtocolParser,
    parse_line,
    parse_line_v1,
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
        "proto": 2,
        "seq": 3151,
        "t_us": 1737863421126400,
        "has_timestamp": True,
        "rms": 28901,
        "peak": 32767,
        "extra": [],
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
    assert e == {
        "type": "record", "proto": 2, "state": "recording", "seconds": 5, "extra": [],
    }


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
    pytest.param("$T,C,1,1000,16," + ",".join(["1"] * 32), id="tof-bad-sensor"),
    pytest.param("$T,A,1,1000,32," + ",".join(["1"] * 64), id="tof-bad-dim"),
    pytest.param("$T,A,1,-5,16," + ",".join(["1"] * 32), id="tof-negative-t_us"),
    pytest.param("$T,A,1,1000,16," + ",".join(["1"] * 31 + ["x"]), id="tof-nan-value"),
    pytest.param("$T,A,1,1000,16," + ",".join(["1"] * 31 + ["40000"]), id="tof-i16-overflow"),
    pytest.param("$T,A,4294967296,1000,16," + ",".join(["1"] * 32), id="tof-u32-overflow"),
    pytest.param("$M,3150,1737863421124800,12", id="mic-missing-field"),
    pytest.param("$M,3150,1737863421124800,12.5,340", id="mic-float-rms"),
    pytest.param("$M,,1737863421124800,12,340", id="mic-empty-seq"),
    pytest.param("$H,1737863421130000,0,0,0,142300", id="hb-missing-temp"),
    pytest.param("$H,1737863421130000,0,0,0,142300,999", id="hb-temp-out-of-i8"),
    pytest.param("$F,7,1000," + ",".join(["1"] * 39), id="mel-39-coeffs"),
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


def test_v1_lines_count_as_version_mismatch_not_malformed():
    """接到舊韌體時，`$TOF`/`$MIC` 不是「畸形」而是「版本不符」。
    混在一起的話錯誤率會衝到 ~100%，蓋掉真正的原因（C04 要顯示版本不符）。"""
    p = ProtocolParser()
    p.feed("$STATUS,res=8")
    p.feed("$TOF,A,64," + ",".join(["17"] * 64))
    p.feed("$MIC,322.1,498")
    assert p.stats.malformed == 0
    assert p.stats.malformed_rate == 0.0
    assert p.stats.dropped_version_mismatch == 2


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


# ================================================================ B02 v1/v2

# 真實舊韌體（`fb286d1:vl53l7cx_test/main/vl53l7cx_test.c:135`）與現在的
# `mock_device.py --proto v1`：dim 欄位是**邊長**，且**只有距離沒有 signal**。
TOF_V1_FW = "$TOF,A,4," + ",".join(["120", "0", "4000", "-1"] * 4)
# 另一種方言：dim 欄位是 zone 數且帶 signal。`mock_device.py` 原本送這種，
# 已修正成上面那種（見回報）。解析器**保留**這條路徑：舊 mock 產生的紀錄
# 檔還在，而且多認一種自描述、無歧義的格式不會有壞處。
TOF_V1_MOCK = "$TOF,B,16," + ",".join(["300"] * 16 + ["70"] * 16)
MIC_V1 = "$MIC,322.1,498"          # 舊韌體 printf("$MIC,%.1f,%d")
STATUS_V1 = "$STATUS,res=4"        # 沒有 proto=、沒有 fw=


def test_parsers_table_matches_story_sketch():
    assert PARSERS[1] is parse_line_v1
    assert PARSERS[2] is parse_line


# ------------------------------------------- v1 兩種 $TOF 方言都要解得出來


def test_v1_real_firmware_tof_side_dim_no_signal():
    e = parse_line_v1(TOF_V1_FW)
    assert e["proto"] == 1
    assert e["sensor"] == "A"
    assert e["dim"] == 16                 # 邊長 4 → 16 zones
    assert e["signal_present"] is False
    assert e["raw_signal"] is None        # 舊韌體根本沒送，不捏造 0
    assert all(s is None for s in e["signal"])
    assert e["distance"][0] == 120 and e["distance"][1] == 0
    assert e["distance"][3] is None and e["valid"][3] is False
    assert e["n_valid"] == 12             # 每 4 個有 1 個 -1
    assert e["pair_violations"] == 0      # 沒有 signal 就沒有配對可違反


def test_v1_mock_tof_zone_dim_with_signal():
    e = parse_line_v1(TOF_V1_MOCK)
    assert e["dim"] == 16
    assert e["signal_present"] is True
    assert e["signal"][0] == 70
    assert e["n_valid"] == 16


def test_v1_8x8_real_firmware_dialect():
    e = parse_line_v1("$TOF,A,8," + ",".join(["500"] * 64))
    assert e["dim"] == 64 and e["signal_present"] is False


def test_v1_tof_has_no_seq_or_timestamp_and_never_fakes_them():
    """v1 線上就沒有這兩個欄位。填 None，不補 0、不補現在時間。"""
    for line in (TOF_V1_FW, TOF_V1_MOCK):
        e = parse_line_v1(line)
        assert e["seq"] is None
        assert e["t_us"] is None
        assert e["has_timestamp"] is False


def test_v1_mic_keeps_float_rms():
    e = parse_line_v1(MIC_V1)
    assert e["proto"] == 1
    assert e["rms"] == pytest.approx(322.1)   # v1 是浮點，不四捨五入假裝是 v2
    assert e["peak"] == 498
    assert e["t_us"] is None


def test_v1_status_is_version_agnostic_and_reports_no_proto():
    """`$STATUS` 是唯一兩版格式相同的行，兩個解析器結果要一致。"""
    assert parse_line_v1(STATUS_V1) == parse_line(STATUS_V1)
    e = parse_line_v1(STATUS_V1)
    assert e["res"] == 4 and e["dim"] == 16
    # 裝置沒講版本 → None。不可以蓋成 1 假裝裝置說了它是 v1。
    assert e["proto"] is None


def test_v1_rec_line_shared_with_v2():
    assert parse_line_v1("$REC,start,5")["state"] == "recording"


@pytest.mark.parametrize("line", [
    pytest.param("$TOF,A,4,120,0,4000", id="v1-tof-short"),
    pytest.param("$TOF,A,5," + ",".join(["1"] * 25), id="v1-tof-bad-dim"),
    pytest.param("$TOF,C,4," + ",".join(["1"] * 16), id="v1-tof-bad-sensor"),
    pytest.param("$TOF,A,4," + ",".join(["1"] * 24), id="v1-tof-count-between"),
    pytest.param("$MIC,322.1", id="v1-mic-short"),
    pytest.param("$MIC,abc,498", id="v1-mic-nan-rms"),
    pytest.param("$MIC,nan,498", id="v1-mic-literal-nan"),
    pytest.param("$MIC,inf,498", id="v1-mic-inf"),
    pytest.param("$MIC,322.1,40000", id="v1-mic-peak-overflow"),
    pytest.param("$T,A,1,1000,16," + ",".join(["1"] * 32), id="v2-line-into-v1-parser"),
])
def test_v1_malformed_returns_none_never_raises(line):
    assert parse_line_v1(line) is None


# ----------------------------------------- 預設嚴格：降級不能是預設行為


def test_default_parser_still_rejects_v1_status():
    p = ProtocolParser()
    assert p.allow_v1 is False
    e = p.feed(STATUS_V1)
    assert e["compatible"] is False
    assert p.version_mismatch is True
    assert p.degraded is False
    assert p.recording_allowed is False
    # 認得出來是 v1，只是不接受——panel 才能顯示「偵測到協定 v1 韌體」
    # 而不是「未知版本」。
    assert p.protocol_version == 1
    assert p.state()["protocol_version"] == 1
    assert "allow_v1=True" in p.mismatch_reason   # 告訴使用者有這個選項


def test_default_parser_detects_v1_from_data_lines_without_status():
    """v1 韌體開機只印一次 `$STATUS`。主機中途接上去永遠等不到那一行，
    所以光看到 `$TOF` 就要能判定版本不符，而不是把它算成畸形行。"""
    p = ProtocolParser()
    assert p.feed(TOF_V1_FW) is None
    assert p.version_mismatch is True
    assert p.stats.malformed == 0
    assert p.stats.dropped_version_mismatch == 1
    assert p.protocol_version == 1
    assert "$TOF" in p.mismatch_reason
    for line in (TOF_V1_MOCK, MIC_V1):
        assert p.feed(line) is None
    assert p.stats.dropped_version_mismatch == 3
    assert p.stats.malformed_rate == 0.0     # 錯誤率不該被版本問題污染


# ---------------------------------------------- allow_v1=True 的降級模式


def test_allow_v1_status_enters_degraded_mode():
    p = ProtocolParser(allow_v1=True)
    e = p.feed(STATUS_V1)
    assert p.version_mismatch is False
    assert p.protocol_version == 1
    assert p.degraded is True
    assert p.proto_confirmed is True
    assert e["proto_detected"] == 1
    assert e["degraded"] is True
    assert e["warning"] == V1_WARNING
    assert e["recording_allowed"] is False


def test_degraded_mode_parses_v1_data_lines():
    p = ProtocolParser(allow_v1=True)
    p.feed(STATUS_V1)
    events = p.feed_many([TOF_V1_FW, TOF_V1_MOCK, MIC_V1, "$REC,start,5"])
    assert [e["type"] for e in events] == ["tof", "tof", "mic", "record"]
    assert all(e["proto"] == 1 for e in events)
    assert p.stats.malformed == 0
    assert p.stats.v1_lines == 4


def test_degraded_mode_warning_and_recording_flag():
    """驗收條件：v1 模式下錄製功能被停用或明顯警示。"""
    p = ProtocolParser(allow_v1=True)
    p.feed(STATUS_V1)
    assert p.warning == V1_WARNING
    assert "無時間戳" in p.warning
    assert p.recording_allowed is False
    st = p.state()
    assert st["protocol_version"] == 1
    assert st["degraded"] is True and st["recording_allowed"] is False
    assert st["warning"] == V1_WARNING


def test_v2_mode_has_no_warning_and_allows_recording():
    for p in (ProtocolParser(), ProtocolParser(allow_v1=True)):
        p.feed(STATUS_V2)
        assert p.protocol_version == 2
        assert p.degraded is False
        assert p.warning is None
        assert p.recording_allowed is True
        assert p.state()["fw"] == "a1b2c3d"


def test_allow_v1_detects_v1_from_data_lines_without_status():
    """沒收到 `$STATUS` 也要能靠 `$TOF` 認出對方是 v1。"""
    p = ProtocolParser(allow_v1=True)
    e = p.feed(TOF_V1_FW)
    assert e is not None and e["proto"] == 1
    assert p.protocol_version == 1
    assert p.degraded is True
    assert p.recording_allowed is False


def test_allow_v1_still_rejects_unknown_future_proto():
    """允許 v1 不代表什麼版本都收。proto=3 主機不認得，一樣要停。"""
    p = ProtocolParser(allow_v1=True)
    e = p.feed("$STATUS,res=4,proto=3,fw=deadbee")
    assert e["compatible"] is False
    assert p.version_mismatch is True
    assert p.degraded is False
    assert p.feed(TOF_A) is None
    assert p.feed(TOF_V1_FW) is None


# --------------------------------------------------- 韌體切換：雙向都要通


def test_switch_v1_to_v2_at_runtime():
    """燒錄新韌體、裝置重開機 → 重發 `$STATUS`，主機自動切回 v2。"""
    p = ProtocolParser(allow_v1=True)
    p.feed(STATUS_V1)
    assert p.feed(TOF_V1_FW)["proto"] == 1
    p.feed(STATUS_V2)
    assert p.degraded is False and p.recording_allowed is True
    assert p.feed(TOF_A)["proto"] == 2
    assert p.stats.malformed == 0


def test_switch_v2_to_v1_at_runtime():
    """燒回舊韌體也要通，而且要重新亮出警告。"""
    p = ProtocolParser(allow_v1=True)
    p.feed(STATUS_V2)
    assert p.feed(TOF_A)["proto"] == 2
    p.feed(STATUS_V1)
    assert p.degraded is True
    assert p.warning == V1_WARNING
    assert p.feed(TOF_V1_FW)["proto"] == 1
    assert p.stats.malformed == 0
    # `$STATUS` 兩版共用，不歸給任何一版；只有資料行才計數。
    assert p.stats.v1_lines == 1 and p.stats.v2_lines == 1


def test_mixed_stream_counts_both_versions_separately():
    p = ProtocolParser(allow_v1=True)
    p.feed(STATUS_V2)
    p.feed(TOF_A)
    p.feed(MIC_CLAP)
    p.feed(TOF_V1_FW)          # 不該出現的混流，但要看得到而不是崩掉
    p.feed(MIC_V1)
    assert p.stats.v2_lines == 2       # $T + $M（$STATUS 不歸給任何一版）
    assert p.stats.v1_lines == 2
    assert p.stats.malformed == 0


def test_degraded_mode_survives_noise():
    p = ProtocolParser(allow_v1=True)
    p.feed(STATUS_V1)
    stream = [
        "I (123) main: booting",
        TOF_V1_FW[:30],                       # 殘行
        "BEGIN_WAV_B64 rate=16000 bits=16 channels=1 bytes=160000",
        "AAAA//8AAP//AAA=" * 20,
        MIC_V1,
        b"$TOF,A,4,\xff\xfe garbage",
        TOF_V1_MOCK,
    ]
    events = p.feed_many(stream)
    assert [e["type"] for e in events] == ["mic", "tof"]
    assert p.stats.malformed == 2
    assert p.degraded is True                 # 雜訊不該把版本狀態弄丟


# ==================================================== §1.1.2 $STATUS 音框參數

STATUS_FULL = (
    "$STATUS,res=8,proto=2,fw=a1b2c3d,"
    "sr=16000,mel=1,mel_win=512,mel_hop=256,mic_hop=512"
)


def test_status_exposes_audio_frame_params():
    e = parse_line(STATUS_FULL)
    assert e["res"] == 8 and e["dim"] == 64 and e["proto"] == 2
    assert e["sr"] == 16000
    assert e["mel"] is True
    assert e["mel_win"] == 512
    assert e["mel_hop"] == 256      # A14 之後：$F = 62.5 Hz
    assert e["mic_hop"] == 512      # $M 維持 31.25 Hz
    assert e["compatible"] is True


def test_status_missing_audio_params_are_none_not_defaults():
    """§1.1.2：缺欄位一律 None。填預設值會讓舊韌體看起來像新韌體，
    `B06` 就會用錯的幀間距去對齊。"""
    e = parse_line(STATUS_V2)       # 只有 res/proto/fw
    for key in ("sr", "mel", "mel_win", "mel_hop", "mic_hop"):
        assert e[key] is None, key


def test_status_mel_off_is_false_not_none():
    """`mel=0` 是「韌體說了：關」，跟「韌體沒說」必須分得出來。"""
    e = parse_line("$STATUS,res=4,proto=2,fw=abc,mel=0")
    assert e["mel"] is False
    assert e["mel_win"] is None


def test_status_field_order_is_irrelevant():
    """§1.1.2 硬性規定：key=value、順序無關，不可用固定位置切分。"""
    a = parse_line(STATUS_FULL)
    b = parse_line(
        "$STATUS,mic_hop=512,mel_hop=256,fw=a1b2c3d,mel=1,"
        "proto=2,mel_win=512,res=8,sr=16000"
    )
    assert a["fields"] == b["fields"]
    for key in ("res", "proto", "fw", "sr", "mel", "mel_win", "mel_hop", "mic_hop"):
        assert a[key] == b[key], key


def test_status_unknown_future_fields_are_ignored_not_fatal():
    """日後還會再加欄位。多出來的欄位不能讓這行解不出來，但也不能被丟掉。"""
    e = parse_line(STATUS_FULL + ",imu=1,batt_mv=3900")
    assert e["compatible"] is True and e["mel_hop"] == 256
    assert e["fields"]["imu"] == "1"
    assert e["fields"]["batt_mv"] == "3900"


def test_status_broken_optional_field_does_not_kill_version_negotiation():
    """選用參數壞掉時仍要解得出 `proto=`——那行還扛著版本協商。"""
    e = parse_line("$STATUS,res=4,proto=2,fw=abc,mel_hop=abc,sr=-1,mel=7")
    assert e is not None
    assert e["compatible"] is True
    assert e["mel_hop"] is None and e["sr"] is None and e["mel"] is None
    assert e["fields"]["mel_hop"] == "abc"      # 原文仍保留供人工判讀


def test_parser_state_carries_audio_params_for_b06_and_panel():
    p = ProtocolParser()
    p.feed(STATUS_FULL)
    st = p.state()
    assert st["mel_hop"] == 256 and st["mic_hop"] == 512
    assert st["sr"] == 16000 and st["mel"] is True
    assert st["mel_win"] == 512
    assert st["fw"] == "a1b2c3d"


def test_parser_state_audio_params_none_before_any_status():
    p = ProtocolParser()
    st = p.state()
    for key in ("sr", "mel", "mel_win", "mel_hop", "mic_hop", "fw"):
        assert st[key] is None, key


def test_v1_status_has_no_audio_params():
    """舊韌體的 `$STATUS,res=4` 什麼都沒說，五個欄位全是 None。"""
    p = ProtocolParser(allow_v1=True)
    p.feed(STATUS_V1)
    st = p.state()
    assert st["protocol_version"] == 1
    for key in ("sr", "mel", "mel_win", "mel_hop", "mic_hop"):
        assert st[key] is None, key


def test_status_line_is_not_counted_as_v1_or_v2():
    p = ProtocolParser()
    p.feed(STATUS_FULL)
    assert p.stats.parsed == 1
    assert p.stats.v1_lines == 0 and p.stats.v2_lines == 0


def test_v1_mock_device_line_shape_matches_real_firmware():
    """`mock_device.py --proto v1` 修正後實際送出的樣子（dim=4）。
    抄自實跑輸出，確保夾具與解析器不會各自漂移。"""
    line = "$TOF,A,4," + ",".join(["17"] * 16)
    e = parse_line_v1(line)
    assert e["proto"] == 1 and e["dim"] == 16
    assert e["signal_present"] is False
    assert e["n_valid"] == 16
    assert parse_line_v1("$STATUS,res=4")["proto"] is None
    assert parse_line_v1("$MIC,274.4,485")["rms"] == pytest.approx(274.4)


# ============================================ §1.1 前向相容（比我新的韌體）

def test_heartbeat_with_bw_field_parses():
    """`A15` 在 `$H` 尾端加了 `bw_bytes_since_last`（第 8 段）。"""
    e = parse_line("$H,1737863421130000,3,0,0,18200,58,45678")
    assert e["type"] == "heartbeat"
    assert e["bw_bytes_since_last"] == 45678
    # 舊欄位一個都不能掉——這才是這個 bug 真正的危害
    assert e["heap"] == 18200 and e["temp_c"] == 58
    assert (e["drop_A"], e["drop_B"], e["drop_M"]) == (3, 0, 0)


def test_heartbeat_without_bw_field_is_none_not_zero():
    """舊韌體沒有第 8 段 → `None`。0 是一個合法的頻寬讀數，拿它當缺值
    會讓舊韌體看起來像「這段期間完全沒傳東西」。"""
    e = parse_line(HB_DROP)
    assert e["bw_bytes_since_last"] is None
    assert e["heap"] == 18200


def test_truncated_heartbeat_is_still_malformed():
    """放寬不能放寬到連截斷都吃下去。截斷是傳輸損壞最常見的形態。"""
    assert parse_line("$H,1737863421130000,0,0,0,142300") is None      # 6 段
    assert parse_line("$H,1737863421130000,0,0,0") is None             # 4 段
    assert parse_line("$H,1737863421130000") is None


def test_heartbeat_beyond_bw_goes_to_extra():
    e = parse_line("$H,1737863421130000,0,0,0,142300,42,45678,999,abc")
    assert e["bw_bytes_since_last"] == 45678
    assert e["extra"] == ["999", "abc"]


def test_heartbeat_bad_bw_field_does_not_kill_the_frame():
    """第 8 段壞掉只讓它變 None，不能連 heap/temp 一起弄丟。"""
    e = parse_line("$H,1737863421130000,0,0,0,142300,42,notanumber")
    assert e is not None
    assert e["bw_bytes_since_last"] is None
    assert e["heap"] == 142300


@pytest.mark.parametrize("line,key,expected_extra", [
    pytest.param(TOF_A + ",777", "tof", ["777"], id="tof-extra"),
    pytest.param(MIC_CLAP + ",777", "mic", ["777"], id="mic-extra"),
    pytest.param(mel_line() + ",777", "mel", ["777"], id="mel-extra"),
    pytest.param("$REC,start,5,777", "record", ["777"], id="rec-extra"),
])
def test_trailing_fields_are_ignored_not_fatal(line, key, expected_extra):
    """§1.1 前向相容通則：多出來的尾端欄位忽略，但不丟棄。"""
    e = parse_line(line)
    assert e is not None and e["type"] == key
    assert e["extra"] == expected_extra


def test_trailing_fields_do_not_corrupt_payload():
    """多的欄位不能被誤當成資料。"""
    e = parse_line(TOF_A + ",777")
    base = parse_line(TOF_A)
    assert e["distance"] == base["distance"]
    assert e["signal"] == base["signal"]
    assert e["n_valid"] == base["n_valid"]

    m = parse_line(mel_line(value=1234) + ",777")
    assert len(m["mel_q"]) == N_MELS and set(m["mel_q"]) == {1234}


@pytest.mark.parametrize("line", [
    pytest.param("$T,A,105,1737863421123456,16," + ",".join(["1"] * 31), id="tof-one-short"),
    pytest.param("$M,3150,1737863421124800,12", id="mic-one-short"),
    pytest.param("$F,7,1000," + ",".join(["1"] * 39), id="mel-one-short"),
    pytest.param("$REC,start", id="rec-short"),
    pytest.param("$MIC,322.1", id="v1-mic-short-again"),
])
def test_truncation_still_detected_after_relaxing(line):
    """放寬成「至少 N 段」之後，少一段仍然要被抓出來。"""
    assert parse_line(line) is None or parse_line_v1(line) is None


def test_v1_mic_tolerates_trailing_fields():
    e = parse_line_v1("$MIC,322.1,498,777")
    assert e["rms"] == pytest.approx(322.1) and e["extra"] == ["777"]


def test_parser_does_not_count_newer_firmware_as_malformed():
    """整條的效果：比主機新的韌體不該讓錯誤率上升。"""
    p = ProtocolParser()
    p.feed(STATUS_FULL)
    p.feed("$H,1737863421130000,0,0,0,142300,42,45678")
    p.feed(TOF_A + ",777")
    p.feed(MIC_CLAP + ",888")
    assert p.stats.malformed == 0
    assert p.stats.parsed == 4


def test_v1_tof_keeps_exact_count_check_on_purpose():
    """v1 是凍結的歷史格式，不會長新欄位；這裡的值數還兼任方言判別，
    放寬會讓截斷的「距離+signal」行被誤讀成完整的「只有距離」行。"""
    assert parse_line_v1("$TOF,A,4," + ",".join(["1"] * 17)) is None   # 16+1
    assert parse_line_v1("$TOF,A,4," + ",".join(["1"] * 24)) is None   # 截斷的 32
    assert parse_line_v1("$TOF,A,4," + ",".join(["1"] * 16)) is not None
    assert parse_line_v1("$TOF,A,4," + ",".join(["1"] * 32)) is not None


def test_mock_device_heartbeat_with_bw_field_shape():
    """`mock_device.py` 加上 `bw_bytes_since_last` 之後實際送出的樣子。
    抄自實跑輸出（8×8 @30fps ≈ 28.5 KB/s ≈ 46080 B/s 的 62%）。"""
    e = parse_line("$H,1000546,0,0,0,151142,38,28388")
    assert e["bw_bytes_since_last"] == 28388
    assert e["heap"] == 151142 and e["temp_c"] == 38
    assert e["extra"] == []


# ==================================================== mock device 的 $F（T04）

def test_mel_frame_from_mock_device_parses():
    """`mock_device.py --mel 1` 實際送出的 `$F`（抄自實跑輸出的前幾個 band）。"""
    bands = [-605, -587, -612, -608, -603, -611, -615, -617, -625, -626]
    line = "$F,0,184," + ",".join(str(b) for b in bands + [-620] * 30)
    e = parse_line(line)
    assert e["type"] == "mel" and e["proto"] == 2
    assert e["seq"] == 0 and e["t_us"] == 184
    assert len(e["mel_q"]) == N_MELS
    assert e["mel_q"][:10] == bands
    # §3.1：int16 = round(log_mel * 100)，所以 -605 → -6.05
    assert e["log_mel"][0] == pytest.approx(-6.05)
    assert e["extra"] == []


def test_mel_seq_is_independent_of_mic_seq():
    """§1.1.1：`$F` 與 `$M` 是兩條獨立串流，各自的 `seq` 沒有固定關係。
    解析器只要忠實暴露各自的 `seq`，不可以假設任何比例。"""
    mel = parse_line("$F,900,5000," + ",".join(["-600"] * N_MELS))
    mic = parse_line("$M,7,5000,120,900")
    assert mel["seq"] == 900 and mic["seq"] == 7
    assert mel["t_us"] == mic["t_us"]        # 同一個 ring 快照可以共用 t_us
    assert mel["type"] != mic["type"]


def test_mel_full_scale_and_noise_floor_values():
    """§3.1 的值域：log10(max(power, 1e-10)) * 100 → [-1000, 0]。"""
    floor = parse_line("$F,1,1," + ",".join(["-1000"] * N_MELS))
    assert floor["log_mel"][0] == pytest.approx(-10.0)
    ceiling = parse_line("$F,2,1," + ",".join(["0"] * N_MELS))
    assert ceiling["log_mel"][0] == pytest.approx(0.0)


# ------------------------- 8. 真板子開機/搶佔雜訊（見 reports/BOOT_OUTPUT.md）
#
# `uart_out_lock()`（韌體端）只保護我們自己寫的 `$` 行函式，`ESP_LOG*`
# 完全不走那把鎖，而 mic_task/uart_cmd_task 是 priority 5、app_main（$T 的
# 來源）預設 priority 1——高優先權隨時能在 `print_tof_line()` 的 130+ 次
# printf() 之間插隊。這裡驗證的是「host 端真的擋得住嗎」，不是韌體行為
# 本身（那邊沒有測試能改，這裡才是唯一能驗證的一端）。


def test_esp_log_spliced_at_comma_boundary_mid_tof_is_rejected():
    """模擬最常見的情形：`print_tof_line()` 印到一半（在兩個 `,%d` 之間）被
    priority 5 的 task 搶走，插進一整行 `ESP_LOGI`。韌體端的 log 呼叫沒有
    自己的前導換行，所以它會直接接在已印出的逗號後面，跟被打斷的 $T
    黏成『一行』，殘餘的後半段 $T 資料則變成下一行（不是 `$` 開頭）。"""
    values = TOF_A.split(",")
    head = ",".join(values[:25])          # $T 前段：seq/t_us/dim + 20 個值
    tail = ",".join(values[25:])          # 被截斷丟失的後段
    log_text = "I (98765) bone_mic: a15_perf: mic_task stack headroom = 3200 bytes"
    glued_line = head + "," + log_text    # 韌體端實際會送出的『一行』
    orphan_line = tail                    # 殘餘尾段，下一次 readline() 收到它

    assert parse_line(glued_line) is None          # 少了尾段的值，長度不足
    assert parse_line(orphan_line) is None          # 不是 `$` 開頭，本來就不會被當資料

    p = ProtocolParser()
    p.feed(STATUS_V2)
    e1 = p.feed(glued_line)
    e2 = p.feed(orphan_line)
    assert e1 is None and e2 is None
    assert p.stats.malformed == 1           # glued_line：$ 開頭但解不出來
    assert p.stats.ignored == 1             # orphan_line：不是 $ 開頭
    assert p.stats.parsed == 1              # 只有 $STATUS


def test_esp_log_spliced_mid_digit_mangles_one_value_and_is_rejected():
    """更刁鑽的情形：搶佔剛好發生在單一 `printf(",%d")` 呼叫**之中**
    （UART TX 緩衝滿、驅動要等待時），把一個數值本身從中間切開，
    log 文字直接嵌進那個 token 裡（例如 `88` 被切成 `8` 和 `8...`）。"""
    values = TOF_A.split(",")
    mangled = values.copy()
    mangled[20] = mangled[20][:1] + "I (98765) bone_mic: a15_perf: ...bytes" + mangled[20][1:]
    line = ",".join(mangled)
    assert parse_line(line) is None         # 該值不再是純數字，_i16 直接拒絕


def test_one_zone_short_tof_is_the_dangerous_case_and_is_caught():
    """peer 標為『最危險』的情形：不是明顯亂碼，只是**少一個 zone 的值**
    ——欄位數比 `2*dim` 少 1，是最容易被『看起來像一個完整但比較短的 $T』
    誤判成好資料的形態。`_parse_tof` 的 `len(values) < 2*dim` 檢查必須擋下它，
    不能因為前向相容通則放寬成『至少 N 段』就連這個也放過。"""
    values = TOF_A.split(",")
    short = ",".join(values[:-1])           # 32 個值只剩 31 個
    e = parse_line(short)
    assert e is None, "少一個 zone 的 $T 被當成好資料解析出來了——這是資料完整性的破口"


def test_consecutive_idf_boot_log_lines_never_become_events():
    """開機時 app_main() 被呼叫之前，IDF 自己的元件初始化會印幾十行
    `I (數字) tag: 文字`——這些必須全部算 ignored，一個都不能被誤判成
    畸形 `$` 行（畸形行計數是用來衡量 UART 品質的，開機噪音混進去會
    污染這個指標）。"""
    boot_lines = [
        "I (27) boot: ESP-IDF v6.0.2 2nd stage bootloader",
        "I (30) boot: chip revision: v0.2",
        "I (39) boot: Partition Table:",
        "I (123) cpu_start: Pro cpu up.",
        "I (456) cpu_start: Starting app cpu, entry point is 0x40376b0c",
        "I (789) heap_init: Initializing. RAM available for dynamic allocation:",
        "I (1011) spi_flash: detected chip: generic",
        "I (1213) main_task: Started on CPU0",
        "I (1415) main_task: Calling app_main()",
    ]
    p = ProtocolParser()
    for line in boot_lines:
        assert p.feed(line) is None
    p.feed(STATUS_V2)
    p.feed(TOF_A)
    assert p.stats.ignored == len(boot_lines)
    assert p.stats.malformed == 0
    assert p.stats.parsed == 2


def test_rom_boot_garbage_bytes_do_not_crash_or_become_events():
    """開機最前面那幾行是 Mask ROM 印的，用固定的 ROM baud（不是我們設定的
    460800），host 端用 460800 去讀會整段變成看不懂的位元組——可能根本不是
    合法 UTF-8。`_decode()` 必須吃得下任何 bytes，不能拋例外，也不能剛好被
    誤判成 `$` 開頭的資料行。"""
    garbage_chunks = [
        b"\xaa\x55\x1c\x00\xff\xfe\x00rst:0x1 (POWERON_RESET)\r\n",
        b"\x00\x80\x81\xffSPIWP:0xee\r\n",
        bytes(range(0, 32)),                 # 一整段隨機控制字元
        b"\xff\xff\xff\xc0\xc1entry 0x403c8d20\r\n",   # 非法 UTF-8 續位元組
    ]
    p = ProtocolParser()
    for chunk in garbage_chunks:
        assert p.feed(chunk) is None          # 不拋例外就是及格
    assert p.stats.malformed == 0             # 沒有一段剛好湊成 `$` 開頭還過欄位檢查
    assert p.stats.parsed == 0
