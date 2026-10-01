import unittest
from datetime import datetime, timedelta, timezone

from tests.support import log_events, make_context, metric_data, window

from sre_toolkit.collectors import logs as log_col
from sre_toolkit.collectors import metrics as metric_col
from sre_toolkit.collectors import network, rds as rds_col
from sre_toolkit.models import Severity, Signal, SignalKind

UTC = timezone.utc


class LogPatterns(unittest.TestCase):
    def test_normalisation_collapses_variable_parts(self):
        a = normalised("conn 10.0.1.5:5432 failed after 3000ms id=7f3a2b1c9d8e4f10")
        b = normalised("conn 10.0.9.7:5432 failed after 4500ms id=0a1b2c3d4e5f6071")
        self.assertEqual(a, b, "two instances of the same error must share one pattern")

    def test_classification_identifies_pool_exhaustion(self):
        message = "HikariPool-1 - Connection is not available, request timed out after 30000ms"
        self.assertIn("connection_pool_exhausted", log_col.classify(message))
        self.assertIn("timeout", log_col.classify(message))

    def test_classification_is_empty_for_benign_lines(self):
        self.assertEqual(log_col.classify("INFO started listener on 8080"), [])

    def test_patterns_counts_and_orders_by_frequency(self):
        now = datetime(2026, 10, 1, 6, 40, tzinfo=UTC)
        events = [
            {"at": now, "message": "ERROR pool timeout after 30000ms", "stream": "a"}
        ] * 5 + [{"at": now, "message": "WARN slow query 1200ms", "stream": "b"}]
        result = log_col.patterns(events)
        self.assertEqual(result[0]["count"], 5)
        self.assertEqual(result[0]["level"], "Error")
        self.assertEqual(result[0]["severity"], "crit")

    def test_signals_derived_from_error_classes(self):
        win = window(30)
        events = [
            {"at": win.start + timedelta(minutes=5), "message": "ERROR OutOfMemoryError heap",
             "stream": "a"}
        ] * 12
        signals = log_col.to_signals(events, win, "logs")
        names = {s.name for s in signals}
        self.assertIn("log:oom", names)
        self.assertIn("Log error volume", names)
        oom = next(s for s in signals if s.name == "log:oom")
        self.assertEqual(oom.severity, Severity.CRIT)

    def test_filter_log_events_is_not_billed(self):
        win = window(30)
        ctx = make_context({"filter_log_events": {"events": log_events(
            3, "ERROR boom", win.start)}})
        collected = log_col.collect(ctx, "/aws/ecs/api", win, limit=10)
        self.assertEqual(len(collected), 3)
        self.assertEqual(ctx.ledger.total, 0.0, "reading logs must not add per-GB cost")


def normalised(message: str) -> str:
    return log_col.normalize(message)


class MetricCollection(unittest.TestCase):
    def test_period_scales_with_window(self):
        self.assertEqual(metric_col.pick_period(30 * 60), 60)
        self.assertEqual(metric_col.pick_period(6 * 3600), 900)
        self.assertEqual(metric_col.pick_period(30 * 86400), 86400)

    def test_all_metrics_are_fetched_in_one_billed_request(self):
        win = window(30)
        specs = metric_col.ecs_specs("prod", "api") + metric_col.rds_specs("prod-db")
        ctx = make_context({
            "get_metric_data": metric_data(win, {s.key: (10.0, 90.0) for s in specs})
        })
        series = metric_col.fetch(ctx, win, specs)

        self.assertEqual(len(series), len(specs))
        self.assertEqual([c[1] for c in ctx.aws.calls].count("get_metric_data"), 1)
        # Billed per metric requested, not per call.
        self.assertAlmostEqual(ctx.ledger.total, 0.01 * len(specs) / 1000)

    def test_baseline_split_detects_a_jump(self):
        win = window(30)
        spec = metric_col.MetricSpec("cpu", "AWS/ECS", "CPUUtilization", {}, "Average", "Percent",
                                     "ECS CPU", warn_above=80, crit_above=95)
        ctx = make_context({"get_metric_data": metric_data(win, {"cpu": (20.0, 95.0)})})
        series = metric_col.fetch(ctx, win, [spec])
        signal = metric_col.to_signal(series[0], win, "cloudwatch")

        self.assertEqual(signal.baseline, 20.0)
        self.assertEqual(signal.peak, 95.0)
        self.assertEqual(signal.severity, Severity.CRIT)
        self.assertIsNotNone(signal.first_seen)

    def test_warn_below_metrics_use_the_minimum(self):
        win = window(30)
        spec = metric_col.MetricSpec("healthy", "AWS/ApplicationELB", "HealthyHostCount", {},
                                     "Minimum", "Count", "Healthy targets", warn_below=2,
                                     crit_below=1, spike_ratio=None)
        ctx = make_context({"get_metric_data": metric_data(win, {"healthy": (6.0, 0.0)})})
        signal = metric_col.to_signal(metric_col.fetch(ctx, win, [spec])[0], win, "cloudwatch")
        self.assertEqual(signal.peak, 0.0)
        self.assertEqual(signal.severity, Severity.CRIT)

    def test_empty_result_yields_no_signal(self):
        win = window(30)
        spec = metric_col.MetricSpec("cpu", "AWS/ECS", "CPUUtilization", {})
        ctx = make_context({"get_metric_data": {"MetricDataResults": []}})
        self.assertIsNone(metric_col.to_signal(metric_col.fetch(ctx, win, [spec])[0], win, "cw"))


class RdsDerivations(unittest.TestCase):
    def test_connection_pressure_expresses_peak_as_percentage(self):
        signal = Signal(name="DB connections", kind=SignalKind.METRIC, source="cloudwatch",
                        baseline=42.0, peak=188.0, unit="Count")
        pressure = rds_col.connection_pressure(signal, 200)
        self.assertEqual(pressure.severity, Severity.CRIT)
        self.assertAlmostEqual(pressure.peak, 94.0)
        self.assertEqual(pressure.tags["class"], "connection_saturation")

    def test_connection_pressure_needs_a_ceiling(self):
        signal = Signal(name="DB connections", kind=SignalKind.METRIC, source="cw", peak=10.0)
        self.assertIsNone(rds_col.connection_pressure(signal, None))

    def test_max_connections_estimated_from_instance_class(self):
        self.assertGreater(rds_col.estimate_max_connections("db.r6g.large", "postgres"), 1000)
        self.assertIsNone(rds_col.estimate_max_connections("db.unknown.size"))


class NetworkProbes(unittest.TestCase):
    def test_hostport_defaults(self):
        self.assertEqual(network._hostport("example.com")[:2], ("example.com", 443))
        self.assertEqual(network._hostport("http://example.com")[:2], ("example.com", 80))
        self.assertEqual(network._hostport("https://example.com:8443")[:2], ("example.com", 8443))

    def test_tcp_probe_on_closed_port_reports_failure(self):
        result = network.tcp_check("127.0.0.1", 1, timeout=0.25)
        self.assertFalse(result["ok"])
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
