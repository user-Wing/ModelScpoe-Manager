from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable, Iterable


MIB = 1024 * 1024


@dataclass(frozen=True)
class SpeedRule:
    start: str
    end: str
    upload_mib: float = 0.0
    download_mib: float = 0.0

    def validated(self) -> "SpeedRule":
        _minutes(self.start)
        _minutes(self.end)
        if self.start == self.end:
            raise ValueError("分时时段的开始和结束时间不能相同")
        if self.upload_mib < 0 or self.download_mib < 0:
            raise ValueError("限速不能小于 0")
        return self


def _minutes(value: str) -> int:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效时间：{value}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"无效时间：{value}")
    return hour * 60 + minute


def _matches(rule: SpeedRule, minute: int) -> bool:
    start, end = _minutes(rule.start), _minutes(rule.end)
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


class TransferPolicy:
    """Resolve base and daily scheduled transfer limits.

    Zero means unlimited. Later matching rules win, which makes overlapping
    schedules predictable and lets the last row act as an override.
    """

    def __init__(
        self,
        enabled: bool = False,
        upload_mib: float = 0.0,
        download_mib: float = 0.0,
        rules: Iterable[SpeedRule] = (),
    ):
        self.enabled = bool(enabled)
        self.upload_mib = max(0.0, float(upload_mib))
        self.download_mib = max(0.0, float(download_mib))
        self.rules = [rule.validated() for rule in rules]

    def limits(self, moment: datetime | None = None) -> tuple[int, int]:
        if not self.enabled:
            return 0, 0
        moment = moment or datetime.now()
        minute = moment.hour * 60 + moment.minute
        upload, download = self.upload_mib, self.download_mib
        for rule in self.rules:
            if _matches(rule, minute):
                upload, download = rule.upload_mib, rule.download_mib
        return int(upload * MIB), int(download * MIB)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "upload_mib": self.upload_mib,
            "download_mib": self.download_mib,
            "rules": [asdict(rule) for rule in self.rules],
        }

    @classmethod
    def from_dict(cls, value: dict | None) -> "TransferPolicy":
        value = value or {}
        rules = []
        for raw in value.get("rules", []):
            try:
                rules.append(SpeedRule(
                    str(raw["start"]), str(raw["end"]),
                    float(raw.get("upload_mib", 0)), float(raw.get("download_mib", 0)),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return cls(
            bool(value.get("enabled", False)),
            float(value.get("upload_mib", 0)),
            float(value.get("download_mib", 0)),
            rules,
        )


class SharedRateLimiter:
    """Thread-safe aggregate limiter whose target may change while running."""

    def __init__(self, rate_supplier: Callable[[], int] | None = None):
        self.rate_supplier = rate_supplier or (lambda: 0)
        self._lock = threading.Lock()
        self._window_started = time.monotonic()
        self._window_bytes = 0

    def throttle(self, amount: int) -> None:
        if amount <= 0:
            return
        rate = max(0, int(self.rate_supplier()))
        if rate <= 0:
            with self._lock:
                self._window_started = time.monotonic()
                self._window_bytes = 0
            return
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._window_started
            if elapsed >= 1.0:
                self._window_started = now
                self._window_bytes = 0
                elapsed = 0.0
            self._window_bytes += amount
            delay = self._window_bytes / rate - elapsed
            if delay > 0:
                time.sleep(delay)
            if time.monotonic() - self._window_started >= 1.0:
                self._window_started = time.monotonic()
                self._window_bytes = 0
