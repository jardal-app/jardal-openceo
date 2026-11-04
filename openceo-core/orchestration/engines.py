"""Engine registry: one CLI per agent run (TASK-2026-00246).

An agent card names an engine (CLI family) and optionally an account;
``params.engine`` / ``params.engine_account`` override both for one
ticket. Precedence, documented and validated in :func:`resolve_engine`:

1. ``params.engine`` when non-blank, else the card engine. Unknown
   engines fail, never fall back to muse silently;
2. ``params.engine_account`` when non-blank, which must fit the resolved
   engine;
3. else the card account when it fits the resolved engine. A card
   account left over from another engine falls back to the default: an
   engine override resets the account unless the ticket overrides it
   too;
4. else the engine default account.

The active attempt keeps the resolved selection: the spec is built
once per run and recorded in ``params._engine_attempt``, so a config
change mid-run cannot hot-swap the process; the next invocation
re-resolves. Recovery (TASK-2026-00247) re-resolves too: the record is
history, and per-run cost rows carry their own engine.

Availability: :func:`check_engine_available` (install, runs before
every run) and :func:`probe_engine_version` (diagnostic ``--version``,
subprocess faked in tests). Auth state is verified offline and declared
per engine; a run whose CLI rejects its login fails with the CLI's own
stderr. Families group selections for independence judgments: the three
agy accounts share one family and never form a quorum among themselves.

Adding an engine: one ``EngineSpec`` here, its accounts in
``catalog.ENGINE_ACCOUNTS``, one ``role_<cli>.py`` translator (argv
extras + badged MCP), one provider branch in ``live.py``, one sandbox
entry when it needs home state, and the parametrized cases in
``tests/test_cli_engines_246.py``. Unknown engines fail closed.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from catalog import ENGINE_ACCOUNTS, KNOWN_ENGINES

__all__ = [
    "ATTEMPT_PARAMS_KEY",
    "ENGINE_ACCOUNT_PARAMS_KEY",
    "ENGINE_PARAMS_KEY",
    "ENGINE_SPECS",
    "EngineResolutionError",
    "EngineSelection",
    "EngineSpec",
    "check_engine_available",
    "command_for",
    "family_of",
    "probe_engine_version",
    "resolve_engine",
]

#: Ticket override keys (explicit per-ticket engine selection).
ENGINE_PARAMS_KEY = "engine"
ENGINE_ACCOUNT_PARAMS_KEY = "engine_account"

#: Recorded selection of the attempt that produced these params.
ATTEMPT_PARAMS_KEY = "_engine_attempt"


@dataclass(frozen=True)
class EngineSpec:
    """One selectable engine: provider, model default, base argv."""

    engine: str
    provider: str
    family: str
    model: str
    effort: str
    command: tuple[str, ...]
    headless_args: tuple[str, ...]
    prompt_via: str
    translator: str


#: The seven selections (TASK-2026-00246): four single-account engines
#: plus agy with its three Owner logins. Models mirror the wrapper pins
#: where the wrapper pins one (musej, claudej, codexf); empty means the
#: account default. Must stay keyed exactly by ``catalog.KNOWN_ENGINES``
#: (pinned in tests, single vocabulary).
ENGINE_SPECS: dict[str, EngineSpec] = {
    "muse-code": EngineSpec(
        engine="muse-code",
        provider="muse",
        family="muse",
        model="muse-spark-1.3-contributor",
        effort="max",
        command=("musej", "exec", "--model",
                 "muse-spark-1.3-contributor"),
        headless_args=(),
        prompt_via="argv",
        translator="muse",
    ),
    "claude-code": EngineSpec(
        engine="claude-code",
        provider="claude",
        family="claude",
        model="opus",
        effort="max",
        command=("claudej",),
        headless_args=("--print",),
        prompt_via="argv",
        translator="claude",
    ),
    "opencode": EngineSpec(
        engine="opencode",
        provider="opencode",
        family="opencode",
        model="",
        effort="",
        command=("opencode", "run"),
        headless_args=(),
        prompt_via="argv",
        translator="opencode",
    ),
    "codex-cli": EngineSpec(
        engine="codex-cli",
        provider="codex",
        family="codex",
        model="gpt-6.1-sol",
        effort="xhigh",
        command=("codexf", "exec", "--ephemeral"),
        headless_args=(),
        prompt_via="stdin",
        translator="codex",
    ),
    "agy": EngineSpec(
        engine="agy",
        provider="agy",
        family="gemini",
        model="",
        effort="",
        # Empty: the account IS the command (agyd/agyj/agy2);
        # command_for() resolves it per selection.
        command=(),
        headless_args=(),
        prompt_via="argv",
        translator="agy",
    ),
}


class EngineResolutionError(ValueError):
    """An engine/account selection is invalid; the message says why."""


@dataclass(frozen=True)
class EngineSelection:
    """Resolved engine + account for one run.

    ``via`` names the origin of the engine ("params" when the ticket
    overrides it, else "card"), not of the account: an account-only
    override still records ``via="card"`` for a card engine.
    """

    engine: str
    account: str
    via: str


def _known_engines() -> str:
    return ", ".join(sorted(ENGINE_SPECS))


def resolve_engine(
    card_engine: Any,
    card_account: Any,
    params: Mapping[str, Any] | None,
) -> EngineSelection:
    """Resolve the engine + account for one run (precedence above)."""
    mapping = params if isinstance(params, Mapping) else {}
    want_engine = str(mapping.get(ENGINE_PARAMS_KEY) or "").strip()
    want_account = str(mapping.get(ENGINE_ACCOUNT_PARAMS_KEY) or "").strip()
    card_engine_clean = str(card_engine or "").strip()
    engine = want_engine or card_engine_clean
    spec = ENGINE_SPECS.get(engine)
    if spec is None:
        origin = f"{ENGINE_PARAMS_KEY} override" if want_engine \
            else "card engine"
        raise EngineResolutionError(
            f"unknown engine {engine!r} in {origin} "
            f"(known: {_known_engines()})")
    accounts = ENGINE_ACCOUNTS.get(engine, ())
    if want_account:
        if want_account not in accounts:
            raise EngineResolutionError(
                f"{ENGINE_ACCOUNT_PARAMS_KEY} override {want_account!r} "
                f"is not an account of engine {engine!r} "
                f"(known: {', '.join(accounts)})")
        return EngineSelection(engine, want_account,
                               "params" if want_engine else "card")
    card_account_clean = str(card_account or "").strip()
    overridden = bool(want_engine) and want_engine != card_engine_clean
    if card_account_clean and not overridden:
        if card_account_clean not in accounts:
            raise EngineResolutionError(
                f"card account {card_account_clean!r} is not an account "
                f"of engine {engine!r} (known: {', '.join(accounts)})")
        return EngineSelection(engine, card_account_clean, "card")
    return EngineSelection(engine, accounts[0],
                           "params" if want_engine else "card")


def command_for(engine: str, account: str) -> list[str]:
    """Base argv for a resolved selection (translator extras come later)."""
    spec = ENGINE_SPECS.get(engine)
    if spec is None:
        raise EngineResolutionError(
            f"unknown engine {engine!r} (known: {_known_engines()})")
    if engine == "agy":
        if account not in ENGINE_ACCOUNTS.get(engine, ()):
            raise EngineResolutionError(
                f"account {account!r} is not an agy account")
        return [account]
    return list(spec.command)


def check_engine_available(
    engine: str,
    account: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """Install check: None when runnable, the concrete reason when not."""
    try:
        argv = command_for(engine, account)
    except EngineResolutionError as exc:
        return str(exc)
    binary = argv[0]
    if "/" in binary or "\\" in binary:
        if Path(binary).is_file():
            return None
        return (f"engine {engine}/{account} unavailable: command "
                f"{binary!r} is not a file")
    if which(binary) is None:
        return (f"engine {engine}/{account} unavailable: command "
                f"{binary!r} not found on PATH")
    return None


def probe_engine_version(
    engine: str,
    account: str,
    *,
    run: Callable[..., Any] = subprocess.run,
    timeout_s: int = 15,
) -> dict[str, Any]:
    """Diagnostic ``--version`` report (never part of run admission)."""
    try:
        argv = command_for(engine, account)
    except EngineResolutionError as exc:
        return {"engine": engine, "account": account, "command": [],
                "ok": False, "version": "", "reason": str(exc)}
    argv = [*argv[:1], "--version"]
    try:
        proc = run(argv, capture_output=True, text=True,
                   timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"engine": engine, "account": account, "command": argv,
                "ok": False, "version": "",
                "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        detail = (str(proc.stderr or proc.stdout or "").strip()
                  .splitlines() or [""])
        return {"engine": engine, "account": account, "command": argv,
                "ok": False, "version": "",
                "reason": f"rc={proc.returncode}: {detail[0][:120]}"}
    lines = (str(proc.stdout or proc.stderr or "").strip()
             .splitlines() or [""])
    return {"engine": engine, "account": account, "command": argv,
            "ok": True, "version": lines[0][:120], "reason": ""}


def family_of(engine: str) -> str:
    """Model family of an engine (quorum independence judgments)."""
    spec = ENGINE_SPECS.get(engine)
    if spec is None:
        raise EngineResolutionError(
            f"unknown engine {engine!r} (known: {_known_engines()})")
    return spec.family


def _check_vocab() -> None:
    if set(ENGINE_SPECS) != set(KNOWN_ENGINES):
        raise EngineResolutionError(
            "engine vocabulary drift: registry "
            f"{sorted(ENGINE_SPECS)} != catalog {sorted(KNOWN_ENGINES)}")
    for engine in ENGINE_SPECS:
        if not ENGINE_ACCOUNTS.get(engine):
            raise EngineResolutionError(
                f"engine {engine!r} has no accounts in the catalog")


_check_vocab()
