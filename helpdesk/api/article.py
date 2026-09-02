import re

import frappe
from textblob import TextBlob
from textblob.exceptions import MissingCorpusError

from helpdesk.search import NUM_RESULTS
from helpdesk.search import search as hd_search


def get_nouns(blob: TextBlob):
    try:
        return [word for word, pos in blob.pos_tags if pos[0] == "N"]
    except LookupError:
        return []


def get_noun_phrases(blob: TextBlob):
    try:
        return blob.noun_phrases
    except (LookupError, MissingCorpusError):
        return []


def search_with_enough_results(
    prev_res: list, query: str, qtype="and"
) -> tuple[list, bool]:
    out = hd_search(query, qtype=qtype)
    if not out:
        return prev_res, len(prev_res) == NUM_RESULTS
    items = prev_res + out[0].get("items", [])
    items = list({v["id"]: v for v in items}.values())[:NUM_RESULTS]  # unique results
    return items, len(items) == NUM_RESULTS


def sanitize_query(query: str) -> str:
    q = query.strip().lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    # Collapse multiple spaces into one
    q = re.sub(r"\s+", " ", q)
    return q.strip()


@frappe.whitelist()
def get_article_stats(article_name: str):
    views = frappe.db.get_value("HD Article", article_name, "views")

    likes = frappe.db.count(
        "HD Article Feedback",
        filters={
            "article": article_name,
            "feedback": 1,
        },
    )

    dislikes = frappe.db.count(
        "HD Article Feedback",
        filters={
            "article": article_name,
            "feedback": 2,
        },
    )

    return {
        "views": views,
        "likes": likes,
        "dislikes": dislikes,
    }


def _article_name(item) -> str | None:
    """Article name from a search hit, whether it exposes .name, .id or a dict."""
    name = getattr(item, "name", None)
    if not name and isinstance(item, dict):
        name = item.get("name") or (item.get("id") or "").split(":", 1)[-1]
    if not name:
        raw = getattr(item, "id", "") or ""
        name = raw.split(":", 1)[-1] if ":" in raw else None
    return name


def _filter_visible_articles(items: list, language: str | None) -> list:
    """Keep only the search hits the caller may actually see (audience) and, when
    given, that match the language. Audience is enforced by running the candidate
    names back through get_list, which applies the HD Article permission query."""
    names = [n for n in (_article_name(i) for i in items) if n]
    if not names:
        return items
    from helpdesk.api.knowledge_base import language_or_filters

    filters = {"name": ["in", names], "status": "Published"}
    visible = set(
        frappe.get_list(
            "HD Article",
            filters=filters,
            or_filters=language_or_filters(language),
            pluck="name",
            limit_page_length=0,
        )
    )
    return [i for i in items if _article_name(i) in visible]


@frappe.whitelist()
def search(query: str, language: str | None = None) -> list:
    return _filter_visible_articles(_raw_search(query), language)


def _raw_search(query: str) -> list:
    query = sanitize_query(query)
    ret, enough = search_with_enough_results([], query)
    if enough:
        return ret
    blob = TextBlob(query)  # fallback
    if noun_phrases := get_noun_phrases(blob):
        query = " ".join(noun_phrases)
        ret, enough = search_with_enough_results(ret, query)
        if enough:
            return ret
        ret, enough = search_with_enough_results(ret, query, qtype="or")
        if enough:
            return ret
    if nouns := get_nouns(blob):
        query = " ".join(nouns)
        ret, enough = search_with_enough_results(ret, query)
        if enough:
            return ret
        ret, enough = search_with_enough_results(ret, query, qtype="or")
    return ret
