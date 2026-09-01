import frappe
from frappe import _
from frappe.core.doctype.user_invitation.user_invitation import UserInvitation

from helpdesk.helpdesk.doctype.hd_settings.helpers import (
    get_default_language,
    use_language,
)


class HelpdeskUserInvitation(UserInvitation):
    def _get_email_title(self):
        # Use the org's brand name (set during onboarding) in the invitation
        # subject — "You've been invited to join <brand> on Helpdesk" — falling
        # back to the framework app title when it isn't set. The override class
        # is registered site-wide, so invitations from other apps (ERPNext,
        # etc.) must keep their own app title.
        if self.app_name != "helpdesk":
            return super()._get_email_title()
        brand = frappe.db.get_single_value("HD Settings", "brand_name")
        if not brand:
            return super()._get_email_title()
        return _("{0} on {1}").format(brand, super()._get_email_title())

    def _after_insert(self):
        # Mirror the framework invitation send, but for helpdesk invites route
        # the accept link to the customer portal host (the desk host_name would
        # land the customer on a domain where their SSO button is hidden) and
        # render the email in the helpdesk default language.
        if self.app_name != "helpdesk":
            return super()._after_insert()
        key = frappe.generate_hash()
        self.db_set("key", frappe.utils.sha256_hash(key))
        invite_link = self._helpdesk_invite_link(key)
        with use_language(get_default_language()):
            email_title = self._get_email_title()
            frappe.sendmail(
                recipients=self.email,
                subject=_("You've been invited to join {0}").format(email_title),
                template="user_invitation",
                args={"title": email_title, "invite_link": invite_link},
                now=True,
            )
        self.db_set("email_sent_at", frappe.utils.now())
        return key

    def _helpdesk_invite_link(self, key: str) -> str:
        path = f"/api/method/frappe.core.api.user_invitation.accept_invitation?key={key}"
        # Route the accept link to the customer portal host. Prefer the configured
        # HD Settings helpdesk_url (config-as-code, e.g. https://help.example.com);
        # fall back to the site_config override and only then to get_url, whose
        # host_name points at the desk domain (wrong host for a customer).
        host = (frappe.db.get_single_value("HD Settings", "helpdesk_url") or "").strip()
        if not host:
            host = (frappe.conf.get("helpdesk_host") or "").strip()
        if not host:
            return frappe.utils.get_url(path)
        if not host.startswith(("http://", "https://")):
            host = "https://" + host
        return host.rstrip("/") + path
