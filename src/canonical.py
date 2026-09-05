"""Canonical URL form and the record id derived from it.

The old Nextcloud pipeline keyed its seen-state on raw feed URLs, tracking
parameters and all, so the same recipe arriving as
``...?adt_ei=*|EMAIL|*`` looked new every time. Everything here exists to make
one recipe map to exactly one key.

The host is lowercased but otherwise left alone: ``sources.json`` lists some
sites with ``www.`` and some without, and that is the form their APIs return.
"""

import hashlib
from urllib.parse import urlsplit, urlunsplit


def canonicalize(url: str) -> str:
    """Scheme forced to https, host lowercased, query and fragment dropped.

    The path is left exactly as the site gave it, trailing slash included,
    because some sites 301 between the two forms and we want to store whichever
    one their API reports.
    """
    if not url or not url.strip():
        raise ValueError("empty url")

    parts = urlsplit(url.strip())
    if not parts.netloc:
        raise ValueError(f"no host in url: {url!r}")

    return urlunsplit(("https", parts.netloc.lower(), parts.path, "", ""))


def record_id(url: str) -> str:
    """Stable staging filename stem for a URL: sha1 of its canonical form."""
    return hashlib.sha1(canonicalize(url).encode("utf-8")).hexdigest()
