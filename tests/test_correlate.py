import unittest
from datetime import timedelta

from tests.support import window

from sre_toolkit.commands._shared import demo_snapshot
from sre_toolkit.correlate import correlate
from sre_toolkit.models import (
    Severity,
    Signal,
    SignalKind,
    Snapshot,
    TimelineEvent,
)


def blank(service="api"):
    return Snapshot(service=service, window=window(30))


def signal(name, severity=Severity.CRIT, cls=None, **kwargs):
    return Signal(
        name=name, kind=kwargs.pop("kind", SignalKind.METRIC),
        source=kwargs.pop("source", "cloudwatch"), severity=severity,
        tags={"class": cls} if cls else {}, **kwargs,
    )


class DemoIncident(unittest.TestCase):
    def test_demo_incident_identifies_connection_exhaustion_and_the_deploy(self):
        snap = demo_snapshot()
        findings = correlate(snap)
        titles = " | ".join(f.title for f in findings)

        self.assertIn("Database connection exhaustion", titles)
        self.assertIn("Change correlation", titles)
        top = findings[0]
        self.assertGreaterEqual(top.confidence, 0.7)
        self.assertTrue(top.evidence, "a finding must carry its evidence")
        self.assertTrue(top.next_steps, "a finding must suggest a next check")

    def test_findings_are_ranked_by_confidence(self):
        findings = correlate(demo_snapshot())
        scores = [f.confidence for f in findings]
        self.assertEqual(scores, sorted(scores, reverse=True))


class RuleBehaviour(unittest.TestCase):
    def test_quiet_window_says_so_instead_of_inventing_a_cause(self):
        findings = correlate(blank())
        self.assertEqual(len(findings), 1)
        self.assertIn("No degradation detected", findings[0].title)
        self.assertLess(findings[0].confidence, 0.2)

    def test_degradation_without_a_known_pattern_is_reported_honestly(self):
        snap = blank()
        snap.add(signal("Some odd metric", Severity.WARN))
        findings = correlate(snap)
        self.assertIn("no known pattern matched", findings[0].title)

    def test_deploy_after_the_errors_is_not_blamed(self):
        snap = blank()
        onset = snap.window.start + timedelta(minutes=2)
        snap.add(signal("HTTP 5xx (target)", first_seen=onset))
        snap.add(TimelineEvent(at=onset + timedelta(minutes=10), source="ecs",
                               title="Deployment completed"))
        titles = [f.title for f in correlate(snap)]
        self.assertFalse(any("Change correlation" in t for t in titles),
                         "a deployment after the onset cannot be the cause")

    def test_deploy_long_before_the_errors_is_not_blamed(self):
        snap = blank()
        snap.add(TimelineEvent(at=snap.window.start - timedelta(hours=2), source="ecs",
                               title="Deployment completed"))
        snap.add(signal("HTTP 5xx (target)", first_seen=snap.window.start + timedelta(minutes=5)))
        titles = [f.title for f in correlate(snap)]
        self.assertFalse(any("Change correlation" in t for t in titles))

    def test_dependency_failure_defers_to_local_saturation(self):
        snap = blank()
        snap.add(signal("log:timeout", kind=SignalKind.LOG, source="logs"))
        snap.add(signal("ECS CPU", Severity.CRIT, baseline=20.0, peak=99.0, unit="Percent"))
        titles = [f.title for f in correlate(snap)]
        self.assertIn("Compute resource saturation", titles)
        self.assertNotIn("Downstream dependency failure", titles)

    def test_oom_restart_loop_is_detected(self):
        snap = blank()
        snap.add(signal("ECS task terminations", Severity.CRIT, cls="oom",
                        kind=SignalKind.RESOURCE, source="ecs"))
        titles = [f.title for f in correlate(snap)]
        self.assertTrue(any("restart loop" in t for t in titles))

    def test_traffic_surge_only_without_a_deploy(self):
        snap = blank()
        snap.add(signal("ALB requests", Severity.INFO, baseline=100.0, peak=500.0))
        snap.add(signal("p95 latency", Severity.WARN, baseline=0.2, peak=2.0, unit="Seconds"))
        self.assertTrue(any("Traffic surge" in f.title for f in correlate(snap)))

        snap.add(TimelineEvent(at=snap.window.start, source="ecs", title="Deployment completed"))
        self.assertFalse(any("Traffic surge" in f.title for f in correlate(snap)))

    def test_max_findings_is_respected(self):
        self.assertLessEqual(len(correlate(demo_snapshot(), max_findings=2)), 2)

    def test_a_broken_rule_cannot_break_the_report(self):
        from sre_toolkit.correlate import rules

        def exploding(_snap):
            raise ValueError("boom")

        rules.RULES.append(exploding)
        try:
            findings = correlate(demo_snapshot())
            self.assertTrue(findings)
        finally:
            rules.RULES.remove(exploding)


class OfflineNarrative(unittest.TestCase):
    def test_narrative_is_deterministic_and_mentions_the_deploy(self):
        from sre_toolkit.ai import offline

        snap = demo_snapshot()
        snap.findings = correlate(snap)
        first = offline.narrate(snap)
        second = offline.narrate(snap)

        self.assertEqual(first, second)
        self.assertIn("customer-api", first)
        self.assertIn("UpdateService", first)
        self.assertIn("no model was called", first)

    def test_narrative_handles_a_quiet_window(self):
        from sre_toolkit.ai import offline

        text = offline.narrate(blank())
        self.assertIn("No signal", text)


class PromptCompaction(unittest.TestCase):
    def test_context_is_bounded_and_excludes_raw_logs(self):
        from sre_toolkit.ai import prompt

        snap = demo_snapshot()
        snap.findings = correlate(snap)
        context = prompt.build(snap)

        self.assertLessEqual(len(context), 6200)
        self.assertLess(prompt.estimate_tokens(context), 2000)
        # Normalised patterns are sent; raw timestamps from log bodies are not.
        self.assertIn("<ts> ERROR", context)
        self.assertNotIn("2026-10-01T06:35:11.204Z", context)
        self.assertIn("RULE-BASED FINDINGS", context)


if __name__ == "__main__":
    unittest.main()
