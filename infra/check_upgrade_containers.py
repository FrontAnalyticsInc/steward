#!/usr/bin/env python3
"""Simulate an upgrade that RENAMES containers, and prove nothing is left holding a port.

Run by CI's `checks` job and standalone:

    python3 infra/check_upgrade_containers.py

Why this exists
---------------
`container_name:` is not a label. It is a claim on a NAME, and — through the
container that holds it — on every port that container publishes. Rename one
and the upgrade has two stacks in play at once: the old containers are still
on the box under the old names when the new compose file is applied. If
`steward-console` is created while `hermes-light-dashboard` still holds :9120,
it does not start. That failure lands mid-upgrade, on a client's box, after the
stack is already down and the data disk has already been migrated — the worst
place on the whole path to discover it.

Nothing else in this repository can catch that. `docker compose config` renders
a file; it has no idea what is running. The backend suite never sees a
container. The property is only visible if you actually run an upgrade from one
set of names to another, so that is what this does: the REAL update.sh, against
a throwaway install, with a stub `docker` that keeps a container table and
enforces the two collisions docker enforces — duplicate name, duplicate port.

The stub forwards `docker compose ... config` to the real docker, because
rendering reads files and never contacts a daemon. Everything else is
simulated. No image is pulled, no container is created, no daemon is touched.

The check is written against the property, not against one rename: the "before"
stack is derived from the current one by substituting the legacy names back in,
so it keeps working the next time a container is renamed — add the pair to
RENAMES and it covers that too.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
UPDATE_SH = ROOT / "update.sh"
DOCKER_DIR = ROOT / "docker"

# The rename this release performs: new container name -> the name it replaced.
# Two services deliberately keep theirs (hermes-gateway, hermes-dashboard) and
# so are absent. steward-init also changed SERVICE name, which is what makes the
# old container an orphan rather than a same-service replacement.
RENAMES = {
    "steward-console": "hermes-light-dashboard",
    "adk-workflows": "hermes-workflows",
    "steward-review-executor": "hermes-review-executor",
    "steward-browser": "hermes-browser",
    "steward-docs": "hermes-docs",
    "steward-init": "hermes-init",
}
SERVICE_RENAMES = {"steward-init": "hermes-init"}

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


# --- the stub ----------------------------------------------------------------
# Kept as source text rather than a fixture file so the behaviour it simulates
# is readable beside the behaviour being tested.
DOCKER_STUB = r'''#!/usr/bin/env python3
"""A `docker` that keeps a container table instead of talking to a daemon.

Implements only what update.sh calls, and implements the two failures that
matter here for real: a name already in use, and a port already allocated.
`compose ... config` is delegated to the real docker, which renders files and
contacts nothing.
"""
import json, os, pathlib, subprocess, sys

STATE = pathlib.Path(os.environ["DOCKER_SIM_STATE"])
REAL = os.environ["DOCKER_SIM_REAL"]
LOG = pathlib.Path(os.environ["DOCKER_SIM_LOG"])


def load():
    return json.loads(STATE.read_text()) if STATE.exists() else []


def save(cs):
    STATE.write_text(json.dumps(cs, indent=2))


def log(msg):
    with LOG.open("a") as fh:
        fh.write(msg + "\n")


def parse_stack(path, project):
    """(service, container_name, [ports]) for every service in a rendered file."""
    import yaml
    doc = yaml.safe_load(pathlib.Path(path).read_text()) or {}
    name = doc.get("name") or project
    out = []
    for svc, body in (doc.get("services") or {}).items():
        body = body or {}
        cname = body.get("container_name") or f"{name}-{svc}-1"
        ports = []
        for p in body.get("ports") or []:
            if isinstance(p, dict):
                if p.get("published"):
                    ports.append(str(p["published"]))
            else:
                bits = str(p).split(":")
                if len(bits) >= 2:
                    ports.append(bits[-2])
        out.append((svc, cname, ports, name))
    return out


def compose(args):
    # -p and -f are what identify the project and the file.
    project, files = None, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-p":
            project = args[i + 1]; i += 2; continue
        if a == "-f":
            files.append(args[i + 1]); i += 2; continue
        if a in ("--env-file",):
            i += 2; continue
        i += 1
    verbs = [a for a in args if a in ("config", "build", "up", "down", "ps", "logs")]
    verb = verbs[0] if verbs else None

    if verb == "config":
        # Read-only, file-only. Let the real thing do it.
        return subprocess.run([REAL, "compose"] + args).returncode

    if verb in ("build", "logs"):
        return 0

    cs = load()

    if verb == "down":
        stack = parse_stack(files[-1], project) if files else []
        proj = project or (stack[0][3] if stack else "steward")
        services = {s for s, _, _, _ in stack}
        orphans = "--remove-orphans" in args
        kept, removed = [], []
        for c in cs:
            if c["project"] != proj:
                kept.append(c); continue
            # Real compose removes containers for the services in the file, and
            # only removes the rest -- orphans -- when asked to.
            if c["service"] in services or orphans:
                removed.append(c["name"])
            else:
                kept.append(c)
        save(kept)
        log(f"down project={proj} remove_orphans={orphans} removed={sorted(removed)}")
        return 0

    if verb == "up":
        stack = parse_stack(files[-1], project)
        proj = project or stack[0][3]
        names = {c["name"] for c in cs}
        ports = {p: c["name"] for c in cs for p in c["ports"]}
        for svc, cname, pl, _ in stack:
            if cname in names:
                sys.stderr.write(
                    f'Error response from daemon: Conflict. The container name "/{cname}" '
                    f'is already in use.\n')
                log(f"up FAILED name-collision {cname}")
                return 1
            for p in pl:
                if p in ports:
                    sys.stderr.write(
                        f"Error response from daemon: driver failed programming external "
                        f"connectivity: Bind for 0.0.0.0:{p} failed: port is already "
                        f"allocated (held by {ports[p]})\n")
                    log(f"up FAILED port-collision {p} held by {ports[p]}")
                    return 1
            cs.append({"name": cname, "service": svc, "project": proj,
                       "ports": pl, "status": "running"})
            names.add(cname)
            for p in pl:
                ports[p] = cname
        save(cs)
        log(f"up project={proj} created={sorted(c[1] for c in stack)}")
        return 0

    if verb == "ps":
        want = args[-1]
        for c in cs:
            if c["service"] == want and c["project"] == (project or c["project"]):
                print("sim-" + c["name"])
                return 0
        return 0

    return 0


def main(argv):
    if not argv:
        return 1
    if argv[0] == "compose":
        return compose(argv[1:])
    if argv[0] == "inspect":
        print("healthy")
        return 0
    if argv[0] == "run":
        return 0
    if argv[0] == "ps":
        # docker ps -aq --filter name=^NAME$
        want = None
        for a in argv:
            if a.startswith("name="):
                want = a[5:].strip("^$")
        for c in load():
            if c["name"] == want:
                print("sim-" + c["name"])
        return 0
    if argv[0] == "rm":
        names = [a for a in argv[1:] if not a.startswith("-")]
        cs = load()
        save([c for c in cs if c["name"] not in names])
        log(f"rm -f {names}")
        return 0
    sys.stderr.write(f"docker sim: unexpected call: {' '.join(argv)}\n")
    return 99


sys.exit(main(sys.argv[1:]))
'''


def render(files: list[str], out: pathlib.Path, profiles: str = "") -> None:
    """Render a stack the way install.sh and update.sh do: interpolated, both env files."""
    cmd = ["docker", "compose", "-p", "steward",
           "--env-file", str(DOCKER_DIR / "config.env.bare.example"),
           "--env-file", str(DOCKER_DIR / ".env.bare.example")]
    for f in files:
        cmd += ["-f", f]
    cmd += ["config"]
    r = subprocess.run(cmd, cwd=DOCKER_DIR, capture_output=True, text=True,
                       env=dict(os.environ, COMPOSE_PROFILES=profiles))
    if r.returncode != 0:
        sys.exit(f"could not render {files}: {r.stderr}")
    out.write_text(r.stdout)


STACK_FILES = ["docker-compose.yml", "docker-compose.deploy.yml",
               "docker-compose.standalone.yml", "docker-compose.source.yml"]


def legacy_stack(new_text: str) -> str:
    """The same stack as it was rendered BEFORE the rename."""
    old = new_text
    for new, was in RENAMES.items():
        old = old.replace(f"container_name: {new}", f"container_name: {was}")
    for new, was in SERVICE_RENAMES.items():
        old = old.replace(f"\n  {new}:\n", f"\n  {was}:\n")
        old = old.replace(f"      {new}:\n", f"      {was}:\n")
    return old


def sim_env(tmp: pathlib.Path, state: pathlib.Path, log: pathlib.Path) -> dict:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "docker"
    stub.write_text(DOCKER_STUB)
    stub.chmod(0o755)
    real = shutil.which("docker", path=os.environ["PATH"])
    if not real:
        sys.exit("no real docker on PATH; `docker compose config` is needed to render")
    return dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        DOCKER_SIM_STATE=str(state),
        DOCKER_SIM_REAL=real,
        DOCKER_SIM_LOG=str(log),
    )


def release_tarball(tmp: pathlib.Path, tag: str) -> str:
    """The target release, served over file:// — the source update.sh fetches."""
    codeload = tmp / "codeload"
    codeload.mkdir(exist_ok=True)
    src = tmp / "release-src"
    if src.exists():
        shutil.rmtree(src)
    (src / "docker").mkdir(parents=True)
    for f in STACK_FILES + ["config.env.bare.example", ".env.bare.example"]:
        shutil.copy(DOCKER_DIR / f, src / "docker" / f)
    shutil.copy(UPDATE_SH, src / "update.sh")
    subprocess.run(["tar", "-czf", str(codeload / tag), "-C", str(tmp), "release-src"],
                   check=True)
    return f"file://{codeload}"


def make_box(home: pathlib.Path, legacy: str, profiles: str = "") -> None:
    (home / "stack").mkdir(parents=True)
    (home / "data").mkdir()
    (home / "stack" / "steward-stack.yml").write_text(legacy)
    (home / "stack" / "config.env").write_text(
        f"IMAGE_TAG=v0.1.3\nHERMES_DATA_DIR={home}/data\nBROWSER_URL=\n"
        f"COMPOSE_PROFILES={profiles}\n")
    (home / "stack" / ".env").write_text("ANTHROPIC_API_KEY=sk-stub\n")
    (home / "data" / "config.yaml").write_text(
        "web:\n  backend: ''\n  search_backend: ddgs\n  extract_backend: ''\n")
    (home / "data" / ".steward-version").write_text(json.dumps({
        "seeded_version": "v0.1.3", "current_version": "v0.1.3",
        "last_migration": "0000", "available_migrations": [],
        "updated_at": "2026-01-01T00:00:00Z"}, indent=2))
    # The runner that is on the box today, under its old name.
    (home / "hermes-update").write_text("#!/bin/sh\nexit 0\n")
    (home / "hermes-update").chmod(0o755)


def containers(state: pathlib.Path) -> list[str]:
    return sorted(c["name"] for c in json.loads(state.read_text()))


def ports_held(state: pathlib.Path) -> dict:
    out: dict[str, list[str]] = {}
    for c in json.loads(state.read_text()):
        for p in c["ports"]:
            out.setdefault(p, []).append(c["name"])
    return out


def stack_names(text: str) -> list[str]:
    import yaml
    doc = yaml.safe_load(text)
    return sorted(b.get("container_name") for b in doc["services"].values())


def bring_up(env, state, stackfile, project="steward"):
    return subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(stackfile), "up", "-d"],
        env=env, capture_output=True, text=True)


# --- the cases ---------------------------------------------------------------

def case_crossing(tmp, new_text, legacy_text, label="default", profiles=""):
    """The upgrade that performs the rename. The whole point of this file."""
    print(f"\n  the crossing ({label} profile): old names running, new stack applied")
    home = tmp / f"box-crossing-{label}"
    state = tmp / f"state-crossing-{label}.json"
    log = tmp / f"log-crossing-{label}.txt"
    state.write_text("[]")
    make_box(home, legacy_text, profiles)
    env = sim_env(tmp, state, log)
    env["STEWARD_BASE_URL"] = release_tarball(tmp, "v0.1.4")

    up = bring_up(env, state, home / "stack" / "steward-stack.yml")
    check("the box starts on the old names", up.returncode, 0)
    before = containers(state)
    check("before: old container set", before, stack_names(legacy_text))
    print(f"        before: {before}")

    r = subprocess.run(["bash", str(UPDATE_SH), "--home", str(home), "--to", "v0.1.4"],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print("        ---- runner output ----")
        print("\n".join("        " + ln for ln in r.stderr.splitlines()[-40:]))
    check("the upgrade exits 0", r.returncode, 0)

    after = containers(state)
    print(f"        after:  {after}")
    check("after: exactly the new container set", after, stack_names(new_text))
    check("no legacy name survives",
          sorted(n for n in after if n in set(RENAMES.values())), [])
    dupes = {p: n for p, n in ports_held(state).items() if len(n) > 1}
    check("every published port is held by exactly one container", dupes, {})
    check("the runner installed itself as `update`", (home / "update").exists(), True)
    check("and removed `hermes-update`", (home / "hermes-update").exists(), False)
    check("`down --remove-orphans` is what took the renamed init container",
          "removed=" in log.read_text() and "hermes-init" in log.read_text(), True)


def case_stale_survivor(tmp, new_text, legacy_text):
    """A legacy container `down` does not match, because it is not in the project.

    Not hypothetical. An interrupted upgrade, a container an operator started by
    hand, a stack brought up under a different project name: all leave a
    container that carries the name and the port and none of the labels compose
    matches on. Before the guard this was an `up` failure after the migrations.
    """
    print("\n  a legacy container `down` cannot see (foreign project)")
    home = tmp / "box-stale"
    state = tmp / "state-stale.json"
    log = tmp / "log-stale.txt"
    state.write_text("[]")
    make_box(home, legacy_text)
    env = sim_env(tmp, state, log)
    env["STEWARD_BASE_URL"] = release_tarball(tmp, "v0.1.4")

    bring_up(env, state, home / "stack" / "steward-stack.yml")
    # Re-label the console's container into a project `down` will not touch.
    cs = json.loads(state.read_text())
    for c in cs:
        if c["name"] == "hermes-light-dashboard":
            c["project"] = "leftover"
    state.write_text(json.dumps(cs, indent=2))

    # Control: with that container still there, `up` on the new stack cannot bind.
    control = tmp / "state-control.json"
    control.write_text(json.dumps(
        [c for c in cs if c["name"] == "hermes-light-dashboard"], indent=2))
    cenv = sim_env(tmp, control, tmp / "log-control.txt")
    newfile = tmp / "new-stack.yml"
    newfile.write_text(new_text)
    ctl = bring_up(cenv, control, newfile)
    check("control: the collision is real — `up` fails on :9120", ctl.returncode, 1)
    check("control: and says why", "port is already allocated" in ctl.stderr, True)
    print(f"        {ctl.stderr.strip().splitlines()[0][:120]}")

    r = subprocess.run(["bash", str(UPDATE_SH), "--home", str(home), "--to", "v0.1.4"],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print("\n".join("        " + ln for ln in r.stderr.splitlines()[-40:]))
    check("the guard clears it and the upgrade exits 0", r.returncode, 0)
    check("the runner said so", "containers renamed in this release are still present"
          in r.stderr, True)
    after = containers(state)
    print(f"        after:  {after}")
    check("after: exactly the new container set", after, stack_names(new_text))
    check("nothing legacy left", [n for n in after if n in set(RENAMES.values())], [])


def main() -> int:
    print("upgrade across a container rename")
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        new = tmp / "rendered-new.yml"
        render(STACK_FILES, new)
        new_text = new.read_text()
        legacy_text = legacy_stack(new_text)
        if legacy_text == new_text:
            sys.exit("the legacy substitution changed nothing — RENAMES is stale")
        case_crossing(tmp, new_text, legacy_text)
        case_stale_survivor(tmp, new_text, legacy_text)

        # The renderer is profile-gated, so the default render never sees it —
        # and hermes-browser -> steward-browser would go unexercised on a box
        # that has it switched on, which is exactly the box that publishes an
        # extra port. Render it in and cross again.
        withbrowser = tmp / "rendered-browser.yml"
        render(STACK_FILES, withbrowser, profiles="browser")
        wb_text = withbrowser.read_text()
        if "steward-browser" not in wb_text:
            sys.exit("the browser profile rendered without the renderer")
        case_crossing(tmp, wb_text, legacy_stack(wb_text), label="browser",
                      profiles="browser")

    print()
    if failures:
        print(f"{len(failures)} failure(s): {failures}")
        return 1
    print("upgrade-containers-clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
