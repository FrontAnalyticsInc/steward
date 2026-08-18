// --- 85-metrics-view.jsx ------------------------------------------------
// The Metrics tab, which is two pages: Outcomes (what the system produced and
// touched) and System Metrics (the cost, activity, model and job ledgers behind
// it). Both were inline in App along with eight pieces of fetched state and the
// dozen derived headlines computed from them.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
//
// fmtNum / fmtUsd / COST_CLASS_META are NOT here — they moved to 10-metrics.jsx,
// because the Agents scorecard formats its numbers with them too and would
// otherwise depend on the Metrics tab's file.
//
// Props and returned fields keep the names the moved code already used. The
// bodies are the same code that ran inside App, moved and not rewritten.

        // Everything the Metrics tab fetches, and the headlines derived from it.
        // App keeps `metricsView` and `navigateMetricsView`: which of the two
        // pages is showing is routing, and the URL owns it.
        //
        // Gated on the tab, unlike the review and kanban polls: eight requests
        // that re-read the whole ledger, and nothing outside this tab reads any
        // of it.
        function useMetrics({ activeTab }) {
            // Wider than the ADK scorecard: these cover Hermes chat and
            // automations too, so the tab can show what the whole system spent
            // rather than only what the workflows did.
            const [metricsCost, setMetricsCost] = useState(null);
            const [metricsActivity, setMetricsActivity] = useState([]);
            const [metricsModels, setMetricsModels] = useState([]);
            const [metricsJobs, setMetricsJobs] = useState([]);
            const [metricsOutputs, setMetricsOutputs] = useState(null);
            const [metricsFlow, setMetricsFlow] = useState(null);
            const [metricsSeries, setMetricsSeries] = useState([]);
            const [metricsDaily, setMetricsDaily] = useState({ produced: [], touched: [] });
            const [metricsAgents, setMetricsAgents] = useState([]);
            const [metricsWindow, setMetricsWindow] = useState(30);
            const [metricsError, setMetricsError] = useState(null);
            // Which kinds are drawn. Empty means "all of them" rather than none:
            // the page has to say something before anyone has touched a filter.
            const [producedFilter, setProducedFilter] = useState([]);
            const [touchedFilter, setTouchedFilter] = useState([]);

            // Empty means "all kinds"; clicking the only selected kind clears
            // back to all rather than leaving an empty chart, which is a state
            // nobody chooses on purpose.
            const toggleKind = useCallback((current, kind) => {
                if (kind === null) return [];
                const has = current.includes(kind);
                const next = has ? current.filter(k => k !== kind) : current.concat([kind]);
                return next.length ? next : [];
            }, []);

            // Everything the Metrics tab renders, in one pass. Eight routes
            // rather than one because they are different grains — cost per
            // class, activity per kind, usage per model, executions per job —
            // and collapsing them server-side would mean inventing a join
            // between populations that must not be added together.
            const fetchMetrics = async (days) => {
                try {
                    const qs = `?days=${days}`;
                    const [cost, activity, models, jobs, outputs, agents, series, flow] = await Promise.all([
                        fetch(`/api/metrics/cost${qs}`),
                        fetch(`/api/metrics/activity${qs}`),
                        fetch(`/api/metrics/models${qs}`),
                        fetch(`/api/metrics/automations${qs}`),
                        fetch(`/api/metrics/outputs${qs}`),
                        fetch(`/api/metrics/agents${qs}`),
                        fetch(`/api/metrics/timeseries${qs}`),
                        fetch(`/api/metrics/flow${qs}`),
                    ]);
                    // Every route is checked, not just `cost`. They fail
                    // independently — the store answers each one with its own
                    // query, and a memory or source problem can take out four of
                    // them while the other four return fine. Guarding on `cost`
                    // alone let those four fall through to `|| []` and render as
                    // a page of legitimate-looking zeroes, which reads as "the
                    // system did nothing" rather than "the numbers are missing".
                    // A blank number and an unavailable one must never look the
                    // same.
                    const named = [
                        ['cost', cost], ['activity', activity], ['models', models],
                        ['automations', jobs], ['outputs', outputs],
                        ['agents', agents], ['timeseries', series],
                    ];
                    const failed = named.filter(([, r]) => !r.ok);
                    if (failed.length) {
                        // 503 is the store saying it cannot read its sources,
                        // which is a different thing from "nothing has happened
                        // yet" and has to look different on the page.
                        const [name, resp] = failed[0];
                        const detail = await resp.json().catch(() => ({}));
                        const msg = detail.detail || `metrics unavailable (${resp.status})`;
                        // Name the routes, so a partial failure is diagnosable
                        // from the page instead of only from the network tab.
                        setMetricsError(
                            failed.length === 1
                                ? `${name}: ${msg}`
                                : `${failed.map(([n]) => n).join(', ')} unavailable — ${name}: ${msg}`
                        );
                        return;
                    }
                    setMetricsError(null);
                    setMetricsCost(await cost.json());
                    setMetricsActivity((await activity.json()).activity || []);
                    setMetricsModels((await models.json()).models || []);
                    setMetricsJobs((await jobs.json()).jobs || []);
                    const outs = await outputs.json();
                    setMetricsOutputs(outs);
                    setMetricsDaily({
                        produced: outs.daily_produced || [],
                        touched: outs.daily_touched || [],
                    });
                    setMetricsAgents((await agents.json()).agents || []);
                    setMetricsSeries((await series.json()).days || []);
                    // Its own guard rather than riding on `cost.ok`: the flow
                    // diagrams read the queue directories as well as the trace
                    // log, so they can be unavailable while every other tile on
                    // the page is fine. A stale diagram beside fresh numbers
                    // would be worse than none.
                    setMetricsFlow(flow.ok ? await flow.json() : null);
                } catch (err) {
                    setMetricsError(String(err));
                }
            };

            useEffect(() => {
                if (activeTab !== 'metrics') return;
                fetchMetrics(metricsWindow);
                const interval = setInterval(() => fetchMetrics(metricsWindow), 15000);
                return () => clearInterval(interval);
            }, [activeTab, metricsWindow]);

            // Outcome headlines. Derived here rather than in the markup so the
            // page reads as a layout and these stay checkable in one place.
            const producedRows = (metricsOutputs || {}).produced || [];
            const touchedRows = (metricsOutputs || {}).touched || [];
            const producedTotal = producedRows.reduce((a, r) => a + (r.total || 0), 0);
            const touchedTotal = touchedRows.reduce((a, r) => a + (r.total || 0), 0);
            // The kinds a chart offers come from the data, not the enum: showing
            // a bubble for a kind nothing has ever produced invites the reader to
            // filter to an empty chart.
            const producedKinds = React.useMemo(
                () => Array.from(new Set(metricsDaily.produced.map(r => r.kind))),
                [metricsDaily.produced]);
            const touchedKinds = React.useMemo(
                () => Array.from(new Set(metricsDaily.touched.map(r => r.kind))),
                [metricsDaily.touched]);
            const workflowRuns = metricsActivity
                .filter(r => r.kind === 'workflow_run')
                .reduce((a, r) => a + (r.activities || 0), 0);
            const failedRuns = metricsActivity
                .reduce((a, r) => a + (r.failed || 0), 0);
            const totalActivities = metricsActivity
                .reduce((a, r) => a + (r.activities || 0), 0);
            const totalTokens = ((metricsCost || {}).classes || [])
                .reduce((a, c) => a + (c.input_tokens || 0) + (c.output_tokens || 0), 0);
            // Only the metered class carries dollars; the summary tile must not
            // imply the other two are free rather than unpriced.
            const meteredSpend = ((metricsCost || {}).classes || [])
                .filter(c => c.cost_class === 'metered')
                .reduce((a, c) => (c.cost_usd === null || c.cost_usd === undefined ? a : a + c.cost_usd), 0);

            return {
                metricsCost, metricsActivity, metricsModels, metricsJobs,
                metricsOutputs, metricsFlow, metricsSeries, metricsDaily,
                metricsAgents, metricsWindow, setMetricsWindow, metricsError,
                producedFilter, setProducedFilter, touchedFilter, setTouchedFilter,
                toggleKind, producedTotal, touchedTotal, producedKinds, touchedKinds,
                workflowRuns, failedRuns, totalActivities, totalTokens, meteredSpend,
            };
        }

        // Outcomes: what the system actually produced and touched. The landing
        // page of the tab, deliberately ahead of the ledgers — those matter when
        // something looks wrong, this matters every day.
        function MetricsOutcomesView({ metrics, metricsView, navigateMetricsView, review }) {
            const {
                metricsWindow, setMetricsWindow, metricsError, metricsOutputs,
                metricsDaily, metricsFlow,
                producedFilter, setProducedFilter, touchedFilter, setTouchedFilter,
                toggleKind, producedTotal, touchedTotal, producedKinds, touchedKinds,
                workflowRuns, failedRuns, totalActivities, totalTokens, meteredSpend,
            } = metrics;
            usePaintedIcons();
            return (
                <div class="h-full overflow-y-auto p-6">
                    <div class="max-w-5xl mx-auto space-y-6">

                        <div class="flex items-start justify-between gap-4">
                            <div>
                                {/* Top left, mirroring the back link on the
                                    system page, so the pair reads as one
                                    switch rather than two unrelated links. */}
                                <button
                                    onClick={() => navigateMetricsView('system')}
                                    class="flex items-center gap-1.5 text-[11px] text-[#b4befe] hover:text-[#cdd6f4] mb-1.5 transition-colors">
                                    <i data-lucide="gauge" class="w-3.5 h-3.5"></i>
                                    System metrics
                                    <i data-lucide="arrow-right" class="w-3 h-3"></i>
                                </button>
                                <h2 class="text-lg font-bold text-[#cdd6f4]">Outcomes</h2>
                                <p class="text-xs text-[#585b70] mt-1">
                                    What the fleet produced and what it read. Refreshes every 15 seconds.
                                </p>
                            </div>
                            <div class="flex gap-1 shrink-0">
                                {[7, 30, 90, 3650].map(d => (
                                    <button
                                        key={d}
                                        onClick={() => setMetricsWindow(d)}
                                        class={`px-2.5 py-1 rounded-md text-[11px] font-medium border transition-colors ${
                                            metricsWindow === d
                                                ? 'bg-[#313244] border-[#585b70] text-[#cdd6f4]'
                                                : 'bg-[#181825] border-[#313244] text-[#585b70] hover:text-[#a6adc8]'
                                        }`}
                                    >{d === 3650 ? 'All' : `${d}d`}</button>
                                ))}
                            </div>
                        </div>

                        {metricsError && (
                            <div class="bg-[#181825] border border-[#ef4444]/40 rounded-xl p-4 text-sm text-[#ef4444]">
                                {metricsError}
                            </div>
                        )}

                        {/* Callouts. `Sent unattended` is given its own
                            tile and its own colour because it is the one
                            number here with no human in the loop — every
                            other figure describes work somebody can still
                            review before it leaves. */}
                        <div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
                            {[
                                {
                                    label: 'Produced',
                                    value: producedTotal,
                                    hint: 'artifacts created',
                                    color: '#cdd6f4',
                                },
                                {
                                    label: 'Sent unattended',
                                    value: (metricsOutputs || {}).unattended_sends,
                                    hint: 'left the building with no review',
                                    color: '#f9e2af',
                                },
                                {
                                    label: 'Touched',
                                    value: touchedTotal,
                                    hint: 'data elements read',
                                    color: '#cdd6f4',
                                },
                                {
                                    label: 'Workflow runs',
                                    value: workflowRuns,
                                    hint: 'ADK pipeline invocations',
                                    color: '#cdd6f4',
                                },
                            ].map(c => (
                                <div key={c.label} class="bg-[#181825] border border-[#313244] rounded-xl p-4">
                                    <div class="text-[10px] uppercase font-bold tracking-wider text-[#9ca3af] mb-2">
                                        {c.label}
                                    </div>
                                    <div class="font-mono text-2xl" style={{ color: c.color }}>
                                        {fmtNum(c.value)}
                                    </div>
                                    <div class="text-[11px] text-[#585b70] mt-1">{c.hint}</div>
                                </div>
                            ))}
                        </div>

                        {/* Produced per day. Lines, not a stack: the
                            question is what each kind did, and a stacked
                            band moves every series' baseline with the one
                            below it. */}
                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                            <div class="px-4 pt-3 flex items-baseline justify-between gap-4">
                                <div>
                                    <div class="text-sm text-[#cdd6f4]">Produced per day</div>
                                    <div class="text-[11px] text-[#585b70]">
                                        artifacts created — a side effect actually happened
                                    </div>
                                </div>
                            </div>
                            <KindBubbles
                                kinds={producedKinds}
                                order={PRODUCED_KINDS}
                                selected={producedFilter}
                                onToggle={(k) => setProducedFilter(toggleKind(producedFilter, k))}
                            />
                            <KindLines
                                rows={metricsDaily.produced}
                                order={PRODUCED_KINDS}
                                selected={producedFilter}
                                emptyNote="Nothing produced in this window — runs report this from trace version 3 onward."
                            />
                        </div>

                        {/* Touched per day, same treatment. Kept a
                            separate chart rather than a second axis:
                            input volume and output volume are different
                            quantities and one scale would flatten
                            whichever is smaller. */}
                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                            <div class="px-4 pt-3 flex items-baseline justify-between gap-4">
                                <div>
                                    <div class="text-sm text-[#cdd6f4]">Touched per day</div>
                                    <div class="text-[11px] text-[#585b70]">
                                        data elements read or considered
                                    </div>
                                </div>
                            </div>
                            <KindBubbles
                                kinds={touchedKinds}
                                order={TOUCHED_KINDS}
                                selected={touchedFilter}
                                onToggle={(k) => setTouchedFilter(toggleKind(touchedFilter, k))}
                            />
                            <KindLines
                                rows={metricsDaily.touched}
                                order={TOUCHED_KINDS}
                                selected={touchedFilter}
                                emptyNote="Nothing recorded in this window — runs report this from trace version 3 onward."
                            />
                        </div>

                        {/* Where things went, rather than how many there were.
                            This belongs on Outcomes and not on System Metrics:
                            it answers what became of the mail, which is the
                            question this screen is for.

                            Two diagrams and never one: filing a message and
                            drafting a reply to it are orthogonal, so a single
                            chained flow would draw the drafts twice. Each one
                            balances — backend/review_flow.py guarantees it and
                            its tests assert it — which is why the awkward nodes
                            ("Still pending", "Outcome not recorded") are on the
                            chart rather than quietly dropped. */}
                        {(metricsFlow?.diagrams || []).map(d => (
                            <div key={d.key}
                                 class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                <div class="px-4 py-3 border-b border-[#313244] flex items-baseline justify-between gap-4">
                                    <span class="text-xs font-semibold uppercase tracking-wider text-[#585b70]">
                                        {d.title}
                                    </span>
                                    <span class="text-[11px] text-[#585b70]">{d.subtitle}</span>
                                </div>
                                <SankeyDiagram diagram={d} />
                                {!!(d.notes || []).length && (
                                    <div class="px-4 py-2 border-t border-[#313244] text-[11px] text-[#585b70] space-y-1">
                                        {d.notes.map((n, i) => <div key={i}>{n}</div>)}
                                    </div>
                                )}
                            </div>
                        ))}

                        {/* The machine, in four numbers, with the door to
                            the rest of them. Enough to notice something is
                            wrong from here; not enough to diagnose it. */}
                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                            <div class="px-4 py-3 border-b border-[#313244] text-xs font-semibold uppercase tracking-wider text-[#585b70]">
                                System
                            </div>
                            <div class="grid grid-cols-2 lg:grid-cols-4 divide-x divide-[#313244]">
                                {[
                                    { label: 'Metered spend', value: fmtUsd(meteredSpend) },
                                    { label: 'Tokens', value: fmtNum(totalTokens) },
                                    { label: 'Activities', value: fmtNum(totalActivities) },
                                    { label: 'Failed runs', value: fmtNum(failedRuns) },
                                ].map(s => (
                                    <div key={s.label} class="px-4 py-3">
                                        <div class="text-[10px] uppercase font-bold tracking-wider text-[#9ca3af] mb-1">
                                            {s.label}
                                        </div>
                                        <div class="font-mono text-lg text-[#cdd6f4]">{s.value}</div>
                                    </div>
                                ))}
                            </div>
                        </div>

                    </div>
                </div>
            );
        }

        // System Metrics: the cost, activity, output, agent, model and job
        // ledgers behind the outcomes page.
        function MetricsSystemView({
            metrics, metricsView, navigateMetricsView,
            navigateAutomation, navigateScorecard, automations, cronJobs,
        }) {
            const {
                metricsWindow, setMetricsWindow, metricsError, metricsCost,
                metricsActivity, metricsOutputs, metricsAgents, metricsModels,
                metricsJobs, metricsSeries,
            } = metrics;
            usePaintedIcons();
            return (
                <div class="h-full overflow-y-auto p-6">
                    <div class="max-w-5xl mx-auto space-y-6">

                        <div class="flex items-start justify-between gap-4">
                            <div>
                                <button
                                    onClick={() => navigateMetricsView(null)}
                                    class="flex items-center gap-1.5 text-[11px] text-[#b4befe] hover:text-[#cdd6f4] mb-1.5 transition-colors">
                                    <i data-lucide="arrow-left" class="w-3.5 h-3.5"></i>
                                    Outcomes
                                </button>
                                <h2 class="text-lg font-bold text-[#cdd6f4]">System Metrics</h2>
                                <p class="text-xs text-[#585b70] mt-1">
                                    ADK workflows, Hermes chat and scheduled automations, across every profile. Refreshes every 15 seconds.
                                </p>
                            </div>
                            <div class="flex gap-1 shrink-0">
                                {[7, 30, 90, 3650].map(d => (
                                    <button
                                        key={d}
                                        onClick={() => setMetricsWindow(d)}
                                        class={`px-2.5 py-1 rounded-md text-[11px] font-medium border transition-colors ${
                                            metricsWindow === d
                                                ? 'bg-[#313244] border-[#585b70] text-[#cdd6f4]'
                                                : 'bg-[#181825] border-[#313244] text-[#585b70] hover:text-[#a6adc8]'
                                        }`}
                                    >{d === 3650 ? 'All' : `${d}d`}</button>
                                ))}
                            </div>
                        </div>

                        {metricsError && (
                            <div class="bg-[#181825] border border-[#ef4444]/40 rounded-xl p-4 text-sm text-[#ef4444]">
                                {metricsError}
                            </div>
                        )}

                        {/* Cost, split three ways and deliberately not totalled.
                            The note under the cards is load-bearing: three dollar-ish
                            numbers side by side invite addition, and adding these
                            would produce a figure that is neither spend nor capacity. */}
                        <div>
                            <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
                                {['metered', 'included', 'unpriced'].map(cls => {
                                    const meta = COST_CLASS_META[cls];
                                    const row = (metricsCost?.classes || []).find(c => c.cost_class === cls);
                                    return (
                                        <div key={cls} class="bg-[#181825] border border-[#313244] rounded-xl p-4">
                                            <div class="text-[10px] uppercase font-bold tracking-wider mb-2" style={{ color: meta.color }}>
                                                {meta.label}
                                            </div>
                                            <div class="font-mono text-2xl text-[#cdd6f4]">
                                                {cls === 'metered'
                                                    ? fmtUsd(row ? row.cost_usd : null)
                                                    : fmtNum(row ? (row.input_tokens || 0) + (row.output_tokens || 0) : null)}
                                                {cls !== 'metered' && row && (
                                                    <span class="text-xs text-[#585b70] ml-1">tok</span>
                                                )}
                                            </div>
                                            <div class="text-[11px] text-[#585b70] mt-1">{meta.hint}</div>
                                            <div class="text-[11px] text-[#9ca3af] mt-2 font-mono">
                                                {row ? `${fmtNum(row.activities)} activities · ${fmtNum(row.api_calls)} calls` : 'nothing recorded'}
                                            </div>
                                        </div>
                                    );
                                })}
                            </div>
                            <p class="text-[11px] text-[#585b70] mt-2">
                                These three are not added together. Metered is money; subscription usage is real work whose marginal
                                cost is zero; unpriced is local inference nobody has a rate for. A single total would be none of those.
                            </p>
                        </div>

                        {/* Trends. Two charts rather than one with two y-scales:
                            tokens and run counts differ by four orders of
                            magnitude, and a dual axis would invite the reader to
                            infer a relationship from the scaling. */}
                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden divide-y divide-[#313244]">
                            <DailyStacks
                                rows={metricsSeries}
                                valueOf={(r) => (r.input_tokens || 0) + (r.output_tokens || 0)}
                                title="Tokens per day"
                                subtitle="input + output, by producer"
                            />
                            <DailyStacks
                                rows={metricsSeries}
                                valueOf={(r) => r.activities || 0}
                                title="Activity per day"
                                subtitle="runs, chats and automations"
                            />
                        </div>

                        {/* Token kinds. Not all tokens are equal, and cache reads
                            in particular dwarf everything else on this host — a
                            single "tokens" number would hide that entirely. */}
                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                            <div class="px-4 py-3 border-b border-[#313244] text-xs font-semibold uppercase tracking-wider text-[#585b70]">
                                Tokens by kind
                            </div>
                            {!metricsCost || !(metricsCost.classes || []).length ? (
                                <div class="p-6 text-center text-sm text-[#585b70]">Nothing recorded in this window.</div>
                            ) : (
                                <div class="overflow-x-auto">
                                    <table class="w-full text-sm">
                                        <thead>
                                            <tr class="text-[10px] uppercase tracking-wider text-[#585b70]">
                                                <th class="text-left font-semibold px-4 py-2">Class</th>
                                                <th class="text-right font-semibold px-4 py-2">Input</th>
                                                <th class="text-right font-semibold px-4 py-2">Output</th>
                                                <th class="text-right font-semibold px-4 py-2">Cache read</th>
                                                <th class="text-right font-semibold px-4 py-2">Cache write</th>
                                                <th class="text-right font-semibold px-4 py-2">Reasoning</th>
                                            </tr>
                                        </thead>
                                        <tbody class="divide-y divide-[#313244]">
                                            {metricsCost.classes.map(row => (
                                                <tr key={row.cost_class}>
                                                    <td class="px-4 py-2 text-[#cdd6f4]">
                                                        {(COST_CLASS_META[row.cost_class] || {}).label || row.cost_class}
                                                    </td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#a6adc8]">{fmtNum(row.input_tokens)}</td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#a6adc8]">{fmtNum(row.output_tokens)}</td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#a6adc8]">{fmtNum(row.cache_read_tokens)}</td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#a6adc8]">{fmtNum(row.cache_write_tokens)}</td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#a6adc8]">{fmtNum(row.reasoning_tokens)}</td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                            )}
                        </div>

                        {/* Activity. `succeeded`/`failed` render as em-dash wherever
                            outcome_known is 0: Hermes records how a session stopped,
                            not whether it worked, so a 0 there would be a verdict
                            nobody issued. */}
                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                            <div class="px-4 py-3 border-b border-[#313244] text-xs font-semibold uppercase tracking-wider text-[#585b70]">
                                Activity
                            </div>
                            {!metricsActivity.length ? (
                                <div class="p-6 text-center text-sm text-[#585b70]">Nothing recorded in this window.</div>
                            ) : (
                                <div class="overflow-x-auto">
                                    <table class="w-full text-sm">
                                        <thead>
                                            <tr class="text-[10px] uppercase tracking-wider text-[#585b70]">
                                                <th class="text-left font-semibold px-4 py-2">Kind</th>
                                                <th class="text-left font-semibold px-4 py-2">Source</th>
                                                <th class="text-left font-semibold px-4 py-2">Profile</th>
                                                <th class="text-right font-semibold px-4 py-2">Runs</th>
                                                <th class="text-right font-semibold px-4 py-2">Ok</th>
                                                <th class="text-right font-semibold px-4 py-2">Failed</th>
                                                <th class="text-right font-semibold px-4 py-2">Last</th>
                                            </tr>
                                        </thead>
                                        <tbody class="divide-y divide-[#313244]">
                                            {metricsActivity.map((row, i) => (
                                                <tr key={i}>
                                                    <td class="px-4 py-2 text-[#cdd6f4]">{row.kind}</td>
                                                    <td class="px-4 py-2 text-[#a6adc8]">{cleanStr(row.source)}</td>
                                                    <td class="px-4 py-2 text-[#585b70]">{cleanStr(row.profile)}</td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#cdd6f4]">{fmtNum(row.activities)}</td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#a6e3a1]">{fmtNum(row.succeeded)}</td>
                                                    <td class={`px-4 py-2 text-right font-mono ${row.failed ? 'text-[#ef4444]' : 'text-[#585b70]'}`}>
                                                        {fmtNum(row.failed)}
                                                    </td>
                                                    <td class="px-4 py-2 text-right font-mono text-[10px] text-[#585b70]">
                                                        {row.last_at ? String(row.last_at).slice(0, 16).replace('T', ' ') : '—'}
                                                    </td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                            )}
                            <div class="px-4 py-2 border-t border-[#313244] text-[11px] text-[#585b70]">
                                A dash under Ok/Failed means that kind has no success vocabulary — not that nothing succeeded.
                            </div>
                        </div>

                        {/* What the fleet produced. `unattended_sends` is pulled
                            out above the table because it is the one number that
                            says how much left the building with nobody looking. */}
                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                            <div class="px-4 py-3 border-b border-[#313244] flex items-center justify-between gap-4">
                                <span class="text-xs font-semibold uppercase tracking-wider text-[#585b70]">Produced &amp; touched</span>
                                {metricsOutputs && metricsOutputs.unattended_sends !== null
                                        && metricsOutputs.unattended_sends !== undefined && (
                                    <span class="text-[11px] text-[#f9e2af] font-mono">
                                        {fmtNum(metricsOutputs.unattended_sends)} sent unattended
                                    </span>
                                )}
                            </div>
                            {!metricsOutputs || (!metricsOutputs.produced?.length && !metricsOutputs.touched?.length) ? (
                                <div class="p-6 text-center text-sm text-[#585b70]">
                                    Not recorded yet — runs report this from trace version 3 onward.
                                </div>
                            ) : (
                                <div class="grid grid-cols-1 sm:grid-cols-2 divide-y sm:divide-y-0 sm:divide-x divide-[#313244]">
                                    {[['Produced', metricsOutputs.produced, 'artifacts created — a side effect happened'],
                                      ['Touched', metricsOutputs.touched, 'data elements read or considered']].map(([label, rows, hint]) => (
                                        <div key={label} class="p-4">
                                            <div class="text-[10px] uppercase font-bold tracking-wider text-[#9ca3af]">{label}</div>
                                            <div class="text-[11px] text-[#585b70] mb-2">{hint}</div>
                                            {!(rows || []).length ? (
                                                <div class="text-sm text-[#585b70]">—</div>
                                            ) : (rows.map(r => (
                                                <div key={r.kind} class="flex items-center justify-between py-1">
                                                    <span class={`text-sm ${r.kind === 'auto_email' ? 'text-[#f9e2af]' : 'text-[#cdd6f4]'}`}>
                                                        {r.kind}
                                                    </span>
                                                    <span class="font-mono text-sm text-[#a6adc8]">{fmtNum(r.total)}</span>
                                                </div>
                                            )))}
                                        </div>
                                    ))}
                                </div>
                            )}
                            <div class="px-4 py-2 border-t border-[#313244] text-[11px] text-[#585b70]">
                                Produced and touched are never added: one is input volume, the other is side effects.
                                A queued draft and an unattended send are counted apart on purpose.
                            </div>
                        </div>

                        {/* Per-agent scorecard. The measured and claimed columns
                            sit side by side and are never combined — see
                            app/self_assessment.py for why that matters. */}
                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                            <div class="px-4 py-3 border-b border-[#313244] text-xs font-semibold uppercase tracking-wider text-[#585b70]">
                                Agent scorecard
                            </div>
                            {!metricsAgents.length ? (
                                <div class="p-6 text-center text-sm text-[#585b70]">No workflow runs in this window.</div>
                            ) : (
                                <div class="overflow-x-auto">
                                    <table class="w-full text-sm">
                                        <thead>
                                            <tr class="text-[10px] uppercase tracking-wider text-[#585b70]">
                                                <th class="text-left font-semibold px-4 py-2">Agent</th>
                                                <th class="text-left font-semibold px-4 py-2">Model</th>
                                                <th class="text-right font-semibold px-4 py-2">Runs</th>
                                                <th class="text-right font-semibold px-4 py-2">Turns</th>
                                                <th class="text-right font-semibold px-4 py-2" title="Measured: fraction of declared checkpoints that passed">Passed</th>
                                                <th class="text-right font-semibold px-4 py-2" title="Claimed: the model's own opinion of its turn">Self</th>
                                                <th class="text-left font-semibold px-4 py-2">Could improve</th>
                                            </tr>
                                        </thead>
                                        <tbody class="divide-y divide-[#313244]">
                                            {metricsAgents.map((row, i) => (
                                                <tr key={i} class="hover:bg-[#1e1e2e] cursor-pointer"
                                                    onClick={() => row.app && row.agent && navigateScorecard(`${row.app}::${row.agent}`)}>
                                                    <td class="px-4 py-2">
                                                        <div class="text-[#cdd6f4]">{cleanStr(row.agent)}</div>
                                                        <div class="text-[10px] text-[#585b70] font-mono">{tail(cleanStr(row.app, ''))}</div>
                                                    </td>
                                                    <td class="px-4 py-2 font-mono text-[11px] text-[#a6adc8] break-all">{cleanStr(row.model)}</td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#a6adc8]">{fmtNum(row.runs)}</td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#a6adc8]">{fmtNum(row.turns)}</td>
                                                    <td class={`px-4 py-2 text-right font-mono ${
                                                        row.checkpoint_pass_rate === null || row.checkpoint_pass_rate === undefined
                                                            ? 'text-[#585b70]'
                                                            : row.checkpoint_pass_rate < 0.8 ? 'text-[#ef4444]' : 'text-[#a6e3a1]'
                                                    }`}>
                                                        {row.checkpoint_pass_rate === null || row.checkpoint_pass_rate === undefined
                                                            ? '—' : `${Math.round(row.checkpoint_pass_rate * 100)}%`}
                                                    </td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#b4befe]">
                                                        {row.self_score === null || row.self_score === undefined
                                                            ? '—' : row.self_score.toFixed(2)}
                                                    </td>
                                                    <td class="px-4 py-2 text-[11px] text-[#9ca3af] max-w-xs truncate" title={cleanStr(row.could_improve, '')}>
                                                        {cleanStr(row.could_improve)}
                                                    </td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                            )}
                            <div class="px-4 py-2 border-t border-[#313244] text-[11px] text-[#585b70]">
                                <span class="text-[#a6e3a1]">Passed</span> is measured from what the stages recorded.
                                <span class="text-[#b4befe]"> Self</span> is the model's own opinion of its turn.
                                They are shown side by side and never averaged together — a model cannot raise a measured number.
                            </div>
                        </div>

                        {/* Usage by model. The cost column is blank for anything
                            not metered, which is most of it. */}
                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                            <div class="px-4 py-3 border-b border-[#313244] text-xs font-semibold uppercase tracking-wider text-[#585b70]">
                                By model
                            </div>
                            {!metricsModels.length ? (
                                <div class="p-6 text-center text-sm text-[#585b70]">Nothing recorded in this window.</div>
                            ) : (
                                <div class="overflow-x-auto">
                                    <table class="w-full text-sm">
                                        <thead>
                                            <tr class="text-[10px] uppercase tracking-wider text-[#585b70]">
                                                <th class="text-left font-semibold px-4 py-2">Model</th>
                                                <th class="text-left font-semibold px-4 py-2">Provider</th>
                                                <th class="text-left font-semibold px-4 py-2">Class</th>
                                                <th class="text-right font-semibold px-4 py-2">Calls</th>
                                                <th class="text-right font-semibold px-4 py-2">In</th>
                                                <th class="text-right font-semibold px-4 py-2">Out</th>
                                                <th class="text-right font-semibold px-4 py-2">Cost</th>
                                            </tr>
                                        </thead>
                                        <tbody class="divide-y divide-[#313244]">
                                            {metricsModels.map((row, i) => (
                                                <tr key={i}>
                                                    <td class="px-4 py-2 font-mono text-xs break-all">
                                                        {row.model
                                                            ? <span class="text-[#cdd6f4]">{row.model}</span>
                                                            : <span class="text-[#585b70] italic" title="These runs predate per-agent model capture (trace v3)">not recorded</span>}
                                                    </td>
                                                    <td class="px-4 py-2 text-[#a6adc8] text-xs">{cleanStr(row.billing_provider)}</td>
                                                    <td class="px-4 py-2 text-xs" style={{ color: (COST_CLASS_META[row.cost_class] || {}).color || '#9ca3af' }}>
                                                        {row.cost_class}
                                                    </td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#a6adc8]">{fmtNum(row.api_calls)}</td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#a6adc8]">{fmtNum(row.input_tokens)}</td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#a6adc8]">{fmtNum(row.output_tokens)}</td>
                                                    <td class="px-4 py-2 text-right font-mono text-[#cdd6f4]">{fmtUsd(row.cost_usd)}</td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </table>
                                </div>
                            )}
                        </div>

                        {/* Scheduled jobs. Counted separately from the activity
                            table above on purpose — most executions never open a
                            model session, so the two are different populations and
                            adding them would misstate both. */}
                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                            <div class="px-4 py-3 border-b border-[#313244] text-xs font-semibold uppercase tracking-wider text-[#585b70]">
                                Scheduled job executions
                            </div>
                            {!metricsJobs.length ? (
                                <div class="p-6 text-center text-sm text-[#585b70]">No executions in this window.</div>
                            ) : (
                                <div class="divide-y divide-[#313244]">
                                    {metricsJobs.map((row, i) => {
                                        // The ledger reports a job by id and nothing
                                        // else, which is exactly as far as a count
                                        // can take you: "78 failed" is where the
                                        // question starts. The row opens the
                                        // automation, where the failures have
                                        // names, times and error text.
                                        //
                                        // Named from the cron list rather than the
                                        // ledger — the metrics store knows the id
                                        // and the scheduler knows what it is called.
                                        const known = cronJobs.find(j => j.id === row.job_id);
                                        return (
                                        <button
                                            key={i}
                                            onClick={() => navigateAutomation(row.job_id)}
                                            class="w-full text-left px-4 py-2.5 flex items-center justify-between gap-4 hover:bg-[#313244]/25 transition"
                                        >
                                            <div class="flex items-center gap-2.5 overflow-hidden">
                                                <span class={`w-2 h-2 rounded-full shrink-0 ${
                                                    row.status === 'failed' ? 'bg-[#ef4444]'
                                                        : row.status === 'completed' ? 'bg-[#a6e3a1]'
                                                        : 'bg-[#585b70]'
                                                }`}></span>
                                                <span class="text-xs text-[#cdd6f4] truncate">
                                                    {known ? (known.name || known.id) : cleanStr(row.job_id)}
                                                </span>
                                                <span class="text-[10px] uppercase tracking-wider text-[#9ca3af]">{cleanStr(row.status)}</span>
                                                {/* A job the scheduler no longer lists.
                                                    Its executions outlive it, and saying
                                                    so beats a link that 404s silently. */}
                                                {!known && (
                                                    <span class="text-[10px] text-[#585b70] shrink-0" title="No profile schedules this id any more">
                                                        deleted
                                                    </span>
                                                )}
                                            </div>
                                            <div class="flex items-center gap-4 shrink-0">
                                                <span class="font-mono text-xs text-[#cdd6f4]">{fmtNum(row.executions)}</span>
                                                <span class="font-mono text-[10px] text-[#585b70]">
                                                    {row.last_at ? String(row.last_at).slice(0, 16).replace('T', ' ') : '—'}
                                                </span>
                                                <i data-lucide="chevron-right" class="w-3.5 h-3.5 text-[#585b70]"></i>
                                            </div>
                                        </button>
                                        );
                                    })}
                                </div>
                            )}
                            <div class="px-4 py-2 border-t border-[#313244] text-[11px] text-[#585b70]">
                                Executions are counted here and not in Activity above: most scheduled runs never open a model session,
                                so the two populations are reported separately rather than summed.
                                Open a row for the runs behind the count.
                            </div>
                        </div>

                    </div>
                </div>
            );
        }
