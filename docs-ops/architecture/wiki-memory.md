---
description: Markdown files plus an FTS5 index — the store, the write path, and why the graph went.
---

# Wiki memory

Memory is a directory of Markdown files under `${HERMES_DATA_DIR}/wiki`, one per
entity, with a derived SQLite FTS5 index beside them. There is no service, no
database container and no credential.

This replaced Graphiti on Neo4j. That is worth explaining rather than just
recording, because the graph was not removed for being slow or expensive.

## Why the graph went

Four findings, each verified against the running system before the change:

**Nothing automated made a graph-shaped query.** Every unattended call site was
semantic-search-then-text. The entire consumed payload was
`[str(f.get("fact"))[:300] for f in facts[:6]]` — no uuid, no traversal.
`invalid_at` was never read by any workflow; `valid_at` served only as a
`min()` staleness clock.

**The codebase had already worked around it twice.**
`contact_context.relevant_facts()` was a substring filter layered on top of the
graph's semantic search *to fix it*, and the dashboard's entity browser
deliberately used Neo4j's fulltext index rather than its vector index "so entity
browsing keeps working through the embedding-dimension mismatches that break
semantic fact search". Both were grep-shaped fixes for a vector-shaped problem.

**Attio was already the source of truth.** The CRM was read on every run
regardless of graph freshness, because open tasks change daily. The graph held
an LLM-derived echo of data being fetched fresh anyway.

**The durability story was aspirational.** `graph_sink.append_records()`, which
the compose file described as "the durable record the graph is rebuilt from",
had zero callers. The extractions directory held three files of synthetic test
data with `.example` domains.

## The store

```
${HERMES_DATA_DIR}/wiki/
  alice@example.com.md
  kestrel-underwriting.md
  .wiki-index.db          # derived, disposable
```

A document is frontmatter plus dated sections:

```markdown
---
key: alice@example.com
title: Alice Smith
aliases: Alice Smith, A. Smith
last_refreshed: 2026-08-08T12:00:00Z
---

## 2026-08-08T12:00:00Z — contact refresh from Attio and Gmail
- Runs analytics at [[Kestrel Underwriting]].
- Prefers email to calls.
```

**Keyed on a stable identifier** — an Attio record id where the CRM knows the
entity, an email address otherwise. This is the substantive improvement over the
graph: Graphiti resolved "Marcus Ravel" and "M. Vale" to one entity with an LLM,
per extraction, and no caller ever read the result back. Here identity is a
filename, and the CRM supplies it.

**Append-only.** The graph re-extracted a whole prose blob per refresh and
accumulated 66 near-duplicate episodes for the same contacts. A dated section
per observation keeps the history legible in a file a human can read.

**Provenance in the heading**, beside the facts it produced, because a fact
whose origin cannot be named is worse than an absent one.

## The index

`.wiki-index.db` is FTS5 over facts, plus a link table for backlinks. It is
**derived and disposable**: delete it and it rebuilds from the markdown. There
is no migration and no second source of truth — the same contract
`metrics_store.py` states for its DuckDB store.

FTS5 rather than vectors is the deliberate choice. The property both earlier
workarounds wanted is a **relevance floor**: a query that matches nothing must
return nothing. `MATCH` gives that; nearest-neighbour search cannot, because it
always has a nearest neighbour. That is precisely the defect
`relevant_facts()` existed to paper over — two unrelated addresses returning the
same five facts about two other people, reporting both as "already known" and
handing a drafting model a stranger's history.

FTS5 ships with the stdlib `sqlite3`, so the store adds no dependency and no
container.

## Reading

`app/memory.py` keeps the function signatures the graph had, so
`contact_context` and `calendar_daily_briefing` did not move. `search_facts`
returns two sources in order:

1. The document keyed on the query itself. An exact key hit needs no scoring.
2. FTS5 matches elsewhere, for facts about them recorded under someone else.

## Writing

Two paths:

| Caller | Function | Shape |
| --- | --- | --- |
| `contact_context`, `calendar_daily_briefing` | `memory.add_episode` | Prose body split into one bullet per observation |
| `org_site_ingest` | `wiki_write.write_triplets` | `(source, edge, target)` intents |

The triplet shape outlived the graph deliberately: a subject, a relation and an
object is exactly what a wiki line with a `[[wikilink]]` expresses, so the
extraction did not change when the storage did.

`write_triplets` groups by subject, so a crawl producing twenty facts about one
organisation writes one dated section rather than twenty.

## Links

`[[wikilink]]` between documents. Grep for `[[Foo]]` and you have the one-hop
neighbourhood — which is all the dashboard's Cytoscape canvas ever rendered
(a single `OPTIONAL MATCH (n)-[r:RELATES_TO]-(m)`). The view survived the
database being deleted; only the database went.

Link targets resolve through the same slug function as filenames, so
`[[Kestrel Underwriting]]` and `[[kestrel underwriting]]` are one document.

## Operational caveats

**The dashboard never writes.** It opens the index read-only and reports a
missing one rather than building it. The two containers run as different users,
and a dashboard-owned index would leave the workflows service unable to refresh
its own store.

**Bind-mount ownership.** `hermes/seed.sh` creates `${HERMES_DATA_DIR}/wiki` as
the invoking user. Without that, Docker creates the missing mount source as root
and the workflows container — which does not run as root — fails its first write
with a permissions error that does not name the cause.

**Organisation facts are one hop away, not blurred in.** The old relevance gate
matched a run-together domain token (`northwindstrategies`) against
whitespace-stripped prose, so a fact about the employer was returned for a query
about the employee. Organisations have their own document here, reached by the
wikilink — retrievable, but attributed correctly.

**No automatic entity merging.** Two documents for the same person under
different addresses stay two documents. The graph guessed at this and sometimes
guessed wrong, which is a worse failure than a duplicate that is visible.
