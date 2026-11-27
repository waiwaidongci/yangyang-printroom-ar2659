"""回测断点续跑的指纹与持久化。

逐日判定结果按「台账读数 + 滞后阶数 + 回测窗口」的指纹缓存。
同一输入重跑时直接取回已判日期，只有未判日期需要重新拟合计算。
"""

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass


def backtest_fingerprint(readings: dict[int, float], order: int, first: int, last: int) -> str:
    payload = {
        "readings": [[day, readings[day]] for day in sorted(readings)],
        "order": order,
        "window": [first, last],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SavedBacktestDay:
    """可 JSON 序列化的逐日判定快照。"""

    day: int
    sample_count: int
    status: str
    actual: float | None
    persistence: float | None
    raw_humidity: float | None
    humidity: float | None
    applied_change: float | None
    boundary: str | None

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "sample_count": self.sample_count,
            "status": self.status,
            "actual": self.actual,
            "persistence": self.persistence,
            "raw_humidity": self.raw_humidity,
            "humidity": self.humidity,
            "applied_change": self.applied_change,
            "boundary": self.boundary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SavedBacktestDay":
        return cls(
            day=int(data["day"]),
            sample_count=int(data["sample_count"]),
            status=data["status"],
            actual=data["actual"],
            persistence=data["persistence"],
            raw_humidity=data["raw_humidity"],
            humidity=data["humidity"],
            applied_change=data["applied_change"],
            boundary=data["boundary"],
        )


class CheckpointStore:
    def load(self, key: str) -> list[SavedBacktestDay]:
        raise NotImplementedError

    def save(self, key: str, entries: list[SavedBacktestDay]) -> None:
        raise NotImplementedError


class InMemoryCheckpointStore(CheckpointStore):
    """进程内断点存储，供测试与无状态调用使用。"""

    def __init__(self) -> None:
        self._data: dict[str, list[SavedBacktestDay]] = {}

    def load(self, key: str) -> list[SavedBacktestDay]:
        return list(self._data.get(key, ()))

    def save(self, key: str, entries: list[SavedBacktestDay]) -> None:
        self._data[key] = list(entries)


class JsonFileCheckpointStore(CheckpointStore):
    """每个指纹一个 JSON 文件，原子替换，跨进程中断后可续跑。"""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self.directory, f"backtest-{key}.json")

    def load(self, key: str) -> list[SavedBacktestDay]:
        try:
            with open(self._path(key), encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return []
        return [SavedBacktestDay.from_dict(item) for item in data]

    def save(self, key: str, entries: list[SavedBacktestDay]) -> None:
        payload = [entry.to_dict() for entry in entries]
        target = self._path(key)
        fd, temporary = tempfile.mkstemp(prefix=".backtest-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
