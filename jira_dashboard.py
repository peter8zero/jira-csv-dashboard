#!/usr/bin/env python3
"""Jira CSV Dashboard Generator.

Ingests a Jira CSV export and produces a single self-contained HTML dashboard
with ticket status, assignee workload, staleness, durations, and more.

Usage:
    python3 jira_dashboard.py export.csv
    python3 jira_dashboard.py export.csv -o my_dashboard.html -v
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


def _build_alias_lookup(headers: List[str]) -> Dict[str, Optional[int]]:
    """Build a mapping from canonical field name to column index."""
    lower_headers = [h.strip().lower() for h in headers]
    lookup: Dict[str, Optional[int]] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        lookup[canonical] = None
        for alias in aliases:
            if alias in lower_headers:
                lookup[canonical] = lower_headers.index(alias)
                break
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
    "%d/%b/%y %I:%M %p",    # 01/Jan/24 09:30 AM
    "%d/%b/%y %H:%M",       # 01/Jan/24 09:30
    "%d/%b/%Y %I:%M %p",    # 01/Jan/2024 09:30 AM
    "%d/%b/%Y %H:%M",       # 01/Jan/2024 09:30
    "%Y-%m-%dT%H:%M:%S.%f%z",  # ISO 8601 with tz
    "%Y-%m-%dT%H:%M:%S%z",     # ISO 8601 with tz no ms
    "%Y-%m-%dT%H:%M:%S.%f",    # ISO 8601 no tz
    "%Y-%m-%dT%H:%M:%S",       # ISO 8601 no tz no ms
    "%Y-%m-%d %H:%M:%S",       # 2024-01-15 09:30:00
    "%Y-%m-%d",                 # 2024-01-15
    "%d/%m/%Y %H:%M",          # 01/01/2024 09:30
    "%d/%m/%Y",                 # 01/01/2024
    "%m/%d/%Y %H:%M",          # 01/15/2024 09:30
    "%m/%d/%Y",                 # 01/15/2024
]


def parse_date(value: str) -> Optional[datetime]:
    """Try multiple date formats; return None on failure."""
    if not value or not value.strip():
        return None
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            # Strip timezone info for consistent comparisons
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
    # Try plain numeric (seconds)
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


# ---------------------------------------------------------------------------
# CSV Parsing
# ---------------------------------------------------------------------------

def _extract_comments(row: List[str], comment_cols: List[int]) -> Tuple[Optional[datetime], str]:
    """Extract latest comment date and text from comment columns.

    Jira exports comments in various formats. Try to find the most recent one.
    """
    latest_date: Optional[datetime] = None
    latest_text = ""
    # Jira comment format: "DD/Mon/YY HH:MM AM;username;comment text"
    comment_date_re = re.compile(r"^(\d{1,2}/\w{3}/\d{2,4}\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)")
    for ci in comment_cols:
        if ci >= len(row):
            continue
        val = row[ci].strip()
        if not val:
            continue
        # Try splitting by semicolons (Jira format)
        parts = val.split(";")
        if len(parts) >= 3:
            d = parse_date(parts[0].strip())
            text = parts[-1].strip()
        else:
            # Try to extract date from start
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


def parse_jira_csv(filepath: str, verbose: bool = False) -> List[JiraTicket]:
    """Parse a Jira CSV export into a list of JiraTicket objects."""
    path = Path(filepath)
    content = path.read_text(encoding="utf-8-sig")
    reader = csv.reader(io.StringIO(content))
    try:
        headers = next(reader)
    except StopIteration:
        return []

    lookup = _build_alias_lookup(headers)
    comment_cols = _find_comment_columns(headers)
    tickets: List[JiraTicket] = []

    def _get(row: List[str], canonical: str) -> str:
        idx = lookup.get(canonical)
        if idx is not None and idx < len(row):
            return row[idx].strip()
        return ""

    for row_num, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        t = JiraTicket()
        t.key = _get(row, "key")
        t.summary = _get(row, "summary")
        t.status = _get(row, "status")
        t.assignee = _get(row, "assignee") or "Unassigned"
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

        t.last_comment_date, t.last_comment_text = _extract_comments(row, comment_cols)

        # Store all raw fields
        for i, h in enumerate(headers):
            if i < len(row):
                t.raw_fields[h] = row[i]

        tickets.append(t)

    if verbose:
        print(f"Parsed {len(tickets)} tickets from {filepath}")
        print(f"  Columns found: {', '.join(c for c, i in lookup.items() if i is not None)}")
        print(f"  Comment columns: {len(comment_cols)}")

    return tickets


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

_OPEN_STATUSES = {"open", "to do", "todo", "in progress", "in review", "reopened",
                  "backlog", "selected for development", "blocked", "waiting",
                  "new", "active", "in development", "in testing", "ready for review"}
_CLOSED_STATUSES = {"done", "closed", "resolved", "complete", "completed", "cancelled",
                    "won't do", "wontdo", "duplicate", "rejected"}


def _is_open(status: str) -> bool:
    sl = status.strip().lower()
    if sl in _CLOSED_STATUSES:
        return False
    if sl in _OPEN_STATUSES:
        return True
    # Default: treat as open if no resolution-like word
    return True


@dataclass
class DashboardData:
    total_tickets: int = 0
    open_tickets: int = 0
    closed_tickets: int = 0
    avg_age_open_days: float = 0.0
    overdue_tickets: int = 0
    stale_tickets: int = 0
    status_counts: Dict[str, int] = field(default_factory=dict)
    assignee_counts: Dict[str, int] = field(default_factory=dict)
    priority_counts: Dict[str, int] = field(default_factory=dict)
    type_counts: Dict[str, int] = field(default_factory=dict)
    staleness_rows: List[Dict[str, Any]] = field(default_factory=list)
    avg_resolution_by_type: Dict[str, float] = field(default_factory=dict)
    age_buckets: Dict[str, int] = field(default_factory=dict)
    oldest_open: List[Dict[str, Any]] = field(default_factory=list)
    assignee_breakdown: List[Dict[str, Any]] = field(default_factory=list)
    reporter_breakdown: List[Dict[str, Any]] = field(default_factory=list)
    all_tickets_json: str = "[]"
    all_headers: List[str] = field(default_factory=list)


def compute_dashboard_data(tickets: List[JiraTicket], stale_days: int = 14,
                           now: Optional[datetime] = None) -> DashboardData:
    """Compute all dashboard metrics from parsed tickets."""
    if now is None:
        now = datetime.now()

    d = DashboardData()
    d.total_tickets = len(tickets)

    open_ages: List[float] = []
    resolution_times_by_type: Dict[str, List[float]] = defaultdict(list)
    bucket_labels = ["< 7d", "7–14d", "14–30d", "30–60d", "60–90d", "90d+"]
    d.age_buckets = {b: 0 for b in bucket_labels}

    for t in tickets:
        is_open = _is_open(t.status)
        if is_open:
            d.open_tickets += 1
        else:
            d.closed_tickets += 1

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

        # Age of open tickets
        if is_open and t.created:
            age_days = (now - t.created).total_seconds() / 86400
            open_ages.append(age_days)
            # Buckets
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
                # No activity info - check created date
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
            itype = t.issue_type or "Unknown"
            resolution_times_by_type[itype].append(res_days)

    # Averages
    d.avg_age_open_days = round(sum(open_ages) / len(open_ages), 1) if open_ages else 0.0

    for itype, times in resolution_times_by_type.items():
        d.avg_resolution_by_type[itype] = round(sum(times) / len(times), 1)

    # Sort staleness rows (most stale first)
    d.staleness_rows.sort(key=lambda r: -r["days_since"])

    # Top 10 oldest open
    open_with_age = []
    for t in tickets:
        if _is_open(t.status) and t.created:
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

    # Assignee breakdown
    assignee_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "open": 0, "closed": 0, "overdue": 0, "stale": 0,
        "open_age_sum": 0.0, "open_count_for_age": 0,
    })
    reporter_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "open": 0, "closed": 0, "overdue": 0,
    })
    for t in tickets:
        is_open_t = _is_open(t.status)
        # Assignee
        a = t.assignee
        assignee_stats[a]["total"] += 1
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
        # Reporter
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
        })
    for name, s in sorted(reporter_stats.items(), key=lambda x: -x[1]["total"]):
        d.reporter_breakdown.append({
            "reporter": name, "total": s["total"], "open": s["open"],
            "closed": s["closed"], "overdue": s["overdue"],
        })

    # Full ticket table data
    all_rows = []
    # Collect all unique raw field headers
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
    d.all_tickets_json = json.dumps(all_rows, default=str)

    return d


# ---------------------------------------------------------------------------
# HTML Generation
# ---------------------------------------------------------------------------

def _auto_title(tickets: List[JiraTicket], user_title: Optional[str]) -> str:
    if user_title:
        return user_title
    if not tickets:
        return "Jira Dashboard"
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
                  stale_days: int = 14) -> str:
    """Generate the complete self-contained HTML dashboard."""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Prepare chart data as JSON
    status_data = json.dumps(dict(sorted(data.status_counts.items(), key=lambda x: -x[1])))
    assignee_data = json.dumps(dict(sorted(data.assignee_counts.items(), key=lambda x: -x[1])[:15]))
    priority_data = json.dumps(data.priority_counts)
    type_data = json.dumps(data.type_counts)
    staleness_json = json.dumps(data.staleness_rows)
    resolution_json = json.dumps(data.avg_resolution_by_type)
    age_buckets_json = json.dumps(data.age_buckets)
    oldest_json = json.dumps(data.oldest_open)
    assignee_breakdown_json = json.dumps(data.assignee_breakdown)
    reporter_breakdown_json = json.dumps(data.reporter_breakdown)
    headers_json = json.dumps(data.all_headers)

    title_escaped = html.escape(title)
    source_escaped = html.escape(source_file)

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
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
.card {{
    background: var(--xps-card-bg); border: 1px solid var(--xps-border);
    border-radius: 10px; padding: 20px; text-align: center;
}}
.card-value {{ font-size: 2rem; font-weight: 700; color: var(--xps-blue); }}
.card-label {{ font-size: 0.85rem; color: var(--xps-text-muted); margin-top: 4px; }}
.card-sub {{ font-size: 0.75rem; color: var(--xps-text-muted); }}
.card.danger .card-value {{ color: var(--xps-danger); }}
.card.warning .card-value {{ color: var(--xps-warning); }}
.section {{
    background: var(--xps-card-bg); border: 1px solid var(--xps-border);
    border-radius: 10px; padding: 24px; margin-bottom: 24px;
}}
.section h2 {{
    font-size: 1.1rem; font-weight: 600; margin-bottom: 16px;
    padding-bottom: 8px; border-bottom: 1px solid var(--xps-border);
    color: var(--xps-blue-light);
}}
.charts-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 24px; margin-bottom: 24px; }}
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

<!-- Summary Cards -->
<div class="cards">
    <div class="card">
        <div class="card-value">{data.total_tickets}</div>
        <div class="card-label">Total Tickets</div>
        <div class="card-sub">{data.open_tickets} open / {data.closed_tickets} closed</div>
    </div>
    <div class="card">
        <div class="card-value">{data.avg_age_open_days}</div>
        <div class="card-label">Avg Age (Open)</div>
        <div class="card-sub">days</div>
    </div>
    <div class="card {"danger" if data.overdue_tickets else ""}">
        <div class="card-value">{data.overdue_tickets}</div>
        <div class="card-label">Overdue Tickets</div>
        <div class="card-sub">past due date</div>
    </div>
    <div class="card {"warning" if data.stale_tickets else ""}">
        <div class="card-value">{data.stale_tickets}</div>
        <div class="card-label">Stale Tickets</div>
        <div class="card-sub">no activity in {stale_days}+ days</div>
    </div>
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
</div>

<!-- Assignee Breakdown -->
<div class="section">
    <h2>Assignee Breakdown</h2>
    <div id="assignee-breakdown"></div>
</div>

<!-- Reporter Breakdown -->
<div class="section">
    <h2>Reporter Breakdown</h2>
    <div id="reporter-breakdown"></div>
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

</div>

<script>
// Data
const statusData = {status_data};
const assigneeData = {assignee_data};
const priorityData = {priority_data};
const typeData = {type_data};
const stalenessData = {staleness_json};
const resolutionData = {resolution_json};
const ageBucketsData = {age_buckets_json};
const oldestData = {oldest_json};
const assigneeBreakdown = {assignee_breakdown_json};
const reporterBreakdown = {reporter_breakdown_json};
const allTickets = {data.all_tickets_json};
const allHeaders = {headers_json};

// Theme
function toggleTheme() {{
    const html = document.documentElement;
    html.dataset.theme = html.dataset.theme === 'dark' ? 'light' : 'dark';
}}

// Colour maps
const statusColours = {{
    'To Do': '#8A9499', 'Open': '#8A9499', 'Backlog': '#8A9499', 'New': '#8A9499',
    'In Progress': '#4A9FD9', 'In Review': '#6BB3E3', 'In Development': '#4A9FD9',
    'In Testing': '#6BB3E3', 'Active': '#4A9FD9',
    'Done': '#4CAF50', 'Closed': '#4CAF50', 'Resolved': '#4CAF50', 'Complete': '#4CAF50',
    'Blocked': '#F44336', 'Waiting': '#FF9800', 'Reopened': '#FF9800',
}};
const priorityColours = {{
    'Critical': '#F44336', 'Highest': '#F44336', 'Blocker': '#F44336',
    'High': '#FF9800', 'Major': '#FF9800',
    'Medium': '#FFD54F', 'Normal': '#FFD54F',
    'Low': '#4CAF50', 'Minor': '#4CAF50',
    'Lowest': '#8A9499', 'Trivial': '#8A9499',
}};
const typeColours = {{
    'Bug': '#F44336', 'Defect': '#F44336',
    'Story': '#4A9FD9', 'User Story': '#4A9FD9',
    'Task': '#8A9499', 'Sub-task': '#6BB3E3',
    'Epic': '#9C27B0', 'Initiative': '#9C27B0',
    'Improvement': '#FF9800', 'New Feature': '#4CAF50',
}};
const defaultColours = ['#4A9FD9','#4CAF50','#FF9800','#F44336','#9C27B0','#00BCD4','#8BC34A','#FF5722','#607D8B','#E91E63'];
function getColour(map, key, idx) {{
    return map[key] || defaultColours[idx % defaultColours.length];
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
    // Legend
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

// Assignee breakdown table
const assigneeCols = [
    {{ key: 'assignee', label: 'Assignee' }},
    {{ key: 'total', label: 'Total' }},
    {{ key: 'open', label: 'Open' }},
    {{ key: 'closed', label: 'Closed' }},
    {{ key: 'avg_age', label: 'Avg Age (days)' }},
    {{ key: 'overdue', label: 'Overdue' }},
    {{ key: 'stale', label: 'Stale' }},
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
        html += `<tr><td>${{r.assignee}}</td><td>${{r.total}}</td><td>${{r.open}}</td><td>${{r.closed}}</td><td>${{r.avg_age}}</td><td${{overdueClass}}>${{r.overdue}}</td><td${{staleClass}}>${{r.stale}}</td></tr>`;
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
    const cols = allHeaders.slice(0, 12); // Show first 12 columns max for readability
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

    // Pagination
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

// Render all
renderBarChart('chart-status', statusData, statusColours);
renderBarChart('chart-assignee', assigneeData, null);
renderDonut('chart-priority', priorityData, priorityColours);
renderDonut('chart-type', typeData, typeColours);
renderAssigneeBreakdown();
renderReporterBreakdown();
buildStaleFilterOptions();
renderStaleness();
renderBarChart('resolution-chart', resolutionData, null);
renderBarChart('age-chart', ageBucketsData, null);
renderOldest();
renderTicketTable();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate an HTML dashboard from a Jira CSV export."
    )
    parser.add_argument("input_csv", help="Path to Jira CSV export")
    parser.add_argument("-o", "--output", default="dashboard.html",
                        help="Output HTML file (default: dashboard.html)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print detailed processing stats")
    parser.add_argument("--stale-days", type=int, default=14,
                        help="Days without activity to flag as stale (default: 14)")
    parser.add_argument("--title", default=None,
                        help="Dashboard title (default: auto-detected from issue keys)")
    args = parser.parse_args(argv)

    input_path = Path(args.input_csv)
    if not input_path.exists():
        print(f"Error: File not found: {args.input_csv}", file=sys.stderr)
        return 1

    tickets = parse_jira_csv(str(input_path), verbose=args.verbose)
    if not tickets:
        print("Warning: No tickets found in CSV.", file=sys.stderr)

    title = _auto_title(tickets, args.title)
    data = compute_dashboard_data(tickets, stale_days=args.stale_days)

    if args.verbose:
        print(f"  Open: {data.open_tickets}, Closed: {data.closed_tickets}")
        print(f"  Stale (>{args.stale_days}d): {data.stale_tickets}")
        print(f"  Overdue: {data.overdue_tickets}")
        print(f"  Avg age (open): {data.avg_age_open_days} days")

    html_content = generate_html(
        tickets, data,
        title=title,
        source_file=input_path.name,
        stale_days=args.stale_days,
    )

    output_path = Path(args.output)
    output_path.write_text(html_content, encoding="utf-8")
    print(f"Dashboard written to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
