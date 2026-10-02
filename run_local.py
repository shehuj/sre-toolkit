#!/usr/bin/env python3
"""Interactive local runner for sre-toolkit.

    python3 run_local.py

Asks for the inputs it needs, remembers them between runs, dry-runs anything
billable before you confirm, and prints the equivalent `sre …` command so you can
skip this script once you know the flags.

Stdlib only, and it adds ./src to sys.path, so it works in a fresh clone with
nothing installed. AWS actions additionally need `pip install -e '.[aws]'`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

ANSWERS_PATH = Path(
    os.environ.get("SRE_LOCAL_ANSWERS")
    or Path.home() / ".config" / "sre-toolkit" / "local-run.json"
)

BOLD, DIM, RED, GREEN, YELLOW, CYAN, RESET = (
    ("\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m")
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    else ("", "", "", "", "", "", "")
)


# --------------------------------------------------------------------------
# prompting
# --------------------------------------------------------------------------
class Aborted(Exception):
    """The user pressed Ctrl-C/Ctrl-D, or stdin ran out."""


def _input(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise Aborted from None


def ask(
    question: str,
    default: str | None = None,
    required: bool = False,
    help_text: str = "",
) -> str:
    """Free-text prompt. Enter accepts the default; blank is allowed unless required."""
    if help_text:
        print(f"{DIM}  {help_text}{RESET}")
    suffix = f" [{default}]" if default else (" (required)" if required else " (optional)")
    while True:
        answer = _input(f"{question}{suffix}: ").strip()
        if not answer and default is not None:
            return default
        if answer:
            return answer
        if not required:
            return ""
        print(f"{RED}  this one is required{RESET}")


def ask_choice(question: str, options: list[tuple[str, str]]) -> str:
    """Numbered menu. Returns the key of the chosen option."""
    print(f"\n{BOLD}{question}{RESET}")
    for index, (_, label) in enumerate(options, 1):
        print(f"  {CYAN}{index:>2}{RESET}  {label}")
    while True:
        raw = _input("\nchoose a number (or 'q' to quit): ").strip().lower()
        if raw in ("q", "quit", "exit"):
            raise Aborted
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print(f"{RED}  enter a number between 1 and {len(options)}{RESET}")


def ask_bool(question: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = _input(f"{question} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def ask_float(question: str, default: float) -> float:
    while True:
        raw = _input(f"{question} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return float(raw.lstrip("$"))
        except ValueError:
            print(f"{RED}  enter a number, e.g. 0.05{RESET}")


# --------------------------------------------------------------------------
# remembered answers
# --------------------------------------------------------------------------
def load_answers() -> dict:
    try:
        return json.loads(ANSWERS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_answers(answers: dict) -> None:
    try:
        ANSWERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ANSWERS_PATH.write_text(json.dumps(answers, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass  # remembering inputs is a convenience, never a requirement


def remembered(answers: dict, key: str, question: str, **kwargs) -> str:
    """Ask, defaulting to whatever was answered last time."""
    value = ask(question, default=answers.get(key) or kwargs.pop("default", None), **kwargs)
    if value:
        answers[key] = value
    return value


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------
def preflight() -> dict:
    """Report what this machine can do before offering to do it."""
    print(f"\n{BOLD}ENVIRONMENT{RESET}")
    state: dict = {}

    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    ok = sys.version_info >= (3, 10)
    mark(ok, f"Python {version}" + ("" if ok else " — 3.10 or newer is required"))
    state["python_ok"] = ok

    try:
        import sre_toolkit

        mark(True, f"sre-toolkit {sre_toolkit.__version__} importable from {SRC}")
    except ImportError as exc:
        mark(False, f"cannot import sre_toolkit: {exc}")
        state["python_ok"] = False

    try:
        import boto3  # noqa: F401

        state["boto3"] = True
        mark(True, "boto3 installed — AWS collectors available")
    except ImportError:
        state["boto3"] = False
        mark(None, "boto3 not installed — AWS actions unavailable "
                   "(pip install -e '.[aws]')")

    try:
        import anthropic  # noqa: F401

        state["anthropic"] = True
        mark(True, "anthropic installed — --ai narratives available")
    except ImportError:
        state["anthropic"] = False
        mark(None, "anthropic not installed — AI narrative unavailable "
                   "(pip install -e '.[ai]')")

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    profile = os.environ.get("AWS_PROFILE") or ""
    state["region"] = region
    state["profile"] = profile

    identity = None
    if state["boto3"]:
        identity = whoami(profile or None, region or None)
        if identity:
            mark(True, f"credentials resolve as {identity}")
        else:
            mark(None, "no usable AWS credentials found — free actions still work")
    state["identity"] = identity

    return state


def mark(ok: bool | None, text: str) -> None:
    glyph = {True: f"{GREEN}✓{RESET}", False: f"{RED}✗{RESET}", None: f"{YELLOW}•{RESET}"}[ok]
    print(f"  {glyph} {text}")


def whoami(profile: str | None, region: str | None) -> str | None:
    """Resolve the caller identity. Free API call; returns None if it fails."""
    try:
        import boto3

        session = boto3.Session(profile_name=profile, region_name=region)
        identity = session.client("sts").get_caller_identity()
        arn = identity.get("Arn", "")
        return arn.split("/")[-1] + f" (account {identity.get('Account')})" if arn else None
    except Exception:  # noqa: BLE001 - any failure means "no usable credentials"
        return None


# --------------------------------------------------------------------------
# running the real CLI
# --------------------------------------------------------------------------
def run(argv: list[str], label: str = "") -> int:
    """Invoke the real CLI in-process so there is no second implementation."""
    from sre_toolkit.cli import main

    shown = "sre " + " ".join(_quote(a) for a in argv)
    print(f"\n{DIM}$ {shown}{RESET}\n")
    try:
        code = main(argv)
    except SystemExit as exc:  # argparse usage errors
        code = int(exc.code or 0)
    explain_exit(code, label)
    return code


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


EXIT_MEANING = {
    0: ("healthy", GREEN),
    1: ("warnings found", YELLOW),
    2: ("critical findings", RED),
    3: ("target not found — check the name and region", RED),
    4: ("a missing optional dependency", RED),
    5: ("stopped by the spend limit", YELLOW),
    6: ("a collector could not read — check credentials, region and permissions", RED),
}


def explain_exit(code: int, label: str) -> None:
    meaning, colour = EXIT_MEANING.get(code, ("unexpected error", RED))
    print(f"\n{colour}exit {code}: {meaning}{RESET}" + (f" {DIM}({label}){RESET}" if label else ""))


def price_plan(argv: list[str]) -> float | None:
    """Plan the run with --dry-run and print just the bill of operations.

    Every command emits a `cost` block in --json, so this works uniformly without
    the runner knowing anything about the command it is pricing.
    """
    import io
    from contextlib import redirect_stdout

    from sre_toolkit.cli import main

    buffer = io.StringIO()
    try:
        with redirect_stdout(buffer):
            main(["--dry-run", "--json", *argv])
        payload = json.loads(buffer.getvalue())
    except (SystemExit, Exception):  # noqa: BLE001 - any failure falls back to prose
        return None

    cost = payload.get("cost") or {}
    operations = cost.get("by_operation") or {}
    if not operations:
        return None

    total = 0.0
    rows = []
    for name, row in operations.items():
        usd = row.get("usd", 0.0) + row.get("avoided_usd", 0.0)
        total += usd
        rows.append((name, row.get("calls", 0), usd))
    rows.sort(key=lambda r: (-r[2], r[0]))

    width = max(len(r[0]) for r in rows)
    print(f"\n{BOLD}PLANNED CALLS{RESET}  {DIM}(nothing has been called){RESET}")
    print(f"  {DIM}{'operation'.ljust(width)}  calls  estimated{RESET}")
    for name, calls, usd in rows:
        price = "free" if usd == 0 else f"${usd:.6f}"
        print(f"  {name.ljust(width)}  {str(calls).rjust(5)}  {price}")
    print(f"  {BOLD}{'total'.ljust(width)}  {'':>5}  ${total:.6f}{RESET}")
    return total


def confirm_spend(argv: list[str], answers: dict) -> list[str] | None:
    """Price the run first, then ask. Returns the real argv, or None if declined."""
    total = price_plan(argv)
    if total is None:
        print(f"\n{YELLOW}Could not price this run up front — dry-running it instead.{RESET}")
        run(["--dry-run", "--no-color", *argv], label="estimate only")
    elif total == 0:
        print(f"\n{GREEN}This run uses only unbilled APIs.{RESET}")

    if not ask_bool("\nRun it for real?", default=True):
        print(f"{DIM}nothing was called.{RESET}")
        return None

    default_cap = answers.get("max_spend", 0.05)
    if total:
        print(f"{DIM}The limit below aborts the run before any call that would exceed it. "
              f"The plan above estimates ${total:.6f}.{RESET}")
    cap = ask_float("Spend limit for this run in USD", default_cap)
    answers["max_spend"] = cap
    return ["--max-spend", str(cap), *argv]


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------
def action_demo(answers: dict, state: dict) -> None:
    print(f"\n{DIM}Runs the whole pipeline on a bundled incident. No AWS account, "
          f"no spend. Exits 2 because the demo incident is critical — that is "
          f"expected.{RESET}")
    run(["--demo", "incident", "investigate"], label="demo incident")


def action_health(answers: dict, state: dict) -> None:
    url = remembered(answers, "url", "URL or hostname to probe",
                     default="https://example.com", required=True,
                     help_text="Probed from this machine: DNS, TCP, TLS and HTTP.")
    run(["health", url], label="network probe")


def action_cert(answers: dict, state: dict) -> None:
    raw = remembered(answers, "domains", "Domain(s) to check, space-separated",
                     required=True, help_text="TLS handshake only — free, no AWS access.")
    argv = ["cert", "check"]
    for domain in raw.split():
        argv += ["--domain", domain]
    run(argv, label="certificate expiry")


def action_tests(answers: dict, state: dict) -> None:
    print(f"\n{DIM}$ python -m unittest discover -s tests{RESET}\n")
    env = dict(os.environ, PYTHONPATH=f"{SRC}{os.pathsep}{ROOT}")
    code = subprocess.call(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT, env=env,
    )
    print(f"\n{(GREEN if code == 0 else RED)}unittest exit {code}{RESET}")


def action_prices(answers: dict, state: dict) -> None:
    run(["cost", "prices"], label="price table")


def _scope(answers: dict, state: dict, need_service: bool = True) -> list[str] | None:
    """Collect the common AWS scope flags."""
    if not state.get("boto3"):
        print(f"\n{RED}This needs boto3: pip install -e '.[aws]'{RESET}")
        return None
    if not state.get("identity"):
        print(f"{YELLOW}No credentials resolved — the run will probably fail. "
              f"Try `aws sso login` or set AWS_PROFILE.{RESET}")

    region = remembered(answers, "region", "AWS region",
                        default=state.get("region") or "us-east-1", required=True)
    profile = remembered(answers, "profile", "AWS profile",
                         default=state.get("profile") or "",
                         help_text="Leave blank to use the default credential chain.")
    scope = ["--region", region]
    if profile:
        scope += ["--profile", profile]
    return scope


def _window(answers: dict) -> list[str]:
    print(f"\n{DIM}Windows are UTC. Relative offsets work: '-2h', '-45m'. "
          f"Leave blank for the last 30 minutes.{RESET}")
    start = ask("Window start", default="")
    if not start:
        minutes = ask("How many minutes back", default="30")
        return ["--minutes", minutes]
    end = ask("Window end", default="")
    return ["--start", start] + (["--end", end] if end else [])


def action_investigate(answers: dict, state: dict) -> None:
    scope = _scope(answers, state)
    if scope is None:
        return
    service = remembered(answers, "service", "Service name", required=True,
                         help_text="Used for log-group discovery and as the report label.")
    cluster = remembered(answers, "cluster", "ECS cluster name")
    db = remembered(answers, "db", "RDS instance identifier")
    target_group = remembered(answers, "target_group", "ALB target group name or ARN")

    argv = [*scope, "incident", "investigate", "--service", service]
    for flag, value in (("--cluster", cluster), ("--db", db),
                        ("--target-group", target_group)):
        if value:
            argv += [flag, value]
    argv += _window(answers)

    if ask_bool("\nSave the snapshot as incident.json so you can re-analyse it for free?",
                default=True):
        argv += ["-o", "incident.json"]

    if state.get("anthropic") and ask_bool("Add a Bedrock AI narrative (~$0.003)?",
                                           default=False):
        argv = ["--ai", *argv]

    real = confirm_spend(argv, answers)
    if real:
        run(real, label="incident investigation")
        if "-o" in real:
            print(f"\n{DIM}Re-analyse it free, as often as you like:"
                  f"\n  sre incident summarize incident.json"
                  f"\n  sre incident postmortem incident.json -o postmortem.md{RESET}")


def action_summarize(answers: dict, state: dict) -> None:
    path = ask("Snapshot file", default="incident.json", required=True,
               help_text="Re-analysing a saved snapshot is free and works offline.")
    if not Path(path).exists():
        print(f"{RED}{path} does not exist. Run an investigation with -o first, "
              f"or try: sre --demo incident summarize{RESET}")
        return
    argv = ["incident", "summarize", path]
    if state.get("anthropic") and ask_bool("Add a Bedrock AI narrative (~$0.003)?",
                                           default=False):
        argv = ["--ai", "--max-spend", str(answers.get("max_spend", 0.05)), *argv]
    run(argv, label="offline re-analysis")


def action_component(answers: dict, state: dict) -> None:
    # Ask what to look at before asking how to reach it — otherwise the region
    # prompt arrives before you have said what you are diagnosing.
    which = ask_choice("Which component?", [
        ("ecs", "ECS service — state, deployments, task failures"),
        ("rds", "RDS instance — status, connections, events"),
        ("alb", "Load balancer — target health and error rates"),
    ])
    scope = _scope(answers, state)
    if scope is None:
        return
    if which == "ecs":
        cluster = remembered(answers, "cluster", "ECS cluster name", required=True)
        service = remembered(answers, "service", "ECS service name", required=True)
        argv = [*scope, "aws", "ecs", "--cluster", cluster, "--service", service]
    elif which == "rds":
        instance = remembered(answers, "db", "RDS instance identifier", required=True)
        argv = [*scope, "aws", "rds", "--instance", instance]
    else:
        name = remembered(answers, "lb", "Load balancer name (or target group ARN)",
                          required=True)
        argv = [*scope, "aws", "alb", "--name", name]
    argv += _window(answers)

    real = confirm_spend(argv, answers)
    if real:
        run(real, label=f"{which} diagnosis")


def action_logs(answers: dict, state: dict) -> None:
    scope = _scope(answers, state)
    if scope is None:
        return
    service = remembered(answers, "service", "Service name", required=True)

    if ask_bool("\nJust list candidate log groups first? (free)", default=True):
        run([*scope, "logs", "groups", "--service", service], label="log group discovery")
        if not ask_bool("\nCarry on to the pattern investigation?", default=True):
            return

    argv = [*scope, "logs", "investigate", "--service", service] + _window(answers)
    group = ask("Specific log group", default="",
                help_text="Leave blank to use discovery.")
    if group:
        argv += ["--group", group]

    deep = ask_bool(
        "\nAdd --deep (CloudWatch Logs Insights)? This is billed per GB scanned "
        "and is the most expensive thing here", default=False,
    )
    if deep:
        argv = ["--deep", *argv]

    real = confirm_spend(argv, answers)
    if real:
        run(real, label="log investigation")


def action_slo(answers: dict, state: dict) -> None:
    scope = _scope(answers, state)
    if scope is None:
        return
    target_group = remembered(answers, "target_group",
                              "ALB target group name or ARN", required=True)
    objective = ask("Availability objective in percent", default="99.9")
    days = ask("SLO window in days", default="30")
    argv = [*scope, "slo", "calculate", "--target-group", target_group,
            "--objective", objective, "--days", days]

    real = confirm_spend(argv, answers)
    if real:
        run(real, label="error budget")


def action_dr(answers: dict, state: dict) -> None:
    scope = _scope(answers, state)
    if scope is None:
        return
    argv = [*scope, "dr", "validate"]
    db = remembered(answers, "db", "RDS instance identifier")
    if db:
        argv += ["--db", db]
    buckets = ask("S3 bucket(s) to check, space-separated", default="")
    for bucket in buckets.split():
        argv += ["--bucket", bucket]
    if len(argv) == len(scope) + 2:
        print(f"{RED}Nothing to validate — give a DB instance or a bucket.{RESET}")
        return
    run(argv, label="DR posture (all free calls)")


def action_estimate(answers: dict, state: dict) -> None:
    scope = _scope(answers, state) if state.get("boto3") else ["--region",
                                                              answers.get("region", "us-east-1")]
    if scope is None:
        return
    service = remembered(answers, "service", "Service name", required=True)
    cluster = remembered(answers, "cluster", "ECS cluster name")
    db = remembered(answers, "db", "RDS instance identifier")
    target_group = remembered(answers, "target_group", "ALB target group name or ARN")
    argv = ["--dry-run", *scope, "cost", "estimate", "--service", service]
    for flag, value in (("--cluster", cluster), ("--db", db),
                        ("--target-group", target_group)):
        if value:
            argv += [flag, value]
    run(argv, label="estimate only — nothing was called")


def action_cache(answers: dict, state: dict) -> None:
    run(["cache", "stats"], label="cache")
    if ask_bool("\nClear it?", default=False):
        run(["cache", "clear"], label="cache cleared")


ACTIONS = [
    ("demo", "Demo investigation — no AWS, no spend, proves the install", action_demo),
    ("health", "Health check a URL (DNS, TCP, TLS, HTTP) — free", action_health),
    ("cert", "Certificate expiry for one or more domains — free", action_cert),
    ("tests", "Run the test suite — free", action_tests),
    ("prices", "Show the price table — free", action_prices),
    ("estimate", "Price an investigation without calling AWS — free", action_estimate),
    ("investigate", "Full incident investigation (AWS)", action_investigate),
    ("summarize", "Re-analyse a saved snapshot — free", action_summarize),
    ("component", "Diagnose one component: ECS, RDS or ALB (AWS)", action_component),
    ("logs", "Investigate log patterns (AWS)", action_logs),
    ("slo", "Availability and error budget (AWS)", action_slo),
    ("dr", "Backup and DR validation (AWS, all free calls)", action_dr),
    ("cache", "Inspect or clear the response cache", action_cache),
]


def main() -> int:
    print(f"{BOLD}sre-toolkit — local runner{RESET}")
    print(f"{DIM}Everything below maps to a real `sre` command, which is printed "
          f"before it runs.{RESET}")

    state = preflight()
    if not state.get("python_ok"):
        print(f"\n{RED}Fix the items marked ✗ above before continuing.{RESET}")
        return 1

    answers = load_answers()
    if answers:
        print(f"\n{DIM}Reusing answers from {ANSWERS_PATH} — press Enter to accept "
              f"each default.{RESET}")

    try:
        while True:
            key = ask_choice("What would you like to run?",
                             [(k, label) for k, label, _ in ACTIONS])
            handler = next(fn for k, _, fn in ACTIONS if k == key)
            try:
                handler(answers, state)
            except Aborted:
                print(f"\n{DIM}cancelled{RESET}")
            except Exception as exc:  # noqa: BLE001 - keep the menu alive
                print(f"\n{RED}that run failed: {type(exc).__name__}: {exc}{RESET}")
            save_answers(answers)
            if not ask_bool("\nRun something else?", default=True):
                break
    except Aborted:
        pass

    save_answers(answers)
    print(f"\n{DIM}bye. Inputs remembered in {ANSWERS_PATH}{RESET}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Aborted:
        print()
        raise SystemExit(130) from None
