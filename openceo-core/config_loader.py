from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from catalog import Catalog, load_catalog, planner_registry_view
from settings import Settings

if TYPE_CHECKING:
    from company_mandate import MandatoEmpresa


class ConfigLoader:
    """Loads per-project YAML configuration."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._repo_root = Path(__file__).resolve().parent.parent

    def available_projects(self) -> list[str]:
        projects_root = self._resolve_projects_root()
        if not projects_root.exists():
            return []

        discovered: list[str] = []
        for candidate in projects_root.iterdir():
            if not candidate.is_dir():
                continue
            if (candidate / "project.yaml").exists():
                discovered.append(candidate.name)

        if not discovered:
            return []

        # Sort projects by 'number' (or 'order' / 'id') defined directly in each project.yaml
        def _get_project_sort_key(name: str) -> tuple[int, str]:
            cfg_path = projects_root / name / "project.yaml"
            if cfg_path.exists():
                try:
                    with cfg_path.open("r", encoding="utf-8") as fh:
                        data = yaml.safe_load(fh) or {}
                    p_info = data.get("project") if isinstance(data.get("project"), dict) else {}
                    num = data.get("number")
                    if num is None:
                        num = p_info.get("number")
                    if num is None:
                        num = data.get("order") or p_info.get("order")
                    if num is None:
                        num = data.get("id") or p_info.get("id")

                    if num is not None:
                        try:
                            return (int(num), name.lower())
                        except (ValueError, TypeError):
                            pass
                except Exception:
                    pass
            return (9999, name.lower())

        return sorted(discovered, key=_get_project_sort_key)

    def has_project(self, project: str | None) -> bool:
        if not project:
            return False
        return (self._resolve_projects_root() / project / "project.yaml").exists()

    def projects_root(self) -> Path:
        """Resolved projects directory (F6 TASK-2026-00115 writes new configs here)."""
        return self._resolve_projects_root()

    def load_project(self, project: str | None) -> dict[str, Any]:
        if not project:
            return self._merge_with_defaults("", {})
        cfg_path = self._resolve_projects_root() / project / "project.yaml"
        if cfg_path.exists():
            with cfg_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            return self._merge_with_defaults(project, data)
        return self._merge_with_defaults(project, {})

    def validate_project(self, project: str | None) -> list[str]:
        """Check raw project.yaml for obsolete or misplaced keys.

        Returns human-readable errors; an empty list means the project may
        run. Tier S design v2 §3/§17: Tier S config and the simple-tier
        executors are global (the defaults above), so any ``dev.tier_s`` or
        ``dev.tiers`` content in a project.yaml is invalid, and
        ``dev.coding_tool`` is obsolete (routing is per-task), as is
        ``dev.coding_tier`` (numeric tiers retired, TASK-2026-00138). The single
        global Objective Planner is likewise global: top-level
        ``objective_planner`` content is invalid (TASK-2026-00112), and so
        is top-level ``objectives`` (TASK-2026-00117). The shared review
        machinery is global too: top-level ``review`` content is invalid
        (TASK-2026-00141), and so is the company-wide CEO reviewer:
        top-level ``ceo`` content is invalid (TASK-2026-00119). The
        keyword ``routing`` block is obsolete (the single planner
        decides, TASK-2026-00204). The optional ``dev.routing`` tier
        guide (TASK-2026-00161) is shape-checked.
        Callers park the project's tickets in ``waiting(config)`` when
        this is non-empty; other projects are unaffected.
        """
        if not project:
            return []
        cfg_path = self._resolve_projects_root() / project / "project.yaml"
        if not cfg_path.exists():
            return []
        try:
            with cfg_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as exc:
            return [f"{project}/project.yaml: unreadable ({exc})"]
        if not isinstance(data, dict):
            return [f"{project}/project.yaml: top level must be a mapping"]
        errors: list[str] = []
        planner = data.get("objective_planner")
        if planner is not None and planner != {}:
            if isinstance(planner, dict):
                found = ", ".join(sorted(str(key) for key in planner))
                errors.append(
                    "objective_planner must not be set in project.yaml: "
                    "the single Objective Planner is global. Remove it from "
                    f"{project}/project.yaml (found: {found})."
                )
            else:
                errors.append(
                    f"objective_planner in {project}/project.yaml must be "
                    "a mapping or absent."
                )
        objectives = data.get("objectives")
        if objectives is not None and objectives != {}:
            if isinstance(objectives, dict):
                found = ", ".join(sorted(str(key) for key in objectives))
                errors.append(
                    "objectives must not be set in project.yaml: "
                    "company-level objectives are global. Remove it from "
                    f"{project}/project.yaml (found: {found})."
                )
            else:
                errors.append(
                    f"objectives in {project}/project.yaml must be "
                    "a mapping or absent."
                )
        review = data.get("review")
        if review is not None and review != {}:
            if isinstance(review, dict):
                found = ", ".join(sorted(str(key) for key in review))
                errors.append(
                    "review must not be set in project.yaml: "
                    "the shared review machinery is global. Remove it from "
                    f"{project}/project.yaml (found: {found})."
                )
            else:
                errors.append(
                    f"review in {project}/project.yaml must be "
                    "a mapping or absent."
                )
        ceo = data.get("ceo")
        if ceo is not None and ceo != {}:
            if isinstance(ceo, dict):
                found = ", ".join(sorted(str(key) for key in ceo))
                errors.append(
                    "ceo must not be set in project.yaml: "
                    "the company CEO reviewer is global. Remove it from "
                    f"{project}/project.yaml (found: {found})."
                )
            else:
                errors.append(
                    f"ceo in {project}/project.yaml must be "
                    "a mapping or absent."
                )
        # Email allowlist (TASK-2026-00208): recipients the secretary may
        # mail without the Owner. Malformed shape parks the project's
        # tickets; absent means nothing is pre-approved (fail-closed).
        secretary = data.get("secretary")
        if secretary is not None:
            if not isinstance(secretary, dict):
                errors.append(
                    f"{project}/project.yaml: 'secretary' must be a mapping."
                )
            else:
                recipients = secretary.get("allowed_recipients")
                if recipients is not None:
                    if not isinstance(recipients, list) or any(
                            not isinstance(entry, str) or not entry.strip()
                            for entry in recipients):
                        errors.append(
                            f"{project}/project.yaml: "
                            "'secretary.allowed_recipients' must be a list "
                            "of non-empty strings."
                        )
                    elif len(recipients) > 128:
                        errors.append(
                            f"{project}/project.yaml: "
                            "'secretary.allowed_recipients' carries at most "
                            "128 entries."
                        )
                    elif any(len(entry.strip()) > 254 for entry in recipients):
                        errors.append(
                            f"{project}/project.yaml: "
                            "'secretary.allowed_recipients' entries run at "
                            "most 254 characters."
                        )
        if "routing" in data:
            errors.append(
                "routing is obsolete: the single planner decides every "
                "pedido (TASK-2026-00204); keyword triggers, patterns, "
                "default_agent and llm_fallback no longer route anything. "
                f"Remove it from {project}/project.yaml."
            )
        dev = data.get("dev")
        if dev is None:
            return errors
        if not isinstance(dev, dict):
            errors.append(
                f"{project}/project.yaml: 'dev' must be a mapping")
            return errors
        if "coding_tool" in dev:
            errors.append(
                "dev.coding_tool is obsolete: routing is per-task "
                "(TaskDocument.coding_tool, set at creation). Remove it from "
                f"{project}/project.yaml."
            )
        if "coding_tier" in dev:
            errors.append(
                "dev.coding_tier is obsolete: numeric tiers are retired, "
                "routing is per-task (TaskDocument.coding_tool, set at "
                "creation). Remove it from "
                f"{project}/project.yaml."
            )
        tier_s = dev.get("tier_s")
        if tier_s is not None and tier_s != {}:
            if isinstance(tier_s, dict):
                found = ", ".join(sorted(str(key) for key in tier_s))
                errors.append(
                    "dev.tier_s must not be set in project.yaml: Tier S cycle "
                    f"config is global. Remove it from {project}/project.yaml "
                    f"(found: {found})."
                )
            else:
                errors.append(
                    f"dev.tier_s in {project}/project.yaml must be a mapping or absent."
                )
        tiers = dev.get("tiers")
        if tiers is not None and tiers != {}:
            if isinstance(tiers, dict):
                found = ", ".join(sorted(str(key) for key in tiers))
                errors.append(
                    "dev.tiers must not be set in project.yaml: simple-tier "
                    f"executors are global. Remove it from {project}/project.yaml "
                    f"(found: {found})."
                )
            else:
                errors.append(
                    f"dev.tiers in {project}/project.yaml must be a mapping or absent."
                )
        # Optional vault mapping for Tier S knowledge notes (design §9.8, F7b).
        vault = dev.get("vault_project")
        if vault is not None:
            if not isinstance(vault, dict):
                errors.append(
                    f"{project}/project.yaml: 'dev.vault_project' must be a "
                    "mapping {dir, project}."
                )
            elif not vault.get("dir") or not vault.get("project"):
                errors.append(
                    f"{project}/project.yaml: 'dev.vault_project' needs "
                    "'dir' and 'project'."
                )
        # Optional per-project tier guide (TASK-2026-00161): a
        # money-sensitive project prefers tier_s for money/algorithm work,
        # never forced. Malformed shape parks the project's tickets.
        routing = dev.get("routing")
        if routing is not None:
            if not isinstance(routing, dict):
                errors.append(
                    f"{project}/project.yaml: 'dev.routing' must be a mapping."
                )
            else:
                money = routing.get("money_sensitive")
                if money is not None and money is not True \
                        and money is not False:
                    errors.append(
                        f"{project}/project.yaml: "
                        "'dev.routing.money_sensitive' must be true or false."
                    )
                for key in ("money_domain", "money_stake"):
                    words = routing.get(key)
                    if words is None:
                        continue
                    if not isinstance(words, list) or any(
                            not isinstance(entry, str) or not entry.strip()
                            for entry in words):
                        errors.append(
                            f"{project}/project.yaml: 'dev.routing.{key}' "
                            "must be a list of non-empty strings."
                        )
                    elif len(words) > 64:
                        errors.append(
                            f"{project}/project.yaml: 'dev.routing.{key}' "
                            "carries at most 64 entries."
                        )
                    elif any(len(entry.strip()) > 80 for entry in words):
                        errors.append(
                            f"{project}/project.yaml: 'dev.routing.{key}' "
                            "entries run at most 80 characters."
                        )
        # dev.gates shape (design §11, §17.2 — F1). dev.qa stays tolerated:
        # the old engine still reads it, and §17.3 key removal lands when the
        # old engine retires, not while both dispatch paths exist.
        if "gates" in dev:
            from agents.dev.tier_s.gates import validate_gates_config
            errors.extend(
                f"{project}/project.yaml: {err}"
                for err in validate_gates_config(dev)
            )
        # dev.oracle_cmd / dev.oracle_timeout_minutes: how the orchestrator
        # runs a frozen oracle (policy §24, TASK-2026-00178). Optional.
        from agents.dev.tier_oracle import validate_oracle_config
        errors.extend(
            f"{project}/project.yaml: {err}"
            for err in validate_oracle_config(dev)
        )
        return errors

    def company_mandate_path(self) -> Path:
        """Exact path of the versioned company mandate (F4 TASK-2026-00114)."""
        return self._resolve_projects_root() / "company_mandate.yaml"

    def load_company_mandate(self) -> "MandatoEmpresa":
        """Load the validated company mandate.

        Raises :class:`MandateError` when the file is missing or invalid:
        without a valid mandate there is no autonomy (fail-closed).
        """
        from company_mandate import load_mandate_file

        return load_mandate_file(self.company_mandate_path())

    def project_root(self, project: str | None) -> Path:
        base = self._resolve_projects_root() / (project or "_global")
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _resolve_projects_root(self) -> Path:
        configured = Path(self._settings.PROJECTS_DIR)

        candidates = [configured] if configured.is_absolute() else [
            configured,
            Path(__file__).resolve().parent / self._settings.PROJECTS_DIR,
            self._repo_root / self._settings.PROJECTS_DIR,
        ]
        for candidate in candidates:
            if self._has_project_configs(candidate):
                return candidate
        for candidate in candidates:
            if candidate.exists():
                return candidate

        # Fallback path even if it does not exist yet.
        return self._repo_root / self._settings.PROJECTS_DIR

    @staticmethod
    def _has_project_configs(path: Path) -> bool:
        if not path.exists() or not path.is_dir():
            return False
        return any(
            candidate.is_dir() and (candidate / "project.yaml").exists()
            for candidate in path.iterdir()
        )

    @staticmethod
    def _merge_with_defaults(project: str, cfg: dict[str, Any]) -> dict[str, Any]:
        defaults = {
            "project": {
                "name": project,
                "repos": [],
            },
            "dev": {
                # No dev.coding_tool default: routing is per-task
                # (TaskDocument.coding_tool). A project default would silently
                # route untagged tickets (Tier S design §3).
                "default_provider": "auto",
                "branch_pattern": "{session_id}-{slug}",
                "auto_push": False,
                "create_pr": True,
                "pr_body": "empty",
                # Tier S merge policy (design §12, F2): the engine integrates
                # on close when true. Only the Tier S engine reads it.
                "auto_merge": True,
                "budget_exclude": [],
                # Tier S gates (design §11, F1): run by the orchestrator on every
                # tier. Empty = none configured (telemetry note, not a block).
                "gates": [],
                # Global Tier S cycle config: only what the single-ticket
                # cycle owns. The review machinery (pool, rounds, inbox, fix
                # tiers, closer/arbiter roles and timeouts) lives in the
                # shared top-level `review` block (TASK-2026-00141).
                # Projects must not set dev.tier_s (see validate_project).
                # Accounts are interchangeable: swapping e.g. claudej -> clauded
                # only changes `account:` (+ its command) here, never tickets,
                # code or data (design v2 §3.1, §4.4).
                "tier_s": {
                    "timeout_minutes": 15,
                    "min_available_families": 3,
                    # Tier S engine budgets, waits and timeouts (design §13, §14,
                    # §17.1 — F1/F2). Single global source; projects override
                    # nothing here.
                    "budgets": {
                        "files_per_ticket": 8,
                        "lines_per_ticket": 400,
                        "hard_brake_factor": 2,
                        "gate_retries": 2,
                        "escalate_after": 2,
                        "capsule_tokens": 2000,
                        "handoff_words": 300,
                        "agents_md_tokens": 3000,
                    },
                    "waiting": {
                        "mid_cycle_hold_minutes": 30,
                        "backoff_minutes": [15, 30, 60],
                    },
                    "timeouts_minutes": {
                        "planner": 20,
                        "plan_review": 15,
                        "build": 60,
                        "fix": 30,
                    },
                    # Roles run one-shot (live executor): `command` + `headless_args`
                    # form the base argv, the prompt travels per `prompt_via`.
                    # model/effort ride the command explicitly (§4.4); accounts
                    # stay interchangeable via `account:` (+ command).
                    "roles": {
                        "planner": {"provider": "claude", "account": "claudej", "label": "claudej", "model": "opus", "effort": "max", "active": True, "command": ["claudej"], "headless_args": ["--print"], "prompt_via": "argv"},
                        "plan-reviewer": {"provider": "codex", "account": "codexj", "label": "codexj", "model": "gpt-6.1-sol", "effort": "xhigh", "active": True, "command": ["codexj"], "headless_args": ["exec", "--ephemeral", "-"], "prompt_via": "stdin"},
                        "builder": {"provider": "muse", "account": "muse", "label": "muse", "model": "muse-spark-1.3-contributor", "effort": "max", "active": True, "command": ["musej", "exec", "--model", "muse-spark-1.3-contributor"], "prompt_via": "argv", "role_profile": "dev"},
                    },
                    # Review machinery (pool, rounds, inbox, fix tiers) and the
                    # legacy objectives flow keys moved to the shared
                    # top-level `review` / `objectives` blocks (TASK-2026-00141).
                },
                # Global simple-tier executors (design v2 §3.1, §17.1, F0.1/F5).
                # Projects must not set dev.tiers (see validate_project).
                # `model_args` are appended after the wrapper argv so the
                # orchestrator's model/effort win explicitly (§4.4). Tier B
                # needs none: its wrapper already pins the exact same
                # values (-m gpt-6.1-sol, effort xhigh).
                "tiers": {
                    "tier_a": {"account": "claudej", "model": "opusplan", "effort": "xhigh",
                               "model_args": ["--model", "opusplan", "--effort", "xhigh"],
                               "role_profile": "dev"},
                    "tier_b": {"account": "codexj", "model": "gpt-6.1-sol", "effort": "xhigh",
                               "model_args": [],
                               "role_profile": "dev"},
                    "tier_c": {"chain": ["muse", "dsh", "claudep"],
                               "muse": {"model": "muse-spark-1.3-contributor", "effort": "max",
                                        "command": ["musej", "exec", "--model", "muse-spark-1.3-contributor"],
                                        "role_profile": "dev"},
                               "dsh": {"model": "deepseek-v4.1-flash", "effort": "max",
                                       "command": ["node", "/home/develop/development/Jardal/jardal-deepseek-harness/apps/cli/lib/bin.js", "--profile", "headless", "-"],
                                       "env": {"DSH_HOME": "/home/develop/.dsh"}},
                               "claudep": {"model": "deepseek-flash[1m]", "command": ["claudep"]}},
                },
                "providers": {
                    "codex": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                    "claude": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                    "gemini": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                    "opencode": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                    "glm": {
                        "enabled": False,
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                    "kimi": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                    "muse": {
                        "command": [],
                        "cwd": None,
                        "env": {},
                    },
                },
                "qa": {
                    "playwright": "optional",
                    "confidence_threshold": 0.9,
                    "max_retries": 2,
                    # Project policy: human review mandated (e.g. a client
                    # demanding human QA). Fifth escalation category
                    # (autonomy F4 TASK-2026-00114, TASK-2026-00142); default False.
                    "require_human_review": False,
                },
            },
            # Shared review machinery (autonomy epic TASK-2026-00110, F2
            # TASK-2026-00141): the single source both engines read — the
            # Tier S ticket cycle and the company-level objectives engine
            # (final review, inbox, arbitration). Moved out of dev.tier_s.
            # Projects must not set review (see validate_project).
            "review": {
                # One-shot roles shared by both engines (live executor):
                # `command` + `headless_args` form the base argv, the prompt
                # travels per `prompt_via`. The closer runs only in the
                # objectives final review; the arbiter in both engines.
                "roles": {
                    "closer": {"provider": "muse", "account": "muse", "label": "muse", "model": "muse-spark-1.3-contributor", "effort": "max", "active": True, "command": ["musej", "exec", "--model", "muse-spark-1.3-contributor"], "prompt_via": "argv", "role_profile": "dev"},
                    "arbiter": {"provider": "claude", "account": "claudej", "label": "claudej", "model": "opus", "effort": "max", "active": True, "command": ["claudej"], "headless_args": ["--print"], "prompt_via": "argv"},
                },
                "timeouts_minutes": {
                    "arbiter": 10,
                    "closer": 15,
                },
                # Expired subscriptions remain configured but inactive; free reserves are ranked in the catalog.
                "review": {
                    "round1_parallel": 3,
                    "round2_parallel": 2,
                    "quorum": 2,
                    "distinct_by": "family",
                    "min_available_families": 3,
                    "max_rounds": 2,
                    "reviewer_timeout_minutes": 15,
                    "recovery": {
                        "attempts_per_slot": 3,
                        "retry_delay_seconds": 300,
                        "max_wait_seconds": 43200,
                    },
                    # Free-model router feeding (epic TASK-2026-00283,
                    # TASK-2026-00293): the two best free-review models
                    # feed the free OpenCode slots. Back to manual after
                    # the validation pilot (TASK-2026-00301); `enabled:
                    # True` reactivates it when data ranks a review pair.
                    # Anti-cut cascade (TASK-2026-00310): free → own →
                    # metered waves with a deterministic spend guard,
                    # then relaxed quorum (extras count), else block
                    # with reasons. Off by default: classic quorum.
                    # Paid tier (Owner pre-authorized 2026-10-26): cap
                    # 0.50/review under the 10.00 mandate bar (the
                    # prepaid credit size; never new money). The guard
                    # still refuses any cap above the threshold.
                    "cascade": {
                        "enabled": True,
                        "paid_cap_usd": "0.50",
                        "paid_estimate_usd": "0.05",
                        "dsh_estimate_usd": "0.00",
                        "paid_dynamic": True,
                        "paid_whitelist": [
                            "openai/gpt-oss-20b",
                            "inclusionai/ling-3.0-flash",
                            "mistralai/mistral-nemo",
                        ],
                        "premium": False,
                        "premium_cap_usd": "1.00",
                        "premium_estimate_usd": "0.50",
                        "premium_whitelist": [],
                    },
                    "free_router": {
                        "enabled": False,
                        "round2_policy": "reuse_best_plus_novel",
                        "snapshot_max_age_hours": 30,
                    },
                },
                "reviewers": [
                    {"label": "qwen-max", "provider": "qwen", "model": "qwen3.8-max", "family": "qwen", "priority": 1, "active": False, "quota": "reactive", "expires_on": "2026-10-19", "prompt_via": "argv", "command": ["qwen", "-m", "qwen3.8-max", "--approval-mode", "plan"], "limit_patterns": [], "expired_patterns": []},
                    {"label": "qwen-glm", "provider": "qwen", "model": "glm-5.3", "family": "glm", "priority": 2, "active": False, "quota": "reactive", "expires_on": "2026-10-19", "prompt_via": "argv", "command": ["qwen", "-m", "glm-5.3", "--approval-mode", "plan"], "limit_patterns": [], "expired_patterns": []},
                    {"label": "dsh", "provider": "dsh", "model": "deepseek-v4.1-flash", "family": "deepseek", "priority": 3, "active": True, "cost_tier": "metered", "quota": "reactive", "prompt_via": "stdin", "command": ["node", "/home/develop/development/Jardal/jardal-deepseek-harness/apps/cli/lib/bin.js", "--profile", "headless", "-"], "env": {"DSH_HOME": "/home/develop/.dsh"}, "limit_patterns": [], "expired_patterns": []},
                    {"label": "claudep", "provider": "claude", "model": "deepseek-flash", "family": "deepseek", "priority": 9, "active": False, "quota": "reactive", "prompt_via": "argv", "command": ["claudep"], "headless_args": ["--print"]},  # deepseek reserve while dsh serves
                    {"label": "muse-review", "provider": "muse", "model": "muse-spark-1.3-contributor", "family": "muse", "priority": 99, "active": True, "extra": True, "cost_tier": "own", "quota": "reactive", "prompt_via": "argv", "command": ["muse", "exec", "--model", "muse-spark-1.3-contributor", "--reasoning-effort", "max"]},  # extra: never counts for quorum (§7.3), except cascade final degradation (00310, audited). Plain muse on purpose: isolated reviewer (sandbox+approvals ON, no --trust-workspace); musej appends its flags last so they cannot be overridden.
                    {"label": "gemini", "provider": "gemini", "model": "gemini", "family": "gemini", "account": "agyd", "priority": 60, "active": True, "cost_tier": "own", "quota": "reactive", "prompt_via": "argv", "headless_args": ["--print"]},  # single logical Gemini slot (00250): the account IS the command, selectable by config among agyd/agyj/agy2 (no static command; resolved + validated by review.with_resolved_command, fail-closed on unknown account or command mismatch). Model is the generic account default: versions are never pinned here (verified agy 1.3.5 --help/--version/models 2026-10-14, 0 tokens). --print takes the prompt as its value (bare agyd would hang waiting for input). Own flat-rate plan, never a free tier (00310).
                    {"label": "mimo", "provider": "opencode", "model": "opencode/mimo-v2.6-flash-free", "family": "mimo", "priority": 4, "active": True, "cost_tier": "free", "quota": "reactive", "prompt_via": "argv", "command": ["opencode", "run", "-m", "opencode/mimo-v2.6-flash-free"]},  # script candidate (00250): runs today in the automatico.py live flow. opencode has no effort flag (verified v2.0.22 run --help 2026-10-14, 0 tokens). Availability stays reactive: a dead model ID fails explicit (generic model-not-found disables the slot), never silently.
                    {"label": "nemotron", "provider": "opencode", "model": "opencode/nemotron-3.5-super-vl-free", "family": "nemotron", "priority": 5, "active": True, "cost_tier": "free", "quota": "reactive", "prompt_via": "argv", "command": ["opencode", "run", "-m", "opencode/nemotron-3.5-super-vl-free"]},  # repointed 2026-10-26 by Owner order: nemotron-3-ultra-free is dead (invalid-request + gone from the live catalog); super-vl-free is live with tools.
                    {"label": "openrouter-paid", "provider": "opencode", "model": "openrouter/openai/gpt-oss-20b", "family": "openrouter", "priority": 70, "active": True, "cascade_only": True, "cost_tier": "metered", "paid_dynamic": True, "quota": "reactive", "prompt_via": "argv", "command": ["opencode", "run", "-m", "openrouter/openai/gpt-oss-20b"], "review_hardened": True, "review_target": {"env_var": "FREE_ROUTER_OPENROUTER_API_KEY", "providers": {"openrouter": {"name": "Paid OpenRouter (cascade 00310)", "package": "@opencode/ai/providers/openai-compatible", "env": ["FREE_ROUTER_OPENROUTER_API_KEY"], "settings": {"baseURL": "https://openrouter.ai/api/v1", "apiKey": "{env:FREE_ROUTER_OPENROUTER_API_KEY}"}, "models": {"openai/gpt-oss-20b": {"name": "openai/gpt-oss-20b"}, "inclusionai/ling-3.0-flash": {"name": "inclusionai/ling-3.0-flash"}, "mistralai/mistral-nemo": {"name": "mistralai/mistral-nemo"}}}}}},  # paid last resort (00310): cheapest-first auto-ranking (paid.json) with this static top-3 as fallback; whitelist + hard cap; credential loads at runtime, never versioned.
                ],
                # Findings inbox promotion (design §8.3, F4) and fixed
                # fix-ticket tiers (design §3.3).
                "inbox": {
                    "promotion": {"severity": "CRITICAL", "anchor": "strong", "min_families": 2, "or_category": "security"},
                },
                "fix_ticket_tiers": {"security": "tier_s", "critical": "tier_a", "default": "tier_c"},
            },
            # Single global Objective Planner (autonomy epic TASK-2026-00110,
            # TASK-2026-00112; normative policy + ADR in docs/). It lives
            # outside dev.tier_s on purpose: Tier S ticket planning
            # (Claude/Codex plan for one ticket) is a different pipeline.
            # Projects must not set objective_planner (see validate_project).
            "objective_planner": {
                "engine": "muse_code",
                "profile": "muse_spark_1_3_contributor",
                "invocation": "headless",
                "ticket_horizon": [1, 3],
                "timeout_minutes": 15,
                # None = openceo-core/objectives/routing_policy.md, next to
                # the planner module (resolved by it): the core file, with
                # per-agent detail in routing_policy.d/ (TASK-2026-00201).
                "policy_path": None,
                # One-shot role spec (live executor shape): a fresh muse
                # process per planning cycle, prompt via argv, result read
                # back from .openceo/objectives/<id>/PLANNER_RESULT.json
                # (per-objective exclusive output, TASK-2026-00176).
                # musej (full access): the planner writes the result
                # file, it is not an isolated reviewer.
                "role": {
                    "provider": "muse", "account": "muse",
                    "label": "muse", "model": "muse-spark-1.3-contributor",
                    "effort": "max", "active": True,
                    "command": ["musej", "exec", "--model",
                                "muse-spark-1.3-contributor"],
                    "prompt_via": "argv",
                    "role_profile": "planner",
                },
                # Authoritative agent registry for target_agent (policy
                # §6): derived from the single catalog (TASK-2026-00195;
                # humans included by TASK-2026-00198).
                "agents": _planner_agents_from_catalog(),
            },
            # Company-level objectives (autonomy epic TASK-2026-00110, F2
            # TASK-2026-00117). The engine lives outside dev.tier_s, and so
            # does its config (TASK-2026-00141 retired the
            # dev.tier_s.objectives legacy fallback). Projects must not set
            # objectives (see validate_project). The company mandate (F4
            # TASK-2026-00114) owns the real capacity numbers; these are
            # safe deterministic defaults.
            "objectives": {
                "max_tickets": 20,
                "max_planning_runs_without_progress": 2,
                "max_final_reviews": 2,
                "inbox_items_for_planner": 15,
                "max_active_objectives_global": 3,
                "max_active_objectives_per_project": 2,
                "max_active_tickets_per_objective": 6,
            },
            # Autonomous company CEO (autonomy epic TASK-2026-00110,
            # F4 TASK-2026-00119). One headless Muse run per review
            # over bounded compiled state, own CEODecision contract.
            # Same account/model/invocation as the planner, separate
            # role label so telemetry never confuses a CEO review
            # with a planning cycle. Projects must not set ceo
            # (see validate_project).
            "ceo": {
                "engine": "muse_code",
                "profile": "muse_spark_1_3_contributor",
                "invocation": "headless",
                "timeout_minutes": 15,
                "role": {
                    "provider": "muse", "account": "muse",
                    "label": "ceo_reviewer",
                    "model": "muse-spark-1.3-contributor",
                    "effort": "max", "active": True,
                    "command": ["musej", "exec", "--model",
                                "muse-spark-1.3-contributor"],
                    "prompt_via": "argv",
                    "role_profile": "ceo",
                },
            },
            "voice": {
                "peer_programming": {
                    "enabled": True,
                    "require_project_selection": True,
                }
            },
            "notifications": [],
            "research": {
                # Politica de research POR PROYECTO. Nace apagada de forma deliberada:
                # habilitar research es una decision explicita del proyecto, no un
                # efecto colateral de tener claves de proveedor en el entorno. Los
                # adaptadores pagados nacen apagados aunque exista clave.
                "enabled": False,
                "default_mode": "quick",
                "allowed_modes": ["quick"],
                # Techo de clasificacion que este proyecto puede pedir. El servidor
                # recorta cualquier peticion por encima; el payload no eleva derechos.
                "classification_ceiling": "public",
                "adapters": {
                    "brave": True,
                    "serper": False,
                    "firecrawl": False,
                    "grounding": False,
                },
                # Presupuesto pagado aprobado para este proyecto (USD, cadena decimal).
                # "0.00" significa pago cero: no se llaman proveedores pagados.
                "paid_budget_usd": "0.00",
                "limits": {
                    "quick": {"search_requests": 3, "pages": 5, "steps": 8, "seconds": 120},
                    "deep": {"search_requests": 15, "pages": 25, "steps": 30, "seconds": 900},
                    # ``watch`` define lote/periodo por suscripcion (Entrega B).
                    "watch": {"search_requests": 0, "pages": 10, "steps": 0, "seconds": 60},
                },
                # Referencias de cuenta autorizadas. Vacio = ninguna cuenta social.
                "allowed_account_refs": [],
            },
        }
        defaults["review"]["reviewers"].extend(
            _reviewer_reserves(Path(__file__).resolve().parent.parent))
        return _deep_merge(defaults, cfg)


def _reviewer_reserves(repo: Path) -> list[dict[str, Any]]:
    path = repo / "catalog" / "reviewer_models.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("models") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: models must be a nonempty ranked list")
    slots = []
    seen: set[str] = set()
    for rank, entry in enumerate(entries):
        model = str(entry.get("model") or "") if isinstance(entry, dict) else ""
        family = str(entry.get("family") or "") if isinstance(entry, dict) else ""
        if (not model.startswith("opencode/") or not family or model in seen
                or not (model.endswith("-free") or model == "opencode/big-pickle")):
            raise ValueError(f"{path}: invalid free reviewer at rank {rank + 1}")
        seen.add(model)
        slots.append({
            "label": f"reserve-{model.split('/', 1)[1]}",
            "provider": "opencode", "model": model, "family": family,
            "priority": 100 + rank, "active": True, "reserve_only": True,
            "extra": family == "anonymous",
            "quota": "reactive", "prompt_via": "argv",
            "command": ["opencode", "run", "--standalone", "--format", "json", "-m", model],
        })
    return slots


def _planner_agents_from_catalog(
    catalog: Catalog | None = None,
) -> dict[str, dict[str, Any]]:
    """Planner registry derived from the single catalog.

    AI agents plus humans (owner and staff names): the planner may target
    any enabled one (TASK-2026-00198). Shape kept: name →
    {"enabled": bool, "reason"?: str}, consumed by the planner prompt and
    ``validate_planner_result``.
    """
    return planner_registry_view(catalog or load_catalog())


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
