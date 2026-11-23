#!/usr/bin/env python3
"""Daily computer-vision paper collector.

The collector uses public metadata APIs and the Python standard library.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import http.client
import json
import logging
import math
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_ATOM_URL = "https://rss.arxiv.org/atom/cs.CV"
CVF_MENU_URL = "https://openaccess.thecvf.com/menu"
DEFAULT_TIMEZONE = "Asia/Hong_Kong"
DEFAULT_USER_AGENT = "cv-paper-watch/2.0 (+https://github.com/z196013/day)"

FOCUS_QUERIES = [
    "underwater image enhancement",
    "underwater image restoration",
    "underwater enhancement",
    "underwater restoration",
    "image restoration",
    "physical prior",
    "physics prior",
    "color correction",
    "color constancy",
    "white balance",
    "diffusion image restoration",
    "diffusion image enhancement",
    "Mamba image restoration",
    "Mamba image enhancement",
    "Transformer image restoration",
    "Transformer image enhancement",
    "low-light enhancement",
    "low light enhancement",
]

ARXIV_SPECIFIC_TERMS = [
    '"underwater image enhancement"',
    '"underwater image restoration"',
    '"underwater enhancement"',
    '"underwater restoration"',
    '"image restoration"',
    '"image enhancement"',
    '"physical prior"',
    '"physics prior"',
    '"color correction"',
    '"color constancy"',
    '"white balance"',
    '"low-light enhancement"',
    '"low light enhancement"',
]

ARXIV_ARCH_TERMS = ["diffusion", "Mamba", "Transformer"]
ARXIV_APPLICATION_TERMS = [
    "restoration",
    "enhancement",
    "low-light",
    '"low light"',
    '"color correction"',
    "dehazing",
    "denoising",
    "deblurring",
]

STOPWORDS = {
    "a",
    "an",
    "and",
    "by",
    "et",
    "for",
    "in",
    "of",
    "on",
    "the",
    "to",
    "with",
}

SOURCE_PRIORITY = {
    "CVF Open Access": 30,
    "OpenReview": 20,
    "arXiv cs.CV": 10,
}

RATING_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}
GITHUB_RE = re.compile(
    r"(?P<url>(?:https?://)?(?:www\.)?"
    r"(?:github\.com|gitlab\.com|bitbucket\.org)/[^\s<>'\")\]]+)",
    re.IGNORECASE,
)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s<>'\"]+", re.IGNORECASE)


@dataclass
class Paper:
    title: str
    authors: list[str] = field(default_factory=list)
    source: str = ""
    venue: str = ""
    paper_url: str = ""
    pdf_url: str = ""
    code_url: str = ""
    abstract: str = ""
    published_date: str = ""
    updated_date: str = ""
    doi: str = ""
    external_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    matched_keywords: list[str] = field(default_factory=list)
    possible_innovation: str = ""
    underwater_relation: str = ""
    worth_reading: bool = False
    recommendation: str = "C"
    score: float = 0.0


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            attr_map = {key.lower(): value for key, value in attrs if value is not None}
            self._href = attr_map.get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, normalize_whitespace(" ".join(self._text))))
            self._href = None
            self._text = []


class RateLimiter:
    def __init__(self) -> None:
        self._last_request: dict[str, float] = {}

    def wait(self, url: str, min_delay: float) -> None:
        if min_delay <= 0:
            return
        host = urllib.parse.urlparse(url).netloc
        now = time.monotonic()
        elapsed = now - self._last_request.get(host, 0.0)
        if elapsed < min_delay:
            time.sleep(min_delay - elapsed)
        self._last_request[host] = time.monotonic()


RATE_LIMITER = RateLimiter()


@dataclass
class RequestPolicy:
    timeout: float = 30.0
    retries: int = 2
    backoff: float = 3.2
    max_wait: float = 60.0


REQUEST_POLICY = RequestPolicy()


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def record_warning(warnings: list[str] | None, message: str) -> None:
    logging.warning(message)
    if warnings is not None:
        warnings.append(message)


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class DetailCache:
    """Cache successful CVF metadata, never failures or stale fallback results."""

    def __init__(self, path: Path | None, ttl_hours: float = 168) -> None:
        self.path = path
        self.ttl = ttl_hours * 3600
        self.entries: dict[str, Any] = {}
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("version") == 1 and isinstance(data.get("entries"), dict):
                    self.entries = data["entries"]
            except (OSError, ValueError, AttributeError) as exc:
                logging.warning("Ignoring unreadable CVF cache: %s", exc)

    def get(self, url: str) -> Paper | None:
        try:
            entry = self.entries[url]
            age = time.time() - entry["cached_at"]
            if not 0 <= age < self.ttl:
                return None
            paper = Paper(**entry["paper"])
            if not paper.title or paper.paper_url != url:
                return None
            return paper
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, paper: Paper) -> None:
        self.entries[paper.paper_url] = {"cached_at": time.time(), "paper": dataclasses.asdict(paper)}

    def save(self) -> None:
        if self.path:
            atomic_json(self.path, {"version": 1, "entries": self.entries})


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def strip_tags(value: str) -> str:
    text = re.sub(r"<script\b.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    return normalize_whitespace(re.sub(r"<[^>]+>", " ", text))


def normalize_doi(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    value = value.rstrip(".,;)")
    return value


def normalize_title(title: str) -> str:
    text = html.unescape(title or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    words = [word for word in text.split() if word not in STOPWORDS]
    return " ".join(words)


def canonical_arxiv_id(value: str) -> str:
    value = re.sub(r"^.*?arxiv\.org/(?:abs|pdf)/", "", value or "", flags=re.IGNORECASE)
    value = re.sub(r"^oai:arxiv\.org:", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\.pdf$", "", value)
    if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", value):
        return re.sub(r"v\d+$", "", value)
    return ""


def first_author_surname(authors: list[str]) -> str:
    if not authors:
        return ""
    first = normalize_whitespace(authors[0]).lower()
    if not first:
        return ""
    if "," in first:
        return re.sub(r"[^a-z0-9-]", "", first.split(",", 1)[0])
    parts = re.findall(r"[a-z0-9-]+", first)
    return parts[-1] if parts else ""


def jaccard_similarity(left: str, right: str) -> float:
    left_tokens = set(normalize_title(left).split())
    right_tokens = set(normalize_title(right).split())
    if not left_tokens and not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def find_code_url(*texts: str) -> str:
    for text in texts:
        for match in GITHUB_RE.finditer(text or ""):
            url = match.group("url").rstrip(".,;:)")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            return url
    return ""


def extract_doi(*texts: str) -> str:
    for text in texts:
        match = DOI_RE.search(text or "")
        if match:
            return normalize_doi(match.group(0))
    return ""


def retry_after_seconds(value: str) -> float:
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            return max(0.0, stamp.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def fetch_url(url: str, min_delay: float = 0.0, timeout: float | None = None) -> str:
    policy = REQUEST_POLICY
    user_agent = os.environ.get("PAPER_BOT_USER_AGENT", DEFAULT_USER_AGENT)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    target = urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl()
    for attempt in range(policy.retries + 1):
        RATE_LIMITER.wait(url, min_delay)
        retry_after = 0.0
        try:
            with urllib.request.urlopen(request, timeout=timeout or policy.timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            error = exc
            status = exc.code
            message = f"HTTP {exc.code} for {target}"
            retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
            retry_after = retry_after_seconds(exc.headers.get("Retry-After", "")) if exc.headers else 0
            exc.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            error = exc
            status = None
            message = f"Could not fetch {target}: {exc}"
            retryable = True
        if not retryable or attempt == policy.retries or retry_after > policy.max_wait:
            raise FetchError(message, status=status) from error
        delay = max(retry_after, min(policy.max_wait, policy.backoff * 2**attempt + random.uniform(0, 0.5)))
        logging.warning("%s; retry %d/%d in %.1fs", message, attempt + 1, policy.retries, delay)
        time.sleep(delay)
    raise AssertionError("unreachable")


def build_arxiv_query() -> str:
    specific = " OR ".join(f"all:{term}" for term in ARXIV_SPECIFIC_TERMS)
    arch = " OR ".join(f"all:{term}" for term in ARXIV_ARCH_TERMS)
    application = " OR ".join(f"all:{term}" for term in ARXIV_APPLICATION_TERMS)
    return f"cat:cs.CV AND ({specific} OR (({arch}) AND ({application})))"


def collect_arxiv(max_results: int, arxiv_delay: float, warnings: list[str] | None = None) -> list[Paper]:
    params = {
        "search_query": build_arxiv_query(),
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = ARXIV_API_URL + "?" + urllib.parse.urlencode(params)
    logging.info("Fetching arXiv cs.CV results")
    try:
        return parse_arxiv_feed(fetch_url(url, min_delay=max(3.2, arxiv_delay)))
    except (RuntimeError, ET.ParseError) as exc:
        api_error = str(exc)
    try:
        papers = parse_arxiv_feed(fetch_url(ARXIV_ATOM_URL, min_delay=max(3.2, arxiv_delay)))
    except (RuntimeError, ET.ParseError) as exc:
        raise RuntimeError(f"API: {api_error}; daily Atom fallback: {exc}") from exc
    record_warning(warnings, f"arXiv API unavailable ({api_error}); using daily cs.CV Atom feed. "
                   "Fallback covers the current announcement batch only, not the API search history.")
    relevant = []
    for paper in papers:
        paper.raw["acquisition"] = "daily-atom-fallback"
        classify_paper(paper)
        if paper.score > 0:
            relevant.append(paper)
    return relevant[:max_results]


def parse_arxiv_feed(xml_text: str) -> list[Paper]:
    root = ET.fromstring(xml_text)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
        "dc": "http://purl.org/dc/elements/1.1/",
    }
    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        raise RuntimeError("Unexpected arXiv response: expected an Atom feed")
    papers: list[Paper] = []
    for entry in root.findall("atom:entry", ns):
        title = normalize_whitespace(entry.findtext("atom:title", default="", namespaces=ns))
        abstract = normalize_whitespace(entry.findtext("atom:summary", default="", namespaces=ns))
        announcement_type = entry.findtext("arxiv:announce_type", default="", namespaces=ns)
        if announcement_type:
            abstract = re.sub(r"^arxiv:\S+\s+Announce Type:\s+\S+\s+Abstract:\s*", "", abstract,
                              flags=re.IGNORECASE)
        authors = [
            normalize_whitespace(author.findtext("atom:name", default="", namespaces=ns))
            for author in entry.findall("atom:author", ns)
        ]
        authors = [author for author in authors if author]
        if not authors:
            creators = entry.findtext("dc:creator", default="", namespaces=ns)
            authors = [normalize_whitespace(author) for author in creators.split(",") if author.strip()]
        abs_url = normalize_whitespace(entry.findtext("atom:id", default="", namespaces=ns))
        if "/api/errors" in abs_url or title.lower() == "error":
            raise RuntimeError(f"arXiv API error: {abstract}")
        if not title or not canonical_arxiv_id(abs_url):
            continue
        arxiv_id = canonical_arxiv_id(abs_url)
        if not abs_url.startswith(("https://", "http://")):
            abs_url = f"https://arxiv.org/abs/{arxiv_id}"
        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            title_attr = link.attrib.get("title", "")
            href = link.attrib.get("href", "")
            if title_attr == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = href
        doi = normalize_doi(entry.findtext("arxiv:doi", default="", namespaces=ns)
                            or entry.findtext("arxiv:DOI", default="", namespaces=ns))
        if not doi:
            doi = extract_doi(abstract, abs_url)
        if not pdf_url:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        published = entry.findtext("atom:published", default="", namespaces=ns)[:10]
        updated = entry.findtext("atom:updated", default="", namespaces=ns)[:10]
        raw = {"arxiv_id": arxiv_id}
        if announcement_type:
            # News timestamps describe announcements/feed generation, not the
            # original submission or revision timestamps returned by the API.
            raw.update(announcement_type=announcement_type, announcement_date=published)
            published = ""
            updated = ""
        code_url = find_code_url(abstract)
        papers.append(
            Paper(
                title=title,
                authors=authors,
                source="arXiv cs.CV",
                venue="arXiv cs.CV",
                paper_url=abs_url,
                pdf_url=pdf_url,
                code_url=code_url,
                abstract=abstract,
                published_date=published,
                updated_date=updated,
                doi=doi,
                external_id=arxiv_id,
                raw=raw,
            )
        )
    return papers


def parse_links(markup: str) -> list[tuple[str, str]]:
    parser = LinkParser()
    parser.feed(markup)
    return parser.links


def infer_cvf_venue(url: str) -> str:
    path = urllib.parse.urlparse(url).path.strip("/")
    if not path:
        return "CVF Open Access"
    segment = path.split("/")[0]
    segment = segment.replace("_", " ")
    match = re.search(r"(CVPR|ICCV|ECCV|WACV)\s*(20\d{2})(.*)", segment, re.IGNORECASE)
    if not match:
        return segment
    suffix = normalize_whitespace(match.group(3).replace("-", " "))
    venue = f"{match.group(1).upper()} {match.group(2)}"
    if suffix:
        venue += f" {suffix}"
    return venue


def discover_cvf_venues(max_venues: int) -> list[tuple[str, str]]:
    markup = fetch_url(CVF_MENU_URL, min_delay=0.5)
    links = parse_links(markup)
    venues: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, label in links:
        if not href:
            continue
        absolute = urllib.parse.urljoin(CVF_MENU_URL, href)
        if not re.search(r"(CVPR|ICCV|ECCV|WACV)20\d{2}", absolute, re.IGNORECASE):
            continue
        if label.lower() not in {"main conference", "findings", "workshops"}:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        venues.append((absolute, infer_cvf_venue(absolute)))
        if len(venues) >= max_venues:
            break
    return venues


def cvf_listing_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.query or "_workshops" in parsed.path:
        return url
    return urllib.parse.urlunparse(parsed._replace(query="day=all"))


def extract_cvf_paper_links(markup: str, base_url: str) -> list[tuple[str, str]]:
    links = parse_links(markup)
    papers: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, label in links:
        if "_paper.html" not in href:
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        if urllib.parse.urlparse(absolute).netloc != urllib.parse.urlparse(base_url).netloc:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        papers.append((absolute, label))
    return papers


def expand_cvf_listing_pages(
    url: str, max_pages: int = 10, markup_cache: dict[str, str] | None = None,
) -> list[str]:
    first_url = cvf_listing_url(url)
    pages = [first_url]
    markup = fetch_url(first_url, min_delay=0.5)
    if markup_cache is not None:
        markup_cache[first_url] = markup
    if extract_cvf_paper_links(markup, first_url):
        return pages

    for href, label in parse_links(markup):
        if len(pages) >= max_pages:
            break
        if not href or label.lower() == "menu":
            continue
        absolute = urllib.parse.urljoin(first_url, href)
        if urllib.parse.urlparse(absolute).netloc != urllib.parse.urlparse(first_url).netloc:
            continue
        if "_paper.html" in absolute or absolute.endswith(".pdf"):
            continue
        if re.search(r"(CVPR|ICCV|ECCV|WACV)20\d{2}", absolute, re.IGNORECASE):
            if absolute not in pages:
                pages.append(cvf_listing_url(absolute))
    return pages


def looks_relevant_from_title(title: str) -> bool:
    lowered = title.lower()
    direct_terms = [
        "underwater",
        "restoration",
        "enhancement",
        "low-light",
        "low light",
        "color correction",
        "color constancy",
        "white balance",
        "dehazing",
        "denoising",
        "deblurring",
        "deraining",
        "physical prior",
        "physics prior",
        "scattering",
    ]
    if any(term in lowered for term in direct_terms):
        return True
    architecture_terms = ["diffusion", "mamba", "transformer"]
    application_terms = ["image", "vision", "inverse", "degradation", "reconstruction"]
    return any(term in lowered for term in architecture_terms) and any(
        term in lowered for term in application_terms
    )


def extract_div_text(markup: str, div_id: str) -> str:
    match = re.search(
        rf"<div[^>]+id=[\"']{re.escape(div_id)}[\"'][^>]*>(.*?)</div>",
        markup,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return strip_tags(match.group(1)) if match else ""


def parse_cvf_detail(url: str, venue: str, fallback_title: str) -> Paper:
    markup = fetch_url(url, min_delay=0.5)
    title = extract_div_text(markup, "papertitle") or fallback_title
    authors_raw = extract_div_text(markup, "authors")
    authors_raw = re.split(r"\bProceedings\b", authors_raw, maxsplit=1)[0]
    authors_raw = re.split(r"\b\d{4}\b", authors_raw, maxsplit=1)[0]
    authors = [
        normalize_whitespace(author)
        for author in re.split(r"\s*,\s*|\s+;\s+", authors_raw)
        if normalize_whitespace(author)
    ]
    abstract = extract_div_text(markup, "abstract")
    links = parse_links(markup)
    pdf_url = ""
    for href, label in links:
        if href.lower().endswith(".pdf") and ("paper" in href.lower() or label.lower() == "pdf"):
            pdf_url = urllib.parse.urljoin(url, href)
            break
    if not pdf_url:
        for href, label in links:
            if href.lower().endswith(".pdf"):
                pdf_url = urllib.parse.urljoin(url, href)
                break
    code_url = find_code_url(markup, abstract)
    doi = extract_doi(markup)
    external_id = Path(urllib.parse.urlparse(url).path).stem
    return Paper(
        title=title,
        authors=authors,
        source="CVF Open Access",
        venue=venue,
        paper_url=url,
        pdf_url=pdf_url,
        code_url=code_url,
        abstract=abstract,
        doi=doi,
        external_id=external_id,
        raw={"cvf_venue": venue},
    )


def collect_cvf(
    max_venues: int, max_detail_pages: int, cache: DetailCache | None = None,
    warnings: list[str] | None = None,
) -> list[Paper]:
    logging.info("Discovering recent CVF Open Access venues")
    papers: list[Paper] = []
    candidates: dict[str, tuple[str, str]] = {}
    markup_cache: dict[str, str] = {}
    listing_successes = 0
    venues = discover_cvf_venues(max_venues)
    if not venues:
        raise RuntimeError("CVF menu returned no supported venues; the page format may have changed")
    for venue_url, venue in venues:
        logging.info("Scanning CVF venue: %s", venue)
        try:
            listing_pages = expand_cvf_listing_pages(venue_url, markup_cache=markup_cache)
        except Exception as exc:  # noqa: BLE001
            record_warning(warnings, f"Skipping CVF venue {venue}: {exc}")
            continue
        for listing_url in listing_pages:
            try:
                markup = markup_cache.get(listing_url)
                if markup is None:
                    markup = fetch_url(listing_url, min_delay=0.5)
            except Exception as exc:  # noqa: BLE001
                record_warning(warnings, f"Skipping CVF listing {listing_url}: {exc}")
                continue
            paper_links = extract_cvf_paper_links(markup, listing_url)
            if paper_links:
                listing_successes += 1
            for paper_url, title in paper_links:
                if looks_relevant_from_title(title):
                    candidates.setdefault(paper_url, (title, venue))
    if not listing_successes:
        raise RuntimeError("No CVF venue listings could be fetched")

    def priority(item: tuple[str, tuple[str, str]]) -> tuple[bool, float]:
        candidate = Paper(title=item[1][0])
        classify_paper(candidate)
        return candidate.raw["screening"]["relevance"] != "direct", -candidate.score

    attempts = 0
    cache_hits = 0
    consecutive_failures = 0
    try:
        for paper_url, (title, venue) in sorted(candidates.items(), key=priority):
            cached = cache.get(paper_url) if cache else None
            if cached is not None:
                papers.append(cached)
                cache_hits += 1
                continue
            if attempts >= max_detail_pages or consecutive_failures >= 3:
                continue
            attempts += 1  # Failed requests also consume the detail-page budget.
            try:
                paper = parse_cvf_detail(paper_url, venue, title)
                if not paper.title or not paper.abstract:
                    raise RuntimeError("Missing title/abstract; CVF page may be incomplete")
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                record_warning(warnings, f"Could not parse CVF paper {paper_url}: {exc}")
                continue
            consecutive_failures = 0
            papers.append(paper)
            if cache:
                cache.put(paper)
    finally:
        if cache:
            try:
                cache.save()
            except OSError as exc:
                record_warning(warnings, f"Could not save CVF cache: {exc}")
    logging.info("CVF: %d candidates, %d detail requests, %d cache hits", len(candidates), attempts, cache_hits)
    if candidates and attempts and not papers:
        raise RuntimeError("All attempted CVF detail requests failed")
    return papers


def unwrap_openreview_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def openreview_content(note: Any, *keys: str) -> Any:
    content = getattr(note, "content", {}) or {}
    for key in keys:
        if key in content:
            value = unwrap_openreview_value(content[key])
            if value:
                return value
    return ""


def openreview_date(note: Any, updated: bool = False) -> str:
    attributes = ("tmdate", "mdate", "pdate", "tcdate", "cdate") if updated else ("pdate", "tcdate", "cdate")
    for attr in attributes:
        value = getattr(note, attr, None)
        if isinstance(value, (int, float)) and value > 0:
            return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).date().isoformat()
    return ""


def normalize_openreview_authors(value: Any) -> list[str]:
    value = unwrap_openreview_value(value)
    if isinstance(value, list):
        names = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("fullname") or item.get("name") or ""
            if isinstance(item, str) and normalize_whitespace(item):
                names.append(normalize_whitespace(item))
        return names
    if isinstance(value, str):
        return [normalize_whitespace(item) for item in value.split(",") if normalize_whitespace(item)]
    return []


def collect_openreview(
    limit_per_term: int, request_delay: float, warnings: list[str] | None = None,
) -> list[Paper]:
    logging.info("Searching OpenReview")
    # Direct public GETs share timeouts/retries without requiring SDK credentials.
    # API v2 calls root submission notes "forum"; v1 calls them "papers".
    configured = os.environ.get("OPENREVIEW_API_BASEURL")
    bases = [configured] if configured else ["https://api2.openreview.net", "https://api.openreview.net"]
    papers: list[Paper] = []
    seen: set[str] = set()
    successful_queries = 0
    for base in bases:
        previous_successes = successful_queries
        consecutive_failures = 0
        for term in FOCUS_QUERIES:
            is_v1 = urllib.parse.urlparse(base).hostname == "api.openreview.net"
            params = {"term": term, "content": "all", "group": "all",
                      "source": "papers" if is_v1 else "forum", "limit": limit_per_term}
            if not is_v1:
                params["sort"] = "tmdate:desc"
            try:
                payload = json.loads(fetch_url(base.rstrip("/") + "/notes/search?" + urllib.parse.urlencode(params),
                                               min_delay=request_delay))
                if not isinstance(payload, dict) or not isinstance(payload.get("notes"), list):
                    raise RuntimeError("Unexpected OpenReview response: missing notes list")
                notes = [SimpleNamespace(**note) for note in payload["notes"] if isinstance(note, dict)]
                successful_queries += 1
                consecutive_failures = 0
            except (RuntimeError, ValueError, TypeError) as exc:
                record_warning(warnings, f"OpenReview {base} search failed for {term!r}: {exc}")
                consecutive_failures += 1
                if consecutive_failures >= 3 or (isinstance(exc, FetchError) and exc.status == 429):
                    break
                continue
            for note in notes:
                if getattr(note, "replyto", None) or getattr(note, "ddate", None):
                    continue
                note_id = getattr(note, "id", "") or getattr(note, "forum", "")
                if not note_id or note_id in seen:
                    continue
                seen.add(note_id)
                title = normalize_whitespace(str(openreview_content(note, "title") or ""))
                if not title:
                    continue
                abstract = normalize_whitespace(str(openreview_content(note, "abstract", "TLDR", "tldr") or ""))
                authors = normalize_openreview_authors(openreview_content(note, "authors"))
                venue = normalize_whitespace(
                    str(openreview_content(note, "venue", "venueid"))
                    or str(getattr(note, "invitation", "")).split("/", 1)[0]
                )
                forum = getattr(note, "forum", "") or note_id
                paper_url = f"https://openreview.net/forum?id={forum}"
                pdf = openreview_content(note, "pdf")
                pdf_url = ""
                if isinstance(pdf, str) and pdf:
                    pdf_url = pdf if pdf.startswith(("https://", "http://")) else f"https://openreview.net/pdf?id={note_id}"
                code_url = find_code_url(abstract, json.dumps(getattr(note, "content", {}), ensure_ascii=False))
                doi = normalize_doi(str(openreview_content(note, "doi"))) or extract_doi(
                    abstract, str(openreview_content(note, "html", "url")))
                papers.append(
                    Paper(
                        title=title,
                        authors=authors,
                        source="OpenReview",
                        venue=venue or "OpenReview",
                        paper_url=paper_url,
                        pdf_url=pdf_url,
                        code_url=code_url,
                        abstract=abstract,
                        published_date=openreview_date(note),
                        updated_date=openreview_date(note, updated=True),
                        doi=doi,
                        external_id=note_id,
                        raw={"openreview_id": note_id, "forum": forum, "search_term": term, "api_base": base},
                    )
                )
        if successful_queries > previous_successes:
            break  # Legacy API is a fallback, not a duplicate daily crawl.
    if not successful_queries:
        raise RuntimeError("All OpenReview searches failed")
    return papers


def matched_focus_terms(text: str) -> list[str]:
    lowered = " " + re.sub(r"[^\w]+", " ", text.casefold()) + " "
    matches: list[str] = []
    term_groups = {
        "underwater image enhancement": ["underwater image enhancement", "underwater enhancement"],
        "underwater image restoration": ["underwater image restoration", "underwater restoration", "underwater dehazing"],
        "image restoration": ["image restoration", "image deblurring", "image denoising", "image deraining",
                              "image dehazing", "image enhancement", "image super resolution", "video restoration",
                              "image recovery", "photo restoration", "blind denoising", "all in one restoration"],
        "physical prior": ["physical prior", "physics prior", "physics based", "physics guided", "optical model", "scattering model"],
        "color correction": ["color correction", "color constancy", "white balance", "color cast", "color casts"],
        "diffusion": ["diffusion", "score based", "generative prior"],
        "Mamba": ["mamba", "state space model", "selective scan"],
        "Transformer": ["transformer", "transformers", "self attention", "cross attention"],
        "low-light enhancement": ["low light enhancement", "low light image enhancement", "low light image restoration",
                                  "low light video enhancement", "low light image denoising", "illumination enhancement"],
    }
    for label, needles in term_groups.items():
        if any(" " + needle + " " in lowered for needle in needles):
            matches.append(label)
    if re.search(r"\bunderwater\b.{0,65}\b(enhance\w*|restor\w*|dehaz\w*)\b", lowered):
        if "underwater image enhancement" not in matches:
            matches.append("underwater image enhancement")
    return matches


def classify_paper(paper: Paper) -> None:
    # Establish the task before awarding architecture, venue, or code bonuses.
    # Venue names and incidental keywords never establish relevance themselves.
    title_terms = matched_focus_terms(paper.title)
    primary_abstract = " ".join(re.split(r"(?<=[.!?])\s+", paper.abstract)[:2])[:700]
    primary_terms = matched_focus_terms(primary_abstract)
    matches = matched_focus_terms(paper.title + " " + paper.abstract)
    tasks = {"underwater image enhancement", "underwater image restoration", "image restoration",
             "physical prior", "color correction", "low-light enhancement"}
    title_text = re.sub(r"[^\w]+", " ", paper.title.casefold())
    other_task = any(term in title_text for term in (
        "image generation", "image synthesis", "text to image", "video generation", "avatar",
        "object detection", "person detection", "semantic segmentation", "action recognition",
        "visual question answering", "3d generation",
    ))
    visual = bool(re.search(r"\b(images?|imaging|photos?|visual|color)\b",
                            (paper.title + " " + primary_abstract).lower()))
    title_tasks = tasks.intersection(title_terms)
    primary_tasks = tasks.intersection(primary_terms)
    if not visual:
        title_tasks.discard("physical prior")
        primary_tasks.discard("physical prior")
    eligible = bool(title_tasks or (primary_tasks and not other_task))
    direct = eligible and any("underwater" in term for term in title_tasks | primary_tasks)
    breakdown: dict[str, float] = {}
    if eligible:
        if direct:
            breakdown["水下增强/复原任务"] = 40 if any("underwater" in t for t in title_tasks) else 30
        for term, weight in (("image restoration", 18), ("physical prior", 10),
                             ("color correction", 14), ("low-light enhancement", 16)):
            if term in title_tasks | primary_tasks:
                breakdown[term] = weight
        if title_tasks:
            breakdown["标题任务匹配"] = 8
        for term, weight in (("diffusion", 8), ("Mamba", 8), ("Transformer", 4)):
            if term in matches:
                breakdown[term] = weight
        if paper.code_url:
            breakdown["提供代码链接"] = 3
        venue = paper.venue.upper()
        if paper.source == "CVF Open Access":
            breakdown["已公开会议论文"] = 2 if "WORKSHOP" in venue else 5
        elif paper.source == "OpenReview" and re.search(r"\b(ACCEPTED|ORAL|POSTER|SPOTLIGHT)\b", venue):
            if not re.search(r"SUBMIT|REJECT|WITHDRAW|UNDER REVIEW", venue):
                breakdown["已标注接收状态"] = 5
    score = sum(breakdown.values())
    if not paper.abstract and score > 34:
        breakdown["缺少摘要，限制推荐等级"] = 34 - score
        score = 34
    recommendation = "S" if direct and score >= 45 else "A" if score >= 42 else "B" if score >= 24 else "C"
    if not eligible:
        reason = "缺少明确的增强/复原任务证据；架构、会议或代码链接不单独构成推荐依据。"
        relevance = "out-of-scope"
    elif direct:
        reason = "标题或摘要开头明确涉及水下图像增强/复原，建议核对全文与实验。"
        relevance = "direct"
    else:
        reason = "存在复原、颜色、低照度或成像物理任务证据；向水下场景迁移仍需实验验证。"
        relevance = "transfer"
    paper.matched_keywords = matches
    paper.score = score
    paper.possible_innovation = "关键词线索（待全文核对）：" + ("、".join(matches) or "无") + "。"
    paper.underwater_relation = reason
    paper.recommendation = recommendation
    paper.worth_reading = eligible and (recommendation in {"S", "A"} or direct)
    paper.raw["screening"] = {"version": 2, "relevance": relevance, "reason": reason,
                              "score_breakdown": breakdown}


SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    authors TEXT,
    first_author TEXT,
    source TEXT NOT NULL,
    venue TEXT,
    paper_url TEXT,
    pdf_url TEXT,
    code_url TEXT,
    abstract TEXT,
    possible_innovation TEXT,
    underwater_relation TEXT,
    worth_reading INTEGER NOT NULL DEFAULT 0,
    recommendation TEXT NOT NULL DEFAULT 'C',
    score REAL NOT NULL DEFAULT 0,
    matched_keywords TEXT,
    published_date TEXT,
    updated_date TEXT,
    doi TEXT,
    external_id TEXT,
    fingerprint TEXT NOT NULL UNIQUE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_normalized_title ON papers(normalized_title);
CREATE INDEX IF NOT EXISTS idx_papers_last_seen ON papers(last_seen);
CREATE INDEX IF NOT EXISTS idx_papers_recommendation ON papers(recommendation);

CREATE TABLE IF NOT EXISTS paper_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    venue TEXT,
    external_id TEXT,
    paper_url TEXT,
    pdf_url TEXT,
    source_key TEXT NOT NULL UNIQUE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    sources TEXT NOT NULL,
    total_seen INTEGER NOT NULL DEFAULT 0,
    new_papers INTEGER NOT NULL DEFAULT 0,
    report_path TEXT,
    errors TEXT
);

CREATE TABLE IF NOT EXISTS run_papers (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    is_new INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, paper_id)
);
"""


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA)
    return conn


def rescore_legacy_papers(conn: sqlite3.Connection) -> int:
    """Upgrade screening metadata without changing identity or observation dates."""
    changed = 0
    for row in conn.execute("SELECT * FROM papers").fetchall():
        raw = json.loads(row["raw_json"] or "{}")
        if raw.get("screening", {}).get("version") == 2:
            continue
        paper = Paper(title=row["title"], authors=json.loads(row["authors"] or "[]"),
                      source=row["source"], venue=row["venue"] or "", abstract=row["abstract"] or "",
                      code_url=row["code_url"] or "", raw=raw)
        classify_paper(paper)
        conn.execute(
            """UPDATE papers SET score = ?, recommendation = ?, worth_reading = ?, matched_keywords = ?,
            possible_innovation = ?, underwater_relation = ?, raw_json = ? WHERE id = ?""",
            (paper.score, paper.recommendation, int(paper.worth_reading),
             json.dumps(paper.matched_keywords, ensure_ascii=False), paper.possible_innovation,
             paper.underwater_relation, json.dumps(paper.raw, ensure_ascii=False, sort_keys=True), row["id"]),
        )
        changed += 1
    return changed


def fingerprint_for(paper: Paper) -> str:
    doi = normalize_doi(paper.doi)
    if doi:
        return f"doi:{doi}"
    normalized = normalize_title(paper.title)
    surname = first_author_surname(paper.authors)
    if len(normalized.split()) <= 3 and surname:
        return f"title-author:{normalized}:{surname}"
    return f"title:{normalized}"


def find_existing_paper(conn: sqlite3.Connection, paper: Paper) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT papers.* FROM papers JOIN paper_sources ON papers.id = paper_sources.paper_id "
        "WHERE paper_sources.source_key = ?", (source_key_for(paper),),
    ).fetchone()
    if row:
        return row
    if paper.source == "arXiv cs.CV":
        arxiv_id = canonical_arxiv_id(paper.external_id) or canonical_arxiv_id(paper.paper_url)
        if arxiv_id:
            row = conn.execute(
                "SELECT papers.* FROM papers JOIN paper_sources ON papers.id = paper_sources.paper_id "
                "WHERE paper_sources.source = ? AND "
                "(paper_sources.external_id = ? OR paper_sources.external_id GLOB ?)",
                (paper.source, arxiv_id, arxiv_id + "v[0-9]*"),
            ).fetchone()
            if row:
                return row
    doi = normalize_doi(paper.doi)
    if doi:
        row = conn.execute("SELECT * FROM papers WHERE doi = ?", (doi,)).fetchone()
        if row:
            return row

    normalized = normalize_title(paper.title)
    row = conn.execute("SELECT * FROM papers WHERE normalized_title = ?", (normalized,)).fetchone()
    if row:
        return row

    surname = first_author_surname(paper.authors)
    if surname:
        candidates = conn.execute(
            "SELECT * FROM papers WHERE first_author = ?",
            (surname,),
        ).fetchall()
        for candidate in candidates:
            if jaccard_similarity(candidate["title"], paper.title) >= 0.90:
                return candidate
    return None


def choose_text(current: str | None, incoming: str | None, prefer_longer: bool = True) -> str:
    current = current or ""
    incoming = incoming or ""
    if not current:
        return incoming
    if not incoming:
        return current
    if prefer_longer and len(incoming) > len(current):
        return incoming
    return current


def source_key_for(paper: Paper) -> str:
    stable = paper.external_id or paper.paper_url or fingerprint_for(paper)
    if paper.source == "arXiv cs.CV":
        stable = canonical_arxiv_id(stable) or stable
    elif paper.source == "OpenReview":
        stable = paper.raw.get("forum") or stable
    return f"{paper.source}:{stable}"


def upsert_paper(
    conn: sqlite3.Connection,
    paper: Paper,
    run_id: int,
    now_iso: str,
) -> tuple[int, bool]:
    existing = find_existing_paper(conn, paper)
    incoming = paper
    if existing is not None:
        prefer = SOURCE_PRIORITY.get(paper.source, 0) >= SOURCE_PRIORITY.get(existing["source"], 0)
        old_authors = json.loads(existing["authors"] or "[]")
        raw = json.loads(existing["raw_json"] or "{}")
        if prefer:
            raw.update(paper.raw)
        paper = Paper(
            title=paper.title if prefer else existing["title"],
            authors=paper.authors if (prefer and paper.authors) or len(paper.authors) > len(old_authors) else old_authors,
            source=paper.source if prefer else existing["source"],
            venue=(paper.venue if prefer and paper.venue else existing["venue"]) or paper.venue,
            paper_url=(paper.paper_url if prefer and paper.paper_url else existing["paper_url"]) or paper.paper_url,
            pdf_url=(paper.pdf_url if prefer and paper.pdf_url else existing["pdf_url"]) or paper.pdf_url,
            code_url=paper.code_url or existing["code_url"] or "",
            abstract=paper.abstract if prefer and paper.abstract else choose_text(existing["abstract"], paper.abstract),
            published_date=choose_text(existing["published_date"], paper.published_date, prefer_longer=False),
            updated_date=max(existing["updated_date"] or "", paper.updated_date),
            doi=normalize_doi(paper.doi) or existing["doi"] or "",
            external_id=paper.external_id if prefer else existing["external_id"],
            raw=raw,
        )
    # Recompute from merged metadata. Old, inflated scores must be allowed to fall.
    classify_paper(paper)
    values = {
        "title": paper.title,
        "normalized_title": normalize_title(paper.title),
        "authors": json.dumps(paper.authors, ensure_ascii=False),
        "first_author": first_author_surname(paper.authors),
        "source": paper.source, "venue": paper.venue, "paper_url": paper.paper_url,
        "pdf_url": paper.pdf_url, "code_url": paper.code_url, "abstract": paper.abstract,
        "possible_innovation": paper.possible_innovation, "underwater_relation": paper.underwater_relation,
        "worth_reading": int(paper.worth_reading), "recommendation": paper.recommendation,
        "score": paper.score, "matched_keywords": json.dumps(paper.matched_keywords, ensure_ascii=False),
        "published_date": paper.published_date, "updated_date": paper.updated_date,
        "doi": normalize_doi(paper.doi), "external_id": paper.external_id,
        "last_seen": now_iso, "raw_json": json.dumps(paper.raw, ensure_ascii=False, sort_keys=True),
    }
    if existing is None:
        values.update(fingerprint=fingerprint_for(paper), first_seen=now_iso)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        cursor = conn.execute(f"INSERT INTO papers ({columns}) VALUES ({placeholders})", tuple(values.values()))
        paper_id = int(cursor.lastrowid)
        is_new = True
    else:
        paper_id = int(existing["id"])
        assignments = ", ".join(f"{key} = ?" for key in values)
        conn.execute(f"UPDATE papers SET {assignments} WHERE id = ?", (*values.values(), paper_id))
        is_new = False

    paper = incoming  # Each source retains its own identity and links.

    conn.execute(
        """
        INSERT INTO paper_sources (
            paper_id, source, venue, external_id, paper_url, pdf_url,
            source_key, first_seen, last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            paper_id = excluded.paper_id,
            venue = excluded.venue,
            paper_url = excluded.paper_url,
            pdf_url = excluded.pdf_url,
            last_seen = excluded.last_seen
        """,
        (
            paper_id,
            paper.source,
            paper.venue,
            paper.external_id,
            paper.paper_url,
            paper.pdf_url,
            source_key_for(paper),
            now_iso,
            now_iso,
        ),
    )
    conn.execute(
        """
        INSERT INTO run_papers (run_id, paper_id, is_new)
        VALUES (?, ?, ?)
        ON CONFLICT(run_id, paper_id) DO UPDATE SET
            is_new = MAX(run_papers.is_new, excluded.is_new)
        """,
        (run_id, paper_id, int(is_new)),
    )
    return paper_id, is_new


def local_now(timezone_name: str) -> dt.datetime:
    if ZoneInfo is None:
        return dt.datetime.now(dt.timezone.utc)
    return dt.datetime.now(ZoneInfo(timezone_name))


def start_run(conn: sqlite3.Connection, run_date: str, sources: list[str], now_iso: str) -> int:
    cursor = conn.execute(
        "INSERT INTO runs (run_date, started_at, sources) VALUES (?, ?, ?)",
        (run_date, now_iso, ",".join(sources)),
    )
    return int(cursor.lastrowid)


def complete_run(
    conn: sqlite3.Connection,
    run_id: int,
    now_iso: str,
    report_path: str,
    errors: list[str],
) -> None:
    stats = conn.execute(
        "SELECT COUNT(*) AS total_seen, SUM(is_new) AS new_papers FROM run_papers WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    conn.execute(
        """
        UPDATE runs
        SET completed_at = ?, total_seen = ?, new_papers = ?, report_path = ?, errors = ?
        WHERE id = ?
        """,
        (
            now_iso,
            int(stats["total_seen"] or 0),
            int(stats["new_papers"] or 0),
            report_path,
            json.dumps(errors, ensure_ascii=False),
            run_id,
        ),
    )


def report_rows(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT papers.*, run_papers.is_new
        FROM papers
        JOIN run_papers ON papers.id = run_papers.paper_id
        WHERE run_papers.run_id = ?
        ORDER BY
            CASE papers.recommendation
                WHEN 'S' THEN 0
                WHEN 'A' THEN 1
                WHEN 'B' THEN 2
                ELSE 3
            END,
            papers.score DESC,
            papers.id DESC
        """,
        (run_id,),
    ).fetchall()


def md_link(url: str) -> str:
    return f"[link]({url})" if url else "无"


def truncate(text: str, limit: int = 900) -> str:
    text = normalize_whitespace(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def write_report(
    conn: sqlite3.Connection,
    run_id: int,
    report_date: str,
    reports_dir: Path,
    errors: list[str],
    source_status: list[dict[str, Any]] | None = None,
    new_only: bool = False,
    min_score: float = 12,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    rows = report_rows(conn, run_id)
    path = reports_dir / f"{report_date}.md"
    total = len(rows)
    new_count = sum(int(row["is_new"]) for row in rows)
    daily_rows = conn.execute(
        """SELECT papers.*, 1 AS is_new FROM papers
        WHERE substr(first_seen, 1, 10) = ? AND score >= ?
          AND id IN (SELECT paper_id FROM run_papers JOIN runs ON runs.id = run_papers.run_id WHERE runs.run_date = ?)
        ORDER BY score DESC, id DESC""", (report_date, min_score, report_date),
    ).fetchall()
    if new_only:
        rows = daily_rows
    by_rating: dict[str, int] = {rating: 0 for rating in ["S", "A", "B", "C"]}
    by_source: dict[str, int] = {}
    for row in rows:
        by_rating[row["recommendation"]] = by_rating.get(row["recommendation"], 0) + 1
        by_source[row["source"]] = by_source.get(row["source"], 0) + 1

    lines = [
        f"# 计算机视觉论文监控 - {report_date}",
        "",
        f"- 本次匹配论文数：{total}",
        f"- 新增论文数：{new_count}",
        f"- 今日新增论文数（含同日重跑）：{len(daily_rows)}",
        f"- 展示模式：{'仅今日新增' if new_only else '本次全部匹配'}；展示 {len(rows)} 篇",
        "- 推荐等级统计："
        + ", ".join(f"{rating}: {by_rating.get(rating, 0)}" for rating in ["S", "A", "B", "C"]),
        "- 来源统计："
        + (", ".join(f"{source}: {count}" for source, count in sorted(by_source.items())) or "无"),
        "",
        "> 推荐为标题/摘要规则筛选，不代表论文质量或已验证创新；方法线索需核对全文。",
        "",
    ]
    if source_status:
        lines.extend(["## 采集状态", "", "| 来源 | 状态 | 原始条目 | 通过筛选 | 耗时（秒） |",
                      "| --- | --- | ---: | ---: | ---: |"])
        labels = {"ok": "正常", "partial": "降级/部分成功", "failed": "失败"}
        for status in source_status:
            lines.append(f"| {status['source']} | {labels[status['status']]} | {status['raw_count']} | "
                         f"{status['kept_count']} | {status['duration_seconds']:.1f} |")
        lines.append("")
    if errors:
        lines.extend(["## 来源警告", ""])
        lines.extend(f"- {error}" for error in errors)
        lines.append("")
    if not rows:
        message = "今天没有新增匹配论文。已出现的论文仍保留在数据库中。" if new_only else "本次运行没有找到匹配论文。"
        lines.extend(["## 论文列表", "", message, ""])
    else:
        lines.extend(["## 论文列表", ""])
    for index, row in enumerate(rows, start=1):
        authors = ", ".join(json.loads(row["authors"] or "[]")) or "无"
        matched = ", ".join(json.loads(row["matched_keywords"] or "[]")) or "无"
        worth = "是" if row["worth_reading"] else "否"
        new_marker = "新增" if row["is_new"] else "已出现"
        raw = json.loads(row["raw_json"] or "{}")
        lines.extend(
            [
                f"### {index}. [{row['recommendation']}] {row['title']}",
                "",
                f"- 状态：{new_marker}",
                f"- 标题：{row['title']}",
                f"- 作者：{authors}",
                f"- 来源：{row['source']} ({row['venue'] or '无'})",
                f"- 论文链接：{md_link(row['paper_url'])}",
                f"- PDF 链接：{md_link(row['pdf_url'])}",
                f"- 代码链接：{md_link(row['code_url'])}",
                f"- 发表/更新时间：{row['published_date'] or '无'} / {row['updated_date'] or '无'}",
                *([f"- arXiv 公告日期：{raw['announcement_date']}（公告批次日期；非原始提交/更新时间）"]
                  if raw.get("announcement_date") else []),
                f"- 命中方向：{matched}",
                f"- 评分：{row['score']:.1f}",
                f"- 摘要：{truncate(row['abstract']) or '无'}",
                f"- 方法线索：{row['possible_innovation'] or '无'}",
                "- 评分依据：" + "、".join(
                    f"{key} {value:+g}" for key, value in
                    raw.get("screening", {}).get("score_breakdown", {}).items()
                ),
                f"- 与水下图像增强的关系：{row['underwater_relation'] or '无'}",
                f"- 是否值得精读：{worth}",
                f"- 推荐等级：{row['recommendation']}",
                "",
            ]
        )
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(path)
    exported = []
    for row in rows:
        item = dict(row)
        for key in ("authors", "matched_keywords"):
            item[key] = json.loads(item[key] or "[]")
        item["raw"] = json.loads(item.pop("raw_json") or "{}")
        exported.append(item)
    atomic_json(path.with_suffix(".json"), {"date": report_date, "run_id": run_id, "total_seen": total,
                "new_papers": new_count, "daily_new_papers": len(daily_rows), "new_only": new_only,
                "sources": source_status or [], "warnings": errors, "papers": exported})
    return path


def collect_sources(args: argparse.Namespace) -> tuple[list[Paper], list[str], list[dict[str, Any]]]:
    papers: list[Paper] = []
    errors: list[str] = []
    statuses: list[dict[str, Any]] = []
    for source in args.sources.split(","):
        warnings: list[str] = []
        started = time.monotonic()
        collected: list[Paper] = []
        try:
            if source == "arxiv":
                collected = collect_arxiv(args.max_per_source, args.arxiv_delay, warnings)
            elif source == "cvf":
                cache = DetailCache(Path(args.cache_dir) / "cvf-details.json", args.cvf_cache_hours)
                collected = collect_cvf(args.max_cvf_venues, args.max_cvf_detail_pages, cache, warnings)
            else:
                collected = collect_openreview(args.openreview_limit_per_term, args.request_delay, warnings)
            status = "partial" if warnings else "ok"
        except Exception as exc:  # noqa: BLE001
            record_warning(warnings, f"{source} failed: {exc}")
            status = "failed"
        kept = 0
        for paper in collected:
            classify_paper(paper)
            if paper.score > 0 and paper.score >= args.min_score:
                kept += 1
        papers.extend(collected)
        errors.extend(warnings)
        statuses.append({"source": source, "status": status, "raw_count": len(collected), "kept_count": kept,
                         "duration_seconds": round(time.monotonic() - started, 2), "warnings": warnings})
    return papers, errors, statuses


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect focused computer-vision papers.")
    parser.add_argument("--db", default="papers.db", help="SQLite database path.")
    parser.add_argument("--reports-dir", default="reports", help="Directory for Markdown reports.")
    parser.add_argument("--sources", default="arxiv,cvf,openreview", help="Comma-separated source list.")
    parser.add_argument("--max-per-source", type=int, default=80, help="Maximum arXiv records.")
    parser.add_argument("--openreview-limit-per-term", type=int, default=8)
    parser.add_argument("--max-cvf-venues", type=int, default=8)
    parser.add_argument("--max-cvf-detail-pages", type=int, default=90)
    parser.add_argument("--min-score", type=float, default=12.0)
    parser.add_argument("--arxiv-delay", type=float, default=3.2)
    parser.add_argument("--request-delay", type=float, default=0.8)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--report-date", default="")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--http-timeout", type=float, default=30, help="Timeout per HTTP attempt in seconds.")
    parser.add_argument("--http-retries", type=int, default=2, help="Retries after the initial HTTP attempt.")
    parser.add_argument("--cache-dir", default=".cache/paper-watch", help="CVF metadata cache and last-run diagnostics.")
    parser.add_argument("--cvf-cache-hours", type=float, default=168, help="CVF cache TTL; 0 disables cache reads.")
    parser.add_argument("--new-only", action="store_true", help="Show today's new papers, including earlier runs today.")
    args = parser.parse_args(argv)
    sources = list(dict.fromkeys(source.strip().lower() for source in args.sources.split(",") if source.strip()))
    if not sources or set(sources) - {"arxiv", "cvf", "openreview"}:
        parser.error("--sources must contain arxiv, cvf, and/or openreview")
    args.sources = ",".join(sources)
    for key in ("max_per_source", "openreview_limit_per_term", "max_cvf_venues", "max_cvf_detail_pages", "http_timeout"):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) <= 0:
            parser.error(f"--{key.replace('_', '-')} must be positive")
    for key in ("http_retries", "arxiv_delay", "request_delay", "cvf_cache_hours", "min_score"):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) < 0:
            parser.error(f"--{key.replace('_', '-')} must be finite and nonnegative")
    if args.max_per_source > 2000 or args.openreview_limit_per_term > 1000 or args.http_retries > 5:
        parser.error("Limits: max-per-source <= 2000, openreview-limit-per-term <= 1000, http-retries <= 5")
    if args.report_date:
        try:
            if dt.date.fromisoformat(args.report_date).isoformat() != args.report_date:
                raise ValueError
        except ValueError:
            parser.error("--report-date must be YYYY-MM-DD")
    try:
        local_now(args.timezone)
    except (KeyError, ValueError):
        parser.error("Unknown timezone; install requirements.txt for Windows timezone data")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    REQUEST_POLICY.timeout = args.http_timeout
    REQUEST_POLICY.retries = args.http_retries
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s: %(message)s",
    )
    now = local_now(args.timezone)
    run_date = args.report_date or now.date().isoformat()
    now_iso = now.isoformat(timespec="seconds")
    sources = [source.strip().lower() for source in args.sources.split(",") if source.strip()]

    conn = connect_db(Path(args.db))
    with conn:
        rescored = rescore_legacy_papers(conn)
        if rescored:
            logging.info("Updated screening for %d existing papers; metadata and history preserved", rescored)
        run_id = start_run(conn, run_date, sources, now_iso)

    try:
        papers, errors, statuses = collect_sources(args)
        all_failed = all(status["status"] == "failed" for status in statuses)
        atomic_json(Path(args.cache_dir) / "last-run.json", {"date": run_date, "sources": statuses,
                    "all_sources_failed": all_failed, "warnings": errors})
        if all_failed:
            with conn:
                complete_run(conn, run_id, local_now(args.timezone).isoformat(timespec="seconds"), "", errors)
            logging.error("All sources failed. Existing daily reports have been preserved.")
            return 1
        inserted = 0
        skipped = 0
        with conn:
            for paper in papers:
                classify_paper(paper)
                if paper.score <= 0 or paper.score < args.min_score:
                    skipped += 1
                    continue
                upsert_paper(conn, paper, run_id, now_iso)
                inserted += 1
            report_path = write_report(conn, run_id, run_date, Path(args.reports_dir), errors,
                                       statuses, args.new_only, args.min_score)
            complete_run(conn, run_id, local_now(args.timezone).isoformat(timespec="seconds"), str(report_path), errors)
        logging.info("Kept %d records, skipped %d off-topic/low-score records", inserted, skipped)
        logging.info("Report written to %s", report_path)
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            summary = report_path.read_text(encoding="utf-8").split("## 论文列表", 1)[0]
            with open(summary_path, "a", encoding="utf-8") as summary_file:
                summary_file.write(summary)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
