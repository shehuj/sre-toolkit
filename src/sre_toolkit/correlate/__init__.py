"""Deterministic correlation. This is the part that must work with no AI and no
network: given a snapshot, produce ranked hypotheses and the evidence for them.
"""

from .engine import correlate  # noqa: F401
