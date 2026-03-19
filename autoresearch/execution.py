"""Execution helpers for manifest-driven AutoResearch iterations."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import difflib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from autoresearch.models import ResearchRun


class CommandExecutionError(RuntimeError):
    """Raised when a phase command fails."""

    def __init__(self, phase: str, message: str, *, result: Optional["CommandExecutionResult"] = None):
        super().__init__(message)
        self.phase = phase
        self.result = result


@dataclass(slots=True)
class WorkspaceSetup:
    """Resolved workspace information for one iteration."""

    source_root: str
    workspace_dir: str
    iteration_dir: str
    snapshot_enabled: bool = False
    copied_paths: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CommandExecutionResult:
    """Persisted metadata about a phase command execution."""

    phase: str
    status: str
    command: str | List[str]
    shell: bool
    cwd: str
    executor: str
    started_at: str
    completed_at: str
    duration_seconds: float
    returncode: int
    log_path: str
    output_preview: str = ""
    result_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkspaceChange:
    """One file-level change inside a workspace snapshot."""

    path: str
    status: str
    allowed: bool
    before_path: Optional[str] = None
    after_path: Optional[str] = None
    diff_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class _StrictFormatDict(dict):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


def format_template(value: Any, context: Mapping[str, Any]) -> Any:
    """Format strings recursively using *context*."""
    if isinstance(value, str):
        try:
            return value.format_map(_StrictFormatDict({key: str(item) for key, item in context.items()}))
        except KeyError as exc:  # pragma: no cover - exercised through runtime tests
            raise ValueError(f"Unknown template variable in manifest command: {exc.args[0]}") from exc
    if isinstance(value, list):
        return [format_template(item, context) for item in value]
    if isinstance(value, tuple):
        return [format_template(item, context) for item in value]
    if isinstance(value, dict):
        return {str(key): format_template(item, context) for key, item in value.items()}
    return value


def resolve_workspace_setup(
    *,
    store,
    run: ResearchRun,
    iteration: int,
) -> WorkspaceSetup:
    """Resolve and optionally snapshot the workspace for one iteration."""
    manifest = dict(run.manifest or {})
    workspace_cfg = dict(manifest.get("workspace") or {})
    source_root = _resolve_source_root(run, workspace_cfg)
    iteration_dir = store.get_iteration_dir(run.id, iteration)

    snapshot_cfg = workspace_cfg.get("snapshot")
    if isinstance(snapshot_cfg, Mapping):
        snapshot_include = _normalize_paths(snapshot_cfg.get("include"))
    else:
        snapshot_include = []
    snapshot_include.extend(_normalize_paths(workspace_cfg.get("include")))
    snapshot_include.extend(_normalize_paths(manifest.get("fixed_surface")))
    snapshot_include.extend(_normalize_paths(manifest.get("mutable_surface")))

    snapshot_mode = str(workspace_cfg.get("mode") or "").strip().lower()
    snapshot_enabled = snapshot_mode == "snapshot" or bool(snapshot_include)
    if not snapshot_enabled:
        return WorkspaceSetup(
            source_root=str(source_root),
            workspace_dir=str(source_root),
            iteration_dir=str(iteration_dir),
            snapshot_enabled=False,
        )

    workspace_dir = iteration_dir / "workspace"
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    copied_paths: List[str] = []
    for relative_path in _dedupe_preserve_order(snapshot_include):
        matched = _expand_relative_path(source_root, relative_path)
        if not matched:
            raise FileNotFoundError(
                f"Workspace snapshot path '{relative_path}' did not match any file under {source_root}"
            )
        for path in matched:
            copied_paths.append(_copy_path_into_workspace(path, source_root, workspace_dir))

    return WorkspaceSetup(
        source_root=str(source_root),
        workspace_dir=str(workspace_dir),
        iteration_dir=str(iteration_dir),
        snapshot_enabled=True,
        copied_paths=sorted(set(copied_paths)),
    )


def execute_phase_command(
    *,
    phase: str,
    command: str | Sequence[str],
    cwd: Path,
    context: Mapping[str, Any],
    env: Optional[Mapping[str, Any]],
    timeout_seconds: int,
    log_path: Path,
    result_path: Optional[Path] = None,
) -> CommandExecutionResult:
    """Execute a manifest command using Hermes environments when available."""
    rendered_command = format_template(command, context)
    rendered_env = format_template(dict(env or {}), context)
    command_payload: str | List[str]
    shell = isinstance(rendered_command, str)

    if isinstance(rendered_command, str):
        command_payload = rendered_command
    else:
        command_payload = [str(item) for item in rendered_command]

    started_at = _utcnow_iso()
    start_time = time.monotonic()
    output = ""
    returncode = 1
    executor = "subprocess"

    try:
        env_result = _execute_with_hermes_environment(
            command=command_payload,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            extra_env={str(key): str(value) for key, value in rendered_env.items()},
            task_id=str(context.get("task_id") or f"autoresearch-{context.get('run_id', 'run')}"),
        )
        if env_result is not None:
            output = str(env_result.get("output") or "")
            returncode = int(env_result.get("returncode", 1))
            executor = str(env_result.get("executor") or "hermes-environment")
        else:
            completed = _execute_with_subprocess(
                command=command_payload,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                extra_env={str(key): str(value) for key, value in rendered_env.items()},
            )
            output = completed["output"]
            returncode = completed["returncode"]
    except subprocess.TimeoutExpired:
        output = f"Command timed out after {timeout_seconds}s"
        returncode = 124

    duration_seconds = round(time.monotonic() - start_time, 3)
    completed_at = _utcnow_iso()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")

    result = CommandExecutionResult(
        phase=phase,
        status="completed" if returncode == 0 else "failed",
        command=command_payload,
        shell=shell,
        cwd=str(cwd),
        executor=executor,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        returncode=returncode,
        log_path=str(log_path),
        output_preview=output[:800],
        result_path=str(result_path) if result_path else None,
    )
    if result_path is not None:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")

    if returncode != 0:
        raise CommandExecutionError(
            phase,
            f"{phase} command failed with exit code {returncode}",
            result=result,
        )
    return result


def resolve_artifact_path(path_value: Optional[str], *, base_dir: Path, context: Mapping[str, Any]) -> Optional[Path]:
    """Resolve an artifact path from manifest configuration."""
    if not path_value:
        return None
    rendered = str(format_template(path_value, context)).strip()
    if not rendered:
        return None
    path = Path(rendered).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def load_metrics_file(path: Optional[Path]) -> Dict[str, float]:
    """Load numeric metrics from a JSON file."""
    if path is None or not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    metrics: Dict[str, float] = {}
    _flatten_numeric_metrics(raw, metrics)
    return metrics


def load_text_artifact(path: Optional[Path]) -> str:
    """Load a text artifact if present."""
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def collect_workspace_changes(
    *,
    workspace: WorkspaceSetup,
    mutable_surface: Sequence[str],
    audit_dir: Path,
) -> Dict[str, Any]:
    """Inspect a snapshot workspace and return structured file changes."""
    workspace_root = Path(workspace.workspace_dir)
    source_root = Path(workspace.source_root)
    snapshot_paths = list(workspace.copied_paths)
    if not workspace.snapshot_enabled:
        return {
            "workspace_snapshot": False,
            "changes": [],
            "allowed_changes": [],
            "blocked_changes": [],
            "changed_paths": [],
            "blocked_paths": [],
            "allowed_paths": [],
        }

    before_dir = audit_dir / "before"
    after_dir = audit_dir / "after"
    diff_dir = audit_dir / "diffs"
    before_dir.mkdir(parents=True, exist_ok=True)
    after_dir.mkdir(parents=True, exist_ok=True)
    diff_dir.mkdir(parents=True, exist_ok=True)

    workspace_files = {
        path.relative_to(workspace_root).as_posix(): path
        for path in workspace_root.rglob("*")
        if path.is_file()
    }
    candidate_paths = set(workspace_files.keys()) | set(snapshot_paths)

    changes: List[WorkspaceChange] = []
    for relative_path in sorted(candidate_paths):
        workspace_path = workspace_root / relative_path
        source_path = source_root / relative_path
        source_exists = source_path.exists() and source_path.is_file()
        workspace_exists = workspace_path.exists() and workspace_path.is_file()

        status = ""
        if workspace_exists and not source_exists:
            status = "created"
        elif source_exists and not workspace_exists:
            status = "deleted"
        elif workspace_exists and source_exists and not _files_equal(source_path, workspace_path):
            status = "modified"
        else:
            continue

        allowed = _path_allowed(relative_path, mutable_surface)
        before_copy = _copy_optional_file(source_path if source_exists else None, before_dir, relative_path)
        after_copy = _copy_optional_file(workspace_path if workspace_exists else None, after_dir, relative_path)
        diff_path = _write_diff_file(
            source_path if source_exists else None,
            workspace_path if workspace_exists else None,
            diff_dir,
            relative_path,
        )
        changes.append(
            WorkspaceChange(
                path=relative_path,
                status=status,
                allowed=allowed,
                before_path=str(before_copy) if before_copy else None,
                after_path=str(after_copy) if after_copy else None,
                diff_path=str(diff_path) if diff_path else None,
            )
        )

    allowed_changes = [item.to_dict() for item in changes if item.allowed]
    blocked_changes = [item.to_dict() for item in changes if not item.allowed]
    return {
        "workspace_snapshot": True,
        "changes": [item.to_dict() for item in changes],
        "allowed_changes": allowed_changes,
        "blocked_changes": blocked_changes,
        "changed_paths": [item.path for item in changes],
        "allowed_paths": [item.path for item in changes if item.allowed],
        "blocked_paths": [item.path for item in changes if not item.allowed],
    }


def restore_workspace_paths(
    *,
    workspace: WorkspaceSetup,
    relative_paths: Sequence[str],
) -> List[str]:
    """Restore the given relative paths from source into the workspace snapshot."""
    restored: List[str] = []
    workspace_root = Path(workspace.workspace_dir)
    source_root = Path(workspace.source_root)

    for relative_path in relative_paths:
        rel = str(relative_path).strip()
        if not rel:
            continue
        workspace_path = workspace_root / rel
        source_path = source_root / rel
        if source_path.exists() and source_path.is_file():
            workspace_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, workspace_path)
            restored.append(rel)
        elif workspace_path.exists():
            workspace_path.unlink()
            restored.append(rel)
            _cleanup_empty_parents(workspace_path.parent, workspace_root)
    return restored


def select_primary_metric(
    metrics: Mapping[str, float],
    *,
    preferred_name: Optional[str] = None,
) -> tuple[Optional[str], Optional[float]]:
    """Pick a primary metric from a metrics mapping."""
    if preferred_name and preferred_name in metrics:
        return preferred_name, float(metrics[preferred_name])
    if not metrics:
        return None, None
    name = sorted(metrics.keys())[0]
    return name, float(metrics[name])


def is_better_metric(candidate: float, incumbent: Optional[float], *, higher_is_better: bool) -> bool:
    """Return True when *candidate* beats *incumbent*."""
    if incumbent is None:
        return True
    if higher_is_better:
        return candidate > incumbent
    return candidate < incumbent


def _resolve_source_root(run: ResearchRun, workspace_cfg: Mapping[str, Any]) -> Path:
    root_value = str(workspace_cfg.get("root") or "").strip()
    source_path = ""
    if isinstance(run.manifest, Mapping):
        source_path = str(run.manifest.get("source_path") or "").strip()
    if root_value:
        root_path = Path(root_value).expanduser()
        if not root_path.is_absolute() and source_path:
            root_path = Path(source_path).resolve().parent / root_path
        return root_path.resolve()
    if source_path:
        return Path(source_path).resolve().parent
    metadata_root = str((run.metadata or {}).get("workspace_root") or "").strip()
    if metadata_root:
        return Path(metadata_root).expanduser().resolve()
    return Path.cwd().resolve()


def _normalize_paths(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        results: List[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                results.append(text)
        return results
    return []


def _dedupe_preserve_order(items: Sequence[str]) -> List[str]:
    seen = set()
    results: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        results.append(item)
    return results


def _expand_relative_path(source_root: Path, relative_path: str) -> List[Path]:
    if any(char in relative_path for char in "*?[]"):
        return [path.resolve() for path in source_root.glob(relative_path)]
    path = (source_root / relative_path).resolve()
    return [path] if path.exists() else []


def _copy_path_into_workspace(path: Path, source_root: Path, workspace_dir: Path) -> str:
    relative = path.relative_to(source_root)
    destination = workspace_dir / relative
    if path.is_dir():
        shutil.copytree(path, destination, dirs_exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return relative.as_posix()


def _execute_with_subprocess(
    *,
    command: str | List[str],
    cwd: Path,
    timeout_seconds: int,
    extra_env: Mapping[str, str],
) -> Dict[str, Any]:
    env = dict(os.environ)
    env.update(extra_env)
    if isinstance(command, str):
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    else:
        completed = subprocess.run(
            command,
            shell=False,
            cwd=str(cwd),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    return {
        "output": completed.stdout or "",
        "returncode": int(completed.returncode),
        "executor": "subprocess",
    }


def _execute_with_hermes_environment(
    *,
    command: str | List[str],
    cwd: Path,
    timeout_seconds: int,
    extra_env: Mapping[str, str],
    task_id: str,
) -> Optional[Dict[str, Any]]:
    try:
        from tools.terminal_tool import _create_environment, _get_env_config
    except Exception:
        return None

    try:
        env_config = _get_env_config()
        env_type = str(env_config.get("env_type") or "local")
        if env_type == "docker":
            image = str(env_config.get("docker_image") or "")
        elif env_type == "singularity":
            image = str(env_config.get("singularity_image") or "")
        elif env_type == "modal":
            image = str(env_config.get("modal_image") or "")
        elif env_type == "daytona":
            image = str(env_config.get("daytona_image") or "")
        else:
            image = ""

        environment = _create_environment(
            env_type,
            image,
            str(cwd),
            timeout_seconds,
            ssh_config={
                "host": env_config.get("ssh_host"),
                "user": env_config.get("ssh_user"),
                "port": env_config.get("ssh_port"),
                "key": env_config.get("ssh_key"),
                "persistent": env_config.get("ssh_persistent"),
            },
            container_config={
                "container_cpu": env_config.get("container_cpu"),
                "container_memory": env_config.get("container_memory"),
                "container_disk": env_config.get("container_disk"),
                "container_persistent": env_config.get("container_persistent"),
                "docker_volumes": env_config.get("docker_volumes"),
                "docker_forward_env": env_config.get("docker_forward_env"),
                "docker_mount_cwd_to_workspace": env_config.get("docker_mount_cwd_to_workspace"),
            },
            local_config={"persistent": bool(env_config.get("local_persistent"))},
            task_id=task_id,
            host_cwd=env_config.get("host_cwd"),
        )
        if hasattr(environment, "env"):
            current = dict(getattr(environment, "env", {}) or {})
            current.update(extra_env)
            environment.env = current

        command_text = _command_to_shell_text(command)
        result = environment.execute(command_text, cwd=str(cwd), timeout=timeout_seconds)
        payload = dict(result or {})
        payload["executor"] = f"hermes:{env_type}"
        return payload
    except Exception:
        return None
    finally:
        try:
            if "environment" in locals():
                environment.cleanup()
        except Exception:
            pass


def _command_to_shell_text(command: str | List[str]) -> str:
    if isinstance(command, str):
        return command
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def _flatten_numeric_metrics(value: Any, output: Dict[str, float], *, prefix: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            _flatten_numeric_metrics(item, output, prefix=next_prefix)
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping) and "name" in item and "value" in item:
                name = str(item.get("name") or "").strip()
                metric_value = item.get("value")
                if name and isinstance(metric_value, (int, float)):
                    output[name] = float(metric_value)
            else:
                _flatten_numeric_metrics(item, output, prefix=prefix)
        return
    if prefix and isinstance(value, (int, float)):
        output[prefix] = float(value)


def _files_equal(left: Path, right: Path) -> bool:
    return left.read_bytes() == right.read_bytes()


def _path_allowed(relative_path: str, mutable_surface: Sequence[str]) -> bool:
    normalized = Path(relative_path).as_posix()
    for item in mutable_surface:
        spec = str(item or "").strip().replace("\\", "/")
        if not spec:
            continue
        if any(char in spec for char in "*?[]"):
            if Path(normalized).match(spec):
                return True
            continue
        base = spec.rstrip("/")
        if normalized == base or normalized.startswith(base + "/"):
            return True
    return False


def _copy_optional_file(path: Optional[Path], target_root: Path, relative_path: str) -> Optional[Path]:
    if path is None or not path.exists() or not path.is_file():
        return None
    destination = target_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination


def _write_diff_file(
    before_path: Optional[Path],
    after_path: Optional[Path],
    diff_root: Path,
    relative_path: str,
) -> Optional[Path]:
    before_text = _safe_text(before_path)
    after_text = _safe_text(after_path)
    if before_text is None or after_text is None:
        return None
    diff_lines = list(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
    )
    if not diff_lines:
        return None
    destination = diff_root / f"{relative_path}.diff"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(diff_lines), encoding="utf-8")
    return destination


def _safe_text(path: Optional[Path]) -> Optional[str]:
    if path is None or not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _cleanup_empty_parents(path: Path, stop_at: Path) -> None:
    current = path
    stop = stop_at.resolve()
    while current.exists():
        if current.resolve() == stop:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
