import frappe
from frappe.query_builder import DocType, Query


def query_get_one(q: Query) -> dict:
    r = q.run(as_dict=True)

    if len(r) != 1:
        return

    return r.pop()


def default_outgoing_email_account():
    QBEmailAccount = DocType("Email Account")

    r = (
        frappe.qb.from_(QBEmailAccount)
        .select(QBEmailAccount.star)
        .where(QBEmailAccount.default_outgoing == 1)
        .limit(1)
    )

    return query_get_one(r)


def default_ticket_outgoing_email_account():
    QBEmailAccount = DocType("Email Account")
    QBImapFolder = DocType("IMAP Folder")

    r = (
        frappe.qb.from_(QBEmailAccount)
        .select(QBEmailAccount.star)
        .where(QBEmailAccount.default_outgoing == 1)
        .inner_join(QBImapFolder)
        .on(QBImapFolder.parent == QBEmailAccount.name)
        .where(QBImapFolder.append_to == "HD Ticket")
        .limit(1)
    )

    return query_get_one(r)


def helpdesk_outgoing_email_account():
    """The one account a ticket reply is allowed to leave from.

    A reply is the helpdesk speaking to a customer, not the agent writing from
    their own desk: it must always go out from the support mailbox, whatever
    personal Email Accounts the agent happens to carry on their User record, and
    whatever account an earlier reply went out from by mistake.
    """
    return default_ticket_outgoing_email_account() or default_outgoing_email_account()
