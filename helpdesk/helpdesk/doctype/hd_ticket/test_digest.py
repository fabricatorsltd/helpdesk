# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

from datetime import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import convert_utc_to_timezone

from helpdesk.helpdesk.doctype.hd_ticket import digest
from helpdesk.test_utils import make_agent, make_ticket

DIGEST_HOUR = 8
ROME = "digest_rome@test.com"
NEW_YORK = "digest_ny@test.com"
DISABLED = "digest_disabled@test.com"
INACTIVE = "digest_inactive@test.com"
AGENTS = (ROME, NEW_YORK, DISABLED, INACTIVE)

# 06:05 UTC on a Tuesday: 08:05 in Rome (CEST), 02:05 in New York (EDT)
ROME_MORNING = datetime(2026, 9, 8, 6, 5)
# the same Tuesday, six hours on: 08:05 in New York, 14:05 in Rome
NEW_YORK_MORNING = datetime(2026, 9, 8, 12, 5)
SATURDAY_ROME_MORNING = datetime(2026, 9, 12, 6, 5)
SUNDAY_ROME_MORNING = datetime(2026, 9, 13, 6, 5)


class TestAgentDigest(FrappeTestCase):
    def setUp(self):
        # The settings and the sent mark are custom fields created by
        # fab_helpdesk, so a plain helpdesk site has nothing to test here.
        if not frappe.db.has_column("HD Agent", "fab_digest_sent_on"):
            self.skipTest("the digest custom fields are not installed on this site")

        for email, time_zone, enabled, is_active in (
            (ROME, "Europe/Rome", 1, 1),
            (NEW_YORK, "America/New_York", 1, 1),
            (DISABLED, "Europe/Rome", 0, 1),
            (INACTIVE, "Europe/Rome", 1, 0),
        ):
            make_agent(email)
            frappe.db.set_value(
                "User", email, {"time_zone": time_zone, "enabled": enabled}
            )
            frappe.db.set_value(
                "HD Agent", email, {"is_active": is_active, "fab_digest_sent_on": None}
            )

        self.ticket = make_ticket(subject="Digest ticket", status="Open")
        frappe.db.set_value(
            "HD Ticket", self.ticket.name, "_assign", frappe.as_json([ROME])
        )

        frappe.db.set_single_value("HD Settings", "fab_digest_enabled", 1)
        frappe.db.set_single_value("HD Settings", "fab_digest_hour", DIGEST_HOUR)

    def tearDown(self):
        frappe.db.set_single_value("HD Settings", "fab_digest_enabled", 0)
        frappe.delete_doc(
            "HD Ticket", self.ticket.name, force=True, delete_permanently=True
        )
        for email in AGENTS:
            frappe.db.set_value("HD Agent", email, "fab_digest_sent_on", None)

    def run_at(self, utc):
        """Run the job with the send-or-not clock frozen at `utc`, and report
        which of the fixture agents were mailed. The real timezone conversion
        is applied to the frozen instant, so what decides is the production
        arithmetic. Only the timezone clock is frozen: the ticket ages in the
        body still come from the real one, and are not asserted on here.

        Other agents live on the site, so only the fixtures are asserted on.
        """
        with (
            patch.object(
                digest,
                "get_datetime_in_timezone",
                lambda tz: convert_utc_to_timezone(utc, tz),
            ),
            patch.object(frappe, "sendmail") as sendmail,
        ):
            digest.run()

        mailed = set()
        for call in sendmail.call_args_list:
            mailed.update(call.kwargs["recipients"])
        return mailed & set(AGENTS)

    def test_sends_at_the_configured_hour_on_a_weekday(self):
        self.assertEqual(self.run_at(ROME_MORNING), {ROME})

    def test_sends_once_a_day_however_often_the_job_runs(self):
        self.assertEqual(self.run_at(ROME_MORNING), {ROME})
        self.assertEqual(self.run_at(ROME_MORNING), set())

    def test_skips_hours_other_than_the_configured_one(self):
        self.assertEqual(self.run_at(datetime(2026, 9, 8, 7, 5)), set())

    def test_skips_the_weekend(self):
        self.assertEqual(self.run_at(SATURDAY_ROME_MORNING), set())
        self.assertEqual(self.run_at(SUNDAY_ROME_MORNING), set())

    def test_follows_the_recipient_timezone_and_not_the_server_one(self):
        self.assertEqual(self.run_at(NEW_YORK_MORNING), {NEW_YORK})

    def test_skips_disabled_users_and_inactive_agents(self):
        self.assertNotIn(DISABLED, self.run_at(ROME_MORNING))
        frappe.db.set_value("HD Agent", ROME, "fab_digest_sent_on", None)
        self.assertNotIn(INACTIVE, self.run_at(ROME_MORNING))

    def test_sends_nothing_when_the_feature_is_off(self):
        frappe.db.set_single_value("HD Settings", "fab_digest_enabled", 0)
        self.assertEqual(self.run_at(ROME_MORNING), set())

    def test_sends_nothing_when_no_ticket_is_open(self):
        # the whole site has to be quiet, not just this test's own ticket:
        # anything another test left open would keep the digest alive
        for name in frappe.get_all(
            "HD Ticket", filters={"status_category": "Open"}, pluck="name"
        ):
            frappe.db.set_value(
                "HD Ticket", name, {"status": "Replied", "status_category": "Paused"}
            )
        self.assertEqual(self.run_at(ROME_MORNING), set())

    def test_selects_by_status_category_not_by_the_status_label(self):
        """A paused ticket is not open however its status is labelled, and a
        renamed Open-category status still counts."""
        # the first half asserts that nothing goes out, so the whole site has to
        # be quiet: a ticket another test left open would keep the digest alive
        # and the assertion would depend on the order the suite happens to run in
        for name in frappe.get_all(
            "HD Ticket",
            filters={"status_category": "Open", "name": ("!=", self.ticket.name)},
            pluck="name",
        ):
            frappe.db.set_value(
                "HD Ticket", name, {"status": "Replied", "status_category": "Paused"}
            )
        frappe.db.set_value(
            "HD Ticket",
            self.ticket.name,
            {"status": "Replied", "status_category": "Paused"},
        )
        self.assertEqual(self.run_at(ROME_MORNING), set())

        frappe.db.set_value("HD Agent", ROME, "fab_digest_sent_on", None)
        frappe.db.set_value(
            "HD Ticket",
            self.ticket.name,
            {"status": "Replied", "status_category": "Open"},
        )
        self.assertEqual(self.run_at(ROME_MORNING), {ROME})

    def test_the_two_sections_partition_the_backlog(self):
        """A ticket assigned to the recipient, even alongside another agent,
        belongs to the first section only; an unassigned one to the second."""
        frappe.db.set_value(
            "HD Ticket", self.ticket.name, "_assign", frappe.as_json([ROME, NEW_YORK])
        )
        other = make_ticket(subject="Unassigned digest ticket", status="Open")
        self.addCleanup(
            frappe.delete_doc,
            "HD Ticket",
            other.name,
            force=True,
            delete_permanently=True,
        )

        with (
            patch.object(
                digest,
                "get_datetime_in_timezone",
                lambda tz: convert_utc_to_timezone(ROME_MORNING, tz),
            ),
            patch.object(frappe, "sendmail") as sendmail,
        ):
            digest.run()

        message = next(
            call.kwargs["message"]
            for call in sendmail.call_args_list
            if call.kwargs["recipients"] == [ROME]
        )
        self.assertEqual(message.count(f"#{self.ticket.name}<"), 1)
        self.assertEqual(message.count(f"#{other.name}<"), 1)
        assigned_section, others_section = message.split('<p style="margin-top:20px')[
            1:3
        ]
        self.assertIn(f"#{self.ticket.name}<", assigned_section)
        self.assertIn(f"#{other.name}<", others_section)
