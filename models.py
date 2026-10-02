"""RIN 2008 calculations and SQLite persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from geopy.distance import geodesic

DEFAULT_PARAMS = {
    "PKW": {"a": [0.18, 0.21, 0.25, 0.31, 0.39], "b": [-0.676] * 5, "c": [0.0083, 0.0089, 0.0096, 0.0104, 0.0115]},
    "OEV": {"a": [0.19, 0.22, 0.26, 0.32, 0.40], "b": [-0.5] * 5, "c": [0.0031, 0.0037, 0.0044, 0.0052, 0.0063]},
    "IOE": {"a": [0.18, 0.21, 0.25, 0.31, 0.39], "b": [-0.59] * 5, "c": [0.0069, 0.0075, 0.0082, 0.0090, 0.0100]},
}


@dataclass
class Settings:
    mode: str = "PKW"
    gps_interval: float = 2.0
    min_accuracy: float = 5.0
    max_current_speed: float = 350.0
    max_straight_speed: float = 200.0
    moving_cutoff: float = 5.0
    chart_min_x: float = 0.0
    chart_default_max_x: float = 50.0
    chart_min_y: float = 0.0
    chart_default_max_y: float = 60.0
    chart_x_offset: float = 10.0
    chart_y_offset: float = 10.0
    chart_x_step: float = 10.0
    chart_y_step: float = 10.0
    api_endpoint: str = "https://rin.isv.uni-stuttgart.de/api/v1/"
    params: dict[str, dict[str, list[float]]] = field(
        default_factory=lambda: json.loads(json.dumps(DEFAULT_PARAMS))
    )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Settings:
        defaults = cls()
        for key, item in value.items():
            if hasattr(defaults, key):
                setattr(defaults, key, item)
        return defaults

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LocationPoint:
    session_id: str
    timestamp_ms: int
    latitude: float
    longitude: float
    accuracy_m: float
    instant_speed_kmh: float
    straight_speed_kmh: float
    straight_distance_km: float
    total_distance_km: float
    saq: str


@dataclass
class SAQResult:
    grade: int
    letter: str


@dataclass
class TripMetrics:
    total_distance_km: float = 0.0
    straight_distance_km: float = 0.0
    current_speed_kmh: float = 0.0
    straight_speed_kmh: float = 0.0
    start_time_ms: int = 0
    moving_time_ms: int = 0


def wgs84_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates geodesic distance in meters on the WGS-84 reference ellipsoid
    using geopy (Karney's geodesic algorithm).
    """
    if None in (lat1, lon1, lat2, lon2):
        return 0.0
    if lat1 == lat2 and lon1 == lon2:
        return 0.0
    return float(geodesic((lat1, lon1), (lat2, lon2)).meters)


def calc_distance_km(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """Calculates geodesic distance in kilometers on the WGS-84 reference ellipsoid
    using geopy (Karney's geodesic algorithm).
    """
    if None in (lat_a, lon_a, lat_b, lon_b):
        return 0.0
    if lat_a == lat_b and lon_a == lon_b:
        return 0.0
    return float(geodesic((lat_a, lon_a), (lat_b, lon_b)).km)


def haversine_km(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    return calc_distance_km(lat_a, lon_a, lat_b, lon_b)


def saq_for(distance_km: float, straight_speed_kmh: float, settings: Settings) -> tuple[int, str]:
    if distance_km < 0.01 or straight_speed_kmh <= 0:
        return 1, "A"
    parameters = settings.params.get(settings.mode, DEFAULT_PARAMS["PKW"])
    limits = []
    for index in range(5):
        denominator = parameters["a"][index] * (distance_km ** parameters["b"][index]) + parameters["c"][index]
        limits.append(1 / denominator if denominator else 0)
    for index, limit in enumerate(limits, start=1):
        if straight_speed_kmh >= limit:
            return index, "ABCDEF"[index - 1]
    return 6, "F"


def get_saq(dist_km: float, v_luft: float, mode: str, params: dict) -> dict[str, Any]:
    temp_settings = Settings(mode=mode, params=params)
    grade, letter = saq_for(dist_km, v_luft, temp_settings)
    return {"grade": grade, "letter": letter}


def format_duration(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def timestamp_ms(value: Any) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.now().timestamp() * 1000)


class TripTracker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.reset()

    def reset(self) -> None:
        self.session_id: str | None = None
        self.started_ms = 0
        self.paused_ms = 0
        self.pause_start_ms = 0
        self.is_paused = False
        self.start_latitude: float | None = None
        self.start_longitude: float | None = None
        self.last_latitude: float | None = None
        self.last_longitude: float | None = None
        self.last_timestamp_ms = 0
        self.last_speed_kmh = 0.0
        self.total_distance_km = 0.0
        self.straight_distance_km = 0.0
        self.current_speed_kmh = 0.0
        self.straight_speed_kmh = 0.0
        self.moving_time_ms = 0
        self.window: list[tuple[float, float, int]] = []

    def start(self, start_ms: int, session_id: str | None = None) -> str:
        self.reset()
        if not session_id:
            stamp = datetime.fromtimestamp(start_ms / 1000).strftime("%Y%m%d_%H%M%S")
            session_id = f"rin08-{stamp}"
        self.session_id = session_id
        self.started_ms = start_ms
        return self.session_id

    def pause(self, current_ms: int) -> None:
        self.is_paused = True
        self.pause_start_ms = current_ms

    def resume(self, current_ms: int) -> None:
        if self.pause_start_ms > 0:
            self.paused_ms += max(0, current_ms - self.pause_start_ms)
            self.pause_start_ms = 0
        self.is_paused = False
        self.last_timestamp_ms = current_ms

    def effective_elapsed_ms(self, current_ms: int) -> int:
        if not self.started_ms:
            return 0
        total = current_ms - self.started_ms
        paused = self.paused_ms
        if self.is_paused and self.pause_start_ms:
            paused += (current_ms - self.pause_start_ms)
        return max(0, total - paused)

    def load_session(self, session_id: str, points: list[LocationPoint]) -> None:
        self.reset()
        self.session_id = session_id
        if not points:
            return
        first = points[0]
        last = points[-1]
        self.started_ms = first.timestamp_ms
        self.start_latitude = first.latitude
        self.start_longitude = first.longitude
        self.last_latitude = last.latitude
        self.last_longitude = last.longitude
        self.last_timestamp_ms = last.timestamp_ms
        self.last_speed_kmh = last.instant_speed_kmh
        self.total_distance_km = last.total_distance_km
        self.straight_distance_km = last.straight_distance_km
        self.current_speed_kmh = last.instant_speed_kmh
        self.straight_speed_kmh = last.straight_speed_kmh
        cutoff = self.settings.moving_cutoff
        moving_count = sum(1 for p in points if p.instant_speed_kmh >= cutoff)
        self.moving_time_ms = int(moving_count * self.settings.gps_interval * 1000)

    @property
    def metrics(self) -> TripMetrics:
        return TripMetrics(
            total_distance_km=self.total_distance_km,
            straight_distance_km=self.straight_distance_km,
            current_speed_kmh=self.current_speed_kmh,
            straight_speed_kmh=self.straight_speed_kmh,
            start_time_ms=self.started_ms,
            moving_time_ms=self.moving_time_ms,
        )

    def current_saq(self) -> SAQResult:
        grade, letter = saq_for(self.straight_distance_km, self.straight_speed_kmh, self.settings)
        return SAQResult(grade=grade, letter=letter)

    def ingest(
        self,
        latitude: float,
        longitude: float,
        accuracy: float | None = None,
        accuracy_m: float | None = None,
        timestamp: int | None = None,
        timestamp_ms: int | None = None,
    ) -> tuple[LocationPoint | None, str]:
        if self.is_paused:
            return None, "Aufzeichnung pausiert (Punkt ignoriert)"

        actual_acc = accuracy_m if accuracy_m is not None else (accuracy or 0.0)
        actual_ts = timestamp_ms if timestamp_ms is not None else (timestamp or int(datetime.now().timestamp() * 1000))

        if actual_acc > self.settings.min_accuracy:
            return None, f"Punkt verworfen: Genauigkeit {actual_acc:.1f} m"

        if self.start_latitude is None:
            self.start_latitude = self.last_latitude = latitude
            self.start_longitude = self.last_longitude = longitude
            self.last_timestamp_ms = actual_ts
            self.last_speed_kmh = 0.0
            self.window = [(latitude, longitude, actual_ts)]
            return None, "Startpunkt übernommen"

        elapsed_ms = actual_ts - self.last_timestamp_ms
        if elapsed_ms <= 0:
            return None, "Punkt mit ungültigem Zeitstempel verworfen"

        elapsed_s = elapsed_ms / 1000.0
        elapsed_hours = elapsed_ms / 3_600_000.0

        # High-precision WGS-84 ellipsoidal distance (meters & km)
        step_dist_m = wgs84_distance_m(self.last_latitude, self.last_longitude, latitude, longitude)
        step_dist_km = step_dist_m / 1000.0
        instant_speed = (step_dist_km / elapsed_hours) if elapsed_hours > 0 else 0.0

        # 1. GPS Spike Rejection (Absolute Speed Limit)
        if instant_speed > self.settings.max_current_speed:
            return None, f"GPS-Spike verworfen: {instant_speed:.1f} km/h"

        # 2. Acceleration / Jump Rejection (Multipath GPS jump)
        # Rejects non-physical sudden teleport jumps (> 8 m/s² acceleration at high speeds)
        if self.last_speed_kmh > 0 and instant_speed > 25.0 and elapsed_s > 0:
            delta_v_m_s = abs(instant_speed - self.last_speed_kmh) / 3.6
            accel = delta_v_m_s / elapsed_s
            if accel > 8.0:
                return None, f"GPS-Sprung verworfen: Beschleunigung {accel:.1f} m/s²"

        # 3. Stationary Deadband / Jitter Filter (Komoot / Bergfex Standard)
        # Prevents phantom distance accumulation during stops (e.g. at traffic lights)
        deadband_m = max(3.0, actual_acc * 0.7)
        is_stationary_jitter = (step_dist_m < deadband_m) and (instant_speed < self.settings.moving_cutoff)

        if is_stationary_jitter:
            effective_step_km = 0.0
            instant_speed = 0.0
        else:
            effective_step_km = step_dist_km
            self.total_distance_km += effective_step_km
            if instant_speed >= self.settings.moving_cutoff:
                self.moving_time_ms += elapsed_ms

        # 4. Straight-line distance & speed from trip start (WGS-84)
        straight_distance = calc_distance_km(self.start_latitude, self.start_longitude, latitude, longitude)
        effective_ms = self.effective_elapsed_ms(actual_ts)
        total_hours = effective_ms / 3_600_000.0
        straight_speed = (straight_distance / total_hours) if total_hours > 0 else 0.0
        if straight_speed > self.settings.max_straight_speed:
            return None, f"V-Luft-Peak verworfen: {straight_speed:.1f} km/h"

        self.straight_distance_km = straight_distance
        self.straight_speed_kmh = straight_speed
        self.last_speed_kmh = instant_speed
        self.last_latitude = latitude
        self.last_longitude = longitude
        self.last_timestamp_ms = actual_ts

        # 5. Moving window smoothed current speed (last 5 points)
        self.window.append((latitude, longitude, actual_ts))
        self.window = self.window[-5:]
        if len(self.window) > 1:
            span_hours = (self.window[-1][2] - self.window[0][2]) / 3_600_000.0
            travelled = sum(
                calc_distance_km(self.window[i - 1][0], self.window[i - 1][1], self.window[i][0], self.window[i][1])
                for i in range(1, len(self.window))
            )
            self.current_speed_kmh = (travelled / span_hours) if span_hours > 0 else 0.0
        else:
            self.current_speed_kmh = instant_speed

        _, letter = saq_for(self.straight_distance_km, self.straight_speed_kmh, self.settings)
        point = LocationPoint(
            self.session_id or "",
            actual_ts,
            latitude,
            longitude,
            actual_acc,
            instant_speed,
            straight_speed,
            self.straight_distance_km,
            self.total_distance_km,
            letter,
        )
        return point, "Punkt gespeichert"


class SQLiteStore:
    def __init__(self, filename: str = "rin08_live.sqlite3"):
        self.path = Path(filename)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS app_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, started_ms INTEGER NOT NULL, mode TEXT)")
            connection.execute("""CREATE TABLE IF NOT EXISTS location_points (
                session_id TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, latitude REAL NOT NULL,
                longitude REAL NOT NULL, accuracy_m REAL NOT NULL, instant_speed_kmh REAL NOT NULL,
                straight_speed_kmh REAL NOT NULL, straight_distance_km REAL NOT NULL,
                total_distance_km REAL NOT NULL, saq TEXT NOT NULL)""")

    def load_settings(self) -> Settings:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM app_settings WHERE name = 'settings'").fetchone()
        return Settings.from_dict(json.loads(row[0])) if row else Settings()

    def save_settings(self, settings: Settings) -> None:
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO app_settings (name, value) VALUES ('settings', ?)", (json.dumps(settings.to_dict()),))

    def start_session(self, session_id: str, started_or_mode: Any = 0) -> None:
        started_ms = started_or_mode if isinstance(started_or_mode, int) else int(datetime.now().timestamp() * 1000)
        mode = started_or_mode if isinstance(started_or_mode, str) else "PKW"
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO sessions (id, started_ms, mode) VALUES (?, ?, ?)", (session_id, started_ms, mode))

    def add_point(self, point: LocationPoint) -> None:
        with self._connect() as connection:
            connection.execute("INSERT INTO location_points VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", tuple(asdict(point).values()))

    def points_for(self, session_id: str | None) -> list[LocationPoint]:
        if not session_id:
            return []
        with self._connect() as connection:
            rows = connection.execute("SELECT session_id, timestamp_ms, latitude, longitude, accuracy_m, instant_speed_kmh, straight_speed_kmh, straight_distance_km, total_distance_km, saq FROM location_points WHERE session_id = ? ORDER BY timestamp_ms", (session_id,)).fetchall()
        return [LocationPoint(*row) for row in rows]

    def all_points(self) -> list[LocationPoint]:
        with self._connect() as connection:
            rows = connection.execute("SELECT session_id, timestamp_ms, latitude, longitude, accuracy_m, instant_speed_kmh, straight_speed_kmh, straight_distance_km, total_distance_km, saq FROM location_points ORDER BY timestamp_ms ASC").fetchall()
        return [LocationPoint(*row) for row in rows]

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT s.id, s.started_ms, s.mode,
                       COUNT(p.timestamp_ms) as point_count,
                       COALESCE(MAX(p.total_distance_km), 0.0) as total_dist,
                       COALESCE(MAX(p.straight_distance_km), 0.0) as straight_dist,
                       COALESCE(MAX(p.straight_speed_kmh), 0.0) as max_v_luft,
                       COALESCE(MIN(p.timestamp_ms), s.started_ms) as first_ts,
                       COALESCE(MAX(p.timestamp_ms), s.started_ms) as last_ts
                FROM sessions s
                LEFT JOIN location_points p ON s.id = p.session_id
                GROUP BY s.id
                ORDER BY s.started_ms DESC
            """).fetchall()
        results = []
        for row in rows:
            results.append({
                "id": row[0],
                "started_ms": row[1],
                "mode": row[2] or "PKW",
                "point_count": row[3],
                "total_distance_km": row[4],
                "straight_distance_km": row[5],
                "max_v_luft_kmh": row[6],
                "duration_ms": max(0, row[8] - row[7]),
            })
        return results

    def clear_session(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM location_points WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))