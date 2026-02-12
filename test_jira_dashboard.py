#!/usr/bin/env python3
"""Tests for jira_dashboard.py."""

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# Ensure the module is importable
sys.path.insert(0, os.path.dirname(__file__))

from jira_dashboard import (
    COLUMN_ALIASES,
    JiraTicket,
    SourceConfig,
    Ticket,
    _build_alias_lookup,
    _detect_source,
    _extract_comments,
    _find_comment_columns,
    _is_blocked,
    _is_open,
    _jira_config,
    _parse_csv,
    _servicenow_config,
    _split_csv_field,
    compute_dashboard_data,
    format_duration,
    generate_html,
    main,
    parse_date,
    parse_duration_seconds,
    parse_jira_csv,
)


class TestDateParsing(unittest.TestCase):
    def test_jira_format_am_pm(self):
        dt = parse_date("15/Jan/24 09:30 AM")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.month, 1)
        self.assertEqual(dt.day, 15)
        self.assertEqual(dt.hour, 9)

    def test_iso_format(self):
        dt = parse_date("2024-01-15T14:30:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2024)
        self.assertEqual(dt.hour, 14)

    def test_iso_with_timezone(self):
        dt = parse_date("2024-01-15T14:30:00+00:00")
        self.assertIsNotNone(dt)
        self.assertIsNone(dt.tzinfo)

    def test_date_only(self):
        dt = parse_date("2024-06-15")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2024)
        self.assertEqual(dt.month, 6)

    def test_uk_date_format(self):
        dt = parse_date("15/01/2024")
        self.assertIsNotNone(dt)

    def test_empty_string(self):
        self.assertIsNone(parse_date(""))

    def test_none_like(self):
        self.assertIsNone(parse_date("   "))

    def test_invalid(self):
        self.assertIsNone(parse_date("not a date"))

    def test_jira_format_four_digit_year(self):
        dt = parse_date("15/Jan/2024 09:30 AM")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2024)


class TestDurationParsing(unittest.TestCase):
    def test_weeks(self):
        self.assertEqual(parse_duration_seconds("1w"), 5 * 8 * 3600)

    def test_days(self):
        self.assertEqual(parse_duration_seconds("2d"), 2 * 8 * 3600)

    def test_hours(self):
        self.assertEqual(parse_duration_seconds("3h"), 3 * 3600)

    def test_minutes(self):
        self.assertEqual(parse_duration_seconds("30m"), 30 * 60)

    def test_combined(self):
        expected = 1 * 5 * 8 * 3600 + 2 * 8 * 3600 + 3 * 3600 + 30 * 60
        self.assertEqual(parse_duration_seconds("1w 2d 3h 30m"), expected)

    def test_plain_number(self):
        self.assertEqual(parse_duration_seconds("3600"), 3600)

    def test_empty(self):
        self.assertIsNone(parse_duration_seconds(""))

    def test_none(self):
        self.assertIsNone(parse_duration_seconds("   "))

    def test_format_duration_roundtrip(self):
        secs = parse_duration_seconds("1w 2d 3h 30m")
        formatted = format_duration(secs)
        self.assertEqual(formatted, "1w 2d 3h 30m")

    def test_format_duration_zero(self):
        self.assertEqual(format_duration(0), "0m")

    def test_format_duration_none(self):
        self.assertEqual(format_duration(None), "—")


class TestColumnNormalisation(unittest.TestCase):
    def test_standard_headers(self):
        headers = ["Issue key", "Summary", "Status", "Assignee", "Priority"]
        lookup = _build_alias_lookup(headers)
        self.assertEqual(lookup["key"], [0])
        self.assertEqual(lookup["summary"], [1])
        self.assertEqual(lookup["status"], [2])
        self.assertEqual(lookup["assignee"], [3])
        self.assertEqual(lookup["priority"], [4])

    def test_alternate_headers(self):
        headers = ["Key", "Title", "Issue Status", "Assigned To"]
        lookup = _build_alias_lookup(headers)
        self.assertEqual(lookup["key"], [0])
        self.assertEqual(lookup["summary"], [1])  # "title" is a summary alias
        self.assertEqual(lookup["status"], [2])
        self.assertEqual(lookup["assignee"], [3])

    def test_case_insensitive(self):
        headers = ["ISSUE KEY", "SUMMARY", "STATUS"]
        lookup = _build_alias_lookup(headers)
        self.assertEqual(lookup["key"], [0])
        self.assertEqual(lookup["summary"], [1])

    def test_missing_columns(self):
        headers = ["Issue key", "Summary"]
        lookup = _build_alias_lookup(headers)
        self.assertEqual(lookup["key"], [0])
        self.assertEqual(lookup["priority"], [])

    def test_custom_field_wrapper(self):
        headers = ["Custom field (Story Points)", "Custom field (Story point estimate)"]
        lookup = _build_alias_lookup(headers)
        # Both should be candidate columns for story_points
        self.assertIn(0, lookup["story_points"])
        self.assertIn(1, lookup["story_points"])

    def test_duplicate_columns_coalesced(self):
        headers = ["Sprint", "Sprint", "Sprint"]
        lookup = _build_alias_lookup(headers)
        self.assertEqual(lookup["sprint"], [0, 1, 2])

    def test_find_comment_columns(self):
        headers = ["Key", "Summary", "Comment", "Comment.1", "Status"]
        indices = _find_comment_columns(headers)
        self.assertEqual(indices, [2, 3])


class TestCSVParsing(unittest.TestCase):
    def _write_csv(self, tmpdir, rows, filename="test.csv", bom=False):
        path = os.path.join(tmpdir, filename)
        with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
            writer = csv.writer(f)
            for row in rows:
                writer.writerow(row)
        return path

    def test_standard_csv(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["Issue key", "Summary", "Status", "Assignee", "Priority", "Issue Type",
                 "Created", "Updated", "Resolved"],
                ["PROJ-1", "Fix bug", "In Progress", "Alice", "High", "Bug",
                 "2024-01-15", "2024-02-01", ""],
                ["PROJ-2", "Add feature", "Done", "Bob", "Medium", "Story",
                 "2024-01-10", "2024-01-20", "2024-01-20"],
            ])
            tickets = parse_jira_csv(path)
            self.assertEqual(len(tickets), 2)
            self.assertEqual(tickets[0].key, "PROJ-1")
            self.assertEqual(tickets[0].assignee, "Alice")
            self.assertEqual(tickets[1].status, "Done")

    def test_bom_handling(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["Issue key", "Summary", "Status"],
                ["BOM-1", "Test", "Open"],
            ], bom=True)
            tickets = parse_jira_csv(path)
            self.assertEqual(len(tickets), 1)
            self.assertEqual(tickets[0].key, "BOM-1")

    def test_missing_columns_graceful(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["Issue key", "Summary"],
                ["MIN-1", "Minimal ticket"],
            ])
            tickets = parse_jira_csv(path)
            self.assertEqual(len(tickets), 1)
            self.assertEqual(tickets[0].key, "MIN-1")
            self.assertEqual(tickets[0].assignee, "Unassigned")
            self.assertIsNone(tickets[0].created)

    def test_empty_csv(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [["Issue key", "Summary"]])
            tickets = parse_jira_csv(path)
            self.assertEqual(len(tickets), 0)

    def test_comment_extraction(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["Issue key", "Summary", "Status", "Comment"],
                ["COM-1", "With comment", "Open", "15/Jan/24 09:30 AM;user;This is a comment"],
            ])
            tickets = parse_jira_csv(path)
            self.assertEqual(len(tickets), 1)
            self.assertIsNotNone(tickets[0].last_comment_date)
            self.assertIn("comment", tickets[0].last_comment_text.lower())

    def test_story_points(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["Issue key", "Summary", "Story Points"],
                ["SP-1", "Pointed", "5"],
            ])
            tickets = parse_jira_csv(path)
            self.assertEqual(tickets[0].story_points, 5.0)

    def test_raw_fields_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["Issue key", "Summary", "Custom Field"],
                ["RAW-1", "Test", "custom_value"],
            ])
            tickets = parse_jira_csv(path)
            self.assertEqual(tickets[0].raw_fields["Custom Field"], "custom_value")

    def test_verbose_output(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["Issue key", "Summary", "Status"],
                ["V-1", "Verbose test", "Open"],
            ])
            # Should not raise
            tickets = parse_jira_csv(path, verbose=True)
            self.assertEqual(len(tickets), 1)


class TestIsOpen(unittest.TestCase):
    def test_open_statuses(self):
        for s in ["Open", "To Do", "In Progress", "Backlog", "Blocked"]:
            self.assertTrue(_is_open(s), f"{s} should be open")

    def test_closed_statuses(self):
        for s in ["Done", "Closed", "Resolved", "Cancelled"]:
            self.assertFalse(_is_open(s), f"{s} should be closed")

    def test_case_insensitive(self):
        self.assertTrue(_is_open("IN PROGRESS"))
        self.assertFalse(_is_open("DONE"))

    def test_unknown_defaults_open(self):
        self.assertTrue(_is_open("Some Custom Status"))


class TestMetricsComputation(unittest.TestCase):
    def _make_ticket(self, key="T-1", status="Open", created_days_ago=10,
                     resolved_days_ago=None, due_days_from_now=None,
                     last_comment_days_ago=None, issue_type="Task",
                     priority="Medium", assignee="Alice"):
        now = datetime(2024, 6, 15)
        t = JiraTicket()
        t.key = key
        t.summary = f"Ticket {key}"
        t.status = status
        t.assignee = assignee
        t.priority = priority
        t.issue_type = issue_type
        t.created = now - timedelta(days=created_days_ago)
        if resolved_days_ago is not None:
            t.resolved = now - timedelta(days=resolved_days_ago)
        if due_days_from_now is not None:
            t.due_date = now + timedelta(days=due_days_from_now)
        if last_comment_days_ago is not None:
            t.last_comment_date = now - timedelta(days=last_comment_days_ago)
            t.last_comment_text = "Some comment"
        return t

    def test_basic_counts(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open"),
            self._make_ticket("T-2", "In Progress"),
            self._make_ticket("T-3", "Done"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(data.total_tickets, 3)
        self.assertEqual(data.open_tickets, 2)
        self.assertEqual(data.closed_tickets, 1)

    def test_avg_age(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", created_days_ago=20),
            self._make_ticket("T-2", "Open", created_days_ago=10),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertAlmostEqual(data.avg_age_open_days, 15.0)

    def test_overdue_detection(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", due_days_from_now=-5),  # overdue
            self._make_ticket("T-2", "Open", due_days_from_now=5),   # not overdue
            self._make_ticket("T-3", "Done", due_days_from_now=-5),  # closed, not counted
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(data.overdue_tickets, 1)

    def test_stale_detection(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", last_comment_days_ago=20),  # stale
            self._make_ticket("T-2", "Open", last_comment_days_ago=5),   # fresh
            self._make_ticket("T-3", "Done", last_comment_days_ago=20),  # closed
        ]
        data = compute_dashboard_data(tickets, stale_days=14, now=now)
        self.assertEqual(data.stale_tickets, 1)

    def test_stale_custom_days(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", last_comment_days_ago=5),
        ]
        data = compute_dashboard_data(tickets, stale_days=3, now=now)
        self.assertEqual(data.stale_tickets, 1)

    def test_status_counts(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open"),
            self._make_ticket("T-2", "Open"),
            self._make_ticket("T-3", "Done"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(data.status_counts["Open"], 2)
        self.assertEqual(data.status_counts["Done"], 1)

    def test_assignee_counts_open_only(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", assignee="Alice"),
            self._make_ticket("T-2", "Done", assignee="Alice"),
            self._make_ticket("T-3", "Open", assignee="Bob"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(data.assignee_counts["Alice"], 1)
        self.assertEqual(data.assignee_counts["Bob"], 1)

    def test_resolution_by_type(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Done", created_days_ago=10, resolved_days_ago=0, issue_type="Bug"),
            self._make_ticket("T-2", "Done", created_days_ago=20, resolved_days_ago=0, issue_type="Bug"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertIn("Bug", data.avg_resolution_by_type)
        self.assertAlmostEqual(data.avg_resolution_by_type["Bug"], 15.0)

    def test_age_buckets(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", created_days_ago=3),   # < 7d
            self._make_ticket("T-2", "Open", created_days_ago=10),  # 7-14d
            self._make_ticket("T-3", "Open", created_days_ago=50),  # 30-60d
            self._make_ticket("T-4", "Open", created_days_ago=100), # 90d+
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(data.age_buckets["< 7d"], 1)
        self.assertEqual(data.age_buckets["7–14d"], 1)
        self.assertEqual(data.age_buckets["30–60d"], 1)
        self.assertEqual(data.age_buckets["90d+"], 1)

    def test_oldest_open(self):
        now = datetime(2024, 6, 15)
        tickets = [self._make_ticket(f"T-{i}", "Open", created_days_ago=i * 10) for i in range(1, 15)]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(len(data.oldest_open), 10)
        self.assertEqual(data.oldest_open[0]["key"], "T-14")  # oldest first

    def test_empty_tickets(self):
        data = compute_dashboard_data([])
        self.assertEqual(data.total_tickets, 0)
        self.assertEqual(data.open_tickets, 0)

    def test_priority_counts(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", priority="High"),
            self._make_ticket("T-2", "Open", priority="High"),
            self._make_ticket("T-3", "Done", priority="Low"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(data.priority_counts["High"], 2)
        self.assertEqual(data.priority_counts["Low"], 1)

    def test_type_counts(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", issue_type="Bug"),
            self._make_ticket("T-2", "Open", issue_type="Story"),
            self._make_ticket("T-3", "Done", issue_type="Bug"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(data.type_counts["Bug"], 2)
        self.assertEqual(data.type_counts["Story"], 1)

    def test_assignee_breakdown(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", assignee="Alice", created_days_ago=20),
            self._make_ticket("T-2", "Open", assignee="Alice", created_days_ago=10),
            self._make_ticket("T-3", "Done", assignee="Alice"),
            self._make_ticket("T-4", "Open", assignee="Bob", created_days_ago=5,
                              due_days_from_now=-1),
        ]
        data = compute_dashboard_data(tickets, now=now)
        by_name = {r["assignee"]: r for r in data.assignee_breakdown}
        self.assertIn("Alice", by_name)
        self.assertIn("Bob", by_name)
        self.assertEqual(by_name["Alice"]["total"], 3)
        self.assertEqual(by_name["Alice"]["open"], 2)
        self.assertEqual(by_name["Alice"]["closed"], 1)
        self.assertAlmostEqual(by_name["Alice"]["avg_age"], 15.0)
        self.assertEqual(by_name["Bob"]["overdue"], 1)

    def test_assignee_breakdown_stale(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", assignee="Alice",
                              last_comment_days_ago=20),
            self._make_ticket("T-2", "Open", assignee="Alice",
                              last_comment_days_ago=3),
        ]
        data = compute_dashboard_data(tickets, stale_days=14, now=now)
        by_name = {r["assignee"]: r for r in data.assignee_breakdown}
        self.assertEqual(by_name["Alice"]["stale"], 1)

    def test_reporter_breakdown(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open"),
            self._make_ticket("T-2", "Done"),
            self._make_ticket("T-3", "Open", due_days_from_now=-5),
        ]
        # Set reporters manually
        tickets[0].reporter = "Carol"
        tickets[1].reporter = "Carol"
        tickets[2].reporter = "Dave"
        data = compute_dashboard_data(tickets, now=now)
        by_name = {r["reporter"]: r for r in data.reporter_breakdown}
        self.assertIn("Carol", by_name)
        self.assertIn("Dave", by_name)
        self.assertEqual(by_name["Carol"]["total"], 2)
        self.assertEqual(by_name["Carol"]["open"], 1)
        self.assertEqual(by_name["Carol"]["closed"], 1)
        self.assertEqual(by_name["Dave"]["overdue"], 1)

    def test_reporter_breakdown_unknown(self):
        now = datetime(2024, 6, 15)
        tickets = [self._make_ticket("T-1", "Open")]
        tickets[0].reporter = ""
        data = compute_dashboard_data(tickets, now=now)
        by_name = {r["reporter"]: r for r in data.reporter_breakdown}
        self.assertIn("Unknown", by_name)


class TestHTMLGeneration(unittest.TestCase):
    def test_generates_valid_html(self):
        tickets = [JiraTicket(key="T-1", summary="Test", status="Open")]
        data = compute_dashboard_data(tickets)
        html = generate_html(tickets, data, title="Test Dashboard")
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Test Dashboard", html)
        self.assertIn("</html>", html)

    def test_contains_all_sections(self):
        tickets = [JiraTicket(key="T-1", summary="Test", status="Open",
                              priority="High", issue_type="Bug")]
        data = compute_dashboard_data(tickets)
        html = generate_html(tickets, data)
        self.assertIn("chart-status", html)
        self.assertIn("chart-assignee", html)
        self.assertIn("chart-priority", html)
        self.assertIn("chart-type", html)
        self.assertIn("staleness-table", html)
        self.assertIn("assignee-breakdown", html)
        self.assertIn("reporter-breakdown", html)
        self.assertIn("ticket-table", html)
        self.assertIn("Toggle Theme", html)

    def test_escapes_html(self):
        tickets = [JiraTicket(key="T-1", summary="<script>alert('xss')</script>", status="Open")]
        data = compute_dashboard_data(tickets)
        html = generate_html(tickets, data, title="<b>Bad</b>")
        self.assertNotIn("<b>Bad</b>", html)
        self.assertIn("&lt;b&gt;Bad&lt;/b&gt;", html)

    def test_empty_dashboard(self):
        data = compute_dashboard_data([])
        html = generate_html([], data, title="Empty")
        self.assertIn("0", html)
        self.assertIn("Empty", html)


class TestEndToEnd(unittest.TestCase):
    def test_full_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = os.path.join(td, "test.csv")
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Issue key", "Summary", "Status", "Assignee",
                                 "Priority", "Issue Type", "Created", "Updated",
                                 "Resolved", "Due Date", "Comment"])
                writer.writerow(["PROJ-1", "Fix critical bug", "In Progress", "Alice",
                                 "Critical", "Bug", "2024-01-15", "2024-06-01",
                                 "", "2024-03-01",
                                 "15/Jan/24 09:30 AM;alice;Working on this"])
                writer.writerow(["PROJ-2", "Add login page", "Done", "Bob",
                                 "Medium", "Story", "2024-01-10", "2024-02-15",
                                 "2024-02-15", "",
                                 "10/Feb/24 02:00 PM;bob;Completed"])
                writer.writerow(["PROJ-3", "Refactor DB layer", "To Do", "",
                                 "Low", "Task", "2024-05-01", "2024-05-01",
                                 "", "", ""])

            output_path = os.path.join(td, "output.html")
            result = main(["--title", "Test Dashboard", "-o", output_path, csv_path])
            self.assertEqual(result, 0)

            html = Path(output_path).read_text()
            self.assertIn("Test Dashboard", html)
            self.assertIn("PROJ-1", html)
            self.assertIn("<!DOCTYPE html>", html)

    def test_cli_missing_file(self):
        result = main(["/nonexistent/file.csv"])
        self.assertEqual(result, 1)

    def test_cli_verbose(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = os.path.join(td, "test.csv")
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Issue key", "Summary", "Status"])
                writer.writerow(["V-1", "Test", "Open"])
            output_path = os.path.join(td, "out.html")
            result = main(["-v", "-o", output_path, csv_path])
            self.assertEqual(result, 0)

    def test_auto_title_from_keys(self):
        from jira_dashboard import _auto_title
        tickets = [
            JiraTicket(key="ABC-1"),
            JiraTicket(key="ABC-2"),
            JiraTicket(key="XYZ-1"),
        ]
        title = _auto_title(tickets, None)
        self.assertIn("ABC", title)
        self.assertIn("XYZ", title)

    def test_auto_title_user_override(self):
        from jira_dashboard import _auto_title
        title = _auto_title([], "My Custom Title")
        self.assertEqual(title, "My Custom Title")


class TestIsBlocked(unittest.TestCase):
    def test_blocked_statuses(self):
        for s in ["Blocked", "On Hold", "Waiting", "Impediment"]:
            self.assertTrue(_is_blocked(s), f"{s} should be blocked")

    def test_non_blocked_statuses(self):
        for s in ["Open", "In Progress", "Done", "To Do"]:
            self.assertFalse(_is_blocked(s), f"{s} should not be blocked")

    def test_case_insensitive(self):
        self.assertTrue(_is_blocked("BLOCKED"))
        self.assertTrue(_is_blocked("on hold"))


class TestSplitCsvField(unittest.TestCase):
    def test_comma_separated(self):
        self.assertEqual(_split_csv_field("a, b, c"), ["a", "b", "c"])

    def test_empty(self):
        self.assertEqual(_split_csv_field(""), [])

    def test_single(self):
        self.assertEqual(_split_csv_field("one"), ["one"])


class TestNewMetrics(unittest.TestCase):
    """Tests for all new enhancement metrics."""

    def _make_ticket(self, key="T-1", status="Open", created_days_ago=10,
                     resolved_days_ago=None, due_days_from_now=None,
                     last_comment_days_ago=None, issue_type="Task",
                     priority="Medium", assignee="Alice", reporter="Carol",
                     story_points=None, epic_link="", sprint="",
                     labels="", components="",
                     original_estimate_secs=None, time_spent_secs=None):
        now = datetime(2024, 6, 15)
        t = JiraTicket()
        t.key = key
        t.summary = f"Ticket {key}"
        t.status = status
        t.assignee = assignee
        t.reporter = reporter
        t.priority = priority
        t.issue_type = issue_type
        t.created = now - timedelta(days=created_days_ago)
        if resolved_days_ago is not None:
            t.resolved = now - timedelta(days=resolved_days_ago)
        if due_days_from_now is not None:
            t.due_date = now + timedelta(days=due_days_from_now)
        if last_comment_days_ago is not None:
            t.last_comment_date = now - timedelta(days=last_comment_days_ago)
            t.last_comment_text = "Some comment"
        if story_points is not None:
            t.story_points = story_points
        t.epic_link = epic_link
        t.sprint = sprint
        t.labels = labels
        t.components = components
        if original_estimate_secs is not None:
            t.original_estimate_secs = original_estimate_secs
        if time_spent_secs is not None:
            t.time_spent_secs = time_spent_secs
        return t

    def test_resolution_rate(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open"),
            self._make_ticket("T-2", "Done"),
            self._make_ticket("T-3", "Done"),
            self._make_ticket("T-4", "Closed"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertAlmostEqual(data.resolution_rate, 75.0)

    def test_avg_resolution_days(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Done", created_days_ago=20, resolved_days_ago=0),
            self._make_ticket("T-2", "Done", created_days_ago=10, resolved_days_ago=0),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertAlmostEqual(data.avg_resolution_days, 15.0)

    def test_unassigned_tickets(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", assignee="Unassigned"),
            self._make_ticket("T-2", "Open", assignee=""),
            self._make_ticket("T-3", "Open", assignee="Alice"),
            self._make_ticket("T-4", "Done", assignee="Unassigned"),  # closed, not counted
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(data.unassigned_tickets, 2)

    def test_blocked_tickets(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Blocked"),
            self._make_ticket("T-2", "On Hold"),
            self._make_ticket("T-3", "Open"),
            self._make_ticket("T-4", "Done"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(data.blocked_tickets, 2)

    def test_story_points_totals(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", story_points=5.0),
            self._make_ticket("T-2", "Open", story_points=3.0),
            self._make_ticket("T-3", "Done", story_points=8.0),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertAlmostEqual(data.total_story_points, 16.0)
        self.assertAlmostEqual(data.open_story_points, 8.0)

    def test_created_by_month(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", created_days_ago=30),   # ~May 2024
            self._make_ticket("T-2", "Open", created_days_ago=10),   # ~Jun 2024
            self._make_ticket("T-3", "Open", created_days_ago=5),    # ~Jun 2024
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertIn("2024-06", data.created_by_month)
        self.assertEqual(data.created_by_month["2024-06"], 2)

    def test_resolved_by_month(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Done", created_days_ago=30, resolved_days_ago=5),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertIn("2024-06", data.resolved_by_month)
        self.assertEqual(data.resolved_by_month["2024-06"], 1)

    def test_epic_progress(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", epic_link="EPIC-1", story_points=3.0),
            self._make_ticket("T-2", "Done", epic_link="EPIC-1", story_points=5.0),
            self._make_ticket("T-3", "Open", epic_link="EPIC-2"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        by_epic = {e["epic"]: e for e in data.epic_progress}
        self.assertIn("EPIC-1", by_epic)
        self.assertIn("EPIC-2", by_epic)
        self.assertEqual(by_epic["EPIC-1"]["total"], 2)
        self.assertEqual(by_epic["EPIC-1"]["closed"], 1)
        self.assertEqual(by_epic["EPIC-1"]["open"], 1)
        self.assertAlmostEqual(by_epic["EPIC-1"]["pct_done"], 50.0)
        self.assertAlmostEqual(by_epic["EPIC-1"]["story_points"], 8.0)

    def test_epic_progress_empty(self):
        now = datetime(2024, 6, 15)
        tickets = [self._make_ticket("T-1", "Open")]  # no epic_link
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(len(data.epic_progress), 0)

    def test_sprint_progress(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", sprint="Sprint 1", story_points=3.0),
            self._make_ticket("T-2", "Done", sprint="Sprint 1", story_points=5.0),
            self._make_ticket("T-3", "Open", sprint="Sprint 2"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        by_sprint = {s["sprint"]: s for s in data.sprint_progress}
        self.assertIn("Sprint 1", by_sprint)
        self.assertIn("Sprint 2", by_sprint)
        self.assertEqual(by_sprint["Sprint 1"]["total"], 2)
        self.assertEqual(by_sprint["Sprint 1"]["closed"], 1)

    def test_component_counts(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", components="Backend, API"),
            self._make_ticket("T-2", "Open", components="Backend"),
            self._make_ticket("T-3", "Done", components="Frontend"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(data.component_counts["Backend"], 2)
        self.assertEqual(data.component_counts["API"], 1)
        self.assertEqual(data.component_counts["Frontend"], 1)

    def test_label_counts(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", labels="bug, urgent"),
            self._make_ticket("T-2", "Open", labels="bug"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(data.label_counts["bug"], 2)
        self.assertEqual(data.label_counts["urgent"], 1)

    def test_estimation_accuracy(self):
        now = datetime(2024, 6, 15)
        # 8h estimated, 10h actual for a Bug
        tickets = [
            self._make_ticket("T-1", "Done", issue_type="Bug",
                              created_days_ago=10, resolved_days_ago=0,
                              original_estimate_secs=8 * 3600,
                              time_spent_secs=10 * 3600),
            self._make_ticket("T-2", "Done", issue_type="Bug",
                              created_days_ago=5, resolved_days_ago=0,
                              original_estimate_secs=4 * 3600,
                              time_spent_secs=6 * 3600),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertTrue(len(data.estimation_accuracy) > 0)
        bug_row = [e for e in data.estimation_accuracy if e["type"] == "Bug"]
        self.assertEqual(len(bug_row), 1)
        self.assertEqual(bug_row[0]["count"], 2)
        self.assertIn("accuracy_pct", bug_row[0])

    def test_estimation_accuracy_empty(self):
        now = datetime(2024, 6, 15)
        tickets = [self._make_ticket("T-1", "Open")]  # no estimates
        data = compute_dashboard_data(tickets, now=now)
        self.assertEqual(len(data.estimation_accuracy), 0)

    def test_avg_resolution_by_priority(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Done", priority="High",
                              created_days_ago=10, resolved_days_ago=0),
            self._make_ticket("T-2", "Done", priority="Low",
                              created_days_ago=20, resolved_days_ago=0),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertIn("High", data.avg_resolution_by_priority)
        self.assertAlmostEqual(data.avg_resolution_by_priority["High"], 10.0)
        self.assertAlmostEqual(data.avg_resolution_by_priority["Low"], 20.0)

    def test_reporter_assignee_matrix(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", reporter="Carol", assignee="Alice"),
            self._make_ticket("T-2", "Open", reporter="Carol", assignee="Alice"),
            self._make_ticket("T-3", "Open", reporter="Carol", assignee="Bob"),
            self._make_ticket("T-4", "Done", reporter="Dave", assignee="Alice"),
        ]
        data = compute_dashboard_data(tickets, now=now)
        self.assertTrue(len(data.reporter_assignee_matrix) > 0)
        ca = [r for r in data.reporter_assignee_matrix
              if r["reporter"] == "Carol" and r["assignee"] == "Alice"]
        self.assertEqual(len(ca), 1)
        self.assertEqual(ca[0]["count"], 2)

    def test_assignee_breakdown_story_points(self):
        now = datetime(2024, 6, 15)
        tickets = [
            self._make_ticket("T-1", "Open", assignee="Alice", story_points=5.0),
            self._make_ticket("T-2", "Done", assignee="Alice", story_points=3.0),
        ]
        data = compute_dashboard_data(tickets, now=now)
        by_name = {r["assignee"]: r for r in data.assignee_breakdown}
        self.assertIn("story_points", by_name["Alice"])
        self.assertAlmostEqual(by_name["Alice"]["story_points"], 8.0)


class TestNewHTMLSections(unittest.TestCase):
    """Tests that new HTML sections appear in generated output."""

    def test_new_sections_present(self):
        tickets = [
            JiraTicket(key="T-1", summary="Test", status="Open",
                       priority="High", issue_type="Bug",
                       epic_link="EPIC-1", sprint="Sprint 1",
                       components="Backend", labels="urgent"),
        ]
        tickets[0].created = datetime(2024, 1, 15)
        data = compute_dashboard_data(tickets)
        html_out = generate_html(tickets, data)
        # Check for new section IDs / headings
        self.assertIn("Created vs Resolved", html_out)
        self.assertIn("Epic Progress", html_out)
        self.assertIn("Sprint Progress", html_out)
        self.assertIn("Component", html_out)
        self.assertIn("Label", html_out)
        self.assertIn("Priority SLA", html_out)
        self.assertIn("Estimation Accuracy", html_out)
        self.assertIn("Reporter", html_out)

    def test_summary_cards_expanded(self):
        tickets = [JiraTicket(key="T-1", summary="Test", status="Open")]
        data = compute_dashboard_data(tickets)
        html_out = generate_html(tickets, data)
        # Check for new summary card labels
        self.assertIn("Resolution Rate", html_out)
        self.assertIn("Unassigned", html_out)
        self.assertIn("Story Points", html_out)


class TestCommentExtraction(unittest.TestCase):
    def test_semicolon_format(self):
        headers = ["Comment"]
        row = ["15/Jan/24 09:30 AM;user;This is the comment text"]
        comment_cols = _find_comment_columns(headers)
        date, text = _extract_comments(row, comment_cols)
        self.assertIsNotNone(date)
        self.assertIn("comment text", text)

    def test_no_date_in_comment(self):
        headers = ["Comment"]
        row = ["Just a plain comment without date"]
        comment_cols = _find_comment_columns(headers)
        date, text = _extract_comments(row, comment_cols)
        self.assertIsNone(date)
        self.assertIn("plain comment", text)

    def test_multiple_comment_columns(self):
        headers = ["Comment", "Comment.1"]
        row = ["15/Jan/24 09:30 AM;user1;Old comment",
               "20/Jan/24 02:00 PM;user2;Newer comment"]
        comment_cols = _find_comment_columns(headers)
        date, text = _extract_comments(row, comment_cols)
        self.assertIsNotNone(date)
        self.assertEqual(date.day, 20)
        self.assertIn("Newer", text)

    def test_empty_comments(self):
        headers = ["Comment"]
        row = [""]
        comment_cols = _find_comment_columns(headers)
        date, text = _extract_comments(row, comment_cols)
        self.assertIsNone(date)
        self.assertEqual(text, "")


class TestAutoDetect(unittest.TestCase):
    """Tests for _detect_source auto-detection."""

    def test_jira_headers(self):
        headers = ["Issue key", "Summary", "Status", "Sprint", "Epic Link", "Story Points"]
        self.assertEqual(_detect_source(headers), "jira")

    def test_servicenow_headers(self):
        headers = ["Number", "Short description", "State", "Opened at",
                   "Assignment group", "Made SLA", "Contact type"]
        self.assertEqual(_detect_source(headers), "servicenow")

    def test_ambiguous_defaults_jira(self):
        headers = ["Key", "Summary", "Status", "Priority"]
        self.assertEqual(_detect_source(headers), "jira")

    def test_custom_field_boosts_jira(self):
        headers = ["Number", "Summary", "Status", "Custom field (Story Points)"]
        self.assertEqual(_detect_source(headers), "jira")

    def test_sn_indicators_win(self):
        headers = ["Number", "Short description", "State", "Opened at",
                   "Assignment group", "Made SLA", "Reassignment count",
                   "Configuration item"]
        self.assertEqual(_detect_source(headers), "servicenow")

    def test_sn_underscore_headers(self):
        """Real-world SN exports use underscore internal field names."""
        headers = ["number", "short_description", "state", "opened_at",
                   "assignment_group", "made_sla", "contact_type",
                   "caller_id", "resolved_at", "reassignment_count",
                   "sys_class_name", "sys_created_on", "incident_state",
                   "service_offering", "business_service"]
        self.assertEqual(_detect_source(headers), "servicenow")


class TestServiceNowParsing(unittest.TestCase):
    """Tests for parsing ServiceNow CSV exports."""

    def _write_csv(self, tmpdir, rows, filename="sn_test.csv"):
        path = os.path.join(tmpdir, filename)
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            for row in rows:
                writer.writerow(row)
        return path

    def test_basic_sn_csv(self):
        config = _servicenow_config()
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["Number", "Short description", "State", "Assigned to",
                 "Priority", "Opened at", "Category", "Assignment group",
                 "Made SLA", "Reassignment count", "Reopen count"],
                ["INC0001", "Server down", "In Progress", "Alice",
                 "1 - Critical", "2024-01-15 09:00:00", "Hardware", "IT Support",
                 "true", "2", "0"],
                ["INC0002", "Password reset", "Closed", "Bob",
                 "4 - Low", "2024-01-10 10:00:00", "Software", "Help Desk",
                 "false", "1", "1"],
            ])
            tickets = _parse_csv(path, config)
            self.assertEqual(len(tickets), 2)
            self.assertEqual(tickets[0].key, "INC0001")
            self.assertEqual(tickets[0].status, "In Progress")
            self.assertEqual(tickets[0].category, "Hardware")
            self.assertEqual(tickets[0].assignment_group, "IT Support")
            self.assertTrue(tickets[0].made_sla)
            self.assertEqual(tickets[0].reassignment_count, 2)
            self.assertEqual(tickets[0].reopen_count, 0)
            self.assertFalse(tickets[1].made_sla)
            self.assertEqual(tickets[1].reopen_count, 1)

    def test_underscore_column_names(self):
        """Real-world SN exports use underscore internal field names."""
        config = _servicenow_config()
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["number", "short_description", "state", "assigned_to",
                 "priority", "opened_at", "category", "assignment_group",
                 "made_sla", "reassignment_count", "reopen_count",
                 "sys_class_name", "caller_id", "contact_type",
                 "incident_state", "opened_by", "resolved_at"],
                ["INC0001", "Server down", "In Progress", "Alice",
                 "1 - Critical", "2024-01-15 09:00:00", "Hardware", "IT Support",
                 "true", "2", "0", "incident", "Carol", "Phone",
                 "2", "Dave", ""],
            ])
            tickets = _parse_csv(path, config)
            self.assertEqual(len(tickets), 1)
            t = tickets[0]
            self.assertEqual(t.key, "INC0001")
            self.assertEqual(t.summary, "Server down")
            self.assertEqual(t.status, "In Progress")
            self.assertEqual(t.assignee, "Alice")
            self.assertEqual(t.category, "Hardware")
            self.assertEqual(t.assignment_group, "IT Support")
            self.assertTrue(t.made_sla)
            self.assertEqual(t.reassignment_count, 2)
            self.assertEqual(t.contact_type, "Phone")
            # issue_type should come from sys_class_name, NOT category
            self.assertEqual(t.issue_type, "incident")
            # reporter should come from caller_id (preferred) over opened_by
            self.assertEqual(t.reporter, "Carol")

    def test_category_not_stolen_by_issue_type(self):
        """category column should map to category field, not issue_type."""
        config = _servicenow_config()
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["number", "short_description", "state", "category",
                 "sys_class_name"],
                ["INC0001", "Test", "New", "Network", "incident"],
            ])
            tickets = _parse_csv(path, config)
            self.assertEqual(tickets[0].category, "Network")
            self.assertEqual(tickets[0].issue_type, "incident")

    def test_sn_status_recognition(self):
        config = _servicenow_config()
        self.assertFalse(_is_open("Closed", config))
        self.assertFalse(_is_open("Resolved", config))
        self.assertFalse(_is_open("Cancelled", config))
        self.assertTrue(_is_open("New", config))
        self.assertTrue(_is_open("In Progress", config))
        self.assertTrue(_is_open("On Hold", config))

    def test_sn_blocked_recognition(self):
        config = _servicenow_config()
        self.assertTrue(_is_blocked("On Hold", config))
        self.assertTrue(_is_blocked("Pending", config))
        self.assertFalse(_is_blocked("In Progress", config))

    def test_sn_unassigned_default(self):
        config = _servicenow_config()
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["Number", "Short description", "State"],
                ["INC0003", "No assignee", "New"],
            ])
            tickets = _parse_csv(path, config)
            self.assertEqual(tickets[0].assignee, "Unassigned")

    def test_sn_dates_parsed(self):
        config = _servicenow_config()
        with tempfile.TemporaryDirectory() as td:
            path = self._write_csv(td, [
                ["Number", "Short description", "State", "Opened at",
                 "Updated at", "Resolved at"],
                ["INC0004", "Test dates", "Resolved",
                 "2024-01-15 09:00:00", "2024-02-01 14:00:00", "2024-02-01 14:00:00"],
            ])
            tickets = _parse_csv(path, config)
            self.assertIsNotNone(tickets[0].created)
            self.assertIsNotNone(tickets[0].updated)
            self.assertIsNotNone(tickets[0].resolved)


class TestServiceNowMetrics(unittest.TestCase):
    """Tests for ServiceNow-specific metric computations."""

    def _make_sn_ticket(self, key="INC0001", status="New", created_days_ago=10,
                         resolved_days_ago=None, priority="3 - Moderate",
                         assignee="Alice", category="Software",
                         assignment_group="Help Desk", made_sla=None,
                         reassignment_count=None, reopen_count=None,
                         contact_type="", escalation=""):
        now = datetime(2024, 6, 15)
        t = JiraTicket()
        t.key = key
        t.summary = f"Ticket {key}"
        t.status = status
        t.assignee = assignee
        t.priority = priority
        t.issue_type = "Incident"
        t.created = now - timedelta(days=created_days_ago)
        if resolved_days_ago is not None:
            t.resolved = now - timedelta(days=resolved_days_ago)
        t.category = category
        t.assignment_group = assignment_group
        t.made_sla = made_sla
        t.reassignment_count = reassignment_count
        t.reopen_count = reopen_count
        t.contact_type = contact_type
        t.escalation = escalation
        return t

    def test_sla_compliance(self):
        now = datetime(2024, 6, 15)
        config = _servicenow_config()
        tickets = [
            self._make_sn_ticket("INC0001", "Closed", made_sla=True),
            self._make_sn_ticket("INC0002", "Closed", made_sla=True),
            self._make_sn_ticket("INC0003", "Closed", made_sla=False),
        ]
        data = compute_dashboard_data(tickets, now=now, config=config)
        self.assertEqual(data.sla_met_count, 2)
        self.assertEqual(data.sla_missed_count, 1)
        self.assertAlmostEqual(data.sla_compliance_pct, 66.7)

    def test_category_counts(self):
        now = datetime(2024, 6, 15)
        config = _servicenow_config()
        tickets = [
            self._make_sn_ticket("INC0001", category="Software"),
            self._make_sn_ticket("INC0002", category="Software"),
            self._make_sn_ticket("INC0003", category="Hardware"),
        ]
        data = compute_dashboard_data(tickets, now=now, config=config)
        self.assertEqual(data.category_counts["Software"], 2)
        self.assertEqual(data.category_counts["Hardware"], 1)

    def test_assignment_group_counts(self):
        now = datetime(2024, 6, 15)
        config = _servicenow_config()
        tickets = [
            self._make_sn_ticket("INC0001", assignment_group="Help Desk"),
            self._make_sn_ticket("INC0002", assignment_group="Help Desk"),
            self._make_sn_ticket("INC0003", assignment_group="IT Support"),
        ]
        data = compute_dashboard_data(tickets, now=now, config=config)
        self.assertEqual(data.assignment_group_counts["Help Desk"], 2)
        self.assertEqual(data.assignment_group_counts["IT Support"], 1)

    def test_assignment_group_breakdown(self):
        now = datetime(2024, 6, 15)
        config = _servicenow_config()
        tickets = [
            self._make_sn_ticket("INC0001", "New", assignment_group="Help Desk", made_sla=True),
            self._make_sn_ticket("INC0002", "Closed", assignment_group="Help Desk", made_sla=False),
            self._make_sn_ticket("INC0003", "Closed", assignment_group="IT Support", made_sla=True),
        ]
        data = compute_dashboard_data(tickets, now=now, config=config)
        by_group = {r["group"]: r for r in data.assignment_group_breakdown}
        self.assertIn("Help Desk", by_group)
        self.assertEqual(by_group["Help Desk"]["total"], 2)
        self.assertEqual(by_group["Help Desk"]["open"], 1)
        self.assertEqual(by_group["Help Desk"]["closed"], 1)
        self.assertAlmostEqual(by_group["Help Desk"]["sla_pct"], 50.0)

    def test_escalation_counts(self):
        now = datetime(2024, 6, 15)
        config = _servicenow_config()
        tickets = [
            self._make_sn_ticket("INC0001", escalation="Normal"),
            self._make_sn_ticket("INC0002", escalation="Normal"),
            self._make_sn_ticket("INC0003", escalation="Overdue"),
        ]
        data = compute_dashboard_data(tickets, now=now, config=config)
        self.assertEqual(data.escalation_counts["Normal"], 2)
        self.assertEqual(data.escalation_counts["Overdue"], 1)

    def test_reassignment_and_reopen(self):
        now = datetime(2024, 6, 15)
        config = _servicenow_config()
        tickets = [
            self._make_sn_ticket("INC0001", reassignment_count=2, reopen_count=1),
            self._make_sn_ticket("INC0002", reassignment_count=4, reopen_count=0),
        ]
        data = compute_dashboard_data(tickets, now=now, config=config)
        self.assertAlmostEqual(data.avg_reassignment_count, 3.0)
        self.assertAlmostEqual(data.avg_reopen_count, 0.5)

    def test_contact_type_counts(self):
        now = datetime(2024, 6, 15)
        config = _servicenow_config()
        tickets = [
            self._make_sn_ticket("INC0001", contact_type="Phone"),
            self._make_sn_ticket("INC0002", contact_type="Email"),
            self._make_sn_ticket("INC0003", contact_type="Phone"),
        ]
        data = compute_dashboard_data(tickets, now=now, config=config)
        self.assertEqual(data.contact_type_counts["Phone"], 2)
        self.assertEqual(data.contact_type_counts["Email"], 1)

    def test_sla_by_priority(self):
        now = datetime(2024, 6, 15)
        config = _servicenow_config()
        tickets = [
            self._make_sn_ticket("INC0001", priority="1 - Critical", made_sla=True),
            self._make_sn_ticket("INC0002", priority="1 - Critical", made_sla=False),
            self._make_sn_ticket("INC0003", priority="4 - Low", made_sla=True),
        ]
        data = compute_dashboard_data(tickets, now=now, config=config)
        self.assertIn("1 - Critical", data.sla_by_priority)
        self.assertEqual(data.sla_by_priority["1 - Critical"]["met"], 1)
        self.assertEqual(data.sla_by_priority["1 - Critical"]["missed"], 1)
        self.assertEqual(data.sla_by_priority["4 - Low"]["met"], 1)

    def test_source_type_set(self):
        now = datetime(2024, 6, 15)
        config = _servicenow_config()
        data = compute_dashboard_data([], now=now, config=config)
        self.assertEqual(data.source_type, "servicenow")


class TestServiceNowHTML(unittest.TestCase):
    """Tests that SN-specific HTML sections appear and Jira sections are absent."""

    def _make_sn_ticket(self, key="INC0001", status="New"):
        t = JiraTicket(key=key, summary="Test SN", status=status,
                       priority="3 - Moderate", issue_type="Incident")
        t.created = datetime(2024, 1, 15)
        t.category = "Software"
        t.assignment_group = "Help Desk"
        t.made_sla = True
        t.contact_type = "Phone"
        t.escalation = "Normal"
        return t

    def test_sn_sections_present(self):
        config = _servicenow_config()
        tickets = [self._make_sn_ticket()]
        data = compute_dashboard_data(tickets, config=config)
        html_out = generate_html(tickets, data, config=config)
        self.assertIn("Category Breakdown", html_out)
        self.assertIn("Assignment Group Breakdown", html_out)
        self.assertIn("Contact Type Distribution", html_out)
        self.assertIn("Escalation Analysis", html_out)
        self.assertIn("SLA Compliance by Priority", html_out)
        self.assertIn("SLA Compliance", html_out)

    def test_jira_sections_absent_in_sn(self):
        config = _servicenow_config()
        tickets = [self._make_sn_ticket()]
        data = compute_dashboard_data(tickets, config=config)
        html_out = generate_html(tickets, data, config=config)
        self.assertNotIn("Epic Progress", html_out)
        self.assertNotIn("Sprint Progress", html_out)
        self.assertNotIn("Estimation Accuracy", html_out)
        self.assertNotIn("Components", html_out)

    def test_sn_eighth_card_is_sla(self):
        config = _servicenow_config()
        tickets = [self._make_sn_ticket()]
        data = compute_dashboard_data(tickets, config=config)
        html_out = generate_html(tickets, data, config=config)
        self.assertIn("SLA Compliance", html_out)
        self.assertNotIn("Story Points", html_out)

    def test_sn_extra_cards_present(self):
        config = _servicenow_config()
        tickets = [self._make_sn_ticket()]
        data = compute_dashboard_data(tickets, config=config)
        html_out = generate_html(tickets, data, config=config)
        self.assertIn("Avg Reassignments", html_out)
        self.assertIn("Avg Reopens", html_out)


class TestBackwardsCompat(unittest.TestCase):
    """Tests that backwards-compat aliases still work."""

    def test_jiraticket_alias(self):
        # JiraTicket should still be importable and usable
        t = JiraTicket(key="T-1", summary="Test")
        self.assertEqual(t.key, "T-1")

    def test_ticket_alias(self):
        # Ticket should be an alias for JiraTicket
        t = Ticket(key="T-2", summary="Test2")
        self.assertIsInstance(t, JiraTicket)

    def test_parse_jira_csv_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "test.csv")
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Issue key", "Summary", "Status"])
                writer.writerow(["BC-1", "Compat test", "Open"])
            tickets = parse_jira_csv(path)
            self.assertEqual(len(tickets), 1)
            self.assertEqual(tickets[0].key, "BC-1")

    def test_column_aliases_still_accessible(self):
        self.assertIn("key", COLUMN_ALIASES)
        self.assertIn("summary", COLUMN_ALIASES)

    def test_build_alias_lookup_no_config(self):
        headers = ["Issue key", "Summary", "Status"]
        lookup = _build_alias_lookup(headers)
        self.assertEqual(lookup["key"], [0])


class TestServiceNowEndToEnd(unittest.TestCase):
    """End-to-end test for ServiceNow CSV pipeline."""

    def test_full_sn_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = os.path.join(td, "sn_export.csv")
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Number", "Short description", "State",
                                 "Assigned to", "Priority", "Opened at",
                                 "Resolved at", "Category", "Assignment group",
                                 "Made SLA", "Contact type", "Escalation",
                                 "Reassignment count", "Reopen count", "Opened by"])
                writer.writerow(["INC0001", "Server down", "In Progress",
                                 "Alice", "1 - Critical", "2024-01-15",
                                 "", "Hardware", "IT Support",
                                 "true", "Phone", "Normal", "2", "0", "Carol"])
                writer.writerow(["INC0002", "Password reset", "Closed",
                                 "Bob", "4 - Low", "2024-01-10",
                                 "2024-01-11", "Software", "Help Desk",
                                 "false", "Email", "Normal", "1", "1", "Dave"])

            output_path = os.path.join(td, "sn_output.html")
            result = main(["--source", "servicenow", "-o", output_path, csv_path])
            self.assertEqual(result, 0)

            html_content = Path(output_path).read_text()
            self.assertIn("<!DOCTYPE html>", html_content)
            self.assertIn("INC0001", html_content)
            self.assertIn("Category Breakdown", html_content)
            self.assertNotIn("Epic Progress", html_content)

    def test_auto_detect_sn(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = os.path.join(td, "auto.csv")
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Number", "Short description", "State",
                                 "Opened at", "Assignment group", "Made SLA"])
                writer.writerow(["INC0001", "Auto test", "New",
                                 "2024-01-15", "Help Desk", "true"])

            output_path = os.path.join(td, "auto_output.html")
            result = main(["-o", output_path, csv_path])
            self.assertEqual(result, 0)

            html_content = Path(output_path).read_text()
            self.assertIn("SLA Compliance", html_content)

    def test_sn_auto_title(self):
        from jira_dashboard import _auto_title
        config = _servicenow_config()
        tickets = [
            JiraTicket(key="INC0001"),
            JiraTicket(key="INC0002"),
            JiraTicket(key="CHG0001"),
        ]
        title = _auto_title(tickets, None, config)
        self.assertIn("Incident", title)

    def test_sn_auto_title_no_tickets(self):
        from jira_dashboard import _auto_title
        config = _servicenow_config()
        title = _auto_title([], None, config)
        self.assertEqual(title, "ServiceNow Dashboard")

    def test_source_flag_jira(self):
        with tempfile.TemporaryDirectory() as td:
            csv_path = os.path.join(td, "jira.csv")
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Issue key", "Summary", "Status"])
                writer.writerow(["PROJ-1", "Test", "Open"])

            output_path = os.path.join(td, "jira_out.html")
            result = main(["--source", "jira", "-o", output_path, csv_path])
            self.assertEqual(result, 0)

            html_content = Path(output_path).read_text()
            self.assertIn("Story Points", html_content)


if __name__ == "__main__":
    unittest.main()
