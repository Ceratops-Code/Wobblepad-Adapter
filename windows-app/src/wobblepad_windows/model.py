"""Packet parsing, calibration, mapping, and local settings persistence.

The mapping deliberately mirrors the Android implementation. Platform BLE and
input code remain outside this module so behavior can be tested without radio
hardware or global keyboard side effects.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

SERVICE_UUID = "856b152a-734a-5546-bf2b-ed4898184e12"
CHARACTERISTIC_UUID = "856b152b-734a-5546-bf2b-ed4898184e12"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
DEFAULT_KEY_REPEAT_MS = 333
MIN_KEY_REPEAT_MS = 100
MAX_KEY_REPEAT_MS = 1000
Vector = tuple[float, ...]


class Pose(str, Enum):  # noqa: UP042 - preserve the public Pose string representation
    """The five user-held calibration positions."""

    CENTER = "CENTER"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    UP = "UP"
    DOWN = "DOWN"


@dataclass(frozen=True)
class ControlSettings:
    """Directional gains, radial neutral zone, and arrow repeat interval."""

    left: float = 1.0
    right: float = 1.0
    up: float = 1.0
    down: float = 1.0
    dead_zone: float = 0.08
    repeat_interval_ms: int = DEFAULT_KEY_REPEAT_MS

    def normalized(self) -> ControlSettings:
        return ControlSettings(
            left=min(2.0, max(0.5, self.left)),
            right=min(2.0, max(0.5, self.right)),
            up=min(2.0, max(0.5, self.up)),
            down=min(2.0, max(0.5, self.down)),
            dead_zone=min(0.30, max(0.0, self.dead_zone)),
            repeat_interval_ms=min(MAX_KEY_REPEAT_MS, max(MIN_KEY_REPEAT_MS, int(self.repeat_interval_ms))),
        )

    def as_dict(self) -> dict[str, float]:
        value = self.normalized()
        return {
            "left": value.left,
            "right": value.right,
            "up": value.up,
            "down": value.down,
            "deadZone": value.dead_zone,
            "repeatIntervalMs": value.repeat_interval_ms,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ControlSettings:
        return cls(
            left=float(value.get("left", 1.0)),
            right=float(value.get("right", 1.0)),
            up=float(value.get("up", 1.0)),
            down=float(value.get("down", 1.0)),
            dead_zone=float(value.get("deadZone", 0.08)),
            repeat_interval_ms=int(value.get("repeatIntervalMs", DEFAULT_KEY_REPEAT_MS)),
        ).normalized()


@dataclass(frozen=True)
class Stick:
    x: float = 0.0
    y: float = 0.0
    keys: int = 0


def parse_packet(packet: bytes) -> Vector | None:
    """Decode one observed 20-byte frame into nine signed little-endian fields."""

    if len(packet) != 20 or packet[0] != 0x41 or packet[-1] != 0x42:
        return None
    return tuple(
        float(int.from_bytes(packet[1 + index * 2 : 3 + index * 2], "little", signed=True))
        for index in range(9)
    )


def _average(samples: Sequence[Vector]) -> Vector:
    if not samples:
        raise ValueError("Calibration pose has no samples.")
    return tuple(sum(sample[index] for sample in samples) / len(samples) for index in range(9))


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


@dataclass(frozen=True)
class Calibration:
    """Least-squares projection learned from five labeled board poses."""

    center: Vector
    x_axis: Vector
    y_axis: Vector
    gains: tuple[float, float, float, float]

    def project(self, values: Sequence[float]) -> tuple[float, float]:
        if len(values) != 9:
            raise ValueError("A sensor vector must contain nine values.")
        delta = tuple(values[index] - self.center[index] for index in range(9))
        aa = _dot(self.x_axis, self.x_axis)
        ab = _dot(self.x_axis, self.y_axis)
        bb = _dot(self.y_axis, self.y_axis)
        determinant = aa * bb - ab * ab
        if abs(determinant) < 0.000001:
            raise ValueError("Calibration axes are indistinct.")
        ad = _dot(self.x_axis, delta)
        bd = _dot(self.y_axis, delta)
        return (ad * bb - bd * ab) / determinant, (bd * aa - ad * ab) / determinant

    @classmethod
    def from_samples(cls, samples: Mapping[Pose, Sequence[Vector]]) -> Calibration:
        if any(pose not in samples or not 10 <= len(samples[pose]) <= 500 for pose in Pose):
            raise ValueError("Capture all five poses with at least 10 packets each.")
        for vectors in samples.values():
            for vector in vectors:
                if len(vector) != 9 or any(not math.isfinite(value) or not -32768 <= value <= 32767 for value in vector):
                    raise ValueError("Calibration contains invalid sensor values.")
        means = {pose: _average(samples[pose]) for pose in Pose}
        center = means[Pose.CENTER]
        x_axis = tuple((right - left) / 2 for right, left in zip(means[Pose.RIGHT], means[Pose.LEFT], strict=True))
        y_axis = tuple((up - down) / 2 for up, down in zip(means[Pose.UP], means[Pose.DOWN], strict=True))
        aa = _dot(x_axis, x_axis)
        bb = _dot(y_axis, y_axis)
        ab = _dot(x_axis, y_axis)
        noise = max(
            math.sqrt(
                sum(
                    sum((value[index] - means[pose][index]) ** 2 for index in range(9))
                    for value in samples[pose]
                )
                / len(samples[pose])
            )
            for pose in Pose
        )
        if min(aa, bb) <= max(1.0, 4 * noise) ** 2:
            raise ValueError("The tilts were too small or moved too much. Recapture steady poses.")
        if aa * bb - ab * ab <= 0.01 * aa * bb:
            raise ValueError("Left/right and forward/back were too similar. Recapture distinct directions.")
        base = cls(center, x_axis, y_axis, (1.0, 1.0, 1.0, 1.0))
        right = base.project(means[Pose.RIGHT])[0]
        left = -base.project(means[Pose.LEFT])[0]
        up = base.project(means[Pose.UP])[1]
        down = -base.project(means[Pose.DOWN])[1]
        gains = (right, left, up, down)
        if any(gain <= 0.2 for gain in gains):
            raise ValueError("Center must lie between opposite tilts. Recapture center and directions.")
        return replace(base, gains=gains)


class JoystickMapper:
    """Convert calibrated sensor vectors into smoothed X/Y and arrow bits."""

    def __init__(self, calibration: Calibration, settings: ControlSettings | None = None) -> None:
        self.calibration = calibration
        self.settings = (settings or ControlSettings()).normalized()
        self.filtered_x = 0.0
        self.filtered_y = 0.0
        self.last_time: float | None = None
        self.keys = 0

    def reset(self) -> None:
        self.filtered_x = 0.0
        self.filtered_y = 0.0
        self.last_time = None
        self.keys = 0

    def set_settings(self, value: ControlSettings) -> None:
        self.settings = value.normalized()

    def update(self, values: Sequence[float], now: float | None = None) -> Stick:
        px, py = self.calibration.project(values)
        controls = self.settings
        x = px / self.calibration.gains[0 if px >= 0 else 1] * (controls.right if px >= 0 else controls.left)
        y = py / self.calibration.gains[2 if py >= 0 else 3] * (controls.up if py >= 0 else controls.down)
        current = time.monotonic() if now is None else now
        alpha = 1.0 if self.last_time is None else 1 - math.exp(-max(0.0, current - self.last_time) / 0.060)
        self.filtered_x += alpha * (x - self.filtered_x)
        self.filtered_y += alpha * (y - self.filtered_y)
        self.last_time = current
        radius = math.hypot(self.filtered_x, self.filtered_y)
        scale = 0.0 if radius <= controls.dead_zone else min(1.0, (radius - controls.dead_zone) / (1.0 - controls.dead_zone)) / radius
        output_x = self.filtered_x * scale
        output_y = self.filtered_y * scale
        next_keys = 0
        for bit, value in ((1, -output_x), (2, output_x), (4, output_y), (8, -output_y)):
            threshold = 0.25 if self.keys & bit else 0.35
            if value >= threshold:
                next_keys |= bit
        self.keys = next_keys
        return Stick(output_x, output_y, next_keys)


def encode_samples(samples: Mapping[Pose, Sequence[Vector]]) -> dict[str, list[list[float]]]:
    """Create the portable persisted form after validating all poses."""

    Calibration.from_samples(samples)
    return {pose.value: [list(vector) for vector in samples[pose]] for pose in Pose}


def decode_samples(value: Mapping[str, Any]) -> dict[Pose, list[Vector]]:
    """Load and validate one persisted calibration sample set."""

    samples = {
        pose: [tuple(float(item) for item in vector) for vector in value[pose.value]]
        for pose in Pose
    }
    Calibration.from_samples(samples)
    return samples


def load_state(path: pathlib.Path) -> tuple[ControlSettings, dict[str, dict[Pose, list[Vector]]]]:
    """Read private local settings; invalid files fail closed to defaults."""

    if not path.is_file():
        return ControlSettings(), {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("version") != 1:
            raise ValueError("Unsupported settings version.")
        settings = ControlSettings.from_dict(value.get("controls", {}))
        boards = {
            str(address): decode_samples(board["samples"])
            for address, board in value.get("boards", {}).items()
        }
        return settings, boards
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ControlSettings(), {}


def save_state(
    path: pathlib.Path,
    settings: ControlSettings,
    boards: Mapping[str, Mapping[Pose, Sequence[Vector]]],
) -> None:
    """Atomically persist private settings; the writer owns and removes its temp file."""

    value = {
        "version": 1,
        "controls": settings.as_dict(),
        "boards": {address: {"samples": encode_samples(samples)} for address, samples in boards.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
