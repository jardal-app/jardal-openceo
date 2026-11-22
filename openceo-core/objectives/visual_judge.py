"""Visual judge: one verdict per changed view at objective close.

Runs in the objectives final review (TASK-2026-00896, D): for every view
whose ui_gate captures changed past a pixel threshold against the
objective baseline, one isolated model call returns approved/not plus at
most three defects. Never per ticket, never in a loop: a failed view
earns exactly one correction round of fix tickets and a second verdict;
a second failure closes with the verdict in the delivery record.

Providers: muse-review first (``muse exec --image``, verified
repeatable flag). The gemini panel link (agyd/agyj/agy2 rotation) stays
wired but inactive: agy 1.5.9 advertises no headless image flag and the
community ``@file``/prompt-path intake needs file tools, which the
judge forbids (D4: no repo, no diff, no tools, no navigation). Flipping
``AGY_IMAGE_VERIFIED`` requires an Owner-authorized live probe.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "AGY_IMAGE_VERIFIED",
    "JUDGE_TIMEOUT_S",
    "MUSE_JUDGE_MODEL",
    "VISUAL_CHANGE_THRESHOLD",
    "Verdict",
    "build_judge_prompt",
    "capture_key",
    "changed_ratio_png",
    "changed_views",
    "design_summary",
    "final_failed_views",
    "judge_argv_agy",
    "judge_argv_muse",
    "judge_capture_keys",
    "judge_capture_specs",
    "latest_verdicts",
    "load_rubrics",
    "parse_verdict",
    "pending_views",
    "run_gate_worktree",
    "run_verdict",
    "sha256_file",
    "subprocess_run",
]

#: agy headless image intake is UNVERIFIED (D3): gemini verdicts refuse
#: until an Owner-authorized live probe proves a tool-free image path.
AGY_IMAGE_VERIFIED = False

#: Isolated judge model (the muse-review slot model).
MUSE_JUDGE_MODEL = "muse-spark-1.3-contributor"

#: gemini panel link (D2): model slug embeds effort; rotation over the
#: three Owner accounts.
GEMINI_JUDGE_MODEL = "gemini-3.8-flash-high"
GEMINI_ACCOUNTS = ("agyd", "agyj", "agy2")

#: Changed-pixel ratio above which a view counts as changed (D1).
VISUAL_CHANGE_THRESHOLD = 0.001

#: One verdict call budget (prompt carries rubric + paths, no repo).
JUDGE_TIMEOUT_S = 900

#: Marker heading holding the ≤30-line design summary for the judge.
DESIGN_SUMMARY_HEADING = "## Resumen para el juez"

_MAX_DEFECTS = 3
_DESIGN_SUMMARY_LINES = 30


def sha256_file(path: str | Path) -> str:
    """Hex digest of a file (capture identity)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _png_pixels(path: str | Path) -> tuple[int, int, bytes]:
    """Decode an 8-bit non-interlaced PNG to ``(w, h, raw)`` (no deps)."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {path}")
    width = height = 0
    color_type = bit_depth = -1
    interlace = 1
    raw = b""
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = (
                struct.unpack(">IIBBBBB", body))
        elif kind == b"IDAT":
            raw += body
        elif kind == b"IEND":
            break
        pos += 12 + length
    if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
        raise ValueError(f"unsupported PNG form: {path}")
    channels = 3 if color_type == 2 else 4
    stride = width * channels
    pixels = bytearray(height * stride)
    stream = zlib.decompress(raw)
    offset = 0
    previous = bytearray(stride)
    for row in range(height):
        kind = stream[offset]
        offset += 1
        current = bytearray(stream[offset:offset + stride])
        offset += stride
        if kind == 1:
            for index in range(channels, stride):
                current[index] = (current[index]
                                  + current[index - channels]) & 255
        elif kind == 2:
            for index in range(stride):
                current[index] = (current[index]
                                  + previous[index]) & 255
        elif kind == 3:
            for index in range(stride):
                left = current[index - channels] if index >= channels else 0
                current[index] = (current[index]
                                  + ((left + previous[index]) >> 1)) & 255
        elif kind == 4:
            for index in range(stride):
                left = current[index - channels] if index >= channels else 0
                up = previous[index]
                up_left = previous[index - channels] \
                    if index >= channels else 0
                pick = left + up - up_left
                dist = (abs(pick - left), abs(pick - up),
                        abs(pick - up_left))
                best = (left, up, up_left)[dist.index(min(dist))]
                current[index] = (current[index] + best) & 255
        elif kind != 0:
            raise ValueError(f"unknown PNG filter {kind}: {path}")
        pixels[row * stride:(row + 1) * stride] = current
        previous = current
    return width, height, bytes(pixels)


def changed_ratio_png(first: str | Path, second: str | Path) -> float:
    """Changed-byte ratio between two captures (size drift counts as all)."""
    try:
        width_a, height_a, raw_a = _png_pixels(first)
        width_b, height_b, raw_b = _png_pixels(second)
    except (OSError, ValueError, struct.error, zlib.error):
        return 1.0
    if (width_a, height_a) != (width_b, height_b) or not raw_a:
        return 1.0
    changed = sum(byte_a != byte_b
                  for byte_a, byte_b in zip(raw_a, raw_b))
    return changed / len(raw_a)


def _repo_root() -> Path:
    from catalog import catalog_root

    return catalog_root().parent


def _section(path: Path, heading: str, *, level: int = 2) -> str:
    """Markdown section body under an exact heading (fail-closed)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"rubric unreadable: {path}: {exc}") from exc
    marker = "#" * level + " " + heading
    try:
        start = next(index for index, line in enumerate(lines)
                     if line.strip() == marker)
    except StopIteration:
        raise ValueError(
            f"rubric section missing: {marker} in {path}") from None
    body: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("#" * level + " ") and not line.startswith(
                "#" * (level + 1) + " "):
            break
        body.append(line)
    text = "\n".join(body).strip()
    if not text:
        raise ValueError(f"rubric section empty: {marker} in {path}")
    return text


def load_rubrics(repo_root: str | Path | None = None) -> dict[str, str]:
    """Pinned checklists judging every view (D4; fail-closed).

    The unslop ``## Checklist`` and the ui-ux-pro-max canonical
    pre-delivery checklist, read from the vendored skills so the judge
    always grades against the pinned versions.
    """
    root = Path(repo_root) if repo_root is not None else _repo_root()
    return {
        "unslop": _section(
            root / "skills" / "dev" / "unslop" / "SKILL.md", "Checklist"),
        "ui_ux_pro_max": _section(
            root / "skills" / "dev" / "ui-ux-pro-max" / "references"
            / "pro-rules.md",
            "Pre-Delivery Checklist (canonical — the only one)"),
    }


def design_summary(repo: str | Path,
                   doc: str = "DESIGN.md") -> str | None:
    """≤30-line design summary for the judge, None when absent (D4)."""
    path = Path(repo) / doc
    if not path.is_file():
        return None
    try:
        text = _section(path, DESIGN_SUMMARY_HEADING.lstrip("# "),
                        level=2)
    except ValueError:
        return None
    lines = text.splitlines()
    if len(lines) > _DESIGN_SUMMARY_LINES:
        lines = lines[:_DESIGN_SUMMARY_LINES] + ["[…recortado a 30 líneas]"]
    return "\n".join(lines).strip() or None


def build_judge_prompt(view: str, rubrics: Mapping[str, str],
                       summary: str | None = None) -> str:
    """Minimal judge prompt: rubric + design summary, verdict JSON only."""
    parts = [
        "Eres un juez visual. Juzga la vista contra la rúbrica.",
        f"Vista: {view}",
        "",
        "RÚBRICA UNSLOP:",
        rubrics["unslop"],
        "",
        "RÚBRICA UI-UX-PRO-MAX (pre-delivery):",
        rubrics["ui_ux_pro_max"],
    ]
    if summary:
        parts += ["", "RESUMEN DE DISEÑO:", summary]
    parts += [
        "",
        "Responde SOLO con JSON: "
        '{"approved": true|false, "defects": '
        '[{"where": "...", "what": "...", "rule": "..."}]}. '
        "Máximo 3 defectos: dónde, qué y qué regla incumple.",
    ]
    return "\n".join(parts)


@dataclass
class Verdict:
    """One parsed visual verdict (D5)."""

    approved: bool
    defects: list[dict[str, str]] = field(default_factory=list)
    raw: str = ""


def parse_verdict(text: str) -> Verdict:
    """Parse the judge JSON (first top-level object; defects capped at 3)."""
    raw = str(text or "")
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in verdict")
    try:
        payload = json.loads(raw[start:end + 1])
    except ValueError as exc:
        raise ValueError(f"verdict is not JSON: {exc}") from exc
    if not isinstance(payload, Mapping) or not isinstance(
            payload.get("approved"), bool):
        raise ValueError("verdict needs a boolean approved")
    defects: list[dict[str, str]] = []
    entries = payload.get("defects") or []
    if not isinstance(entries, list):
        raise ValueError("verdict defects must be a list")
    for entry in entries[:_MAX_DEFECTS]:
        if not isinstance(entry, Mapping):
            raise ValueError("verdict defect must be an object")
        where = str(entry.get("where") or "").strip()
        what = str(entry.get("what") or "").strip()
        rule = str(entry.get("rule") or "").strip()
        if not where or not what or not rule:
            raise ValueError("verdict defect needs where/what/rule")
        defects.append({"where": where[:300], "what": what[:500],
                        "rule": rule[:300]})
    approved = bool(payload["approved"])
    if not approved and not defects:
        raise ValueError("a failing verdict names its defects")
    return Verdict(approved=approved, defects=defects, raw=raw)


def judge_argv_muse(images: Sequence[str | Path],
                    model: str = MUSE_JUDGE_MODEL) -> list[str]:
    """Isolated muse judge argv (images attached, prompt appended by caller).

    Mirrors the muse-review slot (plain ``muse``, model + max effort);
    isolation comes from the fence and the missing ``--trust-workspace``.
    """
    argv = ["muse", "exec", "--model", model, "--reasoning-effort", "max"]
    for image in images:
        argv += ["--image", str(image)]
    return argv


def judge_argv_agy(prompt: str, account: str = GEMINI_ACCOUNTS[0],
                   model: str = GEMINI_JUDGE_MODEL) -> list[str]:
    """agy judge argv (UNVERIFIED image path; refuses until proven).

    Community reports say prompt-embedded absolute paths let the model
    read images with file tools — tools the judge forbids (D4). Until an
    Owner-authorized live probe proves a tool-free intake, building this
    argv raises instead of running a crippled judge.
    """
    if not AGY_IMAGE_VERIFIED:
        raise ValueError(
            "agy image intake is unverified (D3): muse-review judges until "
            "an Owner-authorized live probe proves a tool-free path")
    if account not in GEMINI_ACCOUNTS:
        raise ValueError(f"not a gemini panel account: {account!r}")
    return [account, "--model", model, "--print", prompt]


def _gemini_account(salt: int) -> str:
    return GEMINI_ACCOUNTS[salt % len(GEMINI_ACCOUNTS)]


async def _default_run_cli(argv: Sequence[str], cwd: str | Path,
                           timeout_s: int,
                           ) -> tuple[int, str, Mapping[str, Any]]:
    """Fenced one-shot CLI run (no repo, no badge, no tools)."""
    from orchestration.agent_sandbox import plan_agent

    workdir = Path(cwd)
    plan = plan_agent(argv=[str(token) for token in argv],
                      workspace=str(workdir), base={}, overlay={},
                      config_args=[str(argv[0])])
    proc = await asyncio.create_subprocess_exec(
        *plan.argv, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, cwd=plan.cwd, env=plan.env)
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return 124, "", {"timeout": True}
    return (proc.returncode or 0,
            (stdout or b"").decode("utf-8", "replace"),
            {"truncated": False})


async def run_verdict(
    view: str,
    images: Sequence[str | Path],
    prompt: str,
    workdir: str | Path,
    *,
    providers: Sequence[str] = ("muse-review", "gemini"),
    salt: int = 0,
    timeout_s: int = JUDGE_TIMEOUT_S,
    run_cli: Callable[[Sequence[str], str | Path, int],
                      Awaitable[tuple[int, str, Mapping[str, Any]]]
                      ] | None = None,
) -> dict[str, Any]:
    """One judged view: first working provider wins (no loop, no retry).

    Returns the verdict record (view/provider/approved/defects/tokens).
    Raises :class:`ValueError` when no provider judges (malformed
    output counts as a provider failure and falls through once).
    """
    runner = run_cli or _default_run_cli
    failures: list[str] = []
    for index, provider in enumerate(providers):
        try:
            if provider == "muse-review":
                argv: Sequence[str] = [
                    *judge_argv_muse(images), prompt]
            elif provider == "gemini":
                argv = judge_argv_agy(
                    prompt + "\n\nCapturas:\n" + "\n".join(
                        str(image) for image in images),
                    _gemini_account(salt + index))
            else:
                failures.append(f"{provider}: unknown provider")
                continue
            returncode, output, usage = await runner(
                argv, workdir, timeout_s)
            if returncode != 0:
                failures.append(
                    f"{provider}: exit {returncode}: {output[:200]}")
                continue
            try:
                verdict = parse_verdict(output)
            except ValueError as exc:
                failures.append(f"{provider}: {exc}")
                continue
            tokens = usage if isinstance(usage, Mapping) else {}
            return {
                "view": view,
                "provider": provider,
                "approved": verdict.approved,
                "defects": verdict.defects,
                "tokens_in": tokens.get("tokens_in"),
                "tokens_out": tokens.get("tokens_out"),
                "at": datetime.now(timezone.utc).isoformat(),
            }
        except ValueError as exc:
            failures.append(f"{provider}: {exc}")
            continue
    raise ValueError("no visual judge available: "
                     + "; ".join(failures[:4]))


def latest_verdicts(verdicts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Latest verdict record per view (append-only log → current state)."""
    latest: dict[str, Any] = {}
    for entry in verdicts or []:
        if isinstance(entry, Mapping) and entry.get("view"):
            latest[str(entry["view"])] = dict(entry)
    return latest


def final_failed_views(
        verdicts: Sequence[Mapping[str, Any]]) -> list[str]:
    """Views whose latest verdict failed at round 2 (no rounds left)."""
    failed: list[str] = []
    for view, entry in sorted(latest_verdicts(verdicts).items()):
        try:
            round_no = int(entry.get("round", 0))
        except (TypeError, ValueError):
            round_no = 0
        if entry.get("approved") is False and round_no >= 2:
            failed.append(view)
    return failed


def pending_views(changed: Sequence[str],
                  verdicts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Changed views still owed a verdict: ``[{view, round}]`` (max 2)."""
    latest = latest_verdicts(verdicts)
    pending: list[dict[str, Any]] = []
    for view in changed:
        entry = latest.get(str(view))
        if entry is None:
            pending.append({"view": str(view), "round": 1})
            continue
        if entry.get("approved") is True:
            continue
        try:
            round_no = int(entry.get("round", 0))
        except (TypeError, ValueError):
            round_no = 0
        if round_no < 2:
            pending.append({"view": str(view), "round": round_no + 1})
    return pending


def judge_capture_keys(cfg: Mapping[str, Any], route: str) -> tuple[str, str]:
    """The two judged captures per view (D4: wide-light + narrow-dark)."""
    return tuple(key for key, _width, _theme
                 in judge_capture_specs(cfg, route))


def judge_capture_specs(
        cfg: Mapping[str, Any], route: str,
) -> tuple[tuple[str, int, str], tuple[str, int, str]]:
    """Judged captures as ``(file key, width, theme)`` (no parsing back)."""
    from qa.ui_gate import slug_for_route

    widths = [int(item) for item in (cfg.get("widths") or [1440])]
    themes = [str(item) for item in (cfg.get("themes") or ["light"])]
    light_theme = "light" if "light" in themes else themes[0]
    dark_theme = "dark" if "dark" in themes else themes[-1]
    slug = slug_for_route(route)
    return ((f"{slug}-{widths[0]}-{light_theme}", widths[0], light_theme),
            (f"{slug}-{widths[-1]}-{dark_theme}", widths[-1], dark_theme))


def capture_key(route: str, width: int, theme: str) -> str:
    """Baseline identity of one capture (route literal, not slug)."""
    return f"{route}|{width}|{theme}"


def changed_views(cfg: Mapping[str, Any],
                  baseline: Mapping[str, Any],
                  head_dir: str | Path,
                  baseline_dir: str | Path | None) -> list[str]:
    """Routes whose judged captures changed past the threshold (D1).

    No baseline at all means every routed view changed. A sha match
    short-circuits; otherwise the pixel ratio decides.
    """
    base_hashes = dict((baseline or {}).get("captures") or {})
    changed: list[str] = []
    for route in cfg.get("routes") or []:
        route_changed = False
        for key, width, theme in judge_capture_specs(cfg, str(route)):
            head_file = Path(head_dir) / f"{key}.png"
            if not head_file.is_file():
                route_changed = True
                break
            try:
                head_sha = sha256_file(head_file)
            except OSError:
                route_changed = True
                break
            base_sha = base_hashes.get(capture_key(str(route), width, theme))
            if base_sha is None or baseline_dir is None:
                route_changed = True
                break
            if base_sha == head_sha:
                continue
            base_file = Path(baseline_dir) / f"{key}.png"
            if changed_ratio_png(base_file, head_file) \
                    > VISUAL_CHANGE_THRESHOLD:
                route_changed = True
                break
        if route_changed:
            changed.append(str(route))
    return changed


def subprocess_run(cmd: str, cwd: str | Path, timeout_s: int) -> int:
    """Shell one judge prep command, returning its exit code (test seam)."""
    import subprocess as _subprocess

    try:
        proc = _subprocess.run(
            cmd, cwd=str(cwd), shell=True, stdout=_subprocess.PIPE,
            stderr=_subprocess.STDOUT, timeout=timeout_s, text=True,
            errors="replace")
    except _subprocess.TimeoutExpired as exc:
        raise ValueError(f"judge command timed out: {cmd[:120]}") from exc
    except OSError as exc:
        raise ValueError(f"judge command not started: {exc}") from exc
    if proc.returncode != 0:
        raise ValueError(
            f"judge command failed ({cmd[:120]}): "
            f"{(proc.stdout or '')[-500:]}")
    return proc.returncode


async def run_gate_worktree(repo: str | Path, sha: str, tag: str,
                            cfg: Mapping[str, Any], out_dir: str | Path,
                            ticket: str) -> dict[str, Any]:
    """ui_gate report for one sha inside a throwaway worktree (judge-owned).

    Installs and builds with the judge-only commands when the project
    declares them; ticket gate runs never pay for either.
    """
    from orchestration import review as review_mod
    from qa import ui_gate as gate_mod

    repo_path = Path(repo)
    worktree = await asyncio.to_thread(
        review_mod.create_worktree, repo_path, sha, f"visual-{tag}")
    try:
        install = (cfg.get("judge_install_cmd") or "").strip() \
            if isinstance(cfg.get("judge_install_cmd"), str) else ""
        build = (cfg.get("judge_build_cmd") or cfg.get("build_cmd") or "")
        build = build.strip() if isinstance(build, str) else ""
        if install:
            await asyncio.to_thread(subprocess_run, install, worktree, 600)
        if build:
            await asyncio.to_thread(subprocess_run, build, worktree, 600)
        return await asyncio.to_thread(
            gate_mod.run_ui_gate, dict(cfg), worktree, out_dir, ticket)
    finally:
        await asyncio.to_thread(
            review_mod.remove_worktree, repo_path, worktree)
