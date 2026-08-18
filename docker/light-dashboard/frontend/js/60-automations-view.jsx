// --- 60-automations-view.jsx -------------------------------------------
// The Automations tab: the list of everything scheduled, and one automation in
// full. Both were rendered inline inside App, which is the shape this file
// exists to end — App now decides which of the two is on screen and hands each
// what it needs.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
//
// Props are named for what the moved JSX already called them (`automations`,
// `cronJobs`, `navigateTab`, ...). That is deliberate and worth keeping: the
// bodies below are the same code that ran inside App, moved and not rewritten,
// so a rename here would be a second change hiding inside the first.

        // One automation's server-side detail — configuration, run history, and
        // the ADK trace behind each run where there is one.
        //
        // A hook rather than state in App: nothing outside this tab reads it,
        // and while it lived up there its polling interval, its error states
        // and its 404 handling were four more names in a component that already
        // had 124. App keeps `activeAutomationId`, because that is routing and
        // the URL owns it; everything downstream of that id lives here.
        function useAutomationDetail(active, jobId) {
            const [detail, setDetail] = useState(null);
            const [loading, setLoading] = useState(false);
            const [error, setError] = useState(null);

            // One route rather than three fetches joined here, because the join
            // that matches an execution to its trace is by time and belongs
            // where both clocks are known. See backend/main.py:_adk_run_for.
            const fetchAutomation = useCallback(async (id) => {
                if (!id) return;
                try {
                    const res = await fetch(`/api/automations/${encodeURIComponent(id)}?days=30&limit=25`);
                    if (res.status === 404) {
                        // A job id that no profile answers to. Deleted, renamed
                        // or mistyped — all of which are the same thing to
                        // someone who followed a link, and none of which are
                        // served better by an empty page than by saying so.
                        setDetail(null);
                        setError('notfound');
                        return;
                    }
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    setDetail(await res.json());
                    setError(null);
                } catch (err) {
                    console.error("Error fetching automation:", err);
                    // Only a first load gets to show the error state. On a poll
                    // tick there is already a good page on screen, and replacing
                    // it with "unreachable" because one refresh failed is worse
                    // than showing numbers a few seconds stale.
                    setDetail(prev => {
                        if (!prev) setError('unreachable');
                        return prev;
                    });
                } finally {
                    setLoading(false);
                }
            }, []);

            // Cleared on the way in, not on the way out: leaving one automation
            // for another used to leave the previous one's executions on screen
            // under the new one's name until the first fetch landed.
            useEffect(() => {
                setDetail(null);
                setError(null);
            }, [jobId]);

            useEffect(() => {
                if (!active || !jobId) return;
                setLoading(true);
                fetchAutomation(jobId);
                // 15s, not the 7s the list polls on: this reads the trace files
                // as well as the cron store, and an execution history does not
                // change faster than the scheduler ticks.
                const interval = setInterval(() => fetchAutomation(jobId), 15000);
                return () => clearInterval(interval);
            }, [active, jobId, fetchAutomation]);

            return { detail, loading, error };
        }

        // Icons are painted into their <i> hosts after render (see renderIcons).
        // App used to do this for the whole page from one effect with a
        // forty-name dependency array; a view that renders its own icons should
        // repaint its own. No dependency array — it runs after every render of
        // this view, and renderIcons skips anything already painted.
        function usePaintedIcons() {
            useEffect(() => { renderIcons(); });
        }

        // Everything scheduled on this host, one row each. The rows are ranked
        // and frozen by App — the order must not change under a reader who is
        // looking at it — so this renders what it is given and sorts nothing.
        function AutomationsListView({
            automations, staleCount, hasDrift, failedGrants, now,
            navigateTab, navigateMetricsView, navigateAutomation,
        }) {
            usePaintedIcons();
            return (
                        <div class="h-full overflow-y-auto p-6">
                            <div class="flex items-center justify-between gap-4 mb-5 flex-wrap">
                                <div>
                                    <h2 class="text-lg font-bold text-[#cdd6f4]">Automations</h2>
                                    <p class="text-xs text-[#585b70] mt-0.5">
                                        {automations.length === 0
                                            ? 'Nothing scheduled on this host.'
                                            : `${automations.length} scheduled · ${staleCount} needing attention`}
                                    </p>
                                </div>
                                {/* Both rosters lost their nav button, so these are
                                    the way in — and each carries the badge its
                                    button used to, rather than dropping the signal.
                                    Silent while healthy: a count appears only when
                                    there is something to answer for.

                                    System metrics joins them because it is the
                                    other page you reach for from here: the list
                                    says which automation is unhappy, /metrics/system
                                    says whether the host is. It carries no badge —
                                    it is diagnostics, not a roster with a count. */}
                                <div class="flex items-center gap-2">
                                    <button
                                        onClick={() => navigateTab('agents')}
                                        class="text-xs font-semibold py-1.5 px-3 rounded-lg border border-[#313244] text-[#a6adc8] hover:text-[#cdd6f4] hover:border-[#45475a] flex items-center gap-2 transition"
                                    >
                                        <i data-lucide="users" class="w-3.5 h-3.5"></i>
                                        Agent scorecards
                                        {hasDrift && (
                                            <span class="bg-[#f9e2af] text-[#11111b] text-[10px] font-bold px-1.5 py-0.5 rounded-full">drift</span>
                                        )}
                                    </button>
                                    <button
                                        onClick={() => navigateTab('integrations')}
                                        class="text-xs font-semibold py-1.5 px-3 rounded-lg border border-[#313244] text-[#a6adc8] hover:text-[#cdd6f4] hover:border-[#45475a] flex items-center gap-2 transition"
                                    >
                                        <i data-lucide="plug" class="w-3.5 h-3.5"></i>
                                        Integrations
                                        {failedGrants > 0 && (
                                            <span class="bg-[#f38ba8] text-[#11111b] text-[10px] font-bold px-1.5 py-0.5 rounded-full">
                                                {failedGrants}
                                            </span>
                                        )}
                                    </button>
                                    <button
                                        onClick={() => navigateMetricsView('system')}
                                        class="text-xs font-semibold py-1.5 px-3 rounded-lg border border-[#313244] text-[#a6adc8] hover:text-[#cdd6f4] hover:border-[#45475a] flex items-center gap-2 transition"
                                    >
                                        <i data-lucide="gauge" class="w-3.5 h-3.5"></i>
                                        System metrics
                                    </button>
                                </div>
                            </div>

                            {automations.length === 0 ? (
                                <div class="text-center py-16 text-sm text-[#585b70]">
                                    No scheduled jobs found.
                                </div>
                            ) : (
                                <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                    <div class="overflow-x-auto">
                                        <table class="w-full text-sm" style={{ borderCollapse: 'collapse' }}>
                                            <thead>
                                                <tr class="text-[10px] uppercase tracking-wider text-[#9ca3af] font-bold border-b border-[#313244]">
                                                    {/* Status leads. The list is read to find what
                                                        is broken, and a verdict in the last column
                                                        is one the eye reaches after crossing four
                                                        columns of configuration it did not ask
                                                        for. */}
                                                    <th class="text-left px-4 py-3 font-bold">Status</th>
                                                    <th class="text-left px-4 py-3 font-bold">Automation</th>
                                                    <th class="text-left px-4 py-3 font-bold">Where</th>
                                                    <th class="text-left px-4 py-3 font-bold">Schedule</th>
                                                    <th class="text-left px-4 py-3 font-bold">Last run</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {automations.map(({ job, where, status }) => {
                                                    return (
                                                        // A row opens the automation's own page rather than
                                                        // expanding in place. The expansion could show what
                                                        // the job is wired to but never how it ran — the run
                                                        // history was on a different tab, and the errors on
                                                        // no tab at all — so "click for more" delivered a
                                                        // third of more. One row, one destination, whole
                                                        // answer.
                                                        <tr
                                                            key={job.id}
                                                            onClick={() => navigateAutomation(job.id)}
                                                            class="border-b border-[#313244] cursor-pointer transition hover:bg-[#313244]/20"
                                                        >
                                                                <td class="px-4 py-3">
                                                                    <StatusPill status={status} />
                                                                </td>
                                                                <td class="px-4 py-3">
                                                                    <div class="flex items-center gap-2">
                                                                        <i
                                                                            data-lucide="chevron-right"
                                                                            class="w-3.5 h-3.5 text-[#585b70] shrink-0"
                                                                        ></i>
                                                                        <span class="font-semibold text-[#cdd6f4]">{job.name || job.id}</span>
                                                                    </div>
                                                                </td>
                                                                <td class="px-4 py-3">
                                                                    <span class="flex items-center gap-1.5 text-[#a6adc8]">
                                                                        <i
                                                                            data-lucide={whereKind(where.label).icon}
                                                                            class="w-3.5 h-3.5 shrink-0"
                                                                            style={{ color: whereKind(where.label).color }}
                                                                        ></i>
                                                                        {where.label}
                                                                    </span>
                                                                    {where.detail && (
                                                                        <span class="block text-[10px] font-mono text-[#585b70] truncate" style={{ maxWidth: '18rem' }}>
                                                                            {where.detail}
                                                                        </span>
                                                                    )}
                                                                </td>
                                                                <td class="px-4 py-3 font-mono text-xs text-[#a6e3a1]">
                                                                    {formatSchedule(job)}
                                                                </td>
                                                                <td class="px-4 py-3 font-mono text-xs text-[#a6adc8]">
                                                                    {job.last_run_at ? relativeAge(job.last_run_at, now) : 'never'}
                                                                </td>
                                                            </tr>
                                                    );
                                                })}
                                            </tbody>
                                        </table>
                                    </div>
                                </div>
                            )}
                        </div>
            );
        }

        // One automation, whole. The answer to "why did that fail last night",
        // which used to be spread over three pages that each held part of it.
        //
        // `activeAutomationId` and the job list come from App because they are
        // routing and shared polling; everything this page adds on top of them
        // is fetched here.
        function AutomationDetailView({
            activeAutomationId, active, cronJobs, adkTeams, now,
            automationAgentLink, navigateTab, navigateScorecard, navigateHermesAgent,
            runCronJobNow, cronRunning, cronRunNotice,
            selectSession, setActiveKanbanTaskId, chatAboutAutomation,
        }) {
            const {
                detail: automationDetail,
                loading: automationLoading,
                error: automationError,
            } = useAutomationDetail(active, activeAutomationId);
            const [openExecutionId, setOpenExecutionId] = useState(null);
            usePaintedIcons();

                    // The server's copy is authoritative, but the list
                    // already has this job, so a click renders the
                    // configuration immediately and fills in the run
                    // history when it lands — rather than showing a
                    // spinner for something already on screen.
                    const detail = automationDetail;
                    const job = (detail && detail.job)
                        || cronJobs.find(j => j.id === activeAutomationId)
                        || null;
                    const where = job ? automationWhere(job) : null;
                    const team = job ? adkTeams.find(t => jobMatchesApp(job, t.app)) : null;
                    const link = job ? automationAgentLink(job, where) : null;
                    const executions = (detail && detail.executions) || [];
                    const totals = (detail && detail.totals) || null;
                    const failed = totals ? (totals.by_status.failed || 0) : 0;
                    return (
                    // h-full for the reason given on the list above:
                    // this page is the one that grows without bound —
                    // twenty-five executions, any of which expands
                    // into a stack trace — so clipping it hides
                    // precisely what it was built to show.
                    <div class="h-full overflow-y-auto p-6">
                        {/* Full width, like the list it drills out of.
                            Centred in a 4xl column this page changed
                            width on the way in, and the execution rows
                            it exists to show — stack traces, trace
                            tables — were the things being squeezed. */}
                        <div class="space-y-5">

                            <div class="flex items-center justify-between gap-4 flex-wrap">
                                {/* Back to the list, always — including on a
                                    cold load of a URL that names a job that
                                    no longer exists, which is the one case
                                    where being stranded is most likely. */}
                                <button
                                    onClick={() => navigateTab('automations')}
                                    class="text-xs text-[#585b70] hover:text-[#cdd6f4] flex items-center gap-1.5 transition"
                                >
                                    <i data-lucide="arrow-left" class="w-3.5 h-3.5"></i>
                                    All automations
                                </button>
                                {/* Only once there is a job to name. On the
                                    404 and loading states the button would
                                    open a chat about nothing. */}
                                {job && (
                                    <button
                                        onClick={() => chatAboutAutomation(job)}
                                        title="Start a new chat with this automation's identity already filled in"
                                        class="text-xs font-semibold py-1.5 px-3 rounded-lg border border-[#313244] text-[#a6adc8] hover:text-[#cdd6f4] hover:border-[#45475a] flex items-center gap-2 transition"
                                    >
                                        <i data-lucide="message-square-plus" class="w-3.5 h-3.5"></i>
                                        Chat about this automation
                                    </button>
                                )}
                            </div>

                            {automationError === 'notfound' ? (
                                <div class="bg-[#181825] border border-[#313244] rounded-xl p-8 text-center space-y-2">
                                    <div class="text-sm text-[#cdd6f4]">No automation with this id.</div>
                                    <div class="text-xs text-[#585b70]">
                                        <span class="font-mono text-[#a6adc8]">{activeAutomationId}</span> is not
                                        scheduled in any profile on this host — it was deleted, or the link is wrong.
                                    </div>
                                </div>
                            ) : !job ? (
                                <div class="bg-[#181825] border border-[#313244] rounded-xl p-8 text-center text-sm text-[#585b70]">
                                    {automationError === 'unreachable'
                                        ? 'Could not reach the server.'
                                        : 'Loading automation…'}
                                </div>
                            ) : (
                                <>
                                {/* The status badge and the execution tally used
                                    to sit here, in a row of their own above the
                                    card. Both were already on the page: the
                                    card's own Outcome cell is the status, and
                                    the tally belongs to the executions it counts,
                                    so it now labels that section instead. What is
                                    left is the job itself, first. */}

                                {/* Which job this is, and when it fires —
                                    the heading the rest of the page hangs
                                    off. Through the same component the
                                    profile pane uses, so a field cannot
                                    come to mean two things in two places. */}
                                <JobCard
                                    job={job}
                                    onRunNow={runCronJobNow}
                                    running={cronRunning}
                                    notice={cronRunNotice}
                                    onOpenOwner={job.agent
                                        ? () => navigateHermesAgent(job.agent, null)
                                        : null}
                                    healthTask={(detail && detail.health_task) || null}
                                    onOpenTask={(taskId) => {
                                        setActiveKanbanTaskId(taskId);
                                        navigateTab('kanban');
                                    }}
                                />

                                {/* trigger → what runs → where it goes. The
                                    shape every automation has, drawn the
                                    same way it was on the expanded list
                                    row it replaces. */}
                                <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                    <SectionHeader icon="workflow" title="What it runs" />
                                    {/* The sentence the top card used to carry,
                                        said once, here, where the diagram under
                                        it draws the same thing. Kind first
                                        ("the workflow", "the script") because
                                        what a name means depends on it: the same
                                        dotted string is an app to launch or a
                                        module to import depending on which. */}
                                    <div class="px-4 pt-3 pb-1 flex items-center justify-between gap-4 flex-wrap">
                                        <span class="text-xs flex items-center gap-2 min-w-0">
                                            <i
                                                data-lucide={whereKind(where.label).icon}
                                                class="w-3.5 h-3.5 shrink-0"
                                                style={{ color: whereKind(where.label).color }}
                                            ></i>
                                            {where.label === 'workflow' ? (
                                                <>
                                                    <span class="text-[#585b70]">Launches the workflow</span>
                                                    <span class="font-mono text-[#b4befe] truncate">{job.adk_app || tail(where.detail) || '—'}</span>
                                                </>
                                            ) : where.label === 'script' ? (
                                                <>
                                                    <span class="text-[#585b70]">Runs the script</span>
                                                    <span class="font-mono text-[#cdd6f4] truncate">{job.script_path || job.script}</span>
                                                    {!job.script_path && (
                                                        <span class="text-[#f9e2af] shrink-0">— not on any mounted script path</span>
                                                    )}
                                                </>
                                            ) : (
                                                <>
                                                    <span class="text-[#585b70]">Runs a prompt on the</span>
                                                    <span class="font-mono text-[#cdd6f4] truncate">{job.agent}</span>
                                                    <span class="text-[#585b70]">profile</span>
                                                </>
                                            )}
                                        </span>
                                        {/* Was a bare link under the diagram, where
                                            it read as a footnote to the last node.
                                            It is the way out of this page to how
                                            the thing performs, so it sits with the
                                            name it is about. */}
                                        {link && (
                                            <button
                                                onClick={link.onClick}
                                                class="text-xs font-semibold py-1.5 px-3 rounded-lg border border-[#313244] text-[#a6adc8] hover:text-[#cdd6f4] hover:border-[#45475a] flex items-center gap-2 transition shrink-0"
                                            >
                                                <i data-lucide="line-chart" class="w-3.5 h-3.5"></i>
                                                {link.kind === 'scorecard'
                                                    ? 'See workflow scorecard'
                                                    : `See the ${link.label} profile`}
                                            </button>
                                        )}
                                    </div>
                                    <div class="px-4 pb-4 space-y-3">
                                        {/* Centred, because the chain is a
                                            diagram and not a list — left-flushed
                                            in a full-width card it sat against
                                            one edge with the rest of the row
                                            empty.
                                            The vertical room is deliberate: this
                                            diagram is what the page is about, and
                                            packed to the same rhythm as the text
                                            around it, it read as one more row. */}
                                        <div class="flex items-center justify-center gap-1.5 flex-wrap py-10">
                                            <TriggerNode job={job} />
                                            {/* The middle of the chain is the work,
                                                so it holds the row's height even
                                                when what it holds is a single node. */}
                                            <div class="flex items-center gap-1.5 min-w-0 min-h-[64px]">
                                                <i data-lucide="arrow-right" class="w-3 h-3 text-[#45475a] shrink-0"></i>
                                                {team && team.agents && team.agents.length > 0 ? (
                                                    <StepFlow
                                                        agents={team.agents}
                                                        parent={null}
                                                        onOpenAgent={(a) => navigateScorecard(`${team.app}::${a.name}`)}
                                                        depth={0}
                                                    />
                                                ) : where.label === 'workflow' ? (
                                                    <span class="text-[11px] text-[#585b70]">Loading steps…</span>
                                                ) : (
                                                    <FlowNode
                                                        primary
                                                        icon={whereKind(where.label).icon}
                                                        color={whereKind(where.label).color}
                                                        label={where.label === 'script'
                                                            ? (String(job.script || '').split('/').pop() || 'script')
                                                            : (job.agent || 'agent')}
                                                        sub={where.label === 'script' ? null : (job.effective_model || null)}
                                                        title={where.label === 'script'
                                                            ? job.script
                                                            : `Runs on the ${job.agent} profile`}
                                                    />
                                                )}
                                            </div>
                                            {outputNodes(job).map(({ sessionId, ...o }, i) => (
                                                <div key={o.label + i} class="flex items-center gap-1.5 shrink-0">
                                                    <i data-lucide="arrow-right" class="w-3 h-3 text-[#45475a] shrink-0"></i>
                                                    <FlowNode
                                                        {...o}
                                                        onClick={sessionId ? () => selectSession(sessionId) : null}
                                                    />
                                                </div>
                                            ))}
                                        </div>

                                        {/* The run worked and nobody was told.
                                            A different failure from last_error,
                                            and invisible everywhere else. */}
                                        {job.last_delivery_error && (
                                            <div class="text-[11px] text-[#f38ba8] bg-[#f38ba8]/5 border border-[#f38ba8]/20 rounded-lg px-3 py-2">
                                                <span class="font-bold">Delivery failing — the run reports to a chat that never receives it.</span>
                                                <span class="block font-mono text-[10px] text-[#f38ba8]/80 mt-1" style={{ overflowWrap: 'anywhere' }}>{job.last_delivery_error}</span>
                                            </div>
                                        )}

                                    </div>
                                </div>

                                <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                    {/* The tally counts the whole window, the list
                                        below is capped — "3 of 412 failed" and
                                        "3 of the last 25" are different claims,
                                        so both numbers are said. */}
                                    <SectionHeader
                                        icon="history"
                                        title="Executions"
                                        right={totals ? (
                                            totals.total === 0
                                                ? `none in the last ${detail.days} days`
                                                : <>
                                                    <span class="font-mono text-[#cdd6f4]">{totals.total}</span> in the last {detail.days} days
                                                    {failed > 0 && <>, <span class="font-mono text-[#f38ba8]">{failed}</span> failed</>}
                                                    {executions.length < totals.total && <> · showing {executions.length}</>}
                                                  </>
                                        ) : (executions.length ? `${executions.length} most recent` : null)} />
                                    {automationLoading && !detail ? (
                                        <div class="p-6 text-center text-sm text-[#585b70]">Loading run history…</div>
                                    ) : executions.length === 0 ? (
                                        <div class="p-6 text-center text-sm text-[#585b70] space-y-1">
                                            <div>No executions recorded in the last {(detail && detail.days) || 30} days.</div>
                                            {/* "Never ran" and "ran, unrecorded"
                                                are different problems, and only
                                                one of them is the scheduler's. */}
                                            {job.last_run_at && (
                                                <div class="text-xs text-[#f9e2af]">
                                                    The job reports a run at{' '}
                                                    <span class="font-mono">{runStamp(job.last_run_at)}</span>{' '}
                                                    that the execution store has no record of.
                                                </div>
                                            )}
                                        </div>
                                    ) : (
                                        <div class="divide-y divide-[#313244]">
                                            {executions.map(ex => (
                                                <ExecutionRow
                                                    key={ex.execution_id}
                                                    execution={ex}
                                                    open={openExecutionId === ex.execution_id}
                                                    onToggle={() => setOpenExecutionId(
                                                        openExecutionId === ex.execution_id ? null : ex.execution_id)}
                                                />
                                            ))}
                                        </div>
                                    )}
                                    <div class="px-4 py-2 border-t border-[#313244] text-[11px] text-[#585b70]">
                                        Recorded by the scheduler that ran the job, so this covers every execution —
                                        including the ones that died before the work started.
                                    </div>
                                </div>
                                </>
                            )}
                        </div>
                    </div>
                    );
        }
