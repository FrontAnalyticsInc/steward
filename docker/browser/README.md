# Renderer

Fetches a URL and returns fully-rendered HTML, so ingest workflows can read
sites that build their pages in JavaScript.

`POST /content?token=…` with `{"url": …, "gotoOptions": {"waitUntil": …,
"timeout": …}}`. That is browserless's contract, kept deliberately — it is what
`workflows/app/agents/kestrel_site_ingest/stages.py` calls, and keeping it meant
the swap off browserless changed nothing on the workflows side.

`GET /json/version?token=…` is the healthcheck, and also reports which
extensions loaded.

## Why this is hand-rolled

We need Chrome extensions. Nothing off the shelf does that:

| | Why not |
| --- | --- |
| browserless | Extensions are a paid feature; the OSS image does not have them. Also SSPL-licensed. |
| chrome-headless-shell | The *old* headless binary. Cannot load extensions at all. |
| `playwright run-server` | Extensions need `launch_persistent_context()`, which is a local launch — and `connect()` has no persistent contexts. |

Extensions require the process that owns the browser to own the launch, so that
process is `app.py`. Playwright is Apache 2.0.

## Adding an extension

Drop an **unpacked** extension — a directory with `manifest.json` at its top
level — into `${HERMES_DATA_DIR:-~/.hermes}/browser/extensions/`, then restart
the container. Extensions are read at browser launch, so a running container
will not pick up a new one.

Confirm it loaded:

```bash
curl -s "http://127.0.0.1:3010/json/version?token=$BROWSER_TOKEN"
# {"Browser":"playwright-chromium","extensions":["your-extension"]}
```

That only proves Chromium accepted it. To prove it actually *runs*, give it a
content script that leaves a mark and look for the mark in rendered output —
this is the probe used to verify the service originally:

```jsonc
// manifest.json
{
  "manifest_version": 3,
  "name": "probe",
  "version": "1.0",
  "content_scripts": [
    { "matches": ["<all_urls>"], "js": ["probe.js"], "run_at": "document_end" }
  ]
}
```

```js
// probe.js
document.documentElement.setAttribute("data-hermes-extension", "loaded");
```

```bash
curl -s -X POST "http://127.0.0.1:3010/content?token=$BROWSER_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com/"}' | grep -o 'data-hermes-extension="[^"]*"'
```

Remove the probe afterwards — a content script matching `<all_urls>` runs on
every page the crawler touches.

### Two things that will bite

**A packed `.crx` will not work.** Chrome removed command-line side-loading of
packed extensions. This is also why the image uses Playwright's bundled
Chromium rather than stock Chrome or Edge — those had the side-loading flags
removed entirely.

**The extension mount is read-only** on purpose. A service whose job is
"fetch any URL and run its JavaScript" should not be able to rewrite the code
it runs.

## Reading a site you are logged in to

`POST /session?token=…` with `{"cookies": [ … ]}` adds cookies to the persistent
profile, so an authenticated site can be read without this service ever seeing a
password. `GET /session?token=…` reports which cookie names and domains the
profile holds, never their values.

**It is disabled unless `ALLOW_SESSION_INJECTION=1`, and it must stay disabled on
the shared instance.** This service renders arbitrary third-party URLs, and
Chromium shares one cookie jar across the context. A session cookie in that
profile is reachable — and cookie-bearing — by every page the crawler is later
pointed at, and `li_at` is `SameSite=None`. Session cookies and arbitrary-URL
rendering must not share a profile.

Run a second instance instead, with its own profile, port and token. See
`browser-linkedin` in `docker-compose.yml`, and
`workflows/scripts/seed_linkedin_session.py` for seeding it.

`/content` also returns an `X-Final-Url` header with the post-redirect URL.
Callers reading an authenticated site need it: a login wall and a real page are
both HTTP 200 with plausible HTML, so without it a scrape of the login page
looks like a successful read of an empty account.

Note what an *expired* cookie looks like — not a login page, but a redirect loop
between the app and the login wall, surfacing as `ERR_TOO_MANY_REDIRECTS` from
`goto()` and so a 502 here, before `X-Final-Url` exists to check.

## The profile

`${HERMES_DATA_DIR:-~/.hermes}/browser/profile` persists between restarts, so a
logged-in session survives. That directory then holds live session cookies —
treat it as a credential store, not a cache.

Both mount directories must exist and be owned by the user running compose
**before first start**. Docker creates a missing bind source as root, and the
browser does not run as root, so it fails to write its profile with an error
that names a Chromium path rather than the permission.

```bash
mkdir -p ~/.hermes/browser/extensions ~/.hermes/browser/profile
```

## Versions move together

The base image tag pins Chromium; `playwright==` in the Dockerfile pins the
client. **They must match.** A client newer than its browsers looks for a
`chromium-<build>` directory that does not exist and fails at launch. Bump both
or neither.
