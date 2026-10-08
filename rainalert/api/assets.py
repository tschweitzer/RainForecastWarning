"""Content-versioned URLs for /static, and the cache headers that go with them (D-57).

`/static` used to be served with an ETag and a Last-Modified but no Cache-Control. That lets a
browser cache a file *heuristically*: fresh, without asking again, for a tenth of its age - so a
`radar.js` last edited ten days before a deploy could be reused for a day after it. That is what
happened to D-56: Opera, rarely used, fetched the new script and showed the slider bubble; Chrome,
Edge and Firefox on the same phone, and Chrome on a desktop, kept the old one and showed nothing.

Now every page refers to its scripts and styles as `/static/<name>?v=<content hash>`, through the
`static_url()` template global. A changed file is a new URL, so no cache anywhere can serve the
old one in its place; and because the URL names the content, it can be cached for a year without
being asked about again. Anything requested without the current version - the icons `sw.js` names,
a page from before a deploy that the browser kept - gets `no-cache` instead: reusable, but checked
against its ETag first, which costs a 304.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import parse_qs

from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"

#: path -> ((mtime_ns, size), version). Keyed on the file's stat rather than computed once: in a
#: deployed image the files never change, but locally they are edited under a running server, and
#: a version cached at startup would then name content the file no longer has - under a header
#: telling the browser never to ask again.
_versions: dict[str, tuple[tuple[int, int], str]] = {}


def version_of(path: Path) -> str:
    """The first 12 hex digits of the file's sha256."""
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _versions.get(str(path))
    if cached and cached[0] == key:
        return cached[1]
    version = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    _versions[str(path)] = (key, version)
    return version


def static_url(name: str) -> str:
    """The URL a template should use for `static/<name>`."""
    return f"/static/{name}?v={version_of(STATIC_DIR / name)}"


class VersionedStaticFiles(StaticFiles):
    """StaticFiles that says how long a response may be kept.

    Immutable only when the request names the file's *current* version. A stale `v` - a page
    rendered before a deploy - is answered with the new content, so it must not be marked as
    belonging to that old URL forever.
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        requested = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("v", [""])[0]
        # `full_path` is the file StaticFiles has already resolved inside its directory, so the
        # hash is of exactly what is being sent - not of a name re-parsed from the request.
        try:
            current = version_of(Path(full_path)) if requested else ""
        except OSError:
            current = ""
        response.headers["Cache-Control"] = (
            IMMUTABLE if requested and requested == current else REVALIDATE
        )
        return response
