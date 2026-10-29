"""Identidad de release y automatico.py versionado (TASK-2026-00367, §16.5).

El core y el runner conocen el SHA exacto que ejecutan (.runtime/RELEASE
con {sha, dirty}); restart aborta con cambios rastreados salvo override
explícito; y la lista de hijos de cada épica sale del código (config por
épica fuera de git, o hijos del ERP).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "openceo-core"))

import automatico
from release import read_release

WRITE_RELEASE = _REPO / "scripts" / "write-release.sh"


def _git(repo: Path, *args: str, env: dict | None = None) -> None:
    merged = dict(env) if env else None
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, env=merged)


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    return repo


def _write_release(repo: Path, out: Path, allow_dirty: bool = False) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ, OPENCEO_ALLOW_DIRTY="1" if allow_dirty else "0")
    return subprocess.run(["bash", str(WRITE_RELEASE), str(repo), str(out)],
                          capture_output=True, text=True, env=env)


def test_write_release_clean(tmp_path: Path):
    repo = _repo(tmp_path, "clean")
    out = tmp_path / "rt"
    proc = _write_release(repo, out)
    assert proc.returncode == 0, proc.stderr
    data = json.loads((out / "RELEASE").read_text())
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    assert data == {"sha": head, "dirty": False, "at": data["at"]}


def test_write_release_tracked_dirty_refused(tmp_path: Path):
    repo = _repo(tmp_path, "dirty")
    (repo / "a.txt").write_text("cambio")
    proc = _write_release(repo, tmp_path / "rt")
    assert proc.returncode == 1
    assert not (tmp_path / "rt" / "RELEASE").exists()


def test_write_release_untracked_only_ok(tmp_path: Path):
    repo = _repo(tmp_path, "untracked")
    (repo / "nuevo.txt").write_text("sin rastrear")
    out = tmp_path / "rt"
    proc = _write_release(repo, out)
    assert proc.returncode == 0, proc.stderr
    assert json.loads((out / "RELEASE").read_text())["dirty"] is False


def test_write_release_override_marks_dirty(tmp_path: Path):
    repo = _repo(tmp_path, "override")
    (repo / "a.txt").write_text("cambio")
    out = tmp_path / "rt"
    proc = _write_release(repo, out, allow_dirty=True)
    assert proc.returncode == 0, proc.stderr
    data = json.loads((out / "RELEASE").read_text())
    assert data["dirty"] is True
    assert len(data["sha"]) == 40


def test_read_release_missing_and_invalid(tmp_path: Path):
    assert read_release(tmp_path)["sha"] == "unknown"
    rt = tmp_path / ".runtime"
    rt.mkdir()
    (rt / "RELEASE").write_text("no-json{")
    assert read_release(tmp_path)["sha"] == "unknown"


def test_read_release_roundtrip(tmp_path: Path):
    repo = _repo(tmp_path, "rr")
    out = tmp_path / "rt"
    assert _write_release(repo, out).returncode == 0
    out_repo = tmp_path / "outrepo"
    out_repo.mkdir()
    (out_repo / ".runtime").mkdir()
    (out_repo / ".runtime" / "RELEASE").write_text(
        (out / "RELEASE").read_text())
    info = read_release(out_repo)
    assert info["dirty"] is False and len(info["sha"]) == 40


def _cfg(tmp_path: Path, epic: str, children: list) -> Path:
    d = tmp_path / "epics"
    d.mkdir(exist_ok=True)
    (d / f"{epic}.json").write_text(
        json.dumps({"epic": epic, "children": children}))
    return d


def _kid(ticket: str, **kw) -> dict:
    base = {"step": "0", "ticket": ticket, "branch": "auto/x",
            "deps": [], "tests": [], "knowledge": False}
    base.update(kw)
    return base


def test_load_epic_children_from_config(tmp_path: Path):
    d = _cfg(tmp_path, "TASK-2026-00365",
             [_kid("TASK-2026-00367", branch="auto/00367-identidad-release",
                   tests=["tests/test_automatico_prompt_349.py"])])
    plan, source = automatico.load_epic_children("TASK-2026-00365", d)
    assert source.startswith("config")
    assert len(plan) == 1
    assert plan[0]["branch"] == "auto/00367-identidad-release"
    assert plan[0]["tests"] == ["tests/test_automatico_prompt_349.py"]
    assert plan[0]["epic"] == "TASK-2026-00365"
    assert str(plan[0]["workspace"]) == str(automatico.REPO)


def test_load_epic_children_rejects_invalid(tmp_path: Path):
    d = _cfg(tmp_path, "TASK-2026-00365", [{"step": "0"}])
    with pytest.raises(ValueError):
        automatico.load_epic_children("TASK-2026-00365", d)
    d2 = _cfg(tmp_path, "TASK-2026-00366", [_kid("TASK-2026-00366")])
    (d2 / "TASK-2026-00366.json").write_text(json.dumps(
        {"epic": "TASK-OTRA", "children": [_kid("TASK-2026-00366")]}))
    with pytest.raises(ValueError):
        automatico.load_epic_children("TASK-2026-00366", d2)


def test_load_epic_children_falls_back_to_erp(tmp_path: Path, monkeypatch):
    calls = []

    def fake_read_erp(names, epic=""):
        calls.append((list(names), epic))
        if not names:
            return {"children": {"TASK-2026-00367": "Open"}, "tickets": {}}
        return {"children": {}, "tickets": {
            "TASK-2026-00367": {"status": "Open", "subject": "Identidad!",
                                "description": "", "depends_on": [],
                                "owner_replied": False}}}

    monkeypatch.setattr(automatico, "read_erp", fake_read_erp)
    plan, source = automatico.load_epic_children(
        "TASK-2026-00367", tmp_path / "vacio")
    assert source == "hijos del ERP"
    assert [c["ticket"] for c in plan] == ["TASK-2026-00367"]
    assert plan[0]["branch"].startswith("auto/00367-")
    assert calls and calls[0][1] == "TASK-2026-00367"


def test_release_info_unknown_without_file(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(automatico, "REPO", tmp_path)
    assert automatico.release_info() == {"sha": "unknown", "dirty": None}


def test_release_info_reads_file(monkeypatch, tmp_path: Path):
    rt = tmp_path / ".runtime"
    rt.mkdir()
    (rt / "RELEASE").write_text(json.dumps({"sha": "abc", "dirty": True}))
    monkeypatch.setattr(automatico, "REPO", tmp_path)
    assert automatico.release_info() == {"sha": "abc", "dirty": True}
