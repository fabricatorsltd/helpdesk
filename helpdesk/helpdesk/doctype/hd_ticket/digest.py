"""Email each agent the tickets still open, on weekday mornings.

The job runs every hour and sends nothing on most of them: a recipient is
served only when the clock in their own timezone has just reached the
configured hour, on a weekday counted in that same timezone. The server
timezone never enters the decision, so agents spread over several countries
each get their own morning.

The whole open backlog is read once and grouped in memory, so the cost does not
grow with the number of agents. Each recipient is marked and mailed inside the
same transaction, which is what keeps the digest to one per person per day.

The settings live on HD Settings as custom fields created by fab_helpdesk; on a
site without them the job is a no-op.
"""

import frappe
from frappe import _
from frappe.utils import (
    cint,
    escape_html,
    format_date,
    get_datetime,
    get_datetime_in_timezone,
    get_system_timezone,
    get_url,
    getdate,
    now_datetime,
)

from helpdesk.helpdesk.doctype.hd_settings.helpers import (
    get_default_language,
    use_language,
)

# Saturday and Sunday, as datetime.weekday() numbers them
WEEKEND = (5, 6)
# tickets with an unknown priority sort last rather than disappearing
UNRANKED_PRIORITY = 999999


def run():
    settings = frappe.db.get_value(
        "HD Settings",
        "HD Settings",
        ["fab_digest_enabled", "fab_digest_hour"],
        as_dict=True,
    )
    if not settings or not cint(settings.fab_digest_enabled):
        return

    hour = cint(settings.fab_digest_hour)
    if hour < 0 or hour > 23:
        return

    recipients = get_recipients()
    if not recipients:
        return

    # Nothing open anywhere means nobody hears from us at all, not even an empty
    # digest. An agent with nothing assigned still gets the mail as long as the
    # helpdesk has something open: the second section is the point of it.
    tickets = get_open_tickets()
    if not tickets:
        return

    now = now_datetime()
    for recipient in recipients:
        try:
            handle_recipient(recipient, tickets, hour, now)
        except Exception as e:
            # roll back first: an Error Log written inside the failed transaction
            # would be rolled back with it, and the failure would go unrecorded
            frappe.db.rollback()
            frappe.log_error(
                message=f"Failed to send the open ticket digest to {recipient.email}. Error: {e}",
                title="Agent Digest Job Failed",
            )
            frappe.db.commit()  # nosemgrep
            continue

        frappe.db.commit()  # nosemgrep


def get_recipients():
    """Active agents whose user account is enabled and has a real address.

    Read in one statement together with the language, the timezone and the day
    the last digest went out, so the per-recipient checks cost nothing.

    `user` and `email` are both carried because they are not interchangeable:
    the mail goes to the address, while _assign holds the User name.
    """
    return frappe.db.sql(
        """
            SELECT a.name AS agent, a.user, a.fab_digest_sent_on AS sent_on,
                u.email, u.language, u.time_zone
            FROM `tabHD Agent` a
            INNER JOIN `tabUser` u ON u.name = a.user
            WHERE a.is_active = 1 AND u.enabled = 1 AND u.email LIKE '%@%'
        """,
        as_dict=True,
    )


def get_open_tickets():
    """The open backlog, once, in the order the digest lists it.

    Selected by status category rather than by the status label: statuses are
    editable records, and the app asks the same question the same way
    everywhere else (see permission_query and the shipped "My open tickets"
    views). Only the shipped Open status is in that category, so this is the
    Open tickets that were asked for, minus the way a rename would have
    emptied the digest with nothing to show for it.

    Highest priority first, and within a priority the ticket that has been
    waiting longest. The priority weight comes from the priority record itself
    (P1 is 100, P4 is 400), so renaming a level never reorders the mail.
    """
    tickets = frappe.db.sql(
        """
            SELECT t.name, t.subject, t.customer, t.priority, t.creation,
                t._assign, COALESCE(p.integer_value, %(unranked)s) AS weight
            FROM `tabHD Ticket` t
            LEFT JOIN `tabHD Ticket Priority` p ON p.name = t.priority
            WHERE t.status_category = 'Open' AND t.is_merged = 0
            ORDER BY weight ASC, t.creation ASC
        """,
        {"unranked": UNRANKED_PRIORITY},
        as_dict=True,
    )

    for ticket in tickets:
        ticket.assignees = set(frappe.parse_json(ticket._assign) or [])

    return tickets


def handle_recipient(recipient, tickets, hour, now):
    """Send this recipient their digest, if their own morning has just struck."""
    time_zone = recipient.time_zone or get_system_timezone()
    local_now = get_datetime_in_timezone(time_zone)
    if local_now.hour != hour or local_now.weekday() in WEEKEND:
        return

    local_today = local_now.date()
    # cheap first pass on the value read with the recipients: on all but the one
    # run a day that finds work to do, this ends it without taking a lock
    if recipient.sent_on and getdate(recipient.sent_on) == local_today:
        return
    if not claim_day(recipient, local_today):
        return

    # The two sections partition the backlog, so a ticket assigned to several
    # agents is in the first section for each of them and in the second for
    # nobody, and an unassigned ticket is in the second section for everyone.
    # Matched on the User name, which is what _assign stores; the address the
    # mail goes to is a different field and the two can drift apart.
    mine = [t for t in tickets if recipient.user in t.assignees]
    others = [t for t in tickets if recipient.user not in t.assignees]

    with use_language(recipient.language or get_default_language()):
        frappe.sendmail(
            recipients=[recipient.email],
            subject=_("[Open tickets] {0}").format(format_date(local_today)),
            message=digest_content(mine, others, now),
            email_headers={"X-Auto-Generated": "hd-agent-digest"},
        )


def claim_day(recipient, local_today):
    """Take today for this recipient, or report that somebody else has it.

    The row is read locked and written in the transaction that run() commits
    around the mail, so the mark and the queued mail land together or not at
    all: a rolled back failure, a retried job or a worker that dies mid-run
    leaves nothing half done. Reading the mark without the lock would not be
    enough on its own, because two overlapping runs of the job (a manual one
    beside the scheduled one) would both have read it empty and both send.
    """
    locked = frappe.db.sql(
        "SELECT fab_digest_sent_on FROM `tabHD Agent` WHERE name = %s FOR UPDATE",
        recipient.agent,
    )
    if locked and locked[0][0] and getdate(locked[0][0]) == local_today:
        return False

    # the date is the recipient's own, so the comparisons need no timezone
    # arithmetic to be read back
    frappe.db.set_value(
        "HD Agent",
        recipient.agent,
        "fab_digest_sent_on",
        local_today,
        update_modified=False,
    )
    return True


def digest_content(mine, others, now):
    """The two sections the digest is made of, each with its own count.

    Both headings stay when a section is empty, with a line in place of the
    table: a heading over nothing reads as a rendering fault.
    """
    return f"""\
<p>{_("Here are the tickets still open this morning.")}</p>
<p style="margin-top:20px;color:#8d95a0;">{_("Assigned to me")} ({len(mine)})</p>
{section(mine, now, _("Nothing is assigned to you right now."))}
<p style="margin-top:20px;color:#8d95a0;">{_("The others")} ({len(others)})</p>
{section(others, now, _("Nothing else is open right now."))}
"""


def section(tickets, now, empty_message):
    if not tickets:
        return f"""<p style="color:#8d95a0;">{empty_message}</p>"""
    return ticket_table(tickets, now)


def ticket_table(tickets, now):
    rows = "\n".join(ticket_row(ticket, now) for ticket in tickets)
    return f"""<table cellpadding="6" style="border-collapse:collapse;font-size:14px;">
  <tr>
    <td style="color:#8d95a0;">{_("Request")}</td>
    <td style="color:#8d95a0;">{_("Customer")}</td>
    <td style="color:#8d95a0;">{_("Priority")}</td>
    <td style="color:#8d95a0;">{_("Open for")}</td>
  </tr>
{rows}
</table>"""


def ticket_row(ticket, now):
    # Agent-facing link, so the ERP host and not the customer portal.
    url = get_url("/helpdesk/tickets/" + str(ticket.name))
    return f"""  <tr style="border-top:1px solid #e5e9ee;">
    <td><a href="{url}"><strong>#{ticket.name}</strong></a> - {escape_html(ticket.subject or "")}</td>
    <td>{escape_html(ticket.customer or "") or _("Not associated")}</td>
    <td>{escape_html(ticket.priority or "") or "-"}</td>
    <td>{age_label(ticket.creation, now)}</td>
  </tr>"""


def age_label(creation, now):
    """How long the ticket has been open, as an elapsed duration.

    A duration rather than a calendar difference, so the same ticket reads the
    same in every recipient's timezone. Counted in whole hours, which also
    catches a creation date in the future (clock skew, an imported ticket)
    rather than reporting it as most of a day.
    """
    hours = int((now - get_datetime(creation)).total_seconds() // 3600)
    if hours < 1:
        return _("less than an hour")
    if hours < 24:
        return _("1 hour") if hours == 1 else _("{0} hours").format(hours)

    days = hours // 24
    return _("1 day") if days == 1 else _("{0} days").format(days)
