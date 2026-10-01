"""Command-line entry point.

argparse, not Click/Typer, on purpose: the core package has zero runtime
dependencies, so `pip install sre-toolkit` resolves nothing, CI installs nothing,
and a Lambda cold start imports nothing it does not need. Collector modules
(boto3, kubernetes, anthropic) are imported inside handlers, so `sre health` and
`sre --demo` work on a machine with none of them installed.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .cache import Cache
from .context import Context
from .errors import SreToolkitError
from .ledger import Ledger
from .render import Console

EPILOG = """\
examples:
  sre health https://api.example.com          free: HTTP, DNS, TLS and TCP probes
  sre --demo incident investigate             full pipeline on a bundled snapshot ($0)
  sre incident snapshot --service payments --cluster prod -o incident.json
  sre incident summarize incident.json        re-analyse a saved snapshot for free
  sre incident investigate --service payments --cluster prod --ai --max-spend 0.05
  sre aws diagnose --service payments --cluster prod --db prod-payments
  sre logs investigate --service payments --deep   billed per GB scanned
  sre slo calculate --target-group payments-tg --objective 99.9
  sre cost estimate --service payments        price a run before making it

cost model: every billed call is metered. Pass --dry-run to price an
investigation without calling AWS, and --max-spend to make a breach impossible.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sre",
        description="OpsPilot — SRE incident investigation toolkit (read-only, cost-metered).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"sre-toolkit {__version__}")

    scope = parser.add_argument_group("scope")
    scope.add_argument("--region", help="AWS region (default: from profile/environment)")
    scope.add_argument("--profile", help="AWS profile to use")

    cost = parser.add_argument_group("cost controls")
    cost.add_argument(
        "--max-spend", type=float, metavar="USD",
        help="abort before any call that would take this run over USD (e.g. 0.05)",
    )
    cost.add_argument(
        "--dry-run", action="store_true",
        help="plan and price the run without calling AWS or Bedrock",
    )
    cost.add_argument(
        "--cache-ttl", type=int, default=900, metavar="SECONDS",
        help="reuse cached API responses younger than this (default: 900)",
    )
    cost.add_argument("--no-cache", action="store_true", help="bypass the response cache")
    cost.add_argument(
        "--deep", action="store_true",
        help="allow CloudWatch Logs Insights queries (billed per GB scanned)",
    )

    ai = parser.add_argument_group("analysis")
    ai.add_argument(
        "--ai", action="store_true",
        help="add a Bedrock-generated narrative (off by default; deterministic analysis is free)",
    )
    ai.add_argument("--model", help="Bedrock model id (default: cheapest capable Claude)")

    out = parser.add_argument_group("output")
    out.add_argument("--json", action="store_true", dest="json_mode",
                     help="machine-readable output on stdout")
    out.add_argument("-o", "--output", metavar="FILE", help="write the JSON artefact to FILE")
    out.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    out.add_argument("--demo", action="store_true",
                     help="use the bundled demo snapshot; no AWS account or spend required")
    out.add_argument("-v", "--verbose", action="store_true", help="log collector detail to stderr")
    out.add_argument(
        "--exit-zero", action="store_true",
        help="always exit 0 when the run succeeds, even if the findings are critical "
             "(real failures — bad target, missing extra, budget breach — still exit non-zero)",
    )

    sub = parser.add_subparsers(dest="group", metavar="<group>")

    from .commands import aws as aws_cmd
    from .commands import cache as cache_cmd
    from .commands import cert as cert_cmd
    from .commands import cost as cost_cmd
    from .commands import health as health_cmd
    from .commands import incident as incident_cmd
    from .commands import k8s as k8s_cmd
    from .commands import logs as logs_cmd
    from .commands import reliability as rel_cmd

    for module in (
        health_cmd, cert_cmd, aws_cmd, k8s_cmd, logs_cmd, incident_cmd, rel_cmd,
        cost_cmd, cache_cmd,
    ):
        module.register(sub)

    return parser


def build_context(args) -> Context:
    console = Console(color=False if args.no_color else None, json_mode=args.json_mode)
    return Context(
        region=args.region,
        profile=args.profile,
        console=console,
        cache=Cache(ttl=args.cache_ttl, enabled=not args.no_cache),
        ledger=Ledger(max_spend=args.max_spend, dry_run=args.dry_run),
        dry_run=args.dry_run,
        demo=args.demo,
        deep=args.deep,
        use_ai=args.ai,
        model=args.model,
        verbose=args.verbose,
        json_mode=args.json_mode,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    ctx = build_context(args)
    try:
        code = handler(ctx, args) or 0
        # Severity codes (1 = warn, 2 = crit) are what make these commands useful in
        # a pipeline, but a reporting run — CI smoke tests, a dashboard job — wants the
        # report without the failure. --exit-zero suppresses only those two; a genuine
        # error still exits non-zero.
        if args.exit_zero and code in (1, 2):
            return 0
        return code
    except SreToolkitError as exc:
        ctx.console.error(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        ctx.console.error("interrupted")
        return 130
    except BrokenPipeError:  # e.g. `sre ... | head`
        return 0


if __name__ == "__main__":
    sys.exit(main())
