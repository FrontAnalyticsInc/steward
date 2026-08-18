// --- 80-kanban-view.jsx -------------------------------------------------
// The Kanban tab: the board rail, its search and status chips, and the task
// pane with a task's comments, runs and block reason. All of it was inline in
// App, together with the two effects that keep the list, the pane and the
// address bar talking about the same task.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
//
// Props and returned fields keep the names the moved code already used
// (`filteredKanbanTasks`, `kanbanDetail`, ...). The bodies are the same code
// that ran inside App, moved and not rewritten, so a rename here would be a
// second change hiding inside the first.

        // The board, the selected task's detail, and the rules that keep the
        // two in step with the URL. App keeps `activeKanbanTaskId` — that is
        // routing, and the route effect's dependency array has to see it.
        //
        // The board poll is NOT gated on the tab being open: the Metrics
        // sidebar shows the task count, so it has to be true while you are
        // looking somewhere else. The per-task detail poll IS gated — it is one
        // request per selected task and nothing off-tab reads it.
        function useKanban({ activeTab, activeKanbanTaskId, setActiveKanbanTaskId, navigateTab, entryRoute }) {
            const [kanbanTasks, setKanbanTasks] = useState([]);
            const [kanbanLoading, setKanbanLoading] = useState(true);
            const [kanbanSearch, setKanbanSearch] = useState('');
            // Task id currently being archived/unarchived, so the button can
            // disable itself rather than firing twice; and the last refusal
            // from the server, shown next to the button that caused it.
            const [kanbanArchiving, setKanbanArchiving] = useState(null);
            const [kanbanArchiveError, setKanbanArchiveError] = useState(null);
            // Defaults to 'active', not everything: a finished task has left the
            // board, so it should not pad the default list.
            const [kanbanFilter, setKanbanFilter] = useState('active'); // see KANBAN_FILTERS
            // What an agent left on the selected task: its comments, its runs and
            // the reason it blocked. Fetched per task rather than joined into the
            // board list, which is polled and would carry every comment on every
            // task on every tick.
            const [kanbanDetail, setKanbanDetail] = useState(null);

            // Fetch Kanban board tasks (supports search)
            const fetchKanban = async (selectLatest = false, searchTerm = kanbanSearch) => {
                try {
                    let url = '/api/kanban';
                    if (searchTerm) {
                        url += `?search=${encodeURIComponent(searchTerm)}`;
                    }
                    const res = await fetch(url);
                    if (res.ok) {
                        const data = await res.json();
                        setKanbanTasks(data);

                        // Pick the first task the active filter actually shows —
                        // selecting data[0] could land on a Done task while the
                        // list is on Active, highlighting a row that isn't there.
                        if (data.length > 0 && (selectLatest || !activeKanbanTaskId)) {
                            const visible = data.filter(t => matchesKanbanFilter(t, kanbanFilter));
                            if (visible.length > 0) setActiveKanbanTaskId(visible[0].id);
                        }
                    }
                } catch (err) {
                    console.error("Error fetching kanban tasks:", err);
                } finally {
                    setKanbanLoading(false);
                }
            };

            // Move a task on or off the board. The server owns the rules about
            // which transitions are legal — a 409 here is the board telling us
            // the task moved under us (a worker picked it up between render and
            // click), so the message is surfaced rather than swallowed and the
            // list is refetched to show what is actually true now.
            const setKanbanArchived = async (taskId, archived) => {
                if (!taskId || kanbanArchiving) return;
                setKanbanArchiving(taskId);
                setKanbanArchiveError(null);
                try {
                    const res = await fetch(
                        `/api/kanban/${encodeURIComponent(taskId)}/${archived ? 'archive' : 'unarchive'}`,
                        { method: 'POST' }
                    );
                    if (!res.ok) {
                        let detail = `Request failed (${res.status})`;
                        try { detail = (await res.json()).detail || detail; } catch (e) {}
                        setKanbanArchiveError(detail);
                    } else {
                        // Follow the task to the chip it just moved to, rather
                        // than leaving the pane on a task the current filter no
                        // longer contains.
                        setKanbanFilter(archived ? 'archived' : 'done');
                    }
                    await fetchKanban();
                } catch (err) {
                    console.error("Error changing kanban archive state:", err);
                    setKanbanArchiveError('Could not reach the server.');
                } finally {
                    setKanbanArchiving(null);
                }
            };

            // The same 7s beat the board ran on inside App's shared polling
            // loop, now on its own interval.
            useEffect(() => {
                fetchKanban();
                const interval = setInterval(() => fetchKanban(), 7000);
                return () => clearInterval(interval);
            }, []);

            // A running task heartbeats about once a minute, so the detail poll is
            // what makes the panel live. 15s is fine for a finished task whose
            // findings are already written; while an agent is actually working it,
            // that is up to a quarter of the gap between beats spent stale.
            const activeKanbanStatus = String(
                (kanbanTasks.find(t => t.id === activeKanbanTaskId) || {}).status || ''
            ).toLowerCase();
            const kanbanDetailIsHot = activeKanbanStatus.includes('running') || activeKanbanStatus.includes('progress');

            // The selected task's comments, runs and block reason. Refetched on a
            // slow timer as well as on selection: an agent can be working the task
            // while it is open, and its findings arrive minutes after the click.
            useEffect(() => {
                if (activeTab !== 'kanban' || !activeKanbanTaskId) { setKanbanDetail(null); return; }
                let cancelled = false;
                const load = async () => {
                    try {
                        const res = await fetch(`/api/kanban/${encodeURIComponent(activeKanbanTaskId)}`);
                        if (res.ok && !cancelled) setKanbanDetail(await res.json());
                    } catch (err) {
                        console.error("Error fetching task detail:", err);
                    }
                };
                load();
                const interval = setInterval(load, kanbanDetailIsHot ? 5000 : 15000);
                return () => { cancelled = true; clearInterval(interval); };
            }, [activeTab, activeKanbanTaskId, kanbanDetailIsHot]);

            // An archive refusal belongs to the task it was raised on. Without
            // this it stays on screen while you click through to the next one,
            // where it describes nothing.
            useEffect(() => { setKanbanArchiveError(null); }, [activeKanbanTaskId, activeTab]);

            // Handle Incremental Kanban Search typing
            const handleKanbanSearchChange = (val) => {
                setKanbanSearch(val);
                fetchKanban(false, val);
            };

            // Filter Kanban tasks dynamically (supports Blocked status!)
            const filteredKanbanTasks = kanbanTasks.filter(t => matchesKanbanFilter(t, kanbanFilter));

            // Whether the Kanban tab exists at all. "Active" is the same
            // predicate the tab's default filter uses: everything not yet done.
            const backlogCount = kanbanTasks.filter(t => matchesKanbanFilter(t, 'active')).length;
            const hasBacklog = backlogCount > 0;

            // Handle switching kanban filter to reset selection
            const changeKanbanFilter = (filter) => {
                setKanbanFilter(filter);
                const filtered = kanbanTasks.filter(t => matchesKanbanFilter(t, filter));
                if (filtered.length > 0) {
                    setActiveKanbanTaskId(filtered[0].id);
                } else {
                    setActiveKanbanTaskId(null);
                }
            };

            // The backlog tab can vanish under the user — the last open task gets
            // marked done while they are looking at it. Fall back to chat rather
            // than leaving them on a tab with no button in the header. Declared
            // here, below hasBacklog, because a hook's dep array is evaluated
            // during render and would hit the const's temporal dead zone above.
            useEffect(() => {
                if (activeTab !== 'kanban' || kanbanLoading || hasBacklog) return;
                // An empty list is not the same fact as an empty backlog. A first
                // fetch that failed leaves zero tasks and clears the loading flag
                // in the same breath, and evicting on that bounced you off the tab
                // for a network blip — with a task URL in the address bar, it threw
                // away the link you had just followed.
                if (kanbanTasks.length === 0) return;
                // A task named in the URL is a destination, not a browsing step.
                // Someone followed a link to a finished task; the backlog being
                // empty is exactly the expected case, not a reason to redirect.
                if (entryRoute.taskId && kanbanTasks.some(t => t.id === entryRoute.taskId)) return;
                navigateTab('chat');
            }, [activeTab, kanbanLoading, hasBacklog, kanbanTasks, navigateTab]);

            // Keeping the list, the pane and the address bar talking about the
            // same task. Two rules, in this order, because the second would
            // otherwise undo the first in the same render pass.
            const deepLinkSettled = useRef(false);
            useEffect(() => {
                if (activeTab !== 'kanban' || kanbanLoading || kanbanTasks.length === 0) return;

                // 1. Arrival. The board defaults to Active, so a link to a
                //    finished task lands on a list that does not contain it.
                //    Widen once, on arrival only, so it never fights a filter
                //    clicked afterwards — and stop here, so rule 2 judges what is
                //    visible under the new filter rather than the old one.
                if (!deepLinkSettled.current) {
                    deepLinkSettled.current = true;
                    const linked = entryRoute.taskId && kanbanTasks.find(t => t.id === entryRoute.taskId);
                    if (linked && !matchesKanbanFilter(linked, kanbanFilter)) {
                        const linkedStatus = String(linked.status || '').toLowerCase();
                        // Archived is checked first: it is terminal like done,
                        // but lives under its own chip, and widening to `done`
                        // or `active` would land the deep link on a list that
                        // still does not contain the task it was pointing at.
                        setKanbanFilter(
                            isArchivedStatus(linkedStatus) ? 'archived'
                            : isDoneStatus(linkedStatus) ? 'done'
                            : 'active'
                        );
                        return;
                    }
                }

                // 2. Steady state. The pane falls back to the first visible task
                //    when the selected one is filtered out, which was harmless
                //    while nothing outside the pane knew what was selected. The
                //    URL knows now, and a silent fallback would leave the address
                //    bar naming a task that is not the one on screen. Adopt the
                //    fallback as the selection so the two cannot disagree.
                if (filteredKanbanTasks.length === 0) return;
                if (filteredKanbanTasks.some(t => t.id === activeKanbanTaskId)) return;
                setActiveKanbanTaskId(filteredKanbanTasks[0].id);
            }, [activeTab, kanbanLoading, kanbanTasks, filteredKanbanTasks, kanbanFilter, activeKanbanTaskId]);

            return {
                kanbanTasks, kanbanLoading, filteredKanbanTasks,
                kanbanSearch, handleKanbanSearchChange,
                kanbanFilter, changeKanbanFilter,
                kanbanDetail, kanbanArchiving, kanbanArchiveError, setKanbanArchived,
                backlogCount, hasBacklog,
            };
        }

        // The board's search box.
        function KanbanSidebarSearch({ kanban }) {
            const { kanbanSearch, handleKanbanSearchChange } = kanban;
            return (
                <div class="relative flex items-center shrink-0">
                    <input 
                        type="text" 
                        value={kanbanSearch}
                        onChange={(e) => handleKanbanSearchChange(e.target.value)}
                        placeholder="Search task specs..." 
                        class="w-full bg-[#11111b] border border-[#313244] rounded-lg py-1.5 pl-8 pr-3 text-xs text-[#cdd6f4] placeholder-[#585b70] focus:outline-none focus:border-[#b4befe]"
                    />
                    <i data-lucide="search" class="w-3.5 h-3.5 text-[#585b70] absolute left-2.5"></i>
                </div>
            );
        }

        // The status chips, each carrying its own count so the row doubles as
        // the board's composition at a glance.
        function KanbanSidebarFilters({ kanban }) {
            const { kanbanTasks, kanbanFilter, changeKanbanFilter } = kanban;
            return (
                <div class="grid grid-cols-6 gap-0.5 p-0.5 bg-[#11111b] rounded-lg border border-[#313244]">
                    {KANBAN_FILTERS.map(f => {
                        const count = kanbanTasks.filter(t => matchesKanbanFilter(t, f)).length;
                        return (
                            <button
                                key={f}
                                onClick={() => changeKanbanFilter(f)}
                                title={
                                    f === 'active' ? 'Everything still on the board — excludes Done and Archived'
                                    : f === 'archived' ? 'Tasks taken off the board. Read-only.'
                                    : undefined
                                }
                                class={`flex flex-col items-center leading-tight text-[9px] font-bold py-1 px-0.5 text-center rounded transition ${
                                    kanbanFilter === f
                                        ? 'bg-[#313244] text-[#b4befe]'
                                        : 'text-[#9ca3af] hover:text-[#cdd6f4]'
                                }`}
                            >
                                <span class="whitespace-nowrap">{KANBAN_FILTER_LABELS[f]}</span>
                                <span class={kanbanFilter === f ? 'text-[#b4befe]/70' : 'text-[#585b70]'}>{count}</span>
                            </button>
                        );
                    })}
                </div>
            );
        }

        // The task rail.
        function KanbanSidebarList({ kanban, activeKanbanTaskId, setActiveKanbanTaskId }) {
            const { kanbanLoading, filteredKanbanTasks } = kanban;
            return (
                <>
                    <h2 class="text-xs font-semibold uppercase tracking-wider text-[#585b70] px-3 mb-2">Tasks List</h2>
                    {kanbanLoading ? (
                        <div class="text-center py-6 text-sm text-[#585b70]">Loading tasks...</div>
                    ) : filteredKanbanTasks.length === 0 ? (
                        <div class="text-center py-6 text-sm text-[#585b70]">No tasks match filter.</div>
                    ) : (
                        filteredKanbanTasks.map(t => {
                            const isSelected = t.id === activeKanbanTaskId;

                            const rowBadge = kanbanBadge(t.status);
                            const badgeColor = rowBadge.color;
                            const badgeText = rowBadge.text;

                            return (
                                <button
                                    key={t.id}
                                    onClick={() => setActiveKanbanTaskId(t.id)}
                                    class={`w-full text-left p-3 rounded-lg flex flex-col gap-1 transition ${
                                        isSelected 
                                            ? 'bg-[#b4befe] text-[#11111b] font-medium' 
                                            : 'hover:bg-[#313244] text-[#a6adc8]'
                                    }`}
                                >
                                    <div class="flex justify-between items-start gap-2">
                                        <span class="text-sm font-bold line-clamp-1 break-words leading-tight">{t.title}</span>
                                        <span class={`text-[9px] font-bold px-1.5 py-0.5 rounded shrink-0 ${
                                            isSelected ? 'bg-black/30 text-white' : badgeColor
                                        }`}>{badgeText}</span>
                                    </div>
                                    <span class={`text-[10px] font-mono ${isSelected ? 'text-[#1e1e2e]' : 'text-[#585b70]'}`}>#{t.id}</span>
                                </button>
                            );
                        })
                    )}
                </>
            );
        }

        // The selected task in full: what it is for, what an agent has done to
        // it, and the reason it is blocked where it is.
        function KanbanMainPanel({ kanban, activeKanbanTaskId, now, selectSession, openReview }) {
            const {
                kanbanLoading, filteredKanbanTasks, kanbanDetail,
                kanbanArchiving, kanbanArchiveError, setKanbanArchived,
            } = kanban;
            usePaintedIcons();
            return (
                <div class="h-full overflow-y-auto p-6">
                    <div class="max-w-4xl mx-auto space-y-6">
                        {kanbanLoading ? (
                            <div class="text-center py-12 text-[#585b70]">Loading task profile...</div>
                        ) : filteredKanbanTasks.length === 0 ? (
                            <div class="bg-[#181825] rounded-xl border border-[#313244] p-12 text-center flex flex-col items-center justify-center gap-4">
                                <i data-lucide="clipboard" class="w-16 h-16 opacity-30 text-[#b4befe]"></i>
                                <h3 class="text-base font-bold text-[#cdd6f4]">No tasks match this status</h3>
                            </div>
                        ) : (
                            (() => {
                                const task = filteredKanbanTasks.find(t => t.id === activeKanbanTaskId) || filteredKanbanTasks[0];
                                if (!task) return null;

                                const taskStatus = String(task.status || '').toLowerCase();
                                const isArchived = isArchivedStatus(taskStatus);
                                // Mirrors ARCHIVABLE_STATUSES on the server. The server is
                                // still the authority — this only decides whether to offer
                                // the button, and a task that moved since the last poll is
                                // refused there and reported.
                                const isArchivable = isDoneStatus(taskStatus) || taskStatus.includes('blocked');
                                const archiveBusy = kanbanArchiving === task.id;
                                const badge = kanbanBadge(task.status);
                                const badgeColor = badge.border;
                                const badgeText = badge.text;

                                const prioColor = task.priority === 'high'
                                    ? 'bg-[#f38ba8]/20 text-[#f38ba8] border-[#f38ba8]/50'
                                    : task.priority === 'medium'
                                    ? 'bg-[#f9e2af]/20 text-[#f9e2af] border-[#f9e2af]/50'
                                    : 'bg-[#a6e3a1]/20 text-[#a6e3a1] border-[#a6e3a1]/50';

                                // Only the detail carries runs and events, and only for the
                                // task it was fetched for — a stale one belongs to whatever
                                // was selected a moment ago and must not describe this task.
                                const detail = kanbanDetail && kanbanDetail.id === task.id ? kanbanDetail : null;
                                const live = kanbanLiveness(detail);
                                // The board's own clock, so the age re-renders with `now`.
                                const pulseAgo = live.pulseAt ? Math.max(0, Math.round(now / 1000 - live.pulseAt)) : null;
                                const pulseLabel = pulseAgo === null
                                    ? null
                                    : pulseAgo < 90 ? `${pulseAgo}s ago`
                                    : pulseAgo < 3600 ? `${Math.round(pulseAgo / 60)}m ago`
                                    : `${Math.round(pulseAgo / 3600)}h ago`;
                                // One history, not two. Comments and events are the same
                                // story told at different grains — the agent commented, then
                                // blocked, then someone unblocked it and it commented again —
                                // and reading that story out of two lists sorted differently
                                // means reconstructing the order by hand. `commented` events
                                // are dropped rather than merged: they carry only the author
                                // and a body length, so the comment row says everything they
                                // do and says it with the body attached.
                                const feed = [
                                    ...(detail ? (detail.events || []) : [])
                                        .filter(e => !isBareHeartbeat(e) && e.kind !== 'commented')
                                        .map((e, i) => ({
                                            kind: 'event',
                                            at: Number(e.created_at) || 0,
                                            key: `e-${e.created_at}-${i}`,
                                            event: e,
                                        })),
                                    ...(detail ? (detail.comments || []) : [])
                                        .map((c, i) => ({
                                            kind: 'comment',
                                            at: Number(c.created_at) || 0,
                                            key: `c-${c.created_at}-${i}`,
                                            comment: c,
                                        })),
                                ]
                                    // Newest first: on a task still running, the last thing the
                                    // agent said is the thing you opened this to read.
                                    .sort((a, b) => b.at - a.at);
                                const commentCount = (detail ? (detail.comments || []) : []).length;

                                return (
                                    <div class="bg-[#181825] rounded-xl border border-[#313244] p-6 space-y-6 shadow-md">

                                        {/* Archived notice. The status pill is easy to miss at the
                                            far right of the header, and an archived task looks exactly
                                            like a live one until you find it — so the fact that this
                                            task is off the board is stated in full, first, before the
                                            title. */}
                                        {isArchived && (
                                            <div class="flex items-center gap-2 text-xs text-[#9ca3af] bg-[#585b70]/15 border border-[#585b70]/40 rounded-lg px-3 py-2">
                                                <i data-lucide="archive" class="w-4 h-4 shrink-0"></i>
                                                <span>
                                                    <strong class="text-[#cdd6f4]">This task is archived.</strong>
                                                    {' '}It has left the board and is not counted under Active. Restore puts it back as Done.
                                                </span>
                                            </div>
                                        )}

                                        {/* A refused or failed move, stated where the click happened.
                                            The list is refetched either way, so without this the board
                                            would just silently snap back to how it was. */}
                                        {kanbanArchiveError && (
                                            <div class="flex items-center gap-2 text-xs text-[#f38ba8] bg-[#f38ba8]/10 border border-[#f38ba8]/40 rounded-lg px-3 py-2">
                                                <i data-lucide="triangle-alert" class="w-4 h-4 shrink-0"></i>
                                                <span>{kanbanArchiveError}</span>
                                            </div>
                                        )}

                                        {/* Task Header info. Title, id and the metadata row are one
                                            block: the metadata reads as a caption on the title, so it
                                            sits directly under the id on the card surface rather than
                                            in a sunken panel of its own. */}
                                        <div class="border-b border-[#313244] pb-4 space-y-3">
                                        <div class="flex justify-between items-start gap-4">
                                            <div>
                                                <h2 class="text-xl font-bold text-[#ffffff] break-words line-clamp-2 leading-snug">{task.title}</h2>
                                                <code class="text-xs text-[#585b70] font-mono">Task ID: #{task.id}</code>
                                            </div>
                                            <div class="flex gap-2 shrink-0 items-center">
                                                {/* Archive / Restore. Offered only where the move is
                                                    legal, so the board never shows a control that the
                                                    server will refuse. */}
                                                {(isArchivable || isArchived) && (
                                                    <button
                                                        onClick={() => setKanbanArchived(task.id, !isArchived)}
                                                        disabled={archiveBusy}
                                                        title={isArchived
                                                            ? 'Put this task back on the board as Done'
                                                            : 'Take this task off the board. It stays readable under Archived.'}
                                                        class={`text-xs font-bold py-1 px-3 rounded-lg border flex items-center gap-1.5 transition ${
                                                            archiveBusy
                                                                ? 'opacity-50 cursor-wait border-[#313244] text-[#585b70]'
                                                                : 'border-[#585b70]/50 text-[#9ca3af] hover:bg-[#585b70]/20 hover:text-[#cdd6f4]'
                                                        }`}
                                                    >
                                                        <i data-lucide={isArchived ? 'archive-restore' : 'archive'} class="w-3.5 h-3.5"></i>
                                                        {isArchived ? 'Restore' : 'Archive'}
                                                    </button>
                                                )}
                                                <span class={`text-xs font-bold py-1 px-3 rounded-lg border capitalize ${prioColor}`}>
                                                    {task.priority || 'Low'} Priority
                                                </span>
                                                {/* The spinner is evidence, not decoration: it turns only
                                                    while a run is open and a heartbeat has landed inside
                                                    the stale window. A task whose container died still
                                                    says "running" in the status column forever, and a
                                                    spinner on that is a lie told once a second. */}
                                                <span class={`text-xs font-bold py-1 px-3 rounded-lg border uppercase tracking-wide flex items-center gap-1.5 ${
                                                    live.isStalled ? 'bg-[#f9e2af]/20 text-[#f9e2af] border-[#f9e2af]/50' : badgeColor
                                                }`}>
                                                    {live.isLive && (
                                                        <i data-lucide="loader-circle" class="w-3.5 h-3.5 animate-spin"></i>
                                                    )}
                                                    {live.isStalled ? 'Stalled' : badgeText}
                                                </span>
                                            </div>
                                        </div>

                                            {/* Metadata row. Who owns the task and what it needs is the
                                                first thing you look for, so it reads as a caption on the
                                                title — its own row directly under the task id, on the
                                                card surface with no fill or padding of its own. */}
                                            <div class="flex flex-wrap gap-x-6 gap-y-2 text-[11px] text-[#a6adc8]">
                                                <span class="flex items-center gap-1.5">
                                                    <strong class="text-[#585b70]">Assignee:</strong>
                                                    <span class="bg-[#313244]/50 px-2 py-0.5 rounded text-[#b4befe] flex items-center gap-1.5">
                                                        <i data-lucide="bot" class="w-3.5 h-3.5"></i>
                                                        {task.assignee || 'Unassigned'}
                                                    </span>
                                                </span>
                                                <span class="flex items-center gap-1.5">
                                                    <strong class="text-[#585b70]">Created At:</strong>
                                                    <span class="font-mono bg-[#313244]/50 px-2 py-0.5 rounded text-[#9ca3af]">{fmtStamp(task.created_at)}</span>
                                                </span>
                                                {task.branch_name && (
                                                    <span class="flex items-center gap-1.5">
                                                        <strong class="text-[#585b70]">Git Branch:</strong>
                                                        <code class="font-mono text-[#f38ba8] bg-[#313244]/50 px-2 py-0.5 rounded">{task.branch_name}</code>
                                                    </span>
                                                )}
                                                {task.skills && (
                                                    <span class="flex items-center gap-1.5">
                                                        <strong class="text-[#585b70]">Required Skills:</strong>
                                                        <span class="font-mono text-[#89b4fa] bg-[#313244]/50 px-2 py-0.5 rounded capitalize">{task.skills}</span>
                                                    </span>
                                                )}
                                            </div>
                                        </div>

                                        {/* Task Body */}
                                        <div class="space-y-4">
                                            {task.body && (
                                                <div>
                                                    <div class="text-[10px] uppercase font-bold text-[#585b70] mb-1.5 tracking-wider">Description</div>
                                                    <p class="text-sm text-[#bac2de] whitespace-pre-wrap leading-relaxed bg-[#11111b] border border-[#313244]/50 p-4 rounded-lg select-text font-sans">
                                                        {task.body}
                                                    </p>
                                                </div>
                                            )}

                                            {/* Execution Result Log Output */}
                                            {task.result && (
                                                <div>
                                                    <div class="text-[10px] uppercase font-bold text-[#585b70] mb-1.5 tracking-wider">Execution Outcomes (Results Logs)</div>
                                                    <pre class="bg-[#11111b] text-[#a6e3a1] font-mono text-xs border border-[#313244] p-4 rounded-lg overflow-x-auto select-all max-h-72 whitespace-pre-wrap">
                                                        {task.result}
                                                    </pre>
                                                </div>
                                            )}

                                            {/* A task blocked on a human used to end here: the reason
                                                was readable and there was nothing anyone could do about
                                                it from this page, so the work sat until someone ran
                                                `hermes kanban unblock` in a terminal. The decision
                                                itself lives in the Review tab, with the changed files
                                                next to the buttons — this is the door to it, on the
                                                page where you find out the task is stuck. */}
                                            {kanbanDetail && kanbanDetail.id === task.id
                                                && kanbanDetail.status === 'blocked'
                                                && kanbanDetail.block_kind === 'needs_input' && openReview && (
                                                <div class="rounded-lg border border-[#f9e2af]/40 bg-[#f9e2af]/10 p-4 flex flex-col gap-3">
                                                    <div class="flex items-center gap-2 text-[11px] uppercase font-bold tracking-wider text-[#f9e2af]">
                                                        <i data-lucide="user-check" class="w-3.5 h-3.5"></i>
                                                        Waiting on you
                                                    </div>
                                                    <div class="text-xs text-[#cdd6f4] leading-relaxed">
                                                        The worker stopped and asked for a human decision. Nothing
                                                        else will happen to this task until you approve it or send it
                                                        back with what to change.
                                                    </div>
                                                    <button
                                                        onClick={() => openReview(task.id)}
                                                        class="self-start text-xs font-semibold px-3 py-2 rounded-lg bg-[#f9e2af] text-[#11111b] hover:bg-[#f5d97a] transition flex items-center gap-2"
                                                    >
                                                        <i data-lucide="code" class="w-3.5 h-3.5"></i>
                                                        Review the code and decide
                                                    </button>
                                                </div>
                                            )}

                                            {/* Why it stopped. A blocked task whose reason you cannot
                                                read is indistinguishable from one that just died, and
                                                the reason is the whole point of blocking rather than
                                                failing. */}
                                            {kanbanDetail && kanbanDetail.id === task.id
                                                && (kanbanDetail.runs || []).filter(r => r.summary || r.error).length > 0 && (
                                                <div>
                                                    <div class="text-[10px] uppercase font-bold text-[#585b70] mb-1.5 tracking-wider">
                                                        Agent runs
                                                    </div>
                                                    <div class="space-y-2">
                                                        {kanbanDetail.runs.filter(r => r.summary || r.error).map(r => (
                                                            <div key={r.id} class={`rounded-lg border p-3 ${
                                                                r.outcome === 'blocked'
                                                                    ? 'bg-[#f38ba8]/10 border-[#f38ba8]/30'
                                                                    : 'bg-[#11111b] border-[#313244]'
                                                            }`}>
                                                                <div class="flex items-center gap-2 flex-wrap text-[11px] mb-1">
                                                                    <span class="font-mono text-[#cdd6f4]">run #{r.id}</span>
                                                                    <span class="text-[#a6adc8]">{r.profile}</span>
                                                                    <span class={r.outcome === 'blocked' ? 'text-[#f38ba8]' : 'text-[#a6e3a1]'}>
                                                                        {r.outcome || r.status}
                                                                    </span>
                                                                    {kanbanDetail.block_kind && r.outcome === 'blocked' && (
                                                                        <span class="text-[10px] uppercase font-bold tracking-wider bg-[#313244] text-[#f9e2af] px-1.5 py-0.5 rounded">
                                                                            {kanbanDetail.block_kind}
                                                                        </span>
                                                                    )}
                                                                </div>
                                                                <div class="text-xs text-[#cdd6f4]" style={{ whiteSpace: 'pre-wrap' }}>
                                                                    {r.summary || r.error}
                                                                </div>
                                                            </div>
                                                        ))}
                                                    </div>
                                                </div>
                                            )}

                                            {/* The conversation the agent worked this task in. The
                                                comments are its conclusions; the session is how it got
                                                there, which is what you want when the conclusion is
                                                wrong. */}
                                            {kanbanDetail && kanbanDetail.id === task.id && kanbanDetail.session_id && (
                                                <button
                                                    onClick={() => { selectSession(kanbanDetail.session_id); }}
                                                    class="text-xs font-semibold px-3 py-2 rounded-lg border border-[#313244] text-[#b4befe] hover:bg-[#313244] transition flex items-center gap-2"
                                                >
                                                    <i data-lucide="message-square" class="w-3.5 h-3.5"></i>
                                                    Open the chat session the agent worked this in
                                                </button>
                                            )}

                                            {/* What it is doing right now.
                                                The dispatcher has been writing task_events all along —
                                                claimed, spawned, heartbeats carrying the agent's own
                                                note about what it is working on, blocks, comments — and
                                                the panel rendered none of it. A running task showed a
                                                title and a status word, so the only way to tell work
                                                from a hang was to open the chat session and read it.
                                                Bare heartbeats are the pulse line, not rows: they are
                                                dozens per task and say only "still here". */}
                                            {detail && (live.openRun || feed.length > 0) && (
                                                <div>
                                                    <div class="flex items-center justify-between gap-3 mb-1.5 flex-wrap">
                                                        <div class="text-[10px] uppercase font-bold text-[#585b70] tracking-wider">
                                                            History
                                                            {commentCount > 0 && (
                                                                <span class="text-[#585b70]/70 normal-case font-mono ml-1.5">
                                                                    · {commentCount} comment{commentCount === 1 ? '' : 's'}
                                                                </span>
                                                            )}
                                                        </div>
                                                        {live.openRun && (
                                                            <div class="flex items-center gap-2 text-[10px] font-mono">
                                                                <span class={`flex items-center gap-1.5 ${live.isLive ? 'text-[#a6e3a1]' : 'text-[#f9e2af]'}`}>
                                                                    {live.isLive
                                                                        ? <i data-lucide="loader-circle" class="w-3 h-3 animate-spin"></i>
                                                                        : <i data-lucide="triangle-alert" class="w-3 h-3"></i>}
                                                                    {live.isLive ? 'working' : 'no heartbeat'}
                                                                </span>
                                                                <span class="text-[#585b70]">
                                                                    run #{live.openRun.id} on {live.openRun.profile}
                                                                    {pulseLabel ? ` · last beat ${pulseLabel}` : ''}
                                                                    {live.beatCount ? ` · ${live.beatCount} beats` : ''}
                                                                </span>
                                                            </div>
                                                        )}
                                                    </div>
                                                    {feed.length === 0 ? (
                                                        <div class="bg-[#11111b] border border-[#313244] rounded-lg p-3 text-xs text-[#585b70] italic">
                                                            Claimed, but nothing reported yet.
                                                        </div>
                                                    ) : (
                                                        <div class="bg-[#11111b] border border-[#313244] rounded-lg divide-y divide-[#313244]/60 max-h-[32rem] overflow-y-auto">
                                                            {feed.slice(0, 60).map(item => {
                                                                // Both row shapes get the wall-clock time as
                                                                // well as the age: "2h ago" answers "is this
                                                                // current", the stamp answers "was this before
                                                                // or after the thing I did", and on a task
                                                                // worked across days only the stamp does.
                                                                const when = (
                                                                    <span class="text-[10px] font-mono text-[#585b70] shrink-0 text-right leading-tight">
                                                                        {fmtStamp(item.at)}
                                                                        <span class="block text-[#45475a]">{fmtAgo(item.at) || '—'}</span>
                                                                    </span>
                                                                );
                                                                if (item.kind === 'comment') {
                                                                    // A comment is prose the agent wrote for a
                                                                    // reader, often paragraphs of it, so it gets
                                                                    // the full width and its own left rule
                                                                    // rather than being squeezed into the note
                                                                    // column the one-line events share.
                                                                    return (
                                                                        <div key={item.key} class="px-3 py-2.5 border-l-2 border-l-[#b4befe]/60 bg-[#b4befe]/[0.03]">
                                                                            <div class="flex items-start justify-between gap-3 mb-1">
                                                                                <span class="text-[10px] uppercase font-bold tracking-wider text-[#b4befe] font-mono">
                                                                                    comment · {item.comment.author || 'unknown'}
                                                                                </span>
                                                                                {when}
                                                                            </div>
                                                                            <div class="text-xs text-[#cdd6f4]" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                                                                                {item.comment.body}
                                                                            </div>
                                                                        </div>
                                                                    );
                                                                }
                                                                const note = kanbanEventNote(item.event);
                                                                return (
                                                                    <div key={item.key} class="flex items-start gap-3 px-3 py-2">
                                                                        <span class="text-[10px] uppercase font-bold tracking-wider text-[#89b4fa] font-mono shrink-0 w-32 truncate" title={item.event.kind}>
                                                                            {item.event.kind}
                                                                        </span>
                                                                        <span class="text-xs text-[#cdd6f4] flex-1 min-w-0" style={{ overflowWrap: 'anywhere' }}>
                                                                            {note || <span class="text-[#585b70] italic">—</span>}
                                                                        </span>
                                                                        {when}
                                                                    </div>
                                                                );
                                                            })}
                                                        </div>
                                                    )}
                                                    {feed.length > 60 && (
                                                        <div class="text-[10px] text-[#585b70] mt-1 font-mono">
                                                            showing the 60 most recent of {feed.length} entries
                                                        </div>
                                                    )}
                                                </div>
                                            )}
                                        </div>

                                    </div>
                                );
                            })()
                        )}
                    </div>
                </div>
            );
        }
