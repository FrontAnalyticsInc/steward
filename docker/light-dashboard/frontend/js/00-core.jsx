// --- 00-core.jsx -----------------------------------------------------------
// Vocabulary the whole console shares: time and schedule formatting, the
// status words an automation is described in, URL<->tab routing, and the
// settings/kanban/scorecard constants. Nothing here renders a page - if two
// tabs would otherwise each invent a word for the same thing, it belongs here.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
        var { useState, useEffect, useLayoutEffect, useRef, useCallback } = React;

        // Draw Lucide icons WITHOUT lucide.createIcons().
        //
        // createIcons() replaces each <i data-lucide> node with a fresh <svg>.
        // Those <i> elements are rendered by React, so once they are swapped out
        // React's next update to that subtree tries to touch a node that is no
        // longer in the tree, throws, and unmounts the whole app — a blank page.
        // Painting the svg *inside* the <i> leaves React's node untouched.
        var renderIcons = () => {
            if (!window.lucide || !window.lucide.icons) return;
            document.querySelectorAll('[data-lucide]').forEach(el => {
                const name = el.getAttribute('data-lucide');
                if (!name || el.dataset.iconPainted === name) return;

                const pascal = name.split('-')
                    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
                    .join('');
                const icon = window.lucide.icons[pascal];
                if (!icon) return;

                const children = icon.map(([tag, attrs]) => {
                    const props = Object.entries(attrs)
                        .map(([k, v]) => `${k}="${String(v).replace(/"/g, '&quot;')}"`)
                        .join(' ');
                    return `<${tag} ${props}/>`;
                }).join('');

                el.innerHTML =
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" ' +
                    'width="100%" height="100%" fill="none" stroke="currentColor" ' +
                    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
                    children + '</svg>';
                el.dataset.iconPainted = name;
            });
        };

        // A bare five-field cron expression, for the older backends that send the
        // schedule as a plain string with no `kind` to read.
        var CRON_EXPR_RE = /^\S+(\s+\S+){4}$/;

        // Cron schedules arrive as an object ({kind, expr, run_at, display}) on newer
        // backends and as a plain string on older ones. Always render a string.
        //
        // A cron expression names a wall clock — and the scheduler reads that clock
        // in UTC, never in the display timezone (see cron/clock.py upstream). `0 7
        // * * *` beside a page rendering everything else in local time reads as 7am
        // local, which is the misreading this label exists to stop. Only `kind:
        // cron` gets it: an interval ("every 10m") and a one-shot's countdown
        // ("once in 10m") are durations, and a zone on a duration is noise. The
        // one-shot's actual instant is rendered by fmtCronStamp, which labels itself.
        var formatSchedule = (job) => {
            if (!job) return '';
            const s = job.schedule;
            const kind = s && typeof s === 'object' ? s.kind : null;
            let text;
            if (typeof job.schedule_display === 'string' && job.schedule_display) text = job.schedule_display;
            else if (!s) return 'unscheduled';
            else if (typeof s === 'string') text = s;
            else if (typeof s === 'object') text = s.display || s.expr || s.run_at || s.kind || 'unscheduled';
            else text = String(s);
            const isCron = kind === 'cron' || (kind === null && CRON_EXPR_RE.test(text));
            return isCron ? `${text} UTC` : text;
        };

        // --- Automations: the three derived columns ---
        // Timestamps come off cron as naive-looking ISO strings that are in fact
        // UTC. Date.parse treats an unsuffixed string as *local* time, which on
        // any non-UTC host shifts every age by the offset and invents staleness
        // that isn't there. Force the Z when it is absent.
        var parseStamp = (v) => {
            if (!v) return null;
            const s = String(v);
            const iso = /(Z|[+-]\d{2}:?\d{2})$/.test(s) ? s : s + 'Z';
            const t = Date.parse(iso);
            return Number.isNaN(t) ? null : t;
        };

        // "14h ago" — coarse on purpose. The point of this column is to be read
        // against the schedule beside it, not to be precise on its own.
        //
        // Deliberately not fmtAgo (below), which the Integrations screen uses.
        // Two differences that are contract, not style: that one takes epoch
        // seconds where cron gives ISO strings, and it reads Date.now() itself
        // where this takes an injected `now`. The injection is what lets the
        // 30s clock tick drive a re-render, and what makes staleness testable
        // without freezing time. Collapsing them means changing Integrations.
        var relativeAge = (v, now) => {
            const t = parseStamp(v);
            if (t === null) return '—';
            const secs = Math.round((now - t) / 1000);
            if (secs < 0) return 'in ' + relativeAge(v, now - 2 * (now - t)).replace(' ago', '');
            if (secs < 60) return `${secs}s ago`;
            if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
            if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
            return `${Math.floor(secs / 86400)}d ago`;
        };

        // How long past due before "late" becomes "broken". The gateway's ticker
        // runs about every 60s, so a job is routinely a few seconds overdue in
        // normal operation and flagging that would make the column cry wolf.
        // Scale with the period where we know it — being 2 minutes late means
        // something very different for a 10-minute job than for a daily one.
        var staleGraceMs = (job) => {
            const s = job && job.schedule;
            if (s && typeof s === 'object' && s.kind === 'interval') {
                const mins = Number(s.minutes || 0);
                if (mins > 0) return Math.max(mins * 60000 * 0.5, 120000);
            }
            return 900000; // 15m, for cron and anything we cannot period-check
        };

        // What kind of thing runs an automation, with the identity underneath.
        // Derivable entirely from what /api/cron/jobs already returns — no new
        // backend field. Three real cases:
        //   workflow — launches an ADK app (adk_cron_link recovered the name)
        //   script   — no_agent, so no LLM is in the loop at all
        //   agent    — a prompt job, run by the Hermes profile that owns it
        //
        // The label is always the kind and never the identity. It used to return
        // the profile name in the third case, which meant the column answered
        // "what kind of thing is this" on two rows and "whose is this" on the
        // third — one column asking two questions, so no header could be true of
        // every row. Naming it after the agent instead would have been worse: a
        // `no_agent` script has no agent, and filing it under one would claim
        // something false about the only rows where it matters most.
        var automationWhere = (job) => {
            if (job.adk_app) return { label: 'workflow', detail: tail(job.adk_app) };
            if (job.runs_agent === false || job.no_agent) return { label: 'script', detail: job.script || '' };
            return { label: 'agent', detail: job.agent || 'default' };
        };

        // One glyph per kind, so "workflow" looks the same everywhere it is
        // said: the Where column, the sidebar tally, and the node the expanded
        // row draws. Three separate renderers used to decide this themselves,
        // and only one of them had icons at all — a reader had to re-learn the
        // vocabulary in each place. Same shape as STATUS_STYLE, deliberately.
        var WHERE_KINDS = {
            workflow: { icon: 'git-branch', color: 'var(--acc-blue)' },
            agent:    { icon: 'bot',        color: 'var(--acc-mauve)' },
            script:   { icon: 'file-code',  color: 'var(--acc-green)' },
        };
        var whereKind = (label) => WHERE_KINDS[label] || WHERE_KINDS.agent;

        // The status column, which is the whole reason Schedule earns its place:
        // "last run 14h ago" is meaningless alone and damning next to "every
        // 10m". Overdue is judged on next_run_at rather than by parsing the
        // schedule — the scheduler already did that arithmetic, and trusting it
        // means this works for cron, interval and one-shot alike.
        var automationStatus = (job, now) => {
            if (job.enabled === false || job.paused_at) {
                return { kind: 'off', label: job.paused_at ? 'paused' : 'off' };
            }
            const next = parseStamp(job.next_run_at);
            const overdue = next !== null && now - next > staleGraceMs(job);
            const status = job.last_status;
            if (overdue) {
                // Both facts, in one cell: it is not firing, and the last time
                // it did it failed. Either alone would send you the wrong way.
                return {
                    kind: 'stale',
                    label: status === 'error' ? 'stale · error' : 'stale',
                    overdueSince: next,
                };
            }
            if (status === 'ok') return { kind: 'ok', label: 'ok' };
            if (status === 'error') return { kind: 'error', label: 'error' };
            return { kind: 'never', label: 'never run' };
        };

        // `pill` is set only on the two states that want you to stop scanning.
        // Giving every state a filled badge would spend the same emphasis on a
        // healthy job as on a broken one, and a table where every row shouts is
        // a table with no signal in it — so ok/off/never stay quiet text.
        var STATUS_STYLE = {
            ok:    { color: 'text-[#a6e3a1]', icon: 'check' },
            error: { color: 'text-[#f38ba8]', icon: 'x', pill: 'bg-[#f38ba8]/15 border-[#f38ba8]/50' },
            stale: { color: 'text-[#fab387]', icon: 'alert-triangle', pill: 'bg-[#fab387]/15 border-[#fab387]/50' },
            off:   { color: 'text-[#585b70]', icon: 'pause' },
            never: { color: 'text-[#585b70]', icon: 'minus' },
        };

        // One automation's status, as the table shows it. A pill when something
        // is wrong, plain text when nothing is — same words either way, so the
        // badge adds weight without adding a second vocabulary to learn.
        function StatusPill({ status }) {
            const style = STATUS_STYLE[status.kind] || STATUS_STYLE.never;
            return style.pill ? (
                <span class={`inline-flex items-center gap-1.5 font-mono text-xs font-bold px-2 py-1 rounded-full border whitespace-nowrap ${style.pill} ${style.color}`}>
                    <i data-lucide={style.icon} class="w-3.5 h-3.5 shrink-0"></i>
                    {status.label}
                </span>
            ) : (
                <span class={`inline-flex items-center gap-1.5 font-mono text-xs whitespace-nowrap ${style.color}`}>
                    <i data-lucide={style.icon} class="w-3.5 h-3.5 shrink-0"></i>
                    {status.label}
                </span>
            );
        }

        // --- URL <-> tab routing ---
        // Each tab owns a path (/chat, /cron, ...) so tabs are linkable and the
        // browser's back/forward buttons move between them. The backend serves
        // index.html for each of these paths, so a deep link loads the app
        // directly on the right tab.
        // 'automations' is the monitoring surface: every scheduled thing on this
        // host, sorted by what it does for you rather than by what runs it.
        // Where it runs is a column, not a category — which is what lets one
        // flat list cover ADK workflows, bare scripts and agent-prompt jobs
        // without splitting into sections that answer a question nobody asked.
        //
        // 'agents' is still the roster — every ADK team and the scorecard for
        // each of its agents — but it is a drill-in, not a top-level heading.
        // It stays in TAB_PATHS (the route works, and an automation row links
        // into it) while having no button in the nav. Sorting by *who* is the
        // question you ask about one automation, after the list told you which.
        //
        // 'memory' was called 'graph' when it still drew one. It is now the
        // markdown store — documents, search, one document, and a graph of the
        // wikilinks between them — and every one of those is a page with its
        // own address under /memory. The old name survives as an alias, and
        // the word 'graph' is reused for what it actually describes: the page
        // at /memory/graph.
        var TAB_PATHS = ['metrics', 'chat', 'kanban', 'automations', 'agents', 'integrations', 'review', 'memory', 'setup'];
        var DEFAULT_TAB = 'chat';
        // /cron is the schedule question, so it lands on Automations now — it
        // was only ever filed under Agents because that tab had absorbed it.
        // Skills, Context and Agent Teams stay pointed at the Agents roster.
        //
        // /approvals is the review tab's old name. Not everything waiting there
        // is a thing to approve — a proposed memory fact or a CRM todo is a
        // thing to *look at* — and "approvals" described the button rather than
        // the work. The alias keeps links already written down working, and the
        // URL effect rewrites them to /review on arrival so they stop being old.
        var TAB_ALIASES = {
            skills: 'agents', context: 'agents', teams: 'agents',
            cron: 'automations',
            approvals: 'review',
            // /graph is the memory tab's old name, from when the tab was a
            // Cytoscape canvas over Neo4j. Same treatment as /approvals: the
            // link keeps working and the URL effect rewrites it to /memory on
            // arrival, so it stops being an old link.
            graph: 'memory',
        };

        // A memory page. 'documents' is the tab's own index and has no segment
        // of its own — /memory, not /memory/documents — for the same reason
        // /chat is the draft conversation: the bare tab path has to name a
        // state you can go Back to.
        var MEMORY_VIEWS = ['documents', 'search', 'document', 'graph'];
        var DEFAULT_MEMORY_VIEW = 'documents';

        // The docs are their own container on the host (docker/docs), not a tab
        // here — they describe the whole stack, including the parts this console
        // cannot show you.
        //
        // Derived from the current hostname rather than hardcoded to 127.0.0.1:
        // this dashboard is reachable over the LAN, and a hardcoded loopback URL
        // would send a remote browser to its own machine. The docs port is
        // loopback-bound, so off-host it will not connect either way — but
        // failing against the right host beats silently resolving to the wrong
        // one. Point this at the published site once there is one.
        var DOCS_URL = `${window.location.protocol}//${window.location.hostname}:9121`;

        // Hermes's own dashboard, for the few things this page deliberately
        // does not do — an MCP OAuth sign-in needs a browser redirect back to
        // the host that started the flow, so it belongs where that flow lives.
        // Derived from the current host for the same reason DOCS_URL is: a
        // hardcoded loopback URL would send a LAN browser to its own machine.
        var HERMES_DASHBOARD_URL = `${window.location.protocol}//${window.location.hostname}:9119`;

        // --- Settings ---
        // A section is a page of its own inside the settings overlay, and its
        // id is a URL segment (/settings/channels), so a section can be linked
        // to and Back steps out of it the way it steps out of anything else.
        var SETTINGS_SECTIONS = [
            { id: 'channels', label: 'Channels', icon: 'message-square',
              blurb: 'Where you can reach the default agent from.' },
            // The pair to Channels: that is how you reach the agent, this is
            // everything the system can reach outward. One page rather than
            // four, because the four are only understandable against each
            // other — the point of the workflows' own Attio token is that it
            // is *not* the assistant's, and split across separate pages that
            // comparison is something you have to remember rather than see.
            { id: 'integrations', label: 'Integrations', icon: 'plug',
              blurb: 'What this system can reach, and as whom.' },
            { id: 'appearance', label: 'Appearance', icon: 'palette',
              blurb: 'How this dashboard looks in this browser.' },
            // Last, and the only section that is purely a readout. It answers
            // the question every support conversation opens with — what are you
            // actually running — and the one the console can otherwise only
            // answer by staying silent: whether an upgrade stopped halfway.
            { id: 'about', label: 'About', icon: 'info',
              blurb: 'What this deployment is running, and how to move it forward.' },
        ];
        var DEFAULT_SETTINGS_SECTION = 'channels';

        // Sections that were once their own page and are now part of another.
        // Kept because a URL that worked yesterday should work today: the chat
        // sidebar shipped links to /settings/connections, and those are in
        // browser histories and possibly in someone's notes.
        var SETTINGS_ALIASES = { connections: 'integrations' };
        var resolveSettingsSection = (raw) => {
            const id = SETTINGS_ALIASES[raw] || raw;
            return SETTINGS_SECTIONS.some(s => s.id === id) ? id : DEFAULT_SETTINGS_SECTION;
        };

        // The channel list is not held here. /api/channels returns Hermes's own
        // messaging-platform catalog — which channels exist, which are
        // configured, and every env var each one needs, with its prompt, help
        // text and docs link. A copy of that schema in this file would be a
        // second source of truth for what a Slack token is called, and the
        // gateway reads only the first one.
        //
        // What a channel's state means, in Hermes's vocabulary:
        //   connected  — configured and enabled; the gateway will run it
        //   disabled   — not turned on (whether or not credentials exist)
        //   error      — turned on, but the adapter reported a problem
        var CHANNEL_STATES = {
            connected: { label: 'connected', color: 'var(--acc-green)', mark: '✓' },
            error:     { label: 'error',     color: 'var(--acc-red)',   mark: '✗' },
            disabled:  { label: 'off',       color: 'var(--txt-muted)', mark: '—' },
            unknown:   { label: 'unknown',   color: 'var(--txt-muted)', mark: '?' },
        };
        var channelState = (s) => CHANNEL_STATES[s] || CHANNEL_STATES.unknown;

        // The theme is the one genuinely per-browser preference left: it
        // describes this screen, not the host, so there is nothing on the host
        // to write it to. Channels went the other way — they are host state,
        // and they now live where the gateway reads them.
        var PREF_THEME = 'hermes.theme';        // 'light' | 'dark' | 'system'

        // The theme is stored as a bare string, not JSON: the bootstrap in
        // <head> reads it before anything else on the page exists and has to
        // stay a few lines long. Read and write it the same way it does.
        var readTheme = () => {
            try {
                const raw = localStorage.getItem(PREF_THEME);
                return raw === 'light' || raw === 'dark' || raw === 'system' ? raw : 'system';
            } catch (err) { return 'system'; }
        };
        var systemTheme = () =>
            (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');

        // A path carries more than the tab. The Agents tab can address one agent
        // of either kind and the Chat tab can address one conversation, so the
        // shape is:
        //
        //   /chat                                the chat tab, nothing open
        //   /chat/<session id>                   that conversation
        //   /kanban                              the board, nothing pinned
        //   /kanban/<task id>                    that task's detail
        //   /automations                         the list, nothing open
        //   /automations/<job id>                that automation in full
        //   /agents                              org view
        //   /agents/scorecard                    scorecard, whole team
        //   /agents/scorecard/<app>/<agent>      scorecard, focused on one agent
        //   /agents/hermes/<profile>             one Hermes agent
        //   /agents/hermes/<profile>/<job id>    that agent, focused on one job
        //
        // A Hermes agent is as much an agent on this host as a workflow team is,
        // and it was the one thing here with no address of its own — its jobs
        // were selectable but not linkable, so a job you found could not be sent
        // to anyone. Only the tab segment is case-folded; app, agent, profile and
        // session names are matched verbatim against what their sources declare.
        var routeFromLocation = () => {
            const segs = window.location.pathname.replace(/^\/+|\/+$/g, '').split('/').filter(Boolean);
            const head = (segs[0] || '').toLowerCase();
            const tab = TAB_PATHS.includes(head) ? head : (TAB_ALIASES[head] || DEFAULT_TAB);
            const none = { tab, teamAgent: null, hermesAgent: null, jobId: null, settings: null, health: false, sessionId: null, taskId: null, automationId: null, metricsView: null, reviewId: null, memoryView: DEFAULT_MEMORY_VIEW, memorySlug: null };
            // Stack health is an overlay like settings, and addressable for the
            // same two reasons: Back has to leave it, and "the host is unwell"
            // is the most linkable thing on this console — /health is what you
            // paste into a message when you are telling someone to go look.
            //
            // No sub-path. Settings has sections you can be sent to; health is
            // one page whose content is whatever is true right now, and a
            // per-service URL would address a row that may not be there.
            if (head === 'health') return { ...none, health: true };
            // Settings is an overlay rather than a tab: it covers whichever tab
            // you opened it from, and that tab is still the thing you go back
            // to. So it carries its own path but leaves `tab` alone — on a cold
            // load of /settings there is no tab underneath and DEFAULT_TAB is
            // what closing it lands on.
            if (head === 'settings') {
                const section = (segs[1] || '').toLowerCase();
                return { ...none, settings: resolveSettingsSection(section) };
            }
            // A missing session segment is meaningful rather than absent: /chat
            // is the new, unsent conversation, which is a state you can go Back
            // to from the one you opened out of it. Read off `head` rather than
            // `tab` so an unknown path falling back to the chat tab does not
            // also hand its second segment over as a session id.
            if (tab === 'chat') return { ...none, sessionId: head === 'chat' ? (segs[1] || null) : null };
            // A task is the unit of work people talk about ("look at t_6e6b2492"),
            // and it had no address — you could only tell someone to open the tab
            // and hunt. Same shape as chat: a missing segment means the tab with
            // nothing pinned, which is a state Back can return to.
            if (tab === 'kanban') return { ...none, taskId: head === 'kanban' ? (segs[1] || null) : null };
            // One automation, in full. Read off `head` rather than `tab` for the
            // same reason chat does: /cron aliases onto this tab, and its second
            // segment is not a job id.
            if (tab === 'automations') {
                return { ...none, automationId: head === 'automations' ? (segs[1] || null) : null };
            }
            // A review item is addressable, which is most of why this tab was
            // rebuilt: selection used to be an array index, so an item could be
            // read but never linked, and Back did nothing inside the tab.
            //
            // Unlike the aliases above, BOTH spellings carry a second segment.
            // /cron drops its because /cron/<x> never meant anything; /approvals
            // is a straight rename, so /approvals/<id> is a link someone may
            // already have, and dropping the id would silently land them on the
            // queue instead of the item they were sent.
            if (tab === 'review') {
                const addressable = head === 'review' || head === 'approvals';
                return { ...none, reviewId: addressable ? (segs[1] || null) : null };
            }
            // The Metrics tab has two pages that answer different questions:
            // what the fleet produced (the default) and how the machine is
            // running. The second gets its own address because "here is what the
            // system is costing" is a thing you send someone, and because Back
            // out of it should return to the outcomes page rather than the
            // previous tab.
            if (tab === 'metrics') {
                const view = (segs[1] || '').toLowerCase();
                return { ...none, metricsView: head === 'metrics' && view === 'system' ? 'system' : null };
            }
            // The memory tab's four pages. Read off `head` rather than `tab`
            // for the reason chat and automations do: /graph aliases onto this
            // tab and carries no segments of its own, so its (absent) second
            // segment must not be read as a view.
            //
            // A slug is decoded because it is written encoded — a document is
            // keyed on an email address, and while the characters in one are
            // legal in a path segment, round-tripping through the encoder is
            // what makes that a property of the code rather than a hope about
            // which addresses turn up.
            if (tab === 'memory') {
                if (head !== 'memory') return none;
                const view = (segs[1] || '').toLowerCase();
                const slug = segs[2] ? decodeURIComponent(segs[2]) : null;
                // /memory/<slug> with no view segment is not a route this
                // writes, but it is the one a person types. Treat a segment
                // that is not a view as the document it looks like.
                if (!MEMORY_VIEWS.includes(view) || view === 'documents') {
                    return segs[1] && !MEMORY_VIEWS.includes(view)
                        ? { ...none, memoryView: 'document', memorySlug: decodeURIComponent(segs[1]) }
                        : none;
                }
                if (view === 'document') {
                    // A document page with no document is the list — there is
                    // nothing else it could show.
                    return slug ? { ...none, memoryView: 'document', memorySlug: slug } : none;
                }
                // The graph, unlike a document, is a page in its own right
                // with or without a focus: /memory/graph is the whole store.
                return { ...none, memoryView: view, memorySlug: view === 'graph' ? slug : null };
            }
            if (tab !== 'agents') return none;
            const kind = (segs[1] || '').toLowerCase();
            if (kind === 'scorecard') {
                const [app, agent] = [segs[2], segs[3]];
                return { ...none, teamAgent: app && agent ? `${app}::${agent}` : null };
            }
            if (kind === 'hermes' && segs[2]) {
                // /agents/hermes/<profile>/<job id> used to open the profile with
                // one job highlighted — a job's configuration, on a page about
                // its owner, with its runs somewhere else entirely. The job now
                // has a page of its own, so a URL naming one lands there. The
                // profile keeps its address; only the two-part form moves.
                if (segs[3]) return { ...none, tab: 'automations', automationId: segs[3] };
                return { ...none, hermesAgent: segs[2], jobId: null };
            }
            return none;
        };

        var tabFromLocation = () => routeFromLocation().tab;

        // The inverse of routeFromLocation. One function so a URL the app writes
        // is always a URL it can read back — and it takes the same object shape
        // routeFromLocation returns, so the round trip is literal rather than a
        // positional argument list the two ends have to agree on by counting.
        //
        // The /scorecard/ segment predates the merge, when an agent's numbers
        // were a separate mode of the Agents tab. It is kept verbatim so links
        // already written down still land on the right agent — the tab simply
        // has no mode to switch any more, so selecting an agent *is* the route.
        var pathForRoute = ({ tab, teamAgent, hermesAgent, jobId, settings, health, sessionId, taskId, automationId, metricsView, reviewId, memoryView, memorySlug }) => {
            // Before settings, matching the stacking order on screen: health
            // opens over settings, so while it is open it is the view, and
            // closing it has to leave the settings path underneath intact.
            if (health) return '/health';
            if (settings) return '/settings/' + settings;
            if (tab === 'metrics') return metricsView === 'system' ? '/metrics/system' : '/metrics';
            if (tab === 'chat') return sessionId ? `/chat/${sessionId}` : '/chat';
            if (tab === 'kanban') return taskId ? `/kanban/${taskId}` : '/kanban';
            if (tab === 'automations') return automationId ? `/automations/${automationId}` : '/automations';
            // Only ever the new spelling. routeFromLocation still reads
            // /approvals/<id>, so an old link opens the right item and the URL
            // effect then replaceStates it to /review/<id> — the rename applies
            // itself on arrival, without an extra history entry to Back through.
            if (tab === 'review') return reviewId ? `/review/${reviewId}` : '/review';
            // Only ever /memory. routeFromLocation still reads /graph, so an
            // old link opens the tab and this then replaceStates the address
            // bar to the new spelling without an extra history entry.
            if (tab === 'memory') {
                if (memoryView === 'document' && memorySlug) return `/memory/document/${encodeURIComponent(memorySlug)}`;
                if (memoryView === 'graph') return memorySlug ? `/memory/graph/${encodeURIComponent(memorySlug)}` : '/memory/graph';
                if (memoryView === 'search') return '/memory/search';
                return '/memory';
            }
            if (tab !== 'agents') return '/' + tab;
            // No job segment. A profile addresses a profile; a job addresses
            // /automations/<id>, which is the only page that can show one whole.
            // routeFromLocation still *reads* the two-part form and sends it
            // there, so links written before this still land somewhere better —
            // but nothing writes it any more, and the round trip stays literal.
            if (hermesAgent) return `/agents/hermes/${hermesAgent}`;
            if (!teamAgent) return '/agents';
            const [app, name] = String(teamAgent || '').split('::');
            return app && name ? `/agents/scorecard/${app}/${name}` : '/agents';
        };

        // --- ADK scorecard formatting ---
        // "not recorded" is a distinct state from zero. Traces written before the
        // utilization instrumentation carry a literal 0 that was never a
        // measurement, so anything unmeasured renders as a dash, never a number.
        var fmtMs = (ms) => {
            if (ms === null || ms === undefined) return '—';
            if (ms < 1000) return `${Math.round(ms)}ms`;
            if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
            return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
        };
        var fmtPct = (v) => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`);
        // The exact number, dash for null — no thousands abbreviation. Used by
        // the Sankey, where the node labels ARE the counts being traced through
        // the diagram and rounding 3180 to "3.2k" loses the thing being read.
        // Named apart from 10-metrics.jsx's fmtNum, which abbreviates, because
        // both are global here and the two are genuinely different rules.
        var fmtExact = (v) => (v === null || v === undefined ? '—' : String(v));

        // OAuth token expiry, as time remaining rather than a date. "expires in 3h"
        // is actionable on a tab you check when something stopped working; a
        // timestamp makes you do the subtraction yourself.
        var fmtExpiry = (epochSeconds) => {
            if (!epochSeconds) return null;
            const secs = epochSeconds - Date.now() / 1000;
            if (secs <= 0) return 'expired';
            if (secs < 3600) return `expires in ${Math.round(secs / 60)}m`;
            if (secs < 86400) return `expires in ${Math.round(secs / 3600)}h`;
            return `expires in ${Math.round(secs / 86400)}d`;
        };

        // A context file's YAML frontmatter, split from its prose.
        //
        // Shown as a labelled block rather than rendered as markdown, because a
        // `---` fence renders as a horizontal rule and the keys under it as one
        // run-on paragraph — the frontmatter on a skill or memory file is
        // structured metadata and reads as such or not at all.
        //
        // The reading pane called this before it existed anywhere in the file,
        // so opening any context document threw a ReferenceError mid-render and
        // the ErrorBoundary replaced the entire dashboard with an error card.
        var splitFrontmatter = (text) => {
            const body = String(text || '');
            if (!body.startsWith('---')) return { frontmatter: null, body };
            // Closing fence on its own line. A document that opens a fence and
            // never closes it is not frontmatter — it is prose beginning with a
            // rule, and slicing it would eat the file.
            const end = body.indexOf('\n---', 3);
            if (end === -1) return { frontmatter: null, body };
            const rest = body.slice(end + 4);
            return {
                frontmatter: body.slice(3, end).trim() || null,
                body: rest.startsWith('\n') ? rest.slice(1) : rest,
            };
        };

        // How long ago something happened, in the coarsest unit that is still
        // true. The Integrations screen is read to answer "is this still
        // running", and "6d ago" answers it where a timestamp makes you do the
        // subtraction. Null is "never", which is a real state there, not a gap.
        var fmtAgo = (epochSeconds) => {
            if (!epochSeconds) return null;
            const secs = Date.now() / 1000 - epochSeconds;
            if (secs < 90) return 'just now';
            if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
            if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
            return `${Math.round(secs / 86400)}d ago`;
        };

        // Kanban writes its timestamps as epoch *seconds*; JS Date wants
        // milliseconds. Handing one straight to new Date() is what dated every
        // task on the board to January 1970 — a real timestamp, off by a factor
        // of a thousand, which reads as a bug in the task rather than in the
        // rendering. Anything already past the year 2001 in ms is left alone, and
        // an ISO string is parsed as itself.
        var epochMs = (v) => {
            if (v === null || v === undefined || v === '') return null;
            if (typeof v === 'string' && !/^\d+(\.\d+)?$/.test(v)) {
                const parsed = Date.parse(v);
                return Number.isNaN(parsed) ? null : parsed;
            }
            const n = Number(v);
            if (!Number.isFinite(n) || n <= 0) return null;
            return n < 1e12 ? n * 1000 : n;
        };

        // Date plus time, because "when did this task start" and "is this the run
        // I just kicked off" are the same question on a board that turns over
        // several times an hour.
        // Rendered in the user's configured Hermes timezone, not the browser's,
        // and always labelled. Before this the dashboard used the machine zone
        // with no zone name printed, so a timestamp rendered in the wrong clock
        // was indistinguishable from a correct one.
        //
        // HERMES_TZ is fetched once at boot (see loadDisplayTimezone). Until it
        // arrives — and if the fetch fails — these fall back to the browser
        // zone, which is the old behavior and never worse than it.
        var HERMES_TZ = null;
        var SCHEDULER_TZ = 'UTC';
        var _dtfCache = new Map();
        var dtf = (opts) => {
            const key = JSON.stringify(opts) + '|' + (HERMES_TZ || '');
            let f = _dtfCache.get(key);
            if (!f) {
                try {
                    f = new Intl.DateTimeFormat([], HERMES_TZ ? { ...opts, timeZone: HERMES_TZ } : opts);
                } catch (e) {
                    // A stale or malformed configured zone must not blank out
                    // every timestamp on the page.
                    f = new Intl.DateTimeFormat([], opts);
                }
                _dtfCache.set(key, f);
            }
            return f;
        };

        var loadDisplayTimezone = async () => {
            try {
                const r = await fetch('/api/timezone');
                if (!r.ok) return;
                const j = await r.json();
                if (j && typeof j.timezone === 'string' && j.timezone) {
                    HERMES_TZ = j.timezone;
                    SCHEDULER_TZ = j.scheduler_timezone || 'UTC';
                    _dtfCache.clear();
                }
            } catch (e) { /* keep the browser zone */ }
        };
        loadDisplayTimezone();

        var fmtStamp = (v) => {
            const ms = epochMs(v);
            if (ms === null) return '—';
            return dtf({
                year: 'numeric', month: 'numeric', day: 'numeric',
                hour: '2-digit', minute: '2-digit', timeZoneName: 'short'
            }).format(new Date(ms));
        };

        // Cron times specifically. Cron runs on UTC by design and does NOT
        // follow the display timezone (see cron/clock.py upstream), so showing
        // one clock would misrepresent when a job actually fires. UTC leads
        // because that is what the schedule literally means; the local
        // equivalent follows so the user can plan around it.
        var dtfUtc = new Intl.DateTimeFormat([], {
            year: 'numeric', month: 'numeric', day: 'numeric',
            hour: '2-digit', minute: '2-digit', timeZone: 'UTC'
        });
        var fmtCronStamp = (v) => {
            // parseStamp, not epochMs: cron emits ISO strings and epochMs uses a
            // bare Date.parse, which reads an unsuffixed one as browser-local
            // and would shift a cron time by the offset. Falls back to epochMs
            // for the numeric forms parseStamp cannot take.
            const ms = parseStamp(v) ?? epochMs(v);
            if (ms === null || ms === undefined) return '—';
            const d = new Date(ms);
            const utc = dtfUtc.format(d) + ' UTC';
            if (!HERMES_TZ || HERMES_TZ === SCHEDULER_TZ) return utc;
            const local = dtf({
                hour: '2-digit', minute: '2-digit', timeZoneName: 'short'
            }).format(d);
            return `${utc} (${local})`;
        };

        // --- Kanban liveness ---
        // A task's status column says "running" from the moment it is claimed
        // until something writes a different word there, which a killed container
        // never does. So a status of running is a claim, not evidence. The dispatcher
        // heartbeats the task about once a minute while it holds it; a beat inside
        // this window is the evidence. Three missed beats before we stop believing
        // it — long enough that a slow tool call is not called dead.
        var KANBAN_HEARTBEAT_STALE_SECS = 200;

        // Bare heartbeats are a pulse, not news: they carry no payload and there
        // are dozens per task. They set the liveness line; everything else is what
        // the agent actually did and gets a row of its own.
        var isBareHeartbeat = (e) => e && e.kind === 'heartbeat' && !kanbanEventNote(e);

        // The note an event carries, if any. Payload is a JSON string on the wire
        // and occasionally already an object; a heartbeat's `note` is the agent
        // saying what it is doing right now, which is the whole point of showing
        // this at all.
        var kanbanEventNote = (e) => {
            if (!e || !e.payload) return '';
            let p = e.payload;
            if (typeof p === 'string') {
                try { p = JSON.parse(p); } catch (err) { return p.trim(); }
            }
            if (!p || typeof p !== 'object') return String(p || '').trim();
            if (typeof p.note === 'string' && p.note.trim()) return p.note.trim();
            if (typeof p.message === 'string' && p.message.trim()) return p.message.trim();
            if (typeof p.summary === 'string' && p.summary.trim()) return p.summary.trim();
            if (typeof p.reason === 'string' && p.reason.trim()) return p.reason.trim();
            const pairs = Object.entries(p)
                .filter(([, v]) => v !== null && v !== undefined && typeof v !== 'object')
                .map(([k, v]) => `${k}: ${v}`);
            return pairs.join(' · ');
        };

        // What the detail panel knows about a task that is claiming to run.
        // Derived from the runs and events the detail endpoint already returned
        // and never rendered.
        var kanbanLiveness = (detail) => {
            const runs = (detail && detail.runs) || [];
            const events = (detail && detail.events) || [];
            const openRun = runs.find(r => !r.ended_at && String(r.status || '').toLowerCase() === 'running') || null;
            const beats = events.filter(e => e.kind === 'heartbeat');
            const lastEventAt = events.reduce((max, e) => Math.max(max, Number(e.created_at) || 0), 0);
            const lastBeatAt = beats.reduce((max, e) => Math.max(max, Number(e.created_at) || 0), 0);
            // Fall back to the run's own start: a task claimed seconds ago is
            // alive even though its first heartbeat has not landed yet.
            const pulseAt = Math.max(lastBeatAt, lastEventAt, openRun ? (Number(openRun.started_at) || 0) : 0);
            const ageSecs = pulseAt ? Math.max(0, Date.now() / 1000 - pulseAt) : null;
            return {
                openRun,
                beatCount: beats.length,
                lastBeatAt: lastBeatAt || null,
                pulseAt: pulseAt || null,
                ageSecs,
                // Live means a run is open *and* something has moved recently.
                isLive: !!openRun && ageSecs !== null && ageSecs < KANBAN_HEARTBEAT_STALE_SECS,
                isStalled: !!openRun && (ageSecs === null || ageSecs >= KANBAN_HEARTBEAT_STALE_SECS),
            };
        };

        // --- Integration grant status ---
        // One vocabulary, shared by the Integrations tab, the chat sidebar and
        // the review queue. Defined once because these three screens showing
        // different words for the same grant state is how a green tick on one
        // screen comes to contradict a red row on another.
        //
        // `unverified` is deliberately not green. The gateway logs that an MCP
        // call happened but not how it ended, so "we saw traffic" is all that
        // can honestly be claimed — rendering that as working would be the
        // exact false confidence this screen is meant to remove.
        var GRANT_STATUS = {
            working:    { mark: '✓', color: 'var(--acc-green)', label: 'working' },
            failed:     { mark: '✗', color: 'var(--acc-red)', label: 'failed' },
            stale:      { mark: '⚠', color: 'var(--acc-yellow)', label: 'stale' },
            unverified: { mark: '◌', color: 'var(--acc-blue)', label: 'used' },
            never:      { mark: '—', color: 'var(--txt-muted)', label: 'never used' },
        };
        var grantStatus = (s) => GRANT_STATUS[s] || GRANT_STATUS.never;

        // The mark alone, sized to sit in a row of text. Colour carries the
        // state and the glyph repeats it, so the screen still reads without
        // colour vision.
        function StatusMark({ status, title }) {
            const s = grantStatus(status);
            return (
                <span
                    class="inline-block w-4 text-center font-bold shrink-0"
                    style={{ color: s.color }}
                    title={title || s.label}
                >{s.mark}</span>
            );
        }

        // Where a status came from. Evidence and inference are never rendered
        // the same way: a tick derived from a real call outcome means something
        // a tick derived from config does not.
        var BASIS_NOTE = {
            usage: 'from the outcome of the last real call',
            activity: 'a call was made; the gateway records no outcome',
            config: 'from configuration — no calls recorded',
        };

        // --- Kanban status filtering ---
        // One predicate, used by the list, the selection reset and the poll's
        // initial pick. It was duplicated three ways before, which is how the
        // three copies came to disagree about what counts as done.
        var KANBAN_FILTERS = ['active', 'todo', 'inprogress', 'blocked', 'done', 'archived'];
        var KANBAN_FILTER_LABELS = {
            active: 'Active', todo: 'To Do', inprogress: 'Progress',
            blocked: 'Blocked', done: 'Done', archived: 'Archived',
        };
        var isDoneStatus = (s) => s.includes('done') || s.includes('complete');
        // `archived` is a real kernel status (see VALID_STATUSES in
        // hermes_cli/kanban_db.py), and it is terminal — an archived task has
        // left the board for good. The board used to have no name for it, so
        // it fell through every predicate here to the default and was counted
        // as Active and labelled "To Do", which is how six archived tasks came
        // to read as six active ones.
        var isArchivedStatus = (s) => s.includes('archiv');
        // Done and Archived are both "off the board". Active is the negation of
        // that set, not of Done alone.
        var isTerminalStatus = (s) => isDoneStatus(s) || isArchivedStatus(s);

        function matchesKanbanFilter(task, filter) {
            const status = String((task && task.status) || 'todo').toLowerCase();
            // "Active" is everything still on the board. Done and Archived are
            // separate buckets things fall into once they leave it, not subsets
            // of all. Archived is checked first everywhere it matters, because
            // an archived task that was also done must land in exactly one.
            if (filter === 'archived') return isArchivedStatus(status);
            if (filter === 'active') return !isTerminalStatus(status);
            if (isArchivedStatus(status)) return false;
            if (filter === 'todo') return status.includes('todo') || status.includes('backlog');
            if (filter === 'inprogress') return status.includes('progress') || status.includes('running');
            if (filter === 'blocked') return status.includes('blocked');
            if (filter === 'done') return isDoneStatus(status);
            return true;
        }

        // Shared status→badge mapping. The list row and the detail header each
        // had their own copy of this ladder and both omitted Archived; one
        // source means the next status the kernel gains is added once.
        function kanbanBadge(status) {
            const s = String(status || '').toLowerCase();
            if (isArchivedStatus(s)) return { text: 'Archived', color: 'bg-[#585b70]/25 text-[#9ca3af]', border: 'bg-[#585b70]/25 text-[#9ca3af] border-[#585b70]/50' };
            if (isDoneStatus(s)) return { text: 'Done', color: 'bg-[#a6e3a1]/20 text-[#a6e3a1]', border: 'bg-[#a6e3a1]/20 text-[#a6e3a1] border-[#a6e3a1]/50' };
            if (s.includes('progress') || s.includes('running')) return { text: 'Running', color: 'bg-[#89b4fa]/20 text-[#89b4fa]', border: 'bg-[#89b4fa]/20 text-[#89b4fa] border-[#89b4fa]/50' };
            if (s.includes('blocked')) return { text: 'Blocked', color: 'bg-[#f38ba8]/20 text-[#f38ba8]', border: 'bg-[#f38ba8]/20 text-[#f38ba8] border-[#f38ba8]/50' };
            return { text: 'To Do', color: 'bg-[#fab387]/20 text-[#fab387]', border: 'bg-[#fab387]/20 text-[#fab387] border-[#fab387]/50' };
        }

        // The bar that titles a section of the detail pane. Was copy-pasted a
        // dozen times with drifting padding; the merged view adds several more,
        // so it is a component now.
        function SectionHeader({ icon, title, right }) {
            return (
                <div class="px-4 py-3 border-b border-[#313244] flex items-center justify-between gap-2">
                    <span class="text-[11px] uppercase tracking-wider font-bold text-[#9ca3af] flex items-center gap-1.5">
                        {icon && <i data-lucide={icon} class="w-3.5 h-3.5"></i>}
                        {title}
                    </span>
                    {right && <span class="text-[10px] text-[#585b70] shrink-0">{right}</span>}
                </div>
            );
        }

        // --- Cron <-> ADK association ---
        // A job names a script, never an app; the backend recovers the app name
        // by reading that script and hands it over verbatim as `adk_app`. Match
        // it here, where the team list already lives. Exact id first, then last
        // dotted segment, because a job's script may name an app by its bare
        // module name where the server reports the dotted path — `gmail_inbox_triage`
        // against `app.agents.gmail_inbox_triage`.
        var tail = (id) => String(id || '').split('.').pop();

        // --- Published explicitly ------------------------------------------
        // Every other top-level declaration in this file lands on the global
        // object by itself, so the next file can use it. These do not, and the
        // reason is worth knowing before adding one: each is a `var f = (...) =>`
        // whose body calls *itself*. Babel rewrites that into a named function
        // expression to keep the self-call resolvable, and the name it binds
        // lives inside this file's scope — so the assignment never reaches the
        // global object and the next file sees `undefined`.
        //
        // The failure is silent until something renders: no console error at
        // load, just a ReferenceError from whichever tab uses it first. If you
        // write a self-recursive top-level arrow, add it here or make it a
        // `function` declaration, which never has this problem.
        window.relativeAge = relativeAge;
        window.loadDisplayTimezone = loadDisplayTimezone;
        window.grantStatus = grantStatus;
        window.isTerminalStatus = isTerminalStatus;
