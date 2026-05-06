from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import requests
from requests.auth import HTTPBasicAuth

from .models import JiraTask, JiraTicket


LABEL_SET_ADMISSIONS = {"amp", "cohort", "scholarship", "admissions_support"}
LABEL_SET_EXEC_ED = {"digitallearning", "oppo"}
LABEL_SET_SFMC = {"sfmc", "marketing"}
LABEL_SET_AMS = {
    "ams_support",
    "allotments",
    "allotment",
    "donations",
    "fa",
    "facultycontract",
    "lms",
    "umbraco",
    "alumni",
    "leads",
    "security_review",
}


class JiraService:
    def __init__(
        self,
        server: str,
        email: str,
        api_token: str,
        module_source: str = "labels",
        module_field: str = "",
        module_label_prefix: str = "",
        label_module_map: Dict[str, str] | None = None,
    ) -> None:
        self.server = server.rstrip("/")
        self.auth = HTTPBasicAuth(email, api_token)
        self.headers = {"Accept": "application/json", "Content-Type": "application/json"}
        self.module_source = (module_source or "labels").strip().lower()
        self.module_field = module_field
        self.module_label_prefix = (module_label_prefix or "").strip().lower()
        # Maps label value (lowercase) → slide-friendly module name
        self.label_module_map: Dict[str, str] = {
            k.lower(): v for k, v in (label_module_map or {}).items()
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_story_bug_tickets(
        self, project_key: str, cutoff_date, start_date=None, max_results: int = 0,
        on_progress=None, active_sprints_only: bool = False,
    ) -> List[JiraTicket]:
        """Fetch Story/Bug tickets. on_progress(msg) is called at key milestones for live UI feedback."""
        def _log(msg: str):
            print(msg)
            if on_progress:
                on_progress(msg)

        sprint_clause = " AND sprint in openSprints()" if active_sprints_only else ""
        cutoff_str = cutoff_date.strftime("%Y-%m-%d")

        if start_date is not None:
            start_str = start_date.strftime("%Y-%m-%d")
            jql_active = (
                f'project = "{project_key}" AND issuetype IN (Story, Bug) '
                f'AND updated >= "{start_str}" AND created <= "{cutoff_str}"'
                f'{sprint_clause} '
                "ORDER BY updated DESC"
            )
            excl = '"On Hold", "Discarded", "Done/Sign-Off", "Deployed to Production"'
            jql_carry = (
                f'project = "{project_key}" AND issuetype IN (Story, Bug) '
                f'AND created < "{start_str}" AND updated < "{start_str}" '
                f'AND status NOT IN ({excl})'
                f'{sprint_clause} '
                "ORDER BY created DESC"
            )
            sprint_label = "active sprints only" if active_sprints_only else "all sprints"
            _log(f"Query 1/2 — Active window (updated >= {start_str}, {sprint_label}): fetching pages...")
            raw_active = self._search_issues(jql_active, hard_limit=max_results, on_progress=on_progress)
            _log(f"Query 1/2 done — {len(raw_active)} active tickets.")

            _log(f"Query 2/2 — Carry-over (created < {start_str}, still open): fetching pages...")
            raw_carry  = self._search_issues(jql_carry,  hard_limit=max_results, on_progress=on_progress)
            _log(f"Query 2/2 done — {len(raw_carry)} carry-over tickets.")

            seen_keys  = {r["key"] for r in raw_active}
            raw_issues = raw_active + [r for r in raw_carry if r["key"] not in seen_keys]
            _log(f"Deduplication complete — {len(raw_issues)} total unique tickets to process.")
        else:
            jql = (
                f'project = "{project_key}" '
                "AND issuetype IN (Story, Bug) "
                f'AND created <= "{cutoff_str}"'
                f'{sprint_clause} '
                "ORDER BY updated DESC"
            )
            sprint_label = "active sprints only" if active_sprints_only else "all sprints"
            _log(f"Running single JQL query ({sprint_label})...")
            raw_issues = self._search_issues(jql, hard_limit=max_results, on_progress=on_progress)
            _log(f"Query done — {len(raw_issues)} tickets.")

        tickets: List[JiraTicket] = []
        total = len(raw_issues)
        _log(f"Fetching changelog & subtasks for each ticket (0/{total} done)...")
        for idx, raw in enumerate(raw_issues, 1):
            fields = raw.get("fields", {})
            key = raw["key"]
            ticket = JiraTicket(
                key=key,
                issue_type=fields.get("issuetype", {}).get("name", "Unknown"),
                summary=fields.get("summary") or "",
                description=self._extract_description(fields.get("description")),
                status=fields.get("status", {}).get("name", ""),
                labels=list(fields.get("labels") or []),
                module=self._extract_module_from_fields(fields, raw),
                created_at=self._parse_datetime(fields.get("created")),
                updated_at=self._parse_datetime(fields.get("updated")),
            )
            ticket.transitions = self._fetch_changelog(key)
            ticket.tasks = self._fetch_tasks(fields.get("subtasks") or [])
            tickets.append(ticket)
            if idx % 10 == 0 or idx == total:
                _log(f"  Changelog/subtasks: {idx}/{total} tickets processed.")

        # Log labels that truly have no known mapping (manual map + built-in routing rules)
        unmapped_labels = {
            lbl for t in tickets for lbl in t.labels
            if not self._is_label_mapped(lbl)
        }
        if unmapped_labels:
            _log(f"[INFO] Labels without a module mapping: {sorted(unmapped_labels)}")

        return tickets

    def _fetch_changelog(self, issue_key: str) -> Dict[str, List[datetime]]:
        """Fetch all status transitions for an issue via the changelog endpoint."""
        url = f"{self.server}/rest/api/3/issue/{issue_key}/changelog"
        transitions: Dict[str, List[datetime]] = {}
        start_at = 0

        while True:
            resp = requests.get(
                url,
                params={"maxResults": 100, "startAt": start_at},
                auth=self.auth,
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            for history in data.get("values", []):
                changed_at = self._parse_datetime(history.get("created"))
                for item in history.get("items", []):
                    if item.get("field") != "status" or changed_at is None:
                        continue
                    to_status = item.get("toString", "")
                    transitions.setdefault(to_status, []).append(changed_at)
            total = data.get("total", 0)
            start_at += len(data.get("values", []))
            if start_at >= total or not data.get("values"):
                break
        return transitions

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _search_issues(self, jql: str, hard_limit: int = 0, on_progress=None) -> List[Dict]:
        """Fetch matching issues using cursor-based pagination (Jira API v3).
        hard_limit: stop early after this many issues (0 = fetch all).
        """
        url = f"{self.server}/rest/api/3/search/jql"
        results: List[Dict] = []
        next_page_token: str | None = None
        page_size = hard_limit if 0 < hard_limit <= 100 else 100

        fields = [
            "summary", "description", "status", "labels",
            "created", "updated", "subtasks", "components",
            "issuetype", "parent",
        ]
        if self.module_field:
            fields.append(self.module_field)

        while True:
            payload: Dict[str, Any] = {
                "jql": jql,
                "maxResults": page_size,
                "fields": fields,
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token

            resp = requests.post(url, json=payload, auth=self.auth, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
            issues = data.get("issues", [])
            results.extend(issues)

            if on_progress:
                on_progress(f"  Page received: {len(issues)} tickets (running total: {len(results)})")

            if hard_limit > 0 and len(results) >= hard_limit:
                results = results[:hard_limit]
                break
            if data.get("isLast", True) or not issues:
                break
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        return results

    def _fetch_tasks(self, subtask_refs: List[Dict]) -> List[JiraTask]:
        tasks: List[JiraTask] = []
        for ref in subtask_refs:
            key = ref.get("key", "")
            if not key:
                continue
            url = f"{self.server}/rest/api/3/issue/{key}"
            params = {"fields": "summary,status,assignee,updated,description"}
            resp = requests.get(url, params=params, auth=self.auth, headers=self.headers)
            if resp.status_code != 200:
                continue
            data = resp.json()
            fields = data.get("fields", {})
            assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
            tasks.append(
                JiraTask(
                    key=key,
                    summary=fields.get("summary") or "",
                    status=(fields.get("status") or {}).get("name", ""),
                    assignee=assignee,
                    updated_at=self._parse_datetime(fields.get("updated")),
                    body_text=self._extract_description(fields.get("description")),
                )
            )
        return tasks

    def _extract_module_from_fields(self, fields: Dict, raw: Dict) -> str:
        labels: List[str] = [str(l).strip() for l in (fields.get("labels") or []) if str(l).strip()]

        if self.module_source == "labels":
            lowered = [l.lower() for l in labels]

            # ── Priority routing ─────────────────────────────────────────────
            # 1) substring: admission / application → Admissions Module
            if any(("admission" in l) or ("application" in l) for l in lowered):
                return "Admissions Module"
            # 2) exact match: Admissions extras
            if any(l in LABEL_SET_ADMISSIONS for l in lowered):
                return "Admissions Module"
            # 3) substring: slcm → Education Cloud Student Success
            if any("slcm" in l for l in lowered):
                return "Education Cloud Student Success"
            # 4) exact match: Executive Education
            if any(l in LABEL_SET_EXEC_ED for l in lowered):
                return "Executive Education"
            # 5) exact match: SFMC
            if any(l in LABEL_SET_SFMC for l in lowered):
                return "SFMC"
            # 6) exact match: AMS Non-Admissions
            if any(l in LABEL_SET_AMS for l in lowered):
                return "AMS Non-Admissions Module"
            # 7) any other labelled ticket → AMS Non-Admissions
            if labels:
                return "AMS Non-Admissions Module"

            if self.module_label_prefix:
                prefixed = [l for l in labels if l.lower().startswith(self.module_label_prefix)]
                if prefixed:
                    first = prefixed[0]
                    label_key = first.lower()
                    if label_key in self.label_module_map:
                        return self.label_module_map[label_key]
                    return first[len(self.module_label_prefix):].strip(" -:_") or first

            non_track = [l for l in labels if "slcm" not in l.lower()]
            chosen = non_track[0] if non_track else (labels[0] if labels else None)
            if chosen:
                return self.label_module_map.get(chosen.lower(), chosen)

            # No label present: group under AMS non-admission by default.
            return "AMS Non-Admissions Module"

        if self.module_source == "customfield" and self.module_field:
            val = fields.get(self.module_field)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, list) and val:
                return ", ".join(str(v) for v in val)
            if val:
                return str(val)

        components = fields.get("components") or []
        if components:
            return components[0].get("name", "General")

        return "General"

    def _is_label_mapped(self, label: str) -> bool:
        lowered = (label or "").strip().lower()
        if not lowered:
            return True
        if lowered in self.label_module_map:
            return True
        if "admission" in lowered or "application" in lowered or "slcm" in lowered:
            return True
        if lowered in LABEL_SET_ADMISSIONS:
            return True
        if lowered in LABEL_SET_EXEC_ED:
            return True
        if lowered in LABEL_SET_SFMC:
            return True
        if lowered in LABEL_SET_AMS:
            return True
        return False

    def _extract_status_transitions(self, raw: Dict) -> Dict[str, List[datetime]]:
        """Legacy helper kept for compatibility; prefer _fetch_changelog for live data."""
        transitions: Dict[str, List[datetime]] = {}
        changelog = raw.get("changelog") or {}
        for history in changelog.get("histories", []):
            changed_at = self._parse_datetime(history.get("created"))
            for item in history.get("items", []):
                if item.get("field") != "status" or changed_at is None:
                    continue
                to_status = item.get("toString", "")
                transitions.setdefault(to_status, []).append(changed_at)
        return transitions

    def _extract_description(self, description) -> str:
        """Handle both plain text (API v2) and Atlassian Document Format (API v3) descriptions."""
        if not description:
            return ""
        if isinstance(description, str):
            return description
        if isinstance(description, dict):
            return self._adf_to_text(description)
        return str(description)

    def _adf_to_text(self, node: Dict, depth: int = 0) -> str:
        """Recursively extract plain text from Atlassian Document Format nodes."""
        if not isinstance(node, dict):
            return str(node) if node else ""
        node_type = node.get("type", "")
        text = node.get("text", "")
        parts: List[str] = []
        if text:
            parts.append(text)
        for child in node.get("content", []):
            parts.append(self._adf_to_text(child, depth + 1))
        joined = " ".join(p for p in parts if p)
        if node_type in ("paragraph", "heading", "bulletList", "orderedList", "listItem", "blockquote"):
            joined = joined.strip() + "\n"
        return joined

    @staticmethod
    def _parse_datetime(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
