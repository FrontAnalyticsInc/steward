#!/usr/bin/env bash
# Seed a Hermes data directory from the versioned material in this repo.
#
# Idempotent and non-destructive: it only writes files that are ABSENT. Running
# it against a live deployment will not overwrite an evolved config, an agent's
# memories, or the kanban board. Delete a file and re-run to restore it.
#
# The one opt-in exception is `--update-instructions`, which overwrites skills/
# and every SOUL.md from the repo. Those are instruction, not state, so there is
# nothing to preserve -- and copy-if-absent otherwise means a correction never
# lands on a deployment that already has the old copy.
#
# What is seeded (versioned, belongs to the deployment):
#   config.yaml, SOUL.md, skills/, profiles/*/{config.yaml,SOUL.md}, scripts/
#   config/model-aliases.yaml, agents/README.md, .gitignore
#
# What is NOT seeded (runtime state, belongs to the host):
#   state.db, kanban.db, sessions/, memories/, cron/jobs.json, auth.json, .env
#
# Credentials are never seeded. A deployment supplies its own through .env —
# ANTHROPIC_API_KEY is read straight from the environment by the model provider,
# so there is no interactive auth step on the default path. `hermes auth` is the
# OAuth flow, and is only needed for a subscription-backed provider.
set -euo pipefail

# Both are overridable so this script can run from inside the hermes-init image
# on a box that has no clone of this repo.
#
# REPO_ROOT is not where the seed material is; it is the host path that gets
# substituted into config.yaml (see render() below). Those two are the same
# directory for a developer with a checkout, and they are NOT the same on a
# deployed box: there the material ships inside an image while the path written
# into config.yaml has to be a real host path, because Hermes creates its tool
# sandbox through the host's docker socket and the daemon resolves that path on
# the host, not in any container. On a deployed box the data directory IS the
# repo root — hermes-init rsyncs workflows/ into it for exactly this reason.
REPO_ROOT="${HERMES_HOST_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SEED_DIR="${HERMES_SEED_DIR:-$REPO_ROOT/hermes}"
DATA_DIR="${HERMES_DATA_DIR:-$HOME/.hermes}"
OBSIDIAN_VAULT="${OBSIDIAN_VAULT_PATH:-}"

# Skills and SOUL.md are the seeded things that are pure instruction rather than
# state: nothing an agent does edits them, so there is no evolved copy to
# protect, and copy-if-absent just means a correction never reaches a deployment
# that already has the old one. That failure is silent and was real -- the live
# adk-workflows skill sat two commits behind for days, still describing a
# directory that no longer existed. --update-instructions overwrites them from
# the repo; config, memories and the board stay copy-if-absent.
UPDATE_INSTRUCTIONS=0
for arg in "$@"; do
  case "$arg" in
    --update-instructions) UPDATE_INSTRUCTIONS=1 ;;
    -h|--help)
      echo "usage: ./hermes/seed.sh [--update-instructions]"
      echo "  --update-instructions  overwrite skills/ and SOUL.md from the repo"
      echo "                         (default: keep existing)"
      exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

echo "seeding Hermes data dir: $DATA_DIR"
echo "  from: $SEED_DIR"
mkdir -p "$DATA_DIR"

render() {
  # Substitute host-specific paths into a template.
  #
  # These cannot be environment variables at runtime: Hermes reads config.yaml
  # directly and does not expand them, and the tool sandbox is created through
  # the host's docker socket, so its volume paths must be real host paths.
  sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
      -e "s|__OBSIDIAN_VAULT__|$OBSIDIAN_VAULT|g" "$1"
}

copy_if_absent() {
  local src="$1" dest="$2"
  if [ -e "$dest" ]; then
    echo "  keep    $dest (exists)"
  else
    mkdir -p "$(dirname "$dest")"
    cp -r "$src" "$dest"
    echo "  seeded  $dest"
  fi
}

# --- top-level config + identity ---
if [ -e "$DATA_DIR/config.yaml" ]; then
  echo "  keep    $DATA_DIR/config.yaml (exists)"
else
  render "$SEED_DIR/config.yaml.template" > "$DATA_DIR/config.yaml"
  echo "  seeded  $DATA_DIR/config.yaml (rendered)"
fi
if [ "$UPDATE_INSTRUCTIONS" -eq 1 ]; then
  cp "$SEED_DIR/SOUL.md" "$DATA_DIR/SOUL.md"
  echo "  updated $DATA_DIR/SOUL.md"
else
  copy_if_absent "$SEED_DIR/SOUL.md" "$DATA_DIR/SOUL.md"
fi

# --- skills ---
mkdir -p "$DATA_DIR/skills"
for skill in "$SEED_DIR"/skills/*/; do
  [ -d "$skill" ] || continue
  dest="$DATA_DIR/skills/$(basename "$skill")"
  if [ "$UPDATE_INSTRUCTIONS" -eq 1 ] && [ -e "$dest" ]; then
    rm -rf "$dest"
    cp -r "$skill" "$dest"
    echo "  updated $dest"
  else
    copy_if_absent "$skill" "$dest"
  fi
done

# --- scripts (the ADK invoker and its per-workflow wrappers) ---
mkdir -p "$DATA_DIR/scripts"
for script in "$SEED_DIR"/scripts/*; do
  [ -f "$script" ] || continue
  copy_if_absent "$script" "$DATA_DIR/scripts/$(basename "$script")"
done

# --- wiki (what the workflows remember about people and organisations) ---
#
# Created empty rather than seeded. The wiki is written by the workflows and by
# the backfill, and shipping example documents would put fictional people in a
# store whose whole point is that a fact is filed under the entity it is about.
#
# Docker creates a missing bind source as root, and the workflows container does
# not run as root, so making it here as the invoking user avoids a permissions
# error on first write that does not name its cause.
mkdir -p "$DATA_DIR/wiki"

# --- config (model aliases, read live by the workflows) ---
#
# Mounted read-only at /code/config. Seeded rather than created empty: an absent
# aliases file leaves the workflows on app/model_aliases.py's built-in defaults,
# which work, but then editing a model means editing code instead of this file.
#
# Same root-ownership reason as the wiki above, with a sharper edge: the mount
# is a directory precisely so a missing source cannot become a root-owned FILE
# that the container can read but nobody can replace.
mkdir -p "$DATA_DIR/config"
copy_if_absent "$SEED_DIR/config/model-aliases.yaml" "$DATA_DIR/config/model-aliases.yaml"

# --- custom agents (this deployment's own workflows) ---
#
# Mounted read-only at /code/agents_local, which is what HERMES_AGENTS_PATH
# points at. Seeded with a README rather than an example agent: an example that
# imports ADK would either be loaded (and appear as a real workflow nobody
# asked for) or be named with a leading underscore to prevent that, at which
# point it no longer demonstrates the thing it exists to demonstrate.
#
# The directory must exist even when empty, for the usual reason — a missing
# bind source becomes a root-owned directory the container cannot write and
# nobody thinks to look at.
mkdir -p "$DATA_DIR/agents"
copy_if_absent "$SEED_DIR/agents-README.md" "$DATA_DIR/agents/README.md"

# --- version control for the curated half of this directory ---
#
# Seeded, not created: the allowlist is versioned material and gets corrections
# like anything else. copy-if-absent all the same, because an operator who has
# added their own entries should keep them — and because the list fails safe,
# so a stale copy under-tracks rather than committing something new and secret.
#
# The repository itself is initialised by hermes-init on a deployed box. A
# developer's checkout is already under version control and does not need a
# second repository inside it, so nothing here runs git.
copy_if_absent "$SEED_DIR/data.gitignore" "$DATA_DIR/.gitignore"

# --- the remaining bind-mount sources ---
#
# These hold no seeded content — they are empty directories the services fill at
# runtime — but they must EXIST before anything mounts them. A bind mount whose
# source is missing is not an error: docker creates it, as an empty directory
# owned by root. The service then starts, finds nothing, and reports itself
# healthy.
#
# docker/hermes-init/entrypoint.sh makes the same set on a deployed box. It is
# duplicated rather than shared because that container runs as root and chowns
# afterwards, while here the point is to create them AS the invoking user. The
# two lists must agree; if you add a mount to compose, add it in both places.
for d in \
  adk \
  memories \
  agents \
  browser/extensions \
  browser/profile
do
  mkdir -p "$DATA_DIR/$d"
done

# 0700 because uploaded service-account and OAuth client JSON land here.
mkdir -p "$DATA_DIR/secrets"
chmod 700 "$DATA_DIR/secrets"

# The approvals queue defaults OUTSIDE the data directory, so it is resolved
# separately — an operator who repoints APPROVALS_DIR gets the subdirs made
# wherever they pointed it, rather than a half-made queue in the old location.
APPROVALS_DIR="${APPROVALS_DIR:-$HOME/approval-queue/approvals}"
for d in pending approved rejected executing executed failed sent; do
  mkdir -p "$APPROVALS_DIR/$d"
done
echo "  ready   $APPROVALS_DIR"

# --- profiles (bounded worker agents) ---
#
# A profile may ship SOUL.md without a config.yaml.template — that used to be
# true of `dev` too, because a config *cloned from a running `default`* can
# carry the dashboard secret and password_hash, and vendoring THAT would commit
# credentials. dev's template avoids the problem instead of working around it:
# it is rendered from the same pristine, secret-free base as `default`'s own
# config.yaml.template (see hermes/profiles/dev/config.yaml.template and
# docs-ops/architecture/profiles.md), so it seeds like any other profile here.
for profile in "$SEED_DIR"/profiles/*/; do
  [ -d "$profile" ] || continue
  name="$(basename "$profile")"
  dest="$DATA_DIR/profiles/$name"
  mkdir -p "$dest"
  if [ -e "$dest/config.yaml" ]; then
    echo "  keep    $dest/config.yaml (exists)"
  elif [ -f "$profile/config.yaml.template" ]; then
    render "$profile/config.yaml.template" > "$dest/config.yaml"
    echo "  seeded  $dest/config.yaml (rendered)"
  else
    echo "  skip    $dest/config.yaml (no template — clone it on the host)"
  fi
  # profile.yaml is the routing description — what the decomposer reads to
  # decide whether a task belongs to this profile. Instruction, not state, and
  # it holds no credentials, so it seeds like SOUL.md does.
  if [ -f "$profile/profile.yaml" ]; then
    if [ "$UPDATE_INSTRUCTIONS" -eq 1 ]; then
      cp "$profile/profile.yaml" "$dest/profile.yaml"
      echo "  updated $dest/profile.yaml"
    else
      copy_if_absent "$profile/profile.yaml" "$dest/profile.yaml"
    fi
  fi
  if [ "$UPDATE_INSTRUCTIONS" -eq 1 ] && [ -f "$profile/SOUL.md" ]; then
    cp "$profile/SOUL.md" "$dest/SOUL.md"
    echo "  updated $dest/SOUL.md"
  else
    copy_if_absent "$profile/SOUL.md" "$dest/SOUL.md"
  fi
done

echo
echo "done. Not seeded (supply per host):"
echo "  .env       — copy docker/.env.example to docker/.env and fill in."
echo "               ANTHROPIC_API_KEY, API_SERVER_KEY, BROWSER_TOKEN and the"
echo "               dashboard basic-auth pair have no working defaults."
echo
echo "Optional, and only for a development host (needs the stack running):"
echo "  GSD Core   — run: ./hermes/install-gsd.sh --profile dev"
