import frappe

from helpdesk.utils import get_helpdesk_url


def before_insert(doc, method=None):
    if (
        doc.type == "Assignment"
        and doc.document_type == "HD Ticket"
        and doc.document_name
    ):
        doc.link = get_helpdesk_url("/helpdesk/tickets/" + str(doc.document_name))
