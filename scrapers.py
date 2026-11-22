"""
Collects posts through each site's WordPress REST API (/wp-json/wp/v2/posts).

This replaced the old HTML scrapers: one request returns up to 100 posts *with* full text and
publish dates, so there's no per-article page scraping and nothing breaks when a theme changes.
"""
import html
import logging
import os
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
PER_PAGE = 100


def make_session(use_proxy: bool = False) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=["GET"], respect_retry_after_header=True)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    if use_proxy:
        session.proxies.update({"http": config.TOR_PROXY, "https": config.TOR_PROXY})
    return session


def html_to_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    for tag in soup(["script", "style", "iframe", "noscript"]):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


def _post_to_record(post: dict, source_name: str) -> dict | None:
    link = post.get("link")
    if not link:
        return None
    published = post.get("date_gmt")
    return {
        "link": link,
        "title": html.unescape(BeautifulSoup(post.get("title", {}).get("rendered", ""), "html.parser").get_text()).strip(),
        "source": source_name,
        "full_text": html_to_text(post.get("content", {}).get("rendered", "")),
        "published_at": f"{published}+00:00" if published else None,
    }


def scrape_source(source: dict, existing_links: set[str], since: datetime) -> list[dict]:
    name = source["name"]
    use_proxy = bool(source.get("proxy_in_ci") and os.getenv("GITHUB_ACTIONS"))
    session = make_session(use_proxy)
    url = f"{source['base_url'].rstrip('/')}/wp-json/wp/v2/posts"
    params = {
        "categories": source["category_id"],
        "per_page": PER_PAGE,
        "after": since.strftime("%Y-%m-%dT%H:%M:%S"),
        "orderby": "date",
        "order": "desc",
        "_fields": "link,title,date_gmt,content",
    }
    logger.info("%s: fetching posts since %s%s", name, since.date(), " via Tor" if use_proxy else "")

    records, page, total_pages = [], 1, 1
    while page <= total_pages:
        try:
            resp = session.get(url, params={**params, "page": page}, timeout=60)
        except requests.RequestException:
            logger.exception("%s: request for page %d failed", name, page)
            break
        if resp.status_code == 400 and page > 1:  # WordPress returns 400 past the last page
            break
        if resp.status_code != 200:
            logger.error("%s: HTTP %d on page %d: %s", name, resp.status_code, page, resp.text[:200])
            break
        total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
        posts = resp.json()
        new_on_page = 0
        for post in posts:
            record = _post_to_record(post, name)
            if record and record["link"] not in existing_links:
                records.append(record)
                new_on_page += 1
        logger.info("%s: page %d/%d -> %d posts, %d new", name, page, total_pages, len(posts), new_on_page)
        page += 1
        time.sleep(1.5)

    logger.info("%s: collected %d new posts", name, len(records))
    return records


def collect_all(existing_links: set[str]) -> list[dict]:
    since = datetime.now(timezone.utc) - relativedelta(months=config.ARTICLE_CUTOFF_MONTHS)
    collected, seen = [], set()
    for source in config.SOURCES:
        try:
            for record in scrape_source(source, existing_links, since):
                if record["link"] not in seen:  # de-duplicate cross-posts
                    seen.add(record["link"])
                    collected.append(record)
        except Exception:
            logger.exception("Source '%s' failed; continuing with the others", source["name"])
    return collected
