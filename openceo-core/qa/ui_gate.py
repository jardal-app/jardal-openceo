"""Project UI gate: deterministic render checks with no model behind them.

Runs on the orchestrator host as a required project gate (``ui_gate``):
it serves the built web UI, visits every configured route at every
width and theme through the agent-browser CLI, and fails on layout,
font, icon, render-leak and accessibility defects. No Playwright, no
Puppeteer, no model calls, no tokens: every browser step is a CLI
invocation driven by this script (TASK-2026-00896, Owner order
2026-11-22: agent-browser only).

Configuration lives in the project's ``project.yaml`` under
``dev.qa.ui_gate`` (shape-checked by :func:`validate_ui_gate_config`):

* ``preview_cmd`` (required): serves the UI, e.g. ``vite preview``;
  ``build_cmd`` (optional) runs first when the gate must build;
  ``url`` (required): base URL the preview serves;
* ``routes`` (required): literal paths to visit;
  ``widths`` (default ``[1440, 390]``), ``themes`` (default
  ``[light, dark]``), ``viewport_height`` (default 900);
* ``selectors``: ``content`` (default ``main``) and ``nav``
  (default ``nav, aside``) used for the margin checks;
* ``checks``: subset to judge (default all of ``margin width layout
  scroll fonts icons leaks axe``); projects without a synthetic
  session yet enable only what their public pages can prove;
* ``min_margin`` (default 24, desktop) and ``min_margin_mobile``
  (default 16, widths under 768); ``max_width`` (default 1600);
* ``fonts``: text families the design document declares;
* ``icons``: ``[{selector, min}]`` counts that must be present;
* ``network_routes``: ``[{pattern, body | body_file}]`` API mocks
  registered before the first navigation (one body per pattern:
  the CLI cannot split one path by HTTP method);
* ``setup_eval`` / ``setup_eval_file`` (exclusive): JS evaluated once
  per browser session after the first navigation (storage seeding);
* ``settle_ms`` (default 2000), ``timeout_minutes`` (default 10);
* ``judge_install_cmd`` / ``judge_build_cmd`` (optional): dependency
  install and build the visual judge runs inside its throwaway
  worktrees (baseline and head); ticket gate runs never use them.

``preview_cmd`` and ``url`` may carry a ``{port}`` placeholder: each run
picks a free loopback port, so concurrent gates never clash. A literal
port keeps working.

Output lands in ``<repo>/.openceo/ui-gate/<ticket>/`` (outside git and
outside the squash by construction): one PNG per route, width and
theme plus ``report.json``. The visual judge (D) reuses those captures,
so no view is captured twice. Exit 0 passes, 1 reports defects, 2
signals a usage or config error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "AXE_FILENAME",
    "GateConfigError",
    "GateFailure",
    "check_axe",
    "check_consistency",
    "check_fonts",
    "check_icons",
    "check_margins",
    "check_max_width",
    "check_scroll",
    "load_ui_gate_config",
    "normalize_ui_gate_config",
    "resolve_gate_port",
    "run_ui_gate",
    "scan_leaks",
    "slug_for_route",
    "validate_ui_gate_config",
]

#: Vendored axe-core, injected as an init script (pinned, no autoupdate).
AXE_FILENAME = "axe.min.js"
AXE_VERSION = "4.14.0"
AXE_SHA256 = "20c09fe157a8a34a30e241aaa1fcdade657734f08ab379ecfbeb7d45cc46e878"

_DEFAULT_WIDTHS = (1440, 390)
_DEFAULT_THEMES = ("light", "dark")
_MOBILE_UNDER = 768
_ALIGN_TOLERANCE_PX = 2
_CENTER_TOLERANCE_PX = 2
_ALL_CHECKS = ("margin", "width", "layout", "scroll", "fonts", "icons",
               "leaks", "axe")

_GENERIC_FONT_FAMILIES = frozenset({
    "serif", "sans-serif", "monospace", "cursive", "fantasy",
    "system-ui", "emoji", "math", "fangsong", "ui-serif",
    "ui-sans-serif", "ui-monospace", "ui-rounded",
})

_LEAK_PATTERNS = (
    ("undefined", re.compile(r"\bundefined\b")),
    ("null", re.compile(r"\bnull\b")),
    ("nan", re.compile(r"\bNaN\b")),
    ("object", re.compile(r"\[object Object\]")),
    ("template", re.compile(r"\{\{")),
    ("i18n", re.compile(
        r"\b[a-z][a-zA-Z0-9]*(?:\.[a-z][a-zA-Z0-9]*){2,}\b")),
    ("uuid", re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)),
)


class GateConfigError(ValueError):
    """The ui_gate config is missing or malformed; the message says how."""


class GateFailure(RuntimeError):
    """Infrastructure failure (server, browser, timeout); not a UI defect."""


def slug_for_route(route: str) -> str:
    """Filesystem slug for a route (``/`` → ``index``)."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(route or "").strip())
    return cleaned.strip("-").lower() or "index"


def scan_leaks(text: str) -> list[dict[str, str]]:
    """Universal render leaks in visible text (B3; nothing Fuente-specific)."""
    found: list[dict[str, str]] = []
    for kind, pattern in _LEAK_PATTERNS:
        match = pattern.search(text or "")
        if match is not None:
            start = max(0, match.start() - 40)
            sample = (text[start:match.end() + 40]).replace("\n", " ")
            found.append({"kind": kind, "sample": sample.strip()[:100]})
    return found


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def check_margins(metrics: Mapping[str, Any],
                  min_margin: float) -> list[str]:
    """Nav→content and right gaps at or above the minimum (B3)."""
    defects: list[str] = []
    content = metrics.get("content") or {}
    viewport = metrics.get("viewport") or {}
    left = _num(content.get("x"))
    width = _num(content.get("width"))
    viewport_width = _num(viewport.get("w"))
    nav_right = _num(metrics.get("nav_right"))
    if left is None or width is None or viewport_width is None:
        return ["margin metrics missing"]
    gap = (left - nav_right) if nav_right is not None else left
    if gap + 1e-9 < min_margin:
        where = "nav-to-content" if nav_right is not None else "left page"
        defects.append(f"{where} margin {gap:.1f}px under {min_margin:g}px")
    right = viewport_width - (left + width)
    if right + 1e-9 < min_margin:
        defects.append(f"right margin {right:.1f}px under {min_margin:g}px")
    return defects


def check_max_width(metrics: Mapping[str, Any],
                    max_width: float) -> list[str]:
    """Content within the maximum, centered when narrower (B3)."""
    content = metrics.get("content") or {}
    viewport = metrics.get("viewport") or {}
    left = _num(content.get("x"))
    width = _num(content.get("width"))
    viewport_width = _num(viewport.get("w"))
    if left is None or width is None or viewport_width is None:
        return ["width metrics missing"]
    if width - 1e-9 > max_width:
        return [f"content {width:.0f}px wider than {max_width:g}px"]
    if viewport_width > max_width and width < viewport_width - 1:
        right = viewport_width - (left + width)
        if abs(left - right) > _CENTER_TOLERANCE_PX:
            return [f"content off-center by {abs(left - right):.1f}px"]
    return []


def check_consistency(reference: Mapping[str, Any],
                      current: Mapping[str, Any]) -> list[str]:
    """Same h1 x and content box across routes at one width (±2px, B3)."""
    defects: list[str] = []
    for label, ref_box, cur_box in (
            ("h1", reference.get("h1"), current.get("h1")),
            ("content", reference.get("content"), current.get("content"))):
        if not isinstance(ref_box, Mapping) \
                or not isinstance(cur_box, Mapping):
            continue
        for key in ("x", "width"):
            ref_num = _num(ref_box.get(key))
            cur_num = _num(cur_box.get(key))
            if ref_num is None or cur_num is None:
                continue
            if abs(ref_num - cur_num) > _ALIGN_TOLERANCE_PX:
                defects.append(
                    f"{label} {key} {cur_num:.1f}px differs "
                    f"from {ref_num:.1f}px")
    return defects


def check_scroll(metrics: Mapping[str, Any]) -> list[str]:
    """No horizontal scroll (1px tolerance, B3)."""
    viewport = metrics.get("viewport") or {}
    viewport_width = _num(viewport.get("w"))
    scroll_width = _num(metrics.get("scroll_width"))
    if viewport_width is None or scroll_width is None:
        return ["scroll metrics missing"]
    if scroll_width > viewport_width + 1:
        return [f"horizontal scroll: {scroll_width:.0f}px "
                f"in {viewport_width:.0f}px"]
    return []


def _first_named_family(stack: str) -> str | None:
    for chunk in str(stack or "").split(","):
        name = chunk.strip().strip("'\"").lower()
        if name and name not in _GENERIC_FONT_FAMILIES:
            return name
    return None


def check_fonts(metrics: Mapping[str, Any],
                declared: Sequence[str]) -> list[str]:
    """Declared families loaded, no text on the fallback (B3)."""
    if not declared:
        return []
    wanted = {str(name).strip().lower(): str(name).strip()
              for name in declared if str(name).strip()}
    if not wanted:
        return []
    loaded = {str(name).strip().lower()
              for name, ok in (metrics.get("fonts_loaded") or {}).items()
              if ok}
    defects = [f"font {wanted[name]} declared but not loaded"
               for name in sorted(set(wanted) - loaded)]
    for stack in (metrics.get("font_stacks") or [])[:60]:
        first = _first_named_family(stack)
        if first is None or first not in loaded:
            defects.append("text painted with the fallback font: "
                           f"{str(stack)[:80]}")
            break
    return defects


def check_icons(metrics: Mapping[str, Any],
                icons: Sequence[Mapping[str, Any]]) -> list[str]:
    """Declared icon counts present (B3)."""
    counts = metrics.get("icon_counts") or {}
    defects: list[str] = []
    for entry in icons or []:
        if not isinstance(entry, Mapping):
            continue
        selector = str(entry.get("selector") or "")
        minimum = entry.get("min", 1)
        try:
            want = int(minimum)
        except (TypeError, ValueError):
            want = 1
        found = counts.get(selector, 0)
        try:
            have = int(found)
        except (TypeError, ValueError):
            have = 0
        if have < want:
            defects.append(f"icons {selector!r}: {have} under {want}")
    return defects


def check_axe(violations: Sequence[Mapping[str, Any]]) -> list[str]:
    """Serious or critical axe violations fail (B3)."""
    defects: list[str] = []
    for entry in violations or []:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("impact") or "") in ("serious", "critical"):
            defects.append(f"axe {entry.get('id')}: "
                           f"{entry.get('impact')} "
                           f"x{entry.get('nodes', '?')}")
    return sorted(defects)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_ui_gate_config(dev_cfg: Mapping[str, Any] | None) -> list[str]:
    """Human-readable errors for ``dev.qa.ui_gate``, [] when valid/absent."""
    qa = (dev_cfg or {}).get("qa")
    cfg = qa.get("ui_gate") if isinstance(qa, Mapping) else None
    if cfg is None:
        return []
    if not isinstance(cfg, Mapping):
        return ["dev.qa.ui_gate must be a mapping"]
    errors: list[str] = []

    def _bad(what: str) -> None:
        errors.append(f"dev.qa.ui_gate.{what}")

    if not _is_nonempty_str(cfg.get("preview_cmd")):
        _bad("preview_cmd must be a non-empty string")
    if not _is_nonempty_str(cfg.get("url")):
        _bad("url must be a non-empty string")
    routes = cfg.get("routes")
    if not isinstance(routes, list) or not routes \
            or not all(_is_nonempty_str(item) for item in routes):
        _bad("routes must be a non-empty list of paths")
    for key in ("widths", "themes"):
        values = cfg.get(key)
        if values is None:
            continue
        if not isinstance(values, list) or not values:
            _bad(f"{key} must be a non-empty list")
    widths = cfg.get("widths")
    if isinstance(widths, list) and any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in widths):
        _bad("widths must be positive integers")
    themes = cfg.get("themes")
    if isinstance(themes, list) and any(
            item not in ("light", "dark") for item in themes):
        _bad("themes must be light and/or dark")
    selectors = cfg.get("selectors")
    if selectors is not None:
        if not isinstance(selectors, Mapping):
            _bad("selectors must be a mapping")
        else:
            for key in ("content", "nav"):
                if key in selectors and not _is_nonempty_str(
                        selectors.get(key)):
                    _bad(f"selectors.{key} must be a non-empty string")
    for key in ("min_margin", "min_margin_mobile", "max_width",
                "viewport_height", "settle_ms"):
        value = cfg.get(key)
        if value is None or isinstance(value, bool):
            if value is not None:
                _bad(f"{key} must be a positive number")
            continue
        if not isinstance(value, (int, float)) or value <= 0:
            _bad(f"{key} must be a positive number")
    timeout = cfg.get("timeout_minutes")
    if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int)
            or timeout <= 0):
        _bad("timeout_minutes must be a positive integer")
    fonts = cfg.get("fonts")
    if fonts is not None and (
            not isinstance(fonts, list)
            or not all(_is_nonempty_str(item) for item in fonts)):
        _bad("fonts must be a list of non-empty strings")
    icons = cfg.get("icons")
    if icons is not None:
        if not isinstance(icons, list):
            _bad("icons must be a list")
        else:
            for index, entry in enumerate(icons):
                if not isinstance(entry, Mapping) or not _is_nonempty_str(
                        entry.get("selector")):
                    _bad(f"icons[{index}].selector must be non-empty")
    net_routes = cfg.get("network_routes")
    if net_routes is not None:
        if not isinstance(net_routes, list):
            _bad("network_routes must be a list")
        else:
            for index, entry in enumerate(net_routes):
                if not isinstance(entry, Mapping) or not _is_nonempty_str(
                        entry.get("pattern")):
                    _bad(f"network_routes[{index}].pattern must be non-empty")
                    continue
                if "body" not in entry and "body_file" not in entry:
                    _bad(f"network_routes[{index}] needs body or body_file")
    if cfg.get("setup_eval") is not None \
            and cfg.get("setup_eval_file") is not None:
        _bad("setup_eval and setup_eval_file are exclusive")
    if cfg.get("setup_eval") is not None and not _is_nonempty_str(
            cfg.get("setup_eval")):
        _bad("setup_eval must be a non-empty string")
    if cfg.get("setup_eval_file") is not None and not _is_nonempty_str(
            cfg.get("setup_eval_file")):
        _bad("setup_eval_file must be a non-empty string")
    if cfg.get("build_cmd") is not None and not _is_nonempty_str(
            cfg.get("build_cmd")):
        _bad("build_cmd must be a non-empty string")
    checks = cfg.get("checks")
    if checks is not None and (
            not isinstance(checks, list) or not checks
            or any(item not in _ALL_CHECKS for item in checks)):
        _bad(f"checks must be a non-empty subset of {list(_ALL_CHECKS)}")
    for key in ("judge_install_cmd", "judge_build_cmd"):
        if cfg.get(key) is not None and not _is_nonempty_str(cfg.get(key)):
            _bad(f"{key} must be a non-empty string")
    return errors


def _projects_root() -> Path:
    return Path(__file__).resolve().parents[2] / "projects"


def _qa_root() -> Path:
    return Path(__file__).resolve().parent


def load_ui_gate_config(project: str,
                        config_path: str | Path | None = None) -> dict[str, Any]:
    """Read and normalize ``dev.qa.ui_gate`` for a project (fail-closed)."""
    path = Path(config_path) if config_path is not None else (
        _projects_root() / project / "project.yaml")
    if not path.is_file():
        raise GateConfigError(f"project config not found: {path}")
    try:
        import yaml
    except ImportError as exc:
        raise GateConfigError(f"pyyaml unavailable: {exc}") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise GateConfigError(f"{path} unreadable: {exc}") from exc
    if not isinstance(data, Mapping):
        raise GateConfigError(f"{path} top level must be a mapping")
    dev = data.get("dev")
    if not isinstance(dev, Mapping):
        raise GateConfigError(f"{path} has no dev block")
    problems = validate_ui_gate_config(dev)
    if problems:
        raise GateConfigError("; ".join(problems))
    qa = dev.get("qa")
    cfg = dict(qa.get("ui_gate")) if isinstance(qa, Mapping) else None
    if not cfg:
        raise GateConfigError(
            f"{project}/project.yaml declares no dev.qa.ui_gate")
    return normalize_ui_gate_config(cfg)


def normalize_ui_gate_config(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Validated mapping → runner config with defaults (pure)."""
    selectors = dict(cfg.get("selectors") or {})
    return {
        "build_cmd": cfg.get("build_cmd"),
        "preview_cmd": str(cfg["preview_cmd"]).strip(),
        "url": str(cfg["url"]).strip().rstrip("/"),
        "routes": [str(item).strip() for item in cfg["routes"]],
        "widths": list(cfg.get("widths") or list(_DEFAULT_WIDTHS)),
        "themes": list(cfg.get("themes") or list(_DEFAULT_THEMES)),
        "viewport_height": cfg.get("viewport_height", 900),
        "settle_ms": cfg.get("settle_ms", 2000),
        "timeout_minutes": cfg.get("timeout_minutes", 10),
        "selectors": {
            "content": selectors.get("content", "main"),
            "nav": selectors.get("nav", "nav, aside"),
        },
        "min_margin": cfg.get("min_margin", 24),
        "min_margin_mobile": cfg.get("min_margin_mobile", 16),
        "max_width": cfg.get("max_width", 1600),
        "fonts": [str(item) for item in (cfg.get("fonts") or [])],
        "icons": [dict(entry) for entry in (cfg.get("icons") or [])],
        "network_routes": [dict(entry)
                           for entry in (cfg.get("network_routes") or [])],
        "setup_eval": cfg.get("setup_eval"),
        "setup_eval_file": cfg.get("setup_eval_file"),
        "checks": list(cfg.get("checks") or list(_ALL_CHECKS)),
        "judge_install_cmd": cfg.get("judge_install_cmd"),
        "judge_build_cmd": cfg.get("judge_build_cmd"),
    }


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def resolve_gate_port(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of ``cfg`` with ``{port}`` expanded to one free loopback port."""
    if "{port}" not in str(cfg.get("preview_cmd") or "") \
            and "{port}" not in str(cfg.get("url") or ""):
        return dict(cfg)
    port = _free_port()
    resolved = dict(cfg)
    resolved["preview_cmd"] = str(cfg.get("preview_cmd") or "").replace(
        "{port}", str(port))
    resolved["url"] = str(cfg.get("url") or "").replace("{port}", str(port))
    return resolved


def _axe_script_path() -> Path:
    return _qa_root() / "vendor" / AXE_FILENAME


def _check_axe_vendor() -> None:
    path = _axe_script_path()
    if not path.is_file():
        raise GateConfigError(f"vendored axe missing: {path}")
    import hashlib

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != AXE_SHA256:
        raise GateConfigError(
            f"vendored axe mismatch: want {AXE_SHA256[:12]}, "
            f"got {digest[:12]}")


@dataclass
class AgentBrowser:
    """One named agent-browser session driven over the CLI (no model)."""

    binary: str
    session: str
    timeout_s: int = 120

    def _run(self, *argv: str, stdin: str | None = None) -> Any:
        cmd = [self.binary, "--session", self.session, *argv]
        try:
            proc = subprocess.run(
                cmd, input=stdin, capture_output=True, text=True,
                timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise GateFailure(
                f"agent-browser {' '.join(argv[:2])} timed out") from exc
        except OSError as exc:
            raise GateFailure(f"agent-browser not runnable: {exc}") from exc
        if proc.returncode != 0:
            raise GateFailure(
                f"agent-browser {' '.join(argv[:2])} failed: "
                f"{(proc.stderr or proc.stdout or '')[:400]}")
        return proc.stdout or ""

    def open(self, url: str | None = None,
             init_script: str | None = None) -> None:
        argv: list[str] = ["open"]
        if init_script is not None:
            argv += ["--init-script", init_script]
        if url is not None:
            argv.append(url)
        self._run(*argv, stdin=None)

    def set_viewport(self, width: int, height: int) -> None:
        self._run("set", "viewport", str(width), str(height))

    def set_media(self, theme: str) -> None:
        self._run("set", "media", theme, "reduced-motion")

    def network_route(self, pattern: str, body: str) -> None:
        self._run("network", "route", pattern, "--body", body)

    def wait_ms(self, delay_ms: int) -> None:
        self._run("wait", str(int(delay_ms)))

    def tab_new(self, url: str) -> None:
        self._run("tab", "new", url)

    def reload(self) -> None:
        self._run("reload")

    def tab_list(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self._run("tab", "list", "--json"))
        except ValueError:
            return []
        tabs = (payload.get("data") or {}).get("tabs", payload.get("tabs", []))
        return [dict(entry) for entry in tabs
                if isinstance(entry, Mapping)]

    def tab_switch(self, ref: str) -> None:
        self._run("tab", ref)

    def eval_json(self, script: str) -> Any:
        """Evaluate JS, parsing the ``--json`` envelope (fail-closed)."""
        try:
            payload = json.loads(self._run("eval", "--json", "--stdin",
                                           stdin=script))
        except ValueError as exc:
            raise GateFailure(f"eval returned no JSON: {exc}") from exc
        if not isinstance(payload, Mapping) or payload.get("error"):
            raise GateFailure(f"eval failed: {str(payload)[:300]}")
        result = (payload.get("data") or {}).get("result")
        if isinstance(result, str):
            try:
                return json.loads(result)
            except ValueError:
                return result
        return result

    def screenshot(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._run("screenshot", str(target))

    def close(self) -> None:
        try:
            self._run("close")
        except GateFailure:
            pass


_PAGE_METRICS_JS = """\
const __cfg = __UI_GATE_CFG__;
const __pick = (sel) => {
  for (const part of String(sel || "").split(",")) {
    const found = document.querySelector(part.trim());
    if (found) return found;
  }
  return null;
};
const __box = (el) => {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return null;
  return {x: r.x, y: r.y, width: r.width, height: r.height};
};
const content = __pick(__cfg.content) || document.body;
const contentBox = __box(content) || {x: 0, y: 0, width: 0, height: 0};
let navRight = null;
const nav = __pick(__cfg.nav);
if (nav) {
  const r = nav.getBoundingClientRect();
  if (r.width > 0 && r.height > 0 && r.right <= contentBox.x + 1) {
    navRight = r.right;
  }
}
let h1 = null;
for (const el of document.querySelectorAll("h1")) {
  const r = el.getBoundingClientRect();
  if (r.width > 0 && r.height > 0) { h1 = {x: r.x, width: r.width}; break; }
}
const loadedFaces = {};
try {
  for (const face of document.fonts) {
    if (face && face.status === "loaded" && face.family) {
      loadedFaces[String(face.family).toLowerCase()] = true;
    }
  }
} catch (e) {}
const fontsLoaded = {};
for (const name of __cfg.fonts) {
  fontsLoaded[name] = !!loadedFaces[String(name).toLowerCase()];
}
const stacks = [];
for (const el of document.querySelectorAll(
    "p,h1,h2,h3,h4,a,button,span,li,td,th,label,div")) {
  if (stacks.length >= 60) break;
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) continue;
  const text = (el.innerText || "").trim();
  if (!text) continue;
  stacks.push(getComputedStyle(el).fontFamily);
}
const iconCounts = {};
for (const entry of __cfg.icons) {
  try {
    iconCounts[entry.selector] =
      document.querySelectorAll(entry.selector).length;
  } catch (e) { iconCounts[entry.selector] = 0; }
}
const visible = document.body ? (document.body.innerText || "") : "";
const __leaks = [];
const __scan = (kind, rx) => {
  const m = rx.exec(visible);
  if (m) {
    __leaks.push({kind: kind,
      sample: visible.slice(Math.max(0, m.index - 40), m.index + 60)});
  }
};
__scan("undefined", /\\bundefined\\b/);
__scan("null", /\\bnull\\b/);
__scan("nan", /\\bNaN\\b/);
__scan("object", /\\[object Object\\]/);
__scan("template", /\\{\\{/);
__scan("i18n", /\\b[a-z][a-zA-Z0-9]*(?:\\.[a-z][a-zA-Z0-9]*){2,}\\b/);
__scan("uuid", /\\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\b/i);
const scroller = document.scrollingElement || document.documentElement;
JSON.stringify({
  viewport: {w: window.innerWidth, h: window.innerHeight},
  content: {x: contentBox.x, y: contentBox.y, width: contentBox.width},
  nav_right: navRight,
  h1: h1,
  scroll_width: scroller ? scroller.scrollWidth : 0,
  fonts_loaded: fontsLoaded,
  font_stacks: stacks,
  icon_counts: iconCounts,
  leaks: __leaks,
  excerpt: visible.slice(0, 500),
});
"""

_AXE_JS = """\
axe.run(document, {resultTypes: ["violations"]}).then((report) =>
  JSON.stringify(report.violations.map((entry) => (
    {id: entry.id, impact: entry.impact, nodes: entry.nodes.length}))));
"""

_FONTS_READY_JS = 'JSON.stringify({ready: document.fonts.status === "loaded"})'


def _session_name(ticket: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "-", str(ticket or "")).strip("-")
    return f"ui-gate-{cleaned or 'ticket'}-{os.getpid()}"


class PreviewServer:
    """Build (optional) + preview lifecycle around one gate run."""

    def __init__(self, repo: Path, cfg: Mapping[str, Any]) -> None:
        self._repo = repo
        self._cfg = cfg
        self._proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> str:
        build_cmd = self._cfg.get("build_cmd")
        if build_cmd:
            self._run_build(str(build_cmd))
        cmd = str(self._cfg["preview_cmd"])
        self._log_path = Path(tempfile.gettempdir()) / (
            f"ui-gate-preview-{os.getpid()}.log")
        try:
            self._log_handle = open(self._log_path, "wb")  # noqa: PTH123
            self._proc = subprocess.Popen(
                cmd, cwd=str(self._repo), shell=True,
                stdout=subprocess.DEVNULL, stderr=self._log_handle,
                text=False, start_new_session=True)
        except OSError as exc:
            raise GateFailure(f"preview not started: {exc}") from exc
        try:
            self._wait_ready(str(self._cfg["url"]))
        except GateFailure:
            raise GateFailure(
                f"preview not serving: {self._log_tail()}") from None
        return str(self._cfg["url"])

    def _log_tail(self, chars: int = 600) -> str:
        try:
            data = self._log_path.read_bytes()[-chars:]
        except OSError:
            return "no preview log"
        return data.decode("utf-8", "replace").strip() or "no preview log"

    def __exit__(self, *exc: Any) -> None:
        proc, self._proc = self._proc, None
        handle = getattr(self, "_log_handle", None)
        try:
            if proc is not None:
                try:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                    proc.wait(timeout=10)
        except OSError:
            pass
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            try:
                Path(getattr(self, "_log_path", "")).unlink(missing_ok=True)
            except OSError:
                pass

    def _run_build(self, cmd: str) -> None:
        try:
            proc = subprocess.run(
                cmd, cwd=str(self._repo), shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=600, text=True, errors="replace")
        except subprocess.TimeoutExpired as exc:
            raise GateFailure("build_cmd timed out") from exc
        except OSError as exc:
            raise GateFailure(f"build_cmd not started: {exc}") from exc
        if proc.returncode != 0:
            raise GateFailure(
                f"build_cmd failed: {(proc.stdout or '')[-800:]}")

    def _wait_ready(self, url: str, timeout_s: int = 120) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise GateFailure("preview exited before serving")
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    if response.status < 500:
                        return
            except Exception:  # noqa: BLE001
                time.sleep(1)
        raise GateFailure(f"preview not ready at {url}")


@dataclass
class _Page:
    route: str
    width: int
    theme: str
    status: str = "pass"
    defects: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    axe: list[dict[str, Any]] = field(default_factory=list)
    screenshot: str = ""
    elapsed_s: float = 0.0

    def fail(self, *items: str) -> None:
        for item in items:
            if item and item not in self.defects:
                self.defects.append(item)
        if self.defects:
            self.status = "fail"


def _route_body(repo: Path, entry: Mapping[str, Any]) -> str:
    if "body" in entry:
        return json.dumps(entry["body"], ensure_ascii=False)
    body_file = repo / str(entry.get("body_file") or "")
    try:
        text = body_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateConfigError(
            f"network_routes body_file unreadable: {body_file}: {exc}"
        ) from exc
    try:
        json.loads(text)
    except ValueError as exc:
        raise GateConfigError(
            f"network_routes body_file is not JSON: {body_file}") from exc
    return text


def _setup_script(repo: Path, cfg: Mapping[str, Any]) -> str | None:
    if cfg.get("setup_eval") is not None:
        return str(cfg["setup_eval"])
    relpath = cfg.get("setup_eval_file")
    if relpath is None:
        return None
    path = repo / str(relpath)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateConfigError(
            f"setup_eval_file unreadable: {path}: {exc}") from exc


def run_ui_gate(cfg: Mapping[str, Any], repo: str | Path,
                out_dir: str | Path, ticket: str,
                binary: str | None = None) -> dict[str, Any]:
    """Serve, visit and judge every route/width/theme. Returns the report."""
    cfg = resolve_gate_port(cfg)
    repo_path = Path(repo).resolve()
    out_path = Path(out_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)
    agent_bin = binary or shutil.which("agent-browser") or "agent-browser"
    if binary is None and shutil.which("agent-browser") is None:
        raise GateConfigError("agent-browser not found on PATH")
    _check_axe_vendor()
    started = time.monotonic()
    deadline = started + int(cfg.get("timeout_minutes", 10)) * 60
    base_url = str(cfg["url"])
    height = int(cfg.get("viewport_height", 900))
    settle_ms = int(cfg.get("settle_ms", 2000))
    selectors = cfg.get("selectors") or {}
    fonts = [str(item) for item in (cfg.get("fonts") or [])]
    icons = [dict(entry) for entry in (cfg.get("icons") or [])]
    setup_js = _setup_script(repo_path, cfg)
    metrics_cfg = json.dumps({
        "content": selectors.get("content", "main"),
        "nav": selectors.get("nav", "nav, aside"),
        "fonts": fonts,
        "icons": [{"selector": str(entry.get("selector") or "")}
                  for entry in icons if isinstance(entry, Mapping)],
    }, ensure_ascii=False)
    page_js = _PAGE_METRICS_JS.replace("__UI_GATE_CFG__", metrics_cfg)
    results: list[_Page] = []
    references: dict[int, dict[str, Any]] = {}
    with PreviewServer(repo_path, cfg):
        for width in cfg["widths"]:
            for theme in cfg["themes"]:
                if time.monotonic() > deadline:
                    raise GateFailure("ui_gate exceeded timeout_minutes")
                browser = AgentBrowser(agent_bin, _session_name(ticket))
                try:
                    # No blank open: it launches without the host browser
                    # flags. Routes register after the launch navigation:
                    # routes plus an init script on one launch leave the
                    # tab blank, so the first page reloads under the mocks.
                    first_route = True
                    for route in cfg["routes"]:
                        page = _Page(route=str(route), width=int(width),
                                     theme=str(theme))
                        page_started = time.monotonic()
                        if first_route:
                            browser.open(
                                f"{base_url}{route}",
                                init_script=str(_axe_script_path()))
                            _await_committed(browser)
                            browser.set_viewport(int(width), height)
                            browser.set_media(str(theme))
                            for entry in cfg.get("network_routes") or []:
                                browser.network_route(
                                    str(entry.get("pattern")),
                                    _route_body(repo_path, entry))
                            if cfg.get("network_routes"):
                                browser.reload()
                        else:
                            # Fresh tabs inherit routes, init scripts and
                            # emulation; reusing one tab races blank loads.
                            browser.tab_new(f"{base_url}{route}")
                        _await_committed(browser)
                        if first_route:
                            if setup_js is not None:
                                browser.eval_json(setup_js)
                            first_route = False
                        browser.wait_ms(settle_ms)
                        _await_fonts(browser)
                        raw = browser.eval_json(page_js)
                        page.metrics = raw if isinstance(raw, Mapping) \
                            else {"raw": raw}
                        violations = browser.eval_json(_AXE_JS)
                        page.axe = violations if isinstance(
                            violations, list) else []
                        shot = (out_path / f"{slug_for_route(route)}-"
                                f"{width}-{theme}.png")
                        browser.screenshot(shot)
                        page.screenshot = shot.name
                        _judge_page(cfg, page, references)
                        page.elapsed_s = round(
                            time.monotonic() - page_started, 3)
                        results.append(page)
                finally:
                    browser.close()
    report = {
        "project": cfg.get("project", ""),
        "ticket": ticket,
        "base_url": base_url,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": round(time.monotonic() - started, 3),
        "verdict": "fail" if any(
            item.status != "pass" for item in results) else "pass",
        "results": [
            {"route": item.route, "width": item.width, "theme": item.theme,
             "status": item.status, "defects": item.defects,
             "metrics": item.metrics, "axe": item.axe,
             "screenshot": item.screenshot, "elapsed_s": item.elapsed_s}
            for item in results
        ],
    }
    (out_path / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return report


def _await_fonts(browser: AgentBrowser, timeout_s: int = 5) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            state = browser.eval_json(_FONTS_READY_JS)
        except GateFailure:
            return
        if isinstance(state, Mapping) and state.get("ready"):
            return
        time.sleep(0.5)


_COMMITTED_JS = ('JSON.stringify({ready: document.readyState, '
                 'href: window.location.href})')


def _await_committed(browser: AgentBrowser, timeout_s: int = 30) -> str:
    """Wait until the navigation commits (guards the blank-tab race)."""
    started = time.monotonic()
    deadline = started + timeout_s
    last_href = ""
    switched = False
    while time.monotonic() < deadline:
        try:
            state = browser.eval_json(_COMMITTED_JS)
        except GateFailure:
            time.sleep(0.5)
            continue
        if not isinstance(state, Mapping):
            time.sleep(0.5)
            continue
        last_href = str(state.get("href") or "")
        if state.get("ready") == "complete" and last_href not in (
                "", "about:blank"):
            return last_href
        if not switched and time.monotonic() - started > 5:
            switched = True
            _switch_to_loaded_tab(browser)
        time.sleep(0.5)
    raise GateFailure(
        f"navigation never committed (last href {last_href!r})")


def _switch_to_loaded_tab(browser: AgentBrowser) -> None:
    """Move to the newest non-blank tab when the active one stays blank."""
    try:
        tabs = browser.tab_list()
    except GateFailure:
        return
    for entry in reversed(tabs):
        url = str(entry.get("url") or "")
        ref = str(entry.get("tabId") or entry.get("targetId") or "")
        if url and not url.startswith("about:") and ref:
            try:
                browser.tab_switch(ref)
            except GateFailure:
                pass
            return


def _judge_page(cfg: Mapping[str, Any], page: _Page,
                references: dict[int, dict[str, Any]]) -> None:
    metrics = page.metrics
    if not isinstance(metrics, Mapping) or not metrics.get("viewport"):
        page.fail("page metrics unreadable")
        return
    enabled = set(cfg.get("checks") or _ALL_CHECKS)
    min_margin = float(cfg["min_margin_mobile"]
                       if page.width < _MOBILE_UNDER
                       else cfg["min_margin"])
    if "margin" in enabled:
        page.fail(*[f"margin: {item}" for item in
                    check_margins(metrics, min_margin)])
    if "width" in enabled:
        page.fail(*[f"width: {item}" for item in
                    check_max_width(metrics, float(cfg["max_width"]))])
    if "scroll" in enabled:
        page.fail(*[f"scroll: {item}" for item in check_scroll(metrics)])
    if "fonts" in enabled:
        page.fail(*[f"font: {item}" for item in
                    check_fonts(metrics, cfg.get("fonts") or [])])
    if "icons" in enabled:
        page.fail(*[f"icon: {item}" for item in
                    check_icons(metrics, cfg.get("icons") or [])])
    if "leaks" in enabled:
        for leak in metrics.get("leaks") or []:
            if isinstance(leak, Mapping):
                page.fail(f"leak:{leak.get('kind')}: "
                          f"{str(leak.get('sample') or '')[:80]}")
    if "axe" in enabled:
        page.fail(*[f"{item}" for item in check_axe(page.axe)])
    if "layout" in enabled:
        if page.width in references:
            page.fail(*[f"layout: {item}" for item in
                        check_consistency(references[page.width], metrics)])
        else:
            references[page.width] = {
                "h1": metrics.get("h1"), "content": metrics.get("content")}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: exit 0 passes, 1 reports defects, 2 misconfigures."""
    parser = argparse.ArgumentParser(
        description="Deterministic UI gate over agent-browser (no model).")
    parser.add_argument("--project", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--repo", default=None,
                        help="target checkout (default: cwd)")
    parser.add_argument("--config", default=None,
                        help="project.yaml path (default: projects/<key>)")
    parser.add_argument("--out", default=None,
                        help="output dir (default: <repo>/.openceo/ui-gate/<ticket>)")
    parser.add_argument("--agent-browser", default=None,
                        help="agent-browser binary (default: PATH)")
    args = parser.parse_args(argv)
    try:
        cfg = load_ui_gate_config(args.project, args.config)
    except GateConfigError as exc:
        print(f"ui_gate config error: {exc}")
        return 2
    cfg["project"] = args.project
    repo = Path(args.repo) if args.repo else Path.cwd()
    safe_ticket = re.sub(r"[^A-Za-z0-9._-]+", "-", args.ticket).strip(".-")
    out = Path(args.out) if args.out else (
        repo / ".openceo" / "ui-gate" / (safe_ticket or "ticket"))
    try:
        report = run_ui_gate(cfg, repo, out, safe_ticket or "ticket",
                             binary=args.agent_browser)
    except GateConfigError as exc:
        print(f"ui_gate config error: {exc}")
        return 2
    except GateFailure as exc:
        print(f"ui_gate infrastructure failure: {exc}")
        return 2
    failed = [item for item in report["results"]
              if item["status"] != "pass"]
    for item in report["results"]:
        mark = "ok" if item["status"] == "pass" else "FAIL"
        print(f"[{mark}] {item['route']} {item['width']}px "
              f"{item['theme']} ({item['elapsed_s']}s)")
        for defect in item["defects"]:
            print(f"      - {defect}")
    print(f"ui_gate {report['verdict']}: "
          f"{len(report['results']) - len(failed)}/"
          f"{len(report['results'])} pages pass "
          f"({report['duration_s']}s)")
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
