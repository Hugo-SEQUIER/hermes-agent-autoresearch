"""Action-oriented AutoResearch tool for Hermes."""

from __future__ import annotations

import json
from typing import Any

from autoresearch.runtime import (
    inspect_project,
    inspect_run,
    list_projects,
    list_runs,
    publish_summary,
    research_cycle,
    status,
    validate_project,
)
from tools.registry import registry


AUTORESEARCH_SCHEMA = {
    "name": "autoresearch",
    "description": (
        "Run bounded AutoResearch workflows for a workspace-defined project. "
        "Use list_projects and inspect_project first, validate_project before running, "
        "research_cycle to execute a family, status/list_runs/inspect_run to inspect results, "
        "and publish_summary to prepare or send a short messaging summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_projects",
                    "inspect_project",
                    "validate_project",
                    "research_cycle",
                    "status",
                    "list_runs",
                    "inspect_run",
                    "publish_summary",
                ],
                "description": "AutoResearch action to perform.",
            },
            "project_root": {
                "type": "string",
                "description": "Optional workspace root containing .hermes/autoresearch/project.yaml. Defaults to the nearest discovered project.",
            },
            "family_id": {
                "type": "string",
                "description": "Family ID to run for action='research_cycle'.",
            },
            "run_id": {
                "type": "string",
                "description": "Run ID for status, inspect_run, or publish_summary.",
            },
            "population": {
                "type": "integer",
                "description": "Optional override for generated candidate count.",
            },
            "survivors": {
                "type": "integer",
                "description": "Optional override for how many candidates survive ranking before selector checks.",
            },
            "seed": {
                "type": "integer",
                "description": "Random seed used for param_mutation families.",
            },
            "model": {
                "type": "string",
                "description": "Optional model override for agent_patch generation.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of runs to return for action='list_runs'.",
            },
            "target": {
                "type": "string",
                "description": "Optional messaging target for publish_summary, for example 'telegram' or 'discord:#research'.",
            },
            "send": {
                "type": "boolean",
                "description": "When true, publish_summary also sends the summary through Hermes messaging.",
            },
        },
        "required": ["action"],
    },
}


def autoresearch_tool(args: dict[str, Any], **kwargs) -> str:
    """Dispatch AutoResearch tool actions."""
    action = (args.get("action") or "").strip().lower()
    project_root = args.get("project_root")

    try:
        if action == "list_projects":
            payload = list_projects(project_root)
        elif action == "inspect_project":
            payload = inspect_project(project_root)
        elif action == "validate_project":
            payload = validate_project(project_root)
        elif action == "research_cycle":
            family_id = args.get("family_id")
            if not family_id:
                return json.dumps({"success": False, "error": "family_id is required for action='research_cycle'"}, indent=2)
            payload = research_cycle(
                project_root=project_root,
                family_id=family_id,
                population=args.get("population"),
                survivors=args.get("survivors"),
                seed=args.get("seed", 7),
                model=args.get("model"),
                task_id=kwargs.get("task_id"),
            )
        elif action == "status":
            run_id = args.get("run_id")
            if not run_id:
                return json.dumps({"success": False, "error": "run_id is required for action='status'"}, indent=2)
            payload = status(run_id=run_id, project_root=project_root)
        elif action == "list_runs":
            payload = list_runs(project_root=project_root, limit=int(args.get("limit", 20)))
        elif action == "inspect_run":
            run_id = args.get("run_id")
            if not run_id:
                return json.dumps({"success": False, "error": "run_id is required for action='inspect_run'"}, indent=2)
            payload = inspect_run(run_id=run_id, project_root=project_root)
        elif action == "publish_summary":
            run_id = args.get("run_id")
            if not run_id:
                return json.dumps({"success": False, "error": "run_id is required for action='publish_summary'"}, indent=2)
            payload = publish_summary(
                run_id=run_id,
                project_root=project_root,
                target=args.get("target"),
                send=bool(args.get("send", False)),
            )
        else:
            return json.dumps({"success": False, "error": f"Unknown autoresearch action '{action}'"}, indent=2)
        return json.dumps({"success": True, **payload}, indent=2)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, indent=2)


registry.register(
    name="autoresearch",
    toolset="autoresearch",
    schema=AUTORESEARCH_SCHEMA,
    handler=autoresearch_tool,
    check_fn=lambda: True,
    emoji="AR",
)
