# Jira / ServiceNow CSV Dashboard

A Python CLI tool that transforms a Jira or ServiceNow CSV export into a single self-contained HTML dashboard with detailed MI breakdowns.

No external dependencies — uses Python standard library only.

## Quick Start

```bash
# Jira CSV — source auto-detected
python3 jira_dashboard.py jira_export.csv
open dashboard.html

# ServiceNow CSV — source auto-detected
python3 jira_dashboard.py servicenow_export.csv
open dashboard.html

# Force source type
python3 jira_dashboard.py export.csv --source servicenow -o sn_dashboard.html
```

## Usage

```
python3 jira_dashboard.py <input_csv> [options]
```

| Argument | Description |
|----------|-------------|
| `input_csv` | Path to your Jira or ServiceNow CSV export (positional, required) |
| `-o, --output` | Output HTML file path (default: `dashboard.html`) |
| `-v, --verbose` | Print detailed processing stats to the terminal |
| `--stale-days N` | Days without activity to flag a ticket as stale (default: 14) |
| `--title TEXT` | Dashboard title (default: auto-detected from issue keys) |
| `--source` | CSV source format: `jira`, `servicenow`, or `auto` (default: `auto`) |

### Examples

```bash
# Basic usage — auto-detects source, generates dashboard.html
python3 jira_dashboard.py export.csv

# Custom output file with verbose logging
python3 jira_dashboard.py export.csv -o sprint42.html -v

# Flag tickets as stale after 7 days, with a custom title
python3 jira_dashboard.py export.csv --stale-days 7 --title "Sprint 42 Dashboard"

# Force ServiceNow mode
python3 jira_dashboard.py incidents.csv --source servicenow -o incidents.html
```

## Getting Your CSV

### From Jira

1. Go to your Jira board or filter view
2. Click **Export** (top right) > **Export CSV (all fields)** or **Export CSV (current fields)**
3. Save the `.csv` file
4. Run the tool against it

### From ServiceNow

1. Navigate to your list view (e.g. Incident > All)
2. Right-click the column headers and select **Export > CSV**
3. Save the `.csv` file
4. Run the tool against it

## Auto-Detection

The tool auto-detects whether a CSV is from Jira or ServiceNow by scoring the headers:

- **Jira indicators**: `Issue key`, `Sprint`, `Epic Link`, `Story Points`, `Custom field (...)` columns
- **ServiceNow indicators**: `Number`, `Opened at`, `Assignment group`, `Made SLA`, `Short description`, `Configuration item`, `Contact type`

The source with the higher score wins. Use `--source jira` or `--source servicenow` to override.

## Dashboard Sections

The generated HTML file is fully self-contained — no internet connection required to view it.

### Summary Cards

**Shared** (both sources): Total Tickets, Resolution Rate, Avg Age (Open), Avg Resolution, Overdue, Stale, Unassigned.

**8th card**:
- **Jira**: Story Points (total with open breakdown)
- **ServiceNow**: SLA Compliance % (met/missed split)

**ServiceNow extra cards**: Avg Reassignments, Avg Reopens.

### Shared Charts & Tables

- **Status Breakdown** — horizontal bar chart colour-coded by status
- **Assignee Workload** — open ticket count per assignee
- **Priority Distribution** — donut chart
- **Issue Type Distribution** — donut chart
- **Created vs Resolved Trend** — monthly bar chart
- **Priority SLA** — avg resolution time by priority
- **Assignee Breakdown** — sortable table with overdue/stale highlights
- **Reporter Breakdown** — sortable table
- **Reporter → Assignee Flow** — top 20 combinations
- **Staleness Report** — filterable, colour-coded table
- **Duration Metrics** — resolution by type + age distribution
- **Top 10 Oldest Open Tickets**
- **Full Ticket Table** — search, sort, paginate

### Jira-Only Sections

- **Epic Progress** — sortable table with progress bars and story points
- **Sprint Progress** — same layout as epic progress
- **Component Breakdown** — bar chart
- **Label Breakdown** — bar chart
- **Estimation Accuracy** — estimated vs actual time by issue type

### ServiceNow-Only Sections

- **Category Breakdown** — bar chart of ticket counts by category
- **Assignment Group Breakdown** — sortable table with SLA % per group
- **Contact Type Distribution** — donut chart
- **Escalation Analysis** — donut chart
- **SLA Compliance by Priority** — stacked bar chart (met/missed per priority)

### Theme Toggle

Dark theme (default) and light theme, toggled via the button in the header.

## Supported CSV Formats

### Jira Column Mapping

| Field | Recognised Column Names |
|-------|------------------------|
| Key | `Issue key`, `Key`, `issue_key`, `IssueKey` |
| Summary | `Summary`, `Title` |
| Status | `Status`, `Issue Status`, `Status Name` |
| Assignee | `Assignee`, `Assigned To`, `Assignee Name` |
| Reporter | `Reporter`, `Reporter Name`, `Created By` |
| Priority | `Priority`, `Priority Name` |
| Issue Type | `Issue Type`, `IssueType`, `Type` |
| Created | `Created`, `Date Created`, `Creation Date` |
| Updated | `Updated`, `Last Updated` |
| Resolved | `Resolved`, `Resolution Date` |
| Due Date | `Due Date`, `Due`, `DueDate` |
| Comments | Any column containing "Comment" |
| Story Points | `Story Points`, `Story Point Estimate` |
| Epic Link | `Epic Link`, `Epic Name`, `Epic` |
| Sprint | `Sprint`, `Sprint Name` |
| Components | `Components`, `Component`, `Component/s` |
| Labels | `Labels`, `Label` |
| Original Estimate | `Original Estimate`, `Time Original Estimate` |
| Time Spent | `Time Spent` |
| Fix Version/s | `Fix Version/s`, `Fix Versions` |

### ServiceNow Column Mapping

| Field | Recognised Column Names |
|-------|------------------------|
| Key | `Number`, `Task Number` |
| Summary | `Short description` |
| Status | `State`, `Status`, `Incident State` |
| Assignee | `Assigned to` |
| Reporter | `Opened by`, `Caller`, `Requested by` |
| Priority | `Priority` |
| Type | `Type`, `Task type`, `Category`, `sys_class_name` |
| Created | `Opened at`, `sys_created_on` |
| Updated | `Updated at`, `sys_updated_on` |
| Resolved | `Resolved at`, `Closed at` |
| Category | `Category` |
| Subcategory | `Subcategory` |
| Assignment Group | `Assignment group` |
| Contact Type | `Contact type` |
| Made SLA | `Made SLA` |
| Escalation | `Escalation` |
| Reassignment Count | `Reassignment count` |
| Reopen Count | `Reopen count` |
| Impact | `Impact` |
| Urgency | `Urgency` |
| Close Notes | `Close notes`, `Resolution notes` |

Date formats are auto-detected: `15/Jan/24 09:30 AM`, `2024-01-15T14:30:00`, `2024-01-15 09:00:00`, `15/01/2024`, and more.

## Running Tests

```bash
python3 test_jira_dashboard.py
```

127 tests covering CSV parsing, date/duration parsing, column normalisation, auto-detection, Jira metrics, ServiceNow metrics, HTML generation (both sources), backwards compatibility, and end-to-end CLI pipelines.
