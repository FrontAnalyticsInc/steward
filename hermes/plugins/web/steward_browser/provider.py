"""Extract page content through the Steward renderer.

The renderer (``docker/browser/app.py``) is a ~60-line FastAPI service with
one useful route::

    POST /content?token=…      body: {"url": …, "gotoOptions": {…}}
    -> 200 text/html            header: X-Final-Url: <post-redirect URL>

That is the whole contract, and it is deliberately the same one browserless
served. It is **not** a CDP endpoint: Hermes' built-in ``browser_*`` tools
speak CDP and cannot drive this service at all, which is why this is a web
provider rather than a ``browser.cdp_url`` setting.

Why it earns its place beside the paid extract backends: it runs a real
Chromium with the page's JavaScript. A plain fetch of a client-rendered page
returns an empty shell — 200 OK, plausible HTML, no content — and that is the
failure this exists to remove. Static pages were never the problem.

Config::

    web:
      extract_backend: steward-browser

    plugins:
      enabled:
        - web/steward_browser

Env (both supplied by the stack; see docker/docker-compose.yml)::

    BROWSER_URL=http://browser:3010
    BROWSER_TOKEN=…            # a real credential, not a formality

An empty ``BROWSER_URL`` means "rendering unavailable" throughout this repo
and keeps that meaning here: :meth:`is_available` returns False rather than
raising, and :meth:`extract` returns a per-URL error that names the missing
variable rather than an empty string. Silently returning nothing is the one
outcome a research workflow cannot tell apart from a page that really is
empty, so nothing in this file may produce it.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import os
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

try:
    from agent.web_search_provider import WebSearchProvider
except ImportError:  # pragma: no cover - standalone unit test, outside Hermes
    # test_steward_browser.py loads this module by path, with no Hermes on
    # sys.path, so that the availability and error contracts can be checked
    # without a container. The real base class is an ABC with exactly these
    # two abstract members.
    class WebSearchProvider:  # type: ignore[no-redef]
        pass

logger = logging.getLogger(__name__)

# The renderer's own default is 60s (docker/docker-compose.yml: BROWSER_TIMEOUT_MS).
# Ours is the outer bound: the HTTP call must outlive the navigation it asked
# for, or every slow page looks like a transport failure instead of a timeout.
_NAV_TIMEOUT_MS = 60_000
_HTTP_TIMEOUT_S = 75.0

# networkidle rather than load: a client-rendered page fires load with an empty
# body and fills it afterwards. Waiting for the network to settle is the whole
# reason this backend exists.
_WAIT_UNTIL = "networkidle"


def _env(name: str) -> str:
    """Read *name* through Hermes' config-aware env lookup, then os.environ.

    ``get_env_value`` also consults ``$HERMES_HOME/.env``, so a value set
    through Hermes' own config layer is visible to gateway sessions and
    subprocess agent runs that never had it exported. Falls back to
    ``os.getenv`` when that module is unavailable (standalone tests).
    """
    val: Optional[str] = None
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value(name)
    except Exception:  # noqa: BLE001 — config layer is optional here
        val = None
    if val is None:
        val = os.getenv(name, "")
    return (val or "").strip()


def _browser_url() -> str:
    return _env("BROWSER_URL").rstrip("/")


def _browser_token() -> str:
    return _env("BROWSER_TOKEN")


# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------
# Stdlib only, on purpose. The renderer is already 3.7 GB and the point of
# this plugin is that it adds no dependency and no account; pulling in a
# readability library to save forty lines would undo half of that.
#
# This is a boilerplate stripper, not an article extractor: it drops the tags
# whose text is never content (script, style, nav chrome) and keeps the rest
# with block structure intact. web_extract_tool truncates and stores the
# remainder itself, so over-keeping costs a truncation, while over-stripping
# loses the page.

_SKIP_CONTENT = {"script", "style", "noscript", "template", "svg", "canvas"}
_BLOCK = {
    "p", "div", "section", "article", "header", "footer", "aside", "main",
    "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table", "thead", "tbody",
    "blockquote", "pre", "ul", "ol", "dl", "dt", "dd", "form", "figure",
    "figcaption", "hr", "br", "nav",
}


class _TextExtractor(HTMLParser):
    """Collect visible text and the document title."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts: List[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_CONTENT:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT:
            # Clamped at zero: malformed markup with a stray closing tag must
            # not push this negative and start swallowing the real page.
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        # Collapse runs of spaces/tabs, then runs of blank lines, so the model
        # is not billed for a page's indentation.
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r" *\n *", "\n", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()


def _html_to_text(page_html: str) -> tuple[str, str]:
    """Return ``(text, title)`` for a rendered document.

    Never raises: a parser error on hostile markup must not take down the
    tool call, and the fallback (tags stripped by regex) is still far better
    than nothing.
    """
    parser = _TextExtractor()
    try:
        parser.feed(page_html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 — malformed markup is expected
        logger.debug("HTML parse fell back to regex stripping: %s", exc)
        stripped = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page_html)
        stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
        return re.sub(r"\s+", " ", _html.unescape(stripped)).strip(), ""
    return parser.text(), " ".join(parser.title.split())


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def _error(url: str, message: str) -> Dict[str, Any]:
    """A per-URL failure in the shape web_extract_tool expects.

    ``content`` is empty AND ``error`` is set — both, always. The tool
    surfaces the error to the model; a result carrying empty content with no
    error reads as "this page had nothing on it", which is the lie this
    provider exists to avoid.
    """
    return {"url": url, "title": "", "content": "", "raw_content": "", "error": message}


class StewardBrowserWebProvider(WebSearchProvider):
    """Extract via the Steward renderer's ``POST /content``."""

    @property
    def name(self) -> str:
        return "steward-browser"

    @property
    def display_name(self) -> str:
        return "Steward Renderer"

    def is_available(self) -> bool:
        """True only when both the address and the credential are present.

        The renderer answers 503 to every request when its own TOKEN is unset
        and 401 when the caller's does not match, so a URL without a token is
        not a usable backend — reporting it available would move a
        configuration mistake to call time, once per page, forever.

        Cheap and network-free by contract: this runs at tool-registration
        time and on every ``hermes tools`` paint.
        """
        return bool(_browser_url()) and bool(_browser_token())

    def supports_search(self) -> bool:
        # A renderer fetches a URL you already have. It cannot find one.
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Render each URL and return its text.

        Async because each page is a network round trip measured in seconds;
        the dispatcher awaits this directly. Pages are fetched one at a time —
        the renderer runs CONCURRENT=2 tabs for the whole stack and queues
        past that, so flooding it buys nothing and starves anything else
        using it.

        ``kwargs``: ``format="html"`` returns the rendered markup verbatim;
        anything else (including the default) returns extracted text.
        Unknown keys are ignored, per the ABC's forward-compat rule.
        """
        base = _browser_url()
        token = _browser_token()

        # Configuration failures are the same for every URL, so answer them
        # once per URL without touching the network. Note this is reachable
        # even though is_available() is False: web_extract_tool dispatches on
        # the configured backend NAME, not on availability.
        if not base:
            return [
                _error(
                    url,
                    "Page rendering is unavailable: BROWSER_URL is not set. "
                    "The renderer is the configured web.extract_backend, so "
                    "this page was not read. Set BROWSER_URL (and enable the "
                    "browser service) or change web.extract_backend.",
                )
                for url in urls
            ]
        if not token:
            return [
                _error(
                    url,
                    "Page rendering is unavailable: BROWSER_TOKEN is not set. "
                    "The renderer refuses every request without it.",
                )
                for url in urls
            ]

        import httpx

        results: List[Dict[str, Any]] = []
        want_html = str(kwargs.get("format") or "").lower() == "html"

        try:
            from tools.interrupt import is_interrupted
        except Exception:  # noqa: BLE001 — absent outside the agent process
            def is_interrupted() -> bool:  # type: ignore[misc]
                return False

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            for url in urls:
                if is_interrupted():
                    results.append(_error(url, "Interrupted"))
                    continue
                results.append(await self._one(client, base, token, url, want_html))
        return results

    async def _one(
        self,
        client: Any,
        base: str,
        token: str,
        url: str,
        want_html: bool,
    ) -> Dict[str, Any]:
        """Render one URL. Returns a result dict; never raises."""
        import httpx

        blocked = self._policy_block(url)
        if blocked is not None:
            return blocked

        try:
            # Token in a header, not the query string. The service accepts
            # both; a query parameter ends up in access logs and in any error
            # message that echoes the request URL.
            response = await client.post(
                f"{base}/content",
                headers={"x-token": token},
                json={
                    "url": url,
                    "gotoOptions": {
                        "waitUntil": _WAIT_UNTIL,
                        "timeout": _NAV_TIMEOUT_MS,
                    },
                },
            )
        except httpx.TimeoutException:
            return _error(
                url,
                f"Renderer timed out after {int(_HTTP_TIMEOUT_S)}s rendering "
                f"{url}. The page may be very large or never settle; try a "
                "more specific URL.",
            )
        except httpx.HTTPError as exc:
            # Unreachable service: wrong address, container not running, or
            # the browser compose profile switched off. Name the address, or
            # the operator has nothing to check.
            return _error(
                url,
                f"Renderer at {base} is unreachable ({type(exc).__name__}: {exc}). "
                "The browser service may not be running.",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad page, not a dead tool
            return _error(url, f"Renderer request failed: {exc}")

        if response.status_code != 200:
            return _error(url, self._http_error_message(base, response))

        page_html = response.text
        final_url = response.headers.get("X-Final-Url") or url

        # Re-check the POST-REDIRECT address. The pre-dispatch SSRF filter in
        # web_extract_tool only saw the URL the model asked for; a public URL
        # that 302s to 169.254.169.254 defeats it, and the renderer follows
        # redirects by design.
        unsafe = self._redirect_block(url, final_url)
        if unsafe is not None:
            return unsafe

        if want_html:
            content = page_html
            title = _html_to_text(page_html)[1]
        else:
            content, title = _html_to_text(page_html)

        if not content.strip():
            # A rendered page with no text is nearly always a wall — consent
            # gate, bot check, login — not an empty document. Say so instead
            # of returning "" and letting it read as an answer.
            return _error(
                url,
                f"Renderer returned a page with no readable text ({final_url}). "
                "This is usually a login wall, a consent gate or a bot check "
                "rather than an empty page.",
            )

        return {
            "url": final_url,
            "title": title,
            "content": content,
            "raw_content": content,
            "metadata": {
                "sourceURL": final_url,
                "requestedURL": url,
                "renderer": "steward-browser",
            },
        }

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _http_error_message(base: str, response: Any) -> str:
        """Turn the renderer's status code into something actionable."""
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("detail") or "")
        except Exception:  # noqa: BLE001 — non-JSON error body
            detail = (response.text or "")[:200]

        code = response.status_code
        if code == 401:
            return (
                f"Renderer at {base} rejected the credential (401). "
                "BROWSER_TOKEN does not match the renderer's TOKEN."
            )
        if code == 503:
            return (
                f"Renderer at {base} is not configured (503): {detail or 'no TOKEN set'}. "
                "It refuses to serve until BROWSER_TOKEN is set on both sides."
            )
        if code == 502:
            return f"Renderer could not load the page (502): {detail}"
        return f"Renderer returned HTTP {code}: {detail}"

    @staticmethod
    def _policy_block(url: str) -> Optional[Dict[str, Any]]:
        """Apply the operator's website-access policy, as firecrawl does.

        Imported lazily and non-fatally: outside the agent process (the
        standalone unit test) there is no policy module, and there is also
        nothing to protect.
        """
        try:
            from tools.website_policy import check_website_access
        except Exception:  # noqa: BLE001
            return None
        blocked = check_website_access(url)
        if not blocked:
            return None
        logger.info(
            "Blocked web_extract for %s by rule %s", blocked["host"], blocked["rule"]
        )
        result = _error(url, blocked["message"])
        result["blocked_by_policy"] = {
            "host": blocked["host"],
            "rule": blocked["rule"],
            "source": blocked["source"],
        }
        return result

    @staticmethod
    def _redirect_block(url: str, final_url: str) -> Optional[Dict[str, Any]]:
        """Block a redirect that landed somewhere private."""
        if final_url == url:
            return None
        try:
            from tools.url_safety import is_safe_url
        except Exception:  # noqa: BLE001
            return None
        if is_safe_url(final_url):
            return None
        logger.info("Blocked redirected web_extract to unsafe URL: %s", final_url)
        return _error(
            final_url,
            "Blocked: URL redirected to a private or internal network address",
        )

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Steward Renderer",
            "badge": "free · bundled",
            "tag": (
                "Extract only. Renders JavaScript in the browser service this "
                "stack already runs — no account, no API key."
            ),
            "env_vars": [
                {
                    "key": "BROWSER_URL",
                    "prompt": "Renderer address (http://browser:3010 inside the stack)",
                    "url": "",
                },
                {
                    "key": "BROWSER_TOKEN",
                    "prompt": "Renderer token (must match the browser service's TOKEN)",
                    "url": "",
                },
            ],
        }
