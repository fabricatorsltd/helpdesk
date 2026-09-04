"""Remind the customer of a ticket waiting on them, then close it.

A ticket parked in the waiting-for-customer status after an agent reply gets one
reminder mail; if the customer still says nothing it is closed with a notice.
Both mails are filed on the thread as automated messages, so they never pass for
the agent reply the countdown is measured from, and they do not reopen or
otherwise touch the ticket (see HDTicket.on_communication_update).

The settings live on HD Settings as custom fields created by fab_helpdesk; on a
site without them the job is a no-op.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, escape_html, format_date, now_datetime

from helpdesk.helpdesk.doctype.hd_settings.helpers import (
    resolve_ticket_language,
    use_language,
)
from helpdesk.helpdesk.doctype.hd_ticket_activity.hd_ticket_activity import (
    log_ticket_activity,
)

REMINDER_ACTIVITY = "sent the customer an inactivity reminder"
CLOSE_ACTIVITY = "closed the ticket after {0} days without a reply from the customer"


def run():
    settings = frappe.db.get_value(
        "HD Settings",
        "HD Settings",
        [
            "fab_inactivity_enabled",
            "fab_inactivity_status",
            "fab_inactivity_reminder_days",
            "fab_inactivity_close_days",
        ],
        as_dict=True,
    )
    if not settings or not cint(settings.fab_inactivity_enabled):
        return

    status = settings.fab_inactivity_status
    reminder_days = cint(settings.fab_inactivity_reminder_days)
    close_days = cint(settings.fab_inactivity_close_days)
    if not status or reminder_days <= 0 or close_days <= reminder_days:
        return

    # Cutoffs in the system timezone, to match how communication_date is stored
    # (same reasoning as close_tickets_after_n_days).
    now = now_datetime()
    reminder_cutoff = add_to_date(now, days=-reminder_days)
    # The closure is counted from the reminder, not from the agent reply: a
    # ticket idle far longer than close_days (a backlog on first activation, a
    # scheduler outage) must still get the same grace period after being warned
    # as one that went quiet yesterday. On the normal path the two coincide.
    grace_days = close_days - reminder_days
    grace_cutoff = add_to_date(now, days=-grace_days)

    for candidate in get_candidates(status, reminder_cutoff, grace_cutoff):
        try:
            handle_ticket(candidate, status, grace_cutoff, grace_days, close_days)
        except Exception as e:
            # roll back first: an Error Log written inside the failed transaction
            # would be rolled back with it, and the failure would go unrecorded
            frappe.db.rollback()
            frappe.log_error(
                message=f"Failed to handle inactivity on ticket {candidate.name}. Error: {e}",
                title="Customer Inactivity Job Failed",
            )
            frappe.db.commit()  # nosemgrep
            continue

        frappe.db.commit()  # nosemgrep


def get_candidates(status, reminder_cutoff, grace_cutoff):
    """Tickets in the waiting status whose last real message is our own reply.

    Automated messages (these very mails) are left out of both aggregates, so
    sending a reminder does not push the closing deadline further away. A ticket
    whose newest message came from the customer is waiting on an agent, not on
    the customer, and never matches.

    Two shapes qualify: not warned yet and silent since the reminder threshold,
    or warned long enough ago to be closed.
    """
    return frappe.db.sql(
        """
            SELECT t.name, t.fab_inactivity_reminder_on, latest_comm.last_sent_date
            FROM `tabHD Ticket` t
            INNER JOIN (
                SELECT reference_name,
                    MAX(communication_date) as last_communication_date,
                    MAX(CASE WHEN sent_or_received = 'Sent' THEN communication_date END)
                        as last_sent_date
                FROM `tabCommunication`
                WHERE reference_doctype = 'HD Ticket'
                AND communication_type = 'Communication'
                GROUP BY reference_name
            ) latest_comm ON t.name = latest_comm.reference_name
            WHERE t.status = %(status)s
            AND t.is_merged = 0
            AND latest_comm.last_sent_date = latest_comm.last_communication_date
            AND (
                (
                    t.fab_inactivity_reminder_on IS NULL
                    AND latest_comm.last_sent_date < %(reminder_cutoff)s
                )
                OR t.fab_inactivity_reminder_on < %(grace_cutoff)s
            )
        """,
        {
            "status": status,
            "reminder_cutoff": reminder_cutoff,
            "grace_cutoff": grace_cutoff,
        },
        as_dict=True,
    )


def handle_ticket(candidate, status, grace_cutoff, grace_days, close_days):
    if not candidate.fab_inactivity_reminder_on:
        send_reminder(candidate, status, grace_days)
        return

    if candidate.fab_inactivity_reminder_on < grace_cutoff:
        close_ticket(candidate, status, close_days)


def still_waiting(ticket, candidate, status):
    """The candidates were listed before any mail went out, and each ticket is
    committed on its own. A customer reply landing meanwhile moves the ticket out
    of the waiting status and clears the mark, and must win over the job."""
    return (
        ticket.status == status
        and not ticket.is_merged
        and ticket.get("fab_inactivity_reminder_on")
        == candidate.fab_inactivity_reminder_on
    )


def send_reminder(candidate, status, grace_days):
    ticket = frappe.get_doc("HD Ticket", candidate.name)
    if not still_waiting(ticket, candidate, status):
        return

    close_on = add_to_date(now_datetime(), days=grace_days)

    # the ticket tag closes the subject, like agent replies, so a customer
    # answer without usable threading headers still lands on this ticket
    with use_language(resolve_ticket_language(ticket)):
        subject = _("[Reminder] {0} (#{1})").format(ticket.subject, ticket.name)
        message = reminder_content(ticket, close_on)

    if not send_customer_mail(ticket, subject, message):
        return

    ticket.db_set("fab_inactivity_reminder_on", now_datetime(), update_modified=False)
    log_ticket_activity(ticket.name, REMINDER_ACTIVITY)


def close_ticket(candidate, status, close_days):
    ticket = frappe.get_doc("HD Ticket", candidate.name)
    if not still_waiting(ticket, candidate, status):
        return

    ticket.status = "Closed"
    # as in close_tickets_after_n_days. It also skips before_save, and with it
    # the feedback mail: nothing to rate on a closure the customer never asked for
    ticket.flags.ignore_validate = True
    ticket.save(ignore_permissions=True)

    with use_language(resolve_ticket_language(ticket)):
        subject = _("[Closed] {0} (#{1})").format(ticket.subject, ticket.name)
        message = closure_content(ticket)

    send_customer_mail(ticket, subject, message)
    log_ticket_activity(ticket.name, CLOSE_ACTIVITY.format(close_days))
    # a reopened ticket parked in the waiting status again starts a fresh cycle
    ticket.db_set("fab_inactivity_reminder_on", None, update_modified=False)


def send_customer_mail(ticket, subject, message):
    """File the mail on the ticket thread and send it, the way an agent reply is
    sent, except that it is an automated message. Returns False when the site
    runs without the email workflow or the ticket has nobody to write to."""
    if ticket.skip_email_workflow():
        return False

    sender_email = ticket.sender_email()
    if not sender_email:
        frappe.throw(
            _("Unable to send email. Please setup default outgoing email account.")
        )

    recipients = ", ".join(ticket._strip_own_mailboxes(ticket.raised_by))
    if not recipients:
        return False
    cc = ticket._merge_reply_cc(None, recipients)

    last_communication = ticket.get_last_communication()
    communication = frappe.get_doc(
        {
            "cc": cc,
            "communication_medium": "Email",
            "communication_type": "Automated Message",
            "content": message,
            "doctype": "Communication",
            "email_account": sender_email.name,
            "email_status": "Open",
            "recipients": recipients,
            "reference_doctype": "HD Ticket",
            "reference_name": ticket.name,
            "sender": sender_email.email_id,
            "sent_or_received": "Sent",
            "status": "Linked",
            "subject": subject,
        }
    )
    if last_communication and last_communication.message_id:
        communication.in_reply_to = last_communication.name
    communication.insert(ignore_permissions=True)

    frappe.sendmail(
        cc=cc,
        communication=communication.name,
        expose_recipients="header",
        message=message,
        recipients=recipients,
        reference_doctype="HD Ticket",
        reference_name=ticket.name,
        reply_to=sender_email.email_id,
        sender=sender_email.email_id,
        subject=subject,
        with_container=False,
        in_reply_to=last_communication.name if last_communication else None,
        email_headers={"X-Auto-Generated": "hd-inactivity"},
    )
    return True


def reminder_content(ticket, close_on):
    return f"""\
<p style="color:#8d95a0;font-size:13px;margin-bottom:16px;">##- {_("Reply above this line")} -##</p>
<p>{_("We have not received a reply from you on request #{0} yet.").format(ticket.name)}</p>
<p style="background:#f3f5f8;padding:10px 14px;border-radius:4px;border:1px solid #e5e9ee;">
  <strong>{_("Request no.")} {ticket.name}</strong><br>
  {escape_html(ticket.subject)}
</p>
<p>{_("If we do not hear from you by {0}, we will close it.").format(format_date(close_on))}</p>
<p>{_("Reply to this email or open the request in the portal:")} <a href="{ticket.portal_uri}">{_("Open the ticket")}</a></p>
"""


def closure_content(ticket):
    return f"""\
<p style="color:#8d95a0;font-size:13px;margin-bottom:16px;">##- {_("Reply above this line")} -##</p>
<p>{_("We have closed request #{0} because we did not receive a reply.").format(ticket.name)}</p>
<p style="background:#f3f5f8;padding:10px 14px;border-radius:4px;border:1px solid #e5e9ee;">
  <strong>{_("Request no.")} {ticket.name}</strong><br>
  {escape_html(ticket.subject)}
</p>
<p>{_("If the problem persists, reply to this email and we will reopen it.")}</p>
<p><a href="{ticket.portal_uri}">{_("Open the ticket")}</a></p>
"""
