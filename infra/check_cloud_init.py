"""Verify cloud-init renders and parses.

`terraform validate` does not evaluate `templatefile`, so a cloud-init that
references a variable the root module does not supply — or that stops being
valid YAML once rendered — fails at apply time, on a box that is already
half-built. This checks both cheaply.

Run: python3 infra/check_cloud_init.py
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

# What infra/main.tf passes into templatefile().
SUPPLIED = {"tailscale_authkey", "hostname"}

here = pathlib.Path(__file__).parent
raw = (here / "cloud-init.yaml").read_text()

found = set(re.findall(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}", raw))
print("template vars referenced :", sorted(found) or "none")
print("supplied by main.tf      :", sorted(SUPPLIED))

missing = found - SUPPLIED
if missing:
    sys.exit(f"FAIL: cloud-init references unsupplied vars: {sorted(missing)}")

unused = SUPPLIED - found
if unused:
    print("note: supplied but unused :", sorted(unused))

rendered = raw
for name, value in {
    "tailscale_authkey": "tskey-test",
    "hostname": "steward",
}.items():
    rendered = rendered.replace("${" + name + "}", value)

doc = yaml.safe_load(rendered)
print("rendered YAML parses     : ok")
print("write_files              :", [f["path"] for f in doc.get("write_files", [])])
print("runcmd steps             :", len(doc.get("runcmd", [])))

# The disk script is the piece most likely to be edited carelessly later, and a
# syntax error in it only surfaces on a booting instance.
script = next(
    f["content"] for f in doc["write_files"] if f["path"].endswith("hermes-mount-data")
)
if "mkfs.ext4" not in script or "blkid" not in script:
    sys.exit("FAIL: mount script lost its format-only-if-empty guard")
print("mount script guard       : present")
