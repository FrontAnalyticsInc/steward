#!/usr/bin/env bash
# Steward installer.
#
#     curl -fsSL https://raw.githubusercontent.com/FrontAnalyticsInc/steward/main/install.sh | bash
#
# This file lives here, in the private repo that builds the images, and is
# published to the public steward repo by the release workflow. Edit it here.
#
# Two properties shape the whole script:
#
# It is piped into bash, so stdin is the SCRIPT, not the terminal. Every prompt
# reads /dev/tty. A `read` on stdin would silently consume the rest of this file
# and execute a truncated installer, which is the worst available failure.
#
# It refuses to run as root. HERMES_UID/HERMES_GID must be a real user — the
# data directory is owned by that pair and every service is handed it — and
# config.yaml sets docker_run_as_host_user. Installing as root produces a
# root-owned data directory that the services cannot write, and the error that
# surfaces days later names a path rather than a cause. So: `| bash`, never
# `| sudo bash`. The script sudo's for the three things that genuinely need it.
set -euo pipefail

VERSION="${STEWARD_VERSION:-v0.1.1}"
STEWARD_HOME="${STEWARD_HOME:-/srv/steward}"
STEWARD_REPO="${STEWARD_REPO:-FrontAnalyticsInc/steward}"
GHCR_REPO="${GHCR_REPO:-frontanalyticsinc/hermes-infra}"

STACK_DIR="$STEWARD_HOME/stack"
DATA_DIR="$STEWARD_HOME/data"
ENV_FILE="$STACK_DIR/.env"
STACK_FILE="$STACK_DIR/steward-stack.yml"
SRC_DIR="$STEWARD_HOME/src"

ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"
NO_KEY=0
INSTALL_DOCKER="${STEWARD_INSTALL_DOCKER:-}"

# Kept verbatim so the script can hand itself the same arguments when it
# re-enters under the docker group below. Captured before the parser eats them.
ORIG_ARGS=("$@")
SELF_URL="${STEWARD_SELF_URL:-https://raw.githubusercontent.com/$STEWARD_REPO/main/install.sh}"

while [ $# -gt 0 ]; do
    case "$1" in
        --api-key)   ANTHROPIC_KEY="${2:-}"; shift 2 ;;
        --version)   VERSION="${2:-}";       shift 2 ;;
        --home)      STEWARD_HOME="${2:-}";  shift 2
                     STACK_DIR="$STEWARD_HOME/stack"
                     DATA_DIR="$STEWARD_HOME/data"
                     ENV_FILE="$STACK_DIR/.env"
                     STACK_FILE="$STACK_DIR/steward-stack.yml"
                     SRC_DIR="$STEWARD_HOME/src" ;;
        -h|--help)
            cat <<'USAGE'
usage: install.sh [options]

  --api-key KEY    Anthropic API key (or set ANTHROPIC_API_KEY)
  --version TAG    release tag to build from (default: the version pinned in this script)
  --home PATH      install location (default: /srv/steward)

Environment: STEWARD_INSTALL_DOCKER=1 installs Docker without asking.
USAGE
            exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# --- output ------------------------------------------------------------------
if [ -t 2 ] && command -v tput >/dev/null 2>&1 && [ -n "${TERM:-}" ]; then
    B="$(tput bold 2>/dev/null || true)"; R="$(tput sgr0 2>/dev/null || true)"
else
    B=""; R=""
fi
say()  { printf '%s\n' "$*" >&2; }
step() { printf '\n%s==>%s %s\n' "$B" "$R" "$*" >&2; }
warn() { printf '  !  %s\n' "$*" >&2; }
die()  { printf '\n%serror:%s %s\n' "$B" "$R" "$*" >&2; exit 1; }

# Prompts read the terminal, never stdin — see the header.
have_tty() { [ -e /dev/tty ] && { : >/dev/tty; } 2>/dev/null; }
ask() {
    local prompt="$1" silent="${2:-}" reply=""
    have_tty || return 1
    if [ "$silent" = "silent" ]; then
        printf '%s' "$prompt" >/dev/tty
        read -rs reply </dev/tty || return 1
        printf '\n' >/dev/tty
    else
        printf '%s' "$prompt" >/dev/tty
        read -r reply </dev/tty || return 1
    fi
    printf '%s' "$reply"
}

# --- preflight ---------------------------------------------------------------
step "Checking this machine"

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root — see the note at the top of this script.
  Steward's data directory is owned by a real uid/gid and the services run as it.
  Use:  curl -fsSL <url> | bash      (no sudo)"

command -v sudo >/dev/null 2>&1 || die "sudo is required (to create $STEWARD_HOME and, if needed, install Docker)."

# Under `curl | bash`, stdin is the SCRIPT. If sudo decides it wants a password
# it reads one from stdin -- swallowing the rest of this file and treating it as
# a failed attempt -- so the operator never sees a prompt, just sudo refusing:
#
#     sudo: I'm sorry alton. I'm afraid I can't do that
#
# which reads like a permissions problem and is not one. It is also intermittent
# by nature: a box whose sudo timestamp is still warm from installing Docker
# gets all the way through, and the same box fifteen minutes later does not.
#
# So the password is asked for HERE, explicitly on the terminal, before anything
# needs it. Everything this script sudo's for happens in the next few steps,
# well inside the timestamp it refreshes.
if ! sudo -n true 2>/dev/null; then
    if have_tty; then
        say "  sudo needs your password"
        sudo -v </dev/tty || die "sudo could not authenticate you."
    else
        die "sudo needs a password, and there is no terminal to ask for it on.
  Run 'sudo -v' first, then re-run this script."
    fi
fi

case "$(uname -m)" in
    x86_64|amd64) ;;
    *) die "$(uname -m) is not supported. The images are built linux/amd64 only;
  an arm64 port of the six images is real work, not a flag." ;;
esac

# Read in a SUBSHELL, never sourced into this one. /etc/os-release defines
# VERSION — sourcing it silently overwrites the release being installed, and the
# first visible symptom is a 404 fetching a tag named "24.04.4 LTS (Noble
# Numbat)". Anything sourced here can shadow a variable this script owns.
if [ -r /etc/os-release ]; then
    os_id="$(. /etc/os-release; printf '%s' "${ID:-}")"
    os_ver="$(. /etc/os-release; printf '%s' "${VERSION_ID:-}")"
    os_name="$(. /etc/os-release; printf '%s' "${PRETTY_NAME:-this OS}")"
    case "$os_id:$os_ver" in
        ubuntu:22.04|ubuntu:24.04) ;;
        ubuntu:*|debian:*) warn "$os_name is untested. Ubuntu 22.04 and 24.04 are what CI and the cloud image use." ;;
        *) die "$os_name is not supported. Steward targets Ubuntu 22.04 or 24.04." ;;
    esac
else
    warn "cannot identify this OS; continuing anyway."
fi

# Memory. The tool sandbox alone is allowed 5 GiB, so 4 GB is not a floor that
# merely performs badly — the first real tool call is an OOM candidate.
mem_kb="$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
# Rounded, not truncated. MemTotal is always short of the advertised size — the
# firmware and the kernel take their cut before /proc/meminfo is written — so an
# 8GB e2-standard-2 reports ~7.76GiB. Truncating that to 7 made this script warn
# about the exact machine it recommends, and reject a 6GB box for having 5.
mem_gb=$(( (mem_kb + 524288) / 1048576 ))
if [ "$mem_gb" -gt 0 ]; then
    [ "$mem_gb" -ge 6 ] || die "${mem_gb}GB of RAM. Steward needs 6GB to start and 8GB to work:
  the tool sandbox is allowed 5GiB on its own. On GCP use e2-standard-2."
    [ "$mem_gb" -ge 8 ] || warn "${mem_gb}GB of RAM. It will run, but a tool call and a heavy page at the same moment will swap."
fi

# --- docker ------------------------------------------------------------------
# Docker's own apt repo. Factored out because two things below need it — the
# engine, and the compose plugin on a box that already had the engine from
# somewhere else. Idempotent; re-adding it is a no-op.
add_docker_repo() {
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -qq
}

if ! command -v docker >/dev/null 2>&1; then
    if [ "$INSTALL_DOCKER" != "1" ]; then
        reply="$(ask "Docker is not installed. Install it from Docker's official apt repo? [y/N] " || true)"
        case "$reply" in
            [yY]*) INSTALL_DOCKER=1 ;;
            *) die "Docker is required. Install it and re-run, or re-run with STEWARD_INSTALL_DOCKER=1." ;;
        esac
    fi
    step "Installing Docker"
    add_docker_repo
    sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

# Compose v2 is a separate package from the engine, so "Docker is installed"
# does not imply it is here. Ubuntu's own `docker.io` package and the Docker
# snap both leave you with an engine and no `docker compose`. The branch above
# installs the plugin alongside the engine; this is the other case — Docker was
# already present and the plugin was not — which earlier versions of this script
# diagnosed correctly and then left the operator to solve unaided.
if ! docker compose version >/dev/null 2>&1; then
    if command -v docker-compose >/dev/null 2>&1; then
        warn "found docker-compose (v1). Steward needs the v2 'docker compose' subcommand;"
        warn "v1 is end-of-life and does not understand this stack file."
    fi
    if [ "$INSTALL_DOCKER" != "1" ]; then
        reply="$(ask "Docker is here but the compose v2 plugin is not. Install docker-compose-plugin? [y/N] " || true)"
        case "$reply" in
            [yY]*) ;;
            *) die "docker compose v2 is required (the 'docker compose' subcommand, not docker-compose).
  Install it with:  sudo apt-get install -y docker-compose-plugin
  If apt cannot find that package, Docker's own repo is not configured on this
  box; see https://docs.docker.com/engine/install/ubuntu/
  If Docker came from a snap, apt cannot fix it — reinstall from Docker's repo." ;;
        esac
    fi
    step "Installing the compose v2 plugin"
    # Try the plugin alone first: on a box that already has Docker's repo this
    # avoids rewriting the apt source, and on a box that does not it fails
    # harmlessly and the repo is added before retrying.
    sudo apt-get install -y -qq docker-compose-plugin 2>/dev/null || {
        add_docker_repo
        sudo apt-get install -y -qq docker-compose-plugin \
            || die "could not install docker-compose-plugin from apt.
  If Docker came from a snap, apt cannot add the plugin to it — reinstall Docker
  from https://docs.docker.com/engine/install/ubuntu/ and re-run."
    }
    docker compose version >/dev/null 2>&1 \
        || die "docker-compose-plugin was installed but 'docker compose' still does not run.
  This usually means the docker CLI in use is a snap, which does not load apt
  plugins. Reinstall Docker from https://docs.docker.com/engine/install/ubuntu/"
    say "  compose v2 installed: $(docker compose version 2>/dev/null | head -1)"
fi

# Talking to the docker socket needs the 'docker' group, and a group added to
# your account does not apply to a shell that already exists. The script used to
# stop here and ask the operator to log out and start the install over. It does
# not need to: `sg` runs a command with a group applied without a new login, so
# the script re-enters ITSELF and carries on.
if ! docker info >/dev/null 2>&1; then
    if [ -n "${STEWARD_REEXEC:-}" ]; then
        die "the docker daemon is not reachable even with the 'docker' group applied.
  Check it is running:  sudo systemctl status docker
  Then re-run this script."
    fi

    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
        say "  adding $USER to the 'docker' group"
        sudo usermod -aG docker "$USER"
    else
        say "  this shell predates your 'docker' group membership"
    fi

    if ! command -v sg >/dev/null 2>&1; then
        die "added $USER to the 'docker' group, and 'sg' is not available to apply it here.
  Log out and back in (or run 'newgrp docker'), then re-run this script."
    fi

    step "Re-entering with the 'docker' group applied"

    # The script has to exist as a FILE to be re-run, and under `curl | bash` it
    # does not — stdin is the script and has already been consumed. Copy it if it
    # came from a file, otherwise fetch the same copy again.
    self="$(mktemp)"
    if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]:-}" ]; then
        cp "${BASH_SOURCE[0]}" "$self"
    else
        curl -fsSL "$SELF_URL" -o "$self" \
            || die "could not re-fetch the installer from $SELF_URL to continue.
  Log out and back in, then re-run this script."
    fi

    # Rebuild the original argument list, quoted, because `sg -c` takes a single
    # string. Anything answered interactively so far was a prompt, not an
    # argument, so nothing is lost by starting the checks again — they are all
    # read-only up to this point.
    qargs=""
    if [ "${#ORIG_ARGS[@]}" -gt 0 ]; then
        for a in "${ORIG_ARGS[@]}"; do qargs="$qargs $(printf '%q' "$a")"; done
    fi

    export STEWARD_REEXEC=1
    exec sg docker -c "bash $(printf '%q' "$self")$qargs"
fi

# Disk, measured where docker actually writes rather than on /. ~13GB of images
# plus the tool sandbox base, and room to pull an upgrade beside them.
docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
avail_gb="$(df -BG --output=avail "$docker_root" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)"
if [ -n "$avail_gb" ] && [ "$avail_gb" -gt 0 ]; then
    [ "$avail_gb" -ge 25 ] || die "${avail_gb}GB free on $docker_root. Building the images needs about 13GB
  of layers plus the build cache that produces them.
  Give this filesystem 40GB, or point Docker's data-root at a larger disk."
    [ "$avail_gb" -ge 40 ] || warn "${avail_gb}GB free on $docker_root. That is enough to build if nothing else
     is competing for the disk, but 40GB is the size this is tested at."
fi

# Ports, checked before anything is written. A port already answering is almost
# always a previous install still running, and 'up' would adopt half of it.
busy=""
for p in 8642 9119 9120 8020 9121; do
    if (command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$p ") ||
       (command -v netstat >/dev/null 2>&1 && netstat -ltn 2>/dev/null | grep -q ":$p "); then
        busy="$busy $p"
    fi
done
[ -z "$busy" ] || die "these ports are already in use:$busy

  If you are UPGRADING an existing Steward, this is the wrong script. Use:
    $STEWARD_HOME/hermes-update --to vX.Y.Z --dry-run
  It snapshots the data disk and runs pending migrations; this one does neither,
  so installing over a running deployment moves the images forward and leaves
  the data disk behind.

  If it is something else on these ports, or a Steward you want gone:
    docker compose -f $STACK_FILE --env-file $ENV_FILE down"

# --- credentials -------------------------------------------------------------
step "Credentials"

if [ -z "$ANTHROPIC_KEY" ]; then
    ANTHROPIC_KEY="$(ask 'Anthropic API key (starts sk-ant-, leave blank to fill in later): ' silent || true)"
fi

if [ -n "$ANTHROPIC_KEY" ]; then
    case "$ANTHROPIC_KEY" in
        sk-ant-*) ;;
        *) warn "that does not look like an Anthropic key (they start 'sk-ant-'). Using it anyway." ;;
    esac
    # Fail in two seconds on a typo rather than at the first workflow run, where
    # the error arrives as an unhelpful 401 inside a container log.
    if command -v curl >/dev/null 2>&1; then
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
            -H "x-api-key: $ANTHROPIC_KEY" -H "anthropic-version: 2023-06-01" \
            https://api.anthropic.com/v1/models 2>/dev/null || echo 000)"
        case "$code" in
            200) say "  key accepted by the Anthropic API" ;;
            401|403) die "the Anthropic API rejected that key ($code). Check it and re-run." ;;
            000) warn "could not reach the Anthropic API to check the key; continuing." ;;
            *)   warn "unexpected response checking the key (HTTP $code); continuing." ;;
        esac
    fi
else
    warn "no Anthropic key given. The stack will be written but NOT started."
fi

# No registry credentials are asked for anywhere in this script. Steward's
# images are built on this box from the source tree fetched below, and every
# base image they build FROM is public.

# --- layout ------------------------------------------------------------------
step "Creating $STEWARD_HOME"

# Owned by the invoking user, which is the pair every service is handed. sudo
# only to create it, never to write into it afterwards.
sudo install -d -o "$(id -u)" -g "$(id -g)" -m 0755 "$STEWARD_HOME"
mkdir -p "$STACK_DIR" "$DATA_DIR"
say "  $STACK_DIR   compose file and .env"
say "  $DATA_DIR    all state — config, databases, memory"

# --- source ------------------------------------------------------------------
step "Fetching Steward $VERSION"

# The source tree is a deliverable here, not a build-time scratch copy: this
# install BUILDS its images rather than pulling them, so the repo has to land on
# the box and stay there. The rendered stack file records build contexts that
# point into it, and an upgrade rebuilds from those same paths.
#
# A tag, never a branch. Two people reporting the same bug have to be running the
# same bits, and `main` cannot promise that. Overridable so a release candidate
# can be tested before it is published, or an air-gapped box can point at a
# mirror.
base="${STEWARD_BASE_URL:-https://codeload.github.com/$STEWARD_REPO/tar.gz}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -fsSL "$base/$VERSION" -o "$tmp/src.tar.gz" \
    || die "could not download $base/$VERSION
  Check that $VERSION is a published tag of $STEWARD_REPO."

# A tarball rather than `git clone`: one fewer dependency on a fresh box, and
# nothing here needs history. Extracted beside the target and swapped in, so an
# interrupted download cannot leave a half-tree that a later build reads from.
rm -rf "$SRC_DIR.new" "$SRC_DIR.old"
mkdir -p "$SRC_DIR.new"
tar -xzf "$tmp/src.tar.gz" -C "$SRC_DIR.new" --strip-components=1 \
    || die "the downloaded archive did not extract"
[ -f "$SRC_DIR.new/docker/docker-compose.yml" ] \
    || die "that archive does not look like the Steward source tree"
[ -d "$SRC_DIR" ] && mv "$SRC_DIR" "$SRC_DIR.old"
mv "$SRC_DIR.new" "$SRC_DIR"
rm -rf "$SRC_DIR.old"
say "  source at $SRC_DIR"

install -m 0755 "$SRC_DIR/hermes-update.sh" "$STEWARD_HOME/hermes-update"

# --- .env --------------------------------------------------------------------
step "Generating this install's secrets"

gen() {
    if command -v openssl >/dev/null 2>&1; then openssl rand -hex "$1"
    else head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}
gen_b64() {
    if command -v openssl >/dev/null 2>&1; then openssl rand -base64 32
    else head -c 32 /dev/urandom | base64 | tr -d '\n'; fi
}

API_SERVER_KEY="$(gen 32)"
DASH_PASSWORD="$(gen 12)"
DASH_SECRET="$(gen_b64)"

# The console checks the browser's Origin header against an allowlist on every
# write route, and a missing entry is a 403 with nothing in the UI to explain
# it — the buttons simply stop working. Loopback alone is therefore wrong the
# moment anyone reaches this box over a tailnet, which is exactly what the
# cloud path tells them to do.
#
# So detect the tailnet name and allow it. Both schemes: `tailscale serve`
# falls back to plain HTTP when tailnet-wide HTTPS certificates are not
# enabled, and which one you get is not knowable from here.
ORIGINS="http://127.0.0.1:9120,http://localhost:9120"
if command -v tailscale >/dev/null 2>&1; then
    ts_name="$(tailscale status --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
    if [ -n "${ts_name:-}" ]; then
        ORIGINS="$ORIGINS,https://$ts_name,http://$ts_name"
        say "  console will also accept requests from $ts_name"
    fi
fi

# Written before the stack starts, and never regenerated on a re-run: rotating
# API_SERVER_KEY under a running stack desyncs the three services that share it,
# which presents as a console that has lost the gateway rather than as a
# credential problem.
if [ -e "$ENV_FILE" ]; then
    warn "$ENV_FILE already exists — keeping it, and keeping its secrets."
    warn "Delete it and re-run if you want a fresh set."

    # One exception to keeping it verbatim. An install that stopped because no
    # key was given writes a keyless .env; on the re-run the operator is asked
    # for the key again, and keeping the file verbatim would throw that answer
    # away and stop for the identical reason. Forever. Only ever fills a blank —
    # a key already in the file always wins, so this cannot overwrite one.
    if [ -n "$ANTHROPIC_KEY" ] &&
       [ -z "$(sed -n 's/^ANTHROPIC_API_KEY=//p' "$ENV_FILE" | tail -1)" ]; then
        env_tmp="$(mktemp)"
        grep -v '^ANTHROPIC_API_KEY=' "$ENV_FILE" > "$env_tmp" || true
        printf 'ANTHROPIC_API_KEY=%s\n' "$ANTHROPIC_KEY" >> "$env_tmp"
        # Copied through, not moved over: `mv` would replace the file and take
        # its 0600 with it.
        cat "$env_tmp" > "$ENV_FILE"
        rm -f "$env_tmp"
        say "  filled in the blank ANTHROPIC_API_KEY"
    fi
else
    umask 077
    cat > "$ENV_FILE" <<ENVEOF
# Written by install.sh for Steward $VERSION. Mode 0600 — it holds credentials.
#
# Everything here is per-install. Do not copy this file to another machine:
# API_SERVER_KEY and the dashboard secret identify THIS deployment, and sharing
# them means either box can act as the other.

IMAGE_TAG=$VERSION
GITHUB_REPOSITORY=$GHCR_REPO
COMPOSE_NETWORK_NAME=steward_net

# Where this stack lives on the host. The console reads it only to print the
# upgrade command in Settings -> About; it has no way to run it.
STEWARD_HOME=$STEWARD_HOME
HERMES_DATA_DIR=$DATA_DIR
APPROVALS_DIR=$DATA_DIR/approvals
GMAIL_SECRETS_DIR=$DATA_DIR/secrets
HERMES_UID=$(id -u)
HERMES_GID=$(id -g)

ANTHROPIC_API_KEY=$ANTHROPIC_KEY
WORKFLOWS_MODEL_PROVIDER=anthropic
WORKFLOWS_DAILY_COST_CAP_USD=10

API_SERVER_KEY=$API_SERVER_KEY
HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=$DASH_PASSWORD
HERMES_DASHBOARD_BASIC_AUTH_SECRET=$DASH_SECRET

# Loopback, and it should stay loopback: the console holds API_SERVER_KEY, so
# anything that can reach it can act as the agent. Reach it with an SSH tunnel
# or 'tailscale serve --bg 9120'.
DASHBOARD_BIND=127.0.0.1
DOCS_BIND=127.0.0.1
DASHBOARD_ALLOWED_ORIGINS=$ORIGINS

# Empty on purpose — both have compose defaults that are wrong here. See
# docker/.env.bare.example in the release notes for why.
REVIEW_CAPABILITIES=
BROWSER_URL=
ENVEOF
    chmod 600 "$ENV_FILE"
    say "  $ENV_FILE written (0600)"
fi

# Re-read whatever is authoritative, so a kept .env wins over the values just
# generated and the summary at the end tells the truth.
#
# Parsed, not sourced. A .env is not a shell script: an API key or a base64
# secret containing (, ), $ or a space is perfectly valid here and a syntax
# error there, and `. "$ENV_FILE"` would fail on the operator's own file after
# they edited it by hand. This also keeps a hand-edited .env from executing
# anything.
env_get() {
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -1
}
ANTHROPIC_API_KEY="$(env_get ANTHROPIC_API_KEY)"
DASH_PASSWORD="$(env_get HERMES_DASHBOARD_BASIC_AUTH_PASSWORD)"
IMAGE_TAG="$(env_get IMAGE_TAG)"

# --- render ------------------------------------------------------------------
step "Rendering the stack"

# Rendered AFTER .env, and INTERPOLATED, both for the same reason.
#
# `config --no-interpolate` leaves "${HERMES_DATA_DIR-/srv/steward/data}" as
# literal text in the source of every bind mount. Compose cannot tell that a
# string starting with `$` is a path, so it types all 24 of them as named
# volumes, and then refuses the whole file at `up`:
#
#     service "light-dashboard" refers to undefined volume /srv/steward/data:
#     invalid compose project
#
# Writing them in long form (type: bind) does not fix it either — an
# uninterpolated source does not start with `/`, so compose treats it as
# relative and prefixes the directory it rendered in. Interpolating is the only
# form that produces a correct absolute path, and it needs .env to exist.
#
# The cost is that this file now carries the same secrets .env does, so it gets
# the same 0600. It was 0644 while it held nothing but placeholders.
( cd "$SRC_DIR/docker" && docker compose --env-file "$ENV_FILE" \
    -f docker-compose.yml \
    -f docker-compose.deploy.yml \
    -f docker-compose.standalone.yml \
    -f docker-compose.source.yml \
    config ) > "$tmp/steward-stack.yml" \
    || die "could not render the compose stack from $SRC_DIR"

# Rendering something compose cannot then read back is the failure this whole
# comment is about, so it is checked here rather than discovered at `up`.
docker compose -f "$tmp/steward-stack.yml" config -q >/dev/null 2>&1 \
    || die "the rendered stack is not a valid compose project. This is a bug;
  please report it with the output of:
    cd $SRC_DIR/docker && docker compose --env-file $ENV_FILE -f docker-compose.yml -f docker-compose.deploy.yml -f docker-compose.standalone.yml -f docker-compose.source.yml config"

install -m 0600 "$tmp/steward-stack.yml" "$STACK_FILE"
say "  $STACK_FILE (0600 — it carries this install's secrets)"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    # Build and start anyway. Every healthcheck this install waits on is
    # model-free -- /health, /list-apps, /api/health/services and a heartbeat
    # file -- so the stack comes up perfectly well without a key. What it cannot
    # do is any actual work.
    #
    # Stopping here used to seem like the honest choice, but it left the operator
    # with a directory of files and no way in: the console, which is where you
    # add the key and configure everything else, is itself one of the services
    # that never started. That is a worse failure than a running console that
    # tells you what is missing. The warning at the end is what keeps it honest.
    NO_KEY=1
    warn "no Anthropic key. Installing and starting anyway — the console will"
    warn "come up, but nothing that calls a model can run until you add one."
fi

# --- build and start ---------------------------------------------------------
step "Building images — this is the slow part (20-40 min on a 2 vCPU box)"

# No `docker login` anywhere. Every base image in these Dockerfiles is public,
# so the build needs no credentials, which is the whole point of this path.
docker compose -f "$STACK_FILE" --env-file "$ENV_FILE" build

step "Starting Steward"
docker compose -f "$STACK_FILE" --env-file "$ENV_FILE" up -d

# --- wait --------------------------------------------------------------------
# `up -d` returning means the containers were CREATED. These four declare
# healthchecks precisely so this wait is a real check rather than a pause.
step "Waiting for services to come up"
WAIT_SERVICES="hermes-gateway workflows light-dashboard review-executor"
deadline=$(( $(date +%s) + 600 ))
while :; do
    pending=""
    for svc in $WAIT_SERVICES; do
        cid="$(docker compose -f "$STACK_FILE" --env-file "$ENV_FILE" ps -q "$svc" 2>/dev/null || true)"
        if [ -z "$cid" ]; then pending="$pending $svc"; continue; fi
        state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)"
        case "$state" in
            healthy|running) ;;
            *) pending="$pending $svc" ;;
        esac
    done
    [ -n "$pending" ] || break
    if [ "$(date +%s)" -ge "$deadline" ]; then
        say ""
        warn "still not healthy after 10 minutes:$pending"
        for svc in $pending; do
            say ""
            say "--- $svc ---"
            docker compose -f "$STACK_FILE" --env-file "$ENV_FILE" logs --tail 50 "$svc" >&2 || true
        done
        die "Steward did not come up. The logs above are the place to start."
    fi
    sleep 5
done
say "  all services healthy"

# --- done --------------------------------------------------------------------
cat >&2 <<DONEEOF

${B}Steward $IMAGE_TAG is running.${R}

  Console    http://127.0.0.1:9120        (no login — see below)
  Hermes UI  http://127.0.0.1:9119        admin / $DASH_PASSWORD

The console has NO authentication of its own. That is survivable only because
it is bound to loopback: anything that can reach port 9120 can approve a review
— which sends mail — and can read this deployment's gateway key. Never answer
"I cannot reach the console" by rebinding it to 0.0.0.0. How to reach it
properly is the last section below.

  Config     $ENV_FILE
  Source     $SRC_DIR   (build contexts point here — do not move it)
  State      $DATA_DIR
  Version    $IMAGE_TAG   (pinned — 'latest' is not what you are running)

  Upgrade    $STEWARD_HOME/hermes-update --to vX.Y.Z --dry-run   (--to is required;
             releases: https://github.com/$STEWARD_REPO/releases)
  Stop       docker compose -f $STACK_FILE --env-file $ENV_FILE down
  Logs       docker compose -f $STACK_FILE --env-file $ENV_FILE logs -f
  Uninstall  docker compose -f $STACK_FILE --env-file $ENV_FILE down -v && sudo rm -rf $STEWARD_HOME

Next: open the console, and run the smoke test in the README to confirm the
gateway is answering and a workflow completes end to end.
DONEEOF

# Last thing on screen when there is no key, because it is the one thing between
# this box and a working Steward. Deliberately after the summary rather than
# inside it: the summary says what is running, and all of that IS running.
if [ "$NO_KEY" = "1" ]; then
    cat >&2 <<NOKEYEOF

${B}One thing is missing: the Anthropic API key.${R}

Every service above is up and healthy, and none of them can do any work. The
healthchecks do not call a model, so a keyless Steward looks exactly like a
working one until the first job runs and fails.

  1. Add it:    ${B}\$EDITOR $ENV_FILE${R}    (the ANTHROPIC_API_KEY= line)
  2. Apply it:  ${B}docker compose -f $STACK_FILE --env-file $ENV_FILE up -d${R}

Step 2 is not optional. The services read the key from their environment when
they start, so editing .env on its own changes nothing that is already running.

Steward calls Anthropic because that is what ships in hermes/config/model-aliases.yaml,
not because it has to: WORKFLOWS_MODEL_PROVIDER also accepts 'gemini' and 'ollama'.
NOKEYEOF
fi

# --- reaching the console ----------------------------------------------------
# Printed last, and last on purpose: everything above is about a box that is now
# running, and this is the only part the operator has to act on to see it.
#
# The console is bound to loopback and has no login of its own, so "open it in a
# browser" is not one step, it is a decision about how to cross the network. A
# tailnet is the answer this recommends because it is the only one that does not
# involve either exposing an unauthenticated console or keeping an ssh tunnel
# alive by hand.
#
# The state of tailscale on this box decides which instructions are useful, so
# they are chosen rather than all printed at once.
ts_state="absent"
if command -v tailscale >/dev/null 2>&1; then
    if tailscale status >/dev/null 2>&1; then ts_state="up"; else ts_state="loggedout"; fi
fi

# MagicDNS name of this node, which is the URL the operator ultimately opens.
# Empty unless tailscale is up; the trailing dot that the API returns is not
# part of a URL.
ts_host=""
if [ "$ts_state" = "up" ]; then
    # --peers=false so the only DNSName in the document is this node's. Without
    # it a tailnet with peers puts theirs in the same output.
    ts_host="$(tailscale status --json --peers=false 2>/dev/null \
        | sed -n 's/.*"DNSName"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -1 | sed 's/\.$//')"
fi

printf '\n%s================ Opening the console ================%s\n' "$B" "$R" >&2

case "$ts_state" in
absent)
    cat >&2 <<TSEOF

Tailscale is not installed here. It is the shortest safe path to the console:
it gives this box a private hostname reachable only by your own devices, with
no port forwarding and nothing exposed to the internet.

ON THIS MACHINE:

  curl -fsSL https://tailscale.com/install.sh | sh
  sudo tailscale up --hostname=steward

  The second command prints a login URL. Open it, and approve this machine.

ON THE MACHINE YOU BROWSE FROM (laptop, phone):

  Install Tailscale from https://tailscale.com/download and sign in with the
  SAME account. Both devices have to be on the same tailnet to see each other.

THEN, BACK ON THIS MACHINE:

  sudo tailscale serve --bg 9120

  and open  ${B}https://steward.<your-tailnet>.ts.net${R}

  \`tailscale status\` prints this machine's full name if you are unsure of the
  <your-tailnet> part.

If \`serve\` complains about certificates, tailnet HTTPS is off by default —
turn it on once at https://login.tailscale.com/admin/dns, or serve over plain
http inside the tailnet instead:

  sudo tailscale serve --bg --http=80 http://127.0.0.1:9120
TSEOF
    ;;
loggedout)
    cat >&2 <<TSEOF

Tailscale is installed here but not logged in.

ON THIS MACHINE:

  sudo tailscale up --hostname=steward
  sudo tailscale serve --bg 9120

  The first prints a login URL — open it and approve this machine.

ON THE MACHINE YOU BROWSE FROM:

  Install Tailscale from https://tailscale.com/download, sign in with the SAME
  account, then open the address \`tailscale status\` shows for this machine,
  on port 443:  https://steward.<your-tailnet>.ts.net
TSEOF
    ;;
up)
    if tailscale serve status 2>/dev/null | grep -q 9120; then
        cat >&2 <<TSEOF

Tailscale is up and already serving the console.

  ${B}https://${ts_host:-<this machine>}${R}

Open that from any device signed in to the same tailnet. Install Tailscale on
it first from https://tailscale.com/download if it is not already.
TSEOF
    else
        cat >&2 <<TSEOF

Tailscale is up on this machine. One command publishes the console to your
tailnet — and only to your tailnet:

  sudo tailscale serve --bg 9120

Then open:

  ${B}https://${ts_host:-<this machine>}${R}

from any device signed in to the same account. Install Tailscale on that device
first from https://tailscale.com/download if it is not already.

If \`serve\` complains about certificates, tailnet HTTPS is off by default —
enable it once at https://login.tailscale.com/admin/dns, or use plain http
inside the tailnet:  sudo tailscale serve --bg --http=80 http://127.0.0.1:9120
TSEOF
    fi
    ;;
esac

cat >&2 <<TSEOF

Whichever you use, the tailnet is now the boundary that the missing login on
:9120 would otherwise be. The default Tailscale ACL lets EVERY device on your
tailnet reach this box, so restrict it, and never enable Funnel on this node —
that would put an unauthenticated console on the public internet.

  https://login.tailscale.com/admin/acls

No tailnet? The console is still reachable over an ssh tunnel, which needs
nothing installed and forwards only to you:

  ssh -N -L 9120:127.0.0.1:9120 $(whoami)@<this-machine>

  then open http://127.0.0.1:9120 on your own machine.
TSEOF
