"""Exceptions that the CLI turns into clean messages instead of tracebacks."""


class SreToolkitError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 1


class MissingDependency(SreToolkitError):
    """An optional extra is needed for this command."""

    exit_code = 4

    def __init__(self, module: str, extra: str):
        super().__init__(
            f"'{module}' is not installed. Install the optional extra:\n"
            f"    pip install 'sre-toolkit[{extra}]'"
        )


class BudgetExceeded(SreToolkitError):
    """The run would cost more than --max-spend allows."""

    exit_code = 5


class CollectorError(SreToolkitError):
    """An API call failed — usually credentials, region or permissions.

    Deliberately not exit code 1: that means "read the telemetry, found warnings",
    which is the opposite of "could not read the telemetry at all".
    """

    exit_code = 6
