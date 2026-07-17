"""Shared data shapes across fetch sources (NYT, RSS) and the digest pipeline.

Split out from digest.py so a fetch module (rss.py) can produce ``Candidate``
without importing digest.py, which imports fetch functions FROM rss.py —
would otherwise be circular.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Candidate:
    title: str
    abstract: str
    url: str
    section: str
