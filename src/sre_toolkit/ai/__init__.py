"""Optional AI analysis.

The split here is the whole cost story of this project: `offline.py` always runs
and costs nothing; `analyzer.py` only runs behind `--ai`, is fed a compacted
context instead of raw telemetry, and is metered token-by-token.
"""
