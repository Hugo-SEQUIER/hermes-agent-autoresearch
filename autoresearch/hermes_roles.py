"""Hermes role execution helpers for AutoResearch."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from autoresearch.models import ResearchRun


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class RoleResult:
    """Structured output from one Hermes role execution."""

    role: str
    status: str
    content: str = ""
    model: str = ""
    started_at: str = field(default_factory=_utcnow_iso)
    completed_at: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HermesRoleRunner:
    """Runs bounded planner/reporter roles via direct AIAgent calls."""

    def __init__(self, enabled_by_default: Optional[bool] = None):
        self.enabled_by_default = enabled_by_default

    def run_role(
        self,
        *,
        role_name: str,
        run: ResearchRun,
        iteration: int,
        context: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> RoleResult:
        manifest = dict(run.manifest or {})
        roles_cfg = manifest.get("roles")
        if not isinstance(roles_cfg, Mapping):
            return RoleResult(role=role_name, status="disabled")

        role_cfg_raw = roles_cfg.get(role_name)
        role_cfg = dict(role_cfg_raw) if isinstance(role_cfg_raw, Mapping) else {}
        if not self._is_role_enabled(role_cfg):
            return RoleResult(role=role_name, status="disabled")

        started_at = _utcnow_iso()
        try:
            runtime_kwargs = self._resolve_runtime_agent_kwargs()
            model = str(role_cfg.get("model") or self._resolve_model())
            enabled_toolsets, disabled_toolsets = self._resolve_toolsets(role_name, role_cfg)

            from run_agent import AIAgent
            task_id = str(
                role_cfg.get("task_id")
                or context.get("task_id")
                or f"autoresearch:{run.id}:{role_name}:{iteration}"
            )
            overrides_registered = False
            workspace_dir = str(context.get("workspace_dir") or "").strip()
            if workspace_dir:
                try:
                    from tools.terminal_tool import register_task_env_overrides

                    register_task_env_overrides(task_id, {"cwd": workspace_dir})
                    overrides_registered = True
                except Exception:
                    overrides_registered = False

            try:
                agent = AIAgent(
                    model=model,
                    max_iterations=max(1, int(role_cfg.get("max_iterations") or 6)),
                    quiet_mode=True,
                    verbose_logging=False,
                    ephemeral_system_prompt=str(
                        role_cfg.get("system_prompt") or self._default_system_prompt(role_name)
                    ),
                    session_id=str(role_cfg.get("session_id") or f"autoresearch:{run.id}:{role_name}"),
                    platform="api_server",
                    skip_context_files=True,
                    skip_memory=True,
                    enabled_toolsets=enabled_toolsets,
                    disabled_toolsets=disabled_toolsets,
                    **runtime_kwargs,
                )
                result = agent.run_conversation(
                    user_message=self._build_role_prompt(
                        role_name=role_name,
                        run=run,
                        iteration=iteration,
                        context=context,
                        payload=payload,
                        role_cfg=role_cfg,
                    ),
                    conversation_history=[],
                    task_id=task_id,
                )
            finally:
                if overrides_registered:
                    try:
                        from tools.terminal_tool import clear_task_env_overrides

                        clear_task_env_overrides(task_id)
                    except Exception:
                        pass
            usage = {
                "input_tokens": int(getattr(agent, "session_prompt_tokens", 0) or 0),
                "output_tokens": int(getattr(agent, "session_completion_tokens", 0) or 0),
                "total_tokens": int(getattr(agent, "session_total_tokens", 0) or 0),
            }
            content = str(result.get("final_response") or "").strip()
            return RoleResult(
                role=role_name,
                status="completed",
                content=content,
                model=model,
                started_at=started_at,
                completed_at=_utcnow_iso(),
                usage=usage,
                metadata={
                    "task_id": task_id,
                    "toolsets": enabled_toolsets or [],
                    "disabled_toolsets": disabled_toolsets or [],
                    "workspace_dir": workspace_dir or None,
                },
            )
        except Exception as exc:
            return RoleResult(
                role=role_name,
                status="failed",
                started_at=started_at,
                completed_at=_utcnow_iso(),
                error=str(exc),
            )

    def _is_role_enabled(self, role_cfg: Mapping[str, Any]) -> bool:
        env_default = self.enabled_by_default
        if env_default is None:
            env_default = str(os.getenv("AUTORESEARCH_ENABLE_HERMES_ROLES", "")).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        if "enabled" in role_cfg:
            return bool(role_cfg.get("enabled"))
        return bool(role_cfg) or bool(env_default)

    @staticmethod
    def _resolve_runtime_agent_kwargs() -> Dict[str, Any]:
        from hermes_cli.runtime_provider import (
            format_runtime_provider_error,
            resolve_runtime_provider,
        )

        try:
            runtime = resolve_runtime_provider(
                requested=os.getenv("HERMES_INFERENCE_PROVIDER"),
            )
        except Exception as exc:
            raise RuntimeError(format_runtime_provider_error(exc)) from exc

        return {
            "api_key": runtime.get("api_key"),
            "base_url": runtime.get("base_url"),
            "provider": runtime.get("provider"),
            "api_mode": runtime.get("api_mode"),
            "command": runtime.get("command"),
            "args": list(runtime.get("args") or []),
        }

    @staticmethod
    def _resolve_model() -> str:
        model = os.getenv("HERMES_MODEL") or os.getenv("LLM_MODEL") or "anthropic/claude-opus-4.6"
        try:
            import yaml as _yaml

            hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
            cfg_path = hermes_home / "config.yaml"
            if cfg_path.exists():
                with open(cfg_path, encoding="utf-8") as handle:
                    cfg = _yaml.safe_load(handle) or {}
                model_cfg = cfg.get("model", {})
                if isinstance(model_cfg, str):
                    model = model_cfg
                elif isinstance(model_cfg, dict):
                    model = model_cfg.get("default", model)
        except Exception:
            pass
        return model

    @staticmethod
    def _resolve_toolsets(
        role_name: str,
        role_cfg: Mapping[str, Any],
    ) -> tuple[Optional[List[str]], Optional[List[str]]]:
        explicit = role_cfg.get("toolsets")
        if explicit is not None:
            toolsets = [str(item).strip() for item in list(explicit or []) if str(item).strip()]
            if toolsets:
                return toolsets, None

        disabled = role_cfg.get("disabled_toolsets")
        if disabled is not None:
            disabled_toolsets = [str(item).strip() for item in list(disabled or []) if str(item).strip()]
            return None, disabled_toolsets

        if role_name == "mutator":
            return ["file", "terminal"], None

        from toolsets import get_all_toolsets

        return None, list(get_all_toolsets())

    def _build_role_prompt(
        self,
        *,
        role_name: str,
        run: ResearchRun,
        iteration: int,
        context: Mapping[str, Any],
        payload: Mapping[str, Any],
        role_cfg: Mapping[str, Any],
    ) -> str:
        custom_prompt = str(role_cfg.get("prompt") or "").strip()
        if custom_prompt:
            return custom_prompt

        body = self._payload_json(payload, max_chars=int(role_cfg.get("max_context_chars") or 12000))
        instructions = []
        if role_name == "mutator":
            instructions = [
                "Mutation instructions:",
                "- Work inside the provided workspace.",
                "- Edit only paths listed in `mutable_surface`.",
                "- Treat `fixed_surface` as read-only reference material.",
                "- Prefer small, intentional edits that align with the plan.",
                "- End with a concise summary of the exact edits performed.",
                "",
            ]
        if role_name == "planner":
            op_messages = list(payload.get("recent_operator_messages") or [])
            if op_messages:
                instructions.extend([
                    "Operator guidance (incorporate into your plan):",
                ])
                for msg in op_messages:
                    author = msg.get("author", "operator")
                    content = msg.get("content", "")
                    instructions.append(f"  - [{author}]: {content}")
                instructions.append("")
        header = [
            f"AutoResearch role: {role_name}",
            f"Run: {run.title}",
            f"Goal: {run.goal}",
            f"Iteration: {iteration}/{run.max_iterations}",
            f"Project: {context.get('project_name') or run.title}",
            "",
            *instructions,
            "Context payload:",
            body,
        ]
        return "\n".join(header)

    @staticmethod
    def _default_system_prompt(role_name: str) -> str:
        if role_name == "planner":
            return (
                "You are Hermes acting as the planner role inside an AutoResearch loop. "
                "Use only the provided context unless tools were explicitly enabled. "
                "If operator guidance is present, treat it as high-priority input and reflect it in your plan. "
                "Produce a concise markdown plan that identifies the current hypothesis, "
                "the most important next action, and the main risks to watch."
            )
        if role_name == "researcher":
            return (
                "You are Hermes acting as the researcher role inside an AutoResearch loop. "
                "Analyze the current metrics, candidate history, and operator guidance to "
                "identify promising hypotheses, unexplored data directions, and potential "
                "angles for the next mutation. Be specific and concise."
            )
        if role_name == "mutator":
            return (
                "You are Hermes acting as the mutator role inside an AutoResearch loop. "
                "You may edit only the allowed mutable files in the provided workspace. "
                "Never modify fixed-surface files. Keep changes minimal, targeted, and comparable. "
                "When finished, summarize exactly what you changed and why."
            )
        if role_name == "orchestrator":
            return (
                "You are Hermes acting as the orchestrator role inside an AutoResearch loop. "
                "Decide which specialized roles should be active for this iteration based on the "
                "current state: metrics trend, iteration number, recent failures, and operator guidance. "
                "Output a JSON object with keys: researcher (bool), critic (bool), and strategy (string). "
                "The strategy field should be a one-sentence explanation of your delegation decision. "
                "Keep delegation depth limited — not every iteration needs every role."
            )
        if role_name == "critic":
            return (
                "You are Hermes acting as the critic role inside an AutoResearch loop. "
                "Examine the latest evaluation results, metric trends, and mutation changes "
                "to identify signs of overfitting, regressions, weak validation, or suspicious "
                "improvements. Be skeptical and specific. Flag concrete concerns with evidence "
                "from the data."
            )
        return (
            "You are Hermes acting as the reporter role inside an AutoResearch loop. "
            "Use the provided iteration results to write a concise markdown summary of "
            "what happened, what the metrics mean, and what the operator should focus on next."
        )

    @staticmethod
    def _payload_json(payload: Mapping[str, Any], *, max_chars: int) -> str:
        raw = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        if len(raw) <= max_chars:
            return raw
        return raw[:max_chars] + "\n...<truncated>..."
