#!/usr/bin/env python3
"""Suggest Swarm check-ins from confirmed APRS stops.

The process listens only for numeric SSIDs on the configured base callsign,
asks Foursquare for nearby places after a confirmed stop, and sends a Pushover
notification with a Swarm deep link. It never creates a Swarm check-in itself.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


APP_NAME = "aprs-swarm-checkin"
DEFAULT_CONFIG = Path.home() / ".config" / "aprs-swarm-checkin.env"
APRS_SERVER = "noam.aprs2.net"
APRS_PORT = 14580
SIMULATION_BASE_CALLSIGN = "K1TEST"
STOP_RADIUS_METERS = 100.0
DEPARTURE_RADIUS_METERS = 250.0
STOP_CONFIRMATION = timedelta(minutes=5)
REMINDER_COOLDOWN = timedelta(hours=1)
LOOKUP_FAILURE_COOLDOWN = timedelta(minutes=5)
FSQ_ENDPOINT = "https://places-api.foursquare.com/places/search"
FSQ_API_VERSION = "2025-06-17"
FSQ_RESULT_LIMIT = 5


class JsonFormatter(logging.Formatter):
    """Small JSON logger that avoids adding request credentials to output."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        for key in ("callsign", "reason", "place", "distance_m", "status"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_logging() -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


LOGGER = configure_logging()


def log(event: str, **extra: Any) -> None:
    LOGGER.info(event, extra=extra)


def load_env_file(path: Path) -> dict[str, str]:
    """Load simple KEY=VALUE configuration without printing its contents."""
    if not path.is_file():
        raise ValueError(f"Configuration file does not exist: {path}")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid configuration line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid configuration key on line {line_number}")
        values[key] = value.strip()
    return values


@dataclass(frozen=True)
class Settings:
    fsq_bearer_token: str
    pushover_app_token: str
    pushover_user_key: str
    pushover_devices: tuple[str, ...]
    aprs_base_callsign: str
    config_path: Path

    @classmethod
    def from_file(cls, path: Path) -> "Settings":
        values = load_env_file(path)
        required = (
            "FSQ_BEARER_TOKEN",
            "PUSHOVER_APP_TOKEN",
            "PUSHOVER_USER_KEY",
            "PUSHOVER_DEVICES",
            "APRS_BASE_CALLSIGN",
        )
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise ValueError("Missing configuration values: " + ", ".join(missing))
        devices = tuple(item.strip() for item in values["PUSHOVER_DEVICES"].split(",") if item.strip())
        if not devices:
            raise ValueError("PUSHOVER_DEVICES must name at least one device")
        return cls(
            fsq_bearer_token=values["FSQ_BEARER_TOKEN"],
            pushover_app_token=values["PUSHOVER_APP_TOKEN"],
            pushover_user_key=values["PUSHOVER_USER_KEY"],
            pushover_devices=devices,
            aprs_base_callsign=normalize_base_callsign(values["APRS_BASE_CALLSIGN"]),
            config_path=path,
        )


@dataclass(frozen=True)
class Position:
    callsign: str
    latitude: float
    longitude: float
    received_at: datetime


@dataclass(frozen=True)
class PlaceCandidate:
    fsq_place_id: str
    name: str
    category: str
    distance_m: float

    @property
    def swarm_url(self) -> str:
        return f"swarm://checkins/add?venueId={self.fsq_place_id}"


@dataclass
class StationState:
    anchor: Position | None = None
    last_position: Position | None = None
    confirmed_at: datetime | None = None
    last_lookup_at: datetime | None = None
    last_notified_at: datetime | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_base_callsign(value: str) -> str:
    """Validate a base callsign; SSIDs belong only on received packets."""
    callsign = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,6}", callsign):
        raise ValueError("APRS_BASE_CALLSIGN must be a base callsign without an SSID")
    return callsign


def is_tracked_callsign(callsign: str, base_callsign: str) -> bool:
    """Accept numeric mobile SSIDs 1 through 15 for one configured base call."""
    base_callsign = normalize_base_callsign(base_callsign)
    pattern = rf"{re.escape(base_callsign)}-(?:[1-9]|1[0-5])"
    return bool(re.fullmatch(pattern, callsign.strip().upper()))


def aprs_filter(base_callsign: str) -> str:
    """Use the broad buddy filter and enforce the precise SSID rule locally."""
    return f"b/{normalize_base_callsign(base_callsign)}*"


def haversine_meters(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> float:
    radius = 6_371_000.0
    lat_a, lon_a, lat_b, lon_b = map(math.radians, (latitude_a, longitude_a, latitude_b, longitude_b))
    delta_lat = lat_b - lat_a
    delta_lon = lon_b - lon_a
    term = math.sin(delta_lat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(term))


def packet_to_position(
    packet: dict[str, Any], base_callsign: str, received_at: datetime | None = None
) -> Position | None:
    callsign = str(packet.get("from", "")).upper()
    if not is_tracked_callsign(callsign, base_callsign):
        return None
    latitude = packet.get("latitude")
    longitude = packet.get("longitude")
    if latitude is None or longitude is None:
        return None
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return None
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        return None
    return Position(callsign, latitude, longitude, received_at or utc_now())


class StopDetector:
    """Maintain independent arrival state for each accepted APRS station."""

    def __init__(self) -> None:
        self.states: dict[str, StationState] = {}

    def observe(self, position: Position) -> bool:
        """Return True only when a fresh place lookup is appropriate."""
        state = self.states.setdefault(position.callsign, StationState())
        if state.anchor is None:
            state.anchor = position
            state.last_position = position
            return False

        distance_from_anchor = haversine_meters(
            state.anchor.latitude,
            state.anchor.longitude,
            position.latitude,
            position.longitude,
        )
        state.last_position = position

        if distance_from_anchor >= DEPARTURE_RADIUS_METERS:
            state.anchor = position
            state.confirmed_at = None
            state.last_lookup_at = None
            state.last_notified_at = None
            log("arrival_candidate_reset", callsign=position.callsign, distance_m=round(distance_from_anchor, 1))
            return False

        if distance_from_anchor > STOP_RADIUS_METERS:
            log("arrival_candidate_outside_stop_radius", callsign=position.callsign, distance_m=round(distance_from_anchor, 1))
            return False

        elapsed = position.received_at - state.anchor.received_at
        if state.confirmed_at is None:
            if elapsed < STOP_CONFIRMATION:
                return False
            state.confirmed_at = position.received_at
            state.last_lookup_at = position.received_at
            return True

        if state.last_lookup_at is None or position.received_at - state.last_lookup_at >= REMINDER_COOLDOWN:
            state.last_lookup_at = position.received_at
            return True
        return False

    def mark_lookup_failure(self, position: Position) -> None:
        state = self.states[position.callsign]
        state.last_lookup_at = position.received_at - REMINDER_COOLDOWN + LOOKUP_FAILURE_COOLDOWN

    def mark_notified(self, position: Position) -> None:
        self.states[position.callsign].last_notified_at = position.received_at


def _first_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and value:
        return _first_text(value[0])
    if isinstance(value, dict):
        return str(value.get("name") or value.get("label") or "").strip()
    return ""


def _place_coordinates(place: dict[str, Any]) -> tuple[float, float] | None:
    coordinate_sources = (
        place,
        place.get("location") if isinstance(place.get("location"), dict) else {},
        place.get("geocodes", {}).get("main", {}) if isinstance(place.get("geocodes"), dict) else {},
    )
    for source in coordinate_sources:
        try:
            latitude = float(source["latitude"])
            longitude = float(source["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        return latitude, longitude
    return None


class FoursquarePlacesClient:
    def __init__(self, bearer_token: str) -> None:
        self.bearer_token = bearer_token

    def nearby(self, position: Position) -> list[PlaceCandidate]:
        query = urlencode(
            {
                "ll": f"{position.latitude:.6f},{position.longitude:.6f}",
                "radius": 150,
                "sort": "DISTANCE",
                "limit": FSQ_RESULT_LIMIT,
            }
        )
        request = Request(
            f"{FSQ_ENDPOINT}?{query}",
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "X-Places-Api-Version": FSQ_API_VERSION,
                "Accept": "application/json",
                "User-Agent": f"{APP_NAME}/1.0",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise RuntimeError(f"Foursquare request failed with HTTP {error.code}") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError("Foursquare request failed") from error

        raw_places: Any
        if isinstance(payload, dict):
            raw_places = payload.get("results", payload.get("places", []))
        else:
            raw_places = payload
        if not isinstance(raw_places, list):
            return []

        candidates: list[PlaceCandidate] = []
        for place in raw_places:
            if not isinstance(place, dict):
                continue
            place_id = str(place.get("fsq_place_id") or place.get("fsq_id") or "").strip()
            name = str(place.get("name") or "").strip()
            if not place_id or not name:
                continue
            category = _first_text(place.get("categories")) or _first_text(place.get("category")) or "Place"
            distance = place.get("distance")
            try:
                distance_m = float(distance)
            except (TypeError, ValueError):
                coordinates = _place_coordinates(place)
                if coordinates is None:
                    distance_m = 0.0
                else:
                    distance_m = haversine_meters(position.latitude, position.longitude, *coordinates)
            candidates.append(PlaceCandidate(place_id, name, category, distance_m))
        return sorted(candidates, key=lambda item: item.distance_m)[:FSQ_RESULT_LIMIT]


class PushoverNotifier:
    endpoint = "https://api.pushover.net/1/messages.json"

    def __init__(self, settings: Settings, dry_run: bool = False) -> None:
        self.settings = settings
        self.dry_run = dry_run

    @staticmethod
    def message_for(position: Position, candidates: list[PlaceCandidate]) -> tuple[str, str, str]:
        top = candidates[0]
        lines = [f"{index}. {candidate.name} — {candidate.category} ({round(candidate.distance_m)} m)" for index, candidate in enumerate(candidates, start=1)]
        title = f"Possible check-in: {top.name}"
        return title, "\n".join(lines), top.swarm_url

    def send(self, position: Position, candidates: list[PlaceCandidate]) -> None:
        title, message, swarm_url = self.message_for(position, candidates)
        if self.dry_run:
            log("dry_run_notification", callsign=position.callsign, place=candidates[0].name)
            print(json.dumps({"title": title, "message": message, "url": swarm_url}, indent=2))
            return

        body = urlencode(
            {
                "token": self.settings.pushover_app_token,
                "user": self.settings.pushover_user_key,
                "device": ",".join(self.settings.pushover_devices),
                "title": title,
                "message": message,
                "url": swarm_url,
                "url_title": "Open suggested place in Swarm",
                "priority": 0,
                "ttl": int(REMINDER_COOLDOWN.total_seconds()),
            }
        ).encode("utf-8")
        request = Request(self.endpoint, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError("Pushover request failed") from error
        if payload.get("status") != 1:
            raise RuntimeError("Pushover rejected the notification")
        log("notification_sent", callsign=position.callsign, place=candidates[0].name)


class ArrivalService:
    def __init__(
        self, base_callsign: str, places: FoursquarePlacesClient, notifier: PushoverNotifier, dry_run: bool = False
    ) -> None:
        self.base_callsign = normalize_base_callsign(base_callsign)
        self.detector = StopDetector()
        self.places = places
        self.notifier = notifier
        self.dry_run = dry_run

    def process_packet(self, packet: dict[str, Any], received_at: datetime | None = None) -> None:
        position = packet_to_position(packet, self.base_callsign, received_at)
        if position is None:
            return
        log("position_received", callsign=position.callsign)
        if not self.detector.observe(position):
            return

        if self.dry_run:
            candidates = [PlaceCandidate("demo-place", "Demo Place", "Simulation", 0.0)]
        else:
            try:
                candidates = self.places.nearby(position)
            except RuntimeError as error:
                self.detector.mark_lookup_failure(position)
                log("place_lookup_failed", callsign=position.callsign, reason=str(error))
                return
        if not candidates:
            log("no_nearby_place", callsign=position.callsign)
            return

        try:
            self.notifier.send(position, candidates)
            self.detector.mark_notified(position)
        except RuntimeError as error:
            log("notification_failed", callsign=position.callsign, reason=str(error))


def parse_fixture_lines(path: Path) -> Iterable[tuple[dict[str, Any], datetime]]:
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            event = json.loads(line)
            packet = event["packet"]
            received_at = datetime.fromisoformat(event["received_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid fixture event on line {line_number}") from error
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        yield packet, received_at


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Protected KEY=VALUE configuration file")
    parser.add_argument("--simulate", type=Path, metavar="FIXTURE", help="Replay JSONL fixture events without network calls")
    parser.add_argument("--dry-run", action="store_true", help="Use demo place suggestions and never call Foursquare or Pushover")
    parser.add_argument("--test-pushover", metavar="VENUE_ID", help="Send one Pushover message that opens the supplied Swarm venue ID")
    return parser.parse_args(argv)


def run_live(service: ArrivalService, base_callsign: str) -> None:
    try:
        import aprslib
    except ImportError as error:
        raise RuntimeError("aprslib is not installed; create the project virtual environment first") from error

    # A verified APRS-IS listener passcode is derivable from the base callsign;
    # it is intentionally not stored in the service configuration file.
    base_callsign = normalize_base_callsign(base_callsign)
    passcode = aprslib.passcode(base_callsign)
    while True:
        try:
            ais = aprslib.IS(base_callsign, passcode, host=APRS_SERVER, port=APRS_PORT)
            ais.set_filter(aprs_filter(base_callsign))
            ais.connect()
            log("aprs_connected")
            for raw_line in ais._socket_readlines(blocking=True):
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode("utf-8", errors="replace")
                if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                    continue
                try:
                    packet = aprslib.parse(raw_line)
                except Exception:
                    log("packet_parse_failed")
                    continue
                service.process_packet(packet)
        except KeyboardInterrupt:
            log("service_stopped")
            return
        except Exception as error:
            log("aprs_connection_failed", reason=str(error))
            time.sleep(30)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.simulate:
        # A fixture run never touches either API, so it remains usable from a
        # clean public checkout without a private configuration file.
        settings = Settings("", "", "", (), SIMULATION_BASE_CALLSIGN, args.config)
    else:
        try:
            settings = Settings.from_file(args.config)
        except ValueError as error:
            log("configuration_error", reason=str(error))
            return 2

    notifier = PushoverNotifier(settings, dry_run=args.dry_run or bool(args.simulate))
    if args.test_pushover:
        test_position = Position(f"{settings.aprs_base_callsign}-1", 0.0, 0.0, utc_now())
        candidate = PlaceCandidate(args.test_pushover, "Swarm deep-link test", "Test", 0.0)
        try:
            notifier.send(test_position, [candidate])
        except RuntimeError as error:
            log("notification_failed", reason=str(error))
            return 1
        return 0

    service_base_callsign = SIMULATION_BASE_CALLSIGN if args.simulate else settings.aprs_base_callsign
    service = ArrivalService(
        service_base_callsign,
        FoursquarePlacesClient(settings.fsq_bearer_token),
        notifier,
        dry_run=args.dry_run or bool(args.simulate),
    )
    if args.simulate:
        for packet, received_at in parse_fixture_lines(args.simulate):
            service.process_packet(packet, received_at)
        return 0

    run_live(service, settings.aprs_base_callsign)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
