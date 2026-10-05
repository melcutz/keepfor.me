import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

URL_REGEX = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
DOMAIN_NO_SCHEME_REGEX = re.compile(
    r"^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s]*)?$", re.IGNORECASE
)


def _clean_trailing_punctuation(candidate: str) -> str:
    """Trim surrounding and trailing punctuation while preserving balanced
    brackets/parens."""
    candidate = candidate.strip()
    while candidate and candidate[-1] in ".,;:!?\"'<>[]{}":
        candidate = candidate[:-1]
    if candidate.endswith(")") and "(" not in candidate:
        candidate = candidate[:-1]
    return candidate.strip()


def _is_valid_http_url(url: str) -> bool:
    """Validate that the string parses cleanly into an http or https URL with netloc."""
    try:
        parsed = urlparse(url)
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def extract_url(text: str | None) -> str | None:
    """Extract and validate an HTTP/HTTPS URL from input text.

    Supports:
    - Pure URLs (web form inputs, clipboard links, API bodies)
    - URLs embedded inside app share text/notes (e.g. iOS share sheet text)
    - URLs without scheme (e.g. 'example.com/article' -> 'https://example.com/article')
    - Trailing punctuation trimming (e.g. 'Check https://example.com/article.' -> 'https://example.com/article')

    Returns the clean URL string, or None if no valid HTTP/HTTPS URL is present.
    """
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None

    # 1. Search for explicit http:// or https:// anywhere in the text
    match = URL_REGEX.search(raw)
    if match:
        candidate = _clean_trailing_punctuation(match.group(0))
        if _is_valid_http_url(candidate):
            return candidate

    # 2. Check if string is a domain/path without scheme (e.g. 'example.com/post')
    if "://" not in raw and " " not in raw and "\t" not in raw and "\n" not in raw:
        if DOMAIN_NO_SCHEME_REGEX.match(raw):
            candidate = _clean_trailing_punctuation("https://" + raw)
            if _is_valid_http_url(candidate):
                return candidate

    return None


TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "ref_src",
    "ref_url",
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "si",
    "spm",
    "_hsenc",
    "_hsmi",
    "yclid",
    "_ga",
    "_gl",
}


def canonicalize_url(url: str) -> str:
    """Canonicalize a URL, removing default ports and tracking parameters."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # Remove standard ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    # Filter query parameters
    query_pairs = parse_qsl(parsed.query, keep_blank_values=False)
    filtered_pairs = [
        (k, v)
        for k, v in query_pairs
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    # Sort query params for deterministic canonical URL
    filtered_pairs.sort(key=lambda x: x[0])
    clean_query = urlencode(filtered_pairs)

    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = "/"

    # Reconstruct without fragment
    return urlunparse((scheme, netloc, path, parsed.params, clean_query, ""))
