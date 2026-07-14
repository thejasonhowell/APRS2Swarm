import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watcher


T0 = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
BASE_CALLSIGN = "K1TEST"


def position(callsign, latitude=40.0, longitude=-89.0, offset_minutes=0):
    return watcher.Position(callsign, latitude, longitude, T0 + timedelta(minutes=offset_minutes))


class CallsignTests(unittest.TestCase):
    def test_only_numeric_mobile_ssids_are_accepted(self):
        for callsign in ("K1TEST-1", "K1TEST-9", "K1TEST-15", "k1test-2"):
            self.assertTrue(watcher.is_tracked_callsign(callsign, BASE_CALLSIGN))
        for callsign in ("K1TEST", "K1TEST-0", "K1TEST-A", "K1TEST-16", "OTHER-9"):
            self.assertFalse(watcher.is_tracked_callsign(callsign, BASE_CALLSIGN))

    def test_base_callsign_is_normalized_and_filter_is_broad(self):
        self.assertEqual(watcher.normalize_base_callsign(" k1test "), BASE_CALLSIGN)
        self.assertEqual(watcher.aprs_filter(BASE_CALLSIGN), "b/K1TEST*")
        with self.assertRaises(ValueError):
            watcher.normalize_base_callsign("K1TEST-1")

    def test_packet_requires_position_fields(self):
        self.assertIsNone(watcher.packet_to_position({"from": "K1TEST-1"}, BASE_CALLSIGN, T0))
        self.assertIsNone(watcher.packet_to_position({"from": "K1TEST-A", "latitude": 1, "longitude": 2}, BASE_CALLSIGN, T0))
        parsed = watcher.packet_to_position({"from": "K1TEST-1", "latitude": 1, "longitude": 2}, BASE_CALLSIGN, T0)
        self.assertEqual(parsed.callsign, "K1TEST-1")


class StopDetectorTests(unittest.TestCase):
    def test_two_reports_five_minutes_apart_confirm_stop(self):
        detector = watcher.StopDetector()
        self.assertFalse(detector.observe(position("K1TEST-1")))
        self.assertTrue(detector.observe(position("K1TEST-1", latitude=40.0005, offset_minutes=5)))

    def test_report_before_five_minutes_does_not_confirm_stop(self):
        detector = watcher.StopDetector()
        detector.observe(position("K1TEST-1"))
        self.assertFalse(detector.observe(position("K1TEST-1", offset_minutes=4)))

    def test_position_outside_stop_radius_does_not_confirm_stop(self):
        detector = watcher.StopDetector()
        detector.observe(position("K1TEST-1"))
        self.assertFalse(detector.observe(position("K1TEST-1", latitude=40.0015, offset_minutes=5)))

    def test_movement_resets_the_candidate(self):
        detector = watcher.StopDetector()
        detector.observe(position("K1TEST-1"))
        self.assertFalse(detector.observe(position("K1TEST-1", latitude=40.003, offset_minutes=5)))
        self.assertFalse(detector.observe(position("K1TEST-1", latitude=40.003, offset_minutes=9)))
        self.assertTrue(detector.observe(position("K1TEST-1", latitude=40.003, offset_minutes=10)))

    def test_reminder_requires_one_hour_and_new_position(self):
        detector = watcher.StopDetector()
        detector.observe(position("K1TEST-1"))
        self.assertTrue(detector.observe(position("K1TEST-1", offset_minutes=5)))
        self.assertFalse(detector.observe(position("K1TEST-1", offset_minutes=64)))
        self.assertTrue(detector.observe(position("K1TEST-1", offset_minutes=65)))


class NotificationTests(unittest.TestCase):
    def test_dry_run_outputs_direct_swarm_url(self):
        settings = watcher.Settings("fsq", "app", "user", ("phone",), BASE_CALLSIGN, Path("/tmp/test.env"))
        notifier = watcher.PushoverNotifier(settings, dry_run=True)
        candidate = watcher.PlaceCandidate("venue-123", "Coffee Shop", "Cafe", 12)
        output = io.StringIO()
        with redirect_stdout(output):
            notifier.send(position("K1TEST-1"), [candidate])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["url"], "swarm://checkins/add?venueId=venue-123")
        self.assertIn("Coffee Shop", payload["message"])

    def test_notification_lists_five_places_and_links_the_best_match(self):
        candidates = [
            watcher.PlaceCandidate(f"venue-{index}", f"Place {index}", "Cafe", index * 10)
            for index in range(1, 6)
        ]
        title, message, url = watcher.PushoverNotifier.message_for(position("K1TEST-1"), candidates)
        self.assertEqual(title, "Possible check-in: Place 1")
        self.assertEqual(url, "swarm://checkins/add?venueId=venue-1")
        for index in range(1, 6):
            self.assertIn(f"{index}. Place {index}", message)


if __name__ == "__main__":
    unittest.main()
