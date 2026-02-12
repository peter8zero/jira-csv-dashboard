# Jira CSV Dashboard

A Python CLI tool that transforms a Jira CSV export into a single self-contained HTML dashboard with detailed MI breakdowns.

No external dependencies — uses Python standard library only.

## Quick Start

```bash
python3 jira_dashboard.py export.csv
open dashboard.html
```

## Usage

```
python3 jira_dashboard.py <input_csv> [options]
```

| Argument | Description |
|----------|-------------|
| `input_csv` | Path to your Jira CSV export (positional, required) |
| `-o, --output` | Output HTML file path (default: `dashboard.html`) |
| `-v, --verbose` | Print detailed processing stats to the terminal |
| `--stale-days N` | Days without activity to flag a ticket as stale (default: 14) |
| `--title TEXT` | Dashboard title (default: auto-detected from issue keys) |

### Examples

```bash
# Basic usage — generates dashboard.html
python3 jira_dashboard.py export.csv

# Custom output file with verbose logging
python3 jira_dashboard.py export.csv -o sprint42.html -v

# Flag tickets as stale after 7 days, with a custom title
python3 jira_dashboard.py export.csv --stale-days 7 --title "Sprint 42 Dashboard"
```

## Getting Your CSV from Jira

1. Go to your Jira board or filter view
2. Click **Export** (top right) > **Export CSV (all fields)** or **Export CSV (current fields)**
3. Save the `.csv` file
4. Run the tool against it

The tool handles Jira's inconsistent column naming automatically (e.g. "Issue key", "Key", "issue_key" all work).

## Dashboard Sections

The generated HTML file is fully self-contained — no internet connection required to view it.

### Summary Cards
Eight cards showing key metrics at a glance:
- **Total Tickets** — with open/closed split
- **Avg Age (Open)** — average age of open tickets in days
- **Overdue** — open tickets past their due date
- **Stale** — open tickets with no activity beyond the stale threshold
- **Resolution Rate** — percentage of tickets resolved
- **Avg Resolution** — average time from creation to resolution
- **Unassigned** — open tickets with no assignee
- **Story Points** — total story points with open points breakdown

### Charts
- **Status Breakdown** — horizontal bar chart colour-coded by status category
- **Assignee Workload** — open ticket count per assignee
- **Priority Distribution** — donut chart (Critical/High/Medium/Low)
- **Issue Type Distribution** — donut chart (Bug/Story/Task/Epic)
- **Component Breakdown** — bar chart of ticket counts by component
- **Label Breakdown** — bar chart of ticket counts by label (top 15)

### Created vs Resolved Trend
Monthly grouped bar chart showing tickets created (blue) vs resolved (green). Reveals whether the backlog is growing or shrinking over time.

### Epic Progress
Sortable table showing per-epic metrics: total tickets, open/closed split, percentage done (with progress bar), and total story points. Gracefully hidden if no epic data is present.

### Sprint Progress
Same layout as epic progress — per-sprint breakdown with progress bars and story point totals.

### Assignee Breakdown
Sortable table showing per-assignee metrics: total tickets, open/closed split, average age of open tickets, overdue count, stale count, and story points. Overdue and stale cells are highlighted in red/amber.

### Reporter Breakdown
Sortable table showing per-reporter metrics: total tickets, open/closed split, and overdue count.

### Reporter → Assignee Flow
Table of the top 20 reporter-assignee combinations by ticket count. Shows who creates work for whom.

### Priority SLA
Bar chart showing average resolution time (days) per priority level. Answers the question: "Are critical tickets being resolved faster?"

### Estimation Accuracy
Table comparing estimated vs actual time by issue type, with accuracy percentage. Colour-coded: green (within 20%), amber (20–50% off), red (>50% off). Uses Original Estimate and Time Spent fields.

### Staleness Report
Sortable, filterable table of all open tickets with key, summary, reporter, assignee, status, last activity date, days since activity, and comment preview. Rows are colour-coded: red (>30 days), amber (>stale threshold), green (recent activity). Filter dropdowns for Key, Status, Reporter, and Assignee.

### Duration Metrics
- Average time to resolution broken down by issue type
- Age distribution of open tickets (histogram buckets: <7d, 7–14d, 14–30d, 30–60d, 60–90d, 90d+)

### Top 10 Oldest Open Tickets
Quick view of the longest-running open tickets.

### Full Ticket Table
All tickets with search, clickable column sorting, and pagination (50 per page).

### Theme Toggle
Dark theme (default) and light theme, toggled via the button in the header.

## Supported CSV Formats

The tool normalises column names automatically. These all work:

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
| Remaining Estimate | `Remaining Estimate`, `Time Remaining Estimate` |
| Fix Version/s | `Fix Version/s`, `Fix Versions` |

Date formats are auto-detected: `15/Jan/24 09:30 AM`, `2024-01-15T14:30:00`, `15/01/2024`, and more.

Duration formats are auto-detected: `1w 2d 3h 30m`, plain seconds, etc.

## Running Tests

```bash
python3 test_jira_dashboard.py
```

92 tests covering CSV parsing, date/duration parsing, column normalisation, metrics computation (including new enhancement metrics), HTML generation, and end-to-end CLI pipeline.
