"""Reliability hunter base package (epic TASK-2026-00365, design v2).

Pure incident models, capture-time redaction, v1 fingerprints, ordered
attribution and global configuration. Only capture.py, supervisor.py,
endpoint.py and deadlines.py do I/O (Mongo insert, local spool, backoff
sleeps, Unix socket, one asyncio timer plus fail-open ticket deadline
patches); guard stays pure. The worker, diagnosis and tickets belong to
later children.
"""

from __future__ import annotations

from . import capture as capture
from . import config as config
from . import classification as classification
from . import deadlines as deadlines
from . import endpoint as endpoint
from . import fingerprint as fingerprint
from . import guard as guard
from . import invariants as invariants
from . import models as models
from . import sanitize as sanitize
from . import supervisor as supervisor
from . import worker as worker
from .config import validate_reliability_config as validate_reliability_config

_default_errors = validate_reliability_config(config.RELIABILITY_DEFAULTS)
if _default_errors:
    raise ValueError(f"reliability defaults invalid: {_default_errors[0]}")
del _default_errors
