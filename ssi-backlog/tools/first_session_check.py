#!/usr/bin/env python3
"""第一筆真實資料錄完，現場一行指令判斷能不能繼續錄。

用法：
    python3 ssi-backlog/tools/first_session_check.py sessions/xxx.h5
    python3 ssi-backlog/tools/first_session_check.py a.h5 b.h5 --target-count 72

背景見 `reports/FIRST_REAL_DATA.md`——這支工具把那份報告裡「一分鐘現場
判斷法」的部分變成一支能跑的程式，讓使用者不用戴著裝置對照七頁報告。

**只分兩類，不要混在一起**（這是這個專案反覆出事的地方）：

- **結構壞掉**（STOP，回傳碼 1）：這個 session 檔本身有問題，
  再錄下去也用不到，例如 baseline 被蓋掉、時間戳往回跳。
- **數字參考**（永遠不是 STOP）：無效 zone 比例、麥克風底噪、削波、
  掉幀數——這些門檻大多是照合成資料訂的（見 `reports/FIRST_REAL_DATA.md`
  第 2、3 項），跟預期不同**可能只是門檻沒校準過**，不代表裝置壞了。

一律用 `analysis/reporting/session_loader.py` 讀檔，不直接開 h5py——
`comparable` 這類 bool attr 直接讀是 `numpy.bool_`，`is True` 會靜默失效，
`session_loader._as_scalar()` 已經處理過這個陷阱。
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[2]  # ssi-backlog/tools/ -> ssi-backlog -> repo root
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from analysis.reporting.session_loader import load_session, SessionData, Trial  # noqa: E402

# E06 需要每個詞錄到的目標筆數。這個數字來自協調者交派任務時給的口頭數字，
# 這支工具沒有另外找出處核對過——如有疑問請跟負責 E06 的人確認，
# 或用 --target-count 覆蓋。
DEFAULT_WORD_TARGET = 72

# `config/quality_thresholds.json` 目前的 noise_floor 綠燈門檻。
# 這個數字精確等於 mock_device.py 靜音時的 RMS（見 FIRST_REAL_DATA.md 第 3 項），
# 只拿來對照顯示，不是這支工具的判定依據。
MOCK_DERIVED_NOISE_FLOOR_GREEN = 300

# 16-bit PCM 滿刻度是 32767；貼近這個值代表可能削波。
CLIP_NEAR_MAX = 32760

BASELINE_LABEL = "_baseline"


class Verdict:
    """一個 session 的檢查結果。`stop`＝結構壞掉，`warn`＝數字或狀態
    值得留意但不阻擋繼續錄，`info`＝純粹的參考數字。"""

    def __init__(self):
        self.stop_reasons = []
        self.warnings = []
        self.info_lines = []

    def stop(self, msg: str) -> None:
        self.stop_reasons.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def info(self, msg: str) -> None:
        self.info_lines.append(msg)

    @property
    def ok(self) -> bool:
        return not self.stop_reasons


def _trial_index(trial: Trial):
    try:
        return int(trial.key.rsplit("_", 1)[-1])
    except ValueError:
        return None


def check_baseline_slot(session: SessionData, v: Verdict) -> None:
    """baseline 佔 `trial_000`，真實錄音要從 `first_trial_idx`（通常是 1）
    開始，避免撞號。若真的撞了，會是**靜默覆寫**——baseline 那個 group
    直接被蓋掉，沒有任何錯誤訊息（見 FIRST_REAL_DATA.md 第 6 項）。
    這裡用「找不找得到 _baseline 這個 label」而不是「trial_000 是不是
    baseline」來判斷——被蓋掉的那種撞號，結果就是完全沒有 _baseline 這個
    trial 了。
    """
    baseline_trials = [t for t in session.trials if t.label == BASELINE_LABEL]
    if not baseline_trials:
        v.stop(f"找不到 label={BASELINE_LABEL!r} 的 trial——baseline 可能被第一筆真實錄音蓋掉了")
        return
    if len(baseline_trials) > 1:
        v.warn(f"有 {len(baseline_trials)} 筆 trial 標記成 {BASELINE_LABEL}，正常應該只有一筆")
    idx = _trial_index(baseline_trials[0])
    if idx != 0:
        v.warn(f"{BASELINE_LABEL} 在 {baseline_trials[0].key}，不是 trial_000，位置不尋常，值得看一眼但不一定是錯")
    real_trials = [t for t in session.trials if t.label != BASELINE_LABEL]
    if not real_trials:
        v.warn("這個 session 除了 baseline 之外沒有任何真實錄音的 trial")
    else:
        v.info(f"baseline 位置正常（{baseline_trials[0].key}），另有 {len(real_trials)} 筆真實錄音的 trial")


def check_monotonic_time(session: SessionData, v: Verdict) -> None:
    """真機斷電/看門狗重置會讓 `esp_timer_get_time()` 歸零重來，`t_us`
    因此往回跳——這個情境合成資料從沒模擬過（見 FIRST_REAL_DATA.md
    第 7 項）。往回跳代表這個 trial 的時間戳已經不可信，跨模態對齊
    （§1.1.1）整條鏈路失去意義，算結構壞掉，不是數字參考。"""
    for t in session.trials:
        for name, arr in (("tof_t_us", t.tof_t_us),):
            if arr is None or arr.size < 2:
                continue
            diffs = np.diff(arr.astype(np.int64))
            n_back = int(np.sum(diffs < 0))
            if n_back:
                v.stop(
                    f"{t.key}（label={t.label!r}）的 {name} 出現 {n_back} 次時間往回跳"
                    "——像是裝置中途重置過，這個 trial 的時間戳不可信"
                )


def check_invalid_zones(session: SessionData, v: Verdict) -> None:
    """`--invalid-zone-rate` 預設 0——mock 平常模式從沒送過無效 zone，
    `valid_zones` 門檻（0.8/0.5）是拿這種資料訂的（FIRST_REAL_DATA.md
    第 4 項）。這裡只印實測比例，不拿門檻判定通過/失敗。"""
    by_sensor = {"A": [], "B": []}
    for t in session.trials:
        if t.label == BASELINE_LABEL:
            continue
        if t.tof_valid_a.size:
            by_sensor["A"].append(1.0 - float(t.tof_valid_a.mean()))
        if t.tof_valid_b.size:
            by_sensor["B"].append(1.0 - float(t.tof_valid_b.mean()))
    if not by_sensor["A"] and not by_sensor["B"]:
        v.info("沒有可計算無效 zone 比例的真實錄音 trial")
        return
    for sensor in ("A", "B"):
        if by_sensor[sensor]:
            avg = float(np.mean(by_sensor[sensor]))
            v.info(
                f"感測器 {sensor} 平均無效 zone 比例：{avg:.1%}"
                "（僅供參考，不代表失敗——valid_zones 門檻是拿假資料訂的）"
            )


def check_mic_noise(session: SessionData, v: Verdict) -> None:
    """baseline 當下已經真的算過 `noise_floor_mu/sigma`（`host/storage/
    baseline.py`），這裡直接用那個數字，不重新估——重新估只是再造一次
    可能不準的猜測。"""
    mu = session.meta.get("noise_floor_mu")
    sigma = session.meta.get("noise_floor_sigma")
    if mu is None:
        v.warn("/meta 沒有 noise_floor_mu——baseline 沒算過噪音門檻")
        return
    v.info(
        f"baseline 實測底噪 RMS ≈ {float(mu):.1f}（σ={float(sigma):.1f}）——"
        f"目前 quality_thresholds.json 的 noise_floor 綠燈門檻是 "
        f"{MOCK_DERIVED_NOISE_FLOOR_GREEN}，那個數字是照模擬器的靜音值訂的，"
        "不是量真麥克風量出來的，跳黃/紅燈不代表裝置有問題"
    )


def check_clipping(session: SessionData, v: Verdict) -> None:
    """mock 的 `MicModel.sample()` 只產生平滑的 rms/peak，peak 從沒有
    連續好幾幀貼著滿刻度——真人講太大聲會是這個形狀，之前沒有任何邏輯
    拿這種資料測過（FIRST_REAL_DATA.md 第 4 項）。這裡只回報數字。"""
    total = 0
    clipped = 0
    for t in session.trials:
        if t.mic_peak is None or t.mic_peak.size == 0:
            continue
        total += t.mic_peak.size
        clipped += int(np.sum(np.abs(t.mic_peak) >= CLIP_NEAR_MAX))
    if total == 0:
        v.info("沒有 mic_peak 資料可以檢查削波")
        return
    ratio = clipped / total
    v.info(
        f"疑似削波幀數：{clipped}/{total}（{ratio:.1%}，貼近 16-bit 滿刻度 "
        f"{CLIP_NEAR_MAX}+）——僅供參考，講太大聲本來就會這樣"
    )


def check_drop_counts(session: SessionData, v: Verdict) -> None:
    """`drop_count` 是必填 trial attr，直接用寫入端已經算好的數字，
    不用另外從 `t_us` 反推掉幀——反推容易因為取樣間隔本來就有抖動而誤判。"""
    drops = [t.attrs.get("drop_count") for t in session.trials
             if t.attrs.get("drop_count") is not None]
    if not drops:
        v.info("trial 沒有 drop_count 資訊")
        return
    total = sum(int(d) for d in drops)
    v.info(
        f"累計 drop_count（掉幀數）：{total}"
        "（僅供參考，錄音 dump 期間本來就會掉幀，見 CONTRACTS.md §1.4）"
    )


def check_vad_chain(session: SessionData, v: Verdict) -> None:
    """VAD 四個時間戳「偵測不到就整個 attr 不寫入」是合法狀態（單一
    trial 沒有也正常），但如果**所有**真實錄音的 trial 都沒有，代表整條
    VAD 鏈路可能根本沒接上，而不是「剛好都沒偵測到」——這兩者用單一
    trial 分不出來，只能看整批。"""
    real_trials = [t for t in session.trials if t.label != BASELINE_LABEL]
    if not real_trials:
        return

    has_vad = sum(
        1 for t in real_trials
        if t.attrs.get("voice_onset_us") is not None or t.attrs.get("lip_onset_us") is not None
    )
    if has_vad == 0:
        v.warn(
            f"{len(real_trials)} 筆真實錄音全部沒有 voice_onset_us/lip_onset_us——"
            "如果裡面有正常講話的錄音，這代表 VAD 鏈路可能沒接上，不是「剛好都沒偵測到」"
        )
    else:
        v.info(f"VAD 有值的 trial：{has_vad}/{len(real_trials)}")

    comparable_count = sum(1 for t in real_trials if t.attrs.get("comparable") is True)
    v.info(f"comparable=True 的 trial：{comparable_count}/{len(real_trials)}（能拿去平均唇動領先量的筆數）")

    if session.meta.get("energy_mu") is None:
        v.warn(
            "/meta 沒有 energy_mu/energy_sigma——唇動偵測的能量門檻會退回用估的，"
            "B16 量到估的偏嚴約 23%，lip_onset_us 會系統性偏晚"
        )
    else:
        v.info("/meta 有 energy_mu/energy_sigma（baseline 當下量的真實門檻，唇動偵測不會用估的）")


def check_word_progress(session: SessionData, v: Verdict, target: int) -> None:
    counts = Counter(t.label for t in session.trials if t.label != BASELINE_LABEL)
    if not counts:
        v.info("目前沒有任何真實詞的錄音")
        return
    lines = [f"{label}: {n}/{target}" for label, n in sorted(counts.items())]
    v.info(
        f"錄製進度（目標數量預設 {target}，依 E06 story，未經本工具核對，"
        f"可用 --target-count 覆蓋）：{'、'.join(lines)}"
    )


CHECKS = (
    check_baseline_slot,
    check_monotonic_time,
    check_invalid_zones,
    check_mic_noise,
    check_clipping,
    check_drop_counts,
    check_vad_chain,
)


def check_session(path, target: int) -> Verdict:
    v = Verdict()
    try:
        session = load_session(path)
    except Exception as exc:  # noqa: BLE001 — 讀檔失敗本身就是要回報的結果
        v.stop(f"讀檔失敗：{exc}")
        return v
    for check in CHECKS:
        check(session, v)
    check_word_progress(session, v, target)
    return v


def format_report(path, v: Verdict) -> str:
    lines = [f"=== {path} ==="]
    lines.append("✅ 結構檢查通過" if v.ok else "🔴 停：" + "；".join(v.stop_reasons))
    if v.warnings:
        lines.append("")
        lines.append("⚠️ 需要注意（不一定要停，但建議看一眼）：")
        lines.extend(f"  - {w}" for w in v.warnings)
    if v.info_lines:
        lines.append("")
        lines.append("📋 數字參考（僅供參考，不是判定標準）：")
        lines.extend(f"  - {i}" for i in v.info_lines)
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="第一筆真實資料的現場健檢——錄完立刻跑，決定能不能繼續錄"
    )
    parser.add_argument("sessions", nargs="+", help="一或多個 session .h5 檔")
    parser.add_argument(
        "--target-count", type=int, default=DEFAULT_WORD_TARGET,
        help=f"每個詞要錄到的目標筆數（預設 {DEFAULT_WORD_TARGET}）",
    )
    args = parser.parse_args(argv)

    results = [(path, check_session(path, args.target_count)) for path in args.sessions]
    overall_ok = all(v.ok for _, v in results)

    print("=" * 60)
    if overall_ok:
        print("✅ 資料看起來可用，可以繼續錄")
    else:
        all_stops = [reason for _, v in results for reason in v.stop_reasons]
        print("🔴 停：" + "；".join(all_stops))
    print("=" * 60)
    print()
    for path, v in results:
        print(format_report(path, v))
        print()

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
