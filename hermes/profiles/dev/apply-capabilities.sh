#!/usr/bin/env bash
#
# Grant the dev profile the capabilities it needs to verify its own work.
#
# Not needed for a normal install or upgrade any more: hermes/seed.sh renders
# hermes/profiles/dev/config.yaml.template, which already carries both patches
# below. This script exists for the OTHER way to get a dev config -- cloning a
# live `default` on the host (`hermes profile create dev --clone-from
# default`) instead of taking the vendored template, e.g. to inherit a
# customized default's model choice or added toolsets. A clone starts from
# `default`'s current settings, which do not include these two, so re-apply
# them here. Only the handful of settings below differ from a stock clone or
# template, and each is idempotent.
#
#   1. platform_toolsets.api_server gains `terminal`.
#
#      Without it dev has no shell on any platform. It could write code and not
#      run it, so every task ended "an operator will run pytest" -- and it
#      shipped a test that had never passed, because it could not have known.
#      Granted on api_server only, which is the platform the kanban dispatcher
#      runs through; cron and cli still have no shell.
#
#   2. terminal.docker_image becomes the workflows runtime image.
#
#      The stock python-nodejs image mounts /opt/workflows fine but has none of
#      the dependencies -- no pytest, no google.adk, no /code/.venv -- so
#      `pytest tests/unit` died on import. Using the image the workflows
#      actually run in means dev tests in the environment its code executes in.
#      Tradeoff: that image has no node.
#
# Run after seeding and after cloning dev's config:
#
#     ./hermes/profiles/dev/apply-capabilities.sh
#
# Re-running is safe and prints what it changed.
set -euo pipefail

DATA_DIR="${HERMES_DATA_DIR:-$HOME/.hermes}"
CONFIG="$DATA_DIR/profiles/dev/config.yaml"
# Matches the fixed tag docker-compose.source.yml's `workflows` build adds
# alongside its versioned ghcr one -- see the comment there and in
# hermes/profiles/dev/config.yaml.template.
IMAGE="${DEV_TERMINAL_IMAGE:-steward-workflows:dev-sandbox}"

if [ ! -f "$CONFIG" ]; then
  echo "error: $CONFIG not found. Clone dev's config from default first (see README)." >&2
  exit 1
fi

CONFIG="$CONFIG" IMAGE="$IMAGE" python3 - <<'PY'
import datetime
import os
import shutil
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - depends on the host
    sys.exit("error: PyYAML is required (pip install pyyaml)")

path = os.environ["CONFIG"]
image = os.environ["IMAGE"]

with open(path) as fh:
    text = fh.read()
config = yaml.safe_load(text)

changed = []

# 1. terminal on api_server -- the platform the kanban dispatcher uses.
toolsets = (config.get("platform_toolsets") or {}).get("api_server")
if isinstance(toolsets, list) and "terminal" not in toolsets:
    toolsets.append("terminal")
    toolsets.sort()
    changed.append("platform_toolsets.api_server += terminal")

# 2. the sandbox image dev runs tests in.
terminal = config.setdefault("terminal", {})
if terminal.get("docker_image") != image:
    changed.append(f"terminal.docker_image: {terminal.get('docker_image')!r} -> {image!r}")
    terminal["docker_image"] = image

if not changed:
    print("  dev capabilities already applied; nothing to do")
    sys.exit(0)

# Backed up only when something is actually changing, so re-running does not
# litter the profile with identical copies.
stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
shutil.copy(path, f"{path}.bak.{stamp}")

# Rewritten wholesale, which drops comments from the generated config. That is
# why the reasoning lives in this script and in the repo, not in the file.
with open(path, "w") as fh:
    yaml.safe_dump(config, fh, sort_keys=False, width=100)

for line in changed:
    print(f"  applied {line}")
PY

echo
echo "Restart the gateway for this to reach new sessions:"
echo "  pkill -USR1 -f 'hermes gateway run'"
