from contextlib import contextmanager
from email.utils import parseaddr

import frappe
from frappe.utils import get_datetime


def is_email_content_empty(content: str | None) -> bool:
    return content is None or content.strip() == ""


def get_default_language() -> str:
    return frappe.conf.get("helpdesk_default_language") or "it"


def resolve_ticket_language(doc) -> str:
    """Pick the email language for a ticket from its sender.

    - existing user account for the sender email -> that user's language
    - else sender linked to a customer -> the customer's default language
    - else -> the helpdesk default language
    """
    from helpdesk.utils import get_customers

    email_id = parseaddr(doc.get("raised_by") or "")[1].lower()

    if email_id:
        user_lang = frappe.db.get_value("User", email_id, "language")
        if user_lang:
            return user_lang

    customer = doc.get("customer")
    if not customer and doc.get("contact"):
        customers = get_customers(contact=doc.get("contact"))
        customer = customers[0] if customers else None

    if customer:
        lang = frappe.db.get_value("HD Customer", customer, "default_language")
        if not lang:
            erpnext_customer = frappe.db.get_value(
                "HD Customer", customer, "erpnext_customer"
            )
            if erpnext_customer:
                lang = frappe.db.get_value("Customer", erpnext_customer, "language")
        if lang:
            return lang

    return get_default_language()


@contextmanager
def use_language(lang: str | None):
    """Temporarily switch the translation language for outbound content."""
    previous = getattr(frappe.local, "lang", None)
    if lang:
        frappe.local.lang = lang
    try:
        yield
    finally:
        frappe.local.lang = previous


def get_default_email_content(type: str) -> str:
    if type == "share_feedback":
        return """\
<p>Hello,</p>
<p>Thanks for reaching out to us. We’d love your feedback on your recent support experience with ticket #{{ doc.name }}.</p>
<a href="{{ url }}" class="btn btn-primary">Share Feedback</a>

<p>Thank you!<br>Support Team</p>"""

    if type == "acknowledgement":
        return """\
<p style="color:#8d95a0;font-size:13px;margin-bottom:16px;">##- Rispondi sopra questa riga | Reply above this line -##</p>
<p>{{ _("Thank you for contacting us. We have received your request and opened a support ticket. Our team will get back to you shortly.") }}</p>
<p style="background:#f3f5f8;padding:10px 14px;border-radius:4px;border:1px solid #e5e9ee;">
  <strong>{{ _("Request no.") }} {{ doc.name }}</strong>
</p>
<p>{{ _("You can add a comment to your request by replying to this email. To view or update it,") }} <a href="{{ ticket_url }}">{{ _("click here") }}</a>.</p>
<p style="color:#8d95a0;">{{ _("If you did not submit this request, you can safely ignore this message.") }}</p>
"""

    if type == "reply_to_agents":
        return """\
<div>
  <p>Hello,</p>
  <p>You have a new reply on the ticket <strong>#{{ doc.name }}</strong>.</p>
  <p><strong>Subject:</strong> {{ doc.subject }}</p>
  <p><strong>Raised By:</strong> {{ doc.raised_by }}</p>
  <p><strong>Priority:</strong> {{ doc.priority }}</p>
   <div style="margin-bottom: 10px">
    <p style="margin-bottom: 20px">Message</p>
    <div
      style="
        background: #f3f5f8;
        padding: 10px;
        border-radius: 4px;
        border: 1px solid #e5e9ee;
      "
    >
      {{ message }}
    </div>
  </div>
  <br />
  <p>
    You can view and respond to this ticket by
    <a href="{{ ticket_url }}">clicking here</a>.
  </p>
  <p>Regards,<br />Support Team</p>
</div>
"""

    if type == "reply_via_agent":
        return """\
<div>
  <h2><strong>Ticket #{{ doc.name }}</strong></h2>
  <h3>You have a new reply on this ticket</h3>
  <br />
  <div style="margin-bottom: 10px">
    <h3 style="margin-bottom: 20px">Message</h3>
    <div
      style="
        background: #f3f5f8;
        padding: 10px;
        border-radius: 4px;
        border: 1px solid #e5e9ee;
      "
    >
      {{ message }}
    </div>
  </div>
  <p>Please visit the customer portal to reply to this message</p>
  <a
    class="btn btn-primary"
    href="{{ ticket_url }}"
    rel="noopener noreferrer"
    target="_blank"
  >View in Portal</a>
  <br />
</div>
"""


default_banner_msg = """Thanks for reaching out 👋. This ticket was created outside our working hours. You can expect the next response by {{ next_working_day }}."""


@frappe.whitelist()
def get_banner_msg():
    """Get current and default banner message for settings UI"""

    current_msg = frappe.db.get_single_value(
        "HD Settings", "outside_working_hours_message"
    )
    enabled = frappe.db.get_single_value("HD Settings", "enable_outside_hours_banner")

    return {
        "default": default_banner_msg,
        "current": current_msg or None,
        "enabled": bool(enabled),
    }


def get_rendered_banner_msg(ticket_id):
    banner_msg = frappe.db.get_single_value(
        "HD Settings", "outside_working_hours_message"
    )
    ticket = frappe.get_doc("HD Ticket", ticket_id).as_dict()
    if not banner_msg:
        banner_msg = default_banner_msg

    next_working_day = None
    next_working_date = None
    expected_response = None

    if ticket.get("response_by"):
        next_working_day_dt = get_datetime(ticket.get("response_by"))
        next_working_day = next_working_day_dt.strftime("%A, %d %b")
        next_working_date = next_working_day_dt.strftime("%d %b")
        expected_response = next_working_day_dt.strftime("%H:%M, %A, %d %b")

    context = {
        "ticket": ticket,
        "next_working_daytime": next_working_day_dt,
        "next_working_day": next_working_day,
        "next_working_date": next_working_date,
        "expected_response": expected_response,
    }

    rendered = frappe.render_template(banner_msg, context)

    return {
        "banner_msg": rendered,
    }
