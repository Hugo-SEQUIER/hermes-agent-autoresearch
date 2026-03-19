"""File-backed storage for AutoResearch runs."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from autoresearch.models import (
    ResearchCandidate,
    OperatorMessage,
    ResearchEvent,
    ResearchMetric,
    ResearchReport,
    ResearchRun,
    utc_now_iso,
)


def _atomic_json_write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
    Path(tmp_path).replace(path)


class AutoResearchStore:
    """Persist research runs as JSON and JSONL under HERMES_HOME."""

    def __init__(self, root: Optional[Path] = None):
        default_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
        default_root = Path(os.getenv("AUTORESEARCH_HOME", default_home / "autoresearch" / "runs"))
        self.root = Path(root) if root else default_root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def list_runs(self) -> List[ResearchRun]:
        """Return all known runs sorted by most recent update."""
        runs: List[ResearchRun] = []
        with self._lock:
            for run_dir in sorted(self.root.iterdir(), reverse=True):
                if not run_dir.is_dir():
                    continue
                run_path = run_dir / "run.json"
                if not run_path.exists():
                    continue
                try:
                    runs.append(ResearchRun.from_dict(self._read_json(run_path)))
                except Exception:
                    continue
        runs.sort(key=lambda item: item.updated_at, reverse=True)
        return runs

    def create_run(
        self,
        *,
        title: str,
        goal: str,
        notes: str = "",
        manifest: Optional[Dict[str, Any]] = None,
        max_iterations: int = 25,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ResearchRun:
        """Create and persist a new run."""
        run_id = uuid.uuid4().hex
        now = utc_now_iso()
        run = ResearchRun(
            id=run_id,
            title=title.strip() or goal.strip()[:80] or "Untitled research run",
            goal=goal.strip(),
            notes=notes.strip(),
            max_iterations=max(1, int(max_iterations or 25)),
            created_at=now,
            updated_at=now,
            manifest=manifest or None,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._write_run(run)
        return run

    def get_run(self, run_id: str) -> Optional[ResearchRun]:
        """Load a run by ID."""
        run_path = self._run_file(run_id)
        with self._lock:
            if not run_path.exists():
                return None
            return ResearchRun.from_dict(self._read_json(run_path))

    def require_run(self, run_id: str) -> ResearchRun:
        """Load a run or raise KeyError."""
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def update_run(self, run_id: str, **changes: Any) -> ResearchRun:
        """Apply field updates to a run and persist them."""
        with self._lock:
            run = self.require_run(run_id)
            for key, value in changes.items():
                if hasattr(run, key):
                    setattr(run, key, value)
            run.updated_at = utc_now_iso()
            self._write_run(run)
            return run

    def append_event(self, run_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> ResearchEvent:
        """Append an event to the run's event log."""
        with self._lock:
            run = self.require_run(run_id)
            event = ResearchEvent(
                id=uuid.uuid4().hex,
                run_id=run_id,
                sequence=run.event_count + 1,
                type=event_type,
                payload=dict(payload or {}),
            )
            self._append_jsonl(self._events_file(run_id), event.to_dict())
            run.event_count = event.sequence
            run.updated_at = event.timestamp
            self._write_run(run)
            return event

    def list_events(self, run_id: str, *, after_sequence: int = 0) -> List[ResearchEvent]:
        """Return run events after the given sequence number."""
        events_path = self._events_file(run_id)
        with self._lock:
            if not events_path.exists():
                return []
            results: List[ResearchEvent] = []
            with open(events_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if int(data.get("sequence", 0)) <= after_sequence:
                        continue
                    results.append(ResearchEvent.from_dict(data))
            return results

    def append_operator_message(
        self,
        run_id: str,
        *,
        content: str,
        scope: str = "run",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OperatorMessage:
        """Append an operator message to the run."""
        with self._lock:
            run = self.require_run(run_id)
            message = OperatorMessage(
                id=uuid.uuid4().hex,
                run_id=run_id,
                content=content.strip(),
                scope=scope.strip() or "run",
                metadata=dict(metadata or {}),
            )
            self._append_jsonl(self._messages_file(run_id), message.to_dict())
            run.operator_message_count += 1
            run.updated_at = message.timestamp
            run.last_operator_message_at = message.timestamp
            self._write_run(run)
            return message

    def list_operator_messages(self, run_id: str) -> List[OperatorMessage]:
        """Return operator-authored messages for a run."""
        messages_path = self._messages_file(run_id)
        with self._lock:
            if not messages_path.exists():
                return []
            results: List[OperatorMessage] = []
            with open(messages_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    results.append(OperatorMessage.from_dict(json.loads(line)))
            return results

    def write_report(
        self,
        run_id: str,
        *,
        kind: str,
        title: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ResearchReport:
        """Persist a report snapshot for the run."""
        with self._lock:
            run = self.require_run(run_id)
            report = ResearchReport(
                id=uuid.uuid4().hex,
                run_id=run_id,
                kind=kind.strip() or "report",
                title=title.strip() or "Report",
                content=content,
                metadata=dict(metadata or {}),
            )
            _atomic_json_write(self._report_file(run_id, report.id), report.to_dict())
            run.report_count += 1
            run.updated_at = report.created_at
            self._write_run(run)
            return report

    def list_reports(self, run_id: str) -> List[ResearchReport]:
        """Return all report snapshots for a run."""
        reports_dir = self._reports_dir(run_id)
        with self._lock:
            if not reports_dir.exists():
                return []
            reports: List[ResearchReport] = []
            for path in sorted(reports_dir.glob("*.json")):
                try:
                    reports.append(ResearchReport.from_dict(self._read_json(path)))
                except Exception:
                    continue
            reports.sort(key=lambda item: item.created_at)
            return reports

    def append_candidate(
        self,
        run_id: str,
        *,
        iteration: int,
        title: str,
        summary: str,
        status: str = "evaluated",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ResearchCandidate:
        """Persist a candidate snapshot for an iteration."""
        with self._lock:
            self.require_run(run_id)
            candidate = ResearchCandidate(
                id=uuid.uuid4().hex,
                run_id=run_id,
                iteration=max(0, int(iteration)),
                title=title.strip() or f"Candidate {iteration}",
                summary=summary.strip(),
                status=status.strip() or "evaluated",
                metadata=dict(metadata or {}),
            )
            self._append_jsonl(self._candidates_file(run_id), candidate.to_dict())
            return candidate

    def list_candidates(self, run_id: str) -> List[ResearchCandidate]:
        """Return candidate snapshots for a run."""
        candidates_path = self._candidates_file(run_id)
        with self._lock:
            if not candidates_path.exists():
                return []
            results: List[ResearchCandidate] = []
            with open(candidates_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    results.append(ResearchCandidate.from_dict(json.loads(line)))
            return results

    def append_metric(
        self,
        run_id: str,
        *,
        iteration: int,
        name: str,
        value: float,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ResearchMetric:
        """Persist a metric point for a run iteration."""
        with self._lock:
            self.require_run(run_id)
            metric = ResearchMetric(
                id=uuid.uuid4().hex,
                run_id=run_id,
                iteration=max(0, int(iteration)),
                name=name.strip() or "metric",
                value=float(value),
                metadata=dict(metadata or {}),
            )
            self._append_jsonl(self._metrics_file(run_id), metric.to_dict())
            return metric

    def list_metrics(self, run_id: str) -> List[ResearchMetric]:
        """Return metric points for a run."""
        metrics_path = self._metrics_file(run_id)
        with self._lock:
            if not metrics_path.exists():
                return []
            results: List[ResearchMetric] = []
            with open(metrics_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    results.append(ResearchMetric.from_dict(json.loads(line)))
            return results

    def increment_mutation_requests(self, run_id: str, amount: int = 1) -> ResearchRun:
        """Increment the mutation request counter on a run."""
        with self._lock:
            run = self.require_run(run_id)
            run.mutation_request_count += max(1, int(amount or 1))
            run.updated_at = utc_now_iso()
            self._write_run(run)
            return run

    def get_run_dir(self, run_id: str) -> Path:
        """Return the run directory, creating it if needed."""
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def get_iteration_dir(self, run_id: str, iteration: int) -> Path:
        """Return a stable directory for a run iteration."""
        path = self.get_run_dir(run_id) / "iterations" / f"{max(0, int(iteration)):04d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def list_iteration_numbers(self, run_id: str) -> List[int]:
        """Return sorted iteration numbers that have directories on disk."""
        iterations_dir = self._run_dir(run_id) / "iterations"
        if not iterations_dir.exists():
            return []
        numbers: List[int] = []
        for entry in sorted(iterations_dir.iterdir()):
            if entry.is_dir():
                try:
                    numbers.append(int(entry.name))
                except ValueError:
                    continue
        return numbers

    def load_mutation_audit_diffs(self, run_id: str, iteration: int) -> Dict[str, str]:
        """Read all diff files from an iteration's mutator_audit directory.

        Returns a mapping of ``{relative_path: diff_content}``.
        """
        audit_dir = (
            self._run_dir(run_id)
            / "iterations"
            / f"{max(0, int(iteration)):04d}"
            / "artifacts"
            / "mutator_audit"
            / "diffs"
        )
        if not audit_dir.exists():
            return {}
        diffs: Dict[str, str] = {}
        with self._lock:
            for diff_file in sorted(audit_dir.rglob("*.diff")):
                relative_key = diff_file.relative_to(audit_dir).as_posix()
                # Strip the .diff suffix to get the original relative path
                if relative_key.endswith(".diff"):
                    relative_key = relative_key[: -len(".diff")]
                try:
                    diffs[relative_key] = diff_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
        return diffs

    def _run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def _run_file(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "run.json"

    def _events_file(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "events.jsonl"

    def _messages_file(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "operator_messages.jsonl"

    def _reports_dir(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "reports"

    def _report_file(self, run_id: str, report_id: str) -> Path:
        return self._reports_dir(run_id) / f"{report_id}.json"

    def _candidates_file(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "candidates.jsonl"

    def _metrics_file(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "metrics.jsonl"

    def _write_run(self, run: ResearchRun) -> None:
        _atomic_json_write(self._run_file(run.id), run.to_dict())

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def _append_jsonl(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(data, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
