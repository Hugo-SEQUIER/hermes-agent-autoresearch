"""AutoResearch runtime primitives."""

from autoresearch.hermes_roles import HermesRoleRunner, RoleResult
from autoresearch.manifests import ProjectManifest, load_manifest
from autoresearch.models import (
    OperatorMessage,
    ResearchCandidate,
    ResearchEvent,
    ResearchMetric,
    ResearchReport,
    ResearchRun,
)
from autoresearch.runtime import (
    AutoResearchManager,
    InvalidRunStateError,
    RunNotFoundError,
    TERMINAL_RUN_STATUSES,
)
from autoresearch.storage import AutoResearchStore

__all__ = [
    "AutoResearchManager",
    "AutoResearchStore",
    "HermesRoleRunner",
    "InvalidRunStateError",
    "OperatorMessage",
    "ProjectManifest",
    "ResearchCandidate",
    "ResearchEvent",
    "ResearchMetric",
    "ResearchReport",
    "ResearchRun",
    "RoleResult",
    "RunNotFoundError",
    "TERMINAL_RUN_STATUSES",
    "load_manifest",
]
