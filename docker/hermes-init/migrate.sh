#!/usr/bin/env bash
# Run the migrations this image carries that the data disk has not seen.
#
#   hermes-migrate --list    what is pending, one id per line, exit 0
#   hermes-migrate --run     run them in order, stop at the first failure
#
# Invoked by the host-side runner as a one-shot container with the data disk
# mounted and the rest of the stack DOWN. It does not update the marker: the
# runner does that, and only after the new stack passes its health checks, so
# that an upgrade which comes up broken re-runs everything on the next attempt
# rather than recording a success nobody verified.
set -uo pipefail

DATA_DIR=/opt/data
MIGRATIONS_DIR=/opt/migrations

# shellcheck source=marker.sh
. /usr/local/lib/steward-marker.sh

mode="${1:---list}"
marker="$(marker_path "$DATA_DIR")"
applied="$(marker_get "$marker" last_migration)"
applied="${applied:-$NO_MIGRATIONS}"

mapfile -t pending < <(pending_ids "$applied" "$MIGRATIONS_DIR")

case "$mode" in
    --list)
        printf '%s\n' "${pending[@]}"
        exit 0
        ;;
    --run) ;;
    *)
        echo "usage: hermes-migrate [--list|--run]" >&2
        exit 2
        ;;
esac

if [ "${#pending[@]}" -eq 0 ]; then
    echo "hermes-migrate: nothing pending (at $applied)"
    exit 0
fi

echo "hermes-migrate: $(printf '%s ' "${pending[@]}")from $applied"

for id in "${pending[@]}"; do
    # One id, one file. Two files sharing a prefix is an authoring mistake that
    # would otherwise run one of them and silently skip the other.
    mapfile -t files < <(find "$MIGRATIONS_DIR" -maxdepth 1 -type f -name "${id}_*" | sort)
    if [ "${#files[@]}" -ne 1 ]; then
        echo "hermes-migrate: expected exactly one file for $id, found ${#files[@]}" >&2
        exit 1
    fi
    script="${files[0]}"

    echo "hermes-migrate: applying $(basename "$script")"
    case "$script" in
        *.sh) bash "$script" ;;
        *.py) python3 "$script" ;;
        *)    echo "hermes-migrate: $script is neither .sh nor .py" >&2; exit 1 ;;
    esac
    rc=$?
    if [ "$rc" -ne 0 ]; then
        # Named, because the runner prints this straight to the operator and
        # "migration failed" without a filename is a support ticket.
        echo "hermes-migrate: FAILED $(basename "$script") (exit $rc)" >&2
        exit "$rc"
    fi
done

echo "hermes-migrate: applied ${#pending[@]}"
