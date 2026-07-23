from urllib.parse import urlsplit

from w3lib.url import canonicalize_url


def normalize_start_urls(urls):
    """Return unique canonical HTTP(S) URLs in input order."""
    normalized = []
    seen = set()
    for raw_url in urls:
        value = str(raw_url).strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                f"only absolute http/https URLs are allowed: {value}"
            )
        canonical = canonicalize_url(value, keep_fragments=False)
        if canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(canonical)
    return normalized
