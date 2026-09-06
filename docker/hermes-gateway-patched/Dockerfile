# The gateway with one file replaced.
#
# `nousresearch/hermes-agent:latest` is not something this repo builds, so a
# change to gateway code has no build step to flow into. Rather than rebuild
# the whole 4GB image from source to carry one patch, derive from the image
# already on this host and overwrite the single module.
#
# Read "the image on this host" literally. That tag does not hold a pristine
# pull — it carries local work (the timezone unification, hence the
# `rollback-20260808-pre-tz` tag beside it), and api_server.py is one of the
# files that work touched. So the copy below is derived from *that* file, not
# from the matching commit in hermes-agent-upstream: deriving from the commit
# would have quietly reverted the timezone changes on deploy.
#
# What this carries. (1) and (2) are the two halves of making the chat panel
# tell the truth about a running turn; (3) is unrelated and was added later:
#
#  1. The chat-completions SSE channel emits a settled tool's output and its
#     real pass/fail verdict, plus reasoning as `hermes.reasoning.delta`.
#     Without it the panel can only draw a spinner turning into an
#     unconditional green check — the output and the fact that a tool failed
#     arrive when the finished turn's transcript is re-read, so an expanded
#     step sits empty and a failed tool looks fine for as long as the turn runs.
#
#  2. A streamed turn can ask for tool approval and be answered. Approvals only
#     ever worked on routes that registered a notifier, and the SSE routes
#     registered none — so every approval-gated tool call failed on this
#     surface no matter what the user would have said.
#
# The panel already understands these events; this is the half that puts them
# on the wire.
#
#  3. `send()` delivers, instead of returning the stub error "API server uses
#     HTTP request/response, not send()". That was true of a reply riding an
#     open request and false of everything else: a cron job finishing at 04:00
#     has no request to ride, so every dashboard-created automation — they all
#     stamp `platform: api_server` as origin — was structurally undeliverable
#     and reported its own delivery as failed. There is no socket to push down
#     here, but there is the transcript the panel renders from, so the brief is
#     appended to the target session's `messages` rows. For this platform the
#     session id IS the chat id (`chat_id`/`session_key` are NULL), which is
#     also why `gateway.mirror`'s origin-column lookup no-ops on this surface.
#     Verified 2026-08-13 end to end through the gateway's own in-process
#     scheduler. Local change — no upstream commit.
#
#  4. POST /api/jobs carries `enabled_toolsets`, `monitor_url` and
#     `enabled: false`. The console's automation library renders a template
#     into exactly `cron.jobs.create_job`'s kwargs and posts them here, and
#     this route read six of them — so a monitor template (the one whose whole
#     mechanism is "fetch this URL, hash it, and suppress the run when the
#     bytes are unchanged") was created as an ordinary job that re-sends the
#     same summary for ever, with nothing anywhere saying so. `enabled: false`
#     is in the same handler because `create_job` has no `enabled` argument:
#     doing it as a second HTTP call leaves the job armed in between.
#     `monitor_script` is deliberately NOT accepted — it names a file to
#     execute. Local change — no upstream commit.
#
# Source of the changes: altonalexander/hermes-agent, commits ef59f2810 and
# e029e36db on kanban-expose-idempotency-key (also pushed as
# worktree-chat-stream-live-errors and worktree-chat-approvals). Applied in
# that order onto the image's own file — see the re-derive note below.
# Pinned by digest, not `:latest`.
#
# Re-derived 2026-08-21 onto sha256:43aef7b3... (upstream's only change in
# this file: _REASONING_EFFORTS grew "max"/"ultra"). Three-way merge via
# `git merge-file`, no conflicts.
#
# Re-derived 2026-09-06 onto sha256:64923fae... (release v2026.8.31), across
# ~2100 changed lines. Two conflicts, resolved as described below. The reason
# to take this one rather than sit on a working pin: it carries a profile
# isolation fix — a non-multiplex gateway answered /p/<other-profile>/ out of
# its own config, serving this deployment's toolsets under another profile's
# URL (upstream's "#91583 defect 2"). Steward runs single-profile, which is
# exactly the shape that was affected.
#
# None of the local changes below were absorbed upstream; all four still
# apply, and every def they touch is still in this file.
#
# `:latest` moved on 2026-08-18 and every fresh install broke on the checksum
# below. The guard was right — the file it protects HAD changed — but the
# failure lands on whoever is installing rather than on whoever changed the
# image, and a machine with the old layer cached kept building green, so it was
# invisible until a cold build ran.
#
# Worse, the superseded manifest stopped being served: the image this patch was
# originally derived from is no longer pullable by any digest. Pinning is
# therefore not optional. A floating tag here means the one file this image
# exists to replace can be swapped underneath it, and the evidence needed to
# re-derive the patch can disappear at the same time.
#
# This digest and the ARG below are one fact in two places. Change them
# together or not at all.
#
# It must be the INDEX digest, never a per-platform one. `docker images
# --digests` and `docker inspect` report the index digest of what you pulled,
# which is the right value; `docker buildx imagetools inspect <image>` prints it
# as the top-level `Digest:` line, with the per-platform manifests listed
# underneath. Taking one of those instead still builds — on the machine that
# produced it — and makes the image unbuildable on every other architecture,
# with no error that says so: the pull simply cannot find a manifest for the
# platform. arm64 is a supported install target, so that would break every Mac.
FROM nousresearch/hermes-agent@sha256:64923faeae267792bf9bf87fe3b4c4869e35004e360c7df01730ad801b74d524

# The overwrite below is only correct while the image's file is the one this
# copy was derived from. Pin that: if the image changes api_server.py, the
# build fails here instead of silently reverting whatever that change carried
# — the failure mode a bare COPY hides for as long as nobody looks, and the
# one that actually fired the first time this was built.
#
# To re-derive, prefer a three-way merge over replaying commits — the local
# changes are not all commits (see the api_send note above, which has none):
#
#   docker run --rm --entrypoint cat <OLD-image> /opt/hermes/gateway/platforms/api_server.py > old_base.py
#   docker run --rm --entrypoint cat <NEW-image> /opt/hermes/gateway/platforms/api_server.py > new_base.py
#   git merge-file -p api_server.py old_base.py new_base.py > merged.py
#
# Do NOT resolve these with `--union`. That was the advice here until
# 2026-09-06, and on that re-derive it would have been wrong: upstream moved
# the whole /v1/runs* route block out of this file into
# platforms/api_server_runs.py (registered via `_api_runs._http_routes`), so
# the route-table conflict is "upstream deleted what we edited beside", not
# "both added". Keeping our side registers six routes twice. The correct
# resolution was upstream's side plus the ONE line that is genuinely ours:
#
#   ("POST", "/v1/approvals/{session_key}/decision", self._handle_session_approval),
#
# The other conflict, the capability manifest dict, IS a plain union.
#
# So: read both sides of every conflict against what upstream did structurally.
# Check afterwards that every def in BOTH parents survives and that nothing
# references a def upstream deleted — and when a def does vanish, confirm it
# vanished because upstream moved it and not because it carried one of our
# changes. (On 2026-09-06 eight nested helpers disappeared; all eight were
# upstream's own, byte-identical to the old base, and moved with the routes.)
# and set this sum to the new file's (`docker run --rm --entrypoint sha256sum
# nousresearch/hermes-agent@<new-digest> /opt/hermes/gateway/platforms/api_server.py`),
# then update the FROM digest above to that same image.
ARG UPSTREAM_API_SERVER_SHA256=6aec6687d47c81b3567e930882a499c5c5c041ecbe40e1d08f93038212401962
RUN printf '%s  %s\n' "${UPSTREAM_API_SERVER_SHA256}" \
        /opt/hermes/gateway/platforms/api_server.py \
    | sha256sum -c - \
    || { echo "ERROR: the published api_server.py changed. See the note above this line in the Dockerfile — re-derive docker/hermes-gateway-patched/api_server.py before rebuilding." >&2; exit 1; }

COPY api_server.py /opt/hermes/gateway/platforms/api_server.py

# Byte-compiled copies of the file this replaces would otherwise shadow it.
RUN find /opt/hermes/gateway/platforms -name 'api_server*.pyc' -delete \
    && python -c "import ast,sys; ast.parse(open('/opt/hermes/gateway/platforms/api_server.py').read())"

# The one web-search backend that needs no key.
#
# The registry in agent/web_search_registry.py offers eight providers. Seven —
# firecrawl, parallel, tavily, exa, xai, searxng, brave-free — want a paid plan
# or at least a signup, which means a client's first automation cannot search
# until someone has bought something. `ddgs` wants a pip install. It reports
# `available=False` today for no reason other than not being here:
# `_ddgs_package_importable()` in tools/web_tools.py is a bare `import ddgs`.
#
# The payoff is larger than one tool. `check_web_api_key()` is the `check_fn`
# for BOTH `web_search` and `web_extract`, and it is an OR across providers —
# so this single package puts `web_search` in the tool schema and un-gates
# `web_extract` for a browser-backed extract provider to fill.
#
# It has to be in the image. /opt/hermes is baked into the layer, not
# bind-mounted, so a `uv pip install` into a running gateway looks like it
# worked and is gone on the next `up --force-recreate`.
#
# Installed by name, not as an extra: unlike exa/firecrawl/parallel-web there
# is no `ddgs` entry in this image's `[project.optional-dependencies]`, so
# there is nothing to switch on. Pinned for that same reason — an undeclared
# dependency resolved at build time makes an unpinned build reproducible only
# by luck, and this image is the thing every fresh install receives.
#
# pip is not present; uv is, and the interpreter is the venv at /opt/hermes/.venv.
ARG DDGS_VERSION=9.14.4
RUN VIRTUAL_ENV=/opt/hermes/.venv uv pip install --no-cache "ddgs==${DDGS_VERSION}" \
    && python -c "import ddgs; print('ddgs', ddgs.__name__, 'importable')"
