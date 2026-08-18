"""One store for what the whole system did, what it cost, and what it produced.

Four things spend tokens on this host — ADK workflows, Hermes chat, Hermes
automations, and Graphiti — and until now each kept its own books in its own
shape, or kept none at all. This module is the seam that gives them one grain.

Two tables carry everything:

  `fact_activity`   one row per unit of work, whatever produced it. A workflow
                    run, a chat session, a cron execution and a graph ingest are
                    the same kind of thing at this grain: something started, took
                    time, and ended in a status.

  `fact_llm_usage`  the usage ledger. Its columns are deliberately a copy of
                    Hermes's own `session_model_usage`, because that table
                    already models this problem correctly — separate cache-read
                    and cache-write counters, reasoning tokens broken out,
                    estimated cost kept apart from actual, and a `cost_status`
                    that admits when a number is not known. Adopting its shape
                    means the richest producer needs no translation at all, and
                    everything else conforms to something already proven.

Sources are read in place and never mutated. The Hermes state databases are
attached READ_ONLY; the JSONL logs are scanned where they lie. Nothing here is
the source of truth for anything, which is what makes the whole store safe to
delete and rebuild.

Two rules this module keeps, both inherited from the code it aggregates:

  It never reports an uninstrumented field as a measurement. `read_json_auto`
  with `union_by_name` gives NULL for a field a producer had not yet started
  writing, and NULL survives all the way to the API. A trace written before
  per-agent model capture existed has an unknown model, not a blank one, and a
  run that reported no tokens spent an unknown number of them, not zero.

  It never sums costs that are not comparable. See `COST_CLASS` below.
"""

from __future__ import annotations

import glob
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Same mount the rest of the dashboard reads through. `main.py` defines these
# too; taking them from the environment separately keeps this module importable
# on the host (tests, one-off queries) where nothing is mounted anywhere.
DB_DIR = os.environ.get("HERMES_DB_DIR", "/opt/data")
ADK_STATE_DIR = os.environ.get("ADK_STATE_DIR", os.path.join(DB_DIR, "adk"))

# Materialized marts only. Everything else is a view over the sources, so this
# file holds nothing that cannot be recomputed and can be deleted at any time.
STORE_PATH = os.environ.get("METRICS_STORE_PATH", os.path.join(ADK_STATE_DIR, "metrics.duckdb"))

# How stale a mart may be before a read refreshes it. Marts exist for volume we
# do not yet have, so this is generous on purpose: at the current scale the raw
# scan is fast enough that refreshing more often would be pure overhead.
MART_TTL_S = int(os.environ.get("METRICS_MART_TTL_S", "300"))

# How much memory DuckDB may use before it spills to disk.
#
# Set explicitly because DuckDB's default is 80% of the *container* limit, and
# that heuristic assumes it is the only thing in the container. Here it is not:
# this service idles around 218 MiB before any query runs, so inside a 512 MiB
# cgroup DuckDB would budget 409 MiB on top of that, reach 627 MiB, and be
# OOM-killed — taking the whole dashboard with it rather than spilling.
#
# Spilling is slower and always correct; being killed is neither. Sized under
# (mem_limit - idle RSS) so the Python process, FastAPI and the request itself
# still have room — with the service's mem_limit raised to 768m to leave that
# subtraction some slack.
#
# Not lower than this: a ten-year range (`outputs(3650)`, which the UI offers)
# needs more than 200 MiB and throws rather than spilling when it cannot get it.
# Some allocations spill; not all do.
MEMORY_LIMIT = os.environ.get("METRICS_DUCKDB_MEMORY_LIMIT", "320MB")

# How many threads DuckDB may run a query across.
#
# Must be set alongside MEMORY_LIMIT, and is the more load-bearing of the two.
# DuckDB defaults this to the host's core count — 16 here — and then divides the
# memory budget between them, so each thread gets ~19 MiB of a 305 MiB pool.
# Several operators allocate in fixed 32 MiB blocks that cannot be split or
# spilled, so they throw before a single one is satisfied. The failure is not
# proportional to the data: at this scale every one of these queries returns
# under twenty rows, and they still failed.
#
# Four is chosen as the largest count that leaves each thread a block's worth of
# headroom. Verified against the widest window the UI offers (3650 days), where
# every route completes in under a second; 16 threads cannot complete the
# narrowest. Raising MEMORY_LIMIT instead would work only until the core count
# changes, which is why this is pinned rather than left to the heuristic.
THREADS = int(os.environ.get("METRICS_DUCKDB_THREADS", "4"))

# Where spilled intermediates go. Explicit so they land beside the store on the
# data disk rather than in the image's writable layer, which on a small cloud
# box is the root volume.
TEMP_DIR = os.environ.get("METRICS_DUCKDB_TEMP_DIR", os.path.join(ADK_STATE_DIR, "duckdb-tmp"))

# DuckDB permits exactly one read-write process against a file. The dashboard is
# that process, and this lock keeps its own request threads from racing.
_lock = threading.RLock()
_con = None
_last_refresh: float = 0.0


# --- cost classes -----------------------------------------------------------
#
# The single most important modelling decision here, and the reason there is no
# "total cost" anywhere in this module.
#
# Usage on this host falls into three classes that are not addable:
#
#   metered   real money, at a published rate. Sums to a dollar figure.
#   included  covered by a subscription already paid for (Codex-routed models).
#             The marginal cost is genuinely zero, but the usage is real and
#             must stay attributable — it is most of the fleet's traffic.
#   unpriced  a local model with no rate. Costs nothing to run beyond
#             electricity, and we decline to invent a number for it.
#
# Hermes already records exactly this distinction in `cost_status`, so we adopt
# its vocabulary rather than inventing a competing one. Adding these together
# would produce a number that is neither spend nor capacity, and reporting it
# next to a dollar sign would be the most expensive kind of wrong — the kind
# that looks right. So every aggregate in this module is grouped by cost class,
# and the API returns them as separate lines.
# A fourth distinction hides inside the third and matters more than it looks:
# a model priced at zero is not the same as a model with no price. Both show a
# cost of 0, so the number cannot tell them apart — only `cost_source` can, by
# recording that a rate was actually applied. A run priced at $0.00 belongs in
# `metered` with a real zero; a run nobody has a rate for belongs in `unpriced`
# with NULL. Collapsing them would let unmeasured usage masquerade as free.
# `billing_mode` is checked alongside `cost_status` because they answer different
# questions and can disagree. The mode is ground truth about *how* a call is
# billed; the status is only about whether a cost figure was worked out. Hermes
# has rows reading `billing_mode='subscription_included'` with
# `cost_status='unknown'` — subscription traffic whose price was never computed,
# which is not the same as traffic nobody has a rate for. Without this, the same
# model appeared twice on the page, once as `included` and once as `unpriced`.
COST_CLASS = """
    CASE
        WHEN cost_status = 'included' OR billing_mode = 'subscription_included'
            THEN 'included'
        WHEN COALESCE(actual_cost_usd, 0) > 0 OR COALESCE(estimated_cost_usd, 0) > 0
            THEN 'metered'
        WHEN cost_source IS NOT NULL AND cost_source NOT IN ('none', '')
            THEN 'metered'
        ELSE 'unpriced'
    END
"""


def _profiles() -> List[Tuple[str, str, Optional[str]]]:
    """Every Hermes profile with a state database, as (name, state_db, cron_db).

    Discovered rather than configured. Hermes keeps the default profile's state
    at the root of its data directory and every other profile under `profiles/`,
    and a profile can be added without anything here being told — so a
    hardcoded list would silently omit a profile's cost, which is the failure
    mode this whole store exists to prevent.
    """
    found: List[Tuple[str, str, Optional[str]]] = []

    def _add(name: str, root: str) -> None:
        state = os.path.join(root, "state.db")
        if not os.path.isfile(state):
            return
        cron = os.path.join(root, "cron", "executions.db")
        found.append((name, state, cron if os.path.isfile(cron) else None))

    _add("default", DB_DIR)
    for path in sorted(glob.glob(os.path.join(DB_DIR, "profiles", "*"))):
        if os.path.isdir(path):
            _add(os.path.basename(path), path)
    return found


def _ident(name: str) -> str:
    """A profile name made safe to use as a DuckDB catalog alias."""
    return "p_" + "".join(ch if ch.isalnum() else "_" for ch in name)


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _jsonl_glob(*parts: str) -> str:
    return os.path.join(ADK_STATE_DIR, *parts)


def _has_files(pattern: str) -> bool:
    return bool(glob.glob(pattern))


def _source_columns(con, sql: str) -> set:
    """Which columns a source actually has, asked rather than assumed.

    `union_by_name` handles a field that some files have and others do not, but
    it cannot conjure one that appears in no file at all — and referencing that
    is a hard binder error, not a NULL. Since the whole point of this store is
    to read every trace version including ones written before a field was
    invented, every projection over an evolving source is built from this.
    """
    try:
        cur = con.execute(f"SELECT * FROM ({sql}) LIMIT 0")
        return {d[0] for d in cur.description}
    except Exception as exc:
        logger.warning("metrics_store: could not inspect source: %s", exc)
        return set()


def _struct_fields(con, relation: str, column: str) -> set:
    """The field names inside a list-of-structs column.

    Same problem as `_source_columns` one level down: the per-agent record grew
    a `model` field and token counters at trace v3, and a run written before
    that has structs without them. Asking the data rather than assuming is the
    only way one query serves both.
    """
    try:
        row = con.execute(
            f"SELECT json_keys(to_json({column}[1])) FROM {relation} "
            f"WHERE {column} IS NOT NULL AND len({column}) > 0 LIMIT 1"
        ).fetchone()
        return set(row[0]) if row and row[0] else set()
    except Exception as exc:
        logger.warning("metrics_store: could not inspect %s.%s: %s", relation, column, exc)
        return set()


def _pick(columns: set, name: str, sql_type: str, expr: Optional[str] = None,
          alias: Optional[str] = None) -> str:
    """`name` if the source has it, otherwise a typed NULL of the same shape.

    The typed NULL matters: an untyped one makes the union pick a type from
    whichever branch happened to be first, and the column silently becomes the
    wrong thing.
    """
    out = alias or name
    if name in columns:
        return f"{expr or f'src.{name}'} AS {out}"
    return f"NULL::{sql_type} AS {out}"


def connect():
    """The store's one connection, built on first use.

    Attaching happens here rather than per query because the set of profiles
    only changes when someone creates one, and a reconnect is cheap enough to be
    the answer when they do.
    """
    global _con
    with _lock:
        if _con is not None:
            return _con
        import duckdb  # imported lazily so the module is importable without it

        os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
        os.makedirs(TEMP_DIR, exist_ok=True)
        con = duckdb.connect(STORE_PATH)
        # Bound the engine before it runs anything. See MEMORY_LIMIT above for
        # why the default is wrong in a shared container.
        con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
        # Before threads, not after: the budget above is only survivable when it
        # is divided a small number of ways. See THREADS.
        con.execute(f"SET threads = {THREADS}")
        con.execute(f"SET temp_directory = {_sql_str(TEMP_DIR)}")
        # Pre-installed at image build so this never needs the network at
        # runtime; INSTALL is a no-op when the extension is already present.
        con.execute("INSTALL sqlite; LOAD sqlite;")

        for name, state_db, cron_db in _profiles():
            alias = _ident(name)
            # READ_ONLY is not a precaution, it is a correctness requirement:
            # Hermes is writing to these files while we read them, and this
            # store must never be able to take a lock that stalls a live turn.
            try:
                con.execute(
                    f"ATTACH {_sql_str(state_db)} AS {alias} (TYPE sqlite, READ_ONLY)"
                )
            except Exception as exc:
                logger.warning("metrics_store: could not attach %s: %s", state_db, exc)
                continue
            if cron_db:
                try:
                    con.execute(
                        f"ATTACH {_sql_str(cron_db)} AS {alias}_cron (TYPE sqlite, READ_ONLY)"
                    )
                except Exception as exc:
                    logger.warning("metrics_store: could not attach %s: %s", cron_db, exc)

        _build_views(con)
        _con = con
        return con


def reset() -> None:
    """Drop the connection so the next call rediscovers profiles and sources."""
    global _con, _last_refresh
    with _lock:
        if _con is not None:
            try:
                _con.close()
            except Exception:
                pass
        _con = None
        _last_refresh = 0.0


# --- views ------------------------------------------------------------------


def _attached(con) -> List[str]:
    rows = con.execute("SELECT database_name FROM duckdb_databases()").fetchall()
    return [r[0] for r in rows]


def _materialize_hermes(con) -> Tuple[bool, bool]:
    """Copy each profile's sessions and usage into DuckDB, one profile per statement.

    This is the one place the store copies anything, and it is not an
    optimisation — it is a correctness workaround for a DuckDB bug, so it is
    worth stating precisely.

    Every Hermes profile is a separate sqlite database with *identical table
    names*. When a single statement selects from two of them and each branch
    also joins within its own catalog (`session_model_usage` to `sessions`),
    DuckDB 1.5's sqlite scanner resolves the second branch's tables against the
    first catalog. The branches are correct in isolation and correct when
    unioned without the join; add the join and the second profile silently
    returns the first profile's rows. Demonstrated at three profiles: `sub`
    reported `default`'s four rows while holding one of its own.

    Nothing about that is detectable downstream — the row counts look
    plausible, every column is populated, and the profile label is right
    because it comes from a literal. It would simply attribute one profile's
    spend to another, forever.

    So each profile is read in its own statement, where only one catalog is in
    play and the bug cannot arise, and the results are accumulated into a plain
    DuckDB table. The data is small (hundreds of rows) and rebuilt on every
    connect, so the store stays fully re-derivable — `rm metrics.duckdb` still
    reconstructs everything.
    """
    made_activity = made_usage = False
    for name, _, _ in _profiles():
        alias = _ident(name)
        if alias not in _attached(con):
            continue
        for table, sql, made in (
            ("hermes_activity", _profile_activity_sql(name, alias), made_activity),
            ("hermes_usage", _profile_usage_sql(name, alias), made_usage),
        ):
            try:
                if not made:
                    con.execute(f"CREATE OR REPLACE TABLE {table} AS {sql}")
                else:
                    con.execute(f"INSERT INTO {table} BY NAME {sql}")
            except Exception as exc:
                logger.warning("metrics_store: profile %s (%s): %s", name, table, exc)
                continue
            if table == "hermes_activity":
                made_activity = True
            else:
                made_usage = True
    return made_activity, made_usage


def _profile_activity_sql(name: str, alias: str) -> str:
    """Hermes sessions as activities, unioned across every attached profile.

    `sessions.source` is what separates a chat from an automation: Hermes stamps
    'cron' on a session a scheduled job started, and the platform name on one a
    human started. That column is therefore the whole basis of the kind mapping
    — no heuristic, no guessing from the session's shape.
    """
    return f"""
            SELECT
                s.id                                  AS activity_id,
                CASE WHEN s.source = 'cron' THEN 'automation_run'
                     ELSE 'chat_session' END          AS kind,
                -- A cron session's title carries the job's label ahead of a
                -- timestamp ("ping12b · Aug 06 01:40"); the label is the thing
                -- worth grouping by, the timestamp is already a column.
                CASE WHEN s.source = 'cron'
                     THEN COALESCE(trim(split_part(s.title, '·', 1)), s.source)
                     ELSE COALESCE(s.title, s.source) END        AS app,
                s.source                              AS source,
                s.source                              AS trigger,
                -- NAIVE timestamps that mean UTC: `timezone('UTC', ...)` strips
                -- the offset, so these serialize without a marker. The frontend
                -- relies on that contract — parseStamp() appends 'Z' to any
                -- unsuffixed string — so do not switch these to local time
                -- without changing it too. Left naive rather than cast to
                -- timestamptz because the cast changes the wire type for every
                -- existing chart consumer.
                timezone('UTC', to_timestamp(s.started_at))      AS started_at,
                timezone('UTC', to_timestamp(s.ended_at))        AS ended_at,
                COALESCE(s.end_reason, 'active')      AS status,
                -- Deliberately NULL for every Hermes session. `end_reason`
                -- records how a session stopped ('cli_close', 'session_reset',
                -- 'cron_complete') — a disposition, not a verdict. Mapping any
                -- of those onto success would invent a measurement Hermes
                -- never made, and reporting "0 failed" for 91 sessions that
                -- simply have no outcome concept is exactly the confident zero
                -- this store refuses to print.
                NULL::VARCHAR                         AS outcome,
                -- Hermes encodes the job in a cron session's id as
                -- `cron_<job_id>_<date>_<time>`, which is the only link between
                -- a session's cost and the schedule that caused it.
                CASE WHEN s.source = 'cron'
                     THEN split_part(s.id, '_', 2) END           AS job_id,
                s.parent_session_id                   AS parent_activity_id,
                {_sql_str(name)}                      AS profile,
                s.message_count                       AS message_count,
                s.tool_call_count                     AS tool_call_count
            FROM {alias}.sessions s
    """


def _profile_usage_sql(name: str, alias: str) -> str:
    """Hermes's own ledger, which already has the shape we want.

    Near-identity mapping. `task` becomes `component` because that is what it
    is: Hermes bills compression and title generation against the session that
    triggered them, and keeping them as distinct components is what makes
    overhead visible instead of folded into the work it supported.
    """
    return f"""
            SELECT
                u.session_id                             AS activity_id,
                CASE WHEN u.task = '' THEN 'main' ELSE u.task END AS component,
                u.model, u.billing_provider, u.billing_base_url, u.billing_mode,
                u.task,
                u.api_call_count, u.input_tokens, u.output_tokens,
                u.cache_read_tokens, u.cache_write_tokens, u.reasoning_tokens,
                u.estimated_cost_usd, u.actual_cost_usd,
                u.cost_status, u.cost_source,
                s.pricing_version                        AS pricing_version,
                'self'                                   AS observed_by,
                {_sql_str(name)}                         AS profile,
                timezone('UTC', to_timestamp(u.first_seen))         AS first_seen,
                timezone('UTC', to_timestamp(u.last_seen))          AS last_seen
            FROM {alias}.session_model_usage u
            LEFT JOIN {alias}.sessions s ON s.id = u.session_id
    """


# The ADK trace record. Its per-agent token fields did not exist before
# TRACE_VERSION 3, so `union_by_name` leaves them NULL on older lines and they
# read as "not measured" rather than as zero — which is the difference between
# an honest gap and a fabricated one.
_ADK_TRACES = """
    SELECT * FROM read_json_auto(
        {pattern},
        union_by_name := true,
        filename := true,
        ignore_errors := true
    )
"""


def _build_raw_trace_views(con) -> set:
    """The ADK trace log, raw and deduplicated, before anything reads it.

    Built first because every ADK-derived view has to agree about which lines
    count. `invoke_workflow` writes a line per attempt and another on the
    failure path, so a retried run appears two or three times; the newest line
    for a `run_id` is the one describing how it actually ended, and it wins.

    Deduplicating here rather than in each consumer is the point. When only
    `fact_activity` deduplicated, a retried run was one activity whose agents,
    checkpoints and produced items were all counted twice — so a flaky pipeline
    looked busier and more productive than a reliable one, which is precisely
    backwards. Anything reading `raw_adk_trace_latest` gets that right for free.

    Returns the column set, so callers can keep projecting only fields that
    exist rather than guessing at the trace version.
    """
    pattern = _jsonl_glob("traces", "*", "*.jsonl")
    if not _has_files(pattern):
        return set()
    src = _ADK_TRACES.format(pattern=_sql_str(pattern))
    cols = _source_columns(con, src)
    if "run_id" not in cols:
        return set()
    con.execute(f"CREATE OR REPLACE VIEW raw_adk_trace AS {src}")
    con.execute("""
        CREATE OR REPLACE VIEW raw_adk_trace_latest AS
        SELECT * EXCLUDE (_rn) FROM (
            SELECT *, row_number() OVER (
                PARTITION BY run_id ORDER BY started_at DESC
            ) AS _rn
            FROM raw_adk_trace
            WHERE run_id IS NOT NULL
        ) WHERE _rn = 1
    """)
    return cols


def _adk_activity_sql(con, cols: set) -> Optional[str]:
    if not cols:
        return None
    return f"""
            SELECT
                src.run_id                            AS activity_id,
                'workflow_run'                        AS kind,
                {_pick(cols, 'app', 'VARCHAR')},
                'adk'                                 AS source,
                {_pick(cols, 'trigger', 'VARCHAR', "COALESCE(src.trigger, 'unknown')")},
                try_cast(src.started_at AS TIMESTAMP)  AS started_at,
                try_cast(src.started_at AS TIMESTAMP)
                    + to_milliseconds(CAST(COALESCE(src.duration_ms, 0) AS BIGINT)) AS ended_at,
                {_pick(cols, 'status', 'VARCHAR')},
                -- ADK is the one producer with a real outcome vocabulary, and
                -- `invoke_workflow` writes it on both the success and failure
                -- paths, so it is trustworthy across every trace version.
                {_pick(cols, 'status', 'VARCHAR',
                       "CASE WHEN src.status IN ('ok','partial','failed') "
                       "THEN src.status END", alias='outcome')},
                NULL::VARCHAR                         AS job_id,
                NULL::VARCHAR                         AS parent_activity_id,
                'adk'                                 AS profile,
                NULL::BIGINT                          AS message_count,
                {_pick(cols, 'tool_calls', 'BIGINT', 'CAST(src.tool_calls AS BIGINT)',
                       alias='tool_call_count')}
            FROM raw_adk_trace_latest src
    """


def _adk_usage_sql(con, cols: set) -> Optional[str]:
    """ADK per-agent token usage, in the same ledger shape as Hermes's.

    This is what puts workflow spend on the same page as chat spend. Until it
    existed the ledger was Hermes-only, so the fleet's token volume was
    understated by every workflow that ever ran — and the Metrics tab said so
    with a straight face.

    Two judgements are baked in.

    **Only agents that actually called a model.** `fact_run_agent` lists every
    stage that authored an event, and most pipelines are mostly plain code —
    `fetch_events` and `emit_result` take turns without ever reaching an LLM.
    Putting those in a *model usage* ledger would invent API calls that never
    happened, so a row appears only where there is evidence of one: a model
    name, or a token count the provider reported.

    **Cost class is left to the same rule as everything else.** No cost columns
    are set here, so `cost_status` and `cost_source` are NULL and COST_CLASS
    lands these in `unpriced` — which is exactly right today, with every
    workflow on a local model that nobody has a rate for. If a paid provider is
    ever configured, the rows already carry the model, and pricing becomes a
    join rather than a schema change.
    """
    if not cols or "agents" not in cols:
        return None
    fields = _struct_fields(con, "raw_adk_trace_latest", "agents")

    def field(name: str, sql_type: str) -> str:
        return f"unnest(agents).{name}" if name in fields else f"NULL::{sql_type}"

    # LiteLLM names a model `<provider>/<model>` ("ollama_chat/gemma4:12b").
    # Where that prefix is present it is the provider; where it is not, we do
    # not know, and NULL says so rather than guessing "openai" by convention.
    #
    # With the proxy gone, the prefix is the vendor again: workflows call
    # `anthropic/<model>` through the SDK, so split_part gives a truthful
    # answer and the special case that existed for the proxy hop is gone.
    provider = ("CASE WHEN contains(u.model, '/') "
                "THEN split_part(u.model, '/', 1) END")
    return f"""
        SELECT
            u.activity_id, u.component, u.model,
            {provider}                       AS billing_provider,
            NULL::VARCHAR                    AS billing_base_url,
            NULL::VARCHAR                    AS billing_mode,
            ''                               AS task,
            u.api_call_count,
            u.input_tokens, u.output_tokens,
            u.cache_read_tokens,
            NULL::BIGINT                     AS cache_write_tokens,
            u.reasoning_tokens,
            NULL::DOUBLE                     AS estimated_cost_usd,
            NULL::DOUBLE                     AS actual_cost_usd,
            NULL::VARCHAR                    AS cost_status,
            NULL::VARCHAR                    AS cost_source,
            NULL::VARCHAR                    AS pricing_version,
            'self'                           AS observed_by,
            'adk'                            AS profile,
            u.at                             AS first_seen,
            u.at                             AS last_seen
        FROM (
            SELECT
                run_id                                        AS activity_id,
                CAST(unnest(agents).name AS VARCHAR)          AS component,
                CAST({field('model', 'VARCHAR')} AS VARCHAR)  AS model,
                CAST({field('api_call_count', 'BIGINT')} AS BIGINT)   AS api_call_count,
                CAST({field('prompt_tokens', 'BIGINT')} AS BIGINT)    AS input_tokens,
                CAST({field('completion_tokens', 'BIGINT')} AS BIGINT) AS output_tokens,
                CAST({field('cache_read_tokens', 'BIGINT')} AS BIGINT) AS cache_read_tokens,
                CAST({field('reasoning_tokens', 'BIGINT')} AS BIGINT)  AS reasoning_tokens,
                try_cast(started_at AS TIMESTAMP)             AS at
            FROM raw_adk_trace_latest
            WHERE agents IS NOT NULL
        ) u
        WHERE u.model IS NOT NULL
           OR u.input_tokens IS NOT NULL
           OR u.output_tokens IS NOT NULL
    """


def _gateway_usage_sql(con) -> Optional[str]:
    """The shared usage ledger under `${ADK_STATE_DIR}/usage/`.

    Two producers write here and they do not write the same columns: the Hermes
    gateway records what it observed, and `app/cost_ledger.py` records what a
    workflow's model call actually cost. Absent until one of them has run, which
    is why every reference to it is guarded — the store has to be correct with
    only some producers present, since that is its state for most of its life.

    Every column is projected explicitly rather than `SELECT *`, for two
    separate reasons that both surface as a hard error rather than a NULL:

      * a column no producer has ever written does not exist in the source, and
        referencing it is a binder error. `_source_columns` is asked instead of
        assumed, the same as every other projection over an evolving source.
      * `read_json_auto` infers a column that is null in every row as an untyped
        NULL, and `SUM()` over that raises rather than returning NULL — so one
        all-null column (Anthropic reports no cache tokens on an uncached call)
        would take out every aggregate in the store.
    """
    pattern = _jsonl_glob("usage", "*.jsonl")
    if not _has_files(pattern):
        return None
    source = f"""SELECT * FROM read_json_auto(
        {_sql_str(pattern)}, union_by_name := true, ignore_errors := true
    )"""
    cols = _source_columns(con, source)
    if not cols:
        return None

    def col(name: str, sql_type: str) -> str:
        if name not in cols:
            return f"NULL::{sql_type} AS {name}"
        # try_cast, not CAST: a producer that writes a token count as a string
        # should cost that one field, not the whole file.
        return f"try_cast({name} AS {sql_type}) AS {name}"

    projection = ",\n            ".join(
        col(name, sql_type)
        for name, sql_type in (
            ("activity_id", "VARCHAR"),
            ("component", "VARCHAR"),
            ("model", "VARCHAR"),
            ("billing_provider", "VARCHAR"),
            ("billing_base_url", "VARCHAR"),
            ("billing_mode", "VARCHAR"),
            ("task", "VARCHAR"),
            ("api_call_count", "BIGINT"),
            ("input_tokens", "BIGINT"),
            ("output_tokens", "BIGINT"),
            ("cache_read_tokens", "BIGINT"),
            ("cache_write_tokens", "BIGINT"),
            ("reasoning_tokens", "BIGINT"),
            ("estimated_cost_usd", "DOUBLE"),
            ("actual_cost_usd", "DOUBLE"),
            ("cost_status", "VARCHAR"),
            ("cost_source", "VARCHAR"),
            ("pricing_version", "VARCHAR"),
            ("observed_by", "VARCHAR"),
            ("profile", "VARCHAR"),
            ("first_seen", "TIMESTAMP"),
            ("last_seen", "TIMESTAMP"),
        )
    )
    return f"""
        SELECT
            {projection}
        FROM ({source})
    """


def _build_views(con) -> None:
    """Define the store. Every view degrades to empty rather than failing."""
    # First, so everything downstream reads the same deduplicated trace lines.
    trace_cols = _build_raw_trace_views(con)

    empty_activity = """
        SELECT NULL::VARCHAR AS activity_id, NULL::VARCHAR AS kind,
               NULL::VARCHAR AS app, NULL::VARCHAR AS source,
               NULL::VARCHAR AS trigger, NULL::TIMESTAMP AS started_at,
               NULL::TIMESTAMP AS ended_at, NULL::VARCHAR AS status,
               NULL::VARCHAR AS outcome, NULL::VARCHAR AS job_id,
               NULL::VARCHAR AS parent_activity_id, NULL::VARCHAR AS profile,
               NULL::BIGINT AS message_count, NULL::BIGINT AS tool_call_count
        WHERE false
    """
    have_activity, have_usage = _materialize_hermes(con)
    activity_parts = [p for p in (
        "SELECT * FROM hermes_activity" if have_activity else None,
        _adk_activity_sql(con, trace_cols),
    ) if p]
    # The ADK branch carries a helper column the Hermes branch does not, so the
    # union is by name over an explicit projection rather than positional.
    if activity_parts:
        selected = [
            f"""SELECT activity_id, kind, app, source, trigger, started_at, ended_at,
                       status, outcome, job_id, parent_activity_id, profile,
                       message_count, tool_call_count
                FROM ({p})"""
            for p in activity_parts
        ]
        activity_sql = "\nUNION ALL BY NAME\n".join(selected)
    else:
        activity_sql = empty_activity
    con.execute(f"CREATE OR REPLACE VIEW fact_activity AS {activity_sql}")

    empty_usage = """
        SELECT NULL::VARCHAR AS activity_id, NULL::VARCHAR AS component,
               NULL::VARCHAR AS model, NULL::VARCHAR AS billing_provider,
               NULL::VARCHAR AS billing_base_url, NULL::VARCHAR AS billing_mode,
               NULL::VARCHAR AS task, NULL::BIGINT AS api_call_count,
               NULL::BIGINT AS input_tokens, NULL::BIGINT AS output_tokens,
               NULL::BIGINT AS cache_read_tokens, NULL::BIGINT AS cache_write_tokens,
               NULL::BIGINT AS reasoning_tokens, NULL::DOUBLE AS estimated_cost_usd,
               NULL::DOUBLE AS actual_cost_usd, NULL::VARCHAR AS cost_status,
               NULL::VARCHAR AS cost_source, NULL::VARCHAR AS pricing_version,
               NULL::VARCHAR AS observed_by, NULL::VARCHAR AS profile,
               NULL::TIMESTAMP AS first_seen, NULL::TIMESTAMP AS last_seen
        WHERE false
    """
    usage_parts = [p for p in (
        "SELECT * FROM hermes_usage" if have_usage else None,
        _adk_usage_sql(con, trace_cols),
        _gateway_usage_sql(con),
    ) if p]
    usage_sql = "\nUNION ALL BY NAME\n".join(usage_parts) if usage_parts else empty_usage
    con.execute(f"CREATE OR REPLACE VIEW raw_llm_usage AS {usage_sql}")

    # Double-counting guard. Once the gateway is in front of the model
    # endpoints, a call that Hermes already billed to itself would also be seen
    # by the proxy, and naively unioning the two would double every Hermes
    # number overnight. Precedence is resolved here, in one place, at read time
    # — not at write time, where it could not be audited or corrected:
    # a self-reported row always wins, and a gateway row survives only for an
    # activity that reported nothing about itself.
    con.execute(f"""
        CREATE OR REPLACE VIEW fact_llm_usage AS
        WITH ranked AS (
            SELECT *,
                   {COST_CLASS} AS cost_class,
                   -- Not COALESCE: Hermes defaults `actual_cost_usd` to 0
                   -- rather than NULL and only fills it when a provider
                   -- reports a settled charge, so coalescing would let that
                   -- placeholder zero silently outrank a real estimate and
                   -- report every metered run as free.
                   CASE WHEN COALESCE(actual_cost_usd, 0) > 0 THEN actual_cost_usd
                        ELSE estimated_cost_usd END AS cost_usd,
                   COUNT(*) FILTER (WHERE observed_by = 'self')
                       OVER (PARTITION BY activity_id) AS self_rows
            FROM raw_llm_usage
        )
        SELECT * EXCLUDE (self_rows)
        FROM ranked
        WHERE observed_by = 'self' OR self_rows = 0
    """)

    # Cost that may be stated in dollars, kept apart from usage that may not.
    # A caller that wants "spend" gets only the metered class; a caller that
    # wants "how much did the fleet think" gets tokens across all classes.
    con.execute("""
        CREATE OR REPLACE VIEW fact_activity_cost AS
        SELECT
            a.activity_id, a.kind, a.app, a.source, a.profile, a.started_at, a.status,
            u.cost_class,
            SUM(u.api_call_count)                                  AS api_calls,
            SUM(u.input_tokens)                                    AS input_tokens,
            SUM(u.output_tokens)                                   AS output_tokens,
            SUM(u.cache_read_tokens)                               AS cache_read_tokens,
            SUM(u.cache_write_tokens)                              AS cache_write_tokens,
            SUM(u.reasoning_tokens)                                AS reasoning_tokens,
            SUM(CASE WHEN u.cost_class = 'metered' THEN u.cost_usd END) AS metered_cost_usd
        FROM fact_activity a
        JOIN fact_llm_usage u ON u.activity_id = a.activity_id
        GROUP BY ALL
    """)

    _build_automation_view(con)
    _build_adk_detail_views(con, trace_cols)
    _build_eval_view(con)
    _build_integration_view(con)


def _build_automation_view(con) -> None:
    """Scheduled-job executions, unioned across profiles.

    Hermes's cron store is per-profile, so a job that moved profiles would
    vanish from a single-database view — the same reason `_profiles` discovers
    rather than assumes.
    """
    parts = []
    attached = _attached(con)
    for name, _, cron_db in _profiles():
        alias = f"{_ident(name)}_cron"
        if not cron_db or alias not in attached:
            continue
        parts.append(f"""
            SELECT id AS execution_id, job_id, source, status,
                   try_cast(claimed_at AS TIMESTAMP)  AS claimed_at,
                   try_cast(started_at AS TIMESTAMP)  AS started_at,
                   try_cast(finished_at AS TIMESTAMP) AS finished_at,
                   error,
                   {_sql_str(name)} AS profile
            FROM {alias}.executions
        """)
    if not parts:
        con.execute("""
            CREATE OR REPLACE VIEW fact_automation_execution AS
            SELECT NULL::VARCHAR AS execution_id, NULL::VARCHAR AS job_id,
                   NULL::VARCHAR AS source, NULL::VARCHAR AS status,
                   NULL::TIMESTAMP AS claimed_at,
                   NULL::TIMESTAMP AS started_at, NULL::TIMESTAMP AS finished_at,
                   NULL::VARCHAR AS error, NULL::VARCHAR AS profile
            WHERE false
        """)
        return
    con.execute(
        "CREATE OR REPLACE VIEW fact_automation_execution AS "
        + "\nUNION ALL BY NAME\n".join(parts)
    )


def _build_adk_detail_views(con, trace_cols: set) -> None:
    """The per-run detail that only ADK traces carry.

    Kept in their own views rather than folded into `fact_activity` because they
    are per-run lists, and because the measured/claimed separation below has to
    survive into the schema. `app/self_assessment.py` is explicit that a
    pipeline's measured checkpoint score and a model's opinion of its own turn
    must never merge into one number; two views is how that rule is expressed
    here, and a query that wants both has to ask for both by name.
    """
    empty = {
        "fact_run_checkpoint":
            "NULL::VARCHAR AS activity_id, NULL::VARCHAR AS stage, NULL::BOOLEAN AS ok",
        "fact_self_report":
            "NULL::VARCHAR AS activity_id, NULL::VARCHAR AS agent, NULL::DOUBLE AS score, "
            "NULL::VARCHAR AS went_well, NULL::VARCHAR AS could_improve",
        "fact_run_agent":
            "NULL::VARCHAR AS activity_id, NULL::VARCHAR AS agent, NULL::BIGINT AS turns, "
            "NULL::BIGINT AS function_calls, NULL::VARCHAR AS model",
        "fact_run_output":
            "NULL::VARCHAR AS activity_id, NULL::VARCHAR AS kind, NULL::BIGINT AS count",
        "fact_run_touchpoint":
            "NULL::VARCHAR AS activity_id, NULL::VARCHAR AS kind, NULL::BIGINT AS count",
    }

    def _empty(view: str) -> None:
        con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT {empty[view]} WHERE false")

    # The raw views were built once, up front, by `_build_raw_trace_views`; an
    # empty column set means there were no trace files to build them from.
    cols = trace_cols
    if not cols:
        for view in empty:
            _empty(view)
        return

    # `self_assessment` and `agents` are nested and were added at different
    # times, so each view is defined only if its source field exists at all.
    # An empty view is a truthful "nothing recorded this"; a missing view would
    # be an outage every caller would have to defend against.

    # Measured: the fraction of a run's declared checkpoints that passed, as
    # recorded by the stages themselves. No model is consulted.
    if "self_assessment" in cols and "run_id" in cols:
        con.execute("""
            CREATE OR REPLACE VIEW fact_run_checkpoint AS
            SELECT run_id AS activity_id,
                   unnest(self_assessment.checkpoints).stage  AS stage,
                   unnest(self_assessment.checkpoints).ok     AS ok
            FROM raw_adk_trace_latest
            WHERE run_id IS NOT NULL AND self_assessment IS NOT NULL
        """)
        # Claimed: a model grading its own turn. Useful as a lead, never as
        # evidence, and never averaged into the column above.
        con.execute("""
            CREATE OR REPLACE VIEW fact_self_report AS
            SELECT run_id AS activity_id,
                   -- Every field cast explicitly. A self-report is optional at
                   -- every level, so a trace file in which no stage offered one
                   -- leaves the list empty, DuckDB types its elements as JSON,
                   -- and the columns come back unjoinable and unaggregatable —
                   -- the view still builds, and only fails when something tries
                   -- to use it. Casting here makes the type independent of what
                   -- the sample happened to contain.
                   CAST(unnest(self_assessment.self_reports).agent AS VARCHAR)  AS agent,
                   CAST(unnest(self_assessment.self_reports).score AS DOUBLE)   AS score,
                   CAST(unnest(self_assessment.self_reports).went_well AS VARCHAR)
                       AS went_well,
                   CAST(unnest(self_assessment.self_reports).could_improve AS VARCHAR)
                       AS could_improve
            FROM raw_adk_trace_latest
            WHERE run_id IS NOT NULL AND self_assessment IS NOT NULL
        """)
    else:
        _empty("fact_run_checkpoint")
        _empty("fact_self_report")

    # What the run touched and produced, in the shared vocabulary. Stored as a
    # JSON map so the vocabulary can grow without a schema migration, and
    # unpivoted to one row per kind here so a caller counts drafts without
    # knowing which key the pipeline used. Absent entirely before trace v3.
    for view, field in (("fact_run_output", "produced"),
                        ("fact_run_touchpoint", "touched")):
        if "metrics" in cols and "run_id" in cols:
            # Round-tripped through JSON rather than read as a struct. DuckDB
            # infers a STRUCT with a fixed field list from the files it sees,
            # which is wrong for an open vocabulary: the first pipeline to emit
            # a new kind would either widen the struct for every row or not be
            # read at all. Going via JSON keeps the view correct for kinds that
            # do not exist yet, which is the whole point of the enum being
            # allowed to grow.
            con.execute(f"""
                CREATE OR REPLACE VIEW {view} AS
                WITH src AS (
                    SELECT run_id, to_json(metrics.{field}) AS j
                    FROM raw_adk_trace_latest
                    WHERE run_id IS NOT NULL AND metrics IS NOT NULL
                ),
                kv AS (
                    SELECT run_id, j, unnest(json_keys(j)) AS kind
                    FROM src WHERE json_type(j) = 'OBJECT'
                )
                SELECT run_id AS activity_id, kind,
                       CAST(json_extract_string(j, '$."' || kind || '"') AS BIGINT) AS count
                FROM kv
            """)
        else:
            con.execute(f"""
                CREATE OR REPLACE VIEW {view} AS
                SELECT NULL::VARCHAR AS activity_id, NULL::VARCHAR AS kind,
                       NULL::BIGINT AS count
                WHERE false
            """)

    if "agents" in cols and "run_id" in cols:
        fields = _struct_fields(con, "raw_adk_trace_latest", "agents")
        # `model` and the cache/reasoning token counters arrive with trace v3.
        # Before that an agent ran on a model the trace cannot name, and NULL
        # says exactly that — a blank string would claim we asked and got none.
        def field(name: str, sql_type: str) -> str:
            return (f"unnest(agents).{name}" if name in fields
                    else f"NULL::{sql_type}") + f" AS {name}"

        con.execute(f"""
            CREATE OR REPLACE VIEW fact_run_agent AS
            SELECT run_id AS activity_id,
                   unnest(agents).name           AS agent,
                   {field('turns', 'BIGINT')},
                   {field('function_calls', 'BIGINT')},
                   {field('model', 'VARCHAR')}
            FROM raw_adk_trace_latest
            WHERE run_id IS NOT NULL AND agents IS NOT NULL
        """)
    else:
        _empty("fact_run_agent")


def _build_eval_view(con) -> None:
    """Eval case outcomes, from the durable JSONL the grader now appends to.

    agents-cli's own per-case detail lands in `artifacts/`, which is not mounted
    and does not survive a container recreate — so before this the only record
    of an eval run was the terminal summary. Trend questions about pipeline
    quality were simply unanswerable.
    """
    pattern = _jsonl_glob("eval-results", "*.jsonl")
    if not _has_files(pattern):
        con.execute("""
            CREATE OR REPLACE VIEW fact_eval_case AS
            SELECT NULL::TIMESTAMP AS occurred_at, NULL::VARCHAR AS eval_set,
                   NULL::VARCHAR AS case_id, NULL::VARCHAR AS metric,
                   NULL::DOUBLE AS score, NULL::DOUBLE AS threshold,
                   NULL::BOOLEAN AS passed, NULL::VARCHAR AS app,
                   NULL::VARCHAR AS category, NULL::VARCHAR AS explanation
            WHERE false
        """)
        return
    con.execute(f"""
        CREATE OR REPLACE VIEW fact_eval_case AS
        -- `at` is a DuckDB keyword, so it cannot survive into the view
        -- unquoted; renamed here so every downstream query stays plain.
        SELECT try_cast("at" AS TIMESTAMP) AS occurred_at,
               eval_set, case_id, metric,
               CAST(score AS DOUBLE) AS score,
               CAST(threshold AS DOUBLE) AS threshold,
               CAST(passed AS BOOLEAN) AS passed,
               app, category, explanation
        FROM read_json_auto({_sql_str(pattern)}, union_by_name := true,
                            ignore_errors := true)
    """)


def _build_integration_view(con) -> None:
    """Outbound calls, already attributed to a run by `integration_log`.

    This is what makes Graphiti's *operations* roll up to the workflow that
    caused them today, before any token accounting exists for it: the consumer
    scope stamps `run_id` on every call, so the join is already there to be made.
    """
    pattern = _jsonl_glob("integration-calls", "*.jsonl")
    if not _has_files(pattern):
        con.execute("""
            CREATE OR REPLACE VIEW fact_integration_call AS
            SELECT NULL::VARCHAR AS activity_id, NULL::VARCHAR AS source,
                   NULL::VARCHAR AS consumer, NULL::VARCHAR AS capability,
                   NULL::VARCHAR AS operation, NULL::BOOLEAN AS ok,
                   NULL::VARCHAR AS error, NULL::TIMESTAMP AS occurred_at
            WHERE false
        """)
        return
    # `at` is a DuckDB keyword, so the log's own field name cannot survive into
    # the view unquoted; renaming it here keeps every downstream query plain.
    con.execute(f"""
        CREATE OR REPLACE VIEW fact_integration_call AS
        SELECT run_id AS activity_id, source, consumer, capability, operation,
               ok, error, timezone('UTC', to_timestamp("at")) AS occurred_at
        FROM read_json_auto({_sql_str(pattern)}, union_by_name := true, ignore_errors := true)
    """)


# --- queries ----------------------------------------------------------------


def _rows(con, sql: str, params: Optional[list] = None) -> List[dict]:
    """Run a query and materialize it, holding the connection lock throughout.

    The lock is not optional, and it has to span execute *and* fetch. A DuckDB
    connection carries the result of the last statement executed on it, and
    FastAPI runs these endpoints in a threadpool — so two overlapping requests
    on one connection let the second execute replace the first's result before
    it is read. That surfaced in production as `fetchone()` returning None for a
    `COUNT(*)`, which cannot otherwise happen, on a page that polls six routes
    at once. Single-threaded testing never reproduces it.
    """
    with _lock:
        cur = con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _scalar(con, sql: str):
    """One value, under the same lock and for the same reason."""
    with _lock:
        row = con.execute(sql).fetchone()
    return row[0] if row else None


def cost_summary(days: int = 30) -> dict:
    """Spend and usage, split by cost class and never totalled.

    Returns one line per class. A caller that wants a single headline number has
    to choose which class it means, which is the point: 'metered' is money, and
    the other two are activity that money was already spent on or that no rate
    exists for.
    """
    con = connect()
    rows = _rows(con, f"""
        SELECT cost_class,
               COUNT(DISTINCT activity_id)   AS activities,
               SUM(api_call_count)           AS api_calls,
               SUM(input_tokens)             AS input_tokens,
               SUM(output_tokens)            AS output_tokens,
               SUM(cache_read_tokens)        AS cache_read_tokens,
               SUM(cache_write_tokens)       AS cache_write_tokens,
               SUM(reasoning_tokens)         AS reasoning_tokens,
               SUM(CASE WHEN cost_class = 'metered' THEN cost_usd END) AS cost_usd
        FROM fact_llm_usage
        WHERE last_seen IS NULL OR last_seen >= timezone('UTC', now()) - to_days(CAST(? AS INTEGER))
        GROUP BY cost_class ORDER BY cost_class
    """, [days])
    return {
        "window_days": days,
        "classes": rows,
        # Stated rather than computed, so no caller can mistake the absence of a
        # total for an oversight.
        "note": "metered is spend; included and unpriced are usage with no dollar figure",
    }


def by_model(days: int = 30) -> List[dict]:
    con = connect()
    return _rows(con, f"""
        SELECT model, billing_provider, cost_class,
               COUNT(DISTINCT activity_id) AS activities,
               SUM(api_call_count)         AS api_calls,
               SUM(input_tokens)           AS input_tokens,
               SUM(output_tokens)          AS output_tokens,
               SUM(cache_read_tokens)      AS cache_read_tokens,
               SUM(reasoning_tokens)       AS reasoning_tokens,
               SUM(CASE WHEN cost_class = 'metered' THEN cost_usd END) AS cost_usd
        FROM fact_llm_usage
        WHERE last_seen IS NULL OR last_seen >= timezone('UTC', now()) - to_days(CAST(? AS INTEGER))
        GROUP BY ALL ORDER BY input_tokens DESC NULLS LAST
    """, [days])


def activity_summary(days: int = 30) -> List[dict]:
    """Activity counts per kind, with success reported only where it is defined.

    `outcome_known` is the sample size for `succeeded`/`failed`, and it is zero
    for every Hermes session — those have no success vocabulary, so the two
    counters come back NULL rather than 0. A caller rendering "0 failed" over a
    population that cannot fail would be stating a result nobody measured.
    """
    con = connect()
    return _rows(con, """
        SELECT kind, source, profile,
               COUNT(*)                                            AS activities,
               COUNT(outcome)                                      AS outcome_known,
               CASE WHEN COUNT(outcome) > 0
                    THEN COUNT(*) FILTER (WHERE outcome IN ('ok', 'partial'))
               END                                                 AS succeeded,
               CASE WHEN COUNT(outcome) > 0
                    THEN COUNT(*) FILTER (WHERE outcome = 'failed')
               END                                                 AS failed,
               MAX(started_at)                                     AS last_at
        FROM fact_activity
        WHERE started_at >= timezone('UTC', now()) - to_days(CAST(? AS INTEGER))
        GROUP BY ALL ORDER BY activities DESC
    """, [days])


def automation_executions(days: int = 30) -> List[dict]:
    """Scheduled-job executions, which are a different population from sessions.

    Counted separately and never added to `fact_activity`, because they do not
    overlap the way an intuition would expect: on this host 162 executions have
    produced 13 recorded LLM sessions. Most scheduled runs do their work without
    a model, so folding the two together would either inflate the automation
    count or imply that the missing executions cost something unrecorded.
    """
    con = connect()
    try:
        return _rows(con, """
            SELECT job_id, status,
                   COUNT(*)        AS executions,
                   MAX(started_at) AS last_at
            FROM fact_automation_execution
            WHERE started_at >= timezone('UTC', now()) - to_days(CAST(? AS INTEGER))
            GROUP BY ALL ORDER BY executions DESC
        """, [days])
    except Exception as exc:
        logger.warning("metrics_store: automation executions unavailable: %s", exc)
        return []


def automation_runs(job_id: str, days: int = 30, limit: int = 50) -> List[dict]:
    """One job's executions, newest first — the rows behind the counts above.

    `automation_executions` answers "how much did this run and how did it end";
    this answers "which run, and what did it say when it broke". The counts were
    the only thing exposed for a long time, which meant a failing automation
    could be seen failing and never inspected.

    Ordered and filtered by `claimed_at`, not `started_at`. A job that was
    claimed by the scheduler and then died before its process reported a start
    has a NULL `started_at`, and that execution is exactly the one worth
    reading — ordering by a column it does not have would drop it off the page.
    """
    con = connect()
    try:
        return _rows(con, """
            SELECT execution_id, job_id, source, status, profile,
                   claimed_at, started_at, finished_at, error,
                   CASE WHEN started_at IS NOT NULL AND finished_at IS NOT NULL
                        THEN date_diff('millisecond', started_at, finished_at)
                   END AS duration_ms
            FROM fact_automation_execution
            WHERE job_id = ?
              AND claimed_at >= timezone('UTC', now()) - to_days(CAST(? AS INTEGER))
            ORDER BY claimed_at DESC
            LIMIT ?
        """, [job_id, days, limit])
    except Exception as exc:
        logger.warning("metrics_store: automation runs unavailable: %s", exc)
        return []


def automation_totals(job_id: str, days: int = 30) -> dict:
    """Outcome tallies for one job over the window.

    Separate from the run list because the list is capped at `limit` and these
    are not: "3 of the last 50 failed" and "3 of 412 failed" are different
    claims, and only the second one is about the automation.
    """
    con = connect()
    try:
        rows = _rows(con, """
            SELECT status, COUNT(*) AS executions, MAX(claimed_at) AS last_at
            FROM fact_automation_execution
            WHERE job_id = ?
              AND claimed_at >= timezone('UTC', now()) - to_days(CAST(? AS INTEGER))
            GROUP BY ALL
        """, [job_id, days])
    except Exception as exc:
        logger.warning("metrics_store: automation totals unavailable: %s", exc)
        return {"total": 0, "by_status": {}, "last_at": None}
    by_status = {str(r["status"]): r["executions"] for r in rows}
    stamps = [r["last_at"] for r in rows if r["last_at"]]
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "last_at": max(stamps) if stamps else None,
    }


def timeseries(days: int = 30) -> List[dict]:
    """Daily activity and usage. Cost stays split by class here too."""
    con = connect()
    return _rows(con, """
        SELECT CAST(a.started_at AS DATE) AS day, a.kind,
               COUNT(DISTINCT a.activity_id) AS activities,
               SUM(c.input_tokens)  AS input_tokens,
               SUM(c.output_tokens) AS output_tokens,
               SUM(c.metered_cost_usd) AS metered_cost_usd
        FROM fact_activity a
        LEFT JOIN fact_activity_cost c ON c.activity_id = a.activity_id
        WHERE a.started_at >= timezone('UTC', now()) - to_days(CAST(? AS INTEGER))
        GROUP BY ALL ORDER BY day
    """, [days])


def outputs(days: int = 30) -> dict:
    """What the fleet produced and touched, by kind.

    The two are returned together but never added: `touched` is input volume
    (messages read, pages fetched) and `produced` is side effects that actually
    happened. The ratio between them is the interesting number; the sum is not
    a quantity at all.
    """
    con = connect()
    window = "started_at >= timezone('UTC', now()) - to_days(CAST(? AS INTEGER))"

    def side(view: str) -> List[dict]:
        return _rows(con, f"""
            SELECT f.kind, SUM(f.count) AS total,
                   COUNT(DISTINCT f.activity_id) AS runs
            FROM {view} f
            JOIN fact_activity a ON a.activity_id = f.activity_id
            WHERE a.{window}
            GROUP BY ALL ORDER BY total DESC
        """, [days])

    def daily(view: str) -> List[dict]:
        """One row per day per kind, for the outcome charts.

        Days with no activity are simply absent rather than zero-filled: the
        chart decides how to draw a gap, and a zero here would assert that a
        pipeline ran and produced nothing, which is a different claim from
        having not run.
        """
        return _rows(con, f"""
            SELECT CAST(a.started_at AS DATE) AS day, f.kind, SUM(f.count) AS total
            FROM {view} f
            JOIN fact_activity a ON a.activity_id = f.activity_id
            WHERE a.{window}
            GROUP BY ALL ORDER BY day
        """, [days])

    produced = side("fact_run_output")
    return {
        "window_days": days,
        "produced": produced,
        "touched": side("fact_run_touchpoint"),
        "daily_produced": daily("fact_run_output"),
        "daily_touched": daily("fact_run_touchpoint"),
        # Surfaced on its own because it is the number that says how much of
        # the fleet's output went out with nobody looking at it.
        "unattended_sends": next(
            (r["total"] for r in produced if r["kind"] == "auto_email"), None),
    }


def agent_scorecard(app: Optional[str] = None, days: int = 30) -> List[dict]:
    """Per agent element: what it did, what it spent, and how well it went.

    The measured and the claimed columns are computed separately and returned
    side by side without ever being combined. `checkpoint_pass_rate` is the
    fraction of that stage's declared checkpoints that passed — no model is
    consulted, and a pipeline cannot flatter itself. `self_score` is a model's
    own opinion of its turn, offered only by stages that make an LLM call
    anyway. Averaging the two would let an optimistic model raise a measured
    number, which is the exact failure app/self_assessment.py exists to prevent,
    so they stay in different columns with different sample sizes.
    """
    con = connect()
    clause = "AND a.app = ?" if app else ""
    params: list = [days] + ([app] if app else [])
    return _rows(con, f"""
        WITH runs AS (
            SELECT activity_id, app FROM fact_activity a
            WHERE a.kind = 'workflow_run'
              AND a.started_at >= timezone('UTC', now()) - to_days(CAST(? AS INTEGER))
              {clause}
        ),
        util AS (
            SELECT r.app, g.agent,
                   COUNT(DISTINCT g.activity_id) AS runs,
                   SUM(g.turns)                  AS turns,
                   SUM(g.function_calls)         AS tool_calls,
                   -- One model per agent is the norm; more than one means the
                   -- agent was re-pointed mid-window, which is worth seeing.
                   COUNT(DISTINCT g.model)       AS models_used,
                   MAX(g.model)                  AS model
            FROM fact_run_agent g JOIN runs r ON r.activity_id = g.activity_id
            GROUP BY ALL
        ),
        measured AS (
            SELECT r.app, c.stage AS agent,
                   COUNT(*)                             AS checkpoints,
                   AVG(CASE WHEN c.ok THEN 1.0 ELSE 0.0 END) AS checkpoint_pass_rate
            FROM fact_run_checkpoint c JOIN runs r ON r.activity_id = c.activity_id
            GROUP BY ALL
        ),
        claimed AS (
            SELECT r.app, s.agent,
                   COUNT(s.score)   AS self_scored_runs,
                   AVG(s.score)     AS self_score,
                   MAX(s.could_improve) AS could_improve
            FROM fact_self_report s JOIN runs r ON r.activity_id = s.activity_id
            GROUP BY ALL
        )
        SELECT COALESCE(u.app, m.app, c.app)     AS app,
               COALESCE(u.agent, m.agent, c.agent) AS agent,
               u.runs, u.turns, u.tool_calls, u.model, u.models_used,
               m.checkpoints, round(m.checkpoint_pass_rate, 3) AS checkpoint_pass_rate,
               c.self_scored_runs, round(c.self_score, 3) AS self_score,
               c.could_improve
        FROM util u
        FULL OUTER JOIN measured m ON m.app = u.app AND m.agent = u.agent
        FULL OUTER JOIN claimed  c ON c.app = COALESCE(u.app, m.app)
                                  AND c.agent = COALESCE(u.agent, m.agent)
        ORDER BY app, agent
    """, params)


def evals(days: int = 30) -> dict:
    """Eval outcomes per set and metric, plus the cases currently failing.

    `pass_rate` is over cases that actually ran in the window. Failing cases are
    listed with the grader's own explanation rather than a count, because "12
    failed" is not actionable and "BEHAVIOUR FAIL [injection] - obeyed injected
    instruction" is.
    """
    con = connect()
    window = "occurred_at >= timezone('UTC', now()) - to_days(CAST(? AS INTEGER))"
    summary = _rows(con, f"""
        SELECT eval_set, metric, app,
               COUNT(*)                              AS cases,
               COUNT(*) FILTER (WHERE passed)        AS passed,
               round(AVG(CASE WHEN passed THEN 1.0 ELSE 0.0 END), 3) AS pass_rate,
               MAX(occurred_at)                      AS last_at
        FROM fact_eval_case WHERE {window}
        GROUP BY ALL ORDER BY last_at DESC
    """, [days])
    failing = _rows(con, f"""
        SELECT case_id, category, explanation, occurred_at
        FROM fact_eval_case
        WHERE {window} AND NOT passed
        ORDER BY occurred_at DESC LIMIT 50
    """, [days])
    return {"window_days": days, "sets": summary, "failing": failing}


def health() -> dict:
    """What the store can currently see. Used by tests and the tab's footer."""
    con = connect()
    profiles = [p[0] for p in _profiles()]
    counts = {}
    for view in ("fact_activity", "fact_llm_usage", "fact_run_checkpoint",
                 "fact_self_report", "fact_run_agent", "fact_integration_call",
                 "fact_run_output", "fact_run_touchpoint", "fact_eval_case"):
        try:
            counts[view] = _scalar(con, f"SELECT COUNT(*) FROM {view}")
        except Exception as exc:
            counts[view] = f"error: {exc}"
    return {"profiles": profiles, "store_path": STORE_PATH, "counts": counts}
