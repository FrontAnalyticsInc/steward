#!/usr/bin/env bash
# Keep the curated half of the data directory under version control.
#
# Sourced and called by hermes-init, which means it runs on install and again
# on every `up` — including the one at the end of an upgrade. That timing is
# the point. hermes-init has just re-seeded, so a commit taken here records
# exactly what the new release did to a directory the operator had been
# editing, next to the commit that recorded what it looked like before.
#
# What that buys, concretely:
#
#   git log --oneline           every release this directory has passed through
#   git diff HEAD~1             what the upgrade changed
#   git checkout HEAD~1 -- x    put back something it should not have
#   git bundle create ... --all a complete, portable export
#
# None of which requires the operator to have thought about any of it in
# advance, which is the only kind of backup that is there when it is needed.
#
# Failure here is never fatal. A data directory that is not a git repository is
# a data directory that works exactly as it did before this file existed; the
# stack must not refuse to start because a bookkeeping step did not.

# Root runs this, over a tree owned by HERMES_UID. Git calls that "dubious
# ownership" and refuses, which is the right default for a shared machine and
# pure obstruction here — we are the process that chowns this directory. Set
# per-invocation rather than in a config file so nothing is left behind that
# widens git's behaviour for anything else in the image.
_track_git() {
    local dir="$1"
    shift
    git -c safe.directory='*' \
        -c user.name='Steward' \
        -c user.email='steward@localhost' \
        -C "$dir" "$@"
}

# Everything below is best-effort. `command -v git` because an older image, or
# one rebuilt without the apk line, should degrade to "no tracking" instead of
# a hard failure two lines later.
track_data_dir() {
    local dir="$1" version="${2:-unknown}"

    if ! command -v git >/dev/null 2>&1; then
        echo "  track   skipped (git not in this image)"
        return 0
    fi

    # The allowlist has to exist first. seed.sh writes it, and it runs before
    # this does — but if it did not, initialising here would stage the whole
    # data directory, which is the sqlite databases, every session transcript
    # and the secrets directory. Refuse instead.
    if [ ! -f "${dir}/.gitignore" ]; then
        echo "  track   skipped (no .gitignore — refusing to track everything)"
        return 0
    fi

    if [ ! -d "${dir}/.git" ]; then
        if ! _track_git "$dir" init -q -b main >/dev/null 2>&1; then
            echo "  track   skipped (git init failed)"
            return 0
        fi
        echo "  track   initialised ${dir}/.git"
    fi

    # -A rather than -u: a newly seeded file is untracked, and the first commit
    # of a release that adds one is exactly where it should appear.
    if ! _track_git "$dir" add -A >/dev/null 2>&1; then
        echo "  track   skipped (nothing could be staged)"
        return 0
    fi

    # Nothing staged is the normal case on a plain restart. Not an error, and
    # not worth a line of output either.
    if _track_git "$dir" diff --cached --quiet >/dev/null 2>&1; then
        return 0
    fi

    if _track_git "$dir" commit -q -m "steward ${version}" >/dev/null 2>&1; then
        echo "  track   committed as ${version}"
    else
        echo "  track   skipped (commit failed)"
    fi
    return 0
}
