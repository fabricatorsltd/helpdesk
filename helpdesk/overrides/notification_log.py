import frappe
from frappe.desk.doctype.notification_log.notification_log import (
    NotificationLog,
    set_notifications_as_unseen,
)


class HelpdeskNotificationLog(NotificationLog):
    def after_insert(self):
        """Agents get their own styled email from the helpdesk on a ticket
        assignment, so keep the in-app notification but drop the generic mail.
        A non-agent assignee has no replacement email and keeps the generic one."""
        if (
            self.type == "Assignment"
            and self.document_type == "HD Ticket"
            and frappe.db.exists("HD Agent", self.for_user)
        ):
            frappe.publish_realtime(
                "notification", after_commit=True, user=self.for_user
            )
            set_notifications_as_unseen(self.for_user)
            return

        super().after_insert()
