"""B09 — Session metadata 的純邏輯層。

HTTP wiring（路由、狀態碼）是 `B19` 的事（見 `esp-mask-test-ad` 的交接安排，
`bridge_server.py` 這輪劃給它）。這裡只管三件事：

1. session 生命週期（`start` / `end` / `current`），用專屬例外型別
   （`MissingFieldsError` / `SessionAlreadyActiveError` / `NoActiveSessionError`）
   讓 HTTP 層 catch 起來對應轉成 400 / 409 / 你們決定的狀態碼，邏輯本身
   不假設任何 HTTP 細節。
2. `wear_id` 自動遞增但可覆寫——這是 story 的核心難點，實驗 B（D12）
   完全依賴這個欄位正確區分「同次戴」與「跨次戴」。
3. 上一次的設定持久化到 `config/last_session.json` 供前端表單預填。

**target_distance_mm / target_angle_deg 沒有在任何 story 或 CONTRACTS.md
裡定義具體數值**（只有 story 裡一句示意用的「距離 25 mm 偏離目標 17 mm」）。
這裡做成建構子的可覆寫參數，不寫死也不猜一個「正式」數字——已經在
CONTRACTS.md 記一筆請 T 軌/A 軌之後補上真正的量測值。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

REQUIRED_FIELDS = ("subject", "mode", "distance_mm", "angle_deg", "ambient")
# wear_id 不在這裡：它會自動遞增，沒填不算「漏填必填欄位」。

DEFAULT_TARGET_DISTANCE_MM = 30.0
DEFAULT_TARGET_ANGLE_DEG = 0.0
DEFAULT_DISTANCE_TOLERANCE_MM = 5.0
DEFAULT_ANGLE_TOLERANCE_DEG = 10.0


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

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id, "subject": self.subject, "wear_id": self.wear_id,
            "mode": self.mode, "distance_mm": self.distance_mm, "angle_deg": self.angle_deg,
            "ambient": self.ambient, "notes": self.notes, "started_at": self.started_at,
            "warnings": list(self.warnings),
        }


def _check_required_fields(metadata: dict) -> None:
    missing = [f for f in REQUIRED_FIELDS if metadata.get(f) in (None, "")]
    if missing:
        raise MissingFieldsError(missing)


def _compute_warnings(metadata, target_distance_mm, target_angle_deg,
                       distance_tolerance_mm, angle_tolerance_deg) -> list:
    warnings = []
    d_dev = metadata["distance_mm"] - target_distance_mm
    if abs(d_dev) > distance_tolerance_mm:
        warnings.append(f"距離 {metadata['distance_mm']:g} mm 偏離目標 {abs(d_dev):g} mm")
    a_dev = metadata["angle_deg"] - target_angle_deg
    if abs(a_dev) > angle_tolerance_deg:
        warnings.append(f"角度 {metadata['angle_deg']:g}° 偏離目標 {abs(a_dev):g}°")
    return warnings


class SessionRegistry:
    """一個 bridge_server 行程對應一個實例（in-process 狀態，不假設多行程共用）。"""

    def __init__(self, last_session_path,
                 target_distance_mm: float = DEFAULT_TARGET_DISTANCE_MM,
                 target_angle_deg: float = DEFAULT_TARGET_ANGLE_DEG,
                 distance_tolerance_mm: float = DEFAULT_DISTANCE_TOLERANCE_MM,
                 angle_tolerance_deg: float = DEFAULT_ANGLE_TOLERANCE_DEG):
        self._last_session_path = Path(last_session_path)
        self._target_distance_mm = target_distance_mm
        self._target_angle_deg = target_angle_deg
        self._distance_tolerance_mm = distance_tolerance_mm
        self._angle_tolerance_deg = angle_tolerance_deg
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

        info = SessionInfo(
            session_id=self._next_session_id(now),
            subject=metadata["subject"], wear_id=wear_id, mode=metadata["mode"],
            distance_mm=metadata["distance_mm"], angle_deg=metadata["angle_deg"],
            ambient=metadata["ambient"], notes=metadata.get("notes", ""),
            started_at=now.isoformat(),
            warnings=_compute_warnings(
                metadata, self._target_distance_mm, self._target_angle_deg,
                self._distance_tolerance_mm, self._angle_tolerance_deg,
            ),
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
