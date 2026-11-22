"""Project UI gate over agent-browser (TASK-2026-00896, B).

Deterministic render checks with no model behind them: margins, width,
layout consistency, scroll, fonts, icons, universal render leaks and
axe serious/critical. No Playwright anywhere (Owner order 2026-11-22).

* pure checks run without a browser;
* one browser test serves tests/fixtures/ui-gate/ over http.server and
  runs the gate end to end (needs agent-browser + its Chrome; skipped
  when absent).
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _path in (REPO / "openceo-core", REPO / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from qa.ui_gate import (  # noqa: E402
    GateConfigError,
    check_axe,
    check_consistency,
    check_fonts,
    check_icons,
    check_margins,
    check_max_width,
    check_scroll,
    load_ui_gate_config,
    run_ui_gate,
    scan_leaks,
    slug_for_route,
    validate_ui_gate_config,
)

FIXTURES = REPO / "tests" / "fixtures" / "ui-gate"


def _metrics(**overrides):
    base = {
        "viewport": {"w": 1440, "h": 900},
        "content": {"x": 264, "width": 1152},
        "nav_right": 240,
        "h1": {"x": 264, "width": 400},
        "scroll_width": 1440,
        "fonts_loaded": {"Fixture Sans": True},
        "font_stacks": ['"Fixture Sans", sans-serif'],
        "icon_counts": {"nav svg": 2},
        "leaks": [],
    }
    base.update(overrides)
    return base


class SlugTests(unittest.TestCase):
    def test_root_is_index(self) -> None:
        self.assertEqual("index", slug_for_route("/"))

    def test_nested_path(self) -> None:
        self.assertEqual("dashboard-tickets", slug_for_route(
            "/dashboard/tickets"))


class LeakScanTests(unittest.TestCase):
    def test_all_seven_kinds(self) -> None:
        text = ("a undefined b null c NaN d [object Object] e {{x}} "
                "f nav.dashboard.title g 01234567-89ab-cdef-0123-"
                "456789abcdef")
        kinds = {item["kind"] for item in scan_leaks(text)}
        self.assertEqual({"undefined", "null", "nan", "object", "template",
                          "i18n", "uuid"}, kinds)

    def test_clean_copy_passes(self) -> None:
        self.assertEqual([], scan_leaks("Panel inicial. Guarda los cambios."))

    def test_nothing_fuente_specific(self) -> None:
        self.assertEqual(
            [], [item for item in scan_leaks("Fuente: panel manual")
                 if "fuente" in item["kind"]])


class MarginTests(unittest.TestCase):
    def test_24px_passes(self) -> None:
        self.assertEqual([], check_margins(_metrics(), 24))

    def test_zero_margin_fails(self) -> None:
        defects = check_margins(_metrics(content={"x": 240, "width": 1200}),
                                24)
        self.assertTrue(any("margin" in item for item in defects),
                        defects)

    def test_right_margin_fails(self) -> None:
        defects = check_margins(_metrics(content={"x": 264, "width": 1176}),
                                24)
        self.assertTrue(any("right margin" in item for item in defects),
                        defects)


class WidthTests(unittest.TestCase):
    def test_within_max_passes(self) -> None:
        self.assertEqual([], check_max_width(_metrics(), 1600))

    def test_wider_than_max_fails(self) -> None:
        defects = check_max_width(
            _metrics(content={"x": 24, "width": 1700}), 1600)
        self.assertTrue(any("wider" in item for item in defects), defects)


class ConsistencyTests(unittest.TestCase):
    def test_same_box_passes(self) -> None:
        self.assertEqual([], check_consistency(_metrics(), _metrics()))

    def test_shifted_h1_fails(self) -> None:
        defects = check_consistency(
            _metrics(), _metrics(h1={"x": 304, "width": 400}))
        self.assertTrue(any("h1 x" in item for item in defects), defects)

    def test_two_px_tolerance(self) -> None:
        self.assertEqual(
            [], check_consistency(
                _metrics(), _metrics(h1={"x": 266, "width": 400})))


class ScrollTests(unittest.TestCase):
    def test_no_scroll_passes(self) -> None:
        self.assertEqual([], check_scroll(_metrics()))

    def test_overflow_fails(self) -> None:
        defects = check_scroll(_metrics(scroll_width=1700))
        self.assertTrue(any("scroll" in item for item in defects), defects)


class FontTests(unittest.TestCase):
    def test_loaded_passes(self) -> None:
        self.assertEqual([], check_fonts(_metrics(), ["Fixture Sans"]))

    def test_unloaded_declared_fails(self) -> None:
        defects = check_fonts(_metrics(), ["Fixture Sans", "No Existe"])
        self.assertTrue(any("No Existe" in item for item in defects),
                        defects)

    def test_fallback_stack_fails(self) -> None:
        defects = check_fonts(
            _metrics(font_stacks=["system-ui, sans-serif"]),
            ["Fixture Sans"])
        self.assertTrue(any("fallback" in item for item in defects),
                        defects)

    def test_no_declared_skips(self) -> None:
        self.assertEqual(
            [], check_fonts(_metrics(font_stacks=["system-ui"]), []))


class IconTests(unittest.TestCase):
    def test_present_passes(self) -> None:
        self.assertEqual(
            [], check_icons(_metrics(), [{"selector": "nav svg", "min": 1}]))

    def test_absent_fails(self) -> None:
        defects = check_icons(
            _metrics(icon_counts={"nav svg": 0}),
            [{"selector": "nav svg", "min": 1}])
        self.assertTrue(any("nav svg" in item for item in defects), defects)


class AxeTests(unittest.TestCase):
    def test_moderate_passes(self) -> None:
        self.assertEqual([], check_axe([{"id": "region", "impact": "moderate",
                                         "nodes": 1}]))

    def test_critical_fails(self) -> None:
        defects = check_axe([{"id": "button-name", "impact": "critical",
                              "nodes": 1}])
        self.assertTrue(any("button-name" in item for item in defects),
                        defects)


class ConfigValidationTests(unittest.TestCase):
    def _dev(self, **ui_gate) -> dict:
        return {"qa": {"ui_gate": {
            "preview_cmd": "x", "url": "http://127.0.0.1:1",
            "routes": ["/"], **ui_gate}}}

    def test_minimal_valid(self) -> None:
        self.assertEqual([], validate_ui_gate_config(self._dev()))

    def test_absent_valid(self) -> None:
        self.assertEqual([], validate_ui_gate_config({}))

    def test_missing_preview_cmd(self) -> None:
        dev = self._dev()
        del dev["qa"]["ui_gate"]["preview_cmd"]
        self.assertTrue(any("preview_cmd" in item
                            for item in validate_ui_gate_config(dev)))

    def test_bad_theme(self) -> None:
        errors = validate_ui_gate_config(self._dev(themes=["sepia"]))
        self.assertTrue(any("themes" in item for item in errors), errors)

    def test_bad_checks(self) -> None:
        errors = validate_ui_gate_config(self._dev(checks=["nope"]))
        self.assertTrue(any("checks" in item for item in errors), errors)

    def test_checks_subset_judged_only(self) -> None:
        from qa import ui_gate as gate_mod
        cfg = {"min_margin": 24, "min_margin_mobile": 16, "max_width": 1600,
               "fonts": ["Fixture Sans"], "icons": [],
               "checks": ["fonts", "leaks"]}
        page = gate_mod._Page(route="/x", width=1440, theme="light")
        page.metrics = _metrics(content={"x": 0, "width": 1700},
                                scroll_width=1700)
        gate_mod._judge_page(cfg, page, {})
        self.assertEqual("pass", page.status, page.defects)

    def test_load_norms_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.yaml"
            path.write_text("dev:\n  qa:\n    ui_gate:\n"
                            "      preview_cmd: x\n"
                            "      url: http://127.0.0.1:1/\n"
                            "      routes: [/a]\n", encoding="utf-8")
            cfg = load_ui_gate_config("demo", path)
        self.assertEqual([1440, 390], cfg["widths"])
        self.assertEqual("http://127.0.0.1:1", cfg["url"])
        self.assertEqual("main", cfg["selectors"]["content"])

    def test_load_missing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.yaml"
            path.write_text("dev: {}\n", encoding="utf-8")
            with self.assertRaises(GateConfigError):
                load_ui_gate_config("demo", path)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class BrowserGateTests(unittest.TestCase):
    """End to end over agent-browser (skipped when it is absent)."""

    def test_fixtures_pass_and_fail_as_designed(self) -> None:
        if shutil.which("agent-browser") is None:
            self.skipTest("no agent-browser binary")
        port = _free_port()
        cfg = {
            "project": "fixture",
            "preview_cmd": f"{sys.executable} -m http.server {port} "
                           "--bind 127.0.0.1",
            "url": f"http://127.0.0.1:{port}",
            "routes": ["/ok.html", "/margin-zero.html", "/fallback-font.html",
                       "/leak.html", "/wide.html", "/shifted.html",
                       "/button-no-name.html", "/mock.html"],
            "widths": [1440],
            "themes": ["light"],
            "viewport_height": 900,
            "settle_ms": 500,
            "timeout_minutes": 10,
            "selectors": {"content": "main.app-content",
                          "nav": "nav.app-nav"},
            "min_margin": 24,
            "min_margin_mobile": 16,
            "max_width": 1600,
            "fonts": ["Fixture Sans"],
            "icons": [{"selector": "nav svg", "min": 1}],
            "network_routes": [{"pattern": "**/api/v1/ping",
                                "body": {"status": "mocked"}}],
        }
        with tempfile.TemporaryDirectory(prefix="ui-gate-") as tmp:
            report = run_ui_gate(cfg, FIXTURES, tmp, "fixture-896")
            by_route = {item["route"]: item
                        for item in report["results"]}
            self.assertEqual("fail", report["verdict"])
            self.assertEqual(
                [], by_route["/ok.html"]["defects"],
                by_route["/ok.html"]["defects"])
            self.assertTrue(any(
                item.startswith("margin:") for item in
                by_route["/margin-zero.html"]["defects"]),
                by_route["/margin-zero.html"]["defects"])
            fallback_defects = by_route["/fallback-font.html"]["defects"]
            self.assertTrue(any(
                item.startswith("font:") for item in fallback_defects),
                fallback_defects)
            self.assertTrue(any(
                "declared but not loaded" in item
                for item in fallback_defects),
                fallback_defects)
            self.assertTrue(any(
                item.startswith("leak:undefined") for item in
                by_route["/leak.html"]["defects"]),
                by_route["/leak.html"]["defects"])
            self.assertTrue(any(
                item.startswith("width:") for item in
                by_route["/wide.html"]["defects"]),
                by_route["/wide.html"]["defects"])
            self.assertTrue(any(
                item.startswith("layout:") for item in
                by_route["/shifted.html"]["defects"]),
                by_route["/shifted.html"]["defects"])
            self.assertTrue(any(
                "axe button-name" in item for item in
                by_route["/button-no-name.html"]["defects"]),
                by_route["/button-no-name.html"]["defects"])
            mock = by_route["/mock.html"]
            self.assertEqual([], mock["defects"], mock["defects"])
            self.assertIn("mocked",
                          (mock["metrics"].get("excerpt") or ""))
            for item in report["results"]:
                shot = Path(tmp) / item["screenshot"]
                self.assertTrue(shot.is_file(), item["screenshot"])
                self.assertGreater(shot.stat().st_size, 0)
            stored = json.loads(
                (Path(tmp) / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(8, len(stored["results"]))

    def test_ok_page_passes_both_widths_and_themes(self) -> None:
        if shutil.which("agent-browser") is None:
            self.skipTest("no agent-browser binary")
        port = _free_port()
        cfg = {
            "project": "fixture",
            "preview_cmd": f"{sys.executable} -m http.server {port} "
                           "--bind 127.0.0.1",
            "url": f"http://127.0.0.1:{port}",
            "routes": ["/ok.html"],
            "widths": [1440, 390],
            "themes": ["light", "dark"],
            "viewport_height": 900,
            "settle_ms": 500,
            "timeout_minutes": 10,
            "selectors": {"content": "main.app-content",
                          "nav": "nav.app-nav"},
            "min_margin": 24,
            "min_margin_mobile": 16,
            "max_width": 1600,
            "fonts": ["Fixture Sans"],
            "icons": [{"selector": "nav svg", "min": 1}],
        }
        with tempfile.TemporaryDirectory(prefix="ui-gate-") as tmp:
            report = run_ui_gate(cfg, FIXTURES, tmp, "fixture-896b")
            self.assertEqual("pass", report["verdict"],
                             json.dumps(report["results"], indent=1)[:2000])
            self.assertEqual(4, len(report["results"]))


if __name__ == "__main__":
    unittest.main()
