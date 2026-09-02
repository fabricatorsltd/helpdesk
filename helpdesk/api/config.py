import frappe


@frappe.whitelist(allow_guest=True)
def get_config():
    fields = [
        "brand_name",
        "brand_logo",
        "favicon",
        "prefer_knowledge_base",
        "setup_complete",
        "skip_email_workflow",
        "is_feedback_mandatory",
        "restrict_tickets_by_agent_group",
        "assign_within_team",
        "disable_saved_replies_global_scope",
        "enable_comment_reactions",
        "show_customer_portal_permission_notice",
    ]
    res = frappe.get_value(doctype="HD Settings", fieldname=fields, as_dict=True)

    res.favicon = (
        res.favicon
        or frappe.db.get_single_value("Website Settings", "favicon")
        or "/assets/helpdesk/desk/favicon.svg"
    )
    res.helpdesk_url = get_helpdesk_url()
    return res


def get_helpdesk_url() -> str:
    """Public origin of the customer portal (agents work on the ERP host, the
    links they share must point customers to the portal host)."""
    url = ""
    if frappe.get_meta("HD Settings").has_field("helpdesk_url"):
        url = frappe.db.get_single_value("HD Settings", "helpdesk_url") or ""
    url = (url or frappe.conf.get("helpdesk_host") or "").strip()
    return url.rstrip("/")
