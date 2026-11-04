"""Fetch documentation from URLs found in external rule issue messages."""

import ipaddress
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"https?://\S+")
_TRAILING_PUNCTUATION = frozenset(".,);:'\"")
_MAX_DOC_BYTES = 50_000

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("fc00::/7"),  # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
]


def _is_safe_url(url: str) -> bool:
    """Return False if the URL targets a private or loopback address."""
    try:
        host = urlparse(url).hostname or ""
        if host in _LOOPBACK_HOSTNAMES:
            return False
        addr = ipaddress.ip_address(host)
        return not any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return True  # hostname (not a bare IP) — allow through


_BLOB_PREFIX = "https://github.com/jpablo/vibe-types/blob/main/"
_RAW_PREFIXES = (
    "https://raw.githubusercontent.com/jpablo/vibe-types/refs/heads/main/",
    "https://raw.githubusercontent.com/jpablo/vibe-types/main/",
)
_SHA_PINNED_RAW_RE = re.compile(r"^https://raw\.githubusercontent\.com/jpablo/vibe-types/([0-9a-fA-F]{7,40})/(.+)$")


def _extract_rel_path(url: str) -> str | None:
    """Strip a vibe-types GitHub prefix from url, returning the relative path portion or None."""
    if url.startswith(_BLOB_PREFIX):
        return url[len(_BLOB_PREFIX) :]
    for prefix in _RAW_PREFIXES:
        if url.startswith(prefix):
            return url[len(prefix) :]
    return None


def _resolve_candidate(rel_path: Path) -> Path | None:
    """Resolve rel_path against the vendor/vibe-types submodule, or None if not safe."""
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            submodule_root = (parent / "vendor" / "vibe-types").resolve()
            candidate = (submodule_root / rel_path).resolve()
            try:
                candidate.relative_to(submodule_root)
            except ValueError:
                return None
            return candidate
    return None


def _vibe_types_local_path(url: str) -> Path | None:
    """Map a vibe-types GitHub URL to its local submodule path, or None if not a vibe-types URL."""
    rel = _extract_rel_path(url)
    if rel is None:
        return None
    rel = rel.split("?")[0].split("#")[0]
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return None
    return _resolve_candidate(rel_path)


def extract_urls(text: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from a string, stripping trailing punctuation."""
    return [url.rstrip("".join(_TRAILING_PUNCTUATION)) for url in _URL_PATTERN.findall(text)]


async def _stream_capped(url: str, client: httpx.AsyncClient) -> str | None:
    """Fetch up to _MAX_DOC_BYTES from url using an existing client."""
    logger.debug("Fetching external rule doc from %s", url)
    chunks: list[bytes] = []
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            total = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= _MAX_DOC_BYTES:
                    break
    except Exception as e:
        logger.debug("Failed to fetch %s: %s", url, e)
        return None
    return b"".join(chunks)[:_MAX_DOC_BYTES].decode("utf-8", errors="replace")


async def _fetch_one(url: str) -> str | None:
    """Fetch a single URL's content via the local submodule fast path or HTTP, no fallback."""
    local = _vibe_types_local_path(url)
    if local is not None:
        if local.exists():
            logger.debug("Reading vibe-types doc locally from %s", local)
            try:
                return local.read_bytes()[:_MAX_DOC_BYTES].decode("utf-8", errors="replace")
            except OSError as e:
                logger.debug("Failed to read local vibe-types file %s: %s", local, e)
        else:
            logger.debug("Local vibe-types path not found: %s (submodule not initialized?)", local)

    # blob URLs return HTML over HTTP — always convert to raw before fetching
    fetch_url = url
    if fetch_url.startswith(_BLOB_PREFIX):
        fetch_url = _RAW_PREFIXES[0] + fetch_url[len(_BLOB_PREFIX) :]

    if not _is_safe_url(fetch_url):
        logger.debug("Blocked SSRF-risk URL: %s", fetch_url)
        return None

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        return await _stream_capped(fetch_url, client)


async def fetch_url_content(url: str) -> str | None:
    """Fetch the text content of a URL, returning None on any error or SSRF-blocked host.

    SHA-pinned vibe-types raw URLs fall back to the "main" branch form if the pinned fetch fails.
    """
    candidates = [url]
    sha_match = _SHA_PINNED_RAW_RE.match(url)
    if sha_match:
        candidates.append(_RAW_PREFIXES[1] + sha_match.group(2))

    for candidate in candidates:
        content = await _fetch_one(candidate)
        if content is not None:
            return content

    return None


async def _fetch_docs_for_urls(urls: list[str]) -> list[str]:
    """Fetch each url's content, collecting non-None results in order."""
    docs = []
    for url in urls:
        content = await fetch_url_content(url)
        if content is not None:
            docs.append(content)
    return docs


async def fetch_external_rule_docs(message: str, *, exclude_vibe_types: bool = False) -> list[str]:
    """Extract URLs from an issue message and return fetched content for each.

    When exclude_vibe_types is True, vibe-types knowledge-file URLs are skipped, since the
    caller already fetched them separately via fetch_vibe_types_knowledge_docs.
    """
    urls = extract_urls(message)
    if exclude_vibe_types:
        urls = [u for u in urls if not is_vibe_types_doc_url(u)]
    return await _fetch_docs_for_urls(urls)


def is_vibe_types_doc_url(url: str) -> bool:
    """Return True if url points at a vibe-types knowledge file (blob/main/SHA-pinned raw form)."""
    return _extract_rel_path(url) is not None or bool(_SHA_PINNED_RAW_RE.match(url))


async def fetch_vibe_types_knowledge_docs(message: str) -> list[str]:
    """Extract vibe-types knowledge-file URLs from an issue message and return fetched content for each."""
    urls = list(dict.fromkeys(u for u in extract_urls(message) if is_vibe_types_doc_url(u)))
    return await _fetch_docs_for_urls(urls)
