from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List


# Default status sets — preserved for backward compatibility
EXCLUDED_CARRY_OVER_STATUSES = {
    "On Hold",
    "Discarded",
    "Done/Sign-Off",
    "Deployed to Production",
}


@dataclass
class StatusConfig:
    """User-configurable status groupings for bucketing in the analysis layer."""
    excluded_carry_over: frozenset = field(
        default_factory=lambda: frozenset(EXCLUDED_CARRY_OVER_STATUSES)
    )
    uat_statuses: frozenset = field(
        default_factory=lambda: frozenset({"Deployed to UAT"})
    )
    prod_statuses: frozenset = field(
        default_factory=lambda: frozenset({"Deployed to Production"})
    )
    ready_qa_statuses: frozenset = field(
        default_factory=lambda: frozenset({"Ready for QA"})
    )


@dataclass
class ReportWindow:
    cutoff_date: date
    start_date: date
    weeks: int


@dataclass
class JiraTask:
    key: str
    summary: str
    status: str
    assignee: str
    updated_at: datetime | None
    body_text: str = ""


@dataclass
class StatusTransition:
    at: datetime
    from_status: str
    to_status: str


@dataclass
class JiraTicket:
    key: str
    issue_type: str
    summary: str
    description: str
    status: str
    labels: List[str]
    module: str
    created_at: datetime | None
    updated_at: datetime | None
    tasks: List[JiraTask] = field(default_factory=list)
    linked_dependency_keys: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    ai_summary: str = ""
    transitions: Dict[str, List[datetime]] = field(default_factory=dict)
    status_history: List[StatusTransition] = field(default_factory=list)

    @property
    def is_slcm(self) -> bool:
        return any("slcm" in label.lower() for label in self.labels)


@dataclass
class ModuleSnapshot:
    module: str
    carried_over: List[JiraTicket] = field(default_factory=list)
    created_in_period: List[JiraTicket] = field(default_factory=list)
    moved_to_uat: List[JiraTicket] = field(default_factory=list)
    moved_to_prod: List[JiraTicket] = field(default_factory=list)
    moved_to_ready_qa: List[JiraTicket] = field(default_factory=list)


@dataclass
class TrackSnapshot:
    track: str
    stories_count: int
    bugs_count: int
    summaries: List[str]
