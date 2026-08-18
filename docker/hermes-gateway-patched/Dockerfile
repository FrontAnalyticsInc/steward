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
# Source of the changes: altonalexander/hermes-agent, commits ef59f2810 and
# e029e36db on kanban-expose-idempotency-key (also pushed as
# worktree-chat-stream-live-errors and worktree-chat-approvals). Applied in
# that order onto the image's own file — see the re-derive note below.
# Pinned by digest, not `:latest`.
#
# `:latest` moved on 2026-08-18 and every fresh install broke on the checksum
# below — correctly, that is what it is for, but the failure lands on whoever
# is installing rather than on whoever changed the image. A machine with the
# old layer cached kept building fine, so the break was invisible until a cold
# build ran. Reproducibility here is not a nicety: this file's whole job is to
# overwrite ONE file in this image, and "this image" has to mean one thing.
#
# b85fada is the build whose api_server.py hashes to the ARG below. Moving one
# without the other cannot work, so they are changed together, and the re-derive
# note explains how.
FROM nousresearch/hermes-agent@sha256:b85fada52766a5893711cb32fe056f7e5b71f7b5431630c881d387dedbd8a60a

# The overwrite below is only correct while the image's file is the one this
# copy was derived from. Pin that: if the image changes api_server.py, the
# build fails here instead of silently reverting whatever that change carried
# — the failure mode a bare COPY hides for as long as nobody looks, and the
# one that actually fired the first time this was built.
#
# To re-derive: extract the new file from the image, apply
# `git show <commit> -- gateway/platforms/api_server.py` from
# hermes-agent-upstream onto it, replace api_server.py here with the result,
# and set this sum to the new file's (`docker run --rm --entrypoint sha256sum
# nousresearch/hermes-agent@<new-digest> /opt/hermes/gateway/platforms/api_server.py`),
# then update the FROM digest above to that same image.
ARG UPSTREAM_API_SERVER_SHA256=ecbc2aa1246b65fc7b77c1a3db8d3b6b0f9d0e052725edce7273a4988fb4af56
RUN printf '%s  %s\n' "${UPSTREAM_API_SERVER_SHA256}" \
        /opt/hermes/gateway/platforms/api_server.py \
    | sha256sum -c - \
    || { echo "ERROR: the published api_server.py changed. See the note above this line in the Dockerfile — re-derive docker/hermes-gateway-patched/api_server.py before rebuilding." >&2; exit 1; }

COPY api_server.py /opt/hermes/gateway/platforms/api_server.py

# Byte-compiled copies of the file this replaces would otherwise shadow it.
RUN find /opt/hermes/gateway/platforms -name 'api_server*.pyc' -delete \
    && python -c "import ast,sys; ast.parse(open('/opt/hermes/gateway/platforms/api_server.py').read())"
