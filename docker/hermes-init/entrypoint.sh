#!/usr/bin/env bash
# Populate a mounted data directory, then exit. Runs once, before the services
# that read it — compose gates them on service_completed_successfully.
#
# Idempotent twice over: rsync --ignore-existing for the workflows tree, and
# seed.sh's own copy-if-absent for everything else. Re-running on a live box
# adds what is missing and touches nothing else.
set -euo pipefail

DATA_DIR=/opt/data
SEED_DIR=/seed/hermes

# The path the DATA DIRECTORY has on the host — not in this container.
#
# Required, and deliberately not defaulted. It is substituted into config.yaml
# as the tool sandbox's volume source, and the docker daemon resolves that on
# the host. A wrong value produces a config that looks correct and fails much
# later, when an agent first tries to run a tool, with an error that names a
# path nobody recognises. Better to refuse now.
if [ -z "${HERMES_HOST_DATA_DIR:-}" ]; then
    echo "hermes-init: HERMES_HOST_DATA_DIR is not set." >&2
    echo "  It must be the host path of the directory mounted at ${DATA_DIR}," >&2
    echo "  e.g. /srv/steward/data. config.yaml records it as the tool sandbox's" >&2
    echo "  volume source, and the docker daemon resolves it on the host." >&2
    exit 2
fi

uid="${HERMES_UID:-1000}"
gid="${HERMES_GID:-1000}"

echo "hermes-init: seeding ${DATA_DIR} (host path ${HERMES_HOST_DATA_DIR})"

# --- the workflows tree ---
#
# The agent-authoring surface. The gateway mounts it read-write and agents write
# new modules into it, so it is customer state, not shipped code — hence
# --ignore-existing, which means an image upgrade does NOT rewrite it.
#
# Note what that implies: the code that RUNS comes from the workflows image, not
# from this tree. This copy exists for the two consumers that need real files —
# the tool sandbox (mounted by host path) and light-dashboard's AST fallback,
# which parses source to describe pipelines that app-info cannot.
mkdir -p "${DATA_DIR}/workflows"
rsync -a --ignore-existing /seed/workflows/ "${DATA_DIR}/workflows/"

# --- every other bind-mount source under the data dir ---
#
# A bind mount whose source does not exist is not an error: docker CREATES it,
# as an empty directory owned by root. The service then starts, finds nothing,
# and reports itself healthy — the failure this whole overlay exists to prevent.
#
# seed.sh does not cover these because they hold no seeded content; they are
# empty directories the services fill at runtime. Empty is fine. Root-owned and
# created behind our back is not, so they are made here, before anything mounts
# them, and swept up by the chown at the end.
#
# The approvals queue is the one that matters most. It used to default OUTSIDE
# the data directory, which on a cloud box means the boot disk — so replacing
# the VM destroyed the human-approval queue while the data disk survived intact,
# and nothing in the deploy would have reported that.
for d in \
    adk \
    memories \
    wiki \
    config \
    browser/extensions \
    browser/profile \
    approvals/pending \
    approvals/approved \
    approvals/executing \
    approvals/executed \
    approvals/failed
do
    mkdir -p "${DATA_DIR}/${d}"
done

# The queue is reported by seed.sh below, which prints the path it actually
# resolved. If an operator points APPROVALS_DIR outside the data directory, that
# path is outside this container's only mount and creating it is install.sh's
# job — which is why install.sh keeps it under the data dir.

# --- credential drop ---
#
# Where uploaded service-account and OAuth client JSON land. The console writes
# here through its own read-write /opt/data mount; workflows and review-executor
# see the same directory read-only at /secrets. 0700 because the contents are
# private keys.
mkdir -p "${DATA_DIR}/secrets"
chmod 700 "${DATA_DIR}/secrets"

# --- everything else, via the same seed.sh a developer runs ---
# APPROVALS_DIR is passed explicitly even though the loop above already made
# that tree. seed.sh falls back to $HOME/approval-queue/approvals, which is the
# right default for a developer and is /root/... in here — so leaving it unset
# creates a stray queue inside a container that is about to be discarded, and
# prints a path that is not the one anything reads.
HERMES_DATA_DIR="${DATA_DIR}" \
HERMES_SEED_DIR="${SEED_DIR}" \
HERMES_HOST_REPO_ROOT="${HERMES_HOST_DATA_DIR}" \
APPROVALS_DIR="${DATA_DIR}/approvals" \
    bash "${SEED_DIR}/seed.sh" "$@"

# --- version marker ---
#
# Written on every run, not only the first, because `available_migrations` has
# to describe the image that is running RIGHT NOW — that is what lets the
# console say "migration pending" without being able to see /opt/migrations,
# which lives in this image and not in its own.
#
# `seeded_version` is the exception: set once, from the release that created
# this directory, and preserved forever after. It is how you tell a support
# conversation whether an instance is six months old or was installed on
# Tuesday.
#
# `last_migration` is NOT advanced here. Migrations are applied by
# hermes-migrate under the host-side runner, and the runner records them only
# after the upgraded stack passes its health checks. Advancing it here would
# mark migrations as applied on a plain `up`, where none of them ran.
# shellcheck source=marker.sh
. /usr/local/lib/steward-marker.sh

marker="$(marker_path "${DATA_DIR}")"
version="${STEWARD_VERSION:-unknown}"

seeded="$(marker_get "$marker" seeded_version)"
seeded="${seeded:-$version}"
applied="$(marker_get "$marker" last_migration)"
applied="${applied:-$NO_MIGRATIONS}"

# shellcheck disable=SC2046
marker_write "$marker" "$seeded" "$version" "$applied" $(migration_ids /opt/migrations)
echo "  marker  ${marker} (seeded ${seeded}, now ${version}, migration ${applied})"

# --- ownership ---
#
# This container runs as root so it can write into a mount whose ownership is
# not yet settled; everything it creates is therefore root-owned, which is the
# exact failure the repo warns about elsewhere (a gateway that cannot read its
# own config). One recursive chown at the end is the fix. HERMES_UID/GID is the
# single owner of this directory by design — every service is given the same
# pair — so this is idempotent rather than opinionated.
chown -R "${uid}:${gid}" "${DATA_DIR}"

echo "hermes-init: done"
