from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "ref_url",
    "fbclid", "gclid", "gclsrc", "dclid",
    "mc_cid", "mc_eid",
    "igshid", "si", "spm", "_hsenc", "_hsmi",
    "yclid", "_ga", "_gl"
}

def canonicalize_url(url: str) -> str:
    """Canonicalizes a URL by lowercasing host, removing default ports and tracking parameters."""
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
        (k, v) for k, v in query_pairs 
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
