"""B09 — Session metadata 的純邏輯層。

HTTP wiring（路由、狀態碼）是 `B19` 的事（`bridge_server.py` 這輪劃給它）。
這裡只管三件事：

1. session 生命週期（`start` / `end` / `current`），用專屬例外型別
   （`MissingFieldsError` / `SessionAlreadyActiveError` / `NoActiveSessionError`）
   讓 HTTP 層 catch 起來對應轉成 400 / 409 / 你們決定的狀態碼，邏輯本身
   不假設任何 HTTP 細節。
2. `wear_id` 自動遞增但可覆寫——這是 story 的核心難點，實驗 B（D12）
   完全依賴這個欄位正確區分「同次戴」與「跨次戴」。
3. 上一次的設定持久化到 `config/last_session.json` 供前端表單預填。

**目標配戴幾何（`target_distance_mm` / `target_angle_deg`）還沒量測**，
由 `config/session_targets.json` 提供，`E01` 上機量測前一律是 `null`。
**沒有目標值就不能捏造警告**——`target_check` 明確回報 `not_configured`，
`warnings` 保持空陣列，不吐一句看起來煞有介事、其實沒有依據的偏離量。
一個假的「距離正常」比沒有檢查更危險：它會讓人在錯的配戴位置錄完整批資料。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

REQUIRED_FIELDS = ("subject", "mode", "distance_mm", "angle_deg", "ambient")
# wear_id 不在這裡：它會自動遞增，沒填不算「漏填必填欄位」。

DEFAULT_SESSION_TARGETS_PATH = Path(__file__).resolve().parents[2] / "config" / "session_targets.json"

NOT_CONFIGURED_NOTE = "目標幾何未設定（config/session_targets.json），待 E01 量測"


class MissingFieldsError(ValueError):
    """對應 HTTP 400。`fields` 是缺的欄位名稱列表，訊息裡也指名。"""

    def __init__(self, fields):
        self.fields = list(fields)
        super().__init__(f"缺少必填欄位: {', '.join(self.fields)}")


class SessionAlreadyActiveError(RuntimeError):
    """對應 HTTP 409：已經有一個進行中的 session，不能重複 start。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"已經有一個進行中的 session: {session_id}")


class NoActiveSessionError(RuntimeError):
    """`end()` 時沒有進行中的 session。"""


@dataclass
class SessionInfo:
    session_id: str
    subject: str
    wear_id: int
    mode: str
    distance_mm: float
    angle_deg: float
    ambient: str
    notes: str
    started_at: str
    warnings: list = field(default_factory=list)
    target_check: str = "not_configured"  # "not_configured" | "ok" | "warning"
    note: str = ""
    baseline_done: bool = False  # B10：session start 後強制錄 30s baseline，錄好前不能進 trial

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id, "subject": self.subject, "wear_id": self.wear_id,
            "mode": self.mode, "distance_mm": self.distance_mm, "angle_deg": self.angle_deg,
            "ambient": self.ambient, "notes": self.notes, "started_at": self.started_at,
            "warnings": list(self.warnings), "target_check": self.target_check, "note": self.note,
            "baseline_done": self.baseline_done,
        }


def load_session_targets(path=DEFAULT_SESSION_TARGETS_PATH) -> dict:
    """讀 `config/session_targets.json`。檔案不存在或壞掉都當成「全部未設定」
    ——這是保守的一邊：寧可少一個警告，也不要因為讀檔失敗擋掉 session start。"""
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    return {
        "target_distance_mm": data.get("target_distance_mm"),
        "target_angle_deg": data.get("target_angle_deg"),
        "tolerance_distance_mm": data.get("tolerance_distance_mm"),
        "tolerance_angle_deg": data.get("tolerance_angle_deg"),
    }


def _check_required_fields(metadata: dict) -> None:
    missing = [f for f in REQUIRED_FIELDS if metadata.get(f) in (None, "")]
    if missing:
        raise MissingFieldsError(missing)


def _check_one_axis(value, target, tolerance, label, unit) -> Optional[str]:
    """`target`/`tolerance` 任一是 `None` 就不檢查、不捏造警告，回 `None`。"""
    if target is None or tolerance is None:
        return None
    deviation = value - target
    if abs(deviation) > tolerance:
        return f"{label} {value:g}{unit} 偏離目標 {abs(deviation):g}{unit}"
    return None


def _evaluate_targets(metadata: dict, targets: dict):
    """回傳 `(warnings, target_check, note)`。

    `target_check`：兩個維度都沒設定目標值 → `"not_configured"`；
    至少一個維度有設定 → 依那些維度是否超出容許誤差判 `"ok"`/`"warning"`
    （沒設定的維度不影響判定，也不出現在 `warnings` 裡）。
    """
    distance_configured = targets["target_distance_mm"] is not None and targets["tolerance_distance_mm"] is not None
    angle_configured = targets["target_angle_deg"] is not None and targets["tolerance_angle_deg"] is not None

    if not distance_configured and not angle_configured:
        return [], "not_configured", NOT_CONFIGURED_NOTE

    warnings = []
    d_warning = _check_one_axis(
        metadata["distance_mm"], targets["target_distance_mm"], targets["tolerance_distance_mm"],
        "距離", " mm",
    )
    if d_warning:
        warnings.append(d_warning)
    a_warning = _check_one_axis(
        metadata["angle_deg"], targets["target_angle_deg"], targets["tolerance_angle_deg"],
        "角度", "°",
    )
    if a_warning:
        warnings.append(a_warning)

    note = ""
    if not distance_configured:
        note = "距離目標未設定，只檢查了角度"
    elif not angle_configured:
        note = "角度目標未設定，只檢查了距離"

    return warnings, ("warning" if warnings else "ok"), note


class SessionRegistry:
    """一個 bridge_server 行程對應一個實例（in-process 狀態，不假設多行程共用）。"""

    def __init__(self, last_session_path, session_targets: Optional[dict] = None,
                 session_targets_path=DEFAULT_SESSION_TARGETS_PATH):
        self._last_session_path = Path(last_session_path)
        self._session_targets = session_targets if session_targets is not None else load_session_targets(session_targets_path)
        self._current: Optional[SessionInfo] = None
        self._sessions_today: dict = {}

    @property
    def current(self) -> Optional[SessionInfo]:
        """`None` 代表沒有進行中的 session——HTTP 層對應回 204。"""
        return self._current

    def get_prefill(self) -> dict:
        """給前端表單預填：上次的欄位值，`wear_id` 已經 +1。沒有歷史就回空 dict。"""
        last = self._read_last_session()
        if last is None:
            return {}
        prefill = dict(last)
        prefill["wear_id"] = last.get("wear_id", 0) + 1
        return prefill

    def start(self, metadata: dict, now: Optional[datetime] = None) -> SessionInfo:
        if self._current is not None:
            raise SessionAlreadyActiveError(self._current.session_id)

        _check_required_fields(metadata)

        now = now or datetime.now()
        wear_id = metadata.get("wear_id")
        if wear_id is None:
            last = self._read_last_session()
            wear_id = (last.get("wear_id", 0) + 1) if last else 1

        warnings, target_check, note = _evaluate_targets(metadata, self._session_targets)

        info = SessionInfo(
            session_id=self._next_session_id(now),
            subject=metadata["subject"], wear_id=wear_id, mode=metadata["mode"],
            distance_mm=metadata["distance_mm"], angle_deg=metadata["angle_deg"],
            ambient=metadata["ambient"], notes=metadata.get("notes", ""),
            started_at=now.isoformat(),
            warnings=warnings, target_check=target_check, note=note,
        )
        self._current = info
        self._write_last_session(info)
        return info

    def end(self) -> SessionInfo:
        if self._current is None:
            raise NoActiveSessionError("沒有進行中的 session")
        ended = self._current
        self._current = None
        return ended

    def mark_baseline_recorded(self) -> None:
        """B10：baseline 品質檢查通過、trial_000 已經寫進 HDF5 之後呼叫。
        `B11`（trial 狀態機）應該在允許任何非 baseline 的 trial 開始前檢查
        `current.baseline_done`——這裡只負責記錄狀態，不負責擋（B11 的事）。"""
        if self._current is None:
            raise NoActiveSessionError("沒有進行中的 session")
        self._current.baseline_done = True

    def _next_session_id(self, now: datetime) -> str:
        """`<date>_S<NN>`，跟 B07 story 範例的 session 檔名慣例一致
        （`data/sessions/2026-09-01_S01.h5`）。序號只在這個 registry 實例
        的生命週期內遞增——跨 bridge_server 重啟的序號延續不在這個
        story 範圍內（story 沒有要求，也沒有指定持久化的地方）。"""
        date_str = now.strftime("%Y-%m-%d")
        seq = self._sessions_today.get(date_str, 0) + 1
        self._sessions_today[date_str] = seq
        return f"{date_str}_S{seq:02d}"

    def _read_last_session(self) -> Optional[dict]:
        if not self._last_session_path.exists():
            return None
        try:
            return json.loads(self._last_session_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _write_last_session(self, info: SessionInfo) -> None:
        self._last_session_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_session_path.write_text(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
