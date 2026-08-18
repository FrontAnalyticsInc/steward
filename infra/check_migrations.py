#!/usr/bin/env python3
"""Execute the upgrade machinery against a temporary data disk.

Run by CI's `checks` job and standalone:

    python3 infra/check_migrations.py

Why this exists as an executed test rather than a review: for the whole of
v0.1.0 there are zero migration files, so every path here returns "nothing
pending" — which is indistinguishable from a runner that is silently broken.
The first release that ships a migration is the worst possible time to discover
that `pending_ids` compares wrong or that a failure does not halt the run. So
these cases synthesise migration files and check the behaviour now.

It runs the REAL scripts. `migrate.sh` hardcodes the container paths
(/opt/data, /opt/migrations, /usr/local/lib/steward-marker.sh) because in the
image they are correct and a configurable path is one more thing to get wrong;
those three lines are rewritten into a tmpdir here. Nothing else is modified —
if the substitution stops matching, the rewrite raises rather than testing a
script that no longer resembles the shipped one.

Not pytest: this repo's two other structural checks (check_standalone.py,
check_cloud_init.py) are plain scripts that CI runs directly, and adding a
pytest rootdir at the repo root would collide with the one under
docker/light-dashboard.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
INIT = ROOT / "docker" / "hermes-init"
MARKER_SH = INIT / "marker.sh"
MIGRATE_SH = INIT / "migrate.sh"
UPDATE_SH = ROOT / "hermes-update.sh"

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def install(tmp: pathlib.Path) -> pathlib.Path:
    """Copy the real scripts into tmp, repointing only the container paths."""
    (tmp / "lib").mkdir()
    (tmp / "data").mkdir()
    (tmp / "migrations").mkdir()
    (tmp / "lib" / "steward-marker.sh").write_text(MARKER_SH.read_text())

    src = MIGRATE_SH.read_text()
    for old, new in (
        ("DATA_DIR=/opt/data", f"DATA_DIR={tmp}/data"),
        ("MIGRATIONS_DIR=/opt/migrations", f"MIGRATIONS_DIR={tmp}/migrations"),
        ("/usr/local/lib/steward-marker.sh", f"{tmp}/lib/steward-marker.sh"),
    ):
        if old not in src:
            raise SystemExit(
                f"check_migrations: {MIGRATE_SH} no longer contains {old!r}.\n"
                "  This test rewrites that line to run the script outside its "
                "image. Update the substitution rather than deleting the check."
            )
        src = src.replace(old, new)

    path = tmp / "hermes-migrate"
    path.write_text(src)
    path.chmod(0o755)
    return path


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def write_marker(tmp: pathlib.Path, seeded: str, current: str, last: str, ids: list[str]) -> None:
    """Through marker.sh's own writer, so the format under test is the real one."""
    run(
        [
            "bash",
            "-c",
            f'. "{tmp}/lib/steward-marker.sh"; '
            f'marker_write "{tmp}/data/.steward-version" "{seeded}" "{current}" "{last}" '
            + " ".join(ids),
        ],
        check=True,
    )


def migration(tmp: pathlib.Path, name: str, body: str) -> None:
    p = tmp / "migrations" / name
    p.write_text(body)
    p.chmod(0o755)


# ---------------------------------------------------------------------------


def case_no_migrations(tmp: pathlib.Path, migrate: pathlib.Path) -> None:
    """The v0.1.0 shape: no marker, no migration files.

    The count matters as much as the exit code. hermes-update derives
    PENDING_COUNT with `grep -c .`, and `printf '%s\\n' "${empty[@]}"` emits one
    blank line — so an implementation that looked correct could still report 1
    pending migration and then fail trying to run it.
    """
    r = run([str(migrate), "--list"])
    check("empty --list exits 0", r.returncode, 0)
    check("empty --list has no non-blank lines", len([x for x in r.stdout.split("\n") if x]), 0)
    r = run([str(migrate), "--run"])
    check("empty --run exits 0", r.returncode, 0)


def case_fresh_disk(tmp: pathlib.Path, migrate: pathlib.Path) -> None:
    """No marker at all → everything is pending, .sh and .py alike, in order."""
    migration(tmp, "0001_first.sh", f'#!/usr/bin/env bash\necho ran >> {tmp}/data/order\n')
    migration(
        tmp,
        "0002_second.py",
        f'open("{tmp}/data/order", "a").write("ran\\n")\n',
    )
    migration(tmp, "0003_third.sh", f'#!/usr/bin/env bash\necho ran >> {tmp}/data/order\n')

    r = run([str(migrate), "--list"])
    check("fresh disk lists all three", r.stdout.split(), ["0001", "0002", "0003"])

    r = run([str(migrate), "--run"])
    check("fresh disk --run exits 0", r.returncode, 0)
    check(
        "all three executed",
        len((tmp / "data" / "order").read_text().split()),
        3,
    )
    (tmp / "data" / "order").unlink()


def case_version_skip(tmp: pathlib.Path, migrate: pathlib.Path) -> None:
    """Applied 0001, image carries 0001-0003 → only the later two are pending.

    Ids are compared as zero-padded strings, which is the reason for the padding.
    """
    write_marker(tmp, "v0.1.0", "v0.1.0", "0001", ["0001", "0002", "0003"])
    r = run([str(migrate), "--list"])
    check("version skip lists only what is newer", r.stdout.split(), ["0002", "0003"])

    # The case that distinguishes "newer than applied" from "not equal to
    # applied": with 0002 applied, 0001 is older and must stay skipped. An
    # equality test passes the check above and re-runs 0001 here — against a
    # disk that 0002 has already restructured.
    write_marker(tmp, "v0.1.0", "v0.1.0", "0002", ["0001", "0002", "0003"])
    r = run([str(migrate), "--list"])
    check("already-applied earlier ids stay skipped", r.stdout.split(), ["0003"])


def case_failure_halts(tmp: pathlib.Path, migrate: pathlib.Path) -> None:
    """A migration that exits non-zero stops the run before the next one.

    Continuing past a failure would apply 0003 to a disk that 0002 left
    half-converted, and the marker would then be the only record of a state no
    migration ever produced.
    """
    # Its own marker, not whatever the previous case left. These cases share one
    # tmpdir, and a state-dependent test that silently stops exercising the
    # thing it names is worse than no test.
    write_marker(tmp, "v0.1.0", "v0.1.0", "0001", ["0001", "0002", "0003"])
    migration(tmp, "0002_second.py", 'import sys\nsys.exit(3)\n')
    r = run([str(migrate), "--run"])
    check("failed migration propagates its exit code", r.returncode, 3)
    check("the failing script is named", "0002_second.py" in r.stderr, True)
    check("the migration after it did not run", (tmp / "data" / "order").exists(), False)


def case_duplicate_id(tmp: pathlib.Path, migrate: pathlib.Path) -> None:
    """Two files sharing an id is an authoring mistake, not a coin flip."""
    write_marker(tmp, "v0.1.0", "v0.1.0", "0003", ["0001", "0002", "0003"])
    migration(tmp, "0004_one.sh", "#!/usr/bin/env bash\ntrue\n")
    migration(tmp, "0004_two.sh", "#!/usr/bin/env bash\ntrue\n")
    r = run([str(migrate), "--run"])
    check("duplicate id is refused", r.returncode, 1)
    check("duplicate id is explained", "expected exactly one file" in r.stderr, True)
    (tmp / "migrations" / "0004_two.sh").unlink()


def case_marker_roundtrip(tmp: pathlib.Path, migrate: pathlib.Path) -> None:
    """marker.sh writes it, hermes-update rewrites it with sed, health.py reads JSON.

    Three implementations, two languages, one file. The rewrite is a sed over
    string values precisely so the two shell writers cannot drift, but that only
    holds while the output stays parseable and the key names stay in step with
    backend/health.py.
    """
    marker = tmp / "data" / ".steward-version"
    write_marker(tmp, "v0.1.0", "v0.1.0", "0001", ["0001", "0002"])
    doc = json.loads(marker.read_text())
    check(
        "marker_write emits the keys health.py reads",
        sorted(doc),
        ["available_migrations", "current_version", "last_migration", "seeded_version", "updated_at"],
    )

    # The exact sed from hermes-update.sh's "record it" step.
    run(
        [
            "bash",
            "-c",
            f'sed -e \'s/"last_migration": "[^"]*"/"last_migration": "0002"/\' '
            f'-e \'s/"current_version": "[^"]*"/"current_version": "v0.2.0"/\' '
            f'"{marker}" > "{marker}.new" && mv "{marker}.new" "{marker}"',
        ],
        check=True,
    )
    doc = json.loads(marker.read_text())
    check("rewritten marker is still valid JSON", doc["current_version"], "v0.2.0")
    check("rewrite advanced last_migration", doc["last_migration"], "0002")
    check("rewrite left seeded_version alone", doc["seeded_version"], "v0.1.0")


def case_update_requires_target() -> None:
    """`hermes-update` with no --to must refuse.

    It ships inside a release and is installed from it, so its pinned version is
    always the one already running. Defaulting to it made a bare invocation
    snapshot the disk, re-pull the current images and report a successful
    upgrade.
    """
    src = UPDATE_SH.read_text()
    check(
        "the pinned version is not used as a default target",
        'TARGET="${STEWARD_VERSION:-' in src,
        False,
    )
    check("a --to-less run is refused", "TARGET_EXPLICIT" in src, True)

    with tempfile.TemporaryDirectory() as d:
        home = pathlib.Path(d)
        (home / "stack").mkdir()
        (home / "stack" / "steward-stack.yml").write_text("services: {}\n")
        (home / "stack" / ".env").write_text("IMAGE_TAG=v0.1.0\nHERMES_DATA_DIR=" + d + "/data\n")

        r = run(["bash", str(UPDATE_SH), "--home", str(home), "--dry-run"])
        check("no --to exits non-zero", r.returncode != 0, True)
        check("no --to says what to pass", "--to" in r.stderr, True)
        check("no --to names the installed version", "v0.1.0" in r.stderr, True)
        # The refusal has to land before anything is touched. A snapshot
        # directory here would mean it got as far as tarring the data disk.
        check("no --to changed nothing on disk", (home / "snapshots").exists(), False)


def main() -> int:
    print("hermes-update / migration machinery")
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        migrate = install(tmp)
        for fn in (
            case_no_migrations,
            case_fresh_disk,
            case_version_skip,
            case_failure_halts,
            case_duplicate_id,
            case_marker_roundtrip,
        ):
            print(f"\n{fn.__name__}")
            fn(tmp, migrate)

    print("\ncase_update_requires_target")
    case_update_requires_target()

    if failures:
        print(f"\n{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
