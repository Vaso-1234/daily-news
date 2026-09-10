"""Fetch the top headlines from WSJ and BBC, enrich each one by scraping the
article page for a longer description + Open Graph image, generate a ~100 word
natural-language summary, and write site/data.json for the daily-news site.

Style rules for summary text:
  * no em dashes or en dashes (replaced with commas)
  * sentence-final periods rendered as '...'
  * trailing '...' on every summary

Runs both locally and inside the GitHub Actions cron. Fails soft so a single
bad feed or article never breaks the whole build.
"""

from __future__ import annotations

import html
import json
import logging
import re
import socket
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("fetch_news")

socket.setdefaulttimeout(20)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "site" / "data.json"

SUMMARY_TARGET_WORDS = 100
SUMMARY_HARD_MAX_WORDS = 120
ARTICLE_TIMEOUT_SEC = 12
# Rolling window: keep every item published in the last N hours, drop the rest.
# This makes the site behave like a briefing that accumulates through the day
# and clears once you've had 24 hours to read it.
WINDOW_HOURS = 24
# Safety cap so a very chatty news day can't produce an unreadably long page.
MAX_ITEMS_TOTAL = 80
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class FeedSpec:
    source: str
    section: str
    url: str


# WSJ's public RSS at feeds.a.dj.com has been serving multi-month stale content
# in Sep 2026. Google News returns opaque redirect URLs that need protobuf
# decoding to resolve. Bing News RSS returns real destination URLs in its
# apiclick "url=" query param, so we use that as the WSJ source.
BING_NEWS_BASE = "https://www.bing.com/news/search"

# Focused on economics, business, tech, and markets. No general news, sports,
# or human-interest stuff. WSJ queries are scoped to the relevant URL prefixes.
FEEDS: list[FeedSpec] = [
    FeedSpec(
        "WSJ",
        "Business",
        f"{BING_NEWS_BASE}?q=site%3Awsj.com%2Fbusiness&format=rss&count=25&freshness=day",
    ),
    FeedSpec(
        "WSJ",
        "Economy",
        f"{BING_NEWS_BASE}?q=site%3Awsj.com%2Feconomy&format=rss&count=25&freshness=day",
    ),
    FeedSpec(
        "WSJ",
        "Markets",
        f"{BING_NEWS_BASE}?q=site%3Awsj.com%2Ffinance&format=rss&count=25&freshness=day",
    ),
    FeedSpec(
        "WSJ",
        "Tech",
        f"{BING_NEWS_BASE}?q=site%3Awsj.com%2Ftech&format=rss&count=25&freshness=day",
    ),
    FeedSpec("BBC", "Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    FeedSpec("BBC", "Tech", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
]


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Lines / fragments that are boilerplate junk and should never enter a summary.
_BOILERPLATE_PATTERNS = [
    re.compile(r"copyright\s+\u00a9?\s*\d{4}", re.IGNORECASE),
    re.compile(r"all\s+rights\s+reserved", re.IGNORECASE),
    re.compile(r"^\s*[a-f0-9]{25,}\s*$", re.IGNORECASE),  # WSJ tracking hash
    re.compile(r"dow\s+jones\s*&\s*company", re.IGNORECASE),
    re.compile(r"subscribe\s+to\s+", re.IGNORECASE),
    re.compile(r"^\s*(follow live|live coverage)[:\s]", re.IGNORECASE),
    # BBC breaking-news template
    re.compile(r"this\s+breaking\s+news\s+story\s+is\s+being\s+updated", re.IGNORECASE),
    re.compile(r"please\s+refresh\s+the\s+page", re.IGNORECASE),
    re.compile(r"you\s+can\s+receive\s+breaking\s+news", re.IGNORECASE),
    re.compile(r"(follow\s+@\w+|get\s+the\s+latest\s+alerts)", re.IGNORECASE),
    re.compile(r"(sign\s+up\s+for\s+(our\s+)?newsletter|newsletter\s+sign\s+up)", re.IGNORECASE),
]

# Cleanup fragments inside otherwise-good sentences.
_ARTIFACTS_RE = re.compile(
    r"(,\s*external\b|\bimage\s+source[,:]?|\bimage\s+caption[,:]?)",
    re.IGNORECASE,
)

# Elements we don't want any text from at all.
_JUNK_TAGS = (
    "script",
    "style",
    "noscript",
    "figcaption",
    "figure",
    "aside",
    "nav",
    "header",
    "footer",
    "form",
    "button",
)


session = requests.Session()
session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
)


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", raw))
    return _WS_RE.sub(" ", text).strip()


def parse_published(entry) -> str | None:
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if not value:
            continue
        try:
            dt = date_parser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, TypeError, OverflowError):
            continue
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct:
        try:
            dt = datetime(*struct[:6], tzinfo=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            return None
    return None


def unwrap_click_link(link: str) -> str:
    """Bing News (apiclick.aspx) and similar wrappers put the real URL in ?url=."""
    if not link:
        return link
    lower = link.lower()
    if "bing.com/news/apiclick" not in lower and "url=" not in lower:
        return link
    try:
        parsed = urlparse(link)
        qs = parse_qs(parsed.query)
        if "url" in qs and qs["url"]:
            return qs["url"][0]
    except (ValueError, TypeError):
        pass
    return link


def strip_source_suffix(title: str, source: str) -> str:
    """Some aggregator feeds append ' - WSJ' to titles."""
    if not title:
        return title
    aliases = {
        "WSJ": ("WSJ", "The Wall Street Journal", "Wall Street Journal"),
        "BBC": ("BBC", "BBC News"),
    }.get(source, (source,))
    for sep in (" - ", " | ", " \u2013 ", " \u2014 "):
        for alias in aliases:
            suffix = f"{sep}{alias}"
            if title.endswith(suffix):
                return title[: -len(suffix)].strip()
    return title.strip()


# BBC's ichef.bbci.co.uk serves the same image at multiple sizes. The path
# segment /standard/<width>/ or /branded_news/<width>/ controls the width.
# RSS gives us /240/, which looks blurry on a full-width card, so we rewrite
# to /1024/ where possible.
_BBC_IMG_UPSCALE_RE = re.compile(
    r"(ichef\.bbci\.co\.uk/[^/]+/(?:standard|branded_news|news|test)/)\d+/"
)


def upscale_image_url(url: str) -> str:
    if not url:
        return url
    return _BBC_IMG_UPSCALE_RE.sub(r"\g<1>1024/", url)


_SIZE_HINT_RE = re.compile(r"/(\d{3,4})[x/]")


def image_size_hint(url: str) -> int:
    """Cheap heuristic for how big an image is likely to be, from the URL."""
    if not url:
        return 0
    m = _SIZE_HINT_RE.search(url)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    # WSJ's images.wsj.net/im-XXXX/social variant is ~1200x630 (social share).
    if "images.wsj.net" in url and "/social" in url:
        return 1200
    return 500  # neutral middle ground when we can't tell


def rss_images(entry) -> list[str]:
    """Pull any image URLs feedparser exposes on the RSS entry."""
    urls: list[str] = []
    for key in ("media_thumbnail", "media_content"):
        for m in entry.get(key, []) or []:
            url = m.get("url")
            if url:
                urls.append(url)
    for enc in entry.get("enclosures", []) or []:
        url = enc.get("href") or enc.get("url")
        if url and enc.get("type", "").startswith("image"):
            urls.append(url)
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and link.get("type", "").startswith(
            "image"
        ):
            urls.append(link.get("href"))
    return [upscale_image_url(u) for u in urls if u]


def is_boilerplate(text: str) -> bool:
    if not text:
        return True
    return any(p.search(text) for p in _BOILERPLATE_PATTERNS)


def clean_fragment(text: str) -> str:
    text = _ARTIFACTS_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip(" ,;:")
    return text


def fetch_article(url: str) -> tuple[list[str], list[str]]:
    """Return (paragraphs, image_urls) scraped from the article page.

    Works even when a paywall blocks the body, because OG tags stay public.
    Returns paragraphs as a list so the summary composer can dedupe them.
    """
    try:
        resp = session.get(url, timeout=ARTICLE_TIMEOUT_SEC, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("    article fetch failed (%s): %s", url, exc)
        return [], []

    # Force UTF-8 when the server doesn't advertise it explicitly; BBC often
    # serves UTF-8 pages without a charset header, which requests then
    # misdetects as latin-1 and turns "\u00a3129" into "\u00c2\u00a3129".
    if not resp.encoding or resp.encoding.lower() in {"iso-8859-1", "latin-1"}:
        resp.encoding = resp.apparent_encoding or "utf-8"

    try:
        soup = BeautifulSoup(resp.content, "lxml", from_encoding=resp.encoding)
    except Exception as exc:  # noqa: BLE001
        log.warning("    parse failed (%s): %s", url, exc)
        return [], []

    def meta(prop_or_name: str, key: str = "property") -> str | None:
        tag = soup.find("meta", attrs={key: prop_or_name})
        if tag and tag.get("content"):
            return tag["content"].strip()
        return None

    paragraphs: list[str] = []
    og_desc = meta("og:description") or meta("description", key="name")
    if og_desc and not is_boilerplate(og_desc):
        paragraphs.append(clean_fragment(og_desc))

    # Strip out captions, asides, nav, etc. so their text can't leak into <p> scans.
    for tag in soup.find_all(_JUNK_TAGS):
        tag.decompose()

    article_root = soup.find("article") or soup
    for p in article_root.find_all("p", limit=10):
        text = _WS_RE.sub(" ", p.get_text(" ", strip=True)).strip()
        if len(text) < 40:
            continue
        low = text.lower()
        if any(
            junk in low
            for junk in (
                "sign in",
                "subscribe",
                "cookies",
                "javascript",
                "please enable",
                "newsletter",
            )
        ):
            continue
        if is_boilerplate(text):
            continue
        paragraphs.append(clean_fragment(text))
        if sum(len(x.split()) for x in paragraphs) > 180:
            break

    images: list[str] = []
    og_image = meta("og:image") or meta("twitter:image", key="name") or meta(
        "twitter:image"
    )
    if og_image:
        images.append(upscale_image_url(urljoin(url, og_image)))

    return paragraphs, images


def _too_similar(a: str, b: str, threshold: float = 0.75) -> bool:
    """Cheap fuzzy dedupe so the same lede doesn't repeat 3 times in a card."""
    if not a or not b:
        return False
    short, long_ = sorted([a, b], key=len)
    if short in long_:
        return True
    ratio = SequenceMatcher(None, a, b).quick_ratio()
    if ratio < threshold:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def compose_summary(chunks: list[str]) -> str:
    """Dedupe near-identical chunks, stitch, trim to ~100 words at a sentence
    boundary."""
    picked: list[str] = []
    for chunk in chunks:
        clean = clean_fragment(chunk) if chunk else ""
        if not clean or is_boilerplate(clean):
            continue
        if any(_too_similar(clean, existing) for existing in picked):
            continue
        picked.append(clean)
        if sum(len(p.split()) for p in picked) >= SUMMARY_HARD_MAX_WORDS:
            break

    combined = " ".join(picked).strip()
    combined = _WS_RE.sub(" ", combined).strip()
    if not combined:
        return ""

    words = combined.split(" ")
    if len(words) <= SUMMARY_HARD_MAX_WORDS:
        return combined

    trimmed = " ".join(words[:SUMMARY_HARD_MAX_WORDS])
    last_period = max(
        trimmed.rfind(". "),
        trimmed.rfind("? "),
        trimmed.rfind("! "),
    )
    if last_period >= 60:
        trimmed = trimmed[: last_period + 1]
    else:
        trimmed = " ".join(trimmed.split(" ")[:SUMMARY_TARGET_WORDS])
    return trimmed.strip()


# em-dash, en-dash, and " - " used as visual em-dash all read as intrusive to
# the user's preferred aesthetic; regular hyphens inside words ("day-to-day")
# are left alone.
_DASH_RE = re.compile(r"\s*[\u2014\u2013]\s*|(?<=\S)\s+-\s+(?=\S)")
_TRAILING_DOTS_RE = re.compile(r"[.!?\s]+$")
_SENTENCE_END_RE = re.compile(r"(?<=[a-z0-9\)\]\"'])\.(?=\s+[A-Z\"'])")


def styleize(text: str) -> str:
    """Apply the user's style: no em/en dashes, sentence periods become '...'."""
    if not text:
        return ""
    text = _DASH_RE.sub(", ", text)
    text = _SENTENCE_END_RE.sub("...", text)
    text = _TRAILING_DOTS_RE.sub("", text).rstrip()
    if text:
        text = text + "..."
    return text


def dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        key = u.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def fetch_feed(spec: FeedSpec) -> list[dict]:
    log.info("fetching %s / %s", spec.source, spec.section)
    try:
        parsed = feedparser.parse(spec.url)
    except Exception as exc:  # noqa: BLE001
        log.warning("  failed to parse %s: %s", spec.url, exc)
        return []

    if parsed.bozo and not parsed.entries:
        log.warning("  bozo with no entries: %s", parsed.bozo_exception)
        return []

    items: list[dict] = []
    for entry in parsed.entries:
        link = unwrap_click_link((entry.get("link") or "").strip())
        title = strip_source_suffix(
            strip_html(entry.get("title", "")).strip(), spec.source
        )
        if not title or not link:
            continue
        # Bing News RSS publishes real one-sentence article snippets in its
        # <description>/<summary> field (its own public search-result excerpt).
        # These matter a lot for WSJ, whose article pages now return HTTP 401
        # with a JS-required bounce page to non-browser clients, so the on-page
        # scrape yields nothing and the Bing snippet is the ONLY summary text
        # we get. Google News, in contrast, just repeats the title inside an
        # <a> tag, so its "summary" is worthless and we deliberately drop it.
        raw_rss_summary = strip_html(
            entry.get("summary") or entry.get("description") or ""
        )
        if "news.google.com" in spec.url:
            rss_summary = ""
        else:
            rss_summary = raw_rss_summary
        items.append(
            {
                "title": title,
                "link": link,
                "rss_summary": rss_summary,
                "published": parse_published(entry),
                "source": spec.source,
                "section": spec.section,
                "rss_images": rss_images(entry),
            }
        )
    log.info("  got %d raw items", len(items))
    return items


def url_key(link: str) -> str:
    return (link or "").split("#")[0]


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_in_window(item: dict, cutoff: datetime) -> bool:
    pub = parse_iso(item.get("published"))
    return pub is not None and pub >= cutoff


def load_previous_items() -> list[dict]:
    if not OUTPUT_PATH.exists():
        return []
    try:
        payload = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        return payload.get("items", []) or []
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not load previous data.json: %s", exc)
        return []


def pick_best_image(candidates: list[str]) -> list[str]:
    """Return the single highest-resolution candidate (or nothing)."""
    unique = dedupe_urls([c for c in candidates if c])
    if not unique:
        return []
    unique.sort(key=image_size_hint, reverse=True)
    return unique[:1]


def enrich(item: dict) -> dict:
    log.info("  enriching: %s", item["title"][:80])
    paragraphs, _images_ignored = fetch_article(item["link"])

    chunks: list[str] = [*paragraphs]
    if item.get("rss_summary"):
        chunks.append(item["rss_summary"])
    raw_summary = compose_summary(chunks)
    summary = styleize(raw_summary)

    return {
        "title": item["title"],
        "link": item["link"],
        "summary": summary,
        "published": item.get("published"),
        "source": item["source"],
        "section": item["section"],
    }


def build_payload() -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)

    # 1. Carry forward previously enriched items that are still in the window
    # AND have a real summary. Items whose previous enrichment produced an
    # empty summary (typically because the article scrape was blocked by a
    # bot wall like Datadome) are NOT carried forward, so they fall back
    # through the enrichment path and get a fresh chance at the RSS-snippet
    # fallback introduced in the aggregator handling above.
    prev_items = load_previous_items()
    kept_prev: dict[str, dict] = {}
    dropped_empty = 0
    for item in prev_items:
        if not is_in_window(item, cutoff):
            continue
        if not (item.get("summary") or "").strip():
            dropped_empty += 1
            continue
        kept_prev[url_key(item.get("link", ""))] = item
    log.info(
        "carrying forward %d items still within the %dh window (dropped %d empty)",
        len(kept_prev),
        WINDOW_HOURS,
        dropped_empty,
    )

    # 2. Fetch fresh raw items from every feed.
    raw_items: list[dict] = []
    for spec in FEEDS:
        raw_items.extend(fetch_feed(spec))

    # 3. Only enrich items that are (a) in the window and (b) not already
    # enriched from a previous run. This keeps hourly refreshes cheap.
    to_enrich: list[dict] = []
    seen_new: set[str] = set()
    for item in raw_items:
        if not is_in_window(item, cutoff):
            continue
        key = url_key(item["link"])
        if key in kept_prev or key in seen_new:
            continue
        seen_new.add(key)
        to_enrich.append(item)
    log.info("enriching %d newly-seen items", len(to_enrich))

    enriched_new: list[dict] = []
    for item in to_enrich:
        try:
            enriched_new.append(enrich(item))
        except Exception as exc:  # noqa: BLE001
            log.warning("  enrich failed for %s: %s", item["link"], exc)
            enriched_new.append(
                {
                    "title": item["title"],
                    "link": item["link"],
                    "summary": styleize(item.get("rss_summary", "")),
                    "published": item.get("published"),
                    "source": item["source"],
                    "section": item["section"],
                }
            )

    # 4. Merge, sort newest-first, apply the safety cap.
    combined: dict[str, dict] = dict(kept_prev)
    for item in enriched_new:
        combined[url_key(item["link"])] = item

    items = sorted(
        combined.values(),
        key=lambda x: x.get("published") or "",
        reverse=True,
    )[:MAX_ITEMS_TOTAL]

    sources = sorted({spec.source for spec in FEEDS})
    counts = {src: sum(1 for i in items if i["source"] == src) for src in sources}

    return {
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "window_hours": WINDOW_HOURS,
        "sources": sources,
        "counts": counts,
        "items": items,
    }


def main() -> int:
    payload = build_payload()
    if not payload["items"]:
        log.error("no items produced; refusing to overwrite data.json")
        return 1
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log.info(
        "wrote %d items across %d sources to %s",
        len(payload["items"]),
        len(payload["sources"]),
        OUTPUT_PATH,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
