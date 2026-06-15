import os
import json
from urllib.request import Request, urlopen
from urllib.parse import quote
import yaml
from serpapi import GoogleSearch
from util import *


# Crossref types that are NOT journal articles (skip these)
NON_JOURNAL_TYPES = {
    "proceedings-article",
    "paper-conference",
    "poster",
    "speech",
    "report",
}


JOURNAL_PREFERRED_TYPES = {
    "journal-article",
    "article-journal",
}


@log_cache
@cache.memoize(name=__file__, expire=30 * (60 * 60 * 24))
def find_doi(title, author_first):
    """Query Crossref API to find DOI by title and first author.
    Returns (doi_id, crossref_type) or (None, None).
    Prefers journal articles over preprints when multiple versions exist."""
    try:
        query = quote(title)
        url = f"https://api.crossref.org/works?query.title={query}&rows=5"
        if author_first:
            url += f"&query.author={quote(author_first)}"
        request = Request(
            url=url,
            headers={"User-Agent": "VCL-LN/1.0 (mailto:visual_cognition_lab@ln.edu.hk)"},
        )
        response = json.loads(urlopen(request).read())
        items = response.get("message", {}).get("items", [])

        # First pass: look for a journal article
        for item in items:
            doi = item.get("DOI", "")
            ctype = item.get("type", "")
            if doi and ctype in ("journal-article", "article-journal"):
                return f"doi:{doi}", ctype

        # Fallback: return the first result
        if items:
            doi = items[0].get("DOI", "")
            ctype = items[0].get("type", "")
            if doi:
                return f"doi:{doi}", ctype
    except Exception:
        pass
    return None, None


def main(entry):
    """
    receives single list entry from google-scholar data file
    returns list of sources to cite
    """

    # get api key (serp api key to access google scholar)
    api_key = os.environ.get("GOOGLE_SCHOLAR_API_KEY", "")
    if not api_key:
        raise Exception('No "GOOGLE_SCHOLAR_API_KEY" env var')

    # get id from entry
    _id = get_safe(entry, "gsid", "")
    if not _id:
        raise Exception('No "gsid" key')

    # fetch all articles with pagination (num=100 max per page)
    @log_cache
    @cache.memoize(name=__file__, expire=1 * (60 * 60 * 24))
    def fetch_all(_id):
        all_articles = []
        start = 0
        page = 1
        while True:
            p = {
                "engine": "google_scholar_author",
                "api_key": api_key,
                "author_id": _id,
                "num": 100,
                "start": start,
            }
            result = GoogleSearch(p).get_dict()
            articles = get_safe(result, "articles", [])
            log(f"    Page {page}: {len(articles)} article(s)", 2)
            all_articles.extend(articles)
            if len(articles) < 100:
                break
            start += 100
            page += 1
        return all_articles

    all_articles = fetch_all(_id)
    log(f"Total: {len(all_articles)} article(s)", 1)

    # deduplicate by title (prefer entries with a DOI)
    article_map = {}
    for work in all_articles:
        title = get_safe(work, "title", "").lower().strip().rstrip(".")
        if not title:
            continue
        authors = get_safe(work, "authors", "")
        author_first = authors.split(",")[0].strip() if authors else ""
        doi_id = crossref_type = None
        if title:
            doi_id, crossref_type = find_doi(title, author_first)

        if title not in article_map:
            article_map[title] = {
                "doi_id": doi_id,
                "type": crossref_type,
                "work": work,
            }
        elif doi_id and not article_map[title]["doi_id"]:
            # second occurrence has a DOI but first didn't → replace
            article_map[title] = {
                "doi_id": doi_id,
                "type": crossref_type,
                "work": work,
            }
        # else: already have a DOI or same quality → keep first

    log(f"After dedup: {len(article_map)} unique article(s)", 1)

    # list of sources to return
    sources = []

    # build sources from deduplicated articles
    for title_key, info in article_map.items():
        work = info["work"]
        doi_id = info["doi_id"]
        crossref_type = info.get("type", "")
        year = get_safe(work, "year", "")
        title = get_safe(work, "title", "")
        authors = get_safe(work, "authors", "")

        # Skip non-journal types (conference proceedings, posters, etc.)
        if crossref_type and crossref_type in NON_JOURNAL_TYPES:
            log(f"Skipping {crossref_type}: {doi_id}", 3)
            continue

        if doi_id:
            log(f"Found DOI: {doi_id}", 3)
            # DOI found — let Manubot handle full citation details
            source = {"id": doi_id}
        else:
            # no DOI — keep Google Scholar citation data as fallback
            source = {
                "id": get_safe(work, "citation_id", ""),
                "title": title,
                "authors": (
                    list(map(str.strip, authors.split(","))) if authors else []
                ),
                "publisher": get_safe(work, "publication", ""),
                "date": (year + "-01-01") if year else "",
                "link": get_safe(work, "link", ""),
            }

        # copy fields from entry to source
        source.update(entry)

        # add source to list
        sources.append(source)

    # --- Exclusion list (user adds IDs here to remove unwanted articles) ---
    exclude_path = "_data/gs-exclude.yaml"
    if os.path.isfile(exclude_path):
        try:
            exclude_data = load_data(str(exclude_path))
            exclude_ids = set()
            for entry in exclude_data:
                eid = get_safe(entry, "id", "")
                if eid:
                    exclude_ids.add(str(eid))
            if exclude_ids:
                before = len(sources)
                sources = [s for s in sources if get_safe(s, "id", "") not in exclude_ids]
                log(f"Excluded: {before - len(sources)} article(s)", 1)
        except Exception:
            log("Could not parse exclude file", 2, "WARNING")

    # --- Write new articles (not in sources.yaml) to gs-new.yaml ---
    try:
        new_articles = []
        if os.path.isfile("_data/sources.yaml"):
            known = load_data("_data/sources.yaml")
            known_ids = {s.get("id", "") for s in known}
            new_articles = [s for s in sources if s.get("id", "") not in known_ids]

        if new_articles:
            new_dump = [{"id": s["id"]} for s in new_articles if s.get("id", "")]
            with open("_data/gs-new.yaml", "w", encoding="utf-8") as f:
                f.write("# New articles from Google Scholar (not yet in sources.yaml)\n")
                f.write("# Review and merge relevant ones into sources.yaml\n")
                f.write("---\n")
                yaml.dump(new_dump, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            log(f"New articles: {len(new_dump)} written to gs-new.yaml", 1)
        elif os.path.isfile("_data/sources.yaml"):
            log("No new articles found (all already in sources.yaml)", 1)
    except Exception:
        log("Could not check sources.yaml for new articles", 2, "WARNING")

    return sources
