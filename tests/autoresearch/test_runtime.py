from pathlib import Path

import pytest

from autoresearch.runtime import AutoResearchManager, InvalidRunStateError


def test_create_run_bootstraps_events_and_report(tmp_path: Path):
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")

    run = manager.create_run(
        name="Demo run",
        goal="Find better strategies",
        manifest={"fixed_surface": ["evaluate.py"]},
        autostart=True,
    )

    assert run["title"] == "Demo run"
    assert run["status"] == "running"
    assert run["report_count"] == 1

    reports = manager.list_reports(run["id"])
    assert len(reports) == 1
    assert reports[0]["kind"] == "run_brief"

    events = manager.list_events(run["id"])
    assert [event["type"] for event in events] == [
        "run.created",
        "report.written",
        "run.resumed",
    ]


def test_operator_messages_and_mutation_requests_update_run(tmp_path: Path):
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")
    run = manager.create_run(name="Demo", goal="Test run")

    operator_event = manager.add_operator_message(
        run["id"],
        content="Focus on robustness next",
        scope="run",
        author="operator",
    )
    mutation_event = manager.request_mutation(run["id"], reason="Force a new candidate")
    updated = manager.get_run(run["id"])

    assert operator_event["type"] == "operator.message"
    assert mutation_event["type"] == "mutation.requested"
    assert updated["operator_message_count"] == 1
    assert updated["mutation_request_count"] == 1


def test_terminal_run_cannot_resume(tmp_path: Path):
    manager = AutoResearchManager(base_dir=tmp_path / "autoresearch")
    run = manager.create_run(name="Demo", goal="Stop me")

    manager.stop_run(run["id"])

    with pytest.raises(InvalidRunStateError):
        manager.resume_run(run["id"])
