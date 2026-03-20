"""In-process manager for AutoResearch runs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from autoresearch.execution import (
    CommandExecutionError,
    CommandExecutionResult,
    WorkspaceSetup,
    collect_workspace_changes,
    execute_phase_command,
    is_better_metric,
    load_metrics_file,
    load_text_artifact,
    resolve_artifact_path,
    resolve_workspace_setup,
    restore_workspace_paths,
    select_primary_metric,
)
from autoresearch.hermes_roles import HermesRoleRunner, RoleResult
from autoresearch.manifests import load_manifest
from autoresearch.models import (
    ResearchCandidate,
    ResearchEvent,
    ResearchMetric,
    ResearchReport,
    ResearchRun,
)
from autoresearch.storage import AutoResearchStore

TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "stopped"})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunNotFoundError(KeyError):
    """Raised when a run ID is unknown."""


class InvalidRunStateError(ValueError):
    """Raised when a state transition is not allowed."""


class AutoResearchManager:
    """Owns run persistence and live event coordination for a single process."""

    def __init__(
        self,
        store: Optional[AutoResearchStore] = None,
        base_dir: Optional[Path] = None,
        role_runner: Optional[HermesRoleRunner] = None,
    ):
        self.store = store or AutoResearchStore(root=base_dir)
        self.role_runner = role_runner or HermesRoleRunner()
        self._condition = threading.Condition()
        self._worker_lock = threading.RLock()
        self._workers: Dict[str, threading.Thread] = {}
        self._iteration_delay = max(0.01, float(os.getenv("AUTORESEARCH_ITERATION_DELAY", "0.05")))
        self._command_timeout = max(5, int(os.getenv("AUTORESEARCH_COMMAND_TIMEOUT", "180")))

    def list_runs(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        runs = [self._serialize_run(run, include_manifest=False) for run in self.store.list_runs()]
        if limit is not None:
            return runs[: max(0, int(limit))]
        return runs

    def get_run(self, run_id: str, include_manifest: bool = True) -> Dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return self._serialize_run(run, include_manifest=include_manifest)

    def list_events(self, run_id: str, after_seq: int = 0) -> List[Dict[str, Any]]:
        self._require_run(run_id)
        return [self._serialize_event(event) for event in self.store.list_events(run_id, after_sequence=after_seq)]

    def list_reports(self, run_id: str) -> List[Dict[str, Any]]:
        self._require_run(run_id)
        return [self._serialize_report(report) for report in self.store.list_reports(run_id)]

    def list_candidates(self, run_id: str) -> List[Dict[str, Any]]:
        self._require_run(run_id)
        return [self._serialize_candidate(candidate) for candidate in self.store.list_candidates(run_id)]

    def list_metrics(self, run_id: str) -> List[Dict[str, Any]]:
        self._require_run(run_id)
        return [self._serialize_metric(metric) for metric in self.store.list_metrics(run_id)]

    def list_iterations(self, run_id: str) -> List[Dict[str, Any]]:
        """Return per-iteration summaries derived from candidates and metrics."""
        self._require_run(run_id)
        candidates = self.store.list_candidates(run_id)
        metrics = self.store.list_metrics(run_id)

        candidates_by_iter: Dict[int, Any] = {}
        for candidate in candidates:
            candidates_by_iter[candidate.iteration] = candidate

        metrics_by_iter: Dict[int, List[Any]] = {}
        for metric in metrics:
            metrics_by_iter.setdefault(metric.iteration, []).append(metric)

        iteration_numbers = sorted(
            set(candidates_by_iter.keys())
            | set(metrics_by_iter.keys())
            | set(self.store.list_iteration_numbers(run_id))
        )

        results: List[Dict[str, Any]] = []
        for iteration in iteration_numbers:
            candidate = candidates_by_iter.get(iteration)
            iter_metrics = metrics_by_iter.get(iteration, [])

            mutation_changed = 0
            mutation_blocked = 0
            primary_metric_name = None
            primary_metric_value = None
            for m in iter_metrics:
                if m.name == "mutation.changed_files":
                    mutation_changed = int(m.value)
                elif m.name == "mutation.blocked_files":
                    mutation_blocked = int(m.value)
                elif m.name not in ("mutation.allowed_files", "iteration_progress"):
                    if primary_metric_name is None:
                        primary_metric_name = m.name
                        primary_metric_value = m.value

            entry: Dict[str, Any] = {
                "iteration": iteration,
                "status": candidate.status if candidate else "unknown",
                "title": candidate.title if candidate else f"Iteration {iteration}",
                "has_mutation_audit": mutation_changed > 0 or mutation_blocked > 0,
                "mutation_changed_files": mutation_changed,
                "mutation_blocked_files": mutation_blocked,
            }
            if primary_metric_name:
                entry["primary_metric"] = {
                    "name": primary_metric_name,
                    "value": primary_metric_value,
                }
            if candidate:
                entry["candidate_id"] = candidate.id
            results.append(entry)
        return results

    def get_mutation_audit(self, run_id: str, iteration: int) -> Dict[str, Any]:
        """Return mutation audit data with inline diffs for a specific iteration."""
        self._require_run(run_id)

        # Find the candidate for this iteration to get structured audit metadata
        candidates = self.store.list_candidates(run_id)
        candidate = None
        for c in candidates:
            if c.iteration == iteration:
                candidate = c
                break

        audit_meta: Dict[str, Any] = {}
        if candidate and isinstance(candidate.metadata, dict):
            mutator_block = candidate.metadata.get("mutator")
            if isinstance(mutator_block, dict):
                audit_meta = dict(mutator_block.get("audit") or {})

        # Load diff file contents from disk
        diffs = self.store.load_mutation_audit_diffs(run_id, iteration)

        # Enrich the changes list with inline diff content
        changes = list(audit_meta.get("changes") or [])
        for change in changes:
            relative_path = change.get("path", "")
            if relative_path in diffs:
                change["diff"] = diffs[relative_path]

        return {
            "iteration": iteration,
            "run_id": run_id,
            "workspace_snapshot": audit_meta.get("workspace_snapshot", False),
            "changed_paths": audit_meta.get("changed_paths") or [],
            "allowed_paths": audit_meta.get("allowed_paths") or [],
            "blocked_paths": audit_meta.get("blocked_paths") or [],
            "restored_paths": audit_meta.get("restored_paths") or [],
            "changes": changes,
            "diffs": diffs,
            "role": audit_meta.get("role"),
        }

    def create_run(
        self,
        *,
        name: Optional[str],
        goal: str = "",
        manifest_path: Optional[str] = None,
        manifest: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_iterations: Optional[int] = None,
        autostart: bool = False,
    ) -> Dict[str, Any]:
        resolved_manifest = self._resolve_manifest(manifest_path, manifest)
        resolved_max_iterations = self._resolve_max_iterations(
            max_iterations=max_iterations,
            manifest=resolved_manifest,
            metadata=metadata,
        )
        goal = (
            str(goal or "").strip()
            or str((resolved_manifest or {}).get("objective") or "").strip()
            or str((resolved_manifest or {}).get("name") or "").strip()
            or str(name or "").strip()
        )
        if not goal:
            raise ValueError("Missing required field: goal")

        run = self.store.create_run(
            title=str(name or "").strip(),
            goal=goal,
            notes=str((metadata or {}).get("notes") or ""),
            manifest=resolved_manifest,
            max_iterations=resolved_max_iterations,
            metadata=dict(metadata or {}),
        )
        self._emit_event(
            run.id,
            "run.created",
            {
                "title": run.title,
                "goal": run.goal,
                "autostart": bool(autostart),
            },
        )
        report = self.store.write_report(
            run.id,
            kind="run_brief",
            title="Run brief",
            content=self._build_run_brief(run),
            metadata={"manifest_present": bool(run.manifest)},
        )
        self._emit_event(
            run.id,
            "report.written",
            {
                "report_id": report.id,
                "kind": report.kind,
                "title": report.title,
            },
        )
        if autostart:
            self.resume_run(run.id)
        return self.get_run(run.id)

    def pause_run(self, run_id: str) -> Dict[str, Any]:
        run = self._load_run(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            raise InvalidRunStateError(f"Cannot pause a {run.status} run")
        if run.status != "running":
            raise InvalidRunStateError(f"Cannot pause a {run.status} run")
        self.store.update_run(run_id, status="paused", phase="paused")
        self._emit_event(run_id, "run.paused", {"phase": "paused"})
        return self.get_run(run_id)

    def resume_run(self, run_id: str) -> Dict[str, Any]:
        run = self._load_run(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            raise InvalidRunStateError(f"Cannot resume a {run.status} run")
        if run.status != "running":
            self.store.update_run(run_id, status="running", phase="running", last_error=None)
            self._emit_event(run_id, "run.resumed", {"phase": "running"})
        self._ensure_worker(run_id)
        return self.get_run(run_id)

    def stop_run(self, run_id: str) -> Dict[str, Any]:
        run = self._load_run(run_id)
        if run.status == "stopped":
            return self.get_run(run_id)
        if run.status in {"completed", "failed"}:
            raise InvalidRunStateError(f"Cannot stop a {run.status} run")
        self.store.update_run(run_id, status="stopped", phase="stopped")
        self._emit_event(run_id, "run.stopped", {"phase": "stopped"})
        return self.get_run(run_id)

    def add_operator_message(
        self,
        run_id: str,
        *,
        content: str,
        scope: str = "run",
        author: str = "operator",
    ) -> Dict[str, Any]:
        content = str(content or "").strip()
        if not content:
            raise ValueError("Missing required field: message")
        message = self.store.append_operator_message(
            run_id,
            content=content,
            scope=scope,
            metadata={"author": author},
        )
        return self._emit_event(
            run_id,
            "operator.message",
            {
                "message_id": message.id,
                "scope": message.scope,
                "author": author,
                "content": message.content,
            },
        )

    def add_global_message(
        self, *, content: str, author: str = "operator"
    ) -> Dict[str, Any]:
        content = str(content or "").strip()
        if not content:
            raise ValueError("Missing required field: message")
        message = self.store.append_global_message(
            content=content,
            metadata={"author": author},
        )
        return message.to_dict()

    def list_global_messages(self) -> List[Dict[str, Any]]:
        return [m.to_dict() for m in self.store.list_global_messages()]

    def request_mutation(self, run_id: str, *, reason: str = "") -> Dict[str, Any]:
        run = self.store.increment_mutation_requests(run_id)
        return self._emit_event(
            run_id,
            "mutation.requested",
            {
                "count": run.mutation_request_count,
                "reason": str(reason or "").strip(),
            },
        )

    def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        message: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = dict(data or {})
        if message:
            payload["message"] = message
        return self._emit_event(run_id, event_type, payload)

    def write_report(
        self,
        run_id: str,
        *,
        report_type: str = "report",
        title: str = "",
        content: str = "",
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._require_run(run_id)
        report = self.store.write_report(
            run_id,
            kind=report_type,
            title=title or "Report",
            content=content,
            metadata=dict(data or {}),
        )
        self._emit_event(
            run_id,
            "report.written",
            {
                "report_id": report.id,
                "kind": report.kind,
                "title": report.title,
            },
        )
        return self._serialize_report(report)

    def add_candidate(
        self,
        run_id: str,
        *,
        iteration: int,
        title: str,
        summary: str,
        status: str = "evaluated",
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._require_run(run_id)
        candidate = self.store.append_candidate(
            run_id,
            iteration=iteration,
            title=title,
            summary=summary,
            status=status,
            metadata=dict(data or {}),
        )
        self._emit_event(
            run_id,
            "candidate.recorded",
            {
                "candidate_id": candidate.id,
                "iteration": candidate.iteration,
                "title": candidate.title,
                "status": candidate.status,
            },
        )
        return self._serialize_candidate(candidate)

    def add_metric(
        self,
        run_id: str,
        *,
        iteration: int,
        name: str,
        value: float,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._require_run(run_id)
        metric = self.store.append_metric(
            run_id,
            iteration=iteration,
            name=name,
            value=value,
            metadata=dict(data or {}),
        )
        self._emit_event(
            run_id,
            "metric.recorded",
            {
                "metric_id": metric.id,
                "iteration": metric.iteration,
                "name": metric.name,
                "value": metric.value,
            },
        )
        return self._serialize_metric(metric)

    def wait_for_events(self, run_id: str, after_seq: int, timeout: float) -> bool:
        """Block until a new event exists after *after_seq* or timeout expires."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                run = self._load_run(run_id)
                if run.event_count > after_seq:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)

    def wait_for_status(self, run_id: str, statuses: List[str], timeout: float) -> Optional[Dict[str, Any]]:
        """Block until the run reaches one of *statuses* or timeout expires."""
        target = set(statuses)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                run = self._load_run(run_id)
                serialized = self._serialize_run(run)
                if run.status in target:
                    return serialized
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)

    def _ensure_worker(self, run_id: str) -> None:
        with self._worker_lock:
            worker = self._workers.get(run_id)
            if worker and worker.is_alive():
                return
            worker = threading.Thread(
                target=self._run_worker,
                args=(run_id,),
                name=f"autoresearch-{run_id[:8]}",
                daemon=True,
            )
            self._workers[run_id] = worker
            worker.start()

    def _run_worker(self, run_id: str) -> None:
        try:
            self.append_event(
                run_id,
                "worker.started",
                message="AutoResearch background worker started",
            )
            while True:
                run = self._load_run(run_id)
                if run.status != "running":
                    return
                if run.current_iteration >= run.max_iterations:
                    self._complete_run(run_id)
                    return
                self._execute_iteration(run_id, run.current_iteration + 1)
        except RunNotFoundError:
            return
        except Exception as exc:
            self._fail_run(run_id, exc)
        finally:
            with self._worker_lock:
                current = self._workers.get(run_id)
                if current is threading.current_thread():
                    self._workers.pop(run_id, None)
            with self._condition:
                self._condition.notify_all()

    def _execute_iteration(self, run_id: str, iteration: int) -> None:
        run = self._load_run(run_id)
        if run.status != "running":
            return

        manifest = dict(run.manifest or {})
        workspace = resolve_workspace_setup(store=self.store, run=run, iteration=iteration)
        recent_messages = self._recent_operator_messages(run_id, limit=3)
        mutation_requests = int(self._load_run(run_id).mutation_request_count)
        context = self._build_iteration_context(run, iteration, workspace)

        self.store.update_run(run_id, phase="planning")
        self.append_event(
            run_id,
            "iteration.started",
            message=f"Iteration {iteration} started",
            data={"iteration": iteration},
        )
        self.append_event(
            run_id,
            "workspace.prepared",
            message=f"Workspace prepared for iteration {iteration}",
            data={
                "iteration": iteration,
                "workspace": workspace.to_dict(),
            },
        )
        plan_summary = self._build_iteration_summary(
            run=run,
            iteration=iteration,
            manifest=manifest,
            workspace=workspace,
            recent_messages=recent_messages,
            mutation_requests=mutation_requests,
        )

        # --- Orchestrator: decide which roles to activate this iteration ---
        delegation: Dict[str, Any] = {"researcher": True, "critic": True, "strategy": ""}
        orchestrator_role = self._invoke_role(
            role_name="orchestrator",
            run=run,
            iteration=iteration,
            context=context,
            payload={
                "run": self._serialize_run(run),
                "iteration": iteration,
                "max_iterations": run.max_iterations,
                "progress_ratio": round(iteration / max(1, run.max_iterations), 4),
                "recent_candidates": self._recent_candidates(run_id, limit=3),
                "recent_metrics": self._recent_metrics(run_id, limit=12),
                "recent_operator_messages": recent_messages,
            },
        )
        if orchestrator_role and orchestrator_role.status == "completed" and orchestrator_role.content:
            try:
                parsed = json.loads(orchestrator_role.content.strip())
                if isinstance(parsed, dict):
                    delegation["researcher"] = bool(parsed.get("researcher", True))
                    delegation["critic"] = bool(parsed.get("critic", True))
                    delegation["strategy"] = str(parsed.get("strategy", ""))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass  # keep defaults: run all roles
        self.append_event(
            run_id,
            "orchestrator.completed",
            message=f"Orchestrator finished iteration {iteration}",
            data={
                "iteration": iteration,
                "delegation": delegation,
                "role": orchestrator_role.to_dict() if orchestrator_role else None,
            },
        )

        if delegation.get("researcher", True):
            researcher_role = self._invoke_role(
                role_name="researcher",
                run=run,
                iteration=iteration,
                context=context,
                payload={
                    "run": self._serialize_run(run),
                    "workspace": workspace.to_dict(),
                    "recent_operator_messages": recent_messages,
                    "recent_candidates": self._recent_candidates(run_id, limit=5),
                    "recent_metrics": self._recent_metrics(run_id, limit=20),
                    "plan_summary": plan_summary,
                },
            )
        else:
            researcher_role = None
        research_summary = ""
        if researcher_role and researcher_role.status == "completed" and researcher_role.content:
            research_summary = researcher_role.content.strip()
        self.append_event(
            run_id,
            "researcher.completed",
            message=f"Researcher finished iteration {iteration}",
            data={
                "iteration": iteration,
                "research_summary": research_summary,
                "role": researcher_role.to_dict() if researcher_role else None,
            },
        )
        planner_role = self._invoke_role(
            role_name="planner",
            run=run,
            iteration=iteration,
            context=context,
            payload={
                "run": self._serialize_run(run),
                "workspace": workspace.to_dict(),
                "recent_operator_messages": recent_messages,
                "has_operator_guidance": bool(recent_messages),
                "recent_candidates": self._recent_candidates(run_id, limit=3),
                "recent_metrics": self._recent_metrics(run_id, limit=12),
                "mutation_request_count": mutation_requests,
                "research_summary": research_summary,
            },
        )
        if planner_role and planner_role.status == "completed" and planner_role.content:
            plan_summary = planner_role.content.strip()
        self.append_event(
            run_id,
            "planner.completed",
            message=f"Planner finished iteration {iteration}",
            data={
                "iteration": iteration,
                "mutation_requests": mutation_requests,
                "plan_summary": plan_summary,
                "role": planner_role.to_dict() if planner_role else None,
            },
        )
        if not self._should_continue(run_id):
            return

        dataset_result = self._run_manifest_phase(
            run_id=run_id,
            iteration=iteration,
            phase="dataset",
            config=manifest.get("dataset"),
            context=context,
            workspace=workspace,
            command_keys=("build_command", "command"),
            skip_reason="no dataset command configured",
            once_per_run=bool((manifest.get("dataset") or {}).get("once", True)),
        )

        # --- Dataset snapshot reproducibility ---
        dataset_cfg = manifest.get("dataset") or {}
        if isinstance(dataset_cfg, Mapping) and dataset_cfg.get("snapshot", False):
            ds_path = dataset_cfg.get("path")
            dataset_dir = Path(ds_path) if ds_path else Path(workspace.workspace_dir) / "data"
            if dataset_dir.is_dir():
                hash_hex, file_count = self._compute_dataset_hash(dataset_dir)
                if iteration == 1:
                    # First iteration: record the baseline snapshot
                    self.add_metric(
                        run_id,
                        iteration=iteration,
                        name="dataset.snapshot_hash",
                        value=0.0,
                        data={"hash": hash_hex, "file_count": file_count, "phase": "dataset"},
                    )
                    self._emit_event(
                        run_id,
                        "dataset.snapshot",
                        {
                            "iteration": iteration,
                            "hash": hash_hex,
                            "file_count": file_count,
                            "dataset_dir": str(dataset_dir),
                        },
                    )
                else:
                    # Subsequent iterations: check for drift
                    stored_hash: str | None = None
                    for m in self.store.list_metrics(run_id):
                        if m.name == "dataset.snapshot_hash" and m.iteration == 1:
                            stored_hash = (m.metadata or {}).get("hash")
                            break
                    if stored_hash and hash_hex != stored_hash:
                        self._emit_event(
                            run_id,
                            "dataset.drift_detected",
                            {
                                "iteration": iteration,
                                "old_hash": stored_hash,
                                "new_hash": hash_hex,
                                "dataset_dir": str(dataset_dir),
                            },
                        )

        if not self._should_continue(run_id):
            return

        mutator_role = self._invoke_role(
            role_name="mutator",
            run=self._load_run(run_id),
            iteration=iteration,
            context=context,
            payload={
                "run": self._serialize_run(self._load_run(run_id)),
                "workspace": workspace.to_dict(),
                "plan_summary": plan_summary,
                "mutable_surface": list(manifest.get("mutable_surface") or []),
                "fixed_surface": list(manifest.get("fixed_surface") or []),
                "recent_operator_messages": recent_messages,
                "recent_candidates": self._recent_candidates(run_id, limit=3),
                "recent_metrics": self._recent_metrics(run_id, limit=12),
            },
        )
        mutation_audit = self._audit_mutator_changes(
            run_id=run_id,
            iteration=iteration,
            workspace=workspace,
            manifest=manifest,
            role_result=mutator_role,
        )
        if not self._should_continue(run_id):
            return

        mutation_result = self._run_manifest_phase(
            run_id=run_id,
            iteration=iteration,
            phase="mutation",
            config=manifest.get("mutation"),
            context=context,
            workspace=workspace,
            command_keys=("command", "mutate_command"),
            skip_reason="no mutation command configured",
            once_per_run=False,
        )
        if not self._should_continue(run_id):
            return

        isolate_eval = manifest.get("evaluation", {}).get("isolate", False)
        eval_workspace = workspace
        eval_workspace_dir: Optional[Path] = None

        if isolate_eval:
            eval_workspace_dir = Path(workspace.iteration_dir) / "eval_workspace"
            shutil.copytree(
                workspace.workspace_dir,
                str(eval_workspace_dir),
                dirs_exist_ok=True,
            )
            eval_workspace = WorkspaceSetup(
                source_root=workspace.source_root,
                workspace_dir=str(eval_workspace_dir),
                iteration_dir=workspace.iteration_dir,
                snapshot_enabled=workspace.snapshot_enabled,
                copied_paths=list(workspace.copied_paths),
            )
            self._emit_event(
                run_id,
                "evaluation.isolated",
                {"eval_workspace_dir": str(eval_workspace_dir)},
            )

        evaluation_result = self._run_manifest_phase(
            run_id=run_id,
            iteration=iteration,
            phase="evaluation",
            config=manifest.get("evaluation"),
            context=context,
            workspace=eval_workspace,
            command_keys=("command", "evaluate_command"),
            skip_reason="no evaluation command configured",
            once_per_run=False,
        )

        if isolate_eval and eval_workspace_dir is not None:
            if evaluation_result is not None and evaluation_result.returncode == 0:
                for artifact_name in ("metrics.json", "summary.txt"):
                    src = eval_workspace_dir / artifact_name
                    if src.exists():
                        shutil.copy2(str(src), workspace.workspace_dir)

        if not self._should_continue(run_id):
            return

        evaluation_cfg = dict(manifest.get("evaluation") or {})
        metrics_file = resolve_artifact_path(
            evaluation_cfg.get("metrics_file") or evaluation_cfg.get("metrics_path"),
            base_dir=Path(workspace.workspace_dir),
            context=context,
        )
        summary_file = resolve_artifact_path(
            evaluation_cfg.get("summary_file") or evaluation_cfg.get("candidate_summary_file"),
            base_dir=Path(workspace.workspace_dir),
            context=context,
        )
        metrics = load_metrics_file(metrics_file)
        summary_text = load_text_artifact(summary_file)
        if not metrics and evaluation_result is not None:
            metrics = {"evaluation.exit_code": float(evaluation_result.returncode)}

        promotion_cfg = dict(manifest.get("promotion") or {})
        preferred_metric = (
            str(promotion_cfg.get("metric") or "").strip()
            or str(evaluation_cfg.get("primary_metric") or "").strip()
            or None
        )
        primary_metric_name, primary_metric_value = select_primary_metric(
            metrics,
            preferred_name=preferred_metric,
        )
        higher_is_better = bool(
            promotion_cfg.get("higher_is_better")
            if "higher_is_better" in promotion_cfg
            else evaluation_cfg.get("higher_is_better", True)
        )
        best_before = self._best_metric_value(run_id, primary_metric_name, higher_is_better=higher_is_better)
        candidate_status = self._evaluate_promotion(
            evaluation_result=evaluation_result,
            primary_metric_name=primary_metric_name,
            primary_metric_value=primary_metric_value,
            best_before=best_before,
            higher_is_better=higher_is_better,
            promotion_cfg=promotion_cfg,
        )

        for metric_name, metric_value in metrics.items():
            self.add_metric(
                run_id,
                iteration=iteration,
                name=metric_name,
                value=metric_value,
                data={
                    "phase": "evaluation",
                    "source": str(metrics_file) if metrics_file else None,
                },
            )

        progress_value = round(iteration / max(1, self._load_run(run_id).max_iterations), 4)
        self.add_metric(
            run_id,
            iteration=iteration,
            name="iteration_progress",
            value=progress_value,
            data={"phase": "reporting"},
        )

        critique = None
        if delegation.get("critic", True):
            critic_role = self._invoke_role(
                role_name="critic",
                run=self._load_run(run_id),
                iteration=iteration,
                context=context,
                payload={
                    "run": self._serialize_run(self._load_run(run_id)),
                    "workspace": workspace.to_dict(),
                    "candidate_status": candidate_status,
                    "metrics": metrics,
                    "primary_metric": self._compact_dict(
                        {
                            "name": primary_metric_name,
                            "value": primary_metric_value,
                            "higher_is_better": higher_is_better if primary_metric_name else None,
                        }
                    ),
                    "plan_summary": plan_summary,
                    "mutation_audit": mutation_audit,
                    "recent_candidates": self._recent_candidates(run_id, limit=5),
                    "recent_metrics": self._recent_metrics(run_id, limit=20),
                },
            )
        else:
            critic_role = None
        if critic_role and critic_role.status == "completed" and critic_role.content:
            critique = critic_role.content.strip()
        self.append_event(
            run_id,
            "critic.completed",
            message=f"Critic finished iteration {iteration}",
            data={
                "iteration": iteration,
                "critique": critique,
                "role": critic_role.to_dict() if critic_role else None,
            },
        )

        candidate_summary = self._build_candidate_summary(
            iteration=iteration,
            primary_metric_name=primary_metric_name,
            primary_metric_value=primary_metric_value,
            evaluation_result=evaluation_result,
            summary_text=summary_text,
            plan_summary=plan_summary,
        )
        candidate_metadata = self._compact_dict(
            {
                "workspace": workspace.to_dict(),
                "plan_summary": plan_summary,
                "dataset": dataset_result.to_dict() if dataset_result else None,
                "mutator": self._compact_dict(
                    {
                        "role": mutator_role.to_dict() if mutator_role else None,
                        "audit": mutation_audit,
                    }
                ),
                "mutation": mutation_result.to_dict() if mutation_result else None,
                "evaluation": evaluation_result.to_dict() if evaluation_result else None,
                "artifacts": self._compact_dict(
                    {
                        "metrics_file": str(metrics_file) if metrics_file else None,
                        "summary_file": str(summary_file) if summary_file else None,
                    }
                ),
                "primary_metric": self._compact_dict(
                    {
                        "name": primary_metric_name,
                        "value": primary_metric_value,
                        "higher_is_better": higher_is_better if primary_metric_name else None,
                    }
                ),
                "operator_messages": recent_messages,
                "mutation_requests_seen": mutation_requests,
                "roles": self._compact_dict(
                    {
                        "orchestrator": self._compact_dict(
                            {
                                "delegation": delegation,
                                "role": orchestrator_role.to_dict() if orchestrator_role else None,
                            }
                        ),
                        "researcher": researcher_role.to_dict() if researcher_role else None,
                        "planner": planner_role.to_dict() if planner_role else None,
                        "mutator": mutator_role.to_dict() if mutator_role else None,
                        "critic": critic_role.to_dict() if critic_role else None,
                    }
                ),
            }
        )

        self.store.update_run(run_id, current_iteration=iteration, phase="reporting")
        candidate_title = self._build_candidate_title(iteration, primary_metric_name, primary_metric_value)
        candidate_preview = {
            "title": candidate_title,
            "summary": candidate_summary,
            "status": candidate_status,
            "metadata": candidate_metadata,
        }

        report_content = self._build_iteration_report(
            run=self._load_run(run_id),
            iteration=iteration,
            plan_summary=plan_summary,
            workspace=workspace,
            dataset_result=dataset_result,
            mutation_audit=mutation_audit,
            mutation_result=mutation_result,
            evaluation_result=evaluation_result,
            metrics=metrics,
            recent_messages=recent_messages,
            candidate=candidate_preview,
        )
        reporter_role = self._invoke_role(
            role_name="reporter",
            run=self._load_run(run_id),
            iteration=iteration,
            context=context,
            payload={
                "run": self._serialize_run(self._load_run(run_id)),
                "workspace": workspace.to_dict(),
                "plan_summary": plan_summary,
                "recent_operator_messages": recent_messages,
                "candidate": candidate_preview,
                "mutation_audit": mutation_audit,
                "metrics": metrics,
                "dataset": dataset_result.to_dict() if dataset_result else None,
                "mutation": mutation_result.to_dict() if mutation_result else None,
                "evaluation": evaluation_result.to_dict() if evaluation_result else None,
                "critique": critique,
            },
        )
        if reporter_role and reporter_role.status == "completed" and reporter_role.content:
            report_content = self._merge_role_report(reporter_role.content, report_content)
            roles_metadata = dict(candidate_metadata)
            roles_block = dict(roles_metadata.get("roles") or {})
            roles_block["reporter"] = reporter_role.to_dict()
            roles_metadata["roles"] = roles_block
            candidate_metadata = roles_metadata

        candidate = self.add_candidate(
            run_id,
            iteration=iteration,
            title=candidate_title,
            summary=candidate_summary,
            status=candidate_status,
            data=candidate_metadata,
        )
        if candidate_status == "promoted":
            self.append_event(
                run_id,
                "candidate.promoted",
                message=f"Candidate {iteration} is the current best candidate",
                data={
                    "iteration": iteration,
                    "candidate_id": candidate["id"],
                    "metric_name": primary_metric_name,
                    "metric_value": primary_metric_value,
                },
            )

        report = self.write_report(
            run_id,
            report_type="iteration",
            title=f"Iteration {iteration}",
            content=report_content,
            data={
                "iteration": iteration,
                "candidate_id": candidate["id"],
                "workspace": workspace.to_dict(),
                "roles": self._compact_dict(
                    {
                        "planner": planner_role.to_dict() if planner_role else None,
                        "reporter": reporter_role.to_dict() if reporter_role else None,
                    }
                ),
            },
        )

        self.store.update_run(run_id, phase="running")
        self.append_event(
            run_id,
            "iteration.completed",
            message=f"Iteration {iteration} completed",
            data={"iteration": iteration, "report_id": report["id"]},
        )

        stopping_reason = self._check_stopping_conditions(
            manifest=manifest,
            metrics=metrics,
            primary_metric_name=primary_metric_name,
            primary_metric_value=primary_metric_value,
            higher_is_better=higher_is_better,
        )
        if stopping_reason:
            self.append_event(
                run_id,
                "run.stopping_condition_met",
                message=stopping_reason,
                data={"iteration": iteration},
            )
            self._complete_run(run_id, reason=stopping_reason)
            return

        time.sleep(self._iteration_delay)

    def _run_manifest_phase(
        self,
        *,
        run_id: str,
        iteration: int,
        phase: str,
        config: Any,
        context: Dict[str, Any],
        workspace: WorkspaceSetup,
        command_keys: tuple[str, ...],
        skip_reason: str,
        once_per_run: bool,
    ) -> Optional[CommandExecutionResult]:
        if not isinstance(config, Mapping):
            self.append_event(
                run_id,
                f"{phase}.skipped",
                message=f"{phase.capitalize()} skipped: {skip_reason}",
                data={"iteration": iteration, "reason": skip_reason},
            )
            return None

        command: Any = None
        for key in command_keys:
            if config.get(key):
                command = config.get(key)
                break
        if not command:
            self.append_event(
                run_id,
                f"{phase}.skipped",
                message=f"{phase.capitalize()} skipped: {skip_reason}",
                data={"iteration": iteration, "reason": skip_reason},
            )
            return None

        if once_per_run and iteration > 1:
            self.append_event(
                run_id,
                f"{phase}.skipped",
                message=f"{phase.capitalize()} skipped after first iteration",
                data={"iteration": iteration, "reason": "once_per_run"},
            )
            return None

        phase_name = "evaluating" if phase == "evaluation" else phase
        self.store.update_run(run_id, phase=phase_name)

        run_from = str(config.get("run_from") or config.get("workspace") or "").strip().lower()
        base_dir = Path(workspace.source_root) if run_from in {"source", "project"} else Path(workspace.workspace_dir)
        cwd_relative = str(config.get("cwd") or "").strip()
        command_cwd = (base_dir / cwd_relative).resolve() if cwd_relative else base_dir.resolve()
        command_cwd.mkdir(parents=True, exist_ok=True)

        timeout_seconds = max(1, int(config.get("timeout_seconds") or self._command_timeout))
        logs_dir = Path(workspace.iteration_dir) / "logs"
        artifacts_dir = Path(workspace.iteration_dir) / "artifacts"
        log_path = logs_dir / f"{phase}.log"
        result_path = artifacts_dir / f"{phase}.json"

        self.append_event(
            run_id,
            f"{phase}.started",
            message=f"{phase.capitalize()} started",
            data={
                "iteration": iteration,
                "cwd": str(command_cwd),
                "timeout_seconds": timeout_seconds,
            },
        )

        try:
            result = execute_phase_command(
                phase=phase,
                command=command,
                cwd=command_cwd,
                context=context,
                env=config.get("env"),
                timeout_seconds=timeout_seconds,
                log_path=log_path,
                result_path=result_path,
            )
        except CommandExecutionError as exc:
            payload = {
                "iteration": iteration,
                "error": str(exc),
            }
            if exc.result is not None:
                payload["result"] = exc.result.to_dict()
            self.append_event(
                run_id,
                f"{phase}.failed",
                message=f"{phase.capitalize()} failed",
                data=payload,
            )
            raise

        self.append_event(
            run_id,
            f"{phase}.completed",
            message=f"{phase.capitalize()} completed",
            data={
                "iteration": iteration,
                "result": result.to_dict(),
            },
        )
        return result

    def _build_iteration_context(
        self,
        run: ResearchRun,
        iteration: int,
        workspace: WorkspaceSetup,
    ) -> Dict[str, Any]:
        return {
            "run_id": run.id,
            "task_id": f"{run.id}-{iteration}",
            "goal": run.goal,
            "title": run.title,
            "project_name": str((run.manifest or {}).get("name") or run.title),
            "iteration": iteration,
            "max_iterations": run.max_iterations,
            "current_iteration": run.current_iteration,
            "mutation_request_count": run.mutation_request_count,
            "run_dir": str(self.store.get_run_dir(run.id)),
            "iteration_dir": workspace.iteration_dir,
            "workspace_dir": workspace.workspace_dir,
            "project_root": workspace.source_root,
            "source_root": workspace.source_root,
            "python_executable": sys.executable,
        }

    def _build_iteration_summary(
        self,
        *,
        run: ResearchRun,
        iteration: int,
        manifest: Dict[str, Any],
        workspace: WorkspaceSetup,
        recent_messages: List[Dict[str, Any]],
        mutation_requests: int,
    ) -> str:
        phases = []
        for name in ("dataset", "mutation", "evaluation"):
            if isinstance(manifest.get(name), Mapping) and manifest.get(name):
                phases.append(name)
        workspace_note = "snapshot workspace" if workspace.snapshot_enabled else "project workspace"
        notes = []
        if recent_messages:
            notes.append(f"{len(recent_messages)} recent operator message(s)")
        if mutation_requests:
            notes.append(f"{mutation_requests} mutation request(s) logged")
        if not notes:
            notes.append("no operator guidance recorded yet")
        phase_text = ", ".join(phases) if phases else "report-only"
        return (
            f"Iteration {iteration} is executing goal '{run.goal}' using the {workspace_note}. "
            f"Configured phases: {phase_text}. Current context: {', '.join(notes)}."
        )

    def _build_candidate_title(
        self,
        iteration: int,
        primary_metric_name: Optional[str],
        primary_metric_value: Optional[float],
    ) -> str:
        if primary_metric_name and primary_metric_value is not None:
            return f"Candidate {iteration} ({primary_metric_name}={primary_metric_value:.4f})"
        return f"Candidate {iteration}"

    def _build_candidate_summary(
        self,
        *,
        iteration: int,
        primary_metric_name: Optional[str],
        primary_metric_value: Optional[float],
        evaluation_result: Optional[CommandExecutionResult],
        summary_text: str,
        plan_summary: str,
    ) -> str:
        if summary_text:
            return summary_text[:1200]
        metric_line = "no primary metric was produced"
        if primary_metric_name and primary_metric_value is not None:
            metric_line = f"{primary_metric_name}={primary_metric_value:.4f}"
        status_line = "evaluation skipped"
        if evaluation_result is not None:
            status_line = f"evaluation exited with code {evaluation_result.returncode}"
        return f"Iteration {iteration}: {metric_line}; {status_line}. {plan_summary}"

    def _build_iteration_report(
        self,
        *,
        run: ResearchRun,
        iteration: int,
        plan_summary: str,
        workspace: WorkspaceSetup,
        dataset_result: Optional[CommandExecutionResult],
        mutation_audit: Optional[Dict[str, Any]],
        mutation_result: Optional[CommandExecutionResult],
        evaluation_result: Optional[CommandExecutionResult],
        metrics: Dict[str, float],
        recent_messages: List[Dict[str, Any]],
        candidate: Dict[str, Any],
    ) -> str:
        lines = [
            f"Iteration {iteration}",
            "",
            "Plan",
            plan_summary,
            "",
            "Workspace",
            f"- source_root: {workspace.source_root}",
            f"- workspace_dir: {workspace.workspace_dir}",
            f"- snapshot_enabled: {workspace.snapshot_enabled}",
        ]
        if workspace.copied_paths:
            lines.append(f"- copied_paths: {', '.join(workspace.copied_paths)}")

        lines.extend(["", "Phases"])
        for label, result in (
            ("dataset", dataset_result),
            ("mutation", mutation_result),
            ("evaluation", evaluation_result),
        ):
            if result is None:
                lines.append(f"- {label}: skipped")
            else:
                lines.append(
                    f"- {label}: exit={result.returncode}, duration={result.duration_seconds:.3f}s, log={result.log_path}"
                )
        if mutation_audit:
            lines.extend(["", "Mutation Audit"])
            changed = mutation_audit.get("changed_paths") or []
            blocked = mutation_audit.get("blocked_paths") or []
            lines.append(f"- changed_paths: {', '.join(changed) if changed else 'none'}")
            if blocked:
                lines.append(f"- blocked_paths: {', '.join(blocked)}")
            restored = mutation_audit.get("restored_paths") or []
            if restored:
                lines.append(f"- restored_paths: {', '.join(restored)}")

        lines.extend(["", "Metrics"])
        if metrics:
            for name, value in sorted(metrics.items()):
                lines.append(f"- {name}: {value}")
        else:
            lines.append("- No metrics were produced")

        lines.extend(["", "Operator Guidance"])
        if recent_messages:
            for item in recent_messages:
                lines.append(f"- {item['author']}: {item['content']}")
        else:
            lines.append("- No recent operator messages")

        lines.extend(
            [
                "",
                "Candidate",
                f"- title: {candidate['title']}",
                f"- status: {candidate['status']}",
                f"- summary: {candidate['summary']}",
                "",
                f"Goal: {run.goal}",
            ]
        )
        return "\n".join(lines)

    def _check_stopping_conditions(
        self,
        *,
        manifest: Dict[str, Any],
        metrics: Dict[str, float],
        primary_metric_name: Optional[str],
        primary_metric_value: Optional[float],
        higher_is_better: bool,
    ) -> Optional[str]:
        stopping = manifest.get("stopping")
        if not isinstance(stopping, Mapping):
            return None

        target_metric = str(stopping.get("target_metric") or "").strip() or primary_metric_name
        target_value_raw = stopping.get("target_value")
        if target_metric and target_value_raw is not None:
            try:
                target_value = float(target_value_raw)
            except (TypeError, ValueError):
                return None
            actual = metrics.get(target_metric)
            if actual is None:
                return None
            stop_when_higher = bool(stopping.get("higher_is_better", higher_is_better))
            if stop_when_higher and actual >= target_value:
                return f"Stopping target reached: {target_metric} >= {target_value}"
            if not stop_when_higher and actual <= target_value:
                return f"Stopping target reached: {target_metric} <= {target_value}"
        return None

    @staticmethod
    def _evaluate_promotion(
        *,
        evaluation_result: Optional[CommandExecutionResult],
        primary_metric_name: Optional[str],
        primary_metric_value: Optional[float],
        best_before: Optional[float],
        higher_is_better: bool,
        promotion_cfg: Dict[str, Any],
    ) -> str:
        """Decide candidate status using manifest promotion rules.

        Supports:
        - ``threshold``: absolute value the metric must reach to promote.
        - ``min_improvement``: minimum delta over best-so-far to promote.
        - Falls back to simple ``is_better_metric`` when neither is set.
        """
        if evaluation_result is not None and evaluation_result.returncode != 0:
            return "failed"

        if not primary_metric_name or primary_metric_value is None:
            return "evaluated"

        # Threshold gate: metric must reach an absolute value to be promotable
        threshold_raw = promotion_cfg.get("threshold")
        if threshold_raw is not None:
            try:
                threshold = float(threshold_raw)
            except (TypeError, ValueError):
                threshold = None
            if threshold is not None:
                if higher_is_better and primary_metric_value < threshold:
                    return "evaluated"
                if not higher_is_better and primary_metric_value > threshold:
                    return "evaluated"

        # Must beat the previous best
        if not is_better_metric(primary_metric_value, best_before, higher_is_better=higher_is_better):
            return "evaluated"

        # Minimum improvement gate: delta over best must exceed a floor
        min_improvement_raw = promotion_cfg.get("min_improvement")
        if min_improvement_raw is not None and best_before is not None:
            try:
                min_improvement = float(min_improvement_raw)
            except (TypeError, ValueError):
                min_improvement = None
            if min_improvement is not None and min_improvement > 0:
                delta = abs(primary_metric_value - best_before)
                if delta < min_improvement:
                    return "evaluated"

        return "promoted"

    def _recent_operator_messages(self, run_id: str, *, limit: int = 3) -> List[Dict[str, Any]]:
        messages = self.store.list_operator_messages(run_id)
        results: List[Dict[str, Any]] = []
        for message in messages[-max(0, int(limit)):]:
            results.append(
                {
                    "id": message.id,
                    "author": str(message.metadata.get("author") or "operator"),
                    "scope": message.scope,
                    "content": message.content,
                    "timestamp": message.timestamp,
                }
            )
        return results

    def _best_metric_value(
        self,
        run_id: str,
        metric_name: Optional[str],
        *,
        higher_is_better: bool,
    ) -> Optional[float]:
        if not metric_name:
            return None
        values = [metric.value for metric in self.store.list_metrics(run_id) if metric.name == metric_name]
        if not values:
            return None
        return max(values) if higher_is_better else min(values)

    def _recent_candidates(self, run_id: str, *, limit: int = 3) -> List[Dict[str, Any]]:
        candidates = self.store.list_candidates(run_id)
        return [candidate.to_dict() for candidate in candidates[-max(0, int(limit)):]]

    def _recent_metrics(self, run_id: str, *, limit: int = 12) -> List[Dict[str, Any]]:
        metrics = self.store.list_metrics(run_id)
        return [metric.to_dict() for metric in metrics[-max(0, int(limit)):]]

    def _audit_mutator_changes(
        self,
        *,
        run_id: str,
        iteration: int,
        workspace: WorkspaceSetup,
        manifest: Dict[str, Any],
        role_result: Optional[RoleResult],
    ) -> Dict[str, Any]:
        if role_result is None:
            return {}

        audit_dir = Path(workspace.iteration_dir) / "artifacts" / "mutator_audit"
        mutation_audit = collect_workspace_changes(
            workspace=workspace,
            mutable_surface=list(manifest.get("mutable_surface") or []),
            audit_dir=audit_dir,
        )
        restored_paths: List[str] = []
        blocked_paths = list(mutation_audit.get("blocked_paths") or [])
        if blocked_paths:
            restored_paths = restore_workspace_paths(workspace=workspace, relative_paths=blocked_paths)
            self.append_event(
                run_id,
                "mutator.changes.reverted",
                message="Mutator attempted changes outside the mutable surface; blocked paths were restored",
                data={
                    "iteration": iteration,
                    "blocked_paths": blocked_paths,
                    "restored_paths": restored_paths,
                },
            )

        changed_count = len(mutation_audit.get("changed_paths") or [])
        allowed_count = len(mutation_audit.get("allowed_paths") or [])
        blocked_count = len(blocked_paths)
        self.add_metric(
            run_id,
            iteration=iteration,
            name="mutation.changed_files",
            value=float(changed_count),
            data={"phase": "mutator"},
        )
        self.add_metric(
            run_id,
            iteration=iteration,
            name="mutation.allowed_files",
            value=float(allowed_count),
            data={"phase": "mutator"},
        )
        if blocked_count:
            self.add_metric(
                run_id,
                iteration=iteration,
                name="mutation.blocked_files",
                value=float(blocked_count),
                data={"phase": "mutator"},
            )

        role_payload = role_result.to_dict()
        mutation_audit.update(
            {
                "role": role_payload,
                "restored_paths": restored_paths,
            }
        )
        # Build per-change summaries with truncated diff previews for SSE consumers
        change_summaries: List[Dict[str, Any]] = []
        for change in mutation_audit.get("changes") or []:
            summary: Dict[str, Any] = {
                "path": change.get("path", ""),
                "status": change.get("status", ""),
                "allowed": change.get("allowed", False),
            }
            diff_path = change.get("diff_path")
            if diff_path:
                try:
                    diff_text = Path(diff_path).read_text(encoding="utf-8", errors="replace")
                    summary["diff_preview"] = diff_text[:2000]
                    summary["diff_truncated"] = len(diff_text) > 2000
                except OSError:
                    pass
            change_summaries.append(summary)

        self.append_event(
            run_id,
            "mutator.audit.completed",
            message="Mutator audit completed",
            data={
                "iteration": iteration,
                "changed_paths": mutation_audit.get("changed_paths") or [],
                "allowed_paths": mutation_audit.get("allowed_paths") or [],
                "blocked_paths": blocked_paths,
                "restored_paths": restored_paths,
                "changed_files": changed_count,
                "allowed_files": allowed_count,
                "blocked_files": blocked_count,
                "changes": change_summaries,
                "mutator_summary": (role_result.content or "")[:500],
            },
        )
        return mutation_audit

    def _compute_dataset_hash(self, dataset_dir: Path) -> tuple[str, int]:
        """Compute a combined SHA-256 hash of all files in *dataset_dir*.

        Files are processed in sorted relative-path order for determinism.
        Returns ``(hex_digest, file_count)``.
        """
        hasher = hashlib.sha256()
        file_count = 0
        if dataset_dir.is_dir():
            for file_path in sorted(dataset_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(dataset_dir).as_posix()
                hasher.update(rel.encode("utf-8"))
                try:
                    with open(file_path, "rb") as fh:
                        for chunk in iter(lambda: fh.read(8192), b""):
                            hasher.update(chunk)
                except OSError:
                    pass
                file_count += 1
        return hasher.hexdigest(), file_count

    def _invoke_role(
        self,
        *,
        role_name: str,
        run: ResearchRun,
        iteration: int,
        context: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> Optional[RoleResult]:
        roles_cfg = (run.manifest or {}).get("roles")
        if not isinstance(roles_cfg, Mapping) or role_name not in roles_cfg:
            return None

        self.append_event(
            run.id,
            f"{role_name}.agent.started",
            message=f"Hermes {role_name} role started",
            data={"iteration": iteration},
        )
        result = self.role_runner.run_role(
            role_name=role_name,
            run=run,
            iteration=iteration,
            context=context,
            payload=payload,
        )
        if result.status == "disabled":
            self.append_event(
                run.id,
                f"{role_name}.agent.skipped",
                message=f"Hermes {role_name} role skipped",
                data={"iteration": iteration},
            )
            return None
        if result.status == "failed":
            self.append_event(
                run.id,
                f"{role_name}.agent.failed",
                message=f"Hermes {role_name} role failed",
                data={
                    "iteration": iteration,
                    "error": result.error,
                    "role": result.to_dict(),
                },
            )
            self.append_event(
                run.id,
                f"role.{role_name}.completed",
                message=f"Role '{role_name}' finished with status '{result.status}'",
                data={
                    "iteration": iteration,
                    "role": role_name,
                    "status": result.status,
                    "content_preview": (result.content or "")[:500],
                    "model": result.model,
                    "usage": result.usage,
                    "started_at": result.started_at,
                    "completed_at": result.completed_at,
                    "error": result.error,
                },
            )
            return result
        self.append_event(
            run.id,
            f"{role_name}.agent.completed",
            message=f"Hermes {role_name} role completed",
            data={
                "iteration": iteration,
                "role": result.to_dict(),
                "preview": result.content[:300],
            },
        )
        self.append_event(
            run.id,
            f"role.{role_name}.completed",
            message=f"Role '{role_name}' finished with status '{result.status}'",
            data={
                "iteration": iteration,
                "role": role_name,
                "status": result.status,
                "content_preview": (result.content or "")[:500],
                "model": result.model,
                "usage": result.usage,
                "started_at": result.started_at,
                "completed_at": result.completed_at,
                "error": result.error,
            },
        )
        return result

    @staticmethod
    def _merge_role_report(role_content: str, fallback_report: str) -> str:
        content = str(role_content or "").strip()
        if not content:
            return fallback_report
        return "\n\n".join([content, "---", fallback_report])

    def _should_continue(self, run_id: str) -> bool:
        run = self._load_run(run_id)
        return run.status == "running"

    def _complete_run(self, run_id: str, *, reason: str = "") -> None:
        run = self._load_run(run_id)
        if run.status == "completed":
            return
        run = self.store.update_run(run_id, status="completed", phase="completed")
        self.write_report(
            run_id,
            report_type="final",
            title="Run completed",
            content=self._build_final_report(run_id, reason=reason),
            data={"current_iteration": run.current_iteration, "reason": reason or "max_iterations"},
        )
        self.append_event(
            run_id,
            "run.completed",
            message="Research run completed",
            data={"iterations": run.current_iteration, "reason": reason or "max_iterations"},
        )

    def _build_final_report(self, run_id: str, *, reason: str = "") -> str:
        run = self._load_run(run_id)
        candidates = self.store.list_candidates(run_id)
        metrics = self.store.list_metrics(run_id)
        lines = [
            f"Run completed after {run.current_iteration} iteration(s).",
            f"Goal: {run.goal}",
        ]
        if reason:
            lines.append(f"Reason: {reason}")
        if candidates:
            latest = candidates[-1]
            lines.extend(
                [
                    "",
                    "Latest candidate",
                    f"- title: {latest.title}",
                    f"- status: {latest.status}",
                    f"- summary: {latest.summary}",
                ]
            )
        if metrics:
            lines.extend(["", "Latest metrics"])
            latest_iteration = max(metric.iteration for metric in metrics)
            for metric in sorted((item for item in metrics if item.iteration == latest_iteration), key=lambda item: item.name):
                lines.append(f"- {metric.name}: {metric.value}")
        return "\n".join(lines)

    def _fail_run(self, run_id: str, exc: Exception) -> None:
        try:
            self.store.update_run(
                run_id,
                status="failed",
                phase="failed",
                last_error=str(exc),
                updated_at=_utcnow_iso(),
            )
            self.append_event(
                run_id,
                "run.failed",
                message="Research run failed",
                data={"error": str(exc)},
            )
        except Exception:
            return

    def _emit_event(self, run_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        event = self.store.append_event(run_id, event_type, payload)
        data = self._serialize_event(event)
        with self._condition:
            self._condition.notify_all()
        return data

    def _load_run(self, run_id: str) -> ResearchRun:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return run

    def _require_run(self, run_id: str) -> None:
        self._load_run(run_id)

    @staticmethod
    def _serialize_run(run: ResearchRun, include_manifest: bool = True) -> Dict[str, Any]:
        data = run.to_dict()
        data["name"] = run.title
        data["project_name"] = None
        if isinstance(run.manifest, dict):
            data["project_name"] = run.manifest.get("name")
        data["requested_mutation_count"] = run.mutation_request_count
        data["control"] = {
            "last_operator_message_at": run.last_operator_message_at,
        }
        if not include_manifest:
            data.pop("manifest", None)
        return data

    @staticmethod
    def _serialize_event(event: ResearchEvent) -> Dict[str, Any]:
        payload = dict(event.payload)
        return {
            "id": event.id,
            "run_id": event.run_id,
            "seq": event.sequence,
            "type": event.type,
            "event_type": event.type,
            "timestamp": event.timestamp,
            "payload": payload,
            "data": payload,
            "message": str(payload.get("message") or ""),
        }

    @staticmethod
    def _serialize_report(report: ResearchReport) -> Dict[str, Any]:
        return report.to_dict()

    @staticmethod
    def _serialize_candidate(candidate: ResearchCandidate) -> Dict[str, Any]:
        return candidate.to_dict()

    @staticmethod
    def _serialize_metric(metric: ResearchMetric) -> Dict[str, Any]:
        return metric.to_dict()

    @staticmethod
    def _build_run_brief(run: ResearchRun) -> str:
        lines = [
            f"Title: {run.title}",
            f"Goal: {run.goal}",
            f"Max iterations: {run.max_iterations}",
        ]
        if run.notes:
            lines.extend(["", "Notes:", run.notes])
        if run.manifest:
            lines.extend(["", "Manifest:", str(run.manifest)])
        return "\n".join(lines)

    @staticmethod
    def _resolve_manifest(manifest_path: Optional[str], manifest: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if manifest is not None:
            return dict(manifest)
        if not manifest_path:
            return None
        return load_manifest(Path(manifest_path)).to_dict()

    @staticmethod
    def _resolve_max_iterations(
        *,
        max_iterations: Optional[int],
        manifest: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
    ) -> int:
        if max_iterations is not None:
            return max(1, int(max_iterations))

        if isinstance(metadata, dict) and metadata.get("max_iterations") is not None:
            return max(1, int(metadata["max_iterations"]))

        if isinstance(manifest, dict):
            stopping = manifest.get("stopping")
            if isinstance(stopping, dict) and stopping.get("max_iterations") is not None:
                return max(1, int(stopping["max_iterations"]))
            if manifest.get("max_iterations") is not None:
                return max(1, int(manifest["max_iterations"]))

        return 25

    @staticmethod
    def _compact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in data.items()
            if value not in (None, "", [], {}, ())
        }
