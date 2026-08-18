"""Verify the rendered standalone stack needs nothing but images.

The customer-facing stack file is produced by rendering three compose files
together. Rendering succeeds whether or not the result is actually deployable:
`docker compose config` is happy to emit a `build:` block or a `../workflows`
bind mount, and both only fail on a box that has no repo — which is every box
except a developer's.

That failure is also silent in the worst way. A missing bind source is created
by docker as an empty root-owned directory, so the service starts, finds no
files, and reports itself healthy while doing nothing.

So this asserts the properties the rendered file must have, and it is meant to
run in CI on every change to any of the three inputs. Without it, the next
service someone adds with a bind mount breaks every customer's next upgrade and
nobody finds out until they upgrade.

Run: python3 infra/check_standalone.py path/to/hermes-stack.yml
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

# Values that ship as compose defaults today and must never reach a customer's
# rendered file. Each is a credential whose published default is worse than an
# absent one: it looks configured, so nothing prompts anyone to change it.
FORBIDDEN_DEFAULTS = (
    "some_long_secure_secret_key_here",
    "change-me",
)

# Images we do not build. Anything else must come from our registry, or the
# rendered file is pinned to something nobody publishes.
# Bind sources that are legitimately absolute host paths rather than something
# under the data directory. Each is a host facility, not stack state.
ALLOWED_ABSOLUTE_SOURCES = ("/var/run/docker.sock",)

# The variables a bind source may be rooted at. Anything else is either a path
# on the boot disk (lost when the VM is replaced) or a developer default that
# resolves somewhere unintended on a deployed box.
ALLOWED_SOURCE_ROOTS = (
    "${HERMES_DATA_DIR",
    "${APPROVALS_DIR",
    "${GMAIL_SECRETS_DIR",
    # An operator may keep their own agents outside the data disk — a checkout
    # they edit, say. It defaults under HERMES_DATA_DIR, and the fallback check
    # below is what holds that default honest.
    "${TENANT_AGENTS_DIR",
)

# Environment variables whose value is a path INSIDE a container that must be
# backed by a mount. The bug this catches shipped twice.
#
# Both times the shape was identical: the base compose file pairs an env var
# with the bind mount that gives it content, docker-compose.standalone.yml
# replaces that service's volumes with `!override`, and the pairing is broken
# without the variable changing at all. MODEL_ALIASES_PATH lost its mount and
# every deployed box silently fell back to built-in defaults;
# HERMES_AGENTS_PATH lost its mount and the documented way to add a custom
# workflow did nothing, on every box, for every release.
#
# Neither failed. That is what makes this worth a check rather than a habit —
# the service starts, finds nothing where it was told to look, treats "nothing"
# as a valid answer, and reports itself healthy.
PATH_ENV_NEEDS_MOUNT = (
    "HERMES_AGENTS_PATH",
    "MODEL_ALIASES_PATH",
)

# The data directory's own default. Every absolute fallback baked into a mount
# has to live under it, because that is the path install.sh mounts the data disk
# at — and the only path a replaced VM keeps.
DATA_ROOT_DEFAULT = "/srv/steward/data"

# Services that must answer for themselves. install.sh waits on compose status
# and reports the failing service; without health defined, "up" means only that
# the container was created, and the wait step becomes decoration.
#
# hermes-init is deliberately absent: it is a one-shot that must EXIT, and the
# thing depending on it already gates on service_completed_successfully.
REQUIRE_HEALTHCHECK = (
    "browser",
    "hermes-gateway",
    "light-dashboard",
    "review-executor",
    "workflows",
)

ALLOWED_FOREIGN_IMAGE_PREFIXES = (
    # The last one. Once the gateway is built from the fork and both
    # hermes-gateway and hermes-dashboard point at our own image, this tuple
    # should be empty and every image in the stack should come from our
    # registry.
    #
    # It used to admit seven more, for the telemetry stack and its backing
    # stores. Every one of those services has been deleted; leaving their
    # prefixes here meant this check would have silently accepted them coming
    # back.
    "nousresearch/hermes-agent",
)


def _paths_in(value: str) -> list[str]:
    """The container paths an env value resolves to, for the mount check.

    This file is rendered --no-interpolate, so the value is still the literal
    `${HERMES_AGENTS_PATH:-/code/agents_local}` rather than a path. What a box
    actually gets is the default baked into that expression, since none of
    these variables is set by install.sh — so the default is the thing to
    check, and splitting the raw string on ":" would tear the `:-` apart and
    check two halves of a variable name.

    A variable with no default resolves to empty, which is not a path and not
    something this file can verify; those return nothing rather than a guess.
    """
    value = value.strip()
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-(.*))?\}", value)
    if match:
        default = match.group(2)
        if default is None:
            return []
        value = default
    return [part for part in value.split(":") if part.startswith("/")]


def _is_host_path(source: str) -> bool:
    """True if this volume source is a path on the host rather than a named volume.

    Deliberately shape-based; see the call site for why the rendered `type:`
    field cannot be trusted in a --no-interpolate render.
    """
    return source.startswith((".", "/", "$")) or "/" in source


def check(path: pathlib.Path) -> list[str]:
    doc = yaml.safe_load(path.read_text())
    services = (doc or {}).get("services") or {}
    if not services:
        return [f"{path}: no services — did the render fail?"]

    problems: list[str] = []

    for name, svc in sorted(services.items()):
        svc = svc or {}

        if "build" in svc and svc["build"] is not None:
            problems.append(
                f"{name}: has a `build:` block. A deployed box has no source to "
                f"build from; add an `image:` in docker-compose.deploy.yml."
            )

        image = svc.get("image")
        if not image:
            problems.append(f"{name}: no `image:`. Nothing to pull.")
        elif not image.startswith("ghcr.io/") and not image.startswith(
            ALLOWED_FOREIGN_IMAGE_PREFIXES
        ):
            problems.append(
                f"{name}: image {image!r} is neither ours nor a known upstream. "
                f"If it is a new dependency, add it to "
                f"ALLOWED_FOREIGN_IMAGE_PREFIXES here."
            )

        for vol in svc.get("volumes") or []:
            # Rendered volumes are usually the long form; tolerate both.
            source = vol.get("source") if isinstance(vol, dict) else str(vol).split(":")[0]
            if not source:
                continue

            # Classify by SHAPE, never by the rendered `type:` field.
            #
            # This file is rendered --no-interpolate so the customer's .env still
            # drives it, and compose cannot tell what a `${HERMES_DATA_DIR:-...}`
            # will become — so it labels every one of them `type: volume`.
            # Trusting that field skips precisely the mounts worth checking, and
            # the checker passes a file with $HOME defaults and an off-disk
            # approvals queue still in it. It did.
            #
            # A genuine named volume is a bare identifier: no separator, no
            # variable, no leading dot.
            if not _is_host_path(source):
                continue
            if source.startswith("./") or source.startswith("../"):
                problems.append(
                    f"{name}: bind mount {source!r} is relative to the compose "
                    f"file's directory, which does not exist on a deployed box. "
                    f"Point it at a path under HERMES_DATA_DIR and have "
                    f"hermes-init populate it."
                )
            elif source in ALLOWED_ABSOLUTE_SOURCES:
                pass
            elif "${HOME" in source:
                # $HOME is whoever compose runs as, which on a deployed box is
                # not a decision anyone made. Worse, it fails OPEN: the mount
                # silently lands on the boot disk and the service reports
                # healthy against empty state.
                problems.append(
                    f"{name}: bind mount {source!r} falls back to $HOME. That "
                    f"default is for a developer's checkout; here it puts state "
                    f"on the boot disk under whatever user compose ran as. Root "
                    f"it at HERMES_DATA_DIR instead."
                )
            elif not source.startswith(ALLOWED_SOURCE_ROOTS):
                # Absolute paths pass the relative-path check above while still
                # being wrong: anything not under the data directory lives on
                # the boot disk and does not survive replacing the VM. The
                # approvals queue shipped this way.
                problems.append(
                    f"{name}: bind mount {source!r} is not rooted at "
                    f"{' or '.join(r + '}' for r in ALLOWED_SOURCE_ROOTS)}. "
                    f"Anything else is on the boot disk, so a rebuilt VM loses "
                    f"it while the data disk survives — silently, because an "
                    f"empty directory is valid state."
                )

            # The root being an approved variable is not enough — the fallback
            # baked INTO that variable is what a box with no .env entry actually
            # gets. `${APPROVALS_DIR:-/srv/steward/approvals}` passes every check
            # above and still puts the human-approval queue on the boot disk,
            # where replacing the VM destroys it and the data disk survives
            # looking perfectly intact. That is how it shipped.
            for fallback in re.findall(r":-([^:}]*)", source):
                if not fallback.startswith("/"):
                    continue
                if fallback == DATA_ROOT_DEFAULT or fallback.startswith(
                    DATA_ROOT_DEFAULT + "/"
                ):
                    continue
                if fallback in ALLOWED_ABSOLUTE_SOURCES:
                    continue
                problems.append(
                    f"{name}: bind mount {source!r} falls back to {fallback!r}, "
                    f"which is outside {DATA_ROOT_DEFAULT}. A box that does not "
                    f"set this variable puts that state on the boot disk, so "
                    f"rebuilding the VM loses it silently."
                )

        if name in REQUIRE_HEALTHCHECK and not svc.get("healthcheck"):
            problems.append(
                f"{name}: no healthcheck. install.sh waits on compose status "
                f"and prints the service that failed; with no health defined "
                f"this service is 'up' the moment its container exists, and "
                f"the wait proves nothing."
            )

        env = svc.get("environment") or {}
        # Rendered environment is a mapping; a list would mean --no-interpolate
        # was passed without `config` normalising, which is worth knowing.
        items = env.items() if isinstance(env, dict) else (
            (e.split("=", 1) + [""])[:2] for e in env
        )
        mount_targets = [
            (vol.get("target") if isinstance(vol, dict) else str(vol).split(":")[1])
            for vol in svc.get("volumes") or []
            if isinstance(vol, dict) or str(vol).count(":") >= 1
        ]

        for key, value in items:
            for bad in FORBIDDEN_DEFAULTS:
                if value and bad in str(value):
                    problems.append(
                        f"{name}: {key} still carries the placeholder {bad!r}. "
                        f"Remove the compose default and let install.sh generate "
                        f"it — a published placeholder is a published credential."
                    )

            if key in PATH_ENV_NEEDS_MOUNT and value:
                # Colon-separated, like PATH — HERMES_AGENTS_PATH is documented
                # that way, and a single path is just the one-element case.
                for wanted in _paths_in(str(value)):
                    wanted = wanted.strip()
                    if not wanted:
                        continue
                    covered = any(
                        wanted == target or wanted.startswith(target.rstrip("/") + "/")
                        for target in mount_targets
                        if target
                    )
                    if not covered:
                        problems.append(
                            f"{name}: {key}={wanted!r} names a path in the "
                            f"container, but nothing is mounted there. The "
                            f"service will read an empty or missing directory "
                            f"and carry on as if that were the answer. Add the "
                            f"mount to docker-compose.standalone.yml — a "
                            f"`volumes: !override` there replaces the base "
                            f"file's list rather than extending it, which is "
                            f"how this is lost every time."
                        )

    if "hermes-init" not in services:
        problems.append(
            "hermes-init is missing. Nothing would populate the data directory, "
            "and every relocated mount above would resolve to an empty root-owned "
            "directory."
        )

    return problems


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2

    path = pathlib.Path(sys.argv[1])
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    problems = check(path)
    if problems:
        print(f"{path}: {len(problems)} problem(s)\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"{path}: standalone-clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
