#!/usr/bin/env bash
# Install GSD Core (https://github.com/open-gsd/gsd-core) into a Hermes profile.
#
# GSD Core is a spec-driven development framework: a Discuss -> Plan -> Execute
# -> Verify -> Ship phase loop that runs its heavy research and planning work in
# fresh subagents. Hermes is a first-class runtime target upstream, so the
# vendor installer does the porting for us -- it rewrites Claude's Task()
# dispatch to Hermes's delegate_task and adds the `version:` frontmatter field
# the SKILL.md spec requires.
#
# The installer nests the 72 skills under 6 namespace routers, intending cold
# start to pay 6 descriptions rather than 72. Hermes's loader recurses anyway,
# so all 71 end up registered under category `gsd`. Trim with skills.disabled
# in config.yaml if that is too much context for a given profile.
#
# Why this is a script and not seeded files like everything else in seed.sh:
# GSD is ~626 vendor files that upstream expects to manage through its own
# installer (`gsd update`). Pinning a version here keeps the deployment
# reproducible without vendoring a third-party tree into this repo, and an
# upgrade is a one-line change to GSD_VERSION below.
#
# Why it runs inside the container rather than on the host: the installer bakes
# absolute paths into the artifacts it writes, and it resolves them from
# wherever it runs. The data dir is bind-mounted host $HOME/.hermes ->
# container /opt/data, so a host-side install writes host paths that the
# gateway cannot resolve. Running it in the container writes /opt/data/... and
# the container's own node. Same files on disk either way; only the container
# gets the paths right.
#
# Ownership: the gateway process runs as uid 1000 (`hermes`), which is the host
# user that owns the bind mount. `docker exec` defaults to root, which would
# leave root-owned files the agent cannot read -- hence the explicit -u below.
#
# Prerequisite: the gateway container must be running (docker compose up -d).
# This is the one piece of provisioning that cannot run before the stack is up,
# which is why seed.sh does not call it.
#
# Usage:
#   ./hermes/install-gsd.sh                    # install into the default profile
#   ./hermes/install-gsd.sh --profile worker   # install into a named profile
#   ./hermes/install-gsd.sh --force            # reinstall even if up to date
#   ./hermes/install-gsd.sh --uninstall        # remove GSD from the profile
set -euo pipefail

# Pinned so a rebuild reproduces the same tree. Bump to upgrade.
GSD_VERSION="1.9.1"

CONTAINER="${HERMES_CONTAINER:-hermes-gateway}"
# The image remaps the `hermes` user to these at boot and chowns the data dir.
HERMES_UID="${HERMES_UID:-1000}"
HERMES_GID="${HERMES_GID:-1000}"
# Container-side mount point of the host's Hermes data dir.
DATA_MOUNT="${HERMES_DATA_MOUNT:-/opt/data}"

PROFILE="default"
FORCE=0
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="${2:?--profile needs a name}"; shift 2 ;;
    --profile=*) PROFILE="${1#*=}"; shift ;;
    --force) FORCE=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '/^# Usage:/,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;$d'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# `default` is the root data dir; every other profile lives under profiles/.
if [ "$PROFILE" = "default" ]; then
  CONFIG_DIR="$DATA_MOUNT"
else
  CONFIG_DIR="$DATA_MOUNT/profiles/$PROFILE"
fi

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  echo "error: container '$CONTAINER' is not running." >&2
  echo "       start the stack first: cd docker && docker compose up -d" >&2
  exit 1
fi

# HOME must point at a dir uid 1000 can write: the image's /root is not it, and
# npm needs a cache directory before it will run anything.
in_container() {
  docker exec -u "$HERMES_UID:$HERMES_GID" -e HOME="$DATA_MOUNT" "$CONTAINER" "$@"
}

installed_version() {
  in_container sh -c "cat '$CONFIG_DIR/gsd-core/VERSION' 2>/dev/null" | tr -d '[:space:]'
}

echo "GSD Core -> profile '$PROFILE' ($CONFIG_DIR in $CONTAINER)"

if [ "$UNINSTALL" -eq 1 ]; then
  in_container sh -c "cd /tmp && npx -y @opengsd/gsd-core@$GSD_VERSION --hermes --global --config-dir '$CONFIG_DIR' --uninstall"
  echo "  removed GSD from profile '$PROFILE'"
  exit 0
fi

CURRENT="$(installed_version || true)"
if [ "$CURRENT" = "$GSD_VERSION" ] && [ "$FORCE" -eq 0 ]; then
  echo "  keep    $GSD_VERSION already installed (--force to reinstall)"
  exit 0
fi
[ -n "$CURRENT" ] && echo "  found   $CURRENT installed, updating to $GSD_VERSION"

in_container sh -c "cd /tmp && npx -y @opengsd/gsd-core@$GSD_VERSION --hermes --global --config-dir '$CONFIG_DIR'"

FINAL="$(installed_version || true)"
if [ "$FINAL" != "$GSD_VERSION" ]; then
  echo "error: install finished but VERSION reads '${FINAL:-<missing>}', expected $GSD_VERSION" >&2
  exit 1
fi

SKILLS="$(in_container sh -c "find '$CONFIG_DIR/skills/gsd' -name SKILL.md 2>/dev/null | wc -l" | tr -d '[:space:]')"
echo "  seeded  GSD $GSD_VERSION ($SKILLS skills under skills/gsd/)"
echo
echo "Restart the profile to pick up the new skills:"
echo "  docker compose restart $CONTAINER"
echo
echo "Note: the installer also writes a settings.json of guard hooks. Hermes has"
echo "no settings.json hook surface, so those are inert -- the skills are the"
echo "part that works. Harmless to leave in place."
