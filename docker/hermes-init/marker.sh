#!/usr/bin/env bash
# Read and write ${DATA_DIR}/.steward-version.
#
# Sourced by hermes-init (which creates it) and hermes-migrate (which reads it).
# The host-side runner, hermes-update, writes it too — from bash, without this
# file — so the format has to be simple enough that two implementations cannot
# drift. It is flat JSON with string values and no nesting, for that reason.
#
# On the DATA DISK, never in the image. An image knows what version it is; only
# the disk knows what version this deployment has actually reached, and that is
# the thing an upgrade has to reason about.
#
#   seeded_version       the release that first created this data directory,
#                        never changed afterwards. Tells you how old an install
#                        really is, which a support conversation always wants.
#   current_version      the release running against it now.
#   last_migration       the highest migration id applied, "0000" for none.
#   available_migrations every id present in the CURRENT image, so the console
#                        can compute what is pending without being able to see
#                        /opt/migrations itself — it is in a different image.
#   updated_at           UTC, ISO 8601.

MARKER_NAME=.steward-version
NO_MIGRATIONS=0000

marker_path() { printf '%s/%s' "${1:-/opt/data}" "$MARKER_NAME"; }

# One field, or empty. Deliberately not a JSON parser: jq is not in every image
# this runs in, and the writer below guarantees one field per line.
marker_get() {
    local file="$1" key="$2"
    [ -f "$file" ] || return 0
    sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$file" | head -1
}

# Everything after the fixed four arguments is treated as the available
# migration ids.
#
# Atomic. A marker half-written by a container that died mid-upgrade would be
# read as a valid state by the next run, and the whole point of the file is that
# it is trustworthy. Temp file in the same directory — a rename is only atomic
# within a filesystem — then mv.
marker_write() {
    local file="$1" seeded="$2" current="$3" last_mig="$4"
    shift 4
    local ids="" id
    for id in "$@"; do
        [ -n "$ids" ] && ids="$ids, "
        ids="$ids\"$id\""
    done
    local tmp
    tmp="$(mktemp "${file}.XXXXXX")" || return 1
    cat > "$tmp" <<JSON
{
  "seeded_version": "$seeded",
  "current_version": "$current",
  "last_migration": "$last_mig",
  "available_migrations": [$ids],
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
    chmod 644 "$tmp" && mv -f "$tmp" "$file"
}

# Migration ids present in this image, ascending. The filename prefix IS the id.
#
# Glob rather than `find -printf`. This runs in an Alpine image, where `find` is
# busybox's and has no -printf — it fails, prints nothing, and the caller reads
# that as "no migrations". Which is indistinguishable from the correct answer
# for v0.1.0, and would have stayed invisible until the first release that
# actually shipped one.
migration_ids() {
    local dir="${1:-/opt/migrations}" f base
    [ -d "$dir" ] || return 0
    for f in "$dir"/[0-9][0-9][0-9][0-9]_*.sh "$dir"/[0-9][0-9][0-9][0-9]_*.py; do
        [ -f "$f" ] || continue
        base="${f##*/}"
        printf '%s\n' "${base:0:4}"
    done | sort -u
}

# Ids strictly greater than the one already applied. String comparison is safe
# because the ids are fixed-width and zero-padded, which is why they are.
pending_ids() {
    local applied="${1:-$NO_MIGRATIONS}" dir="${2:-/opt/migrations}" id
    for id in $(migration_ids "$dir"); do
        [ "$id" \> "$applied" ] && printf '%s\n' "$id"
    done
}
