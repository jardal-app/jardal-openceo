"""El prompt worker debe distinguir READY de CONTINUE (bucle TASK-2026-00349).

16 sesiones worker emitieron TICKET_CONTINUE con "falta revision" en vez de
TICKET_READY, asi el orquestador nunca avanzo a revision/cierre.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import automatico


def _child() -> dict:
    return {"step": "F4", "ticket": "TASK-2026-00349",
            "branch": "auto/00349-reanudar-mision", "deps": [],
            "tests": [], "knowledge": False, "workspace": _REPO}


def _ticket() -> dict:
    return {"phase": "work", "parked": "", "note": "", "failure": "",
            "sessions": 16, "base": {}, "reviews": [], "ready": ""}


def test_work_prompt_orders_ready_when_only_review_or_close_remain():
    prompt = automatico.work_prompt(_child(), _ticket())
    assert "tras tu TICKET_READY" in prompt
    assert "reserva TICKET_CONTINUE para implementación sin terminar" in prompt


def test_ready_marker_advances_work_phase():
    out = "todo en verde\nTICKET_READY helper listo para revisión\n"
    marker, _detail = automatico.last_marker(out)
    assert marker == automatico.WORK_READY


ENFORCE = {"20.3": True}
FP_A = {"fp": "sha256:" + "a" * 64, "complete": True, "paths": 1,
        "bytes": 10, "head": "h", "error": None}


def _brake_child(branch="auto/00374-freno-runner") -> dict:
    child = _child()
    child.update(ticket="TASK-2026-00374", branch=branch)
    return child


def _brake_ticket() -> dict:
    ticket = _ticket()
    ticket.update(sessions=0, base={"openceo": "abc"})
    return ticket


def _run_feed(monkeypatch, child, ticket, marker="TICKET_CONTINUE",
              sessions=1, milestone="", decisions=None):
    ticket["sessions"] = sessions
    monkeypatch.setattr(automatico, "runner_work_fp",
                        lambda *a, **k: dict(FP_A))
    return automatico.guard_after_session(
        child, ticket, "work", marker=marker, milestone=milestone,
        decisions=dict(ENFORCE) if decisions is None else decisions)


def test_mode_defaults_to_shadow_without_decision():
    assert automatico.runner_guard_mode({}) == "shadow"
    assert automatico.runner_guard_mode({"20.3": False}) == "shadow"
    assert automatico.runner_guard_mode(dict(ENFORCE)) == "enforce"


def test_mode_defaults_to_shadow_without_file(tmp_path, monkeypatch):
    assert automatico.load_owner_decisions(tmp_path) == {}
    assert automatico.load_owner_decisions(tmp_path / "no-existe") == {}
    monkeypatch.setattr(automatico, "load_owner_decisions", lambda *_a, **_k: {})
    assert automatico.runner_guard_mode(None) == "shadow"
    monkeypatch.setattr(automatico, "load_owner_decisions",
                        lambda *_a, **_k: dict(ENFORCE))
    assert automatico.runner_guard_mode(None) == "enforce"


def test_identical_continue_stops_third_attempt_in_enforce(monkeypatch):
    child, ticket = _brake_child(), _brake_ticket()
    assert _run_feed(monkeypatch, child, ticket, sessions=1) == ""
    assert _run_feed(monkeypatch, child, ticket, sessions=2) == ""
    assert _run_feed(monkeypatch, child, ticket, sessions=3) == "blocked"
    assert ticket["parked"] == "blocked"
    assert "stalled_no_progress" in ticket["note"]
    assert "reanudación" in ticket["note"]
    assert ticket["brake"]["rule"] == "G1"
    assert ticket["brake"]["mode"] == "enforce"
    window = ticket["progress_guard"]["window"]
    assert len(window) == 3
    assert ticket["progress_guard"]["stopped"]["rule"] == "G1"


def test_shadow_records_would_stop_without_parking(monkeypatch):
    child, ticket = _brake_child(), _brake_ticket()
    for session in (1, 2, 3, 4):
        out = _run_feed(monkeypatch, child, ticket, sessions=session,
                        decisions={})
        assert out == ""
    assert ticket["parked"] == ""
    assert ticket["brake"]["shadow"] is True
    assert ticket["brake"]["rule"] == "G1"


def test_new_diff_breaks_streak(monkeypatch):
    child, ticket = _brake_child(), _brake_ticket()
    fps = [dict(FP_A), dict(FP_A),
           {"fp": "sha256:" + "b" * 64, "complete": True, "paths": 1,
            "bytes": 10, "head": "h2", "error": None},
           dict(FP_A), dict(FP_A)]

    def fake_fp(*_args, **_kwargs):
        return dict(fps.pop(0))

    monkeypatch.setattr(automatico, "runner_work_fp", fake_fp)
    for session in (1, 2, 3, 4, 5):
        ticket["sessions"] = session
        out = automatico.guard_after_session(
            child, ticket, "work", marker="TICKET_CONTINUE",
            decisions=dict(ENFORCE))
        assert out == "", f"sesión {session} no debería parar"
    assert ticket["parked"] == ""
    assert len(ticket["progress_guard"]["window"]) == 5


def test_milestones_break_streak(monkeypatch):
    for milestone in ("review-accepted", "gate-passed", "ready-accepted",
                      "done-verified"):
        child, ticket = _brake_child(), _brake_ticket()
        monkeypatch.setattr(automatico, "runner_work_fp",
                            lambda *a, **k: dict(FP_A))
        ticket["sessions"] = 1
        automatico.guard_after_session(child, ticket, "work",
                                       marker="TICKET_CONTINUE",
                                       decisions=dict(ENFORCE))
        ticket["sessions"] = 2
        automatico.guard_after_session(child, ticket, "work",
                                       marker="TICKET_CONTINUE",
                                       decisions=dict(ENFORCE))
        ticket["sessions"] = 3
        automatico.guard_after_session(child, ticket, "work",
                                       marker="TICKET_CONTINUE",
                                       milestone=milestone,
                                       decisions=dict(ENFORCE))
        for session in (4, 5):
            ticket["sessions"] = session
            out = automatico.guard_after_session(
                child, ticket, "work", marker="TICKET_CONTINUE",
                decisions=dict(ENFORCE))
            assert out == "", f"hito {milestone}: sesión {session} no para"
        assert len(ticket["progress_guard"]["window"]) == 2


def test_new_attempt_id_does_not_break_streak(monkeypatch):
    child, ticket = _brake_child(), _brake_ticket()
    monkeypatch.setattr(automatico, "runner_work_fp",
                        lambda *a, **k: dict(FP_A))
    assert automatico.guard_after_session(
        child, ticket, "work", marker="TICKET_CONTINUE", decisions=dict(ENFORCE)) == ""
    ticket["sessions"] = 99
    assert automatico.guard_after_session(
        child, ticket, "work", marker="TICKET_CONTINUE", decisions=dict(ENFORCE)) == ""
    ticket["sessions"] = 100
    assert automatico.guard_after_session(
        child, ticket, "work", marker="TICKET_CONTINUE",
        decisions=dict(ENFORCE)) == "blocked"


def test_invalid_marker_counts_while_skipped_does_not(monkeypatch):
    child, ticket = _brake_child(), _brake_ticket()
    monkeypatch.setattr(automatico, "runner_work_fp",
                        lambda *a, **k: dict(FP_A))
    for session in (1, 2):
        ticket["sessions"] = session
        assert automatico.guard_after_session(
            child, ticket, "work", marker="",
            decisions=dict(ENFORCE)) == ""
    ticket["sessions"] = 3
    assert automatico.guard_after_session(
        child, ticket, "work", marker="",
        decisions=dict(ENFORCE)) == "blocked"

    other = _brake_ticket()
    for session in (1, 2, 3):
        other["sessions"] = session
        assert automatico.guard_after_session(
            child, other, "work", marker="TICKET_CONTINUE", executed=False,
            decisions=dict(ENFORCE)) == ""
    assert "progress_guard" not in other


def test_owner_and_blocked_markers_do_not_feed(monkeypatch):
    seen = []
    monkeypatch.setattr(automatico, "runner_work_fp",
                        lambda *a, **k: seen.append(1) or dict(FP_A))
    monkeypatch.setattr(automatico, "ticket_info",
                        lambda _t: {"status": "Open", "subject": "",
                                    "description": "", "depends_on": [],
                                    "owner_replied": False})
    monkeypatch.setattr(automatico, "repo_problem", lambda: "")
    monkeypatch.setattr(automatico, "git_head", lambda *a: "abc")
    monkeypatch.setattr(automatico, "save_state", lambda _s: None)
    child = _brake_child()
    settings = automatico.Settings(max_sessions=1)
    for marker, parked in ((automatico.WORK_OWNER, "owner"),
                           (automatico.WORK_BLOCKED, "blocked")):
        monkeypatch.setattr(
            automatico, "spawn",
            lambda *a, **k: (0, f"sin avance\n{marker} motivo externo\n"))
        state = {"epic": "TASK-2026-00365", "tickets": {}}
        assert automatico.advance(child, settings, state) == parked
        assert seen == []
        assert "progress_guard" not in state["tickets"][child["ticket"]]


def test_dedup_same_attempt_feeds_once(monkeypatch):
    child, ticket = _brake_child(), _brake_ticket()
    monkeypatch.setattr(automatico, "runner_work_fp",
                        lambda *a, **k: dict(FP_A))
    ticket["sessions"] = 1
    assert automatico.guard_after_session(
        child, ticket, "work", marker="TICKET_CONTINUE",
        decisions=dict(ENFORCE)) == ""
    assert automatico.guard_after_session(
        child, ticket, "work", marker="TICKET_CONTINUE",
        decisions=dict(ENFORCE)) == ""
    assert len(ticket["progress_guard"]["window"]) == 1


def test_guard_failure_fails_open(monkeypatch):
    child, ticket = _brake_child(), _brake_ticket()

    def boom(*_args, **_kwargs):
        raise RuntimeError("git caído")

    monkeypatch.setattr(automatico, "runner_work_fp", boom)
    for session in (1, 2, 3):
        ticket["sessions"] = session
        assert automatico.guard_after_session(
            child, ticket, "work", marker="TICKET_CONTINUE",
            decisions=dict(ENFORCE)) == ""
    assert "huella" in ticket["guard_error"]
    window = ticket["progress_guard"]["window"]
    assert len(window) == 3
    assert all(not entry["fp_complete"] for entry in window)


def test_guard_unavailable_fails_open(monkeypatch):
    child, ticket = _brake_child(), _brake_ticket()
    monkeypatch.setitem(automatico._GUARD, "failed", True)
    assert automatico.guard_after_session(
        child, ticket, "work", marker="TICKET_CONTINUE",
        decisions=dict(ENFORCE)) == ""
    assert "fail-open" in ticket["guard_error"]


def test_reset_guard_on_owner_response():
    ticket = _brake_ticket()
    guard = automatico._guard_module()
    state, _ = guard.evaluate(
        {"generation": 0, "phase": "work",
         "window": [{"attempt_id": "a", "phase": "work",
                      "result_sig": "work:x", "work_fp": "sha256:1",
                      "fp_complete": True}],
         "stopped": None, "reset": None, "milestones": []},
        {"phase": "work", "executed": True, "attempt_id": "b",
         "result_sig": "work:x", "work_fp": "sha256:1",
         "fp_complete": True},
        {"mode": "shadow"})
    ticket["progress_guard"] = state
    automatico._reset_runner_guard(ticket, actor="owner",
                                   reason="owner-responded")
    assert ticket["progress_guard"]["window"] == []
    assert ticket["progress_guard"]["reset"]["actor"] == "owner"


def test_advance_stops_third_identical_continue(monkeypatch):
    calls = []

    def fake_spawn(*_args, **_kwargs):
        calls.append(1)
        return 0, "trabajo sin cambios\nTICKET_CONTINUE sigo en ello\n"

    monkeypatch.setattr(automatico, "spawn", fake_spawn)
    monkeypatch.setattr(automatico, "ticket_info",
                        lambda _t: {"status": "Open", "subject": "",
                                    "description": "", "depends_on": [],
                                    "owner_replied": False})
    monkeypatch.setattr(automatico, "repo_problem", lambda: "")
    monkeypatch.setattr(automatico, "git_head", lambda *a: "abc")
    monkeypatch.setattr(automatico, "save_state", lambda _s: None)
    monkeypatch.setattr(automatico, "runner_work_fp",
                        lambda *a, **k: dict(FP_A))
    monkeypatch.setattr(automatico, "load_owner_decisions",
                        lambda *_a, **_k: dict(ENFORCE))
    child = _brake_child()
    settings = automatico.Settings(max_sessions=16)
    state = {"epic": "TASK-2026-00365", "tickets": {}}
    assert automatico.advance(child, settings, state) == "blocked"
    assert len(calls) == 3
    ticket = state["tickets"][child["ticket"]]
    assert ticket["parked"] == "blocked"
    assert "stalled_no_progress" in ticket["note"]


def test_advance_shadow_never_parks(monkeypatch):
    calls = []

    def fake_spawn(*_args, **_kwargs):
        calls.append(1)
        return 0, "trabajo sin cambios\nTICKET_CONTINUE sigo en ello\n"

    monkeypatch.setattr(automatico, "spawn", fake_spawn)
    monkeypatch.setattr(automatico, "ticket_info",
                        lambda _t: {"status": "Open", "subject": "",
                                    "description": "", "depends_on": [],
                                    "owner_replied": False})
    monkeypatch.setattr(automatico, "repo_problem", lambda: "")
    monkeypatch.setattr(automatico, "git_head", lambda *a: "abc")
    monkeypatch.setattr(automatico, "save_state", lambda _s: None)
    monkeypatch.setattr(automatico, "runner_work_fp",
                        lambda *a, **k: dict(FP_A))
    child = _brake_child()
    settings = automatico.Settings(max_sessions=5)
    state = {"epic": "TASK-2026-00365", "tickets": {}}
    assert automatico.advance(child, settings, state) == "stop"
    assert len(calls) == 5
    ticket = state["tickets"][child["ticket"]]
    assert ticket["parked"] == ""
    assert ticket["brake"]["shadow"] is True


def test_advance_new_diff_keeps_running(monkeypatch):
    calls = []

    def fake_spawn(*_args, **_kwargs):
        calls.append(1)
        return 0, "avance parcial\nTICKET_CONTINUE sigo en ello\n"

    def fake_fp(*_args, **_kwargs):
        return {"fp": f"sha256:fp-{len(calls)}", "complete": True,
                "paths": 1, "bytes": 10, "head": "h", "error": None}

    monkeypatch.setattr(automatico, "spawn", fake_spawn)
    monkeypatch.setattr(automatico, "ticket_info",
                        lambda _t: {"status": "Open", "subject": "",
                                    "description": "", "depends_on": [],
                                    "owner_replied": False})
    monkeypatch.setattr(automatico, "repo_problem", lambda: "")
    monkeypatch.setattr(automatico, "git_head", lambda *a: "abc")
    monkeypatch.setattr(automatico, "save_state", lambda _s: None)
    monkeypatch.setattr(automatico, "runner_work_fp", fake_fp)
    monkeypatch.setattr(automatico, "load_owner_decisions",
                        lambda *_a, **_k: dict(ENFORCE))
    child = _brake_child()
    settings = automatico.Settings(max_sessions=5)
    state = {"epic": "TASK-2026-00365", "tickets": {}}
    assert automatico.advance(child, settings, state) == "stop"
    assert len(calls) == 5
    assert state["tickets"][child["ticket"]]["parked"] == ""


def _git_run(repo, *args):
    import subprocess
    proc = subprocess.run(["git", *args], cwd=repo, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=60, check=False)
    assert proc.returncode == 0, args


def _brake_repo(tmp_path, name="repo"):
    import subprocess
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _git_run(repo, "config", "user.email", "t@t.t")
    _git_run(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("uno\n")
    _git_run(repo, "add", "a.txt")
    _git_run(repo, "commit", "-qm", "base")
    _git_run(repo, "branch", "auto/00374-freno-runner")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          stdout=subprocess.PIPE, check=True,
                          text=True).stdout.strip()
    return repo, base


def test_runner_fp_stable_and_complete(tmp_path):
    repo, base = _brake_repo(tmp_path)
    first = automatico.runner_work_fp("auto/00374-freno-runner", base, repo)
    second = automatico.runner_work_fp("auto/00374-freno-runner", base, repo)
    assert first["fp"] == second["fp"]
    assert first["complete"] is True
    assert first["error"] is None
    assert first["paths"] == 0
    assert first["fp"].startswith("sha256:")


def test_runner_fp_changes_with_branch_commit(tmp_path):
    repo, base = _brake_repo(tmp_path)
    before = automatico.runner_work_fp("auto/00374-freno-runner", base, repo)
    _git_run(repo, "checkout", "-q", "auto/00374-freno-runner")
    (repo / "b.txt").write_text("nuevo\n")
    _git_run(repo, "add", "b.txt")
    _git_run(repo, "commit", "-qm", "trabajo")
    _git_run(repo, "checkout", "-q", "-")
    after = automatico.runner_work_fp("auto/00374-freno-runner", base, repo)
    assert after["fp"] != before["fp"]
    assert after["paths"] == 1


def test_runner_fp_missing_branch_is_stable(tmp_path):
    repo, base = _brake_repo(tmp_path)
    first = automatico.runner_work_fp("auto/inexistente", base, repo)
    second = automatico.runner_work_fp("auto/inexistente", base, repo)
    assert first["fp"] == second["fp"]
    assert first["complete"] is True
    assert first["head"] == base


def test_runner_fp_ignores_runner_scratch(tmp_path):
    repo, base = _brake_repo(tmp_path)
    before = automatico.runner_work_fp("auto/00374-freno-runner", base, repo)
    scratch = repo / ".openceo-automatico"
    scratch.mkdir()
    (scratch / "work-00374-s1.prompt.md").write_text("prompt\n")
    (scratch / "state.json").write_text("{}\n")
    (repo / "nota-suelta.txt").write_text("sin commitear\n")
    after = automatico.runner_work_fp("auto/00374-freno-runner", base, repo)
    assert after["fp"] == before["fp"]
    _git_run(repo, "checkout", "-q", "auto/00374-freno-runner")
    (scratch / "committed.txt").write_text("rastrea pero es scratch\n")
    _git_run(repo, "add", ".openceo-automatico/committed.txt")
    _git_run(repo, "commit", "-qm", "scratch")
    committed = automatico.runner_work_fp(
        "auto/00374-freno-runner", base, repo)
    assert committed["fp"] == before["fp"]
    assert committed["paths"] == 0


def test_runner_fp_only_sees_branch_head(tmp_path):
    repo, base = _brake_repo(tmp_path)
    before = automatico.runner_work_fp("auto/00374-freno-runner", base, repo)
    _git_run(repo, "checkout", "-q", "auto/00374-freno-runner")
    (repo / "a.txt").write_text("cambio sin commitear\n")
    assert automatico.runner_work_fp(
        "auto/00374-freno-runner", base, repo)["fp"] == before["fp"]


def test_runner_fp_limits_and_errors(tmp_path):
    repo, base = _brake_repo(tmp_path)
    _git_run(repo, "checkout", "-q", "auto/00374-freno-runner")
    (repo / "b.txt").write_text("nuevo\n")
    _git_run(repo, "add", "b.txt")
    _git_run(repo, "commit", "-qm", "trabajo")
    limited = automatico.runner_work_fp("auto/00374-freno-runner", base,
                                        repo, max_paths=0)
    assert limited["complete"] is False
    tiny = automatico.runner_work_fp("auto/00374-freno-runner", base, repo,
                                     max_bytes=0)
    assert tiny["complete"] is False
    missing = automatico.runner_work_fp("auto/00374-freno-runner", base,
                                        tmp_path / "no-repo")
    assert missing["complete"] is False
    assert missing["error"] is not None
    assert missing["fp"].startswith("sha256:")
