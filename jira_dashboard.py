#!/usr/bin/env python3
"""Jira / ServiceNow CSV Dashboard Generator.

Ingests a Jira or ServiceNow CSV export and produces a single self-contained
HTML dashboard with ticket status, assignee workload, staleness, durations,
and more.  Source format is auto-detected from CSV headers, or can be forced
with ``--source jira|servicenow``.

Usage:
    python3 jira_dashboard.py export.csv
    python3 jira_dashboard.py export.csv -o my_dashboard.html -v
    python3 jira_dashboard.py export.csv --source servicenow -o sn_dashboard.html
    python3 jira_dashboard.py export.csv --stale-days 7 --title "Sprint 42 Dashboard"
"""

import argparse
import csv
import html
import io
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Column alias mapping
# ---------------------------------------------------------------------------

COLUMN_ALIASES: Dict[str, List[str]] = {
    "key": ["issue key", "key", "issue_key", "issuekey"],
    "summary": ["summary", "title", "description_short"],
    "status": ["status", "issue status", "status name"],
    "assignee": ["assignee", "assigned to", "assignee name"],
    "reporter": ["reporter", "reporter name", "created by"],
    "priority": ["priority", "priority name"],
    "issue_type": ["issue type", "issuetype", "type", "issue_type"],
    "created": ["created", "date created", "creation date", "created date"],
    "updated": ["updated", "date updated", "last updated", "updated date"],
    "resolved": ["resolved", "date resolved", "resolution date", "resolved date"],
    "due_date": ["due date", "due", "duedate", "due_date"],
    "labels": ["labels", "label"],
    "components": ["components", "component", "component/s"],
    "fix_versions": ["fix version/s", "fix versions", "fix version", "fixversions"],
    "resolution": ["resolution", "resolution name"],
    "story_points": ["story points", "story_points", "storypoints", "story point estimate"],
    "original_estimate": ["original estimate", "original_estimate", "time original estimate", "σ original estimate"],
    "time_spent": ["time spent", "time_spent", "timespent"],
    "remaining_estimate": ["remaining estimate", "remaining_estimate", "time remaining estimate"],
    "epic_link": ["epic link", "epic_link", "epic name", "epic"],
    "sprint": ["sprint", "sprint name"],
    "project": ["project", "project key", "project name"],
    "parent": ["parent", "parent key", "parent id"],
}


# ---------------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------------

@dataclass
class SourceConfig:
    """Encapsulates all source-specific knowledge (Jira vs ServiceNow)."""
    name: str = "jira"
    display_name: str = "Jira"
    column_aliases: Dict[str, List[str]] = field(default_factory=dict)
    open_statuses: set = field(default_factory=set)
    closed_statuses: set = field(default_factory=set)
    blocked_statuses: set = field(default_factory=set)
    status_colours: Dict[str, str] = field(default_factory=dict)
    priority_colours: Dict[str, str] = field(default_factory=dict)
    type_colours: Dict[str, str] = field(default_factory=dict)
    default_unassigned: str = "Unassigned"
    # Section toggles
    has_epics: bool = False
    has_sprints: bool = False
    has_story_points: bool = False
    has_estimation: bool = False
    has_sla: bool = False
    has_contact_type: bool = False
    has_escalation: bool = False
    has_reassignment: bool = False
    has_categories: bool = False
    has_assignment_groups: bool = False


def _jira_config() -> SourceConfig:
    """Return a SourceConfig for Jira CSV exports."""
    return SourceConfig(
        name="jira",
        display_name="Jira",
        column_aliases=dict(COLUMN_ALIASES),
        open_statuses={"open", "to do", "todo", "in progress", "in review", "reopened",
                       "backlog", "selected for development", "blocked", "waiting",
                       "new", "active", "in development", "in testing", "ready for review",
                       "in uat"},
        closed_statuses={"done", "closed", "resolved", "complete", "completed", "cancelled",
                         "won't do", "wontdo", "duplicate", "rejected"},
        blocked_statuses={"blocked", "waiting", "on hold", "impediment"},
        status_colours={
            "To Do": "#8A9499", "Open": "#8A9499", "Backlog": "#8A9499", "New": "#8A9499",
            "In Progress": "#4A9FD9", "In Review": "#6BB3E3", "In Development": "#4A9FD9",
            "In Testing": "#6BB3E3", "Active": "#4A9FD9", "In UAT": "#6BB3E3",
            "Done": "#4CAF50", "Closed": "#4CAF50", "Resolved": "#4CAF50", "Complete": "#4CAF50",
            "Blocked": "#F44336", "Waiting": "#FF9800", "Reopened": "#FF9800",
        },
        priority_colours={
            "Critical": "#F44336", "Highest": "#F44336", "Blocker": "#F44336",
            "High": "#FF9800", "Major": "#FF9800",
            "Medium": "#FFD54F", "Normal": "#FFD54F",
            "Low": "#4CAF50", "Minor": "#4CAF50",
            "Lowest": "#8A9499", "Trivial": "#8A9499",
        },
        type_colours={
            "Bug": "#F44336", "Defect": "#F44336",
            "Story": "#4A9FD9", "User Story": "#4A9FD9",
            "Task": "#8A9499", "Sub-task": "#6BB3E3",
            "Epic": "#9C27B0", "Initiative": "#9C27B0",
            "Improvement": "#FF9800", "New Feature": "#4CAF50",
        },
        default_unassigned="Unassigned",
        has_epics=True,
        has_sprints=True,
        has_story_points=True,
        has_estimation=True,
        has_sla=False,
        has_contact_type=False,
        has_escalation=False,
        has_reassignment=False,
        has_categories=False,
        has_assignment_groups=False,
    )


_SERVICENOW_ALIASES: Dict[str, List[str]] = {
    "key": ["number", "task number", "ticket number", "task_number", "ticket_number"],
    "summary": ["short description", "short_description", "description"],
    "status": ["state", "status", "incident state", "incident_state"],
    "assignee": ["assigned to", "assigned_to", "assignee"],
    "reporter": ["caller_id", "caller id", "caller", "opened by", "opened_by",
                 "requested by", "requested_by"],
    "priority": ["priority"],
    "issue_type": ["sys_class_name", "type", "task type", "task_type"],
    "created": ["opened at", "opened_at", "sys_created_on", "created"],
    "updated": ["updated at", "updated_at", "sys_updated_on", "sys_updated_by", "updated"],
    "resolved": ["resolved at", "resolved_at", "closed at", "closed_at"],
    "due_date": ["due date", "due_date", "expected start", "expected_start"],
    "labels": [],
    "components": [],
    "fix_versions": [],
    "resolution": ["close code", "close_code", "resolution code", "resolution_code"],
    "story_points": [],
    "original_estimate": [],
    "time_spent": ["time worked", "time_worked"],
    "remaining_estimate": [],
    "epic_link": [],
    "sprint": [],
    "project": ["company", "department", "service_offering", "business_service"],
    "parent": ["parent", "parent incident", "parent_incident"],
    # ServiceNow-specific fields
    "category": ["category"],
    "subcategory": ["subcategory", "sub_category"],
    "assignment_group": ["assignment group", "assignment_group"],
    "contact_type": ["contact type", "contact_type"],
    "impact": ["impact"],
    "urgency": ["urgency"],
    "made_sla": ["made sla", "made_sla"],
    "business_duration": ["business duration", "business_duration", "business_stc",
                          "calendar_duration", "calendar_stc"],
    "escalation": ["escalation"],
    "reassignment_count": ["reassignment count", "reassignment_count"],
    "reopen_count": ["reopen count", "reopen_count", "u_reopen_count_multiplied"],
    "close_notes": ["close notes", "close_notes", "resolution notes", "resolution_notes"],
    "closed_at": ["closed at", "closed_at"],
    "severity": ["severity"],
    "active": ["active"],
    "configuration_item": ["configuration item", "configuration_item", "cmdb_ci", "ci"],
}


def _servicenow_config() -> SourceConfig:
    """Return a SourceConfig for ServiceNow CSV exports."""
    return SourceConfig(
        name="servicenow",
        display_name="ServiceNow",
        column_aliases=dict(_SERVICENOW_ALIASES),
        open_statuses={"new", "in progress", "on hold", "open", "work in progress",
                       "assess", "authorize", "scheduled", "implement", "review",
                       "assessed", "root cause analysis", "fix in progress",
                       "active", "awaiting info", "awaiting problem",
                       "awaiting change", "awaiting vendor",
                       "1", "2", "3", "-5"},
        closed_statuses={"resolved", "closed", "cancelled", "closed complete",
                         "closed incomplete", "closed skipped", "complete",
                         "6", "7", "8", "4"},
        blocked_statuses={"on hold", "pending", "awaiting info",
                          "awaiting problem", "awaiting change",
                          "awaiting vendor", "-5", "3"},
        status_colours={
            "New": "#8A9499", "Open": "#8A9499",
            "In Progress": "#4A9FD9", "Work in Progress": "#4A9FD9",
            "Assess": "#6BB3E3", "Authorize": "#6BB3E3",
            "Scheduled": "#00BCD4", "Implement": "#4A9FD9",
            "Review": "#6BB3E3", "Fix in Progress": "#4A9FD9",
            "On Hold": "#FF9800", "Pending": "#FF9800",
            "Resolved": "#4CAF50", "Closed": "#4CAF50",
            "Closed Complete": "#4CAF50", "Closed Incomplete": "#8A9499",
            "Closed Skipped": "#8A9499", "Cancelled": "#607D8B",
            # Numeric state values
            "1": "#8A9499", "2": "#4A9FD9", "3": "#FF9800",
            "6": "#4CAF50", "7": "#4CAF50", "8": "#607D8B",
        },
        priority_colours={
            "1 - Critical": "#F44336", "1": "#F44336", "Critical": "#F44336",
            "2 - High": "#FF9800", "2": "#FF9800", "High": "#FF9800",
            "3 - Moderate": "#FFD54F", "3": "#FFD54F", "Moderate": "#FFD54F",
            "4 - Low": "#4CAF50", "4": "#4CAF50", "Low": "#4CAF50",
            "5 - Planning": "#8A9499", "5": "#8A9499", "Planning": "#8A9499",
        },
        type_colours={
            "Incident": "#F44336", "incident": "#F44336",
            "Problem": "#FF9800", "problem": "#FF9800",
            "Change": "#4A9FD9", "change_request": "#4A9FD9",
            "Request": "#4CAF50", "sc_request": "#4CAF50",
            "Task": "#8A9499", "sc_task": "#6BB3E3",
            "Catalog Task": "#6BB3E3", "sc_cat_item": "#6BB3E3",
        },
        default_unassigned="Unassigned",
        has_epics=False,
        has_sprints=False,
        has_story_points=False,
        has_estimation=False,
        has_sla=True,
        has_contact_type=True,
        has_escalation=True,
        has_reassignment=True,
        has_categories=True,
        has_assignment_groups=True,
    )


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------

def _detect_source(headers: List[str]) -> str:
    """Score headers to determine whether CSV is Jira or ServiceNow.

    Returns ``"jira"`` or ``"servicenow"``.
    """
    lower = {h.strip().lower() for h in headers}

    jira_indicators = {"issue key", "issue_key", "sprint", "epic link", "epic_link",
                       "story points", "story_points", "issue type", "issuetype",
                       "fix version/s"}
    sn_indicators = {"number", "opened at", "opened_at", "assignment group",
                     "assignment_group", "made sla", "made_sla", "short description",
                     "short_description", "configuration item", "configuration_item",
                     "cmdb_ci", "contact type", "contact_type", "opened by",
                     "opened_by", "caller_id", "resolved at", "resolved_at",
                     "reassignment count", "reassignment_count", "incident_state",
                     "sys_class_name", "sys_created_on", "sys_updated_on",
                     "service_offering", "business_service"}

    # Also check for Custom field(...) wrapper — very Jira-specific
    jira_score = sum(1 for ind in jira_indicators if ind in lower)
    jira_score += sum(1 for h in lower if h.startswith("custom field"))
    sn_score = sum(1 for ind in sn_indicators if ind in lower)

    return "servicenow" if sn_score > jira_score else "jira"


def _build_alias_lookup(headers: List[str], aliases: Optional[Dict[str, List[str]]] = None) -> Dict[str, List[int]]:
    """Build a mapping from canonical field name to candidate column indices.

    Returns a *list* of indices per field because Jira CSV exports often
    contain multiple columns for the same concept (e.g. ``Sprint``,
    ``Sprint``, ``Sprint``; or ``Custom field (Story Points)`` alongside
    ``Custom field (Story point estimate)``).  The caller should coalesce
    by picking the first non-empty value across all candidate columns.

    Also handles Jira's ``Custom field (Name)`` wrapper — e.g. a header of
    ``Custom field (Story Points)`` will match the alias ``story points``.
    """
    if aliases is None:
        aliases = COLUMN_ALIASES
    lower_headers = [h.strip().lower() for h in headers]
    # Also build a version that strips the "custom field (...)" wrapper
    _cf_re = re.compile(r"^custom\s+field\s*\((.+)\)$")
    unwrapped = []
    for h in lower_headers:
        m = _cf_re.match(h)
        unwrapped.append(m.group(1).strip() if m else h)

    lookup: Dict[str, List[int]] = {}
    for canonical, alias_list in aliases.items():
        indices: List[int] = []
        seen: set = set()
        for alias in alias_list:
            # Collect ALL matching columns for each alias
            for i, (lh, uw) in enumerate(zip(lower_headers, unwrapped)):
                if i not in seen and (lh == alias or uw == alias):
                    indices.append(i)
                    seen.add(i)
        lookup[canonical] = indices
    return lookup


def _find_comment_columns(headers: List[str]) -> List[int]:
    """Find all comment-related column indices."""
    indices = []
    for i, h in enumerate(headers):
        hl = h.strip().lower()
        if "comment" in hl:
            indices.append(i)
    return indices


# ---------------------------------------------------------------------------
# Date / duration parsing
# ---------------------------------------------------------------------------

_DATE_FORMATS = [
    "%d/%b/%y %I:%M %p",
    "%d/%b/%y %H:%M",
    "%d/%b/%Y %I:%M %p",
    "%d/%b/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    # US short formats (M/D/YY) — common in ServiceNow exports
    "%m/%d/%y %H:%M:%S",
    "%m/%d/%y %H:%M",
    "%m/%d/%y %I:%M %p",
    "%m/%d/%y",
]


def parse_date(value: str) -> Optional[datetime]:
    """Try multiple date formats; return None on failure."""
    if not value or not value.strip():
        return None
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=None)
        except ValueError:
            continue
    return None


_DURATION_RE = re.compile(
    r"(?:(\d+)\s*w)?\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?",
    re.IGNORECASE,
)


def parse_duration_seconds(value: str) -> Optional[int]:
    """Parse Jira duration strings like '1w 2d 3h 30m' into seconds."""
    if not value or not value.strip():
        return None
    value = value.strip()
    try:
        return int(float(value))
    except ValueError:
        pass
    m = _DURATION_RE.fullmatch(value.strip())
    if m and any(m.groups()):
        weeks = int(m.group(1) or 0)
        days = int(m.group(2) or 0)
        hours = int(m.group(3) or 0)
        minutes = int(m.group(4) or 0)
        seconds = int(m.group(5) or 0)
        return weeks * 5 * 8 * 3600 + days * 8 * 3600 + hours * 3600 + minutes * 60 + seconds
    return None


def format_duration(seconds: Optional[int]) -> str:
    """Format seconds into a human-readable duration."""
    if seconds is None or seconds < 0:
        return "—"
    if seconds == 0:
        return "0m"
    parts = []
    weeks = seconds // (5 * 8 * 3600)
    seconds %= 5 * 8 * 3600
    days = seconds // (8 * 3600)
    seconds %= 8 * 3600
    hours = seconds // 3600
    seconds %= 3600
    minutes = seconds // 60
    if weeks:
        parts.append(f"{weeks}w")
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "< 1m"


def _format_days(days: float) -> str:
    """Format days as a readable string."""
    if days < 1:
        return "< 1d"
    return f"{days:.1f}d"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class JiraTicket:
    key: str = ""
    summary: str = ""
    status: str = ""
    assignee: str = ""
    reporter: str = ""
    priority: str = ""
    issue_type: str = ""
    created: Optional[datetime] = None
    updated: Optional[datetime] = None
    resolved: Optional[datetime] = None
    due_date: Optional[datetime] = None
    labels: str = ""
    components: str = ""
    fix_versions: str = ""
    resolution: str = ""
    story_points: Optional[float] = None
    original_estimate_secs: Optional[int] = None
    time_spent_secs: Optional[int] = None
    remaining_estimate_secs: Optional[int] = None
    epic_link: str = ""
    sprint: str = ""
    project: str = ""
    parent: str = ""
    last_comment_date: Optional[datetime] = None
    last_comment_text: str = ""
    raw_fields: Dict[str, str] = field(default_factory=dict)
    # ServiceNow-specific fields (harmless defaults when unused)
    category: str = ""
    subcategory: str = ""
    assignment_group: str = ""
    contact_type: str = ""
    impact: str = ""
    urgency: str = ""
    made_sla: Optional[bool] = None
    business_duration_secs: Optional[int] = None
    escalation: str = ""
    reassignment_count: Optional[int] = None
    reopen_count: Optional[int] = None
    close_notes: str = ""
    closed_at: Optional[datetime] = None
    severity: str = ""
    active: str = ""


# Backwards-compat alias
Ticket = JiraTicket


# ---------------------------------------------------------------------------
# CSV Parsing
# ---------------------------------------------------------------------------

def _extract_sn_work_notes(row: List[str], comment_cols: List[int]) -> Tuple[Optional[datetime], str]:
    """Extract latest comment date and text from ServiceNow work notes columns.

    ServiceNow work notes format:  ``YYYY-MM-DD HH:MM:SS - Author\\nText``
    or plain text.
    """
    latest_date: Optional[datetime] = None
    latest_text = ""
    sn_note_re = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*-\s*(.+)")
    for ci in comment_cols:
        if ci >= len(row):
            continue
        val = row[ci].strip()
        if not val:
            continue
        # Try SN format first
        m = sn_note_re.match(val)
        if m:
            d = parse_date(m.group(1))
            text = val[m.end():].strip().lstrip("-").strip()
            if not text:
                text = val
        else:
            d = None
            text = val
        if d is not None:
            if latest_date is None or d > latest_date:
                latest_date = d
                latest_text = text
        elif not latest_text:
            latest_text = text
    return latest_date, latest_text


def _extract_comments(row: List[str], comment_cols: List[int]) -> Tuple[Optional[datetime], str]:
    """Extract latest comment date and text from comment columns."""
    latest_date: Optional[datetime] = None
    latest_text = ""
    comment_date_re = re.compile(r"^(\d{1,2}/\w{3}/\d{2,4}\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)")
    for ci in comment_cols:
        if ci >= len(row):
            continue
        val = row[ci].strip()
        if not val:
            continue
        parts = val.split(";")
        if len(parts) >= 3:
            d = parse_date(parts[0].strip())
            text = parts[-1].strip()
        else:
            dm = comment_date_re.match(val)
            if dm:
                d = parse_date(dm.group(1))
                text = val[dm.end():].strip().lstrip(";").strip()
            else:
                d = None
                text = val
        if d is not None:
            if latest_date is None or d > latest_date:
                latest_date = d
                latest_text = text
        elif not latest_text:
            latest_text = text
    return latest_date, latest_text


def _find_work_notes_columns(headers: List[str]) -> List[int]:
    """Find ServiceNow work notes / additional comments columns."""
    indices = []
    for i, h in enumerate(headers):
        hl = h.strip().lower()
        if "work notes" in hl or "additional comments" in hl or "comment" in hl or hl == "actions_taken" or hl == "work_notes":
            indices.append(i)
    return indices


def _read_file(path: Path) -> str:
    """Read a file trying UTF-8 first, falling back to cp1252 for Windows exports.

    Handles the edge case where a file has a UTF-8 BOM but cp1252 body content
    (common with ServiceNow/Windows exports that contain special characters).
    """
    # Read raw bytes once
    raw = path.read_bytes()
    # Strip UTF-8 BOM if present, regardless of actual encoding
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_csv(filepath: str, config: SourceConfig, verbose: bool = False) -> List[JiraTicket]:
    """Parse a CSV export into a list of JiraTicket objects using the given config."""
    path = Path(filepath)
    content = _read_file(path)
    reader = csv.reader(io.StringIO(content))
    try:
        headers = next(reader)
    except StopIteration:
        return []

    lookup = _build_alias_lookup(headers, config.column_aliases)
    is_sn = config.name == "servicenow"
    if is_sn:
        comment_cols = _find_work_notes_columns(headers)
    else:
        comment_cols = _find_comment_columns(headers)
    tickets: List[JiraTicket] = []
    first_raw_row: Optional[List[str]] = None

    def _get(row: List[str], canonical: str) -> str:
        """Return the first non-empty value across all candidate columns."""
        for idx in lookup.get(canonical, []):
            if idx < len(row) and row[idx].strip():
                return row[idx].strip()
        return ""

    for row_num, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        if first_raw_row is None:
            first_raw_row = list(row)
        t = JiraTicket()
        t.key = _get(row, "key")
        t.summary = _get(row, "summary")
        t.status = _get(row, "status")
        t.assignee = _get(row, "assignee") or config.default_unassigned
        t.reporter = _get(row, "reporter")
        t.priority = _get(row, "priority")
        t.issue_type = _get(row, "issue_type")
        t.created = parse_date(_get(row, "created"))
        t.updated = parse_date(_get(row, "updated"))
        t.resolved = parse_date(_get(row, "resolved"))
        t.due_date = parse_date(_get(row, "due_date"))
        t.labels = _get(row, "labels")
        t.components = _get(row, "components")
        t.fix_versions = _get(row, "fix_versions")
        t.resolution = _get(row, "resolution")
        t.epic_link = _get(row, "epic_link")
        t.sprint = _get(row, "sprint")
        t.project = _get(row, "project")
        t.parent = _get(row, "parent")

        sp = _get(row, "story_points")
        if sp:
            try:
                t.story_points = float(sp)
            except ValueError:
                pass

        t.original_estimate_secs = parse_duration_seconds(_get(row, "original_estimate"))
        t.time_spent_secs = parse_duration_seconds(_get(row, "time_spent"))
        t.remaining_estimate_secs = parse_duration_seconds(_get(row, "remaining_estimate"))

        if is_sn:
            t.last_comment_date, t.last_comment_text = _extract_sn_work_notes(row, comment_cols)
            # ServiceNow-specific fields
            t.category = _get(row, "category")
            t.subcategory = _get(row, "subcategory")
            t.assignment_group = _get(row, "assignment_group")
            t.contact_type = _get(row, "contact_type")
            t.impact = _get(row, "impact")
            t.urgency = _get(row, "urgency")
            t.escalation = _get(row, "escalation")
            t.close_notes = _get(row, "close_notes")
            t.closed_at = parse_date(_get(row, "closed_at"))
            t.severity = _get(row, "severity")
            t.active = _get(row, "active")
            # Boolean / numeric SN fields
            sla_val = _get(row, "made_sla").lower()
            if sla_val in ("true", "1", "yes"):
                t.made_sla = True
            elif sla_val in ("false", "0", "no"):
                t.made_sla = False
            bd = _get(row, "business_duration")
            if bd:
                t.business_duration_secs = parse_duration_seconds(bd)
            rc = _get(row, "reassignment_count")
            if rc:
                try:
                    t.reassignment_count = int(float(rc))
                except ValueError:
                    pass
            roc = _get(row, "reopen_count")
            if roc:
                try:
                    t.reopen_count = int(float(roc))
                except ValueError:
                    pass
        else:
            t.last_comment_date, t.last_comment_text = _extract_comments(row, comment_cols)

        for i, h in enumerate(headers):
            if i < len(row):
                t.raw_fields[h] = row[i]

        tickets.append(t)

    if verbose:
        print(f"Parsed {len(tickets)} tickets from {filepath} (source: {config.display_name})")
        mapped = {c: indices for c, indices in lookup.items() if indices}
        unmapped = [c for c, indices in lookup.items() if not indices]
        print(f"  Columns mapped ({len(mapped)}): {', '.join(sorted(mapped))}")
        if unmapped:
            print(f"  Columns NOT mapped: {', '.join(sorted(unmapped))}")
        print(f"  Comment/work-notes columns: {len(comment_cols)}")
        print(f"  CSV headers (first 5): {headers[:5]}")
        # Show sample data from first ticket for key diagnostic fields
        if tickets:
            t0 = tickets[0]
            # Show raw CSV values for date columns to diagnose parsing
            def _raw_val(canonical: str) -> str:
                if first_raw_row is None:
                    return "N/A"
                for idx in lookup.get(canonical, []):
                    if idx < len(first_raw_row) and first_raw_row[idx].strip():
                        return first_raw_row[idx].strip()
                return "(empty)"
            print(f"  Sample ticket: key={t0.key!r}, status={t0.status!r}, "
                  f"created={t0.created}, assignee={t0.assignee!r}")
            print(f"  Raw 'created' (opened_at) value: {_raw_val('created')!r}")
            if is_sn:
                print(f"  Raw 'resolved' value: {_raw_val('resolved')!r}")
                print(f"  Raw 'updated' value: {_raw_val('updated')!r}")
                print(f"    category={t0.category!r}, assignment_group={t0.assignment_group!r}, "
                      f"made_sla={t0.made_sla}, issue_type={t0.issue_type!r}")
            # Show unique status values to diagnose open/closed classification
            statuses = set(t.status for t in tickets if t.status)
            print(f"  Unique status values ({len(statuses)}): {sorted(statuses)[:15]}")
            created_count = sum(1 for t in tickets if t.created is not None)
            print(f"  Tickets with created date: {created_count}/{len(tickets)}")
            if created_count == 0 and tickets:
                # Extra diagnostic: show what columns are mapped to 'created'
                created_indices = lookup.get("created", [])
                created_headers = [headers[i] for i in created_indices if i < len(headers)]
                print(f"  DEBUG 'created' mapped to columns: {created_headers} (indices: {created_indices})")

    return tickets


def parse_jira_csv(filepath: str, verbose: bool = False, config: Optional[SourceConfig] = None) -> List[JiraTicket]:
    """Parse a Jira CSV export into a list of JiraTicket objects.

    Backwards-compatible wrapper around ``_parse_csv``.
    """
    if config is None:
        config = _jira_config()
    return _parse_csv(filepath, config, verbose=verbose)


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

_OPEN_STATUSES = {"open", "to do", "todo", "in progress", "in review", "reopened",
                  "backlog", "selected for development", "blocked", "waiting",
                  "new", "active", "in development", "in testing", "ready for review",
                  "in uat"}
_CLOSED_STATUSES = {"done", "closed", "resolved", "complete", "completed", "cancelled",
                    "won't do", "wontdo", "duplicate", "rejected"}
_BLOCKED_STATUSES = {"blocked", "waiting", "on hold", "impediment"}


def _is_open(status: str, config: Optional[SourceConfig] = None) -> bool:
    sl = status.strip().lower()
    closed = config.closed_statuses if config else _CLOSED_STATUSES
    open_s = config.open_statuses if config else _OPEN_STATUSES
    if sl in closed:
        return False
    if sl in open_s:
        return True
    return True


def _is_blocked(status: str, config: Optional[SourceConfig] = None) -> bool:
    blocked = config.blocked_statuses if config else _BLOCKED_STATUSES
    return status.strip().lower() in blocked


def _split_csv_field(value: str) -> List[str]:
    """Split a comma/semicolon-separated Jira field into individual values."""
    if not value.strip():
        return []
    items = []
    for item in re.split(r"[,;]+", value):
        item = item.strip()
        if item:
            items.append(item)
    return items


@dataclass
class DashboardData:
    # Summary cards
    total_tickets: int = 0
    open_tickets: int = 0
    closed_tickets: int = 0
    avg_age_open_days: float = 0.0
    overdue_tickets: int = 0
    stale_tickets: int = 0
    resolution_rate: float = 0.0
    avg_resolution_days: float = 0.0
    unassigned_tickets: int = 0
    blocked_tickets: int = 0
    total_story_points: float = 0.0
    open_story_points: float = 0.0

    # Charts
    status_counts: Dict[str, int] = field(default_factory=dict)
    assignee_counts: Dict[str, int] = field(default_factory=dict)
    priority_counts: Dict[str, int] = field(default_factory=dict)
    type_counts: Dict[str, int] = field(default_factory=dict)
    component_counts: Dict[str, int] = field(default_factory=dict)
    label_counts: Dict[str, int] = field(default_factory=dict)

    # Trend
    created_by_month: Dict[str, int] = field(default_factory=dict)
    resolved_by_month: Dict[str, int] = field(default_factory=dict)

    # Tables
    staleness_rows: List[Dict[str, Any]] = field(default_factory=list)
    avg_resolution_by_type: Dict[str, float] = field(default_factory=dict)
    avg_resolution_by_priority: Dict[str, float] = field(default_factory=dict)
    age_buckets: Dict[str, int] = field(default_factory=dict)
    oldest_open: List[Dict[str, Any]] = field(default_factory=list)
    assignee_breakdown: List[Dict[str, Any]] = field(default_factory=list)
    reporter_breakdown: List[Dict[str, Any]] = field(default_factory=list)
    epic_progress: List[Dict[str, Any]] = field(default_factory=list)
    sprint_progress: List[Dict[str, Any]] = field(default_factory=list)
    estimation_accuracy: List[Dict[str, Any]] = field(default_factory=list)
    reporter_assignee_matrix: List[Dict[str, Any]] = field(default_factory=list)

    # Full table
    all_tickets_json: str = "[]"
    all_headers: List[str] = field(default_factory=list)

    # Source type
    source_type: str = "jira"

    # ServiceNow-specific metrics
    sla_compliance_pct: float = 0.0
    sla_met_count: int = 0
    sla_missed_count: int = 0
    contact_type_counts: Dict[str, int] = field(default_factory=dict)
    category_counts: Dict[str, int] = field(default_factory=dict)
    subcategory_counts: Dict[str, int] = field(default_factory=dict)
    assignment_group_counts: Dict[str, int] = field(default_factory=dict)
    assignment_group_breakdown: List[Dict[str, Any]] = field(default_factory=list)
    escalation_counts: Dict[str, int] = field(default_factory=dict)
    avg_reassignment_count: float = 0.0
    avg_reopen_count: float = 0.0
    sla_by_priority: Dict[str, Dict[str, int]] = field(default_factory=dict)

    # Issue themes (from short_description clustering)
    issue_themes: List[Dict[str, Any]] = field(default_factory=list)


def _cluster_descriptions(tickets: List[JiraTicket], max_themes: int = 25) -> List[Dict[str, Any]]:
    """Cluster short_description values into themes using keyword extraction.

    Groups similar descriptions by normalising text and finding common 2-3 word
    phrases (ngrams).  Returns top themes with counts and sample descriptions.
    """
    descriptions = [t.summary for t in tickets if t.summary and t.summary.strip()]
    if not descriptions:
        return []

    # Stop words to ignore
    stop = {"a", "an", "the", "to", "of", "for", "in", "on", "is", "it", "and",
            "or", "be", "as", "at", "by", "was", "are", "has", "had", "not", "but",
            "from", "with", "this", "that", "i", "we", "they", "you", "my", "our",
            "do", "so", "if", "no", "up", "out", "can", "all", "been", "have",
            "will", "its", "did", "get", "got", "need", "needs", "needed",
            "please", "hi", "hello", "thanks", "thank", "would", "could",
            "should", "re", "fw", "fwd", "per", "via", "ie", "eg", "etc",
            "also", "just", "about", "their", "them", "there", "these", "those",
            "when", "what", "which", "who", "how", "very", "some", "any", "more",
            "other", "into", "over", "only", "than", "then", "each", "after",
            "before", "between", "same", "being", "both", "does", "done",
            "going", "make", "may", "new", "now", "one", "two", "use", "way"}

    def tokenise(text: str) -> List[str]:
        return [w for w in re.findall(r'[a-z0-9]+', text.lower()) if w not in stop and len(w) > 1]

    # Count bigrams and trigrams
    ngram_counter: Dict[str, int] = defaultdict(int)
    ngram_examples: Dict[str, List[str]] = defaultdict(list)

    for desc in descriptions:
        words = tokenise(desc)
        seen_in_desc = set()
        for n in (3, 2):
            for i in range(len(words) - n + 1):
                ngram = " ".join(words[i:i + n])
                if ngram not in seen_in_desc:
                    seen_in_desc.add(ngram)
                    ngram_counter[ngram] += 1
                    if len(ngram_examples[ngram]) < 3:
                        ngram_examples[ngram].append(desc[:100])

    # Filter: keep ngrams that appear in at least 3 tickets
    candidates = [(ng, cnt) for ng, cnt in ngram_counter.items() if cnt >= 3]
    candidates.sort(key=lambda x: -x[1])

    # Remove overlapping ngrams (if a trigram covers a bigram, prefer trigram)
    used_themes: List[Dict[str, Any]] = []
    covered_bigrams: set = set()
    for ng, cnt in candidates:
        words = ng.split()
        if len(words) == 2 and ng in covered_bigrams:
            continue
        used_themes.append({
            "theme": ng.title(),
            "count": cnt,
            "examples": ngram_examples[ng][:3],
        })
        # Mark constituent bigrams as covered
        if len(words) == 3:
            covered_bigrams.add(" ".join(words[:2]))
            covered_bigrams.add(" ".join(words[1:]))
        if len(used_themes) >= max_themes:
            break

    return used_themes


def compute_dashboard_data(tickets: List[JiraTicket], stale_days: int = 14,
                           now: Optional[datetime] = None,
                           config: Optional[SourceConfig] = None) -> DashboardData:
    """Compute all dashboard metrics from parsed tickets."""
    if now is None:
        now = datetime.now()
    if config is None:
        config = _jira_config()

    d = DashboardData()
    d.source_type = config.name
    d.total_tickets = len(tickets)

    open_ages: List[float] = []
    all_resolution_days: List[float] = []
    resolution_times_by_type: Dict[str, List[float]] = defaultdict(list)
    resolution_times_by_priority: Dict[str, List[float]] = defaultdict(list)
    bucket_labels = ["< 7d", "7–14d", "14–30d", "30–60d", "60–90d", "90d+"]
    d.age_buckets = {b: 0 for b in bucket_labels}

    # Epic/Sprint tracking
    epic_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "open": 0, "closed": 0, "story_points": 0.0,
    })
    sprint_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "open": 0, "closed": 0, "story_points": 0.0,
    })

    # Estimation accuracy tracking
    estimate_by_type: Dict[str, Dict[str, List[int]]] = defaultdict(lambda: {
        "estimated": [], "actual": [],
    })

    # Reporter-Assignee flow
    ra_flow: Dict[Tuple[str, str], int] = defaultdict(int)

    # Component/Label counting
    component_counter: Dict[str, int] = defaultdict(int)
    label_counter: Dict[str, int] = defaultdict(int)

    # ServiceNow-specific accumulators
    sla_met = 0
    sla_missed = 0
    category_counter: Dict[str, int] = defaultdict(int)
    subcategory_counter: Dict[str, int] = defaultdict(int)
    assignment_group_counter: Dict[str, int] = defaultdict(int)
    contact_type_counter: Dict[str, int] = defaultdict(int)
    escalation_counter: Dict[str, int] = defaultdict(int)
    reassignment_values: List[int] = []
    reopen_values: List[int] = []
    sla_by_pri: Dict[str, Dict[str, int]] = defaultdict(lambda: {"met": 0, "missed": 0})

    # Assignment group stats (for breakdown table)
    ag_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "open": 0, "closed": 0, "sla_met": 0, "sla_missed": 0,
    })

    for t in tickets:
        is_open = _is_open(t.status, config)
        if is_open:
            d.open_tickets += 1
        else:
            d.closed_tickets += 1

        # Blocked
        if is_open and _is_blocked(t.status, config):
            d.blocked_tickets += 1

        # Unassigned
        if is_open and t.assignee in (config.default_unassigned, ""):
            d.unassigned_tickets += 1

        # Story points
        if t.story_points is not None:
            d.total_story_points += t.story_points
            if is_open:
                d.open_story_points += t.story_points

        # Status
        status_display = t.status or "Unknown"
        d.status_counts[status_display] = d.status_counts.get(status_display, 0) + 1

        # Assignee (open tickets only for workload)
        if is_open:
            d.assignee_counts[t.assignee] = d.assignee_counts.get(t.assignee, 0) + 1

        # Priority
        if t.priority:
            d.priority_counts[t.priority] = d.priority_counts.get(t.priority, 0) + 1

        # Issue type
        if t.issue_type:
            d.type_counts[t.issue_type] = d.type_counts.get(t.issue_type, 0) + 1

        # Components
        for comp in _split_csv_field(t.components):
            component_counter[comp] += 1

        # Labels
        for lbl in _split_csv_field(t.labels):
            label_counter[lbl] += 1

        # Created/Resolved trend (monthly)
        if t.created:
            month_key = t.created.strftime("%Y-%m")
            d.created_by_month[month_key] = d.created_by_month.get(month_key, 0) + 1
        if t.resolved:
            month_key = t.resolved.strftime("%Y-%m")
            d.resolved_by_month[month_key] = d.resolved_by_month.get(month_key, 0) + 1

        # Age of open tickets
        if is_open and t.created:
            age_days = (now - t.created).total_seconds() / 86400
            open_ages.append(age_days)
            if age_days < 7:
                d.age_buckets["< 7d"] += 1
            elif age_days < 14:
                d.age_buckets["7–14d"] += 1
            elif age_days < 30:
                d.age_buckets["14–30d"] += 1
            elif age_days < 60:
                d.age_buckets["30–60d"] += 1
            elif age_days < 90:
                d.age_buckets["60–90d"] += 1
            else:
                d.age_buckets["90d+"] += 1

        # Overdue
        if is_open and t.due_date and t.due_date < now:
            d.overdue_tickets += 1

        # Staleness
        last_activity = t.last_comment_date or t.updated
        days_since = None
        if last_activity:
            days_since = (now - last_activity).total_seconds() / 86400
        if is_open:
            if days_since is not None and days_since > stale_days:
                d.stale_tickets += 1
            elif days_since is None and t.created:
                created_days = (now - t.created).total_seconds() / 86400
                if created_days > stale_days:
                    d.stale_tickets += 1

        # Staleness table row (all open tickets)
        if is_open:
            d.staleness_rows.append({
                "key": t.key,
                "summary": t.summary[:80],
                "reporter": t.reporter or "Unknown",
                "assignee": t.assignee,
                "status": t.status,
                "last_comment_date": last_activity.strftime("%Y-%m-%d") if last_activity else "—",
                "days_since": round(days_since, 1) if days_since is not None else 999,
                "comment_preview": (t.last_comment_text[:60] + "…") if len(t.last_comment_text) > 60 else t.last_comment_text or "—",
            })

        # Resolution time
        if not is_open and t.created and t.resolved:
            res_days = (t.resolved - t.created).total_seconds() / 86400
            all_resolution_days.append(res_days)
            itype = t.issue_type or "Unknown"
            resolution_times_by_type[itype].append(res_days)
            if t.priority:
                resolution_times_by_priority[t.priority].append(res_days)

        # Epic progress
        if t.epic_link:
            epic_stats[t.epic_link]["total"] += 1
            if is_open:
                epic_stats[t.epic_link]["open"] += 1
            else:
                epic_stats[t.epic_link]["closed"] += 1
            if t.story_points:
                epic_stats[t.epic_link]["story_points"] += t.story_points

        # Sprint progress
        if t.sprint:
            sprint_stats[t.sprint]["total"] += 1
            if is_open:
                sprint_stats[t.sprint]["open"] += 1
            else:
                sprint_stats[t.sprint]["closed"] += 1
            if t.story_points:
                sprint_stats[t.sprint]["story_points"] += t.story_points

        # Estimation accuracy
        if t.original_estimate_secs is not None and t.time_spent_secs is not None:
            itype = t.issue_type or "Unknown"
            estimate_by_type[itype]["estimated"].append(t.original_estimate_secs)
            estimate_by_type[itype]["actual"].append(t.time_spent_secs)

        # Reporter-Assignee flow
        reporter = t.reporter or "Unknown"
        ra_flow[(reporter, t.assignee)] += 1

        # --- ServiceNow-specific per-ticket ---
        if config.has_sla and t.made_sla is not None:
            if t.made_sla:
                sla_met += 1
            else:
                sla_missed += 1
            pri = t.priority or "Unknown"
            if t.made_sla:
                sla_by_pri[pri]["met"] += 1
            else:
                sla_by_pri[pri]["missed"] += 1

        if config.has_categories and t.category:
            category_counter[t.category] += 1
        if config.has_categories and t.subcategory:
            subcategory_counter[t.subcategory] += 1

        if config.has_assignment_groups and t.assignment_group:
            assignment_group_counter[t.assignment_group] += 1
            ag_stats[t.assignment_group]["total"] += 1
            if is_open:
                ag_stats[t.assignment_group]["open"] += 1
            else:
                ag_stats[t.assignment_group]["closed"] += 1
            if t.made_sla is True:
                ag_stats[t.assignment_group]["sla_met"] += 1
            elif t.made_sla is False:
                ag_stats[t.assignment_group]["sla_missed"] += 1

        if config.has_contact_type and t.contact_type:
            contact_type_counter[t.contact_type] += 1

        if config.has_escalation and t.escalation:
            escalation_counter[t.escalation] += 1

        if config.has_reassignment and t.reassignment_count is not None:
            reassignment_values.append(t.reassignment_count)
        if config.has_reassignment and t.reopen_count is not None:
            reopen_values.append(t.reopen_count)

    # --- Aggregations ---

    # Summary values
    d.avg_age_open_days = round(sum(open_ages) / len(open_ages), 1) if open_ages else 0.0
    d.resolution_rate = round((d.closed_tickets / d.total_tickets * 100), 1) if d.total_tickets else 0.0
    d.avg_resolution_days = round(sum(all_resolution_days) / len(all_resolution_days), 1) if all_resolution_days else 0.0
    d.total_story_points = round(d.total_story_points, 1)
    d.open_story_points = round(d.open_story_points, 1)

    # Resolution by type
    for itype, times in resolution_times_by_type.items():
        d.avg_resolution_by_type[itype] = round(sum(times) / len(times), 1)

    # Resolution by priority
    for pri, times in resolution_times_by_priority.items():
        d.avg_resolution_by_priority[pri] = round(sum(times) / len(times), 1)

    # Component/Label counts (sorted by count desc)
    d.component_counts = dict(sorted(component_counter.items(), key=lambda x: -x[1]))
    d.label_counts = dict(sorted(label_counter.items(), key=lambda x: -x[1]))

    # Ensure created_by_month and resolved_by_month cover the same date range
    all_months = sorted(set(list(d.created_by_month.keys()) + list(d.resolved_by_month.keys())))
    for m in all_months:
        d.created_by_month.setdefault(m, 0)
        d.resolved_by_month.setdefault(m, 0)
    # Re-sort by month key
    d.created_by_month = dict(sorted(d.created_by_month.items()))
    d.resolved_by_month = dict(sorted(d.resolved_by_month.items()))

    # Sort staleness rows (most stale first)
    d.staleness_rows.sort(key=lambda r: -r["days_since"])

    # Top 10 oldest open
    open_with_age = []
    for t in tickets:
        if _is_open(t.status, config) and t.created:
            age = (now - t.created).total_seconds() / 86400
            open_with_age.append({
                "key": t.key,
                "summary": t.summary[:60],
                "assignee": t.assignee,
                "status": t.status,
                "age_days": round(age, 1),
                "created": t.created.strftime("%Y-%m-%d"),
            })
    open_with_age.sort(key=lambda r: -r["age_days"])
    d.oldest_open = open_with_age[:10]

    # Assignee breakdown (with story points)
    assignee_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "open": 0, "closed": 0, "overdue": 0, "stale": 0,
        "open_age_sum": 0.0, "open_count_for_age": 0, "story_points": 0.0,
    })
    reporter_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "open": 0, "closed": 0, "overdue": 0,
    })
    for t in tickets:
        is_open_t = _is_open(t.status, config)
        a = t.assignee
        assignee_stats[a]["total"] += 1
        if t.story_points:
            assignee_stats[a]["story_points"] += t.story_points
        if is_open_t:
            assignee_stats[a]["open"] += 1
            if t.created:
                age = (now - t.created).total_seconds() / 86400
                assignee_stats[a]["open_age_sum"] += age
                assignee_stats[a]["open_count_for_age"] += 1
            if t.due_date and t.due_date < now:
                assignee_stats[a]["overdue"] += 1
            last_act = t.last_comment_date or t.updated
            if last_act:
                ds = (now - last_act).total_seconds() / 86400
                if ds > stale_days:
                    assignee_stats[a]["stale"] += 1
            elif t.created:
                if (now - t.created).total_seconds() / 86400 > stale_days:
                    assignee_stats[a]["stale"] += 1
        else:
            assignee_stats[a]["closed"] += 1
        r = t.reporter or "Unknown"
        reporter_stats[r]["total"] += 1
        if is_open_t:
            reporter_stats[r]["open"] += 1
            if t.due_date and t.due_date < now:
                reporter_stats[r]["overdue"] += 1
        else:
            reporter_stats[r]["closed"] += 1

    for name, s in sorted(assignee_stats.items(), key=lambda x: -x[1]["total"]):
        avg_age = round(s["open_age_sum"] / s["open_count_for_age"], 1) if s["open_count_for_age"] else 0.0
        d.assignee_breakdown.append({
            "assignee": name, "total": s["total"], "open": s["open"],
            "closed": s["closed"], "avg_age": avg_age,
            "overdue": s["overdue"], "stale": s["stale"],
            "story_points": round(s["story_points"], 1),
        })
    for name, s in sorted(reporter_stats.items(), key=lambda x: -x[1]["total"]):
        d.reporter_breakdown.append({
            "reporter": name, "total": s["total"], "open": s["open"],
            "closed": s["closed"], "overdue": s["overdue"],
        })

    # Epic progress
    for epic, s in sorted(epic_stats.items(), key=lambda x: -x[1]["total"]):
        pct = round(s["closed"] / s["total"] * 100, 1) if s["total"] else 0
        d.epic_progress.append({
            "epic": epic, "total": s["total"], "open": s["open"],
            "closed": s["closed"], "pct_done": pct,
            "story_points": round(s["story_points"], 1),
        })

    # Sprint progress
    for sprint, s in sorted(sprint_stats.items(), key=lambda x: -x[1]["total"]):
        pct = round(s["closed"] / s["total"] * 100, 1) if s["total"] else 0
        d.sprint_progress.append({
            "sprint": sprint, "total": s["total"], "open": s["open"],
            "closed": s["closed"], "pct_done": pct,
            "story_points": round(s["story_points"], 1),
        })

    # Estimation accuracy
    for itype, data_est in sorted(estimate_by_type.items()):
        estimated = data_est["estimated"]
        actual = data_est["actual"]
        avg_est = sum(estimated) / len(estimated) if estimated else 0
        avg_act = sum(actual) / len(actual) if actual else 0
        accuracy = round((avg_act / avg_est * 100), 1) if avg_est > 0 else 0
        d.estimation_accuracy.append({
            "type": itype,
            "count": len(estimated),
            "avg_estimated": format_duration(int(avg_est)),
            "avg_actual": format_duration(int(avg_act)),
            "accuracy_pct": accuracy,
        })

    # Reporter-Assignee matrix (top 20)
    ra_sorted = sorted(ra_flow.items(), key=lambda x: -x[1])[:20]
    for (reporter, assignee), count in ra_sorted:
        d.reporter_assignee_matrix.append({
            "reporter": reporter, "assignee": assignee, "count": count,
        })

    # --- ServiceNow-specific aggregations ---
    if config.has_sla:
        sla_total = sla_met + sla_missed
        d.sla_met_count = sla_met
        d.sla_missed_count = sla_missed
        d.sla_compliance_pct = round(sla_met / sla_total * 100, 1) if sla_total else 0.0
        for pri, counts in sorted(sla_by_pri.items()):
            d.sla_by_priority[pri] = dict(counts)

    if config.has_categories:
        d.category_counts = dict(sorted(category_counter.items(), key=lambda x: -x[1]))
        d.subcategory_counts = dict(sorted(subcategory_counter.items(), key=lambda x: -x[1]))

    if config.has_assignment_groups:
        d.assignment_group_counts = dict(sorted(assignment_group_counter.items(), key=lambda x: -x[1]))
        for ag_name, s in sorted(ag_stats.items(), key=lambda x: -x[1]["total"]):
            sla_t = s["sla_met"] + s["sla_missed"]
            sla_pct = round(s["sla_met"] / sla_t * 100, 1) if sla_t else 0.0
            d.assignment_group_breakdown.append({
                "group": ag_name, "total": s["total"], "open": s["open"],
                "closed": s["closed"], "sla_pct": sla_pct,
            })

    if config.has_contact_type:
        d.contact_type_counts = dict(sorted(contact_type_counter.items(), key=lambda x: -x[1]))

    if config.has_escalation:
        d.escalation_counts = dict(sorted(escalation_counter.items(), key=lambda x: -x[1]))

    if config.has_reassignment:
        d.avg_reassignment_count = round(sum(reassignment_values) / len(reassignment_values), 1) if reassignment_values else 0.0
        d.avg_reopen_count = round(sum(reopen_values) / len(reopen_values), 1) if reopen_values else 0.0

    # Issue themes from short_description clustering
    d.issue_themes = _cluster_descriptions(tickets)

    # Full ticket table data
    all_rows = []
    all_headers_set: Dict[str, None] = {}
    for t in tickets:
        for h in t.raw_fields:
            if h not in all_headers_set:
                all_headers_set[h] = None
    d.all_headers = list(all_headers_set.keys())

    for t in tickets:
        row_data = {}
        for h in d.all_headers:
            row_data[h] = t.raw_fields.get(h, "")
        all_rows.append(row_data)
    d.all_tickets_json = json.dumps(all_rows, default=str).replace("</", "<\\/")

    return d


# ---------------------------------------------------------------------------
# HTML Generation
# ---------------------------------------------------------------------------

_SN_PREFIX_MAP = {
    "INC": "Incident", "CHG": "Change", "REQ": "Request", "PRB": "Problem",
    "RITM": "Request Item", "TASK": "Task", "SCTASK": "Catalog Task",
}


def _auto_title(tickets: List[JiraTicket], user_title: Optional[str],
                config: Optional[SourceConfig] = None) -> str:
    if user_title:
        return user_title
    if not tickets:
        default = "Dashboard"
        if config:
            default = f"{config.display_name} Dashboard"
        return default

    if config and config.name == "servicenow":
        # Detect SN ticket prefixes
        prefix_counts: Dict[str, int] = defaultdict(int)
        for t in tickets:
            if t.key:
                prefix = re.match(r"^([A-Z]+)", t.key)
                if prefix:
                    prefix_counts[prefix.group(1)] += 1
        if prefix_counts:
            top_prefix = max(prefix_counts, key=prefix_counts.get)  # type: ignore[arg-type]
            label = _SN_PREFIX_MAP.get(top_prefix, top_prefix)
            return f"{label} Dashboard"
        return "ServiceNow Dashboard"

    # Jira: group by project key
    projects = set()
    for t in tickets:
        if t.key and "-" in t.key:
            projects.add(t.key.split("-")[0])
        elif t.project:
            projects.add(t.project)
    if projects:
        return ", ".join(sorted(projects)) + " Dashboard"
    return "Jira Dashboard"


def generate_html(tickets: List[JiraTicket], data: DashboardData,
                  title: str = "Jira Dashboard", source_file: str = "",
                  stale_days: int = 14,
                  config: Optional[SourceConfig] = None) -> str:
    """Generate the complete self-contained HTML dashboard."""
    if config is None:
        config = _jira_config()
    is_sn = config.name == "servicenow"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Prepare chart data as JSON
    status_data = json.dumps(dict(sorted(data.status_counts.items(), key=lambda x: -x[1])))
    assignee_data = json.dumps(dict(sorted(data.assignee_counts.items(), key=lambda x: -x[1])[:15]))
    priority_data = json.dumps(data.priority_counts)
    type_data = json.dumps(data.type_counts)
    component_data = json.dumps(dict(list(data.component_counts.items())[:15]))
    label_data = json.dumps(dict(list(data.label_counts.items())[:15]))
    created_month_json = json.dumps(data.created_by_month)
    resolved_month_json = json.dumps(data.resolved_by_month)
    staleness_json = json.dumps(data.staleness_rows)
    resolution_json = json.dumps(data.avg_resolution_by_type)
    resolution_priority_json = json.dumps(data.avg_resolution_by_priority)
    age_buckets_json = json.dumps(data.age_buckets)
    oldest_json = json.dumps(data.oldest_open)
    assignee_breakdown_json = json.dumps(data.assignee_breakdown)
    reporter_breakdown_json = json.dumps(data.reporter_breakdown)
    epic_json = json.dumps(data.epic_progress)
    sprint_json = json.dumps(data.sprint_progress)
    estimation_json = json.dumps(data.estimation_accuracy)
    ra_matrix_json = json.dumps(data.reporter_assignee_matrix)
    headers_json = json.dumps(data.all_headers)

    issue_themes_json = json.dumps(data.issue_themes)

    # ServiceNow-specific JSON data
    category_data_json = json.dumps(data.subcategory_counts) if is_sn else "{}"
    contact_type_data_json = json.dumps(data.contact_type_counts) if is_sn else "{}"
    escalation_data_json = json.dumps(data.escalation_counts) if is_sn else "{}"
    assignment_group_json = json.dumps(data.assignment_group_breakdown) if is_sn else "[]"
    sla_by_priority_json = json.dumps(data.sla_by_priority) if is_sn else "{}"

    # Inject config-appropriate JS colour maps
    status_colours_json = json.dumps(config.status_colours)
    priority_colours_json = json.dumps(config.priority_colours)
    type_colours_json = json.dumps(config.type_colours)

    title_escaped = html.escape(title)
    source_escaped = html.escape(source_file)

    # 8th summary card: Story Points (Jira) or SLA Compliance % (ServiceNow)
    if is_sn:
        eighth_card_value = f"{data.sla_compliance_pct}%"
        eighth_card_label = "SLA Compliance"
        eighth_card_sub = f"{data.sla_met_count} met / {data.sla_missed_count} missed"
        eighth_card_class = "success" if data.sla_compliance_pct >= 90 else "warning" if data.sla_compliance_pct >= 70 else "danger" if (data.sla_met_count + data.sla_missed_count) > 0 else ""
    else:
        sp_display = f"{data.total_story_points}" if data.total_story_points else "—"
        sp_sub = f"{data.open_story_points} open" if data.total_story_points else "no data"
        eighth_card_value = sp_display
        eighth_card_label = "Story Points"
        eighth_card_sub = sp_sub
        eighth_card_class = ""

    # --- Conditional HTML sections ---

    # Jira-only sections
    epic_section_html = ""
    sprint_section_html = ""
    estimation_section_html = ""
    component_chart_html = ""
    label_chart_html = ""
    if not is_sn:
        epic_section_html = """
<!-- Epic Progress -->
<div class="section">
    <h2>Epic Progress</h2>
    <div id="epic-progress"></div>
</div>"""
        sprint_section_html = """
<!-- Sprint Progress -->
<div class="section">
    <h2>Sprint Progress</h2>
    <div id="sprint-progress"></div>
</div>"""
        estimation_section_html = """
<!-- Estimation Accuracy -->
<div class="section">
    <h2>Estimation Accuracy</h2>
    <div id="estimation-accuracy"></div>
</div>"""
        component_chart_html = """
    <div class="chart-container">
        <h3>Components</h3>
        <div id="chart-components"></div>
    </div>
    <div class="chart-container">
        <h3>Labels</h3>
        <div id="chart-labels"></div>
    </div>"""

    # ServiceNow-only sections
    sn_category_section = ""
    sn_assignment_group_section = ""
    sn_contact_type_section = ""
    sn_escalation_section = ""
    sn_sla_priority_section = ""
    sn_extra_cards = ""
    if is_sn:
        sn_category_section = """
<!-- Subcategory Breakdown -->
<div class="section">
    <h2>Subcategory Breakdown</h2>
    <div id="chart-categories"></div>
</div>"""
        sn_assignment_group_section = """
<!-- Assignment Group Breakdown -->
<div class="section">
    <h2>Assignment Group Breakdown</h2>
    <div id="assignment-group-table"></div>
</div>"""
        sn_contact_type_section = """
<!-- Contact Type Distribution -->
<div class="charts-grid">
    <div class="chart-container">
        <h3>Contact Type Distribution</h3>
        <div id="chart-contact-type"></div>
    </div>
    <div class="chart-container">
        <h3>Escalation Analysis</h3>
        <div id="chart-escalation"></div>
    </div>
</div>"""
        sn_sla_priority_section = """
<!-- SLA by Priority -->
<div class="section">
    <h2>SLA Compliance by Priority</h2>
    <div id="sla-priority-chart"></div>
</div>"""
        sn_extra_cards = f"""
    <div class="card">
        <div class="card-value">{data.avg_reassignment_count}</div>
        <div class="card-label">Avg Reassignments</div>
        <div class="card-sub">per ticket</div>
    </div>
    <div class="card">
        <div class="card-value">{data.avg_reopen_count}</div>
        <div class="card-label">Avg Reopens</div>
        <div class="card-sub">per ticket</div>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_escaped}</title>
<style>
:root {{
    --xps-blue: #4A9FD9;
    --xps-blue-light: #6BB3E3;
    --xps-blue-dark: #3B8AC4;
    --xps-charcoal: #2D3539;
    --xps-charcoal-light: #3D474C;
    --xps-charcoal-dark: #1E2528;
    --xps-dark-bg: #161B1E;
    --xps-card-bg: #232A2E;
    --xps-text: #F0F2F3;
    --xps-text-muted: #8A9499;
    --xps-border: rgba(255, 255, 255, 0.1);
    --xps-success: #4CAF50;
    --xps-warning: #FF9800;
    --xps-danger: #F44336;
}}
[data-theme="light"] {{
    --xps-charcoal: #E8EAEC;
    --xps-charcoal-light: #F5F6F7;
    --xps-charcoal-dark: #D0D4D8;
    --xps-dark-bg: #F0F2F4;
    --xps-card-bg: #FFFFFF;
    --xps-text: #1E2528;
    --xps-text-muted: #5A6469;
    --xps-border: rgba(0, 0, 0, 0.1);
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
    background: var(--xps-dark-bg);
    color: var(--xps-text);
    line-height: 1.5;
}}
.header {{
    position: sticky; top: 0; z-index: 100;
    background: var(--xps-charcoal-dark);
    border-bottom: 2px solid var(--xps-blue);
    padding: 12px 24px;
    display: flex; justify-content: space-between; align-items: center;
    flex-wrap: wrap; gap: 8px;
}}
.header h1 {{ font-size: 1.3rem; font-weight: 600; }}
.header-meta {{ color: var(--xps-text-muted); font-size: 0.8rem; }}
.theme-toggle {{
    background: var(--xps-charcoal); border: 1px solid var(--xps-border);
    color: var(--xps-text); padding: 6px 14px; border-radius: 6px; cursor: pointer;
    font-size: 0.85rem;
}}
.theme-toggle:hover {{ background: var(--xps-blue); color: #fff; }}
.container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }}
.card {{
    background: var(--xps-card-bg); border: 1px solid var(--xps-border);
    border-radius: 10px; padding: 16px; text-align: center;
}}
.card-value {{ font-size: 1.8rem; font-weight: 700; color: var(--xps-blue); }}
.card-label {{ font-size: 0.8rem; color: var(--xps-text-muted); margin-top: 4px; }}
.card-sub {{ font-size: 0.7rem; color: var(--xps-text-muted); }}
.card.danger .card-value {{ color: var(--xps-danger); }}
.card.warning .card-value {{ color: var(--xps-warning); }}
.card.success .card-value {{ color: var(--xps-success); }}
.section {{
    background: var(--xps-card-bg); border: 1px solid var(--xps-border);
    border-radius: 10px; padding: 24px; margin-bottom: 24px;
}}
.section h2 {{
    font-size: 1.1rem; font-weight: 600; margin-bottom: 16px;
    padding-bottom: 8px; border-bottom: 1px solid var(--xps-border);
    color: var(--xps-blue-light);
}}
.charts-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 24px; margin-bottom: 24px; }}
.chart-container {{
    background: var(--xps-card-bg); border: 1px solid var(--xps-border);
    border-radius: 10px; padding: 20px;
}}
.chart-container h3 {{ font-size: 0.95rem; margin-bottom: 12px; color: var(--xps-text); }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
th {{
    text-align: left; padding: 10px 12px; background: var(--xps-charcoal-dark);
    color: var(--xps-text-muted); font-weight: 600; cursor: pointer;
    border-bottom: 2px solid var(--xps-border); white-space: nowrap;
    user-select: none;
}}
th:hover {{ color: var(--xps-blue-light); }}
td {{ padding: 8px 12px; border-bottom: 1px solid var(--xps-border); }}
tr:hover {{ background: var(--xps-charcoal); }}
.stale-red {{ background: rgba(244, 67, 54, 0.1); }}
.stale-amber {{ background: rgba(255, 152, 0, 0.1); }}
.stale-green {{ background: rgba(76, 175, 80, 0.1); }}
.search-box {{
    width: 100%; padding: 10px 14px; margin-bottom: 12px;
    background: var(--xps-charcoal); border: 1px solid var(--xps-border);
    border-radius: 6px; color: var(--xps-text); font-size: 0.9rem;
}}
.search-box:focus {{ outline: none; border-color: var(--xps-blue); }}
.stale-filters {{
    display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; align-items: center;
}}
.stale-filters label {{ font-size: 0.8rem; color: var(--xps-text-muted); }}
.stale-filters select, .stale-filters input {{
    padding: 6px 10px; background: var(--xps-charcoal); border: 1px solid var(--xps-border);
    border-radius: 6px; color: var(--xps-text); font-size: 0.8rem; min-width: 140px;
}}
.stale-filters select:focus, .stale-filters input:focus {{ outline: none; border-color: var(--xps-blue); }}
.stale-filter-group {{ display: flex; flex-direction: column; gap: 3px; }}
.pagination {{
    display: flex; justify-content: center; align-items: center; gap: 8px;
    margin-top: 12px; flex-wrap: wrap;
}}
.pagination button {{
    background: var(--xps-charcoal); border: 1px solid var(--xps-border);
    color: var(--xps-text); padding: 6px 12px; border-radius: 4px; cursor: pointer;
    font-size: 0.8rem;
}}
.pagination button:hover {{ background: var(--xps-blue); color: #fff; }}
.pagination button.active {{ background: var(--xps-blue); color: #fff; }}
.pagination span {{ color: var(--xps-text-muted); font-size: 0.8rem; }}
.duration-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 24px; }}
.bar {{ display: flex; align-items: center; margin-bottom: 6px; }}
.bar-label {{ width: 140px; font-size: 0.8rem; color: var(--xps-text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 0; }}
.bar-track {{ flex: 1; height: 22px; background: var(--xps-charcoal); border-radius: 4px; overflow: hidden; position: relative; }}
.bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; display: flex; align-items: center; padding-left: 8px; font-size: 0.75rem; color: #fff; font-weight: 600; }}
.donut-container {{ display: flex; justify-content: center; align-items: center; gap: 20px; flex-wrap: wrap; }}
.donut-legend {{ font-size: 0.8rem; }}
.donut-legend-item {{ display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }}
.donut-legend-swatch {{ width: 12px; height: 12px; border-radius: 3px; flex-shrink: 0; }}
.no-data {{ color: var(--xps-text-muted); text-align: center; padding: 30px; font-style: italic; }}
.progress-bar-bg {{ width: 100%; height: 16px; background: var(--xps-charcoal); border-radius: 4px; overflow: hidden; }}
.progress-bar-fill {{ height: 100%; background: var(--xps-success); border-radius: 4px; transition: width 0.3s; min-width: 2px; }}
.trend-bar-group {{ display: flex; align-items: flex-end; gap: 2px; }}
.trend-bar {{ min-width: 8px; border-radius: 2px 2px 0 0; }}
.trend-container {{ overflow-x: auto; }}
.trend-chart {{ display: flex; align-items: flex-end; gap: 8px; min-height: 150px; padding: 10px 0; }}
.trend-month {{ display: flex; flex-direction: column; align-items: center; gap: 4px; }}
.trend-month-label {{ font-size: 0.65rem; color: var(--xps-text-muted); white-space: nowrap; }}
.trend-legend {{ display: flex; gap: 16px; margin-bottom: 8px; font-size: 0.8rem; }}
.trend-legend-item {{ display: flex; align-items: center; gap: 4px; }}
.trend-legend-swatch {{ width: 12px; height: 12px; border-radius: 2px; }}
.accuracy-good {{ color: var(--xps-success); font-weight: 600; }}
.accuracy-warn {{ color: var(--xps-warning); font-weight: 600; }}
.accuracy-bad {{ color: var(--xps-danger); font-weight: 600; }}
@media (max-width: 768px) {{
    .charts-grid {{ grid-template-columns: 1fr; }}
    .header {{ flex-direction: column; text-align: center; }}
    .cards {{ grid-template-columns: repeat(2, 1fr); }}
}}
@media (max-width: 480px) {{
    .cards {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>

<div class="header">
    <div>
        <h1>{title_escaped}</h1>
        <div class="header-meta">Source: {source_escaped} &middot; Generated: {generated_at} &middot; {data.total_tickets} tickets</div>
    </div>
    <button class="theme-toggle" onclick="toggleTheme()">Toggle Theme</button>
</div>

<div class="container">

<!-- Summary Cards (8) -->
<div class="cards">
    <div class="card">
        <div class="card-value">{data.total_tickets}</div>
        <div class="card-label">Total Tickets</div>
        <div class="card-sub">{data.open_tickets} open / {data.closed_tickets} closed</div>
    </div>
    <div class="card {"success" if data.resolution_rate >= 80 else "warning" if data.resolution_rate >= 50 else ""}">
        <div class="card-value">{data.resolution_rate}%</div>
        <div class="card-label">Resolution Rate</div>
        <div class="card-sub">{data.closed_tickets} resolved</div>
    </div>
    <div class="card">
        <div class="card-value">{data.avg_age_open_days}</div>
        <div class="card-label">Avg Age (Open)</div>
        <div class="card-sub">days</div>
    </div>
    <div class="card">
        <div class="card-value">{data.avg_resolution_days}</div>
        <div class="card-label">Avg Resolution</div>
        <div class="card-sub">days to close</div>
    </div>
    <div class="card {"danger" if data.overdue_tickets else ""}">
        <div class="card-value">{data.overdue_tickets}</div>
        <div class="card-label">Overdue</div>
        <div class="card-sub">past due date</div>
    </div>
    <div class="card {"warning" if data.stale_tickets else ""}">
        <div class="card-value">{data.stale_tickets}</div>
        <div class="card-label">Stale</div>
        <div class="card-sub">no activity {stale_days}+ days</div>
    </div>
    <div class="card {"warning" if data.unassigned_tickets else ""}">
        <div class="card-value">{data.unassigned_tickets}</div>
        <div class="card-label">Unassigned</div>
        <div class="card-sub">open, no owner</div>
    </div>
    <div class="card {eighth_card_class}">
        <div class="card-value">{eighth_card_value}</div>
        <div class="card-label">{eighth_card_label}</div>
        <div class="card-sub">{eighth_card_sub}</div>
    </div>
{sn_extra_cards}
</div>

<!-- Created vs Resolved Trend -->
<div class="section">
    <h2>Created vs Resolved Trend</h2>
    <div id="trend-chart"></div>
</div>

<!-- Charts -->
<div class="charts-grid">
    <div class="chart-container">
        <h3>Status Breakdown</h3>
        <div id="chart-status"></div>
    </div>
    <div class="chart-container">
        <h3>Assignee Workload (Open)</h3>
        <div id="chart-assignee"></div>
    </div>
    <div class="chart-container">
        <h3>Priority Distribution</h3>
        <div id="chart-priority"></div>
    </div>
    <div class="chart-container">
        <h3>Issue Type Distribution</h3>
        <div id="chart-type"></div>
    </div>
{component_chart_html}
</div>

<!-- Priority SLA -->
<div class="section">
    <h2>Priority SLA — Avg Resolution Time by Priority</h2>
    <div id="priority-sla-chart"></div>
</div>

{sn_sla_priority_section}
{sn_category_section}
{sn_contact_type_section}
{sn_assignment_group_section}

{epic_section_html}
{sprint_section_html}

<!-- Assignee Breakdown -->
<div class="section">
    <h2>Assignee Breakdown</h2>
    <div id="assignee-breakdown"></div>
</div>

<!-- Common Issue Themes -->
<div class="section">
    <h2>Common Issue Themes</h2>
    <p style="color:var(--xps-muted);font-size:0.85rem;margin-bottom:12px;">Recurring phrases from ticket summaries, grouped by frequency. Click a theme to see example descriptions.</p>
    <div id="issue-themes"></div>
</div>

<!-- Reporter → Assignee Flow -->
<div class="section">
    <h2>Reporter → Assignee Flow</h2>
    <div id="ra-matrix"></div>
</div>

<!-- Staleness Table -->
<div class="section">
    <h2>Staleness Report</h2>
    <div class="stale-filters" id="stale-filters"></div>
    <div id="staleness-table"></div>
</div>

<!-- Duration Metrics -->
<div class="section">
    <h2>Duration Metrics</h2>
    <div class="duration-grid">
        <div>
            <h3 style="font-size:0.95rem;margin-bottom:12px;">Avg Resolution Time by Type</h3>
            <div id="resolution-chart"></div>
        </div>
        <div>
            <h3 style="font-size:0.95rem;margin-bottom:12px;">Open Ticket Age Distribution</h3>
            <div id="age-chart"></div>
        </div>
    </div>
</div>

{estimation_section_html}

<!-- Oldest Open -->
<div class="section">
    <h2>Top 10 Oldest Open Tickets</h2>
    <div id="oldest-table"></div>
</div>

<!-- Full Ticket Table -->
<div class="section">
    <h2>All Tickets</h2>
    <input type="text" class="search-box" id="ticket-search" placeholder="Search tickets..." oninput="filterTickets()">
    <div id="ticket-table"></div>
    <div class="pagination" id="pagination"></div>
</div>

<!-- Reporter Breakdown -->
<div class="section">
    <h2>Reporter Breakdown</h2>
    <div id="reporter-breakdown"></div>
</div>

</div>

<script>
// Data
const statusData = {status_data};
const assigneeData = {assignee_data};
const priorityData = {priority_data};
const typeData = {type_data};
const componentData = {component_data};
const labelData = {label_data};
const createdByMonth = {created_month_json};
const resolvedByMonth = {resolved_month_json};
const stalenessData = {staleness_json};
const resolutionData = {resolution_json};
const resolutionPriorityData = {resolution_priority_json};
const ageBucketsData = {age_buckets_json};
const oldestData = {oldest_json};
const assigneeBreakdown = {assignee_breakdown_json};
const reporterBreakdown = {reporter_breakdown_json};
const epicProgress = {epic_json};
const sprintProgress = {sprint_json};
const estimationData = {estimation_json};
const raMatrix = {ra_matrix_json};
const issueThemes = {issue_themes_json};
const allTickets = {data.all_tickets_json};
const allHeaders = {headers_json};
const sourceType = "{config.name}";
const categoryData = {category_data_json};
const contactTypeData = {contact_type_data_json};
const escalationData = {escalation_data_json};
const assignmentGroupData = {assignment_group_json};
const slaPriorityData = {sla_by_priority_json};

// Theme
function toggleTheme() {{
    const html = document.documentElement;
    html.dataset.theme = html.dataset.theme === 'dark' ? 'light' : 'dark';
}}

// Colour maps (injected from config)
const statusColours = {status_colours_json};
const priorityColours = {priority_colours_json};
const typeColours = {type_colours_json};
const defaultColours = ['#4A9FD9','#4CAF50','#FF9800','#F44336','#9C27B0','#00BCD4','#8BC34A','#FF5722','#607D8B','#E91E63'];
function getColour(map, key, idx) {{
    return (map && map[key]) || defaultColours[idx % defaultColours.length];
}}

// Bar chart renderer
function renderBarChart(containerId, data, colourMap) {{
    const el = document.getElementById(containerId);
    if (!data || Object.keys(data).length === 0) {{ el.innerHTML = '<div class="no-data">No data available</div>'; return; }}
    const max = Math.max(...Object.values(data));
    let html = '';
    let i = 0;
    for (const [label, count] of Object.entries(data)) {{
        const pct = max > 0 ? (count / max * 100) : 0;
        const colour = colourMap ? getColour(colourMap, label, i) : defaultColours[i % defaultColours.length];
        html += `<div class="bar">
            <div class="bar-label" title="${{label}}">${{label}}</div>
            <div class="bar-track">
                <div class="bar-fill" style="width:${{pct}}%;background:${{colour}}">${{count}}</div>
            </div>
        </div>`;
        i++;
    }}
    el.innerHTML = html;
}}

// Donut chart renderer (SVG)
function renderDonut(containerId, data, colourMap) {{
    const el = document.getElementById(containerId);
    if (!data || Object.keys(data).length === 0) {{ el.innerHTML = '<div class="no-data">No data available</div>'; return; }}
    const total = Object.values(data).reduce((a, b) => a + b, 0);
    if (total === 0) {{ el.innerHTML = '<div class="no-data">No data available</div>'; return; }}
    const cx = 80, cy = 80, r = 60, ir = 35;
    let svg = `<svg width="160" height="160" viewBox="0 0 160 160">`;
    let startAngle = -Math.PI / 2;
    let i = 0;
    for (const [label, count] of Object.entries(data)) {{
        const angle = (count / total) * 2 * Math.PI;
        const endAngle = startAngle + angle;
        const large = angle > Math.PI ? 1 : 0;
        const x1o = cx + r * Math.cos(startAngle), y1o = cy + r * Math.sin(startAngle);
        const x2o = cx + r * Math.cos(endAngle), y2o = cy + r * Math.sin(endAngle);
        const x1i = cx + ir * Math.cos(endAngle), y1i = cy + ir * Math.sin(endAngle);
        const x2i = cx + ir * Math.cos(startAngle), y2i = cy + ir * Math.sin(startAngle);
        const colour = getColour(colourMap, label, i);
        svg += `<path d="M${{x1o}},${{y1o}} A${{r}},${{r}} 0 ${{large}} 1 ${{x2o}},${{y2o}} L${{x1i}},${{y1i}} A${{ir}},${{ir}} 0 ${{large}} 0 ${{x2i}},${{y2i}} Z" fill="${{colour}}" opacity="0.85"><title>${{label}}: ${{count}}</title></path>`;
        startAngle = endAngle;
        i++;
    }}
    svg += `<text x="${{cx}}" y="${{cy}}" text-anchor="middle" dominant-baseline="central" fill="var(--xps-text)" font-size="18" font-weight="700">${{total}}</text>`;
    svg += '</svg>';
    let legend = '<div class="donut-legend">';
    i = 0;
    for (const [label, count] of Object.entries(data)) {{
        const colour = getColour(colourMap, label, i);
        const pct = ((count / total) * 100).toFixed(1);
        legend += `<div class="donut-legend-item"><div class="donut-legend-swatch" style="background:${{colour}}"></div>${{label}}: ${{count}} (${{pct}}%)</div>`;
        i++;
    }}
    legend += '</div>';
    el.innerHTML = `<div class="donut-container">${{svg}}${{legend}}</div>`;
}}

// Created vs Resolved trend chart
function renderTrend() {{
    const el = document.getElementById('trend-chart');
    const months = Object.keys(createdByMonth);
    if (months.length === 0) {{ el.innerHTML = '<div class="no-data">No date data available</div>'; return; }}
    const allVals = [...Object.values(createdByMonth), ...Object.values(resolvedByMonth)];
    const max = Math.max(...allVals, 1);
    const barHeight = 120;
    let html = '<div class="trend-legend"><div class="trend-legend-item"><div class="trend-legend-swatch" style="background:#4A9FD9"></div>Created</div><div class="trend-legend-item"><div class="trend-legend-swatch" style="background:#4CAF50"></div>Resolved</div></div>';
    html += '<div class="trend-container"><div class="trend-chart">';
    for (const m of months) {{
        const c = createdByMonth[m] || 0;
        const r = resolvedByMonth[m] || 0;
        const ch = Math.max(c / max * barHeight, 2);
        const rh = Math.max(r / max * barHeight, 2);
        html += `<div class="trend-month"><div class="trend-bar-group"><div class="trend-bar" style="width:14px;height:${{ch}}px;background:#4A9FD9" title="Created: ${{c}}"></div><div class="trend-bar" style="width:14px;height:${{rh}}px;background:#4CAF50" title="Resolved: ${{r}}"></div></div><div class="trend-month-label">${{m}}</div></div>`;
    }}
    html += '</div></div>';
    el.innerHTML = html;
}}

// Progress table renderer (epic/sprint)
function renderProgressTable(containerId, data, nameKey, sortId) {{
    const el = document.getElementById(containerId);
    if (!data || data.length === 0) {{ el.innerHTML = '<div class="no-data">No data available</div>'; return; }}
    const cols = [
        {{ key: nameKey, label: nameKey.charAt(0).toUpperCase() + nameKey.slice(1) }},
        {{ key: 'total', label: 'Total' }},
        {{ key: 'open', label: 'Open' }},
        {{ key: 'closed', label: 'Closed' }},
        {{ key: 'pct_done', label: '% Done' }},
        {{ key: 'story_points', label: 'Points' }},
    ];
    let html = '<table><thead><tr>';
    for (const c of cols) {{
        html += `<th onclick="sortProgressTable('${{sortId}}', ${{JSON.stringify(containerId)}}, ${{JSON.stringify(nameKey)}}, '${{c.key}}')">${{c.label}}${{sortArrow(sortId, c.key)}}</th>`;
    }}
    html += '<th style="min-width:120px">Progress</th></tr></thead><tbody>';
    for (const r of data) {{
        html += `<tr><td>${{r[nameKey]}}</td><td>${{r.total}}</td><td>${{r.open}}</td><td>${{r.closed}}</td><td>${{r.pct_done}}%</td><td>${{r.story_points}}</td><td><div class="progress-bar-bg"><div class="progress-bar-fill" style="width:${{r.pct_done}}%"></div></div></td></tr>`;
    }}
    html += '</tbody></table>';
    el.innerHTML = html;
}}
function sortProgressTable(sortId, containerId, nameKey, colKey) {{
    const dataMap = {{ 'epic': epicProgress, 'sprint': sprintProgress }};
    sortTableData(sortId, dataMap[sortId], colKey);
    renderProgressTable(containerId, dataMap[sortId], nameKey, sortId);
}}

// Assignee breakdown table
const assigneeCols = [
    {{ key: 'assignee', label: 'Assignee' }},
    {{ key: 'total', label: 'Total' }},
    {{ key: 'open', label: 'Open' }},
    {{ key: 'closed', label: 'Closed' }},
    {{ key: 'avg_age', label: 'Avg Age (days)' }},
    {{ key: 'overdue', label: 'Overdue' }},
    {{ key: 'stale', label: 'Stale' }},
    {{ key: 'story_points', label: 'Points' }},
];
function sortAssigneeBreakdown(colKey) {{
    sortTableData('ab', assigneeBreakdown, colKey);
    renderAssigneeBreakdown();
}}
function renderAssigneeBreakdown() {{
    const el = document.getElementById('assignee-breakdown');
    if (!assigneeBreakdown || assigneeBreakdown.length === 0) {{ el.innerHTML = '<div class="no-data">No data available</div>'; return; }}
    let html = '<table><thead><tr>';
    for (const c of assigneeCols) {{
        html += `<th onclick="sortAssigneeBreakdown('${{c.key}}')">${{c.label}}${{sortArrow('ab', c.key)}}</th>`;
    }}
    html += '</tr></thead><tbody>';
    for (const r of assigneeBreakdown) {{
        const overdueClass = r.overdue > 0 ? ' style="color:var(--xps-danger);font-weight:600"' : '';
        const staleClass = r.stale > 0 ? ' style="color:var(--xps-warning);font-weight:600"' : '';
        html += `<tr><td>${{r.assignee}}</td><td>${{r.total}}</td><td>${{r.open}}</td><td>${{r.closed}}</td><td>${{r.avg_age}}</td><td${{overdueClass}}>${{r.overdue}}</td><td${{staleClass}}>${{r.stale}}</td><td>${{r.story_points}}</td></tr>`;
    }}
    html += '</tbody></table>';
    el.innerHTML = html;
}}

// Reporter breakdown table
const reporterCols = [
    {{ key: 'reporter', label: 'Reporter' }},
    {{ key: 'total', label: 'Total' }},
    {{ key: 'open', label: 'Open' }},
    {{ key: 'closed', label: 'Closed' }},
    {{ key: 'overdue', label: 'Overdue' }},
];
function sortReporterBreakdown(colKey) {{
    sortTableData('rb', reporterBreakdown, colKey);
    renderReporterBreakdown();
}}
function renderReporterBreakdown() {{
    const el = document.getElementById('reporter-breakdown');
    if (!reporterBreakdown || reporterBreakdown.length === 0) {{ el.innerHTML = '<div class="no-data">No data available</div>'; return; }}
    let html = '<table><thead><tr>';
    for (const c of reporterCols) {{
        html += `<th onclick="sortReporterBreakdown('${{c.key}}')">${{c.label}}${{sortArrow('rb', c.key)}}</th>`;
    }}
    html += '</tr></thead><tbody>';
    for (const r of reporterBreakdown) {{
        const overdueClass = r.overdue > 0 ? ' style="color:var(--xps-danger);font-weight:600"' : '';
        html += `<tr><td>${{r.reporter}}</td><td>${{r.total}}</td><td>${{r.open}}</td><td>${{r.closed}}</td><td${{overdueClass}}>${{r.overdue}}</td></tr>`;
    }}
    html += '</tbody></table>';
    el.innerHTML = html;
}}

// Reporter-Assignee matrix
const raCols = [
    {{ key: 'reporter', label: 'Reporter' }},
    {{ key: 'assignee', label: 'Assignee' }},
    {{ key: 'count', label: 'Tickets' }},
];
function sortRAMatrix(colKey) {{
    sortTableData('ra', raMatrix, colKey);
    renderRAMatrix();
}}
function renderRAMatrix() {{
    const el = document.getElementById('ra-matrix');
    if (!raMatrix || raMatrix.length === 0) {{ el.innerHTML = '<div class="no-data">No data available</div>'; return; }}
    let html = '<table><thead><tr>';
    for (const c of raCols) {{
        html += `<th onclick="sortRAMatrix('${{c.key}}')">${{c.label}}${{sortArrow('ra', c.key)}}</th>`;
    }}
    html += '</tr></thead><tbody>';
    for (const r of raMatrix) {{
        html += `<tr><td>${{r.reporter}}</td><td>${{r.assignee}}</td><td>${{r.count}}</td></tr>`;
    }}
    html += '</tbody></table>';
    el.innerHTML = html;
}}

// Issue themes
function renderIssueThemes() {{
    const el = document.getElementById('issue-themes');
    if (!issueThemes || issueThemes.length === 0) {{ el.innerHTML = '<div class="no-data">Not enough recurring phrases found</div>'; return; }}
    let html = '<table><thead><tr><th>Theme</th><th>Tickets</th><th style="width:60%">Example Descriptions</th></tr></thead><tbody>';
    for (const t of issueThemes) {{
        const exHtml = t.examples.map(e => `<div style="font-size:0.8rem;color:var(--xps-muted);padding:2px 0;">&bull; ${{e}}</div>`).join('');
        const barW = issueThemes.length > 0 ? (t.count / issueThemes[0].count * 100) : 0;
        html += `<tr><td style="white-space:nowrap;font-weight:600">${{t.theme}}</td>`;
        html += `<td><div style="display:flex;align-items:center;gap:8px;"><span>${{t.count}}</span><div style="background:var(--xps-accent);height:6px;border-radius:3px;width:${{barW}}%;min-width:4px;"></div></div></td>`;
        html += `<td>${{exHtml}}</td></tr>`;
    }}
    html += '</tbody></table>';
    el.innerHTML = html;
}}

// Estimation accuracy table
function renderEstimation() {{
    const el = document.getElementById('estimation-accuracy');
    if (!estimationData || estimationData.length === 0) {{ el.innerHTML = '<div class="no-data">No estimation data available (requires Original Estimate and Time Spent fields)</div>'; return; }}
    let html = '<table><thead><tr><th>Issue Type</th><th>Tickets</th><th>Avg Estimated</th><th>Avg Actual</th><th>Accuracy</th></tr></thead><tbody>';
    for (const r of estimationData) {{
        let cls = 'accuracy-good';
        if (r.accuracy_pct > 150 || r.accuracy_pct < 50) cls = 'accuracy-bad';
        else if (r.accuracy_pct > 120 || r.accuracy_pct < 80) cls = 'accuracy-warn';
        html += `<tr><td>${{r.type}}</td><td>${{r.count}}</td><td>${{r.avg_estimated}}</td><td>${{r.avg_actual}}</td><td class="${{cls}}">${{r.accuracy_pct}}%</td></tr>`;
    }}
    html += '</tbody></table>';
    el.innerHTML = html;
}}

// Sortable table state
const tableSort = {{}};
function sortTableData(tableId, data, colKey) {{
    if (!tableSort[tableId]) tableSort[tableId] = {{ col: null, asc: true }};
    const s = tableSort[tableId];
    if (s.col === colKey) {{ s.asc = !s.asc; }}
    else {{ s.col = colKey; s.asc = true; }}
    data.sort((a, b) => {{
        const va = String(a[colKey] ?? '').toLowerCase();
        const vb = String(b[colKey] ?? '').toLowerCase();
        const na = parseFloat(va), nb = parseFloat(vb);
        if (!isNaN(na) && !isNaN(nb)) return s.asc ? na - nb : nb - na;
        return s.asc ? va.localeCompare(vb) : vb.localeCompare(va);
    }});
}}
function sortArrow(tableId, colKey) {{
    const s = tableSort[tableId];
    if (!s || s.col !== colKey) return '';
    return s.asc ? ' ▲' : ' ▼';
}}

// Staleness table with filters
const staleCols = [
    {{ key: 'key', label: 'Key' }},
    {{ key: 'summary', label: 'Summary' }},
    {{ key: 'reporter', label: 'Reporter' }},
    {{ key: 'assignee', label: 'Assignee' }},
    {{ key: 'status', label: 'Status' }},
    {{ key: 'last_comment_date', label: 'Last Activity' }},
    {{ key: 'days_since', label: 'Days' }},
    {{ key: 'comment_preview', label: 'Comment' }},
];
const staleFilters = {{ key: '', status: '', reporter: '', assignee: '' }};

function buildStaleFilterOptions() {{
    const statuses = [...new Set(stalenessData.map(r => r.status))].sort();
    const reporters = [...new Set(stalenessData.map(r => r.reporter))].sort();
    const assignees = [...new Set(stalenessData.map(r => r.assignee))].sort();
    const el = document.getElementById('stale-filters');
    el.innerHTML = `
        <div class="stale-filter-group"><label>Key</label><input type="text" placeholder="Filter by key…" oninput="staleFilters.key=this.value.toLowerCase();renderStaleness()" /></div>
        <div class="stale-filter-group"><label>Status</label><select onchange="staleFilters.status=this.value;renderStaleness()"><option value="">All</option>${{statuses.map(s => `<option value="${{s}}">${{s}}</option>`).join('')}}</select></div>
        <div class="stale-filter-group"><label>Reporter</label><select onchange="staleFilters.reporter=this.value;renderStaleness()"><option value="">All</option>${{reporters.map(s => `<option value="${{s}}">${{s}}</option>`).join('')}}</select></div>
        <div class="stale-filter-group"><label>Assignee</label><select onchange="staleFilters.assignee=this.value;renderStaleness()"><option value="">All</option>${{assignees.map(s => `<option value="${{s}}">${{s}}</option>`).join('')}}</select></div>
    `;
}}

function getFilteredStaleData() {{
    return stalenessData.filter(r => {{
        if (staleFilters.key && !r.key.toLowerCase().includes(staleFilters.key)) return false;
        if (staleFilters.status && r.status !== staleFilters.status) return false;
        if (staleFilters.reporter && r.reporter !== staleFilters.reporter) return false;
        if (staleFilters.assignee && r.assignee !== staleFilters.assignee) return false;
        return true;
    }});
}}

function sortStaleness(colKey) {{
    sortTableData('stale', stalenessData, colKey);
    renderStaleness();
}}
function renderStaleness() {{
    const el = document.getElementById('staleness-table');
    if (!stalenessData || stalenessData.length === 0) {{ el.innerHTML = '<div class="no-data">No open tickets found</div>'; return; }}
    const filtered = getFilteredStaleData();
    let html = '<table><thead><tr>';
    for (const c of staleCols) {{
        html += `<th onclick="sortStaleness('${{c.key}}')">${{c.label}}${{sortArrow('stale', c.key)}}</th>`;
    }}
    html += '</tr></thead><tbody>';
    if (filtered.length === 0) {{
        html += `<tr><td colspan="${{staleCols.length}}" style="text-align:center;color:var(--xps-text-muted);padding:20px;">No tickets match the current filters</td></tr>`;
    }} else {{
        for (const r of filtered) {{
            let cls = '';
            if (r.days_since > 30) cls = 'stale-red';
            else if (r.days_since > {stale_days}) cls = 'stale-amber';
            else cls = 'stale-green';
            html += `<tr class="${{cls}}"><td>${{r.key}}</td><td>${{r.summary}}</td><td>${{r.reporter}}</td><td>${{r.assignee}}</td><td>${{r.status}}</td><td>${{r.last_comment_date}}</td><td>${{r.days_since}}</td><td>${{r.comment_preview}}</td></tr>`;
        }}
    }}
    html += '</tbody></table>';
    el.innerHTML = html;
}}

// Oldest table
const oldestCols = [
    {{ key: 'key', label: 'Key' }},
    {{ key: 'summary', label: 'Summary' }},
    {{ key: 'assignee', label: 'Assignee' }},
    {{ key: 'status', label: 'Status' }},
    {{ key: 'created', label: 'Created' }},
    {{ key: 'age_days', label: 'Age (days)' }},
];
function sortOldest(colKey) {{
    sortTableData('oldest', oldestData, colKey);
    renderOldest();
}}
function renderOldest() {{
    const el = document.getElementById('oldest-table');
    if (!oldestData || oldestData.length === 0) {{ el.innerHTML = '<div class="no-data">No open tickets found</div>'; return; }}
    let html = '<table><thead><tr>';
    for (const c of oldestCols) {{
        html += `<th onclick="sortOldest('${{c.key}}')">${{c.label}}${{sortArrow('oldest', c.key)}}</th>`;
    }}
    html += '</tr></thead><tbody>';
    for (const r of oldestData) {{
        html += `<tr><td>${{r.key}}</td><td>${{r.summary}}</td><td>${{r.assignee}}</td><td>${{r.status}}</td><td>${{r.created}}</td><td>${{r.age_days}}</td></tr>`;
    }}
    html += '</tbody></table>';
    el.innerHTML = html;
}}

// Full ticket table with pagination and sorting
let filteredTickets = [...allTickets];
let currentPage = 1;
const pageSize = 50;
let sortCol = null;
let sortAsc = true;

function filterTickets() {{
    const q = document.getElementById('ticket-search').value.toLowerCase();
    filteredTickets = allTickets.filter(t => {{
        return Object.values(t).some(v => String(v).toLowerCase().includes(q));
    }});
    currentPage = 1;
    renderTicketTable();
}}

function sortTickets(col) {{
    if (sortCol === col) {{ sortAsc = !sortAsc; }}
    else {{ sortCol = col; sortAsc = true; }}
    filteredTickets.sort((a, b) => {{
        const va = String(a[col] || '').toLowerCase();
        const vb = String(b[col] || '').toLowerCase();
        const na = parseFloat(va), nb = parseFloat(vb);
        if (!isNaN(na) && !isNaN(nb)) return sortAsc ? na - nb : nb - na;
        return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    }});
    renderTicketTable();
}}

function renderTicketTable() {{
    const el = document.getElementById('ticket-table');
    if (!allTickets || allTickets.length === 0) {{ el.innerHTML = '<div class="no-data">No ticket data available</div>'; return; }}
    const cols = allHeaders.slice(0, 12);
    const totalPages = Math.ceil(filteredTickets.length / pageSize);
    const start = (currentPage - 1) * pageSize;
    const pageData = filteredTickets.slice(start, start + pageSize);

    let html = '<div style="overflow-x:auto"><table><thead><tr>';
    for (const c of cols) {{
        const arrow = sortCol === c ? (sortAsc ? ' ▲' : ' ▼') : '';
        html += `<th onclick="sortTickets('${{c.replace(/'/g, "\\\\'")}}')">${{c}}${{arrow}}</th>`;
    }}
    html += '</tr></thead><tbody>';
    for (const row of pageData) {{
        html += '<tr>';
        for (const c of cols) {{
            const val = String(row[c] || '');
            const display = val.length > 80 ? val.substring(0, 77) + '…' : val;
            html += `<td title="${{val.replace(/"/g, '&quot;')}}">${{display}}</td>`;
        }}
        html += '</tr>';
    }}
    html += '</tbody></table></div>';
    el.innerHTML = html;

    const pag = document.getElementById('pagination');
    if (totalPages <= 1) {{ pag.innerHTML = `<span>${{filteredTickets.length}} tickets</span>`; return; }}
    let pagHtml = `<span>${{filteredTickets.length}} tickets</span>`;
    pagHtml += `<button onclick="goPage(1)" ${{currentPage===1?'disabled':''}}>«</button>`;
    pagHtml += `<button onclick="goPage(${{currentPage-1}})" ${{currentPage===1?'disabled':''}}>‹</button>`;
    const startP = Math.max(1, currentPage - 3);
    const endP = Math.min(totalPages, currentPage + 3);
    for (let p = startP; p <= endP; p++) {{
        pagHtml += `<button onclick="goPage(${{p}})" class="${{p===currentPage?'active':''}}">${{p}}</button>`;
    }}
    pagHtml += `<button onclick="goPage(${{currentPage+1}})" ${{currentPage===totalPages?'disabled':''}}>›</button>`;
    pagHtml += `<button onclick="goPage(${{totalPages}})" ${{currentPage===totalPages?'disabled':''}}>»</button>`;
    pag.innerHTML = pagHtml;
}}

function goPage(p) {{
    const totalPages = Math.ceil(filteredTickets.length / pageSize);
    if (p < 1 || p > totalPages) return;
    currentPage = p;
    renderTicketTable();
}}

// ServiceNow-specific renderers
function renderAssignmentGroupTable() {{
    const el = document.getElementById('assignment-group-table');
    if (!el || !assignmentGroupData || assignmentGroupData.length === 0) {{ if(el) el.innerHTML = '<div class="no-data">No data available</div>'; return; }}
    const cols = [
        {{ key: 'group', label: 'Assignment Group' }},
        {{ key: 'total', label: 'Total' }},
        {{ key: 'open', label: 'Open' }},
        {{ key: 'closed', label: 'Closed' }},
        {{ key: 'sla_pct', label: 'SLA %' }},
    ];
    let html = '<table><thead><tr>';
    for (const c of cols) {{
        html += `<th onclick="sortAGTable('${{c.key}}')">${{c.label}}${{sortArrow('ag', c.key)}}</th>`;
    }}
    html += '</tr></thead><tbody>';
    for (const r of assignmentGroupData) {{
        const slaClass = r.sla_pct >= 90 ? 'style="color:var(--xps-success);font-weight:600"' : r.sla_pct >= 70 ? 'style="color:var(--xps-warning);font-weight:600"' : r.sla_pct > 0 ? 'style="color:var(--xps-danger);font-weight:600"' : '';
        html += `<tr><td>${{r.group}}</td><td>${{r.total}}</td><td>${{r.open}}</td><td>${{r.closed}}</td><td ${{slaClass}}>${{r.sla_pct}}%</td></tr>`;
    }}
    html += '</tbody></table>';
    el.innerHTML = html;
}}
function sortAGTable(colKey) {{
    sortTableData('ag', assignmentGroupData, colKey);
    renderAssignmentGroupTable();
}}

function renderSLAByPriority() {{
    const el = document.getElementById('sla-priority-chart');
    if (!el || !slaPriorityData || Object.keys(slaPriorityData).length === 0) {{ if(el) el.innerHTML = '<div class="no-data">No SLA data available</div>'; return; }}
    const labels = Object.keys(slaPriorityData);
    const max = Math.max(...labels.map(l => (slaPriorityData[l].met||0) + (slaPriorityData[l].missed||0)), 1);
    let html = '';
    let i = 0;
    for (const pri of labels) {{
        const met = slaPriorityData[pri].met || 0;
        const missed = slaPriorityData[pri].missed || 0;
        const total = met + missed;
        const pct = total > 0 ? (met / total * 100).toFixed(1) : 0;
        const metW = max > 0 ? (met / max * 100) : 0;
        const missedW = max > 0 ? (missed / max * 100) : 0;
        html += `<div class="bar"><div class="bar-label" title="${{pri}}">${{pri}}</div><div class="bar-track"><div class="bar-fill" style="width:${{metW}}%;background:#4CAF50">${{met}} met</div><div class="bar-fill" style="width:${{missedW}}%;background:#F44336;border-radius:0">${{missed}} missed</div></div></div>`;
        i++;
    }}
    el.innerHTML = html;
}}

// Render all — each in try/catch so one failure doesn't block the rest
function safeRender(name, fn) {{ try {{ fn(); }} catch(e) {{ console.error('Render error in ' + name + ':', e); }} }}
safeRender('trend', () => renderTrend());
safeRender('status', () => renderBarChart('chart-status', statusData, statusColours));
safeRender('assignee-chart', () => renderBarChart('chart-assignee', assigneeData, null));
safeRender('priority', () => renderDonut('chart-priority', priorityData, priorityColours));
safeRender('type', () => renderDonut('chart-type', typeData, typeColours));
safeRender('priority-sla', () => renderBarChart('priority-sla-chart', resolutionPriorityData, priorityColours));

if (sourceType !== 'servicenow') {{
    safeRender('components', () => renderBarChart('chart-components', componentData, null));
    safeRender('labels', () => renderBarChart('chart-labels', labelData, null));
    safeRender('epic', () => renderProgressTable('epic-progress', epicProgress, 'epic', 'epic'));
    safeRender('sprint', () => renderProgressTable('sprint-progress', sprintProgress, 'sprint', 'sprint'));
    safeRender('estimation', () => renderEstimation());
}}

if (sourceType === 'servicenow') {{
    safeRender('categories', () => renderBarChart('chart-categories', categoryData, null));
    safeRender('contact-type', () => renderDonut('chart-contact-type', contactTypeData, null));
    safeRender('escalation', () => renderDonut('chart-escalation', escalationData, null));
    safeRender('assignment-group', () => renderAssignmentGroupTable());
    safeRender('sla-priority', () => renderSLAByPriority());
}}

safeRender('assignee-breakdown', () => renderAssigneeBreakdown());
safeRender('issue-themes', () => renderIssueThemes());
safeRender('ra-matrix', () => renderRAMatrix());
safeRender('reporter-breakdown', () => renderReporterBreakdown());
safeRender('stale-filters', () => buildStaleFilterOptions());
safeRender('staleness', () => renderStaleness());
safeRender('resolution', () => renderBarChart('resolution-chart', resolutionData, null));
safeRender('age', () => renderBarChart('age-chart', ageBucketsData, null));
safeRender('oldest', () => renderOldest());
safeRender('ticket-table', () => renderTicketTable());
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate an HTML dashboard from a Jira or ServiceNow CSV export."
    )
    parser.add_argument("input_csv", help="Path to Jira or ServiceNow CSV export")
    parser.add_argument("-o", "--output", default="dashboard.html",
                        help="Output HTML file (default: dashboard.html)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print detailed processing stats")
    parser.add_argument("--stale-days", type=int, default=14,
                        help="Days without activity to flag as stale (default: 14)")
    parser.add_argument("--title", default=None,
                        help="Dashboard title (default: auto-detected from issue keys)")
    parser.add_argument("--source", choices=["jira", "servicenow", "auto"],
                        default="auto",
                        help="CSV source format (default: auto-detect from headers)")
    args = parser.parse_args(argv)

    input_path = Path(args.input_csv)
    if not input_path.exists():
        print(f"Error: File not found: {args.input_csv}", file=sys.stderr)
        return 1

    # Determine source config
    if args.source == "auto":
        # Read headers to auto-detect
        content = _read_file(input_path)
        reader = csv.reader(io.StringIO(content))
        try:
            headers = next(reader)
        except StopIteration:
            headers = []
        detected = _detect_source(headers)
        config = _servicenow_config() if detected == "servicenow" else _jira_config()
        if args.verbose:
            print(f"Auto-detected source: {config.display_name}")
            lower = {h.strip().lower() for h in headers}
            jira_indicators = {"issue key", "issue_key", "sprint", "epic link", "epic_link",
                               "story points", "story_points", "issue type", "issuetype",
                               "fix version/s"}
            sn_indicators = {"number", "opened at", "opened_at", "assignment group",
                             "assignment_group", "made sla", "made_sla", "short description",
                             "short_description", "configuration item", "configuration_item",
                             "cmdb_ci", "contact type", "contact_type", "opened by",
                             "opened_by", "caller_id", "resolved at", "resolved_at",
                             "reassignment count", "reassignment_count", "incident_state",
                             "sys_class_name", "sys_created_on", "sys_updated_on",
                             "service_offering", "business_service"}
            j_hits = sorted(ind for ind in jira_indicators if ind in lower)
            s_hits = sorted(ind for ind in sn_indicators if ind in lower)
            j_cf = sum(1 for h in lower if h.startswith("custom field"))
            print(f"  Jira score: {len(j_hits) + j_cf} (headers: {j_hits}, custom fields: {j_cf})")
            print(f"  ServiceNow score: {len(s_hits)} (headers: {s_hits[:10]}{'...' if len(s_hits) > 10 else ''})")
            print(f"  First header: {headers[0]!r}")
    elif args.source == "servicenow":
        config = _servicenow_config()
    else:
        config = _jira_config()

    tickets = _parse_csv(str(input_path), config, verbose=args.verbose)
    if not tickets:
        print("Warning: No tickets found in CSV.", file=sys.stderr)

    title = _auto_title(tickets, args.title, config)
    data = compute_dashboard_data(tickets, stale_days=args.stale_days, config=config)

    if args.verbose:
        print(f"  Open: {data.open_tickets}, Closed: {data.closed_tickets}")
        print(f"  Resolution rate: {data.resolution_rate}%")
        print(f"  Avg resolution: {data.avg_resolution_days} days")
        print(f"  Stale (>{args.stale_days}d): {data.stale_tickets}")
        print(f"  Overdue: {data.overdue_tickets}")
        print(f"  Unassigned: {data.unassigned_tickets}")
        print(f"  Blocked: {data.blocked_tickets}")
        print(f"  Avg age (open): {data.avg_age_open_days} days")
        if config.has_story_points:
            print(f"  Story points: {data.total_story_points} total, {data.open_story_points} open")
            print(f"  Epics: {len(data.epic_progress)}, Sprints: {len(data.sprint_progress)}")
            print(f"  Components: {len(data.component_counts)}, Labels: {len(data.label_counts)}")
        if config.has_sla:
            print(f"  SLA compliance: {data.sla_compliance_pct}% ({data.sla_met_count} met / {data.sla_missed_count} missed)")
            print(f"  Categories: {len(data.category_counts)}, Assignment groups: {len(data.assignment_group_counts)}")
            print(f"  Avg reassignments: {data.avg_reassignment_count}, Avg reopens: {data.avg_reopen_count}")

    html_content = generate_html(
        tickets, data,
        title=title,
        source_file=input_path.name,
        stale_days=args.stale_days,
        config=config,
    )

    output_path = Path(args.output)
    output_path.write_text(html_content, encoding="utf-8")
    print(f"Dashboard written to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
