#!/usr/bin/env python3
"""Daily computer-vision paper collector.

The collector is intentionally stdlib-first. OpenReview support uses the
official `openreview-py` client when it is installed, and is skipped with a
warning otherwise.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None


ARXIV_API_URL = "https://export.arxiv.org/api/query"
CVF_MENU_URL = "https://openaccess.thecvf.com/menu"
DEFAULT_TIMEZONE = "Asia/Hong_Kong"
DEFAULT_USER_AGENT = (
    "cv-paper-watch/1.0 "
    "(https://github.com/your-org/your-repo; contact: replace-me@example.com)"
)

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


def fetch_url(url: str, min_delay: float = 0.0, timeout: int = 40) -> str:
    RATE_LIMITER.wait(url, min_delay)
    user_agent = os.environ.get("PAPER_BOT_USER_AGENT", DEFAULT_USER_AGENT)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not fetch {url}: {exc.reason}") from exc


def build_arxiv_query() -> str:
    specific = " OR ".join(f"all:{term}" for term in ARXIV_SPECIFIC_TERMS)
    arch = " OR ".join(f"all:{term}" for term in ARXIV_ARCH_TERMS)
    application = " OR ".join(f"all:{term}" for term in ARXIV_APPLICATION_TERMS)
    return f"cat:cs.CV AND ({specific} OR (({arch}) AND ({application})))"


def collect_arxiv(max_results: int, arxiv_delay: float) -> list[Paper]:
    params = {
        "search_query": build_arxiv_query(),
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = ARXIV_API_URL + "?" + urllib.parse.urlencode(params)
    logging.info("Fetching arXiv cs.CV results")
    xml_text = fetch_url(url, min_delay=arxiv_delay)
    root = ET.fromstring(xml_text)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    papers: list[Paper] = []
    for entry in root.findall("atom:entry", ns):
        title = normalize_whitespace(entry.findtext("atom:title", default="", namespaces=ns))
        abstract = normalize_whitespace(entry.findtext("atom:summary", default="", namespaces=ns))
        authors = [
            normalize_whitespace(author.findtext("atom:name", default="", namespaces=ns))
            for author in entry.findall("atom:author", ns)
        ]
        authors = [author for author in authors if author]
        abs_url = normalize_whitespace(entry.findtext("atom:id", default="", namespaces=ns))
        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            rel = link.attrib.get("rel", "")
            title_attr = link.attrib.get("title", "")
            href = link.attrib.get("href", "")
            if title_attr == "pdf" or rel == "related":
                pdf_url = href
        doi = normalize_doi(entry.findtext("arxiv:doi", default="", namespaces=ns))
        if not doi:
            doi = extract_doi(abstract, abs_url)
        arxiv_id = abs_url.rstrip("/").rsplit("/", 1)[-1]
        published = entry.findtext("atom:published", default="", namespaces=ns)[:10]
        updated = entry.findtext("atom:updated", default="", namespaces=ns)[:10]
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
                raw={"arxiv_id": arxiv_id},
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
        if absolute in seen:
            continue
        seen.add(absolute)
        papers.append((absolute, label))
    return papers


def expand_cvf_listing_pages(url: str, max_pages: int = 10) -> list[str]:
    first_url = cvf_listing_url(url)
    pages = [first_url]
    markup = fetch_url(first_url, min_delay=0.5)
    if extract_cvf_paper_links(markup, first_url):
        return pages

    for href, label in parse_links(markup):
        if len(pages) >= max_pages:
            break
        if not href or label.lower() == "menu":
            continue
        absolute = urllib.parse.urljoin(first_url, href)
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


def collect_cvf(max_venues: int, max_detail_pages: int) -> list[Paper]:
    logging.info("Discovering recent CVF Open Access venues")
    papers: list[Paper] = []
    detail_count = 0
    for venue_url, venue in discover_cvf_venues(max_venues):
        logging.info("Scanning CVF venue: %s", venue)
        try:
            listing_pages = expand_cvf_listing_pages(venue_url)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Skipping CVF venue %s: %s", venue, exc)
            continue
        for listing_url in listing_pages:
            try:
                markup = fetch_url(listing_url, min_delay=0.5)
            except Exception as exc:  # noqa: BLE001
                logging.warning("Skipping CVF listing %s: %s", listing_url, exc)
                continue
            for paper_url, title in extract_cvf_paper_links(markup, listing_url):
                if detail_count >= max_detail_pages:
                    return papers
                if not looks_relevant_from_title(title):
                    continue
                try:
                    paper = parse_cvf_detail(paper_url, venue, title)
                except Exception as exc:  # noqa: BLE001
                    logging.warning("Could not parse CVF paper %s: %s", paper_url, exc)
                    continue
                detail_count += 1
                papers.append(paper)
    return papers


def unwrap_openreview_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def openreview_content(note: Any, *keys: str) -> Any:
    content = getattr(note, "content", {}) or {}
    for key in keys:
        if key in content:
            return unwrap_openreview_value(content[key])
    return ""


def openreview_date(note: Any) -> str:
    for attr in ("pdate", "tcdate", "tmdate", "cdate"):
        value = getattr(note, attr, None)
        if isinstance(value, (int, float)) and value > 0:
            return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).date().isoformat()
    return ""


def normalize_openreview_authors(value: Any) -> list[str]:
    value = unwrap_openreview_value(value)
    if isinstance(value, list):
        return [normalize_whitespace(str(item)) for item in value if normalize_whitespace(str(item))]
    if isinstance(value, str):
        return [normalize_whitespace(item) for item in value.split(",") if normalize_whitespace(item)]
    return []


def collect_openreview(limit_per_term: int, request_delay: float) -> list[Paper]:
    try:
        import openreview  # type: ignore
    except ImportError:
        logging.warning("OpenReview skipped because openreview-py is not installed")
        return []

    logging.info("Searching OpenReview")
    client = openreview.Client(baseurl=os.environ.get("OPENREVIEW_API_BASEURL", "https://api.openreview.net"))
    papers: list[Paper] = []
    seen: set[str] = set()
    for term in FOCUS_QUERIES:
        try:
            notes = client.search_notes(term=term, content="all", group="all", source="papers", limit=limit_per_term)
        except TypeError:
            notes = client.search_notes(term=term, limit=limit_per_term)
        except Exception as exc:  # noqa: BLE001
            logging.warning("OpenReview search failed for %r: %s", term, exc)
            continue
        time.sleep(request_delay)
        for note in notes:
            note_id = getattr(note, "id", "") or getattr(note, "forum", "")
            if not note_id or note_id in seen:
                continue
            seen.add(note_id)
            title = normalize_whitespace(str(openreview_content(note, "title")))
            if not title:
                continue
            abstract = normalize_whitespace(str(openreview_content(note, "abstract", "TLDR", "tldr")))
            authors = normalize_openreview_authors(openreview_content(note, "authors"))
            venue = normalize_whitespace(
                str(openreview_content(note, "venue", "venueid"))
                or str(getattr(note, "invitation", "")).split("/", 1)[0]
            )
            forum = getattr(note, "forum", "") or note_id
            paper_url = f"https://openreview.net/forum?id={forum}"
            pdf_url = f"https://openreview.net/pdf?id={note_id}"
            code_url = find_code_url(abstract, json.dumps(getattr(note, "content", {}), ensure_ascii=False))
            doi = extract_doi(abstract)
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
                    updated_date=openreview_date(note),
                    doi=doi,
                    external_id=note_id,
                    raw={"openreview_id": note_id, "forum": forum, "search_term": term},
                )
            )
    return papers


def matched_focus_terms(text: str) -> list[str]:
    lowered = text.lower()
    matches: list[str] = []
    term_groups = {
        "underwater image enhancement": ["underwater image enhancement", "underwater enhancement"],
        "underwater image restoration": ["underwater image restoration", "underwater restoration", "underwater dehazing"],
        "image restoration": ["image restoration", "image deblurring", "image denoising", "image deraining"],
        "physical prior": ["physical prior", "physics prior", "physics-based", "optical model", "scattering model"],
        "color correction": ["color correction", "color constancy", "white balance", "color cast"],
        "diffusion": ["diffusion model", "diffusion prior", "score-based", "generative prior"],
        "Mamba": ["mamba", "state space model", "state-space model", "selective scan"],
        "Transformer": ["transformer", "attention"],
        "low-light enhancement": ["low-light", "low light", "illumination enhancement"],
    }
    for label, needles in term_groups.items():
        if any(needle in lowered for needle in needles):
            matches.append(label)
    return matches


def classify_paper(paper: Paper) -> None:
    text = " ".join([paper.title, paper.abstract, paper.venue]).lower()
    matches = matched_focus_terms(text)
    score = 0.0
    if "underwater image enhancement" in matches:
        score += 35
    if "underwater image restoration" in matches:
        score += 35
    if "image restoration" in matches:
        score += 14
    if "physical prior" in matches:
        score += 16
    if "color correction" in matches:
        score += 12
    if "low-light enhancement" in matches:
        score += 12
    if "diffusion" in matches:
        score += 8
    if "Mamba" in matches:
        score += 8
    if "Transformer" in matches:
        score += 5
    if paper.code_url:
        score += 5
    venue_upper = (paper.venue or paper.source).upper()
    if any(top in venue_upper for top in ["CVPR", "ICCV", "ECCV", "NEURIPS", "ICLR"]):
        score += 10
    elif "WACV" in venue_upper:
        score += 6
    elif paper.source == "CVF Open Access":
        score += 8

    innovation_bits: list[str] = []
    if any("underwater" in match for match in matches):
        innovation_bits.append("直接针对水下退化或增强问题")
    if "physical prior" in matches:
        innovation_bits.append("引入物理/退化先验约束复原")
    if "color correction" in matches:
        innovation_bits.append("关注色偏、颜色恒常性或白平衡")
    if "low-light enhancement" in matches:
        innovation_bits.append("建模低照度、噪声与可见性提升")
    if "diffusion" in matches:
        innovation_bits.append("利用扩散模型或生成式先验处理逆问题/复原")
    if "Mamba" in matches:
        innovation_bits.append("使用 Mamba/状态空间模型进行全局上下文建模")
    if "Transformer" in matches:
        innovation_bits.append("使用 Transformer/注意力机制聚合长程上下文")
    if not innovation_bits:
        innovation_bits.append("可能提供通用图像复原或增强范式")

    direct_underwater = any("underwater" in match for match in matches)
    if direct_underwater:
        relation = "直接相关：论文研究水下图像、水下退化或水下增强。"
    elif any(match in matches for match in ["image restoration", "physical prior", "color correction", "low-light enhancement"]):
        relation = (
            "方法相关：复原、颜色、光照或物理先验思路可迁移到水下图像增强。"
        )
    elif any(match in matches for match in ["diffusion", "Mamba", "Transformer"]):
        relation = (
            "弱到中等相关：架构或生成式先验可能可复用于水下增强，但目标任务可能不同。"
        )
    else:
        relation = "基于当前元数据判断相关性较弱。"

    if score >= 60 or (direct_underwater and score >= 45):
        recommendation = "S"
    elif score >= 42:
        recommendation = "A"
    elif score >= 25:
        recommendation = "B"
    else:
        recommendation = "C"

    paper.matched_keywords = matches
    paper.score = score
    paper.possible_innovation = "；".join(innovation_bits) + "。"
    paper.underwater_relation = relation
    paper.recommendation = recommendation
    paper.worth_reading = recommendation in {"S", "A"} or direct_underwater


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
    conn.executescript(SCHEMA)
    return conn


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
    return f"{paper.source}:{stable}"


def upsert_paper(
    conn: sqlite3.Connection,
    paper: Paper,
    run_id: int,
    now_iso: str,
) -> tuple[int, bool]:
    existing = find_existing_paper(conn, paper)
    normalized = normalize_title(paper.title)
    authors_json = json.dumps(paper.authors, ensure_ascii=False)
    matched_json = json.dumps(paper.matched_keywords, ensure_ascii=False)
    raw_json = json.dumps(paper.raw, ensure_ascii=False, sort_keys=True)

    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO papers (
                title, normalized_title, authors, first_author, source, venue,
                paper_url, pdf_url, code_url, abstract, possible_innovation,
                underwater_relation, worth_reading, recommendation, score,
                matched_keywords, published_date, updated_date, doi, external_id,
                fingerprint, first_seen, last_seen, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper.title,
                normalized,
                authors_json,
                first_author_surname(paper.authors),
                paper.source,
                paper.venue,
                paper.paper_url,
                paper.pdf_url,
                paper.code_url,
                paper.abstract,
                paper.possible_innovation,
                paper.underwater_relation,
                int(paper.worth_reading),
                paper.recommendation,
                paper.score,
                matched_json,
                paper.published_date,
                paper.updated_date,
                normalize_doi(paper.doi),
                paper.external_id,
                fingerprint_for(paper),
                now_iso,
                now_iso,
                raw_json,
            ),
        )
        paper_id = int(cursor.lastrowid)
        is_new = True
    else:
        paper_id = int(existing["id"])
        existing_priority = SOURCE_PRIORITY.get(existing["source"], 0)
        incoming_priority = SOURCE_PRIORITY.get(paper.source, 0)
        prefer_incoming_source = incoming_priority > existing_priority
        conn.execute(
            """
            UPDATE papers
            SET title = ?,
                normalized_title = ?,
                authors = ?,
                first_author = ?,
                source = ?,
                venue = ?,
                paper_url = ?,
                pdf_url = ?,
                code_url = ?,
                abstract = ?,
                possible_innovation = ?,
                underwater_relation = ?,
                worth_reading = ?,
                recommendation = ?,
                score = ?,
                matched_keywords = ?,
                published_date = ?,
                updated_date = ?,
                doi = ?,
                external_id = ?,
                last_seen = ?,
                raw_json = ?
            WHERE id = ?
            """,
            (
                paper.title if prefer_incoming_source else existing["title"],
                normalize_title(paper.title if prefer_incoming_source else existing["title"]),
                authors_json if len(authors_json) > len(existing["authors"] or "") else existing["authors"],
                first_author_surname(paper.authors) or existing["first_author"],
                paper.source if prefer_incoming_source else existing["source"],
                paper.venue if prefer_incoming_source else choose_text(existing["venue"], paper.venue),
                paper.paper_url if prefer_incoming_source else choose_text(existing["paper_url"], paper.paper_url),
                choose_text(existing["pdf_url"], paper.pdf_url),
                choose_text(existing["code_url"], paper.code_url),
                choose_text(existing["abstract"], paper.abstract),
                paper.possible_innovation if paper.score >= (existing["score"] or 0) else existing["possible_innovation"],
                paper.underwater_relation if paper.score >= (existing["score"] or 0) else existing["underwater_relation"],
                int(paper.worth_reading) if paper.score >= (existing["score"] or 0) else existing["worth_reading"],
                paper.recommendation if paper.score >= (existing["score"] or 0) else existing["recommendation"],
                max(float(existing["score"] or 0), paper.score),
                matched_json if paper.score >= (existing["score"] or 0) else existing["matched_keywords"],
                choose_text(existing["published_date"], paper.published_date, prefer_longer=False),
                choose_text(existing["updated_date"], paper.updated_date, prefer_longer=False),
                normalize_doi(paper.doi) or existing["doi"],
                paper.external_id if prefer_incoming_source else existing["external_id"],
                now_iso,
                raw_json if prefer_incoming_source else existing["raw_json"],
                paper_id,
            ),
        )
        is_new = False

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
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    rows = report_rows(conn, run_id)
    path = reports_dir / f"{report_date}.md"
    total = len(rows)
    new_count = sum(int(row["is_new"]) for row in rows)
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
        "- 推荐等级统计："
        + ", ".join(f"{rating}: {by_rating.get(rating, 0)}" for rating in ["S", "A", "B", "C"]),
        "- 来源统计："
        + (", ".join(f"{source}: {count}" for source, count in sorted(by_source.items())) or "无"),
        "",
    ]
    if errors:
        lines.extend(["## 来源警告", ""])
        lines.extend(f"- {error}" for error in errors)
        lines.append("")
    if not rows:
        lines.extend(["## 论文列表", "", "本次运行没有找到匹配论文。", ""])
    else:
        lines.extend(["## 论文列表", ""])
    for index, row in enumerate(rows, start=1):
        authors = ", ".join(json.loads(row["authors"] or "[]")) or "无"
        matched = ", ".join(json.loads(row["matched_keywords"] or "[]")) or "无"
        worth = "是" if row["worth_reading"] else "否"
        new_marker = "新增" if row["is_new"] else "已出现"
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
                f"- 命中方向：{matched}",
                f"- 评分：{row['score']:.1f}",
                f"- 摘要：{truncate(row['abstract']) or '无'}",
                f"- 可能创新点：{row['possible_innovation'] or '无'}",
                f"- 与水下图像增强的关系：{row['underwater_relation'] or '无'}",
                f"- 是否值得精读：{worth}",
                f"- 推荐等级：{row['recommendation']}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def collect_sources(args: argparse.Namespace) -> tuple[list[Paper], list[str]]:
    papers: list[Paper] = []
    errors: list[str] = []
    sources = [source.strip().lower() for source in args.sources.split(",") if source.strip()]
    if "arxiv" in sources:
        try:
            papers.extend(collect_arxiv(args.max_per_source, args.arxiv_delay))
        except Exception as exc:  # noqa: BLE001
            message = f"arXiv failed: {exc}"
            logging.warning(message)
            errors.append(message)
    if "cvf" in sources:
        try:
            papers.extend(collect_cvf(args.max_cvf_venues, args.max_cvf_detail_pages))
        except Exception as exc:  # noqa: BLE001
            message = f"CVF Open Access failed: {exc}"
            logging.warning(message)
            errors.append(message)
    if "openreview" in sources:
        try:
            papers.extend(collect_openreview(args.openreview_limit_per_term, args.request_delay))
        except Exception as exc:  # noqa: BLE001
            message = f"OpenReview failed: {exc}"
            logging.warning(message)
            errors.append(message)
    return papers, errors


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
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
        run_id = start_run(conn, run_date, sources, now_iso)

    papers, errors = collect_sources(args)
    logging.info("Collected %d raw papers", len(papers))

    inserted = 0
    skipped = 0
    with conn:
        for paper in papers:
            classify_paper(paper)
            if paper.score < args.min_score:
                skipped += 1
                continue
            upsert_paper(conn, paper, run_id, now_iso)
            inserted += 1
        report_path = write_report(conn, run_id, run_date, Path(args.reports_dir), errors)
        complete_run(conn, run_id, local_now(args.timezone).isoformat(timespec="seconds"), str(report_path), errors)

    logging.info("Kept %d papers, skipped %d low-score papers", inserted, skipped)
    logging.info("Report written to %s", report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
