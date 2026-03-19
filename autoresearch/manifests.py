"""Manifest loading and normalization for generic AutoResearch projects."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - dependency should normally exist
    yaml = None


def _normalize_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Expected a list of strings")

    items: List[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def _normalize_mapping(value: Any, *, field_name: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"Manifest field '{field_name}' must be a mapping")
    return dict(value)


@dataclass(slots=True)
class ProjectManifest:
    """Normalized project manifest used by the AutoResearch runtime."""

    name: str
    description: str = ""
    objective: str = ""
    workspace: Dict[str, Any] = field(default_factory=dict)
    dataset: Dict[str, Any] = field(default_factory=dict)
    mutation: Dict[str, Any] = field(default_factory=dict)
    evaluation: Dict[str, Any] = field(default_factory=dict)
    reporting: Dict[str, Any] = field(default_factory=dict)
    roles: Dict[str, Any] = field(default_factory=dict)
    fixed_surface: List[str] = field(default_factory=list)
    mutable_surface: List[str] = field(default_factory=list)
    promotion: Dict[str, Any] = field(default_factory=dict)
    stopping: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    source_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source_path: Optional[str] = None) -> "ProjectManifest":
        if not isinstance(data, Mapping):
            raise ValueError("Manifest must be a mapping")

        name = str(data.get("name") or "").strip()
        if not name:
            if source_path:
                name = Path(source_path).stem
            else:
                raise ValueError("Manifest requires a non-empty 'name'")

        known_keys = {
            "name",
            "description",
            "objective",
            "goal",
            "workspace",
            "dataset",
            "mutation",
            "evaluation",
            "reporting",
            "roles",
            "fixed_surface",
            "mutable_surface",
            "promotion",
            "promotion_rules",
            "stopping",
            "stop_rules",
            "metadata",
            "max_iterations",
        }
        return cls(
            name=name,
            description=str(data.get("description") or "").strip(),
            objective=str(data.get("objective") or data.get("goal") or "").strip(),
            workspace=_normalize_mapping(data.get("workspace"), field_name="workspace"),
            dataset=_normalize_mapping(data.get("dataset"), field_name="dataset"),
            mutation=_normalize_mapping(data.get("mutation"), field_name="mutation"),
            evaluation=_normalize_mapping(data.get("evaluation"), field_name="evaluation"),
            reporting=_normalize_mapping(data.get("reporting"), field_name="reporting"),
            roles=_normalize_mapping(data.get("roles"), field_name="roles"),
            fixed_surface=_normalize_string_list(data.get("fixed_surface")),
            mutable_surface=_normalize_string_list(data.get("mutable_surface")),
            promotion=_normalize_mapping(
                data.get("promotion") or data.get("promotion_rules"),
                field_name="promotion",
            ),
            stopping=_normalize_mapping(
                data.get("stopping") or data.get("stop_rules"),
                field_name="stopping",
            ),
            metadata=_normalize_mapping(data.get("metadata"), field_name="metadata"),
            extra={key: value for key, value in dict(data).items() if key not in known_keys},
            source_path=source_path,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "name": self.name,
            "description": self.description,
            "objective": self.objective,
            "workspace": dict(self.workspace),
            "dataset": dict(self.dataset),
            "mutation": dict(self.mutation),
            "evaluation": dict(self.evaluation),
            "reporting": dict(self.reporting),
            "roles": dict(self.roles),
            "fixed_surface": list(self.fixed_surface),
            "mutable_surface": list(self.mutable_surface),
            "promotion": dict(self.promotion),
            "stopping": dict(self.stopping),
            "metadata": dict(self.metadata),
            "source_path": self.source_path,
        }
        data.update(dict(self.extra))
        return data


def load_manifest(path: str | Path) -> ProjectManifest:
    """Load a YAML manifest from disk and return the normalized object."""

    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    if manifest_path.suffix.lower() == ".json":
        raw = json.loads(manifest_path.read_text(encoding="utf-8")) or {}
    else:
        if yaml is None:
            raise RuntimeError(
                "PyYAML is required to load YAML manifests. Install 'pyyaml' or use a JSON manifest."
            )
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    return ProjectManifest.from_dict(raw, source_path=str(manifest_path))
