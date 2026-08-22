#!/usr/bin/env python3
"""Contract tests for the Steward renderer extract provider.

Runnable outside the stack — the only import beyond the stdlib is `httpx`,
which the gateway image already carries::

    python3 hermes/plugins/web/test_steward_browser.py

There is no Hermes on the path here and no container: the provider is loaded
by file path, and the renderer is stood in for by a one-request HTTP server.
That covers everything that can be settled without the stack — the
availability contract, the capability flags, and the rule that a failure
surfaces as an error rather than as empty content. What it cannot cover is
the thing the renderer exists for: whether a JavaScript-rendered page comes
back with its text. That needs the real Chromium and is checked end to end.

Lives one level ABOVE the plugin directory so `hermes/seed.sh` (which copies
plugin directories) does not ship a test file into every deployment.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# No __pycache__ beside the plugin: seed.sh copies the directory verbatim onto
# every deployment, and a stale .pyc from a developer's Python version is not
# something to ship.
sys.dont_write_bytecode = True

_PROVIDER_PATH = Path(__file__).with_name("steward_browser") / "provider.py"
_spec = importlib.util.spec_from_file_location("steward_browser_provider", _PROVIDER_PATH)
assert _spec and _spec.loader
provider_mod = importlib.util.module_from_spec(_spec)
sys.modules["steward_browser_provider"] = provider_mod
_spec.loader.exec_module(provider_mod)

StewardBrowserWebProvider = provider_mod.StewardBrowserWebProvider


class _Env:
    """Set BROWSER_URL / BROWSER_TOKEN for the duration of a block."""

    def __init__(self, url: str, token: str) -> None:
        self.values = {"BROWSER_URL": url, "BROWSER_TOKEN": token}
        self.saved: dict[str, str | None] = {}

    def __enter__(self) -> None:
        import os

        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, *exc: object) -> None:
        import os

        for key, old in self.saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _run(coro):
    return asyncio.run(coro)


class Availability(unittest.TestCase):
    def test_available_with_url_and_token(self) -> None:
        with _Env("http://browser:3010", "secret"):
            self.assertTrue(StewardBrowserWebProvider().is_available())

    def test_unavailable_with_empty_url(self) -> None:
        # No exception either way — this runs on every `hermes tools` paint.
        with _Env("", "secret"):
            self.assertFalse(StewardBrowserWebProvider().is_available())

    def test_unavailable_with_empty_token(self) -> None:
        with _Env("http://browser:3010", ""):
            self.assertFalse(StewardBrowserWebProvider().is_available())

    def test_capabilities(self) -> None:
        provider = StewardBrowserWebProvider()
        self.assertTrue(provider.supports_extract())
        self.assertFalse(provider.supports_search())
        self.assertEqual(provider.name, "steward-browser")


class ErrorsAreLoud(unittest.TestCase):
    """A failure must never look like a page that happened to be empty."""

    def _assert_failed(self, result: dict, needle: str) -> None:
        self.assertTrue(result.get("error"), f"no error field in {result!r}")
        self.assertIn(needle, result["error"])
        self.assertEqual(result.get("content"), "")

    def test_missing_url_reports_error_not_empty_string(self) -> None:
        with _Env("", "secret"):
            results = _run(StewardBrowserWebProvider().extract(["https://example.com"]))
        self.assertEqual(len(results), 1)
        self._assert_failed(results[0], "BROWSER_URL is not set")

    def test_missing_token_reports_error(self) -> None:
        with _Env("http://browser:3010", ""):
            results = _run(StewardBrowserWebProvider().extract(["https://example.com"]))
        self._assert_failed(results[0], "BROWSER_TOKEN is not set")

    def test_unreachable_renderer_reports_error(self) -> None:
        # Port 1 on loopback: nothing listens, and the connection is refused
        # immediately rather than hanging.
        with _Env("http://127.0.0.1:1", "secret"):
            results = _run(StewardBrowserWebProvider().extract(["https://example.com"]))
        self._assert_failed(results[0], "unreachable")

    def test_renderer_401_names_the_credential(self) -> None:
        with _Serving(401, {"detail": "bad or missing token"}) as base:
            with _Env(base, "wrong"):
                results = _run(
                    StewardBrowserWebProvider().extract(["https://example.com"])
                )
        self._assert_failed(results[0], "BROWSER_TOKEN")

    def test_renderer_503_names_the_configuration(self) -> None:
        with _Serving(503, {"detail": "TOKEN is not configured"}) as base:
            with _Env(base, "secret"):
                results = _run(
                    StewardBrowserWebProvider().extract(["https://example.com"])
                )
        self._assert_failed(results[0], "not configured")

    def test_blank_page_is_an_error(self) -> None:
        with _Serving(200, "<html><body>   </body></html>") as base:
            with _Env(base, "secret"):
                results = _run(
                    StewardBrowserWebProvider().extract(["https://example.com"])
                )
        self._assert_failed(results[0], "no readable text")


class TextExtraction(unittest.TestCase):
    PAGE = (
        "<html><head><title> Quarterly  Report </title>"
        "<style>body{color:red}</style></head>"
        "<body><script>var x = 'invisible';</script>"
        "<h1>Revenue</h1><p>Up 12% year on year.</p>"
        "<p>Second paragraph.</p></body></html>"
    )

    def test_returns_rendered_text_and_title(self) -> None:
        with _Serving(200, self.PAGE, final_url="https://example.com/final") as base:
            with _Env(base, "secret"):
                results = _run(
                    StewardBrowserWebProvider().extract(["https://example.com"])
                )
        result = results[0]
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["title"], "Quarterly Report")
        self.assertIn("Revenue", result["content"])
        self.assertIn("Up 12% year on year.", result["content"])
        # Script and style text is never page content.
        self.assertNotIn("invisible", result["content"])
        self.assertNotIn("color:red", result["content"])
        # The post-redirect address is what gets reported back.
        self.assertEqual(result["url"], "https://example.com/final")

    def test_format_html_returns_markup(self) -> None:
        with _Serving(200, self.PAGE) as base:
            with _Env(base, "secret"):
                results = _run(
                    StewardBrowserWebProvider().extract(
                        ["https://example.com"], format="html"
                    )
                )
        self.assertIn("<h1>Revenue</h1>", results[0]["content"])

    def test_request_matches_the_content_contract(self) -> None:
        with _Serving(200, self.PAGE) as base:
            with _Env(base, "secret"):
                _run(StewardBrowserWebProvider().extract(["https://example.com"]))
            request = _Serving.last_request
        self.assertEqual(request["path"], "/content")
        self.assertEqual(request["token"], "secret")
        self.assertEqual(request["body"]["url"], "https://example.com")
        self.assertEqual(request["body"]["gotoOptions"]["waitUntil"], "networkidle")


class _Serving:
    """A stand-in renderer: one status code, one body, on a real socket."""

    last_request: dict = {}

    def __init__(self, status: int, body, final_url: str | None = None) -> None:
        self.status = status
        self.body = body
        self.final_url = final_url

    def __enter__(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                _Serving.last_request = {
                    "path": self.path,
                    "token": self.headers.get("x-token", ""),
                    "body": json.loads(raw or "{}"),
                }
                if isinstance(outer.body, str):
                    payload = outer.body.encode("utf-8")
                    content_type = "text/html; charset=utf-8"
                else:
                    payload = json.dumps(outer.body).encode("utf-8")
                    content_type = "application/json"
                self.send_response(outer.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                if outer.final_url:
                    self.send_header("X-Final-Url", outer.final_url)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
