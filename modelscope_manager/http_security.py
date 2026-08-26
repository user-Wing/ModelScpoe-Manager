from __future__ import annotations

from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_OFFICIAL_HOST_SUFFIXES = ("modelscope.cn", "modelscope.ai")


def is_modelscope_url(url: str) -> bool:
    """Return whether *url* belongs to an official ModelScope host."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in _OFFICIAL_HOST_SUFFIXES)


def modelscope_token_headers(url: str, token: str, *, include_session_cookie: bool = False) -> dict[str, str]:
    """Scope long-lived account credentials to ModelScope-owned hosts only.

    Signed object-storage redirects do not need the account token.  Callers may
    still follow those redirects, but the redirect handler below removes any
    credential headers before crossing the origin boundary.
    """
    token = str(token or "").strip()
    if not token or not is_modelscope_url(url):
        return {}
    headers = {"Authorization": f"Bearer {token}"}
    if include_session_cookie:
        headers["Cookie"] = f"m_session_id={token}"
    return headers


class SafeRedirectHandler(HTTPRedirectHandler):
    """Follow redirects without forwarding credentials to another origin."""

    def redirect_request(self, request, fp, code, msg, headers, new_url):
        redirected = super().redirect_request(request, fp, code, msg, headers, new_url)
        if redirected is None:
            return None
        old = urlsplit(request.full_url)
        new = urlsplit(new_url)
        old_origin = (old.scheme.lower(), (old.hostname or "").lower(), old.port)
        new_origin = (new.scheme.lower(), (new.hostname or "").lower(), new.port)
        if old_origin != new_origin:
            for name in ("Authorization", "Proxy-Authorization", "Cookie"):
                redirected.remove_header(name)
        return redirected


_SAFE_OPENER = build_opener(SafeRedirectHandler())


def safe_urlopen(request: Request, *, timeout: float = 30):
    """Open a urllib request with cross-origin credential stripping."""
    return _SAFE_OPENER.open(request, timeout=timeout)
