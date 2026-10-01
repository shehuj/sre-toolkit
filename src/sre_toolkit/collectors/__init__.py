"""Collectors turn the outside world into Signals, TimelineEvents and Resources.

Every collector takes a Context so it can be cached, priced and budget-checked,
and must never raise for a single failed probe — it records the failure instead.
"""
