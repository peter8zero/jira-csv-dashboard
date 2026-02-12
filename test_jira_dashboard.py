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
    JiraTicket,
    _build_alias_lookup,
    _extract_comments,
    _find_comment_columns,
    _is_open,
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
        self.assertEqual(lookup["key"], 0)
        self.assertEqual(lookup["summary"], 1)
        self.assertEqual(lookup["status"], 2)
        self.assertEqual(lookup["assignee"], 3)
        self.assertEqual(lookup["priority"], 4)

    def test_alternate_headers(self):
        headers = ["Key", "Title", "Issue Status", "Assigned To"]
        lookup = _build_alias_lookup(headers)
        self.assertEqual(lookup["key"], 0)
        self.assertEqual(lookup["summary"], 1)  # "title" is a summary alias
        self.assertEqual(lookup["status"], 2)
        self.assertEqual(lookup["assignee"], 3)

    def test_case_insensitive(self):
        headers = ["ISSUE KEY", "SUMMARY", "STATUS"]
        lookup = _build_alias_lookup(headers)
        self.assertEqual(lookup["key"], 0)
        self.assertEqual(lookup["summary"], 1)

    def test_missing_columns(self):
        headers = ["Issue key", "Summary"]
        lookup = _build_alias_lookup(headers)
        self.assertEqual(lookup["key"], 0)
        self.assertIsNone(lookup["priority"])

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


if __name__ == "__main__":
    unittest.main()
