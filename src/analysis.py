from __future__ import annotations

import re
from datetime import datetime, time
from typing import Dict, Iterable, List

from openai import OpenAI

from .models import EXCLUDED_CARRY_OVER_STATUSES, JiraTicket, ModuleSnapshot, TrackSnapshot

DEPENDENCY_PATTERN = re.compile(r"\b(depends on|blocked by|waiting for|dependency|external)\b", re.IGNORECASE)


def ticket_in_window(ts: datetime | None, start: datetime, end: datetime) -> bool:
    if ts is None:
        return False
    return start <= ts <= end


def build_module_snapshots(tickets: List[JiraTicket], start_date, end_date) -> Dict[str, ModuleSnapshot]:
    start = datetime.combine(start_date, time.min)
    end = datetime.combine(end_date, time.max)

    buckets: Dict[str, ModuleSnapshot] = {}
    for ticket in tickets:
        module = ticket.module or "Unmapped"
        snapshot = buckets.setdefault(module, ModuleSnapshot(module=module))

        if ticket.created_at and ticket.created_at < start and ticket.status not in EXCLUDED_CARRY_OVER_STATUSES:
            snapshot.carried_over.append(ticket)
        if ticket_in_window(ticket.created_at, start, end):
            snapshot.created_in_period.append(ticket)

        if has_transition_in_window(ticket, "Deployed to UAT", start, end):
            snapshot.moved_to_uat.append(ticket)
        if has_transition_in_window(ticket, "Deployed to Production", start, end):
            snapshot.moved_to_prod.append(ticket)
        if has_transition_in_window(ticket, "Ready for QA", start, end):
            snapshot.moved_to_ready_qa.append(ticket)

    return dict(sorted(buckets.items(), key=lambda kv: kv[0].lower()))


def has_transition_in_window(ticket: JiraTicket, transition_name: str, start: datetime, end: datetime) -> bool:
    for ts in ticket.transitions.get(transition_name, []):
        if start <= ts <= end:
            return True
    return False


def derive_dependencies(ticket: JiraTicket) -> List[str]:
    candidates: List[str] = [ticket.summary, ticket.description]
    for task in ticket.tasks:
        candidates.append(task.summary)
        candidates.append(task.body_text)

    lines = []
    for text in candidates:
        if not text:
            continue
        for line in text.splitlines():
            if DEPENDENCY_PATTERN.search(line):
                lines.append(line.strip())

    seen = set()
    dependencies = []
    for item in lines:
        normalized = item.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        dependencies.append(item[:220])
    return dependencies[:5]


def summarize_ticket_with_ai(ticket: JiraTicket, model: str, api_key: str, base_url: str) -> str:
    if not api_key:
        return fallback_summary(ticket)

    client = OpenAI(api_key=api_key, base_url=base_url)
    task_text = "\n".join(f"- {t.key}: {t.summary} [{t.status}]" for t in ticket.tasks[:20])
    dependency_text = "\n".join(f"- {d}" for d in ticket.dependencies) if ticket.dependencies else "- None found"

    prompt = f"""
You are writing status-report bullet lines for enterprise Jira updates.
Provide one bullet sentence in <=30 words with this order:
1) what progressed,
2) key decision(s),
3) dependency/dependencies.

Ticket: {ticket.key}
Type: {ticket.issue_type}
Summary: {ticket.summary}
Description: {ticket.description[:1500]}
Tasks:
{task_text}
Dependencies identified:
{dependency_text}
""".strip()

    try:
        response = client.responses.create(
            model=model,
            input=prompt,
            max_output_tokens=90,
            temperature=0.2,
        )
        text = response.output_text.strip()
        return trim_to_30_words(text)
    except Exception:
        return fallback_summary(ticket)


def fallback_summary(ticket: JiraTicket) -> str:
    task_progress = f"{len(ticket.tasks)} task(s) progressed"
    dependency = ticket.dependencies[0] if ticket.dependencies else "No explicit dependency noted"
    text = f"{ticket.key}: {ticket.summary}. {task_progress}; dependency: {dependency}."
    return trim_to_30_words(text)


def trim_to_30_words(text: str) -> str:
    words = text.split()
    if len(words) <= 30:
        return text
    return " ".join(words[:30]).rstrip(".,;:") + "..."


def build_track_snapshots(tickets: Iterable[JiraTicket], start_date, end_date) -> Dict[str, TrackSnapshot]:
    ams = [t for t in tickets if not t.is_slcm]
    slcm = [t for t in tickets if t.is_slcm]

    def counts(group: List[JiraTicket]) -> tuple[int, int]:
        stories = sum(1 for t in group if t.issue_type.lower() == "story")
        bugs = sum(1 for t in group if t.issue_type.lower() == "bug")
        return stories, bugs

    ams_stories, ams_bugs = counts(ams)
    slcm_stories, slcm_bugs = counts(slcm)

    return {
        "AMS": TrackSnapshot(
            track="AMS",
            stories_count=ams_stories,
            bugs_count=ams_bugs,
            summaries=[t.ai_summary for t in ams if t.ai_summary],
        ),
        "SLCM": TrackSnapshot(
            track="SLCM",
            stories_count=slcm_stories,
            bugs_count=slcm_bugs,
            summaries=[t.ai_summary for t in slcm if t.ai_summary],
        ),
    }
