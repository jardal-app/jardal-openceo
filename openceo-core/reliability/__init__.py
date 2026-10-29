"""Reliability hunter base package (epic TASK-2026-00365, design v2).

Pure incident models, capture-time redaction, v1 fingerprints, ordered
attribution and global configuration. No I/O: later children own Mongo,
the worker, diagnosis and tickets.
"""

from __future__ import annotations

from . import config as config
from . import classification as classification
from . import fingerprint as fingerprint
from . import models as models
from . import sanitize as sanitize
from .config import validate_reliability_config as validate_reliability_config

_default_errors = validate_reliability_config(config.RELIABILITY_DEFAULTS)
if _default_errors:
    raise ValueError(f"reliability defaults invalid: {_default_errors[0]}")
del _default_errors
