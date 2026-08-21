#!/usr/bin/env bash
# hermes-update — move a Steward install to a newer release.
#
#     hermes-update --dry-run          what would happen, and nothing else
#     hermes-update                    upgrade to the version this script pins
#     hermes-update --to v0.4.0        upgrade to a specific tag
#
# Installed at ${STEWARD_HOME}/hermes-update by install.sh, and published
# alongside it on every release. Runs on the HOST, because the things it does —
# compose down, pull, compose up, snapshot the data disk — are all outside any
# container.
#
# The ordering is the whole design:
#
#   snapshot -> down -> pull -> migrate -> up -> health -> THEN write the marker
#
# The marker is the record of what this deployment has reached, and it is
# written last, only after the new stack answers. An upgrade that dies anywhere
# before that leaves a marker saying the OLD version — so the retry re-runs the
# same pending migrations from the same starting point. That is why migrations
# are required to be idempotent: it makes a half-finished upgrade a repeat
# rather than an investigation.
#
# Forward-only. There is no downgrade path and there should not be one: a
# migration that has restructured data cannot generally be un-run, and a
# --downgrade flag that silently does not restore your data is worse than not
# having one. Rolling back a FAILED upgrade is different, and is what the
# snapshot is for.
set -euo pipefail

# Same platform split as install.sh, and it has to be the same or an upgrade
# looks for the install somewhere it was never put. /srv does not exist on macOS
# and cannot be created there; installs land under $HOME instead.
case "$(uname -s)" in
    Darwin) STEWARD_HOME="${STEWARD_HOME:-$HOME/steward}" ;;
    *)      STEWARD_HOME="${STEWARD_HOME:-/srv/steward}" ;;
esac
STACK_DIR="$STEWARD_HOME/stack"
# Same split install.sh writes: CONFIG_FILE (0644) is non-secret defaults and
# feature toggles, ENV_FILE (0600) is credentials only. Both are passed to
# `docker compose --env-file` together everywhere below.
CONFIG_FILE="$STACK_DIR/config.env"
ENV_FILE="$STACK_DIR/.env"
STACK_FILE="$STACK_DIR/steward-stack.yml"
SRC_DIR="$STEWARD_HOME/src"
SNAPSHOT_DIR="$STEWARD_HOME/snapshots"

# The release this copy of the script shipped with — NOT a default upgrade
# target. It cannot be one: this file is published as a release asset and
# installed from it, so the copy on a v0.1.0 box has this pinned to v0.1.0
# forever, and nothing in a running deployment ever learns that a later tag
# exists. Treating it as the target made a bare `hermes-update` re-pull the
# version already installed and report success, which is why --to is required
# to move anywhere. Used only to recognise "you asked for the version you are
# already on" below.
SHIPPED_WITH="${STEWARD_VERSION:-v0.1.3}"
TARGET="$SHIPPED_WITH"
TARGET_EXPLICIT=0
DRY_RUN=0
KEEP_SNAPSHOTS="${STEWARD_KEEP_SNAPSHOTS:-3}"

while [ $# -gt 0 ]; do
    case "$1" in
        --to)      TARGET="${2:-}"; TARGET_EXPLICIT=1; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --home)    STEWARD_HOME="${2:-}"; shift 2
                   STACK_DIR="$STEWARD_HOME/stack"
                   CONFIG_FILE="$STACK_DIR/config.env"
                   ENV_FILE="$STACK_DIR/.env"
                   STACK_FILE="$STACK_DIR/steward-stack.yml"
                   SRC_DIR="$STEWARD_HOME/src"
                   SNAPSHOT_DIR="$STEWARD_HOME/snapshots" ;;
        -h|--help)
            cat <<'USAGE'
usage: hermes-update --to TAG [--dry-run] [--home PATH]

  --to TAG     release to move to. REQUIRED — this script has no way to learn
               which releases exist, so there is no sensible default. Releases
               are listed at
               https://github.com/FrontAnalyticsInc/steward/releases
  --dry-run    print the plan and exit without changing anything
  --home PATH  install location (default: /srv/steward, or ~/steward on macOS)

Passing --to with the version already installed is allowed on purpose: it
re-runs pending migrations, which is how an upgrade that failed at the health
check is finished.

Environment: STEWARD_KEEP_SNAPSHOTS (default 3) — how many to retain.
USAGE
            exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -t 2 ] && command -v tput >/dev/null 2>&1 && [ -n "${TERM:-}" ]; then
    B="$(tput bold 2>/dev/null || true)"; R="$(tput sgr0 2>/dev/null || true)"
else
    B=""; R=""
fi
say()  { printf '%s\n' "$*" >&2; }
step() { printf '\n%s==>%s %s\n' "$B" "$R" "$*" >&2; }
warn() { printf '  !  %s\n' "$*" >&2; }
die()  { printf '\n%serror:%s %s\n' "$B" "$R" "$*" >&2; exit 1; }

[ -f "$STACK_FILE" ] || die "no stack at $STACK_FILE. Is STEWARD_HOME right?"
[ -f "$ENV_FILE" ]   || die "no env file at $ENV_FILE."

# config.env is new: installs from before this split have every variable in
# .env, credentials included in the same file as everything else. Rather than
# leave one of those broken on the first `--env-file "$CONFIG_FILE"` below —
# which would point at a file that has never existed — split it here, once.
# The known non-secret names move to a fresh config.env; anything else stays in
# .env untouched, including a variable an operator added by hand that this
# script has never heard of.
if [ ! -e "$CONFIG_FILE" ]; then
    step "Splitting config.env out of .env (one-time, on the upgrade that added it)"
    CONFIG_KEYS="IMAGE_TAG GITHUB_REPOSITORY COMPOSE_NETWORK_NAME STEWARD_HOME HERMES_DATA_DIR APPROVALS_DIR GMAIL_SECRETS_DIR HERMES_UID HERMES_GID WORKFLOWS_MODEL_PROVIDER WORKFLOWS_DAILY_COST_CAP_USD DASHBOARD_BIND DOCS_BIND DASHBOARD_ALLOWED_ORIGINS REVIEW_CAPABILITIES BROWSER_URL"

    cfg_tmp="$(mktemp)"
    {
        printf '# Split out of .env by hermes-update. Mode 0644 -- nothing here is a\n'
        printf '# credential; those stayed behind in .env (0600).\n\n'
        for k in $CONFIG_KEYS; do
            grep -q "^$k=" "$ENV_FILE" || continue
            printf '%s=%s\n' "$k" "$(sed -n "s/^$k=//p" "$ENV_FILE" | tail -1)"
        done
        # New, so no pre-split .env has it. Off by default, same as a fresh
        # install -- see the README's "What is not in this release".
        printf '\n# Off by default -- see the README'"'"'s "What is not in this release".\n'
        printf 'COMPOSE_PROFILES=\n'
    } > "$cfg_tmp"
    cat "$cfg_tmp" > "$CONFIG_FILE"
    rm -f "$cfg_tmp"
    chmod 644 "$CONFIG_FILE"

    env_tmp="$(mktemp)"
    grep -vE "^($(printf '%s' "$CONFIG_KEYS" | tr ' ' '|'))=" "$ENV_FILE" > "$env_tmp" || true
    cat "$env_tmp" > "$ENV_FILE"
    rm -f "$env_tmp"
    chmod 600 "$ENV_FILE"
    say "  wrote $CONFIG_FILE; $ENV_FILE now holds only credentials"
fi

env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }
config_get() { sed -n "s/^$1=//p" "$CONFIG_FILE" | tail -1; }

DATA_DIR="$(config_get HERMES_DATA_DIR)"
DATA_DIR="${DATA_DIR:-$STEWARD_HOME/data}"
CURRENT_TAG="$(config_get IMAGE_TAG)"
CURRENT_TAG="${CURRENT_TAG:-unknown}"
GHCR_REPO="$(config_get GITHUB_REPOSITORY)"
GHCR_REPO="${GHCR_REPO:-frontanalyticsinc/hermes-infra}"
MARKER="$DATA_DIR/.steward-version"

compose() { docker compose -f "$STACK_FILE" --env-file "$CONFIG_FILE" --env-file "$ENV_FILE" "$@"; }

marker_get() {
    [ -f "$MARKER" ] || return 0
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$MARKER" | head -1
}

SEEDED="$(marker_get seeded_version)";  SEEDED="${SEEDED:-unknown}"
APPLIED="$(marker_get last_migration)"; APPLIED="${APPLIED:-0000}"

# Before the plan, not after: a plan whose "to" line names the version already
# installed is the misreading this check exists to prevent.
if [ "$TARGET_EXPLICIT" -eq 0 ]; then
    die "no target release. Pass --to TAG.

  This script does not know which releases exist — it ships inside one, so the
  copy installed here is pinned to $SHIPPED_WITH and always will be. Without
  --to it would re-pull the version already running and report success.

  This deployment is on $CURRENT_TAG. Newer releases are listed at
    https://github.com/FrontAnalyticsInc/steward/releases

    $0 --to vX.Y.Z --dry-run     # what it would do
    $0 --to vX.Y.Z               # do it"
fi

# --- plan --------------------------------------------------------------------
step "Plan"
say "  from        $CURRENT_TAG"
say "  to          $TARGET"
say "  seeded at   $SEEDED"
say "  migrations  applied through $APPLIED"

if [ "$CURRENT_TAG" = "$TARGET" ]; then
    warn "already on $TARGET. Continuing because you asked for it explicitly —"
    warn "this re-runs pending migrations, which is the supported way to finish"
    warn "an upgrade that failed at the health check."
fi

# Ask the TARGET release what it carries. This box builds its images, so the
# honest source is the target's SOURCE TREE, not a registry: the migrations that
# matter belong to the release being upgraded to, and they do not exist on this
# box until that source is fetched and hermes-init is built from it.
step "Fetching Steward $TARGET"
base="${STEWARD_BASE_URL:-https://codeload.github.com/${STEWARD_REPO:-FrontAnalyticsInc/steward}/tar.gz}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -fsSL "$base/$TARGET" -o "$tmp/src.tar.gz" \
    || die "could not download $base/$TARGET. Check that $TARGET is a published tag."

rm -rf "$SRC_DIR.new"
mkdir -p "$SRC_DIR.new"
tar -xzf "$tmp/src.tar.gz" -C "$SRC_DIR.new" --strip-components=1 \
    || die "the downloaded archive did not extract"
[ -f "$SRC_DIR.new/docker/docker-compose.yml" ] \
    || die "that archive does not look like the Steward source tree"

# The previous source and stack file are kept, not overwritten: restore() puts
# them back, and a rollback that left the new topology in place would be a
# rollback in name only.
rm -rf "$SRC_DIR.prev"
[ -d "$SRC_DIR" ] && cp -a "$SRC_DIR" "$SRC_DIR.prev"
cp -a "$STACK_FILE" "$STACK_FILE.prev"
rm -rf "$SRC_DIR"
mv "$SRC_DIR.new" "$SRC_DIR"
say "  source at $SRC_DIR"

# Re-rendered from the NEW source. The old runner only bumped IMAGE_TAG and
# re-pulled, which silently ignored any topology change in the target release —
# and with build contexts in the file, ignoring it is not survivable.
# `sed -i` is not portable and this script runs on the operator's own machine,
# not in a container. GNU sed takes the suffix as optional; the BSD sed on macOS
# requires it, reads the next argument as the suffix instead, and fails with
# "invalid command code" — during an upgrade, after the stack is already down.
#
# Rewriting through a temp file and copying the bytes back is the same shape
# install.sh uses to fill in a blank key, and it keeps the file's mode: `mv`
# would replace the file and bring its own mode with it. Targets CONFIG_FILE,
# not ENV_FILE — IMAGE_TAG has lived in config.env since the split above, and
# by this line that file is guaranteed to exist either way.
set_image_tag() {
    local tag="$1" tmpf
    tmpf="$(mktemp)"
    sed "s/^IMAGE_TAG=.*/IMAGE_TAG=$tag/" "$CONFIG_FILE" > "$tmpf"
    cat "$tmpf" > "$CONFIG_FILE"
    rm -f "$tmpf"
}

step "Rendering the $TARGET stack"
set_image_tag "$TARGET"
# Interpolated, like install.sh and for the same reason: --no-interpolate
# leaves every bind-mount source as literal "${VAR-default}" text, which compose
# types as a named volume and then refuses at `up`. See the long comment in
# install.sh. Both files exist by this line and IMAGE_TAG was just moved to
# $TARGET, so this renders the stack that is about to be built.
# -p steward for the same reason as in install.sh: without it compose names the
# project after $SRC_DIR/docker and the stack renders as "docker". The rename
# this introduces is handled at the stop step below.
( cd "$SRC_DIR/docker" && docker compose -p steward --env-file "$CONFIG_FILE" --env-file "$ENV_FILE" \
    -f docker-compose.yml \
    -f docker-compose.deploy.yml \
    -f docker-compose.standalone.yml \
    -f docker-compose.source.yml \
    config ) > "$tmp/steward-stack.yml" \
    || die "could not render the compose stack from $SRC_DIR"

# Before the old stack file is replaced. A rendered file compose cannot read is
# an upgrade that would fail at `up` with the previous stack already gone.
docker compose -f "$tmp/steward-stack.yml" config -q >/dev/null 2>&1 \
    || die "the rendered $TARGET stack is not a valid compose project. Nothing has
  been changed; the running stack is untouched."

# 0600, not 0644 — interpolation puts this install's secrets in it.
install -m 0600 "$tmp/steward-stack.yml" "$STACK_FILE"

INIT_IMAGE="ghcr.io/$GHCR_REPO/hermes-init:$TARGET"
step "Building the migration image for $TARGET"
compose build hermes-init >/dev/null 2>&1 \
    || die "could not build hermes-init for $TARGET from $SRC_DIR"

# No `|| true` here, and that matters more than it looks. If this container
# cannot run, an ignored failure yields an empty list, which reads as "nothing
# pending" — and the upgrade then proceeds to swap images without applying the
# migrations that new version requires. Silently. A failure to ASK what is
# pending has to stop the upgrade, not default to zero.
if ! PENDING="$(docker run --rm -v "$DATA_DIR:/opt/data:ro" --entrypoint hermes-migrate "$INIT_IMAGE" --list 2>&1)"; then
    die "could not read the migration list from $INIT_IMAGE:
$(printf '%s' "$PENDING" | sed 's/^/    /')"
fi
PENDING_COUNT="$(printf '%s' "$PENDING" | grep -c . || true)"

if [ "${PENDING_COUNT:-0}" -eq 0 ]; then
    say "  pending     none"
else
    say "  pending     $PENDING_COUNT"
    printf '%s\n' "$PENDING" | sed 's/^/                /' >&2
fi

if [ "$DRY_RUN" -eq 1 ]; then
    step "Dry run — nothing changed"
    say "  Re-run without --dry-run to apply."
    exit 0
fi

# --- snapshot ----------------------------------------------------------------
# Before anything is stopped, so the snapshot is of a state that was working.
step "Snapshotting the data disk"
mkdir -p "$SNAPSHOT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SNAPSHOT="$SNAPSHOT_DIR/data-$CURRENT_TAG-$STAMP.tar.gz"

tar -czf "$SNAPSHOT" -C "$(dirname "$DATA_DIR")" "$(basename "$DATA_DIR")" \
    || die "snapshot failed; refusing to upgrade without one."
say "  $SNAPSHOT ($(du -h "$SNAPSHOT" | cut -f1))"

# Restore is a whole-directory replacement, not a merge. A merge would leave
# files a failed migration created, which is exactly the state being escaped.
# The broken directory is kept, not deleted — it is the only evidence of what
# went wrong, and deleting evidence during a failure is how a bug becomes
# unreproducible.
restore() {
    warn "restoring $SNAPSHOT"
    compose down --remove-orphans >/dev/null 2>&1 || true
    local broken="$DATA_DIR.failed-$STAMP"
    mv "$DATA_DIR" "$broken" 2>/dev/null || true
    mkdir -p "$DATA_DIR"
    if tar -xzf "$SNAPSHOT" -C "$(dirname "$DATA_DIR")"; then
        say "  data restored; the failed state is kept at $broken"
    else
        die "RESTORE FAILED. The snapshot is at $SNAPSHOT and your previous
  data directory is at $broken. Do not run this again until both are sorted."
    fi
    set_image_tag "$CURRENT_TAG"
    # Put the previous source tree and stack file back before starting, so the
    # rolled-back stack is the one that was running, topology included. The
    # images for CURRENT_TAG were built by the previous install or upgrade and
    # are still in the local daemon, so this needs no rebuild.
    if [ -f "$STACK_FILE.prev" ]; then
        mv "$STACK_FILE.prev" "$STACK_FILE"
    fi
    if [ -d "$SRC_DIR.prev" ]; then
        rm -rf "$SRC_DIR"
        mv "$SRC_DIR.prev" "$SRC_DIR"
    fi
    compose up -d >/dev/null 2>&1 || true
    say "  rolled back to $CURRENT_TAG"
}

fail() {
    printf '\n%serror:%s upgrade failed at: %s\n' "$B" "$R" "$1" >&2
    restore
    exit 1
}

# --- stop --------------------------------------------------------------------
step "Stopping the stack"

# compose() reads $STACK_FILE, which by this line is the NEWLY rendered one, and
# a compose project is identified by the name inside it. Every install made
# before `-p steward` rendered that name as "docker", so on the first upgrade
# across that change `down` would look for a project called "steward", find
# nothing, and leave the running containers untouched — and `up` would then
# collide with them on every published port. The old stack file is still on
# disk from the copy above, and it names the project that is actually running,
# so it is what stops it.
prev_project="$(sed -n 's/^name:[[:space:]]*//p' "$STACK_FILE.prev" 2>/dev/null | head -1)"
new_project="$(sed -n 's/^name:[[:space:]]*//p' "$STACK_FILE" 2>/dev/null | head -1)"
if [ -n "$prev_project" ] && [ "$prev_project" != "$new_project" ]; then
    say "  project renamed $prev_project -> $new_project; stopping $prev_project first"
    docker compose -f "$STACK_FILE.prev" --env-file "$CONFIG_FILE" --env-file "$ENV_FILE" \
        down --remove-orphans || fail "docker compose down ($prev_project)"
fi

compose down --remove-orphans || fail "docker compose down"

# --- build -------------------------------------------------------------------
# IMAGE_TAG was already moved to $TARGET above, before hermes-init was built —
# the tag has to be right at build time, not just at `up` time, or the images
# this tags would not be the ones the stack then starts.
step "Building $TARGET (the slow step)"
compose build || fail "docker compose build ($TARGET)"

# --- migrate -----------------------------------------------------------------
# With the stack down and the data disk writable. Read-write here, unlike the
# --list call above.
if [ "${PENDING_COUNT:-0}" -gt 0 ]; then
    step "Running $PENDING_COUNT migration(s)"
    docker run --rm -v "$DATA_DIR:/opt/data" --entrypoint hermes-migrate "$INIT_IMAGE" --run \
        || fail "migrations (see the named script above)"
else
    step "No migrations to run"
fi

# --- start -------------------------------------------------------------------
step "Starting $TARGET"
compose up -d || fail "docker compose up"

step "Waiting for health"
deadline=$(( $(date +%s) + 600 ))
while :; do
    unhealthy=""
    for svc in hermes-gateway workflows light-dashboard review-executor; do
        cid="$(compose ps -q "$svc" 2>/dev/null || true)"
        if [ -z "$cid" ]; then unhealthy="$unhealthy $svc"; continue; fi
        state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)"
        case "$state" in
            healthy|running) ;;
            *) unhealthy="$unhealthy $svc" ;;
        esac
    done
    [ -n "$unhealthy" ] || break
    if [ "$(date +%s)" -ge "$deadline" ]; then
        for svc in $unhealthy; do
            say ""; say "--- $svc ---"
            compose logs --tail 50 "$svc" >&2 || true
        done
        fail "health check after 10 minutes ($unhealthy)"
    fi
    sleep 5
done
say "  all services healthy"

# --- record it ---------------------------------------------------------------
# Last, and only now. hermes-init has already rewritten current_version and
# available_migrations on this `up`; what it cannot know is whether the
# migrations were applied, so that is the field written here.
step "Recording the upgrade"
NEW_APPLIED="$APPLIED"
if [ "${PENDING_COUNT:-0}" -gt 0 ]; then
    NEW_APPLIED="$(printf '%s\n' "$PENDING" | tail -1)"
fi

# Not `fail` — that would restore the snapshot, and by this line the new stack
# is up and answering its health checks. Rolling back a working deployment
# because a bookkeeping file could not be written is a worse outcome than the
# thing it is reacting to. An unrecorded upgrade is recoverable: the marker
# still says the old version, so re-running re-applies the same migrations from
# the same starting point, which is exactly the case they are required to be
# idempotent for.
marker_die() {
    printf '\n%serror:%s %s\n' "$B" "$R" "$1" >&2
    say ""
    say "  The upgrade itself SUCCEEDED — $TARGET is running and healthy. What"
    say "  failed is recording it, so the console will keep reporting"
    say "  $CURRENT_TAG and a re-run will repeat the migrations above."
    say "  Marker: $MARKER"
    exit 1
}

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
tmp="$(mktemp "$MARKER.XXXXXX")" || marker_die "could not create a temp file beside the marker"

if [ -f "$MARKER" ]; then
    sed -e "s/\"last_migration\": \"[^\"]*\"/\"last_migration\": \"$NEW_APPLIED\"/" \
        -e "s/\"current_version\": \"[^\"]*\"/\"current_version\": \"$TARGET\"/" \
        -e "s/\"updated_at\": \"[^\"]*\"/\"updated_at\": \"$NOW\"/" \
        "$MARKER" > "$tmp" || { rm -f "$tmp"; marker_die "could not rewrite $MARKER"; }
else
    # hermes-init writes this on every `up`, so reaching here means the data
    # disk lost it — a restored backup, or a mount that came up empty. Write a
    # whole one rather than dying: the fields are all known here except the
    # migration ids the target image carries, which only hermes-init can see and
    # which it will fill in on the next start.
    warn "no marker at $MARKER — writing a fresh one"
    cat > "$tmp" <<JSON || { rm -f "$tmp"; marker_die "could not write $MARKER"; }
{
  "seeded_version": "$SEEDED",
  "current_version": "$TARGET",
  "last_migration": "$NEW_APPLIED",
  "available_migrations": [],
  "updated_at": "$NOW"
}
JSON
fi

if ! { chmod 644 "$tmp" && mv -f "$tmp" "$MARKER"; }; then
    rm -f "$tmp"
    marker_die "could not replace $MARKER"
fi

# Post-condition. A marker missing the keys the sed above targets would come
# through it unchanged and exit 0 — the substitution matches nothing and sed
# considers that success. Read the file back and check it says what was just
# written, rather than trusting that a write which reported success landed.
written="$(sed -n "s/.*\"current_version\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$MARKER" | head -1)"
[ "$written" = "$TARGET" ] \
    || marker_die "$MARKER still reads current_version=\"${written:-<absent>}\" after writing \"$TARGET\""

say "  $CURRENT_TAG -> $TARGET, migrations through $NEW_APPLIED"

# Keep a few, not all. An upgrade that is weeks old is not the one being
# rolled back, and these are full copies of the data disk.
if [ "$KEEP_SNAPSHOTS" -gt 0 ]; then
    # shellcheck disable=SC2012
    ls -1t "$SNAPSHOT_DIR"/data-*.tar.gz 2>/dev/null | tail -n +"$((KEEP_SNAPSHOTS + 1))" | while read -r old; do
        rm -f "$old" && say "  pruned $(basename "$old")"
    done
fi

# The rollback copies are only useful up to here. Past the marker write the
# upgrade has succeeded, and keeping a stale source tree around invites someone
# to build from it by accident.
rm -rf "$SRC_DIR.prev"
rm -f "$STACK_FILE.prev"

step "Done"
say "  Steward is on $TARGET. Snapshot kept at $SNAPSHOT"
