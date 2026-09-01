import frappe
from frappe import _
from frappe.translate import get_all_translations


@frappe.whitelist()
def set_language(language: str):
    """Let a logged-in portal user change their own interface language.

    Portal customers cannot write their User doc through the document API, so the
    switch goes through this method, which only ever touches the caller's own
    user and accepts an enabled language."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if not frappe.db.exists("Language", language):
        frappe.throw(_("Unknown language: {0}").format(language))
    frappe.db.set_value("User", frappe.session.user, "language", language)
    return language


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_translations():
    language = None
    if frappe.session.user != "Guest":
        language = frappe.db.get_value("User", frappe.session.user, "language")
    if not language:
        language = frappe.db.get_single_value("System Settings", "language")
    return get_all_translations(language)
