import json
import unittest
from datetime import datetime, timezone

from tests.support import ROOT  # noqa: F401  (puts src on sys.path)

from sre_toolkit.models import Finding, Severity, Signal, SignalKind, Snapshot, Window

UTC = timezone.utc


class SnapshotRoundTrip(unittest.TestCase):
    def test_json_round_trip_preserves_everything(self):
        window = Window(datetime(2026, 10, 1, 6, 30, tzinfo=UTC),
                        datetime(2026, 10, 1, 7, 0, tzinfo=UTC))
        snap = Snapshot(service="api", window=window, region="us-east-1")
        snap.add(Signal(
            name="HTTP 5xx", kind=SignalKind.METRIC, source="cloudwatch",
            severity=Severity.CRIT, baseline=1.0, peak=200.0, unit="Count",
            first_seen=window.start, series=[(window.start, 1.0), (window.end, 200.0)],
        ))
        snap.add(Finding(title="pool exhaustion", confidence=0.8, rationale="because"))

        restored = Snapshot.from_dict(json.loads(snap.to_json()))

        self.assertEqual(restored.service, "api")
        self.assertEqual(restored.window.start, window.start)
        self.assertEqual(restored.signals[0].severity, Severity.CRIT)
        self.assertEqual(restored.signals[0].series[0][1], 1.0)
        self.assertEqual(restored.findings[0].confidence_label, "high")

    def test_change_ratio_handles_zero_baseline(self):
        signal = Signal(name="x", kind=SignalKind.METRIC, source="s", baseline=0.0, peak=5.0)
        self.assertEqual(signal.change_ratio(), float("inf"))
        self.assertIsNone(Signal(name="x", kind=SignalKind.METRIC, source="s").change_ratio())

    def test_worst_severity_and_ordering(self):
        window = Window(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 1, 1, tzinfo=UTC))
        snap = Snapshot(service="api", window=window)
        snap.add(Signal(name="a", kind=SignalKind.LOG, source="s", severity=Severity.WARN))
        snap.add(Signal(name="b", kind=SignalKind.LOG, source="s", severity=Severity.CRIT))
        self.assertEqual(snap.worst_severity(), Severity.CRIT)


class DemoFixture(unittest.TestCase):
    def test_bundled_demo_snapshot_loads(self):
        from sre_toolkit.commands._shared import demo_snapshot

        snap = demo_snapshot()
        self.assertEqual(snap.service, "customer-api")
        self.assertGreater(len(snap.signals), 5)
        self.assertTrue(all(s.severity for s in snap.signals))


if __name__ == "__main__":
    unittest.main()
