import frappe


def after_insert(doc, method=None):
    """Every assignment path (desk, portal API, assignment rules) inserts a
    ToDo, so notify the agent from here."""
    # Assignment rules fire while an email account backfills its mailbox; stay
    # quiet there, like the acknowledgement and new ticket emails do.
    if frappe.flags.initial_sync:
        return
    if doc.reference_type != "HD Ticket" or doc.status != "Open":
        return
    if not doc.allocated_to or doc.allocated_to == doc.assigned_by:
        return
    if not frappe.db.exists("HD Ticket", doc.reference_name):
        return

    ticket = frappe.get_doc("HD Ticket", doc.reference_name)
    ticket.notify_agent_assignment(
        doc.allocated_to, doc.assigned_by or frappe.session.user
    )
