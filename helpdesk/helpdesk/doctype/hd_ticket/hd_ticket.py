import json
import uuid
from datetime import timedelta
from email.utils import getaddresses, parseaddr

import frappe
from bs4 import BeautifulSoup, Comment
from frappe import _
from frappe.core.page.permission_manager.permission_manager import remove
from frappe.desk.form.assign_to import add as assign
from frappe.desk.form.assign_to import clear as clear_all_assignments
from frappe.desk.form.assign_to import get as get_assignees
from frappe.model.document import Document
from frappe.permissions import add_permission, update_permission_property
from frappe.query_builder import DocType, Order
from frappe.utils import add_to_date, cint, getdate, now_datetime
from pypika.functions import Count
from pypika.queries import Query
from pypika.terms import Criterion

from helpdesk.helpdesk.doctype.hd_settings.helpers import (
    get_default_email_content,
    get_default_language,
    is_email_content_empty,
    resolve_ticket_language,
    use_language,
)
from helpdesk.helpdesk.doctype.hd_ticket_activity.hd_ticket_activity import (
    log_ticket_activity,
)
from helpdesk.helpdesk.utils.email import (
    default_outgoing_email_account,
    default_ticket_outgoing_email_account,
)
from helpdesk.utils import (
    capture_event,
    get_agents_team,
    get_customers,
    get_doc_room,
    get_helpdesk_url,
    is_admin,
    is_agent,
    publish_event,
)

from ..hd_notification.utils import clear as clear_notifications
from ..hd_service_level_agreement.utils import get_sla


class HDTicket(Document):
    @property
    def default_open_status(self):
        return frappe.db.get_value(
            "HD Service Level Agreement",
            self.sla,
            "default_ticket_status",
        ) or frappe.db.get_single_value("HD Settings", "default_ticket_status")

    @property
    def ticket_reopen_status(self):
        return frappe.db.get_value(
            "HD Service Level Agreement",
            self.sla,
            "ticket_reopen_status",
        ) or frappe.db.get_single_value("HD Settings", "ticket_reopen_status")

    def publish_update(self):
        room = get_doc_room("HD Ticket", self.name)
        publish_event(
            "helpdesk:ticket-update", room=room, data={"ticket_id": self.name}
        )

    def autoname(self):
        return self.name

    def before_insert(self):
        self.generate_key()

    def before_validate(self):
        self.check_update_perms()
        self.set_ticket_type()
        self.set_raised_by()
        self.set_priority()
        self.set_first_responded_on()
        self.set_feedback_values()
        self.set_default_status()
        self.set_status_category()
        self.set_sla()

        self.validate_portal_contact()
        self.set_contact()
        self.set_customer()

    def validate(self):
        self.validate_feedback()

    def before_save(self):
        self.apply_sla()
        if not self.is_new():
            self.handle_ticket_activity_update()

        self.handle_email_feedback()
        if self.is_new():
            self.raised_outside_working_hours = (
                self.is_currently_outside_working_hours()
            )

    def _get_rendered_template(
        self, content: str, default_content: str, args: dict[str, str] | None = None
    ):
        if args is None:
            args = dict()
        template_args = {
            "doc": self.as_dict(),
        }
        for key, value in args.items():
            template_args[key] = value
        return frappe.render_template(
            default_content if is_email_content_empty(content) else content,
            template_args,
        )

    def handle_email_feedback(self):
        if (
            self.is_new()
            or self.via_customer_portal
            or self.feedback_rating
            or not self.has_value_changed("status")
            or not self.key
        ):
            return

        [is_email_feedback_enabled, email_feedback_status] = frappe.get_cached_value(
            "HD Settings",
            "HD Settings",
            ["enable_email_ticket_feedback", "send_email_feedback_on_status"],
        )

        send_feedback_email = int(is_email_feedback_enabled) and (
            email_feedback_status == self.status
            or email_feedback_status == ""
            and self.status == "Closed"
        )

        if not send_feedback_email:
            return

        last_communication = self.get_last_communication()

        url = get_helpdesk_url(f"/ticket-feedback/new?key={self.key}")
        feedback_email_content = frappe.db.get_single_value(
            "HD Settings", "feedback_email_content"
        )
        default_feedback_email_content = get_default_email_content("share_feedback")
        try:
            with use_language(resolve_ticket_language(self)):
                frappe.sendmail(
                    recipients=[self.raised_by],
                    subject=f"Re: {self.subject}",
                    message=self._get_rendered_template(
                        feedback_email_content,
                        default_feedback_email_content,
                        {"url": url},
                    ),
                    reference_doctype="HD Ticket",
                    reference_name=self.name,
                    now=True,
                    in_reply_to=last_communication.name if last_communication else None,
                    email_headers={"X-Auto-Generated": "hd-email-feedback"},
                )
            frappe.msgprint(_("Feedback email has been sent to the customer"))
        except Exception as e:
            frappe.throw(_("Could not send feedback email,due to: {0}").format(e))

    def after_insert(self):

        # Telemetry Event
        self.capture_ticket_created_telemetry_events()
        publish_event("helpdesk:new-ticket")

        if self.get("description"):
            self.create_communication_via_contact(self.description, new_ticket=True)
            self.handle_inline_media_new_ticket()

        send_ack_email = frappe.db.get_single_value(
            "HD Settings", "send_acknowledgement_email"
        )
        # Acknowledge every new ticket to the requester, portal-created included,
        # so they always get the ticket number and reply-by-email link.
        if not frappe.flags.initial_sync and send_ack_email:
            self.send_acknowledgement_email()

        # Notify agents of every new ticket, including portal-created ones. Upstream
        # only notified on email tickets, so tickets opened from the customer portal
        # went unseen until someone happened to look at the queue.
        if not frappe.flags.initial_sync:
            self.notify_agents_new_ticket()

    def capture_ticket_created_telemetry_events(self):
        if self.subject == "Welcome to Helpdesk":
            return

        capture_event("ticket_created")
        if not self.via_customer_portal:
            capture_event("ticket_created_via_email")
        if self.via_customer_portal and not is_agent():
            capture_event("ticket_created_via_customer")

        if self.ticket_split_from:
            log_ticket_activity(
                self.name,
                "split the ticket from #{0}".format(self.ticket_split_from),
            )
            capture_event("ticket_split")

    def on_update(self):
        # flake8: noqa
        if self.status_category == "Open":
            if (
                self.get_doc_before_save()
                and self.get_doc_before_save().status_category != "Open"
            ):
                agents = self.get_assigned_agents()
                if agents:
                    for agent in agents:
                        if agent.name == frappe.session.user:
                            continue
                        self.notify_agent(agent.name, "Reaction")

        self.remove_assignment_if_not_in_team()
        self.publish_update()
        self.capture_update_telemetry_events()

    def notify_agent(self, agent, notification_type="Assignment"):
        frappe.get_doc(
            frappe._dict(
                doctype="HD Notification",
                user_from=frappe.session.user,
                reference_ticket=self.name,
                user_to=agent,
                notification_type=notification_type,
            )
        ).insert(ignore_permissions=True)

    def capture_update_telemetry_events(self):
        capture_event("ticket_updated")

        if self.has_value_changed("status"):
            capture_event("ticket_status_updated")
        if (
            self.has_value_changed("status_category")
            and self.status_category == "Resolved"
        ):
            capture_event("ticket_resolved")

    def set_ticket_type(self):
        if self.ticket_type:
            return
        self.ticket_type = (
            frappe.db.get_single_value("HD Settings", "default_ticket_type") or ""
        )

    def set_raised_by(self):
        if self.raised_by:
            return
        self.raised_by = frappe.session.user

    def validate_portal_contact(self) -> None:
        """Block non-agent users from attributing a ticket to another contact.

        Agents are unrestricted, and so are system channels like email intake,
        which run as Administrator.
        """
        if is_agent():
            return
        if not self.contact:
            return
        if not self.is_new() and not self.has_value_changed("contact"):
            return

        if self.contact != self.get_session_contact():
            frappe.throw(
                _("You can only raise tickets for your own contact."),
                frappe.PermissionError,
            )

    def get_session_contact(self) -> str | None:
        """Resolve the Contact owned by the current session user.

        Match strictly on the ``user`` link. Fall back to the email only when
        it is the session user's own verified email and the Contact is not
        already linked to a different user, so an unrelated record that merely
        shares the email can never satisfy the ownership check.
        """
        contact = frappe.db.get_value("Contact", {"user": frappe.session.user})
        if contact:
            return contact

        user_email = frappe.db.get_value("User", frappe.session.user, "email")
        if not user_email:
            return None
        return frappe.db.get_value(
            "Contact", {"email_id": user_email, "user": ("in", ("", None))}
        )

    def set_contact(self):
        email_id = parseaddr(self.raised_by)[1]
        # flake8: noqa
        if email_id:
            if not self.contact:
                contact = frappe.db.get_value("Contact", {"email_id": email_id})
                if contact:
                    self.contact = contact

    def set_customer(self):
        if not frappe.db.get_single_value(
            "HD Settings", "auto_set_customer_from_contact"
        ):
            return

        # For existing tickets, only validate if customer value has changed
        if not self.is_new() and not self.has_value_changed("customer"):
            return

        contact_customers = get_customers(contact=self.contact) if self.contact else []

        if self.customer:
            if self.customer not in contact_customers and not is_agent():
                frappe.throw(
                    _(
                        "The selected customer {0} is not linked to the contact {1}."
                        "Please select a valid customer or update the contact's linked customers."
                    ).format(self.customer, self.contact),
                    frappe.ValidationError,
                )
            return

        # Auto-set customer only for new tickets
        if self.is_new() and self.contact:
            if len(contact_customers) == 1:
                self.customer = contact_customers[0]
            elif (
                len(contact_customers) > 1
                and not is_agent()
                and self.via_customer_portal
            ):
                frappe.throw(
                    _(
                        "The contact {0} is linked to multiple customers. Please select the customer manually."
                    ).format(self.contact),
                    frappe.ValidationError,
                )

    def set_priority(self):
        if self.priority:
            return
        self.priority = frappe.get_cached_value(
            "HD Ticket Type", self.ticket_type, "priority"
        ) or frappe.get_cached_value("HD Settings", "HD Settings", "default_priority")

    def set_first_responded_on(self):
        if self.is_new():
            return
        if self.first_responded_on:
            return

        old_status_category = (
            self.get_doc_before_save().status_category
            if self.get_doc_before_save()
            else None
        )
        is_closed_or_resolved = (
            old_status_category == "Open" and self.status_category == "Resolved"
        )

        if self.status_category == "Paused" or is_closed_or_resolved:
            self.first_responded_on = frappe.utils.now_datetime()

    def set_feedback_values(self):
        if not self.feedback:
            return
        feedback_option = frappe.get_doc("HD Ticket Feedback Option", self.feedback)
        self.feedback_rating = feedback_option.rating

    @property
    def has_agent_replied(self):
        return frappe.db.exists(
            "Communication",
            {
                "reference_doctype": "HD Ticket",
                "reference_name": self.name,
                "sent_or_received": "Sent",
            },
        )

    def validate_feedback(self):
        is_feedback_mandatory = frappe.get_cached_value(
            "HD Settings", "HD Settings", "is_feedback_mandatory"
        )
        if (
            self.feedback_rating
            or self.status_category != "Resolved"
            or is_agent()
            or not self.has_agent_replied
            or not is_feedback_mandatory
        ):
            return

        frappe.throw(
            _("Ticket must be resolved with a feedback"), frappe.ValidationError
        )

    def check_update_perms(self):
        if self.is_new() or is_agent() or not self.via_customer_portal:
            return
        old_doc = self.get_doc_before_save()
        is_closed = old_doc.status == "Closed"
        is_rated = bool(old_doc.feedback)
        if is_closed or is_rated:
            text = _("Closed or rated tickets cannot be updated by non-agents")
            frappe.throw(text, frappe.PermissionError)

    def handle_ticket_activity_update(self):
        """
        Handles the ticket activity update.
        Should be called inside on_update
        """
        field_maps = {
            "status": "status",
            "priority": "priority",
            "agent_group": "team",
            "ticket_type": "type",
            "contact": "contact",
            "sla": "SLA",
        }
        for field in [
            "status",
            "priority",
            "agent_group",
            "contact",
            "ticket_type",
            "sla",
        ]:
            if self.has_value_changed(field):
                value = self.as_dict()[field]
                if not value:
                    msg = f"cleared {field_maps[field]}"
                else:
                    msg = f"set {field_maps[field]} to {value}"

                log_ticket_activity(self.name, msg)

    def generate_key(self):
        self.key = uuid.uuid4()

    def remove_assignment_if_not_in_team(self):
        """
        Removes the assignment if the agent is not in the team.
        Should be called inside on_update
        """
        if self.is_new():
            return
        if not self.agent_group or (hasattr(self, "_assign") and not self._assign):
            return
        if self.has_value_changed("agent_group") and self.status_category == "Open":
            current_assigned_agent = self.get_assigned_agent()
            if not current_assigned_agent:
                return
            is_agent_in_assigned_team = self.agent_in_assigned_team(
                current_assigned_agent, self.agent_group
            )

            if (
                not is_agent_in_assigned_team
            ) and self.users_present_in_team_assignment_rule():
                clear_all_assignments("HD Ticket", self.name)

    def agent_in_assigned_team(self, agent, team):
        return frappe.db.exists(
            "HD Team Member",
            {
                "parent": team,
                "user": agent,
            },
        )

    def users_present_in_team_assignment_rule(self):
        if not self.agent_group:
            return False

        assignment_rule = frappe.db.get_value(
            "HD Team", self.agent_group, "assignment_rule"
        )
        if not assignment_rule:
            return False

        is_disabled = frappe.db.get_value(
            "Assignment Rule", assignment_rule, "disabled"
        )
        if is_disabled:
            return False

        users = frappe.get_all(
            "Assignment Rule User", filters={"parent": assignment_rule}
        )
        if not users:
            return False

        return True

    @frappe.whitelist()
    def assign_agent(self, agent: str):
        assign({"assign_to": [agent], "doctype": "HD Ticket", "name": self.name})

        if frappe.session.user != agent:
            self.notify_agent(agent, "Assignment")

    def get_assigned_agents(self):
        assignees = get_assignees({"doctype": "HD Ticket", "name": self.name})
        if len(assignees) > 0:
            names = [assignee.owner for assignee in assignees]
            return frappe.get_all("HD Agent", filters={"name": ["in", names]})

    def get_assigned_agent(self):
        # TODO: deprecate this
        # for some reason _assign is not set, maybe a framework bug?
        if hasattr(self, "_assign") and self._assign:
            assignees = json.loads(self._assign)
            if len(assignees) > 0:
                # TODO: temporary fix, remove this when only agents can be assigned to ticket
                exists = frappe.db.exists("HD Agent", assignees[0])
                if exists:
                    return assignees[0]

        assignees = get_assignees({"doctype": "HD Ticket", "name": self.name})
        if len(assignees) > 0:
            # TODO: temporary fix, remove this when only agents can be assigned to ticket
            return frappe.db.exists("HD Agent", assignees[0].owner)

        return None

    def on_trash(self):
        activities = frappe.db.get_all("HD Ticket Activity", {"ticket": self.name})
        for activity in activities:
            frappe.db.delete("HD Ticket Activity", activity)

        comments = frappe.db.get_all(
            "HD Ticket Comment", {"reference_ticket": self.name}
        )
        for comment in comments:
            frappe.db.delete("HD Ticket Comment", comment)

    def skip_email_workflow(self):
        skip: str = frappe.get_value("HD Settings", None, "skip_email_workflow") or "0"

        return bool(int(skip))

    def _resolve_sender_email(self, email_account_name, from_email_id):
        if not email_account_name:
            sender_email = self.sender_email()
            return sender_email, (sender_email.name if sender_email else None)

        if not frappe.db.exists("Email Account", email_account_name):
            frappe.throw(_("No Email Account found for {0}").format(from_email_id))

        sender_email = frappe._dict(name=email_account_name, email_id=from_email_id)
        return sender_email, email_account_name

    def instantly_send_email(self):
        check: str = (
            frappe.get_value("HD Settings", None, "instantly_send_email") or "0"
        )

        return bool(int(check))

    @frappe.whitelist()
    def get_last_communication(self):
        filters = {
            "reference_doctype": "HD Ticket",
            "reference_name": ["=", str(self.name)],
        }

        try:
            communication = frappe.get_last_doc(
                "Communication",
                filters=filters,
            )

            return communication
        except Exception:
            return None

    def last_communication_email(self):
        if not (communication := self.get_last_communication()):
            return

        if not communication.email_account:
            return

        email_account = frappe.get_doc("Email Account", communication.email_account)

        if not email_account.enable_outgoing:
            return

        return email_account

    def sender_email(self):
        """
        Find an email to use as sender. Fall back through multiple choices

        :return: `Email Account`
        """
        if email_account := self.last_communication_email():
            return email_account

        if email_account := default_ticket_outgoing_email_account():
            return email_account

        if email_account := default_outgoing_email_account():
            return email_account

    @property
    def portal_uri(self):
        return get_helpdesk_url(f"/helpdesk/my-tickets/{self.name}")

    @frappe.whitelist()
    def new_comment(self, content: str, attachments: list[str] = []):
        if not is_agent():
            frappe.throw(
                _("You are not permitted to add a comment"), frappe.PermissionError
            )
        c = frappe.new_doc("HD Ticket Comment")
        c.commented_by = frappe.session.user
        c.content = content
        c.is_pinned = False
        c.reference_ticket = self.name
        c.save()
        for attachment in attachments:
            self.attach_file_with_doc(
                "HD Ticket Comment", c.name, attachment.get("file_url")
            )

    def _merge_reply_cc(self, cc, recipients):
        """Union of the ticket's stored CC participants and any CC the agent
        typed, minus the primary recipients. Returns a comma-joined string or None."""
        to_addrs = {addr.lower() for _, addr in getaddresses([recipients or ""]) if addr}
        out = []
        for raw in (self.get("fab_cc"), cc):
            for _, addr in getaddresses([raw or ""]):
                addr = addr.lower()
                if addr and "@" in addr and addr not in to_addrs and addr not in out:
                    out.append(addr)
        return ", ".join(out) or None

    def _cc_list(self):
        return [addr.lower() for _, addr in getaddresses([self.get("fab_cc") or ""]) if addr]

    def _own_mailboxes(self):
        """Lowercased addresses of our own Email Accounts. Replies must never go
        to one of these: the message would loop back into the inbox."""
        return {a.lower() for a in frappe.get_all("Email Account", pluck="email_id") if a}

    def _strip_own_mailboxes(self, raw):
        """Parse an address string and drop any of our own mailboxes, keeping the
        original (display-name) form of the rest."""
        own = self._own_mailboxes()
        return [
            (f"{name} <{addr}>" if name else addr)
            for name, addr in getaddresses([raw or ""])
            if addr and "@" in addr and addr.lower() not in own
        ]

    @frappe.whitelist()
    def add_cc(self, email):
        if not is_agent():
            frappe.throw(
                _("You are not permitted to manage CC"), frappe.PermissionError
            )
        addr = (parseaddr(email or "")[1] or "").strip().lower()
        if not addr or "@" not in addr:
            frappe.throw(_("Invalid email address"))
        current = self._cc_list()
        if addr not in current:
            current.append(addr)
            self.db_set("fab_cc", ", ".join(current), update_modified=False)
        return self.get("fab_cc")

    @frappe.whitelist()
    def remove_cc(self, email):
        if not is_agent():
            frappe.throw(
                _("You are not permitted to manage CC"), frappe.PermissionError
            )
        addr = (parseaddr(email or "")[1] or "").strip().lower()
        current = [a for a in self._cc_list() if a != addr]
        self.db_set("fab_cc", ", ".join(current), update_modified=False)
        return self.get("fab_cc")

    @frappe.whitelist()
    def reply_via_agent(
        self,
        message: str,
        from_email: dict | None = None,
        to: str | None = None,
        cc: str | None = None,
        bcc: str | None = None,
        attachments: list[str] = [],
    ):
        if not is_agent():
            frappe.throw(
                _("You are not permitted to reply as an agent"), frappe.PermissionError
            )
        skip_email_workflow = self.skip_email_workflow()
        medium = "" if skip_email_workflow else "Email"
        # keep the ticket id in the subject as a stable tracking tag so replies
        # stay tied to the ticket even if a client's mailer drops In-Reply-To.
        # Round brackets so the framework's get_reference_name_from_subject
        # (subject.rsplit("#")[-1].strip(" ()")) parses the id on the fallback.
        subject = f"Re: {self.subject} (#{self.name})"
        from_email_id = from_email.get("email_id") if from_email else None
        email_account_name = from_email.get("email_account") if from_email else None
        sender = from_email_id or frappe.session.user
        recipients = to

        sender_email = None
        if not skip_email_workflow:
            sender_email, email_account_name = self._resolve_sender_email(
                email_account_name, from_email_id
            )

        if recipients == "Administrator":
            recipients = frappe.get_value("User", "Administrator", "email")

        # Never send the reply to one of our own mailboxes. Replying from the
        # thread to an email whose sender is the support address (an ack or a
        # previous agent reply) would otherwise target the inbox itself, loop
        # the message back in and spawn a duplicate ticket. Drop our addresses
        # and fall back to the requester when nothing valid is left.
        to_list = self._strip_own_mailboxes(recipients)
        if not to_list:
            to_list = self._strip_own_mailboxes(self.raised_by)
        recipients = ", ".join(to_list)

        # loop in the ticket's CC participants (captured from inbound mail),
        # merged with anything the agent typed, minus the primary recipients
        cc = self._merge_reply_cc(cc, recipients)

        communication = frappe.get_doc(
            {
                "bcc": bcc,
                "cc": cc,
                "communication_medium": medium,
                "communication_type": "Communication",
                "content": message,
                "doctype": "Communication",
                "email_account": email_account_name,
                "email_status": "Open",
                "recipients": recipients,
                "reference_doctype": "HD Ticket",
                "reference_name": self.name,
                "sender": sender,
                "sent_or_received": "Sent",
                "status": "Linked",
                "subject": subject,
            }
        )

        last_communication = self.get_last_communication()
        if last_communication and last_communication.message_id:
            communication.in_reply_to = last_communication.name

        communication.insert(ignore_permissions=True)
        capture_event("agent_replied")

        _attachments = []

        for attachment in attachments:
            file_url = frappe.db.get_value("File", attachment, "file_url")
            self.attach_file_with_doc("Communication", communication.name, file_url)
            self.attach_file_with_doc("HD Ticket", self.name, file_url)
            _attachments.append({"file_url": file_url})

        if skip_email_workflow or not frappe.db.get_single_value(
            "HD Settings", "enable_reply_email_via_agent"
        ):
            return

        if not sender_email:
            frappe.throw(
                _("Unable to send email. Please setup default outgoing email account.")
            )

        message = self.parse_content(message)

        reply_to_email = sender_email.email_id
        email_content = frappe.db.get_single_value(
            "HD Settings", "reply_via_agent_email_content"
        )
        default_email_content = get_default_email_content("reply_via_agent")
        try:
            with use_language(resolve_ticket_language(self)):
                rendered_template = self._get_rendered_template(
                    email_content,
                    default_email_content,
                    {"message": message, "ticket_url": self.portal_uri},
                )
        except Exception as e:
            frappe.throw(_("Could not send an email due to: {0}").format(e))

        send_delayed = True
        send_now = False

        if self.instantly_send_email():
            send_delayed = False
            send_now = True

        try:
            frappe.sendmail(
                attachments=_attachments,
                bcc=bcc,
                cc=cc,
                communication=communication.name,
                delayed=send_delayed,
                expose_recipients="header",
                message=rendered_template,
                now=send_now,
                recipients=recipients,
                reference_doctype="HD Ticket",
                reference_name=self.name,
                reply_to=reply_to_email,
                sender=reply_to_email,
                subject=subject,
                with_container=False,
                in_reply_to=last_communication.name if last_communication else None,
            )
        except Exception as e:
            frappe.throw(str(e))

    @frappe.whitelist()
    # flake8: noqa
    def create_communication_via_contact(
        self, message: str, attachments: list[dict] = [], new_ticket: bool = False
    ):
        # Agents are notified centrally from on_communication_update when the
        # Received communication below is inserted, so portal and email replies
        # take the same path (see notify_agents_new_reply).

        # if self.status_category == "Paused" and not new_ticket:
        if not new_ticket:
            self.status = self.ticket_reopen_status
            self.save(ignore_permissions=True)

        c = frappe.new_doc("Communication")
        c.communication_type = "Communication"
        c.communication_medium = "Email"
        c.sent_or_received = "Received"
        c.email_status = "Open"
        c.subject = f"Re: {self.subject}"
        c.sender = frappe.session.user
        c.content = message
        c.status = "Linked"
        c.reference_doctype = "HD Ticket"
        c.reference_name = self.name
        c.ignore_permissions = True
        c.ignore_mandatory = True
        c.save(ignore_permissions=True)

        _attachments = self.get("attachments") or attachments or []
        if not len(_attachments):
            return
        QBFile = frappe.qb.DocType("File")
        condition_name = [QBFile.name == i["name"] for i in _attachments]
        frappe.qb.update(QBFile).set(QBFile.attached_to_name, c.name).set(
            QBFile.attached_to_doctype, "Communication"
        ).where(Criterion.any(condition_name)).run()

        # attach files to ticket
        file_urls = frappe.get_all(
            "File", filters={"attached_to_name": c.name}, pluck="file_url"
        )
        for url in file_urls:
            self.attach_file_with_doc("HD Ticket", self.name, url)

    def handle_inline_media_new_ticket(self):
        soup = BeautifulSoup(self.description, "html.parser")
        files = []  # List of file URLs
        for tag in soup.find_all(["img", "video"]):
            if tag.has_attr("src"):
                src = tag["src"]
                files.append(src)
        for f in files:
            file = frappe.db.exists(
                "File",
                {
                    "file_url": f,
                    "attached_to_doctype": ["is", "Not Set"],
                    "owner": frappe.session.user,
                },
            )
            if file:
                doc = frappe.get_doc("File", file)
                doc.attached_to_doctype = "HD Ticket"
                doc.attached_to_name = self.name
                doc.save()

    def _internal_reply_addresses(self):
        """Our own mailboxes and active agents. A reply from any of these is not
        a customer reply and must not trigger the agent notification."""
        addrs = {a.lower() for a in frappe.get_all("Email Account", pluck="email_id") if a}
        addrs |= {
            a.lower() for a in frappe.get_all("HD Agent", pluck="name") if a and "@" in a
        }
        return addrs

    def notify_agents_new_reply(self, c):
        """Email agents when someone who is not an agent replies to the ticket.

        Called from on_communication_update, so it covers every reply channel:
        inbound email and portal both create a Communication and land here. The
        opening message is left to notify_agents_new_ticket; agent replies and
        our own auto-mail are filtered out by sender. Guarded to fire once, on
        the communication's insert, so later edits don't re-notify."""
        if not getattr(c.flags, "in_insert", False):
            return
        if not frappe.db.get_single_value("HD Settings", "enable_reply_email_to_agent"):
            return
        if (c.communication_type or "") != "Communication":
            return
        sender = (c.sender or "").lower()
        if not sender or sender in self._internal_reply_addresses():
            return
        # The opener is announced by notify_agents_new_ticket; notify only replies.
        if (
            frappe.db.count(
                "Communication",
                {"reference_doctype": "HD Ticket", "reference_name": self.name},
            )
            <= 1
        ):
            return

        active_agents = frappe.get_all("HD Agent", filters={"is_active": 1}, pluck="name")
        assigned = {a.get("name") for a in (self.get_assigned_agents() or [])}
        # Assigned agents handle the ticket; fall back to the whole active team so
        # a reply on an unassigned ticket never goes unseen.
        recipients = [a for a in active_agents if a in assigned] or active_agents
        recipients = [r for r in recipients if r and "@" in r and r.lower() != sender]
        if not recipients:
            return

        contact_name = None
        if self.contact:
            names = frappe.db.get_value(
                "Contact", self.contact, ["first_name", "last_name"]
            )
            if names:
                contact_name = " ".join(p for p in names if p) or None

        email_content = frappe.db.get_single_value(
            "HD Settings", "reply_email_to_agent_content"
        )
        default_content = get_default_email_content("reply_to_agents")
        try:
            with use_language(get_default_language()):
                frappe.sendmail(
                    recipients=recipients,
                    subject=_("[New reply] #{0}: {1}").format(self.name, self.subject),
                    message=self._get_rendered_template(
                        email_content,
                        default_content,
                        {
                            "raised_by": self.raised_by,
                            "contact_name": contact_name,
                            "customer": self.customer,
                            "message": c.content,
                            "ticket_url": frappe.utils.get_url(
                                "/helpdesk/tickets/" + str(self.name)
                            ),
                        },
                    ),
                    reference_doctype="HD Ticket",
                    reference_name=self.name,
                    now=True,
                    email_headers={"X-Auto-Generated": "hd-new-reply-agents"},
                )
        except Exception:
            self.log_error("Could not notify agents of the new reply")

    def send_acknowledgement_email(self):
        acknowledgement_email_content = frappe.db.get_single_value(
            "HD Settings", "acknowledgement_email_content"
        )
        default_acknowledgement_email_content = get_default_email_content(
            "acknowledgement"
        )

        try:
            with use_language(resolve_ticket_language(self)):
                frappe.sendmail(
                    recipients=[self.raised_by],
                    subject=_("[Request received] {0}").format(self.subject),
                    message=self._get_rendered_template(
                        acknowledgement_email_content,
                        default_acknowledgement_email_content,
                        {"ticket_url": self.portal_uri},
                    ),
                    reference_doctype="HD Ticket",
                    reference_name=self.name,
                    now=True,
                    expose_recipients="header",
                    email_headers={"X-Auto-Generated": "hd-acknowledgement"},
                )
        except Exception as e:
            frappe.throw(
                _("Could not send an acknowledgement email due to: {0}").format(e)
            )

    def _agent_email_context(self):
        """Template args shared by the agent-facing ticket emails."""
        contact_name = None
        if self.contact:
            names = frappe.db.get_value(
                "Contact", self.contact, ["first_name", "last_name"]
            )
            if names:
                contact_name = " ".join(p for p in names if p) or None

        contract = None
        if self.customer:
            erpnext_customer = frappe.db.get_value(
                "HD Customer", self.customer, "erpnext_customer"
            )
            if erpnext_customer:
                contract = frappe.db.get_value(
                    "Contract",
                    {"party_type": "Customer", "party_name": erpnext_customer},
                    "name",
                )

        return {
            "raised_by": self.raised_by,
            "contact_name": contact_name,
            "customer": self.customer,
            "contract": contract,
            "message": self.description,
            "response_by": frappe.utils.format_datetime(self.response_by)
            if self.response_by
            else None,
            "resolution_by": frappe.utils.format_datetime(self.resolution_by)
            if self.resolution_by
            else None,
            # Agent-facing link, so the ERP host and not the customer portal.
            "ticket_url": frappe.utils.get_url("/helpdesk/tickets/" + str(self.name)),
        }

    def notify_agents_new_ticket(self):
        recipients = [
            a
            for a in frappe.get_all("HD Agent", filters={"is_active": 1}, pluck="name")
            if a and "@" in a
        ]
        if not recipients:
            return

        default_content = get_default_email_content("new_ticket_to_agents")
        try:
            with use_language(get_default_language()):
                frappe.sendmail(
                    recipients=recipients,
                    subject=_("[New request] #{0}: {1}").format(self.name, self.subject),
                    message=self._get_rendered_template(
                        None,
                        default_content,
                        self._agent_email_context(),
                    ),
                    reference_doctype="HD Ticket",
                    reference_name=self.name,
                    now=True,
                    email_headers={"X-Auto-Generated": "hd-new-ticket-agents"},
                )
        except Exception:
            self.log_error("Could not notify agents of the new ticket")

    def notify_agent_assignment(self, agent, assigned_by):
        """Email the agent a ticket got assigned to. The generic Frappe
        assignment mail is suppressed for agents, this replaces it."""
        if not agent or "@" not in agent:
            return
        if not frappe.db.exists("HD Agent", agent):
            return
        user = frappe.db.get_value(
            "User", agent, ["enabled", "language"], as_dict=True
        )
        if not user or not user.enabled:
            return

        try:
            context = self._agent_email_context()
            context["assigned_by"] = (
                frappe.db.get_value("User", assigned_by, "full_name") or assigned_by
            )
            with use_language(user.language or get_default_language()):
                frappe.sendmail(
                    recipients=[agent],
                    subject=_("[Assigned to you] #{0}: {1}").format(
                        self.name, self.subject
                    ),
                    message=self._get_rendered_template(
                        None,
                        get_default_email_content("assigned_to_agent"),
                        context,
                    ),
                    reference_doctype="HD Ticket",
                    reference_name=self.name,
                    now=True,
                    email_headers={"X-Auto-Generated": "hd-assignment-agent"},
                )
        except Exception:
            self.log_error("Could not notify the agent of the assignment")

    @frappe.whitelist()
    def mark_seen(self):
        self.add_viewed(
            unique_views=True, force=True
        )  # Document class method, no way to add unique_views via document settings, hence used force and unique_views=True
        self.add_seen()
        clear_notifications(ticket=self.name)

    def set_sla(self):
        """
        Find an SLA to apply to this ticket.
        """
        if sla := get_sla(self):
            self.sla = sla.name

    def apply_sla(self):
        """
        Apply SLA if set.
        """
        if sla := frappe.get_last_doc("HD Service Level Agreement", {"name": self.sla}):
            sla.apply(self)

    def get_sla(self):
        return frappe.get_doc("HD Service Level Agreement", {"name": self.sla})

    def is_currently_outside_working_hours(self):
        """Return True if current time is outside this SLA's working hours."""

        sla = self.get_sla()
        current_date = getdate()
        now = now_datetime()

        current_td = timedelta(
            hours=now.hour,
            minutes=now.minute,
            seconds=now.second,
            microseconds=now.microsecond,
        )

        day_name = current_date.strftime("%A")
        Holiday = DocType("HD Holiday")

        # Check holidays for this SLA
        holidays = (
            frappe.qb.from_(Holiday)
            .select(Holiday.holiday_date)
            .where(Holiday.parent == sla.name)
            .run(pluck=True)
        )

        if current_date in holidays:
            return True

        working_hours = sla.get_working_hours()
        # No working hours today
        if day_name not in working_hours:
            return True

        start_time, end_time = working_hours[day_name]

        # Outside working hours
        if not (start_time <= current_td < end_time):
            return True
        return False

    def set_default_status(self):
        if self.is_new():
            self.status = self.default_open_status

    def set_status_category(self):
        self.status_category = self.status_category or frappe.get_value(
            "HD Ticket Status",
            self.status,
            "category",
        )

    def get_merge_target(self):
        # Follow the chain of merged tickets to the final, non-merged ticket. Return None
        # if the chain dead-ends on a missing ticket or loops back on itself (a corrupt
        # cycle), so a reply is never redirected onto another merged ticket.
        current_ticket_name = self.merged_with
        visited_ticket_names = {self.name}
        while current_ticket_name and current_ticket_name not in visited_ticket_names:
            ticket = frappe.db.get_value(
                "HD Ticket",
                current_ticket_name,
                ["is_merged", "merged_with"],
                as_dict=True,
            )
            if not ticket:
                return None
            visited_ticket_names.add(current_ticket_name)
            if not ticket.is_merged:
                return current_ticket_name
            if not ticket.merged_with:
                return None
            current_ticket_name = ticket.merged_with
        return None

    def redirect_communication_to_merge_target(self, communication):
        merge_target_name = self.get_merge_target()
        if not merge_target_name:
            return False
        communication.db_set("reference_name", merge_target_name)
        merge_target = frappe.get_doc("HD Ticket", merge_target_name)
        merge_target.on_communication_update(communication)
        return True

    # `on_communication_update` is a special method exposed from `Communication` doctype.
    # It is called when a communication is updated. Beware of changes as this effectively
    # is an external dependency. Refer `communication.py` of Frappe framework for more.
    # Since this is called from communication itself, `c` is the communication doc.
    def on_communication_update(self, c):
        # A reply to a merged ticket belongs to its merge target; redirect it there. If no
        # safe target resolves (cycle/dead-end), fall through and handle it here so the
        # reply isn't dropped.
        if c.sent_or_received == "Received" and self.is_merged and self.merged_with:
            if self.redirect_communication_to_merge_target(c):
                return

        # If communication is incoming, then it is a reply from customer, and ticket must
        # be reopened.
        # handle re opening tickets for email
        if c.sent_or_received == "Received":
            # check if agent has replied

            if self.has_agent_replied:
                self.status = self.ticket_reopen_status
            else:
                self.status = self.default_open_status
            # if received that means customer has replied
            self.last_customer_response = frappe.utils.now_datetime()
            # Notify agents of the reply, on any channel (email-in and portal
            # both create a Received communication that reaches this handler).
            self.notify_agents_new_reply(c)
        # If communication is outgoing, it must be a reply from agent
        if c.sent_or_received == "Sent":
            # Ignore system notifications
            if c.communication_type and c.communication_type == "Automated Message":
                return
            # Set first response date if not set already
            self.first_responded_on = (
                self.first_responded_on or frappe.utils.now_datetime()
            )
            self.last_agent_response = frappe.utils.now_datetime()

            # TODO: remove this feature once we add automation feature
            if frappe.db.get_single_value("HD Settings", "auto_update_status"):
                self.status = frappe.db.get_single_value(
                    "HD Settings", "update_status_to"
                )

        # Fetch description from communication if not set already. This might not be needed
        # anymore as a communication is created when a ticket is created.
        self.description = self.description or c.content
        # Save the ticket, allowing for hooks to run.
        self.save()

    def attach_file_with_doc(self, doctype, docname, file_url):
        if frappe.db.exists(
            "File",
            {
                "file_url": file_url,
                "attached_to_doctype": doctype,
                "attached_to_name": docname,
            },
        ):
            return
        file_doc = frappe.new_doc("File")
        file_doc.attached_to_doctype = doctype
        file_doc.attached_to_name = docname
        file_doc.file_url = file_url
        file_doc.save(ignore_permissions=True)

    @staticmethod
    def default_list_data(show_customer_portal_fields=False):
        columns = [
            {
                "label": "ID",
                "type": "Int",
                "key": "name",
                "width": "auto",
            },
            {
                "label": "Subject",
                "type": "Data",
                "key": "subject",
                "width": "25rem",
            },
            {
                "label": "Status",
                "type": "Select",
                "key": "status",
                "width": "8rem",
            },
            {
                "label": "First response",
                "type": "Datetime",
                "key": "response_by",
                "width": "8rem",
            },
            {
                "label": "Resolution",
                "type": "Datetime",
                "key": "resolution_by",
                "width": "8rem",
            },
            {
                "label": "Assigned To",
                "type": "MultipleAvatar",
                "key": "_assign",
                "width": "8rem",
            },
            {
                "label": "Customer",
                "type": "Link",
                "key": "customer",
                "options": "HD Customer",
                "width": "8rem",
            },
            {
                "label": "Priority",
                "type": "Link",
                "options": "HD Ticket Priority",
                "key": "priority",
                "width": "10rem",
            },
            {
                "label": "Type",
                "type": "Link",
                "options": "HD Ticket Type",
                "key": "ticket_type",
                "width": "11rem",
            },
            {
                "label": "Team",
                "type": "Link",
                "options": "HD Team",
                "key": "agent_group",
                "width": "10rem",
            },
            {
                "label": "Contact",
                "type": "Link",
                "key": "contact",
                "options": "Contact",
                "width": "8rem",
            },
            {
                "label": "Rating",
                "type": "Rating",
                "key": "feedback_rating",
                "width": "10rem",
            },
            {
                "label": "Created",
                "type": "Datetime",
                "key": "creation",
                "options": "Contact",
                "width": "8rem",
            },
        ]
        customer_portal_columns = [
            {
                "label": "ID",
                "type": "Int",
                "key": "name",
                "width": "5rem",
            },
            {
                "label": "Subject",
                "type": "Data",
                "key": "subject",
                "width": "22rem",
            },
            {
                "label": "Status",
                "type": "Select",
                "key": "status",
                "width": "11rem",
            },
            {
                "label": "Priority",
                "type": "Link",
                "options": "HD Ticket Priority",
                "key": "priority",
                "width": "10rem",
            },
            {
                "label": "First response",
                "type": "Datetime",
                "key": "response_by",
                "width": "8rem",
            },
            {
                "label": "Resolution",
                "type": "Datetime",
                "key": "resolution_by",
                "width": "8rem",
            },
            {
                "label": "Team",
                "type": "Link",
                "options": "HD Team",
                "key": "agent_group",
                "width": "10rem",
            },
            {
                "label": "Created",
                "type": "Datetime",
                "key": "creation",
                "options": "Contact",
                "width": "8rem",
            },
        ]
        rows = [
            "name",
            "subject",
            "status",
            "priority",
            "ticket_type",
            "agent_group",
            "contact",
            "agreement_status",
            "response_by",
            "resolution_by",
            "customer",
            "first_responded_on",
            "modified",
            "creation",
            "_assign",
            "resolution_date",
        ]
        return {
            "columns": (
                customer_portal_columns if show_customer_portal_fields else columns
            ),
            "rows": rows,
        }

    def parse_content(self, content):
        """
        Finds 'src' attribute of img/video and replaces it  with 'embed' attribute
        embed tag is important because framework replaces it with <img src="cid:content_id">
        this in turn is displayed as an image in the mail sent to the customer
        """
        if not content:
            return ""

        soup = BeautifulSoup(content, "html.parser")

        # comments (e.g. Outlook MSO conditionals in quoted replies) get mangled
        # by the markdown conversion in sendmail and show up as visible text
        for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
            comment.extract()

        for tag in soup.find_all(["img", "video"]):
            if tag.name == "img":
                tag["embed"] = tag.get("src")
            elif tag.name == "video":
                tag["embed"] = tag.get("src")

        return str(soup)

    @staticmethod
    def filter_standard_fields(fields):
        for f in fields:
            if f["name"] in customer_not_allowed_fields:
                fields.remove(f)
        return fields


# Check if `user` has access to this specific ticket (`doc`). This implements extra
# permission checks which is not possible with standard permission system. This function
# is being called from hooks. `doc` is the ticket to check against
def has_permission(doc, user=None):
    user = user or frappe.session.user
    if is_admin(user):
        return True
    if user in (doc.contact, doc.raised_by, doc.owner):
        return True
    if _is_customer_manager(doc.customer, user):
        return True
    if _user_in_cc(doc, user):
        return True
    if not is_agent(user):
        return False
    return _agent_has_permission(doc, user)


def _user_in_cc(doc, user: str) -> bool:
    """A user CC'd on the ticket may view it, even if it is not their own.

    CC is both an email loop and a visibility grant, matched on the user's email
    against the stored participant list."""
    cc = doc.get("fab_cc")
    if not cc:
        return False
    email = (frappe.db.get_value("User", user, "email") or user or "").lower()
    if not email:
        return False
    return email in {addr.lower() for _, addr in getaddresses([cc]) if addr}


def _is_customer_manager(customer: str, user: str) -> bool:
    return any(
        c.get("name") == customer and c.get("is_manager")
        for c in get_customers(user, get_roles=True)
    )


def _agent_has_permission(doc, user: str) -> bool:
    if not frappe.db.get_single_value("HD Settings", "restrict_tickets_by_agent_group"):
        return True
    show_tickets_without_team = frappe.db.get_single_value(
        "HD Settings", "do_not_restrict_tickets_without_an_agent_group"
    )
    if show_tickets_without_team and not doc.get("agent_group"):
        return True

    if doc.get("_assign"):
        try:
            if user in json.loads(doc._assign):
                return True
        except (ValueError, TypeError):
            return False

    teams = get_agents_team()
    if any(team.get("ignore_restrictions") for team in teams):
        return True

    team_names = [t.team_name for t in teams]
    is_team_member = frappe.db.exists(
        "HD Team Member", {"parent": ["in", team_names], "user": frappe.session.user}
    )
    return bool(is_team_member) and doc.get("agent_group") in team_names


# Custom perms for list query. Only the `WHERE` part
# https://frappeframework.com/docs/user/en/python-api/hooks#modify-list-query
def permission_query(user: str | None = None):
    user = user or frappe.session.user
    if is_admin(user):
        return
    if not is_agent(user):
        return _customer_query(user)
    return _agent_query(user)


def _customer_query(user: str) -> str:
    """Non-agents see their own tickets, plus all tickets of customers whose whole
    set is visible to them: customers they manage, and customers configured for
    company-wide ticket visibility (fab_ticket_visibility = Company-wide)."""
    query = _get_base_visibility(user)
    visible_customers = _get_full_view_customers(user)
    if visible_customers:
        query += " OR " + _build_in_clause("customer", visible_customers)
    return query


def _get_company_wide_customers(user: str) -> list[str]:
    """Customers the user belongs to whose ticket visibility is set to company-wide,
    so every member sees all of that customer's tickets, not just managers."""
    if not frappe.db.has_column("HD Customer", "fab_ticket_visibility"):
        return []
    return [
        c
        for c in get_customers(user)
        if frappe.db.get_value("HD Customer", c, "fab_ticket_visibility")
        == "Company-wide"
    ]


def _get_full_view_customers(user: str) -> list[str]:
    """Customers whose entire ticket set the user may see: managed + company-wide."""
    return list(set(_get_managed_customers(user)) | set(_get_company_wide_customers(user)))


def _agent_query(user: str) -> str | None:
    query = _get_base_visibility(user)

    if not frappe.db.get_single_value("HD Settings", "restrict_tickets_by_agent_group"):
        return  # Restrictions disabled, return all tickets

    show_tickets_without_team = frappe.db.get_single_value(
        "HD Settings", "do_not_restrict_tickets_without_an_agent_group"
    )
    if show_tickets_without_team:
        query += " OR (`tabHD Ticket`.agent_group is null OR `tabHD Ticket`.agent_group = '')"

    # An agent on a team with `ignore_restrictions` set can see every team's tickets.
    teams = get_agents_team()
    if any(team.get("ignore_restrictions") for team in teams):
        all_teams = frappe.get_all("HD Team", pluck="name")
        if not all_teams:
            return query
        query += " OR (" + _build_in_clause("agent_group", all_teams) + ")"
        if not show_tickets_without_team:
            query += " OR (`tabHD Ticket`.agent_group is null)"
        return query

    query += " OR (JSON_SEARCH(`tabHD Ticket`._assign, 'all', {u}) IS NOT NULL)".format(
        u=frappe.db.escape(user)
    )
    team_names = [t.get("team_name") for t in teams]
    if team_names:
        query += " OR (" + _build_in_clause("agent_group", team_names) + ")"
    return query


def _get_base_visibility(user: str) -> str:
    """WHERE fragment for tickets a user is directly tied to: owner, contact, or raiser."""

    return "(`tabHD Ticket`.owner = {u} OR `tabHD Ticket`.contact = {u} OR `tabHD Ticket`.raised_by = {u})".format(
        u=frappe.db.escape(user)
    )


def _get_managed_customers(user: str) -> list[str]:
    return [
        str(c.get("name"))
        for c in get_customers(user, get_roles=True)
        if c.get("is_manager")
    ]


def _build_in_clause(field: str, values: list[str]) -> str:
    _values = ", ".join(frappe.db.escape(v) for v in values)
    return f"`tabHD Ticket`.{field} in ({_values})"


def set_guest_ticket_creation_permission():
    doctype = "HD Ticket"
    add_permission(doctype, "Guest", 0)

    role = "Guest"
    permlevel = 0
    ptype = ["read", "write", "create", "if_owner"]

    for p in ptype:
        # update permissions
        update_permission_property(doctype, role, permlevel, p, 1)


def remove_guest_ticket_creation_permission():
    doctype = "HD Ticket"
    role = "Guest"
    permlevel = 0
    remove(doctype, role, permlevel, 1)


customer_not_allowed_fields = ["customer"]


def close_tickets_after_n_days():
    if frappe.db.get_single_value("HD Settings", "auto_close_tickets") == 0:
        return

    status, days_threshold = frappe.db.get_value(
        "HD Settings", "HD Settings", ["auto_close_status", "auto_close_after_days"]
    )
    days_threshold = cint(days_threshold)

    # Compute the cutoff in the system timezone to match how communication_date is
    # stored. Using the database's NOW() instead would select the wrong tickets when
    # the DB server runs in a different timezone (e.g. UTC) than the Frappe system.
    inactivity_cutoff = add_to_date(now_datetime(), days=-days_threshold)

    tickets_to_close = (
        frappe.db.sql(
            """
                SELECT t.name
                FROM `tabHD Ticket` t
                INNER JOIN (
                    SELECT reference_name, MAX(communication_date) as last_communication_date
                    FROM `tabCommunication`
                    WHERE reference_doctype = 'HD Ticket'
                    GROUP BY reference_name
                ) latest_comm ON t.name = latest_comm.reference_name
                WHERE t.status = %(status)s
                AND latest_comm.last_communication_date < %(inactivity_cutoff)s
            """,
            {"inactivity_cutoff": inactivity_cutoff, "status": status},
            pluck="name",
        )
        or []
    )
    tickets_to_close = list(set(tickets_to_close))

    # cant do set_value because SLA will not be applied as setting directly to db and doc is not running.
    for ticket in tickets_to_close:
        doc = frappe.get_doc("HD Ticket", ticket)
        doc.status = "Closed"
        doc.flags.ignore_validate = True
        try:
            doc.save(ignore_permissions=True)
            # activity log for auto closing the ticket
            log_ticket_activity(
                doc.name,
                f"automatically closed the ticket after {days_threshold} day{'s' if days_threshold > 1 else ''} of inactivity",
            )
        except Exception as e:
            frappe.log_error(
                message=f"Failed to auto close ticket {doc.name} after {days_threshold} days. Error: {e}",
                title="Auto Close Ticket Failed",
            )
            continue

        frappe.db.commit()  # nosemgrep


def update_sla_status_in_ticket():
    stale_tickets = frappe.get_all(
        "HD Ticket",
        filters={
            "status_category": ["=", "Open"],
            "sla": ["is", "set"],
        },
        pluck="name",
    )
    for ticket in stale_tickets:
        doc = frappe.get_doc("HD Ticket", ticket)
        sla = frappe.get_doc("HD Service Level Agreement", doc.sla)
        sla.handle_agreement_status(doc)
        try:
            frappe.db.set_value(
                "HD Ticket",
                doc.name,
                "agreement_status",
                doc.agreement_status,
                update_modified=False,
            )

        except Exception as e:
            frappe.log_error(
                message=f"Failed to update agreement status for ticket {doc.name}. Error: {e}",
                title="Update SLA Status Failed",
            )
            continue
        frappe.db.commit()  # nosemgrep
