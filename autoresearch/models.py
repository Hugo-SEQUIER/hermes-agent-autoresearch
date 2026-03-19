"""Data models for the AutoResearch runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ResearchRun:
    """Persisted summary of a research run."""

    id: str
    title: str
    goal: str
    notes: str = ""
    status: str = "created"
    phase: str = "created"
    max_iterations: int = 25
    current_iteration: int = 0
    event_count: int = 0
    report_count: int = 0
    operator_message_count: int = 0
    mutation_request_count: int = 0
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    last_error: Optional[str] = None
    last_operator_message_at: Optional[str] = None
    manifest: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResearchRun":
        return cls(
            id=str(data["id"]),
            title=str(data.get("title") or ""),
            goal=str(data.get("goal") or ""),
            notes=str(data.get("notes") or ""),
            status=str(data.get("status") or "created"),
            phase=str(data.get("phase") or "created"),
            max_iterations=int(data.get("max_iterations", 25)),
            current_iteration=int(data.get("current_iteration", 0)),
            event_count=int(data.get("event_count", 0)),
            report_count=int(data.get("report_count", 0)),
            operator_message_count=int(data.get("operator_message_count", 0)),
            mutation_request_count=int(data.get("mutation_request_count", 0)),
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            last_error=data.get("last_error"),
            last_operator_message_at=data.get("last_operator_message_at"),
            manifest=data.get("manifest"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class ResearchEvent:
    """Append-only event emitted by a research run."""

    id: str
    run_id: str
    sequence: int
    type: str
    timestamp: str = field(default_factory=utc_now_iso)
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResearchEvent":
        return cls(
            id=str(data["id"]),
            run_id=str(data["run_id"]),
            sequence=int(data["sequence"]),
            type=str(data["type"]),
            timestamp=str(data.get("timestamp") or utc_now_iso()),
            payload=dict(data.get("payload") or {}),
        )


@dataclass
class OperatorMessage:
    """Operator-authored message linked to a run."""

    id: str
    run_id: str
    content: str
    scope: str = "run"
    timestamp: str = field(default_factory=utc_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OperatorMessage":
        return cls(
            id=str(data["id"]),
            run_id=str(data["run_id"]),
            content=str(data.get("content") or ""),
            scope=str(data.get("scope") or "run"),
            timestamp=str(data.get("timestamp") or utc_now_iso()),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class ResearchReport:
    """Persisted report snapshot for a run."""

    id: str
    run_id: str
    kind: str
    title: str
    content: str
    created_at: str = field(default_factory=utc_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResearchReport":
        return cls(
            id=str(data["id"]),
            run_id=str(data["run_id"]),
            kind=str(data.get("kind") or "report"),
            title=str(data.get("title") or ""),
            content=str(data.get("content") or ""),
            created_at=str(data.get("created_at") or utc_now_iso()),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class ResearchCandidate:
    """Persisted candidate snapshot for a run iteration."""

    id: str
    run_id: str
    iteration: int
    title: str
    summary: str
    status: str = "evaluated"
    created_at: str = field(default_factory=utc_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResearchCandidate":
        return cls(
            id=str(data["id"]),
            run_id=str(data["run_id"]),
            iteration=int(data.get("iteration", 0)),
            title=str(data.get("title") or ""),
            summary=str(data.get("summary") or ""),
            status=str(data.get("status") or "evaluated"),
            created_at=str(data.get("created_at") or utc_now_iso()),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class ResearchMetric:
    """Persisted metric point for a run iteration."""

    id: str
    run_id: str
    iteration: int
    name: str
    value: float
    created_at: str = field(default_factory=utc_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResearchMetric":
        return cls(
            id=str(data["id"]),
            run_id=str(data["run_id"]),
            iteration=int(data.get("iteration", 0)),
            name=str(data.get("name") or ""),
            value=float(data.get("value", 0.0)),
            created_at=str(data.get("created_at") or utc_now_iso()),
            metadata=dict(data.get("metadata") or {}),
        )
