import logging
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup
import trafilatura

logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    title: Optional[str] = None
    description: Optional[str] = None
    keywords: Optional[str] = None
    canonical_url: Optional[str] = None
    language: Optional[str] = None
    og_tags: dict[str, str] = field(default_factory=dict)
    body: str = ""
    raw_html: str = ""
    parser_used: str = ""  # Which parser extracted the body


def extract_metadata(html: str, parser_mode: str = "auto") -> ParseResult:
    """Extract metadata and body text from HTML.

    Args:
        html: Raw HTML string.
        parser_mode: Which parser to use for body extraction.
            - "auto": Try trafilatura first, fall back to bs4 on failure.
            - "trafilatura": Use trafilatura only.
            - "bs4": Use BeautifulSoup only.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    result = ParseResult(raw_html=html)

    # --- Metadata extraction (always uses BS4) ---

    # Title
    title_tag = soup.find("title")
    if title_tag and title_tag.string:
        result.title = title_tag.string.strip()

    # Meta description
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        result.description = meta_desc["content"].strip()

    # Meta keywords
    meta_kw = soup.find("meta", attrs={"name": "keywords"})
    if meta_kw and meta_kw.get("content"):
        result.keywords = meta_kw["content"].strip()

    # Canonical URL
    canonical = soup.find("link", attrs={"rel": "canonical"})
    if canonical and canonical.get("href"):
        result.canonical_url = canonical["href"].strip()

    # Language
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        result.language = html_tag["lang"].strip()

    # OG tags
    for og_tag in soup.find_all("meta", attrs={"property": True}):
        prop = og_tag.get("property", "")
        if prop.startswith("og:") and og_tag.get("content"):
            result.og_tags[prop] = og_tag["content"].strip()

    # --- Body text extraction ---

    if parser_mode == "trafilatura":
        body = _trafilatura_extract(html)
        result.parser_used = "trafilatura"
    elif parser_mode == "bs4":
        body = _bs4_body_extraction(soup)
        result.parser_used = "bs4"
    else:
        # auto: trafilatura first, bs4 fallback
        body = _trafilatura_extract(html)
        if body:
            result.parser_used = "trafilatura"
        else:
            body = _bs4_body_extraction(soup)
            result.parser_used = "bs4"

    result.body = body or ""

    return result


def _trafilatura_extract(html: str) -> Optional[str]:
    """Extract body text using trafilatura."""
    try:
        return trafilatura.extract(html)
    except Exception as e:
        logger.warning(f"Trafilatura extraction failed: {e}")
        return None


def _bs4_body_extraction(soup: BeautifulSoup) -> str:
    """Extract body text using BeautifulSoup with boilerplate removal."""
    # Work on a copy to avoid mutating the original soup
    soup_copy = BeautifulSoup(str(soup), "lxml")

    # Remove unwanted elements
    for tag in soup_copy.find_all(
        ["script", "style", "nav", "footer", "aside", "header", "noscript"]
    ):
        tag.decompose()

    # Get text
    text = soup_copy.get_text(separator=" ", strip=True)

    # Normalize whitespace
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        stripped = " ".join(line.split())
        if stripped:
            cleaned.append(stripped)

    return "\n".join(cleaned)
