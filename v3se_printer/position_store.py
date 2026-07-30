from __future__ import annotations

import json
import math
import os
from pathlib import Path
import threading


Coordinates = tuple[float, float, float]


class PrinterPositionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._positions = self._load()

    def get(self, port: str) -> Coordinates | None:
        self._require_port(port)
        with self._lock:
            return self._positions.get(port)

    def remember(self, port: str, coordinates: Coordinates) -> None:
        self._require_port(port)
        position = tuple(self._number(value) for value in coordinates)
        if len(position) != 3:
            raise ValueError("Remembered position must contain X, Y, and Z")
        with self._lock:
            updated = dict(self._positions)
            updated[port] = position
            self._write(updated)
            self._positions = updated

    def forget(self, port: str) -> None:
        self._require_port(port)
        with self._lock:
            if port not in self._positions:
                return
            updated = dict(self._positions)
            del updated[port]
            self._write(updated)
            self._positions = updated

    def _load(self) -> dict[str, Coordinates]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if not isinstance(payload, dict) or set(payload) != {"version", "printers"}:
            raise ValueError("Malformed printer position store")
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise ValueError("Unsupported printer position store version")
        records = payload["printers"]
        if not isinstance(records, dict):
            raise ValueError("Malformed printer position records")
        positions: dict[str, Coordinates] = {}
        for port, record in records.items():
            self._require_port(port)
            if not isinstance(record, dict) or set(record) != {"x", "y", "z"}:
                raise ValueError(f"Malformed remembered position for {port}")
            positions[port] = tuple(self._number(record[axis]) for axis in ("x", "y", "z"))
        return positions

    def _write(self, positions: dict[str, Coordinates]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = {
            "version": 1,
            "printers": {
                port: {"x": position[0], "y": position[1], "z": position[2]}
                for port, position in sorted(positions.items())
            },
        }
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    @staticmethod
    def _require_port(port: object) -> None:
        if not isinstance(port, str) or not port or port != port.strip():
            raise ValueError("Printer port must be a non-empty string")

    @staticmethod
    def _number(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Remembered coordinates must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Remembered coordinates must be finite numbers")
        return number
