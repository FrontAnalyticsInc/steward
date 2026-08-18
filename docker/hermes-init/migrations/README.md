# Migrations

Forward-only, numbered, ordered. Empty as of v0.1.0 — the machinery ships before
it is needed, because retrofitting version tracking onto instances already in
the field is the expensive version of this work.

## Writing one

`NNNN_short_name.sh` or `NNNN_short_name.py`, where `NNNN` is a zero-padded
four-digit number that has never been used before. Numbers are the ordering and
the identity; never renumber one that has shipped.

They run:

- **with the stack down** and the data disk mounted at `/opt/data`
- **as root**, inside the `hermes-init` image
- **in ascending order**, stopping at the first non-zero exit

## Rules

**Idempotent.** A migration can run twice. An upgrade that fails at the health
check leaves the marker unchanged, so the retry re-runs everything pending —
that is deliberate, and it is what makes a partial upgrade safe rather than a
thing to debug by hand.

**Exit non-zero on failure, loudly.** The runner names the failing migration and
restores a snapshot. A migration that fails silently and exits 0 gets a marker
saying it succeeded, which is the one outcome there is no recovery from.

**Never touch customer-modified files.** The wiki, `SOUL.md`, memories, the
kanban board and the workflows tree on the data disk belong to the operator.
Migrate *structure* — a database column, a renamed directory, a config key. If
you find yourself rewriting the content of something a person edits, stop.

**Assume a version skip.** Someone will be four releases behind. `0004` runs
directly after `0001` with `0002` and `0003` in the same batch, and none of them
may assume the state left by the release they shipped in — only the state the
previous *migration* left.

## Testing

```bash
# What would run, without running it
hermes-update --dry-run

# The failure path is the one worth rehearsing: a migration that exits 1 must
# roll back and leave the marker untouched.
```
