"""Tests for the AutoResearch manifest loader and file-backed run manager."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from autoresearch.hermes_roles import RoleResult
from autoresearch.manifests import ProjectManifest, load_manifest
from autoresearch.runtime import AutoResearchManager, InvalidRunStateError


def _write_demo_project(tmp_path):
    project_dir = tmp_path / "demo-project"
    project_dir.mkdir()

    (project_dir / "train.py").write_text("ITERATION = 0\n", encoding="utf-8")
    (project_dir / "prepare.py").write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "from pathlib import Path",
                "",
                "counter_path = Path(os.environ['COUNTER_FILE'])",
                "count = int(counter_path.read_text(encoding='utf-8')) if counter_path.exists() else 0",
                "counter_path.write_text(str(count + 1), encoding='utf-8')",
                "artifacts = Path('artifacts')",
                "artifacts.mkdir(parents=True, exist_ok=True)",
                "(artifacts / 'dataset.json').write_text(json.dumps({'count': count + 1}), encoding='utf-8')",
                "print(f'dataset build {count + 1}')",
            ]
        ),
        encoding="utf-8",
    )
    (project_dir / "mutate.py").write_text(
        "\n".join(
            [
                "import argparse",
                "from pathlib import Path",
                "",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--iteration', type=int, required=True)",
                "args = parser.parse_args()",
                "artifacts = Path('artifacts')",
                "artifacts.mkdir(parents=True, exist_ok=True)",
                "Path('train.py').write_text(f'ITERATION = {args.iteration}\\n', encoding='utf-8')",
                "(artifacts / 'mutation.txt').write_text(f'mutated {args.iteration}', encoding='utf-8')",
                "print(f'mutation {args.iteration}')",
            ]
        ),
        encoding="utf-8",
    )
    (project_dir / "evaluate.py").write_text(
        "\n".join(
            [
                "import argparse",
                "import json",
                "from pathlib import Path",
                "",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--iteration', type=int, required=True)",
                "args = parser.parse_args()",
                "score = float(args.iteration)",
                "robustness = float(10 - args.iteration)",
                "artifacts = Path('artifacts')",
                "artifacts.mkdir(parents=True, exist_ok=True)",
                "(artifacts / 'metrics.json').write_text(json.dumps({'score': score, 'robustness': robustness}), encoding='utf-8')",
                "(artifacts / 'summary.txt').write_text(f'Iteration {args.iteration} summary with score {score}', encoding='utf-8')",
                "print(f'evaluation {args.iteration}')",
            ]
        ),
        encoding="utf-8",
    )

    return project_dir


def _build_manifest(project_dir, counter_file, *, max_iterations=2, target_value=None):
    stopping = {"max_iterations": max_iterations}
    if target_value is not None:
        stopping.update({"target_metric": "score", "target_value": target_value})

    return {
        "name": "demo-project",
        "objective": "Exercise manifest-driven AutoResearch execution",
        "workspace": {
            "root": str(project_dir),
            "mode": "snapshot",
        },
        "fixed_surface": ["prepare.py", "evaluate.py"],
        "mutable_surface": ["mutate.py", "train.py"],
        "dataset": {
            "build_command": [sys.executable, "prepare.py"],
            "once": True,
            "env": {"COUNTER_FILE": str(counter_file)},
        },
        "mutation": {
            "command": [sys.executable, "mutate.py", "--iteration", "{iteration}"],
        },
        "evaluation": {
            "command": [sys.executable, "evaluate.py", "--iteration", "{iteration}"],
            "metrics_file": "artifacts/metrics.json",
            "summary_file": "artifacts/summary.txt",
            "primary_metric": "score",
            "higher_is_better": True,
        },
        "stopping": stopping,
    }


class _FakeRoleRunner:
    def __init__(self):
        self.calls = []

    def run_role(self, *, role_name, run, iteration, context, payload):
        self.calls.append({"role_name": role_name, "iteration": iteration, "payload": payload})
        return RoleResult(
            role=role_name,
            status="completed",
            content=f"{role_name.title()} role output for iteration {iteration}",
            model="fake-hermes",
            completed_at="2026-01-01T00:00:00+00:00",
        )


class _FakeMutatorRoleRunner(_FakeRoleRunner):
    def run_role(self, *, role_name, run, iteration, context, payload):
        if role_name == "mutator":
            workspace_dir = Path(context["workspace_dir"])
            (workspace_dir / "train.py").write_text(
                f"ITERATION = {iteration}\nMUTATED = True\n",
                encoding="utf-8",
            )
            # This edit should be reverted by the runtime because evaluate.py is fixed surface.
            (workspace_dir / "evaluate.py").write_text(
                "BROKEN = True\n",
                encoding="utf-8",
            )
            return RoleResult(
                role=role_name,
                status="completed",
                content=f"Mutated train.py during iteration {iteration}",
                model="fake-hermes",
                completed_at="2026-01-01T00:00:00+00:00",
            )
        return super().run_role(
            role_name=role_name,
            run=run,
            iteration=iteration,
            context=context,
            payload=payload,
        )


def test_load_manifest_from_json(tmp_path):
    manifest_path = tmp_path / "project.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "demo-project",
                "objective": "Improve candidate quality",
                "workspace": {"root": "."},
                "fixed_surface": ["evaluate.py", "splits.py"],
                "mutable_surface": ["train.py"],
                "dataset": {"builder": "prepare.py"},
                "roles": {"planner": {"enabled": True}},
                "custom_hook": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    manifest = load_manifest(manifest_path)

    assert isinstance(manifest, ProjectManifest)
    assert manifest.name == "demo-project"
    assert manifest.fixed_surface == ["evaluate.py", "splits.py"]
    assert manifest.mutable_surface == ["train.py"]
    assert manifest.roles == {"planner": {"enabled": True}}
    assert manifest.extra == {"custom_hook": {"enabled": True}}
    assert manifest.source_path == str(manifest_path.resolve())


def test_create_run_persists_manifest_and_events(tmp_path):
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")

    run = manager.create_run(
        name="Generic run",
        goal="Find a stronger baseline",
        manifest={
            "name": "reusable-project",
            "objective": "Find a stronger baseline",
            "fixed_surface": ["evaluate.py"],
            "mutable_surface": ["train.py"],
        },
        metadata={"owner": "test"},
    )

    assert run["status"] == "created"
    assert run["manifest"]["name"] == "reusable-project"
    assert run["metadata"] == {"owner": "test"}

    events = manager.list_events(run["id"])
    assert [item["event_type"] for item in events] == ["run.created", "report.written"]

    listed = manager.list_runs()
    assert listed[0]["id"] == run["id"]
    assert "manifest" not in listed[0]


def test_control_actions_and_report_tracking(tmp_path):
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")
    manager._iteration_delay = 0.25
    run = manager.create_run(name="Control", goal="Control loop", autostart=False)

    resumed = manager.resume_run(run["id"])
    assert resumed["status"] == "running"

    paused = manager.pause_run(run["id"])
    assert paused["status"] == "paused"

    event = manager.add_operator_message(run["id"], content="Focus on validation", scope="run")
    assert event["event_type"] == "operator.message"

    mutation = manager.request_mutation(run["id"], reason="Need a fresh candidate")
    assert mutation["event_type"] == "mutation.requested"

    report = manager.write_report(
        run["id"],
        report_type="iteration",
        title="Iteration 1",
        content="Initial control-plane report",
    )
    assert report["kind"] == "iteration"

    reports = manager.list_reports(run["id"])
    assert any(item["title"] == "Iteration 1" for item in reports)

    stopped = manager.stop_run(run["id"])
    assert stopped["status"] == "stopped"


def test_wait_for_events_observes_new_event(tmp_path):
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")
    run = manager.create_run(name="Watcher", goal="Observe events")

    def _emit():
        time.sleep(0.05)
        manager.append_event(run["id"], "planner.note", message="Planner emitted an event")

    thread = threading.Thread(target=_emit, daemon=True)
    thread.start()

    assert manager.wait_for_events(run["id"], after_seq=1, timeout=1.0) is True
    events = manager.list_events(run["id"], after_seq=1)
    assert events[-1]["event_type"] == "planner.note"


def test_pause_requires_running_state(tmp_path):
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")
    run = manager.create_run(name="Invalid pause", goal="Pause validation")

    with pytest.raises(InvalidRunStateError):
        manager.pause_run(run["id"])


def test_manifest_execution_generates_real_artifacts_and_metrics(tmp_path):
    project_dir = _write_demo_project(tmp_path)
    counter_file = tmp_path / "dataset-counter.txt"
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")

    run = manager.create_run(
        name="Manifest worker",
        manifest=_build_manifest(project_dir, counter_file, max_iterations=5, target_value=2.0),
        autostart=True,
    )

    completed = manager.wait_for_status(run["id"], ["completed"], timeout=5.0)
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["current_iteration"] == 2

    candidates = manager.list_candidates(run["id"])
    metrics = manager.list_metrics(run["id"])
    reports = manager.list_reports(run["id"])
    events = manager.list_events(run["id"])

    assert counter_file.read_text(encoding="utf-8") == "1"
    assert len(candidates) == 2
    assert candidates[-1]["status"] == "promoted"
    assert candidates[-1]["metadata"]["workspace"]["snapshot_enabled"] is True
    assert "score" == candidates[-1]["metadata"]["primary_metric"]["name"]
    assert candidates[-1]["metadata"]["primary_metric"]["value"] == 2.0
    assert Path(candidates[-1]["metadata"]["evaluation"]["log_path"]).exists()
    assert Path(candidates[-1]["metadata"]["artifacts"]["metrics_file"]).exists()
    assert any(metric["name"] == "score" for metric in metrics)
    assert sum(1 for metric in metrics if metric["name"] == "iteration_progress") == 2
    assert any(report["kind"] == "final" for report in reports)
    assert any(event["event_type"] == "run.stopping_condition_met" for event in events)


def test_manifest_roles_enrich_plan_and_report(tmp_path):
    project_dir = _write_demo_project(tmp_path)
    counter_file = tmp_path / "dataset-counter.txt"
    fake_roles = _FakeRoleRunner()
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch", role_runner=fake_roles)

    manifest = _build_manifest(project_dir, counter_file, max_iterations=1)
    manifest["roles"] = {
        "planner": {"enabled": True},
        "reporter": {"enabled": True},
    }

    run = manager.create_run(
        name="Role worker",
        manifest=manifest,
        autostart=True,
    )

    completed = manager.wait_for_status(run["id"], ["completed"], timeout=5.0)
    assert completed is not None
    assert completed["status"] == "completed"

    candidates = manager.list_candidates(run["id"])
    reports = manager.list_reports(run["id"])
    events = manager.list_events(run["id"])

    assert [call["role_name"] for call in fake_roles.calls] == ["planner", "reporter"]
    assert candidates[0]["metadata"]["roles"]["planner"]["content"] == "Planner role output for iteration 1"
    assert candidates[0]["metadata"]["roles"]["reporter"]["content"] == "Reporter role output for iteration 1"
    assert any(
        report["kind"] == "iteration" and "Reporter role output for iteration 1" in report["content"]
        for report in reports
    )
    assert any(event["event_type"] == "planner.agent.completed" for event in events)
    assert any(event["event_type"] == "reporter.agent.completed" for event in events)


def test_mutator_role_audits_and_reverts_fixed_surface_changes(tmp_path):
    project_dir = _write_demo_project(tmp_path)
    counter_file = tmp_path / "dataset-counter.txt"
    fake_roles = _FakeMutatorRoleRunner()
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch", role_runner=fake_roles)

    manifest = _build_manifest(project_dir, counter_file, max_iterations=1)
    manifest["roles"] = {"mutator": {"enabled": True}}
    manifest.pop("mutation")

    run = manager.create_run(
        name="Mutator worker",
        manifest=manifest,
        autostart=True,
    )

    completed = manager.wait_for_status(run["id"], ["completed"], timeout=5.0)
    assert completed is not None
    assert completed["status"] == "completed"

    candidates = manager.list_candidates(run["id"])
    events = manager.list_events(run["id"])
    candidate = candidates[0]
    mutator_meta = candidate["metadata"]["mutator"]
    workspace_dir = Path(candidate["metadata"]["workspace"]["workspace_dir"])

    assert mutator_meta["role"]["content"] == "Mutated train.py during iteration 1"
    assert "train.py" in mutator_meta["audit"]["allowed_paths"]
    assert "evaluate.py" in mutator_meta["audit"]["blocked_paths"]
    assert "evaluate.py" in mutator_meta["audit"]["restored_paths"]
    assert "MUTATED = True" in (workspace_dir / "train.py").read_text(encoding="utf-8")
    assert "BROKEN = True" not in (workspace_dir / "evaluate.py").read_text(encoding="utf-8")
    assert any(event["event_type"] == "mutator.audit.completed" for event in events)
    assert any(event["event_type"] == "mutator.changes.reverted" for event in events)


def test_autostart_worker_completes_and_generates_default_progress_metric(tmp_path):
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")
    run = manager.create_run(
        name="Worker demo",
        goal="Exercise background loop",
        autostart=True,
        max_iterations=2,
    )

    completed = manager.wait_for_status(run["id"], ["completed"], timeout=3.0)
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["current_iteration"] == 2

    candidates = manager.list_candidates(run["id"])
    metrics = manager.list_metrics(run["id"])
    reports = manager.list_reports(run["id"])
    events = manager.list_events(run["id"])

    assert len(candidates) == 2
    assert len(metrics) == 2
    assert all(metric["name"] == "iteration_progress" for metric in metrics)
    assert any(report["kind"] == "final" for report in reports)
    assert any(event["event_type"] == "run.completed" for event in events)


def test_pause_resume_and_stop_worker_lifecycle(tmp_path):
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")
    manager._iteration_delay = 0.2
    run = manager.create_run(
        name="Worker control",
        goal="Control lifecycle",
        autostart=False,
        max_iterations=10,
    )

    resumed = manager.resume_run(run["id"])
    assert resumed["status"] == "running"

    paused = manager.pause_run(run["id"])
    assert paused["status"] == "paused"

    resumed = manager.resume_run(run["id"])
    assert resumed["status"] == "running"

    stopped = manager.stop_run(run["id"])
    assert stopped["status"] == "stopped"
    final = manager.wait_for_status(run["id"], ["stopped"], timeout=1.0)
    assert final is not None


def test_list_iterations_returns_per_iteration_summaries(tmp_path):
    project_dir = _write_demo_project(tmp_path)
    counter_file = tmp_path / "dataset-counter.txt"
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")

    manifest = _build_manifest(project_dir, counter_file, max_iterations=2)
    run = manager.create_run(name="Iterations", manifest=manifest, autostart=True)
    completed = manager.wait_for_status(run["id"], ["completed"], timeout=5.0)
    assert completed is not None

    iterations = manager.list_iterations(run["id"])
    assert len(iterations) == 2
    assert iterations[0]["iteration"] == 1
    assert iterations[1]["iteration"] == 2
    assert "candidate_id" in iterations[0]
    assert "primary_metric" in iterations[0]
    assert iterations[0]["primary_metric"]["name"] == "score"


def test_list_iterations_includes_mutation_audit_flags(tmp_path):
    project_dir = _write_demo_project(tmp_path)
    counter_file = tmp_path / "dataset-counter.txt"
    fake_roles = _FakeMutatorRoleRunner()
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch", role_runner=fake_roles)

    manifest = _build_manifest(project_dir, counter_file, max_iterations=1)
    manifest["roles"] = {"mutator": {"enabled": True}}
    manifest.pop("mutation")

    run = manager.create_run(name="Mutation iter", manifest=manifest, autostart=True)
    completed = manager.wait_for_status(run["id"], ["completed"], timeout=5.0)
    assert completed is not None

    iterations = manager.list_iterations(run["id"])
    assert len(iterations) == 1
    entry = iterations[0]
    assert entry["has_mutation_audit"] is True
    assert entry["mutation_changed_files"] > 0


def test_get_mutation_audit_returns_diffs_and_paths(tmp_path):
    project_dir = _write_demo_project(tmp_path)
    counter_file = tmp_path / "dataset-counter.txt"
    fake_roles = _FakeMutatorRoleRunner()
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch", role_runner=fake_roles)

    manifest = _build_manifest(project_dir, counter_file, max_iterations=1)
    manifest["roles"] = {"mutator": {"enabled": True}}
    manifest.pop("mutation")

    run = manager.create_run(name="Audit run", manifest=manifest, autostart=True)
    completed = manager.wait_for_status(run["id"], ["completed"], timeout=5.0)
    assert completed is not None

    audit = manager.get_mutation_audit(run["id"], 1)
    assert audit["iteration"] == 1
    assert audit["run_id"] == run["id"]
    assert "train.py" in audit["allowed_paths"]
    assert "evaluate.py" in audit["blocked_paths"]
    assert "evaluate.py" in audit["restored_paths"]
    # Diffs should be present for modified files
    assert len(audit["diffs"]) > 0
    # At least one change entry should carry inline diff content
    changes_with_diff = [c for c in audit["changes"] if c.get("diff")]
    assert len(changes_with_diff) > 0


def test_get_mutation_audit_empty_for_no_mutator(tmp_path):
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")
    run = manager.create_run(
        name="No mutator", goal="Test empty audit", autostart=True, max_iterations=1,
    )
    completed = manager.wait_for_status(run["id"], ["completed"], timeout=3.0)
    assert completed is not None

    audit = manager.get_mutation_audit(run["id"], 1)
    assert audit["iteration"] == 1
    assert audit["changed_paths"] == []
    assert audit["diffs"] == {}


def test_mutator_audit_event_includes_diff_previews_and_summaries(tmp_path):
    project_dir = _write_demo_project(tmp_path)
    counter_file = tmp_path / "dataset-counter.txt"
    fake_roles = _FakeMutatorRoleRunner()
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch", role_runner=fake_roles)

    manifest = _build_manifest(project_dir, counter_file, max_iterations=1)
    manifest["roles"] = {"mutator": {"enabled": True}}
    manifest.pop("mutation")

    run = manager.create_run(name="Audit event", manifest=manifest, autostart=True)
    completed = manager.wait_for_status(run["id"], ["completed"], timeout=5.0)
    assert completed is not None

    events = manager.list_events(run["id"])
    audit_events = [e for e in events if e["event_type"] == "mutator.audit.completed"]
    assert len(audit_events) == 1

    payload = audit_events[0]["payload"]
    assert payload["changed_files"] > 0
    assert "allowed_paths" in payload
    assert isinstance(payload["changes"], list)
    assert len(payload["changes"]) > 0
    # At least one change should have a diff_preview
    previews = [c for c in payload["changes"] if c.get("diff_preview")]
    assert len(previews) > 0
    assert "mutator_summary" in payload


def test_promotion_with_threshold_gate(tmp_path):
    """Candidate should not promote if metric is below the threshold."""
    project_dir = _write_demo_project(tmp_path)
    counter_file = tmp_path / "dataset-counter.txt"
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")

    manifest = _build_manifest(project_dir, counter_file, max_iterations=1)
    # The evaluate.py in the demo project produces score=float(iteration),
    # so iteration 1 → score=1.0. Set threshold above that so the candidate
    # stays "evaluated" not "promoted".
    manifest["promotion"] = {
        "metric": "score",
        "higher_is_better": True,
        "threshold": 5.0,
    }

    run = manager.create_run(name="Threshold gate", manifest=manifest, autostart=True)
    completed = manager.wait_for_status(run["id"], ["completed"], timeout=5.0)
    assert completed is not None

    candidates = manager.list_candidates(run["id"])
    assert len(candidates) == 1
    assert candidates[0]["status"] == "evaluated"


def test_promotion_with_min_improvement_gate(tmp_path):
    """Candidate should not promote when improvement is below min_improvement."""
    project_dir = _write_demo_project(tmp_path)
    counter_file = tmp_path / "dataset-counter.txt"
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")

    manifest = _build_manifest(project_dir, counter_file, max_iterations=2)
    # min_improvement=10.0 means the second candidate must beat the first by 10+.
    # The demo project produces very close scores, so second candidate stays "evaluated".
    manifest["promotion"] = {
        "metric": "score",
        "higher_is_better": True,
        "min_improvement": 10.0,
    }

    run = manager.create_run(name="Min improvement", manifest=manifest, autostart=True)
    completed = manager.wait_for_status(run["id"], ["completed"], timeout=5.0)
    assert completed is not None

    candidates = manager.list_candidates(run["id"])
    assert len(candidates) == 2
    # First candidate has no prior best, so it promotes (no delta check applies)
    assert candidates[0]["status"] == "promoted"
    # Second candidate should not promote since delta is tiny
    assert candidates[1]["status"] == "evaluated"


def test_promotion_without_promotion_config_falls_back_to_default(tmp_path):
    """Without promotion config, any improvement should still promote."""
    project_dir = _write_demo_project(tmp_path)
    counter_file = tmp_path / "dataset-counter.txt"
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")

    manifest = _build_manifest(project_dir, counter_file, max_iterations=1)
    # No promotion config at all — should use default is_better_metric behavior
    manifest.pop("promotion", None)

    run = manager.create_run(name="Default promo", manifest=manifest, autostart=True)
    completed = manager.wait_for_status(run["id"], ["completed"], timeout=5.0)
    assert completed is not None

    candidates = manager.list_candidates(run["id"])
    assert len(candidates) == 1
    # First candidate with any metric beats None, so it should promote
    assert candidates[0]["status"] == "promoted"
