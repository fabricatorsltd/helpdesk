import re
from email import message_from_string
from email.utils import getaddresses

import frappe
from frappe import _
from frappe.core.doctype.communication.communication import Communication
from frappe.email.doctype.email_account.email_account import EmailAccount
from frappe.email.doctype.email_queue.email_queue import EmailQueue
from frappe.email.receive import InboundMail


class CustomInboundMail(InboundMail):
    """
    Extend InboundMail with robust thread stitching for forwarded emails.
       1. Run the standard Frappe parent_communication lookups first (In-Reply-To → Communication, EmailQueue, communication-name fallback)
       2. If still no parent, use the References header from emails, which may contain multiple message IDs in a thread
    """

    def _find_communication_by_message_id(self, msg_id: str):
        """Return a Communication for msg_id, checking both Communication and EmailQueue."""
        # Direct hit: incoming email stored its message_id on Communication
        comm = Communication.find_one_by_filters(
            message_id=msg_id, order_by="creation DESC"
        )
        if comm:
            return comm

        # Outgoing email: message_id lives in EmailQueue, not on Communication
        eq = EmailQueue.find_one_by_filters(message_id=msg_id)
        if eq and eq.communication:
            return Communication.find(eq.communication, ignore_error=True) or None

        return None

    def parent_communication(self):
        # Respect cached result from any prior call on this instance
        if self._parent_communication is not None:
            return self._parent_communication

        # Run the standard Frappe lookup method first. Checks for finding in reply to in Communication then if not found it looks in EmailQueue
        result = super().parent_communication()
        if result:
            return result

        # fallback: use the References header from emails
        references_raw = self.mail.get("References") or ""
        ref_ids = re.findall(r"<([^>]+)>", references_raw)

        for ref_id in reversed(ref_ids):
            communication = self._find_communication_by_message_id(ref_id)
            if communication:
                self._parent_communication = communication
                return self._parent_communication

        self._parent_communication = ""
        return self._parent_communication

    def reference_document(self):
        # Respect cached result from any prior call on this instance
        if self._reference_document is not None:
            return self._reference_document

        reference_document = super().reference_document()
        if reference_document:
            return reference_document

        # Frappe only falls back to subject matching when the doctype sits on the
        # Email Account. With IMAP folders it sits on the folder row and reaches us
        # as self.append_to, so a reply carrying "(#0026)" but no usable In-Reply-To
        # opened a second ticket. Recover the thread from the subject, but only for
        # senders already entitled to it.
        if self.append_to == "HD Ticket" and not self.email_account.append_to:
            ticket = self.ticket_from_subject()
            if ticket:
                self._reference_document = ticket
                return self._reference_document

        self._reference_document = ""
        return self._reference_document

    def ticket_from_subject(self):
        """Ticket named in the subject tag, if the sender may write to it."""
        name = self.get_reference_name_from_subject()
        if not name or not frappe.db.exists("HD Ticket", name):
            return None

        ticket = self.get_doc("HD Ticket", name, ignore_error=True)
        if not ticket or not self.sender_belongs_to_ticket(ticket):
            return None

        return ticket

    def sender_belongs_to_ticket(self, ticket):
        sender = (self.from_email or "").lower()
        if not sender:
            return False

        allowed = {
            addr.lower()
            for _, addr in getaddresses([ticket.get("fab_cc") or ""])
            if addr
        }
        if ticket.raised_by:
            allowed.add(ticket.raised_by.lower())
        if ticket.contact:
            contact_email = frappe.db.get_value("Contact", ticket.contact, "email_id")
            if contact_email:
                allowed.add(contact_email.lower())
        if sender in allowed:
            return True

        if frappe.db.exists("HD Agent", {"name": sender, "is_active": 1}):
            return True

        own = {a.lower() for a in frappe.get_all("Email Account", pluck="email_id") if a}
        return sender in own


class CustomEmailAccount(EmailAccount):
    def get_inbound_mails(self) -> list[InboundMail]:
        """retrive and return inbound mails."""
        mails = []

        def process_mail(messages, append_to=None):
            for index, message in enumerate(messages.get("latest_messages", [])):
                try:
                    _msg = message_from_string(
                        message.decode("utf-8", errors="replace")
                    )

                    # Important: If the email is auto-generated, we do not create a ticket
                    if _msg.get("X-Auto-Generated"):
                        continue

                    uid = (
                        messages["uid_list"][index]
                        if messages.get("uid_list")
                        else None
                    )
                    seen_status = messages.get("seen_status", {}).get(uid)
                    if self.email_sync_option != "UNSEEN" or seen_status != "SEEN":
                        _inbound_mail = CustomInboundMail(
                            message,
                            self,
                            frappe.safe_decode(uid),
                            seen_status,
                            append_to,
                        )
                        mails.append(_inbound_mail)
                except Exception as e:
                    # Log the error but continue processing other emails
                    frappe.log_error(
                        title=_(
                            "Error processing email at index {0}, message: {1}"
                        ).format(index, e),
                        message=frappe.get_traceback(),
                    )
                    self.handle_bad_emails(index, message, frappe.get_traceback())
                    continue

        if not self.enable_incoming:
            return []

        try:
            if self.service == "Frappe Mail":
                frappe_mail_client = self.get_frappe_mail_client()
                messages = frappe_mail_client.pull_raw(
                    last_received_at=self.last_synced_at
                )
                process_mail(messages)
                self.db_set(
                    "last_synced_at",
                    messages["last_received_at"],
                    update_modified=False,
                )
            else:
                email_sync_rule = self.build_email_sync_rule()
                email_server = self.get_incoming_server(
                    in_receive=True, email_sync_rule=email_sync_rule
                )
                if self.use_imap:
                    # process all given imap folder
                    for folder in self.imap_folder:
                        if email_server.select_imap_folder(folder.folder_name):
                            email_server.settings["uid_validity"] = folder.uidvalidity
                            messages = (
                                email_server.get_messages(
                                    folder=f'"{folder.folder_name}"'
                                )
                                or {}
                            )
                            process_mail(messages, folder.append_to)
                else:
                    # process the pop3 account
                    messages = email_server.get_messages() or {}
                    process_mail(messages)

                # close connection to mailserver
                email_server.logout()
        except Exception:
            self.log_error(
                title=_("Error while connecting to email account {0}").format(self.name)
            )
            return []

        return mails
