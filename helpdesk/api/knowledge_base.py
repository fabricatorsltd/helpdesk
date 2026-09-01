import frappe
from bs4 import BeautifulSoup
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import get_user_info_for_avatar

from helpdesk.utils import get_customers, is_agent


def _reader_can_see_article(article) -> bool:
    """Audience gate for non-staff readers: published, and either public or
    restricted to one of the reader's customers. The fab_* fields are optional
    (present only when fab_helpdesk is installed); missing means public."""
    if article.get("status") != "Published":
        return False
    if (article.get("fab_visibility") or "Public") == "Public":
        return True
    allowed = {r.get("customer") for r in (article.get("fab_customers") or [])}
    return bool(set(get_customers()) & allowed)


@frappe.whitelist(allow_guest=True)
def get_article(name: str):
    article = frappe.get_doc("HD Article", name).as_dict()

    if not is_agent() and not _reader_can_see_article(article):
        frappe.throw(_("Access denied"), frappe.PermissionError)

    author = get_user_info_for_avatar(article["author"])
    feedback = (
        frappe.db.get_value(
            "HD Article Feedback",
            {"article": name, "user": frappe.session.user},
            "feedback",
        )
        or 0
    )

    return {
        "name": article.name,
        "title": article.title,
        "content": article.content,
        "author": author,
        "creation": article.creation,
        "status": article.status,
        "published_on": article.published_on,
        "modified": article.modified,
        "category_name": frappe.db.get_value(
            "HD Article Category", article.category, "category_name"
        ),
        "category_id": article.category,
        "feedback": int(feedback),
    }

    return article


@frappe.whitelist()
def delete_articles(articles: list[str]):
    for article in articles:
        frappe.delete_doc("HD Article", article)


@frappe.whitelist()
def create_category(title: str):
    if title.strip().lower() == "general":
        frappe.throw(
            _(
                "General is a reserved category name. Please use a different name to proceed."
            )
        )
    category = frappe.new_doc("HD Article Category", category_name=title).insert()
    article = frappe.new_doc(
        "HD Article", title="New Article", category=category.name
    ).insert()
    return {"article": article.name, "category": category.name}


@frappe.whitelist()
def move_to_category(category: str, articles: list[str]):
    frappe.has_permission("HD Article", "write", throw=True)

    for article in articles:
        try:
            article_category = frappe.db.get_value("HD Article", article, "category")
            category_existing_articles = frappe.db.count(
                "HD Article", {"category": article_category}
            )
            if category_existing_articles == 1:
                frappe.throw(_("Category must have atleast one article"))
                return
            else:
                frappe.db.set_value(
                    "HD Article", article, "category", category, update_modified=False
                )
        except Exception as e:
            frappe.db.rollback()
            frappe.throw(_("Error moving article to category"))


@frappe.whitelist()
def get_categories(language: str | None = None):
    categories = frappe.get_list(
        "HD Article Category",
        fields=["name", "category_name", "modified"],
    )
    article_filters = {"status": "Published"}
    if language and frappe.db.has_column("HD Article", "fab_language"):
        article_filters["fab_language"] = language
    for c in categories:
        # get_list (not db.count) so the audience permission filter and the
        # optional language filter both apply to the visible-article count.
        c["article_count"] = len(
            frappe.get_list(
                "HD Article",
                filters={**article_filters, "category": c.name},
                pluck="name",
                limit_page_length=0,
            )
        )

    categories.sort(key=lambda c: c["article_count"], reverse=True)
    categories = [c for c in categories if c["article_count"] > 0]
    return categories


@frappe.whitelist()
def get_category_articles(category: str, language: str | None = None):
    filters = {"category": category, "status": "Published"}
    if language and frappe.db.has_column("HD Article", "fab_language"):
        filters["fab_language"] = language
    articles = frappe.get_list(
        "HD Article",
        filters=filters,
        fields=["name", "title", "published_on", "modified", "author", "content"],
    )
    for article in articles:
        article["author"] = get_user_info_for_avatar(article["author"])
        soup = BeautifulSoup(article["content"], "html.parser")
        article["content"] = str(soup.text)[:100]

    return articles


@frappe.whitelist()
def merge_category(source: str, target: str):
    frappe.has_permission("HD Article Category", "delete", throw=True)

    if source == target:
        frappe.throw(_("Source and target category cannot be same"))
    general_category = get_general_category()
    if source == general_category:
        frappe.throw(_("Cannot merge General category"))
    source_articles = frappe.get_all(
        "HD Article",
        filters={"category": source},
        pluck="name",
    )
    for article in source_articles:
        frappe.db.set_value(
            "HD Article", article, "category", target, update_modified=False
        )

    frappe.delete_doc("HD Article Category", source)


@frappe.whitelist()
def get_general_category():
    return frappe.db.get_value(
        "HD Article Category", {"category_name": "General"}, "name"
    )


@frappe.whitelist()
def get_category_title(category: str):
    return frappe.db.get_value("HD Article Category", category, "category_name")


@frappe.whitelist()
@rate_limit(key="article", seconds=60 * 60)
def increment_views(article: str):
    views = frappe.db.get_value("HD Article", article, "views") or 0
    views += 1
    frappe.db.set_value("HD Article", article, "views", views, update_modified=False)
