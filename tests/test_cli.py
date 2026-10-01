import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

from sre_toolkit.cli import main
from sre_toolkit.context import parse_time

from tests.support import make_context

UTC = timezone.utc


def run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(argv)
    return code, buffer.getvalue()


class DemoRuns(unittest.TestCase):
    def test_demo_investigation_runs_with_no_credentials(self):
        code, out = run(["--demo", "--no-color", "incident", "investigate"])
        self.assertEqual(code, 2, "a critical incident should exit 2")
        self.assertIn("INCIDENT INVESTIGATION", out)
        self.assertIn("Database connection exhaustion", out)
        self.assertIn("NO AUTOMATED REMEDIATION PERFORMED", out)
        self.assertIn("$0.00", out)

    def test_json_output_is_a_valid_snapshot(self):
        code, out = run(["--demo", "--json", "incident", "snapshot"])
        payload = json.loads(out)
        self.assertEqual(payload["service"], "customer-api")
        self.assertIn("signals", payload)
        self.assertIn("cost", payload)
        self.assertEqual(code, 2)

    def test_timeline_is_ordered(self):
        _, out = run(["--demo", "--no-color", "incident", "timeline"])
        stamps = [line[:8] for line in out.splitlines() if line[:2].isdigit()]
        self.assertEqual(stamps, sorted(stamps))

    def test_postmortem_emits_markdown_sections(self):
        _, out = run(["--demo", "incident", "postmortem"])
        for heading in ("# Post-incident review", "## Timeline (UTC)", "## Action items"):
            self.assertIn(heading, out)

    def test_exit_zero_suppresses_severity_codes_only(self):
        severity_code, _ = run(["--demo", "--no-color", "incident", "investigate"])
        self.assertEqual(severity_code, 2)

        suppressed, out = run(["--exit-zero", "--demo", "--no-color", "incident", "investigate"])
        self.assertEqual(suppressed, 0)
        self.assertIn("Database connection exhaustion", out,
                      "--exit-zero must still print the full report")

    def test_exit_zero_does_not_mask_real_failures(self):
        code, _ = run(["--exit-zero", "incident", "summarize"])
        self.assertEqual(code, 1, "a usage error is not a severity code")

    def test_summarize_without_a_file_explains_itself(self):
        code, _ = run(["incident", "summarize"])
        self.assertEqual(code, 1)


class FreeCommands(unittest.TestCase):
    def test_price_table_lists_free_and_billed_operations(self):
        code, out = run(["--no-color", "cost", "prices"])
        self.assertEqual(code, 0)
        self.assertIn("cloudwatch:GetMetricData", out)
        self.assertIn("free", out)
        self.assertIn("anthropic.claude-haiku-4-5", out)

    def test_cache_stats_reports_a_path(self):
        code, out = run(["--no-color", "cache", "stats"])
        self.assertEqual(code, 0)
        self.assertIn("sre-toolkit", out)

    def test_bare_invocation_prints_help(self):
        code, out = run([])
        self.assertEqual(code, 0)
        self.assertIn("incident", out)

    def test_missing_required_argument_is_an_argparse_error(self):
        with self.assertRaises(SystemExit) as caught:
            run(["health"])
        self.assertEqual(caught.exception.code, 2)


class WindowParsing(unittest.TestCase):
    def test_iso_epoch_and_relative_forms(self):
        self.assertEqual(
            parse_time("2026-10-01T06:30:00"), datetime(2026, 10, 1, 6, 30, tzinfo=UTC)
        )
        self.assertEqual(parse_time("2026-10-01T06:30:00Z").tzinfo, UTC)
        self.assertEqual(parse_time("1759300200"), datetime.fromtimestamp(1759300200, UTC))

        now = datetime.now(UTC)
        self.assertLess(abs((now - parse_time("-45m")) - timedelta(minutes=45)),
                        timedelta(seconds=2))
        self.assertLess(abs((now - parse_time("2h ago")) - timedelta(hours=2)),
                        timedelta(seconds=2))

    def test_default_window_is_the_last_n_minutes(self):
        ctx = make_context()
        win = ctx.resolve_window(None, None, minutes=15)
        self.assertEqual(win.seconds, 15 * 60)

    def test_inverted_window_is_rejected(self):
        ctx = make_context()
        with self.assertRaises(ValueError):
            ctx.resolve_window("2026-10-01T07:00:00", "2026-10-01T06:00:00", 30)


class AiCostGuards(unittest.TestCase):
    def test_ai_step_is_skipped_entirely_without_the_flag(self):
        from sre_toolkit.commands._shared import demo_snapshot
        from sre_toolkit.investigate import analyse

        ctx = make_context()
        snap = analyse(ctx, demo_snapshot())
        self.assertEqual(snap.cost["total_usd"], 0.0)
        self.assertNotIn("ai_usage", snap.meta)

    def test_dry_run_prices_the_ai_call_without_making_it(self):
        from sre_toolkit.commands._shared import demo_snapshot
        from sre_toolkit.investigate import analyse

        ctx = make_context(use_ai=True, dry_run=True)
        ctx.dry_run = True
        snap = analyse(ctx, demo_snapshot())

        self.assertEqual(ctx.ledger.total, 0.0)
        self.assertGreater(ctx.ledger.avoided, 0.0, "the estimate should be visible")
        self.assertIn("dry run", snap.narrative)

    def test_budget_blocks_the_model_call_before_any_sdk_import(self):
        from sre_toolkit.ai import analyzer
        from sre_toolkit.commands._shared import demo_snapshot
        from sre_toolkit.errors import BudgetExceeded

        ctx = make_context(max_spend=0.0000001, use_ai=True)
        with self.assertRaises(BudgetExceeded):
            analyzer.analyse(ctx, demo_snapshot())

    def test_model_default_is_the_cheapest_option(self):
        from sre_toolkit.pricing import BEDROCK_MODELS, DEFAULT_MODEL

        cheapest = min(BEDROCK_MODELS.items(), key=lambda kv: kv[1]["input"])[0]
        self.assertEqual(DEFAULT_MODEL, cheapest)


if __name__ == "__main__":
    unittest.main()
