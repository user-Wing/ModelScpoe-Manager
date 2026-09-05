from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TransferSample:
    timestamp: int
    upload_speed: float = 0.0
    download_speed: float = 0.0
    upload_bytes: int = 0
    download_bytes: int = 0


class TransferStatistics:
    """Second-resolution transfer statistics for the current app session."""

    def __init__(self, started_at: float):
        self.started_at = float(started_at)
        self._samples: dict[int, TransferSample] = {}

    def record_speeds(self, timestamp: float, upload_speed: float, download_speed: float) -> None:
        sample = self._sample(timestamp)
        sample.upload_speed = max(0.0, float(upload_speed))
        sample.download_speed = max(0.0, float(download_speed))

    def add_bytes(self, timestamp: float, *, upload: int = 0, download: int = 0) -> None:
        sample = self._sample(timestamp)
        sample.upload_bytes += max(0, int(upload))
        sample.download_bytes += max(0, int(download))

    def query(self, start: float, end: float) -> list[TransferSample]:
        lower, upper = sorted((int(start), int(end)))
        return [
            self._samples[key]
            for key in sorted(self._samples)
            if lower <= key <= upper
        ]

    def totals(self, start: float, end: float) -> tuple[int, int]:
        samples = self.query(start, end)
        return (
            sum(sample.upload_bytes for sample in samples),
            sum(sample.download_bytes for sample in samples),
        )

    def _sample(self, timestamp: float) -> TransferSample:
        second = int(timestamp)
        return self._samples.setdefault(second, TransferSample(second))


class UploadHealthMonitor:
    """Learn a sustained upload ceiling and detect prolonged degradation."""

    def __init__(
        self,
        learning_duration: float = 3600.0,
        slow_duration: float = 1800.0,
        fast_ratio: float = 0.75,
        slow_ratio: float = 0.5,
        minimum_speed: float = 1024.0,
        inactive_grace: float = 10.0,
    ):
        self.learning_duration = float(learning_duration)
        self.slow_duration = float(slow_duration)
        self.fast_ratio = float(fast_ratio)
        self.slow_ratio = float(slow_ratio)
        self.minimum_speed = float(minimum_speed)
        self.inactive_grace = float(inactive_grace)
        self.learned_speed = 0.0
        self._candidate_speed = 0.0
        self._fast_since: float | None = None
        self._slow_since: float | None = None
        self._inactive_since: float | None = None
        self._reconnect_armed = True

    def update(self, now: float, speed: float, active: bool) -> bool:
        speed = max(0.0, float(speed))
        if not active:
            if self._inactive_since is None:
                self._inactive_since = now
            elif now - self._inactive_since > self.inactive_grace:
                self._fast_since = None
            self._slow_since = None
            return False
        self._inactive_since = None

        self._learn(now, speed)
        if self.learned_speed <= 0:
            return False

        if speed < self.learned_speed * self.slow_ratio:
            if self._slow_since is None:
                self._slow_since = now
            if self._reconnect_armed and now - self._slow_since >= self.slow_duration:
                self._reconnect_armed = False
                return True
        else:
            self._slow_since = None
        return False

    def reset_after_reconnect(self) -> None:
        self._slow_since = None
        self._reconnect_armed = True

    def _learn(self, now: float, speed: float) -> None:
        if speed < self.minimum_speed:
            self._fast_since = None
            return

        baseline = max(self._candidate_speed, self.learned_speed)
        if baseline <= 0 or speed >= baseline * self.fast_ratio:
            self._candidate_speed = max(self._candidate_speed, speed)
            if self._fast_since is None:
                self._fast_since = now
            if now - self._fast_since >= self.learning_duration:
                self.learned_speed = max(self.learned_speed, self._candidate_speed)
        else:
            self._candidate_speed = max(self.learned_speed, speed)
            self._fast_since = now
