// --- 90-app.jsx ------------------------------------------------------------
// App itself - all the state, the fetches, and the per-tab views - plus the
// error boundary and the mount. This is the file the split is aimed at next:
// each tab peels off into its own view and a hook that owns its state.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
        function App() {
            // First-run gate. Asked once on mount, and only ever able to REPORT —
            // the endpoint behind it holds no credential and changes nothing.
            // `setupDone` is the operator saying "I have seen this"; it lives in
            // sessionStorage so it does not follow them to another browser, and
            // does not outlive a genuine reinstall.
            const [setupState, setSetupState] = useState(null);
            const [setupDismissed, setSetupDismissed] = useState(
                () => sessionStorage.getItem('steward.setupSeen') === '1');

            useEffect(() => {
                let alive = true;
                fetch('/api/setup/state')
                    .then(r => r.ok ? r.json() : null)
                    .then(d => { if (alive && d) setSetupState(d); })
                    .catch(() => {});
                return () => { alive = false; };
            }, []);

            const dismissSetup = useCallback(() => {
                sessionStorage.setItem('steward.setupSeen', '1');
                setSetupDismissed(true);
            }, []);

            // Read once. Every later URL change goes through the sync effect
            // below, so this is only ever the entry URL.
            const entryRoute = useRef(routeFromLocation()).current;

            const [activeTab, setActiveTab] = useState(entryRoute.tab); // one of TAB_PATHS

            // Switch tabs and push a history entry so the URL stays in sync and
            // Back returns to the previous tab. Re-selecting the current tab is a
            // no-op rather than stacking duplicate history entries.
            const navigateTab = useCallback((tab) => {
                // Compare against the URL rather than state: the effects below keep
                // the two in sync, and this keeps the setter pure (a pushState inside
                // a state updater would fire twice under StrictMode).
                if (tabFromLocation() !== tab) {
                    window.history.pushState({ tab }, '', '/' + tab);
                }
                setSettingsSection(null);
                // Clicking the tab means its own landing page. Without this,
                // returning to Metrics from another tab would drop you back on
                // /metrics/system, which is the page you go *to*, not the one
                // the tab is about.
                if (tab === 'metrics') setMetricsView(null);
                setActiveTab(tab);
                // Clicking a tab means "take me to that tab", so the tab's own
                // index is what it has to land on. Without this the pushed
                // /automations is immediately replaced by the URL effect, which
                // still reads an open automation off state and writes the detail
                // path back — the tab button and the Back-to-list button both
                // appear to do nothing.
                //
                // Only the automations selection is cleared. The relationship
                // differs per tab: /automations is a list you drill out of, so
                // returning to it is the point, while /chat is a *new, unsent*
                // conversation — clearing there would mean clicking the Chat tab
                // throws away the conversation you were reading. Kanban is the
                // same: /kanban is the board with a task still pinned.
                if (tab === 'automations') setActiveAutomationId(null);
                // Memory is the same relationship: /memory is the document
                // list, which is the page the tab is *about*, and leaving a
                // document open would make the tab button look inert to
                // anyone who pressed it while reading one.
                if (tab === 'memory') { setMemoryView(DEFAULT_MEMORY_VIEW); setMemorySlug(null); }
            }, []);

            // --- Settings overlay ---
            // Which settings section is open, or null for "not open". It is not
            // part of activeTab on purpose: the overlay sits on top of a tab
            // and closing it puts that tab back, so the two have to be able to
            // hold a value at the same time.
            const [settingsSection, setSettingsSection] = useState(entryRoute.settings);

            // Opening pushes — it is a place you go, and Back is one of the two
            // ways out of it (the X is the other). Moving between sections
            // inside the overlay only sets state; the URL effect below replaces
            // the path, so a section switch does not pile up history entries
            // between you and the tab you came from.
            const openSettings = useCallback((section) => {
                const want = '/settings/' + (section || DEFAULT_SETTINGS_SECTION);
                if (window.location.pathname !== want) {
                    window.history.pushState({ settings: section || DEFAULT_SETTINGS_SECTION }, '', want);
                }
                setSettingsSection(section || DEFAULT_SETTINGS_SECTION);
            }, []);
            const closeSettings = useCallback(() => setSettingsSection(null), []);

            // --- Stack health ---
            // Addressable, exactly like Settings: /health owns a path so Back
            // leaves the modal instead of leaving the console, and so the page
            // can be linked to — "the host is unwell, look at /health" is the
            // most sendable thing on this dashboard.
            const [health, setHealth] = useState(null);
            const [healthError, setHealthError] = useState(null);
            const [healthOpen, setHealthOpen] = useState(entryRoute.health);
            const [healthRefreshing, setHealthRefreshing] = useState(false);

            // Opening pushes — it is a place you go, and Back is one of the two
            // ways out of it (the X and Esc are the other). Closing only sets
            // state; the URL effect below replaces the path with whatever is
            // underneath, the same way closeSettings does.
            const openHealth = useCallback(() => {
                if (window.location.pathname !== '/health') {
                    window.history.pushState({ health: true }, '', '/health');
                }
                setHealthOpen(true);
            }, []);
            const closeHealth = useCallback(() => setHealthOpen(false), []);

            // --- Theme ---
            // 'system' is a real choice, not the absence of one: it keeps
            // following the OS after the fact, which is what a laptop that goes
            // light at sunrise needs. 'light'/'dark' pin it.
            const [theme, setTheme] = useState(readTheme);
            useEffect(() => {
                const apply = () => {
                    document.documentElement.dataset.theme =
                        theme === 'system' ? systemTheme() : theme;
                };
                apply();
                try { localStorage.setItem(PREF_THEME, theme); } catch (err) { /* private mode */ }
                if (theme !== 'system') return;
                const mq = window.matchMedia('(prefers-color-scheme: light)');
                mq.addEventListener('change', apply);
                return () => mq.removeEventListener('change', apply);
            }, [theme]);

            // --- Messaging channels ---
            // Host state, not a preference: /api/channels proxies Hermes's own
            // messaging-platform API, so what this shows is what the gateway
            // will read at boot. Fetched when the section opens rather than
            // polled — it changes when someone changes it, and a settings page
            // that reshuffles under a half-typed token is worse than a stale one.
            const [channelList, setChannelList] = useState([]);
            const [channelEnvPath, setChannelEnvPath] = useState(null);
            const [channelsLoading, setChannelsLoading] = useState(false);
            const [channelsError, setChannelsError] = useState(null);
            const [openChannelId, setOpenChannelId] = useState(null);
            const [savingChannelId, setSavingChannelId] = useState(null);
            const [channelSaveErrors, setChannelSaveErrors] = useState({});
            // A save is only half the job: the gateway reads this configuration
            // at boot, so until it restarts the page would otherwise be showing
            // a promise it has not kept.
            const [restartNeeded, setRestartNeeded] = useState(false);
            const [restarting, setRestarting] = useState(false);
            const [restartDone, setRestartDone] = useState(false);

            const fetchChannels = useCallback(async () => {
                setChannelsLoading(true);
                try {
                    const res = await fetch('/api/channels');
                    const body = await res.json();
                    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
                    setChannelList(body.channels || []);
                    setChannelEnvPath(body.env_path || null);
                    setChannelsError(null);
                } catch (err) {
                    setChannelsError(String(err.message || err));
                } finally {
                    setChannelsLoading(false);
                }
            }, []);

            useEffect(() => {
                if (settingsSection === 'channels') fetchChannels();
            }, [settingsSection, fetchChannels]);

            const saveChannel = useCallback(async (id, payload) => {
                setSavingChannelId(id);
                setChannelSaveErrors(prev => ({ ...prev, [id]: null }));
                try {
                    const res = await fetch(`/api/channels/${id}`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload),
                    });
                    const body = await res.json();
                    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
                    setRestartNeeded(true);
                    setRestartDone(false);
                    await fetchChannels();
                } catch (err) {
                    setChannelSaveErrors(prev => ({ ...prev, [id]: String(err.message || err) }));
                } finally {
                    setSavingChannelId(null);
                }
            }, [fetchChannels]);

            const restartGateway = useCallback(async () => {
                setRestarting(true);
                try {
                    const res = await fetch('/api/channels/restart', { method: 'POST' });
                    const body = await res.json().catch(() => ({}));
                    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
                    setRestartDone(true);
                } catch (err) {
                    setChannelsError(String(err.message || err));
                } finally {
                    setRestarting(false);
                }
            }, []);

            // --- MCP connections ---
            // Host state like channels, and fetched the same way: when the
            // section opens, and again after every write. Not polled — it
            // changes when someone changes it, and a settings page that
            // reshuffles under a half-typed URL is worse than a stale one.
            const [mcpList, setMcpList] = useState([]);
            const [mcpAutoReload, setMcpAutoReload] = useState(true);
            const [mcpLoading, setMcpLoading] = useState(false);
            const [mcpError, setMcpError] = useState(null);
            const [openMcpName, setOpenMcpName] = useState(null);
            const [savingMcpName, setSavingMcpName] = useState(null);
            const [testingMcpName, setTestingMcpName] = useState(null);
            const [mcpTestResults, setMcpTestResults] = useState({});
            const [mcpSaveErrors, setMcpSaveErrors] = useState({});
            const [mcpAdding, setMcpAdding] = useState(false);
            const [mcpAddError, setMcpAddError] = useState(null);

            const fetchMcpServers = useCallback(async () => {
                setMcpLoading(true);
                try {
                    const res = await fetch('/api/mcp/servers');
                    const body = await res.json();
                    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
                    setMcpList(body.servers || []);
                    setMcpAutoReload(body.auto_reload !== false);
                    setMcpError(null);
                } catch (err) {
                    setMcpError(String(err.message || err));
                } finally {
                    setMcpLoading(false);
                }
            }, []);

            // --- The read-only three quarters of the same page ---
            // Workflow API access, automation output targets and email
            // identities. One fetch: they come from one snapshot of the
            // workflows service, and splitting them into three calls would let
            // the page show a credential as configured in one section and the
            // identity it belongs to as absent in another.
            const [wfIntegrations, setWfIntegrations] = useState(null);
            const [wfIntegrationsError, setWfIntegrationsError] = useState(null);

            const fetchWfIntegrations = useCallback(async () => {
                try {
                    const res = await fetch('/api/settings/integrations');
                    const body = await res.json();
                    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
                    setWfIntegrations(body);
                    setWfIntegrationsError(null);
                } catch (err) {
                    setWfIntegrationsError(String(err.message || err));
                }
            }, []);

            useEffect(() => {
                if (settingsSection !== 'integrations') return;
                fetchMcpServers();
                fetchWfIntegrations();
            }, [settingsSection, fetchMcpServers, fetchWfIntegrations]);

            // Every write goes through here: same error slot per connection,
            // same refetch afterwards. Refetching rather than patching local
            // state is deliberate — Hermes normalises what it stores (a bearer
            // token becomes a ${VAR} header, a command line is re-split), so
            // the row should show what was saved, not what was typed.
            const writeMcp = useCallback(async (name, doIt) => {
                setSavingMcpName(name);
                setMcpSaveErrors(prev => ({ ...prev, [name]: null }));
                try {
                    const res = await doIt();
                    const body = await res.json().catch(() => ({}));
                    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
                    await fetchMcpServers();
                    return true;
                } catch (err) {
                    setMcpSaveErrors(prev => ({ ...prev, [name]: String(err.message || err) }));
                    return false;
                } finally {
                    setSavingMcpName(null);
                }
            }, [fetchMcpServers]);

            const saveMcpServer = useCallback((name, payload) => writeMcp(name, () =>
                fetch(`/api/mcp/servers/${encodeURIComponent(name)}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                })), [writeMcp]);

            const toggleMcpServer = useCallback((name, enabled) => writeMcp(name, () =>
                fetch(`/api/mcp/servers/${encodeURIComponent(name)}/enabled`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled }),
                })), [writeMcp]);

            const removeMcpServer = useCallback(async (name) => {
                const ok = await writeMcp(name, () =>
                    fetch(`/api/mcp/servers/${encodeURIComponent(name)}`, { method: 'DELETE' }));
                // Nothing left to keep open, and a stale probe result for a
                // name that no longer exists would reappear against a new
                // connection that happened to reuse it.
                if (ok) {
                    setOpenMcpName(null);
                    setMcpTestResults(prev => { const next = { ...prev }; delete next[name]; return next; });
                }
            }, [writeMcp]);

            const addMcpServer = useCallback(async (payload) => {
                setMcpAdding(true);
                setMcpAddError(null);
                try {
                    const res = await fetch('/api/mcp/servers', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload),
                    });
                    const body = await res.json().catch(() => ({}));
                    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
                    await fetchMcpServers();
                    // Open the row that was just created: adding a connection
                    // is rarely the last step — it usually wants a Test.
                    setOpenMcpName(payload.name);
                    return true;
                } catch (err) {
                    setMcpAddError(String(err.message || err));
                    return false;
                } finally {
                    setMcpAdding(false);
                }
            }, [fetchMcpServers]);

            const testMcpServer = useCallback(async (name) => {
                setTestingMcpName(name);
                setMcpTestResults(prev => ({ ...prev, [name]: null }));
                try {
                    const res = await fetch(`/api/mcp/servers/${encodeURIComponent(name)}/test`, {
                        method: 'POST',
                    });
                    const body = await res.json().catch(() => ({}));
                    // A probe that could not be run at all (502) is reported in
                    // the same place a probe that failed is — from where the
                    // button was pressed, both mean "it did not connect".
                    setMcpTestResults(prev => ({
                        ...prev,
                        [name]: res.ok ? body : { ok: false, error: body.detail || `HTTP ${res.status}` },
                    }));
                } catch (err) {
                    setMcpTestResults(prev => ({ ...prev, [name]: { ok: false, error: String(err.message || err) } }));
                } finally {
                    setTestingMcpName(null);
                }
            }, []);

            // --- System-wide metrics ---
            // Everything the Metrics tab fetches, and the headlines derived
            // from it, live in useMetrics (85-metrics-view.jsx). App keeps
            // which of the two pages is showing, because that is routing.
            const metrics = useMetrics({ activeTab });

            // null = the outcomes page, 'system' = the diagnostics behind it.
            const [metricsView, setMetricsView] = useState(entryRoute.metricsView);

            // Move between the two Metrics pages. Pushes rather than replaces:
            // going to the system page is a jump between views, and Back should
            // return to the outcomes page rather than to the previous tab.
            const navigateMetricsView = useCallback((view) => {
                const want = pathForRoute({ tab: 'metrics', metricsView: view || null });
                if (window.location.pathname !== want) {
                    window.history.pushState({ tab: 'metrics', metricsView: view || null }, '', want);
                }
                setActiveTab('metrics');
                setMetricsView(view || null);
            }, []);


            // --- ADK Teams & Scorecard ---
            const [adkTeams, setAdkTeams] = useState([]);
            const [adkHealth, setAdkHealth] = useState(null);
            const [adkLoading, setAdkLoading] = useState(true);
            // Scorecards are per-app and each one reads that app's whole trace
            // history, so they are fetched lazily for the selected app only and
            // kept keyed by app rather than replaced.
            const [adkScores, setAdkScores] = useState({}); // { [app]: scorecard }
            // Headline health for every app at once, for the fleet view.
            const [adkFleet, setAdkFleet] = useState([]);
            // "app::agentName", or null for the fleet view. Null is the landing
            // state on purpose: the first thing to see is the whole roster and
            // how it is wired together, not whichever agent sorted first — unless
            // the entry URL names an agent, which is a request for that agent.
            const [activeTeamAgent, setActiveTeamAgent] = useState(entryRoute.teamAgent);

            // --- Hermes agents ---
            // The profiles this host runs, from /api/agents. They are listed in
            // their own right rather than inferred from the jobs they own: a
            // profile with nothing scheduled is still an agent on this host, and
            // deriving the list from cron would hide exactly those.
            const [hermesAgents, setHermesAgents] = useState([]);
            const [hermesLoading, setHermesLoading] = useState(true);
            // Selected Hermes profile name, or null. Mutually exclusive with
            // activeTeamAgent — the pane shows one agent, of one kind.
            const [activeHermesAgent, setActiveHermesAgent] = useState(entryRoute.hermesAgent);
            // Which job a Run now click was about. No longer a selection — a job
            // is a page now, not a thing highlighted inside another page — but
            // the trigger notice has to name which job it is about, because the
            // scorecard's Launch section can show several at once.
            const [activeCronJobId, setActiveCronJobId] = useState(null);

            // --- Chat selection ---
            // The open conversation, or null for the unsent draft. Declared up
            // here with the other addressable selections, and for the same
            // reason activeCronJobId is: the route effect's dep array names it,
            // and deps are evaluated during render. The rest of the chat state
            // lives with the shared data below.
            const [activeSessionId, setActiveSessionId] = useState(entryRoute.sessionId);

            // --- Kanban selection ---
            // The open task, seeded from the URL so a cold load of /kanban/<id>
            // opens that task rather than whatever the board auto-selects. Up here
            // with the other addressable selections for the same reason they are:
            // the route effect's dep array names it and deps are evaluated during
            // render, and — because these scripts are transpiled with a `const`
            // that becomes a hoisted `var` — reading it from below its declaration
            // does not throw, it silently reads `undefined`. That is exactly how
            // the task id went missing from the address bar. The rest of the
            // kanban state lives with the shared data below.
            const [activeKanbanTaskId, setActiveKanbanTaskId] = useState(entryRoute.taskId);

            // The open review item, seeded from the URL for the same reasons —
            // and declared here rather than with the rest of the review state
            // below because of the same hoisting trap the comment above
            // describes. This one used to be an array index, which is why an
            // item could never be linked to at all.
            const [activeReviewId, setActiveReviewId] = useState(entryRoute.reviewId);

            // WHICH automation is open, and nothing else about it. Up here with
            // the other selections for the same reason activeCronJobId is: the
            // route effect's dep array names it, and deps are evaluated during
            // render, which would hit a later declaration's dead zone.
            //
            // What the server says about that automation — its detail, its
            // executions, which one is expanded — belongs to the view and lives
            // in useAutomationDetail. The id is routing and the URL owns it; the
            // rest was four names and a polling effect that only one tab read.
            const [activeAutomationId, setActiveAutomationId] = useState(entryRoute.automationId);

            // Which memory page is open and which document it is about. Up
            // here for the same reason activeAutomationId is: the route
            // effect's dep array names both, and deps are evaluated during
            // render. The rest of the tab's state — what the server said, the
            // search box, the loading flags — stays down with the other data.
            const [memoryView, setMemoryView] = useState(entryRoute.memoryView);
            const [memorySlug, setMemorySlug] = useState(entryRoute.memorySlug);

            // Back/forward: adopt whatever the restored URL names. One URL carries
            // both the tab and the selected agent, and on the Agents tab a missing
            // agent segment is meaningful — it is the fleet view, not "unchanged".
            useEffect(() => {
                const onPopState = () => {
                    const r = routeFromLocation();
                    setHealthOpen(r.health);
                    // A /health URL says nothing about what is underneath it —
                    // not the tab, and not an open settings section. Returning
                    // here leaves both exactly as they were, so Back out of the
                    // modal puts you back where you opened it from.
                    if (r.health) return;
                    setSettingsSection(r.settings);
                    // A settings URL says nothing about the tab underneath it,
                    // and adopting its DEFAULT_TAB fallback would quietly move
                    // you off the tab you opened settings from.
                    if (r.settings) return;
                    setActiveTab(r.tab);
                    // Same for a missing session segment: /chat is the draft
                    // chat, not "leave whatever was open". The transcript
                    // follows from the effect below, so Back into a
                    // conversation loads it exactly as clicking it does.
                    if (r.tab === 'chat') setActiveSessionId(r.sessionId);
                    if (r.tab === 'kanban') setActiveKanbanTaskId(r.taskId);
                    // /metrics with no sub-path is the outcomes page, which is
                    // a state Back must be able to return to from /metrics/system.
                    if (r.tab === 'metrics') setMetricsView(r.metricsView);
                    // Same rule again: /automations with no id is the list, a
                    // state Back has to be able to return to from a detail page.
                    if (r.tab === 'automations') setActiveAutomationId(r.automationId);
                    // And again for review: /review with no id is the queue with
                    // nothing open, which is where Back out of an item lands.
                    if (r.tab === 'review') setActiveReviewId(r.reviewId);
                    // And again for memory: /memory with no view is the
                    // document list, which is where Back out of a document or
                    // the graph has to land.
                    if (r.tab === 'memory') {
                        setMemoryView(r.memoryView);
                        setMemorySlug(r.memorySlug);
                    }
                    if (r.tab === 'agents') {
                        setActiveTeamAgent(r.teamAgent);
                        setActiveHermesAgent(r.hermesAgent);
                    }
                };
                window.addEventListener('popstate', onPopState);
                return () => window.removeEventListener('popstate', onPopState);
            }, []);

            // Keep the address bar showing the view. replaceState, not pushState:
            // clicking down a roster is browsing within a tab, and pushing each
            // step would bury the previous tab under a pile of Back presses.
            // navigateTab still pushes, so Back stays per-tab.
            // Also normalizes an entry URL of "/" or an unknown path.
            useEffect(() => {
                const want = pathForRoute({
                    tab: activeTab,
                    teamAgent: activeTeamAgent,
                    hermesAgent: activeHermesAgent,
                    settings: settingsSection,
                    health: healthOpen,
                    sessionId: activeSessionId,
                    taskId: activeKanbanTaskId,
                    automationId: activeAutomationId,
                    metricsView,
                    reviewId: activeReviewId,
                    memoryView,
                    memorySlug,
                });
                if (window.location.pathname !== want) {
                    window.history.replaceState({ tab: activeTab, settings: settingsSection, health: healthOpen }, '', want);
                }
            }, [activeTab, activeTeamAgent, activeHermesAgent, settingsSection, healthOpen, activeSessionId, activeKanbanTaskId, activeAutomationId, metricsView, activeReviewId, memoryView, memorySlug]);

            // Deep-link to one agent. Used by the per-agent links in the fleet
            // view; pushes, because it is a jump between views rather than a step
            // within one.
            const navigateScorecard = useCallback((agentId) => {
                const want = pathForRoute({ tab: 'agents', teamAgent: agentId });
                if (window.location.pathname !== want) {
                    window.history.pushState({ tab: 'agents' }, '', want);
                }
                setActiveTab('agents');
                setActiveHermesAgent(null);
                setActiveTeamAgent(agentId);
            }, []);

            // The same jump, for a Hermes profile. A job id is optional: naming
            // one opens its agent with that job in view, which is what clicking a
            // job in the rail means.
            const navigateHermesAgent = useCallback((name, jobId) => {
                const want = pathForRoute({ tab: 'agents', hermesAgent: name, jobId: jobId || null });
                if (window.location.pathname !== want) {
                    window.history.pushState({ tab: 'agents' }, '', want);
                }
                setActiveTab('agents');
                setActiveTeamAgent(null);
                setActiveHermesAgent(name);
                setActiveCronJobId(jobId || null);
            }, []);

            // Open one automation. Pushes rather than replaces, because it is a
            // jump between views — and it is reached from four places now (the
            // list, the metrics ledger, a team's Launch section, an old
            // /agents/hermes/<p>/<job> link), all of which are somewhere you
            // want Back to return to.
            const navigateAutomation = useCallback((jobId) => {
                if (!jobId) return;
                const want = pathForRoute({ tab: 'automations', automationId: jobId });
                if (window.location.pathname !== want) {
                    window.history.pushState({ tab: 'automations' }, '', want);
                }
                setActiveTab('automations');
                setActiveAutomationId(jobId);
                // Detail belongs to the automation being left, not the one being
                // opened. Without this the previous job's runs are on screen
                // under the new job's name until the fetch lands.
                setAutomationDetail(null);
                setAutomationError(null);
                setOpenExecutionId(null);
                setCronRunNotice(null);
            }, []);

            // Move around inside the memory tab. Pushes, because each of these
            // is a place you go and Back out of: the list → a document → that
            // document's neighbourhood is a trail, not a set of toggles.
            //
            // Takes a key OR a slug for the same reason openWikiDocument did:
            // a wikilink target is the entity's name ([[Kestrel Underwriting]])
            // while a list row carries the filename, and the backend slugifies
            // either. The URL therefore sometimes holds a key — which is fine,
            // because it resolves to the same document and the address is
            // still stable for whoever it is sent to.
            const navigateMemory = useCallback((view, slugOrKey) => {
                const nextView = view || DEFAULT_MEMORY_VIEW;
                const nextSlug = slugOrKey || null;
                const want = pathForRoute({ tab: 'memory', memoryView: nextView, memorySlug: nextSlug });
                if (window.location.pathname !== want) {
                    window.history.pushState({ tab: 'memory' }, '', want);
                }
                setActiveTab('memory');
                setSettingsSection(null);
                setMemoryView(nextView);
                setMemorySlug(nextSlug);
            }, []);

            // --- Chat ---
            // The rail, the conversation, the composer, the context panel and
            // the reading pane all live in useChat (75-chat-view.jsx). App
            // keeps activeSessionId, because that is routing, and reads back
            // the session count, the unread badge, the API-down reason and the
            // two entry points other tabs use to send you here.
            const chat = useChat({
                activeTab, activeSessionId, setActiveSessionId,
                navigateTab, setActiveKanbanTaskId, healthOpen,
            });
            
            // --- Kanban ---
            // The board, the task pane, and the rules that keep the list,
            // the pane and the address bar naming the same task all live in
            // useKanban (80-kanban-view.jsx). App keeps activeKanbanTaskId,
            // declared above with the other selections the route effect
            // reads, because that one is routing.
            const kanban = useKanban({
                activeTab, activeKanbanTaskId, setActiveKanbanTaskId,
                navigateTab, entryRoute,
            });
            
            // --- Cron States ---
            const [cronJobs, setCronJobs] = useState([]);
            // activeCronJobId lives up with the Hermes agent state: the route
            // effect's dep array names it, and a hook's deps are evaluated during
            // render, which would hit this declaration's temporal dead zone.
            // Which job has a trigger in flight, and what the gateway said about
            // it. Keyed by nothing — only one job can be fired at a time from a
            // single panel, and the notice is cleared on selection change.
            const [cronRunning, setCronRunning] = useState(false);
            const [cronRunNotice, setCronRunNotice] = useState(null);

            // --- Wiki memory ---
            // Four pages, because they answer different questions: 'documents'
            // is "who is on file" (always loadable), 'search' is "what is known
            // about X" (needs a query, so it starts empty), 'document' is one
            // record whole, and 'graph' is how the records refer to each other.
            //
            // Which page is open, and which document it is about, are held by
            // the URL rather than by a mode toggle — and declared up with the
            // other routing state, above the effect whose dep array names
            // them. See the note on activeAutomationId for why that matters.
            const [wikiHealth, setWikiHealth] = useState(null);
            const [wikiDocuments, setWikiDocuments] = useState([]);
            const [wikiQuery, setWikiQuery] = useState('');
            const [wikiFacts, setWikiFacts] = useState([]);
            const [wikiSearched, setWikiSearched] = useState(false);
            const [wikiLoading, setWikiLoading] = useState(false);
            const [wikiError, setWikiError] = useState(null);
            const [activeWikiDoc, setActiveWikiDoc] = useState(null);
            const [wikiGraph, setWikiGraph] = useState(null);
            const [wikiGraphLoading, setWikiGraphLoading] = useState(false);
            const [wikiGraphError, setWikiGraphError] = useState(null);

            // --- Integrations ---
            // Sources, each with its grants (see /api/integrations). One payload
            // rather than one per source: a source row shows the worst status
            // among its grants, which is only meaningful if they were all read
            // at the same moment.
            const [integrations, setIntegrations] = useState(null);
            const [integrationsError, setIntegrationsError] = useState(null);
            // Which source rows are expanded. A Set, keyed by source, so
            // expansion survives a poll — the list re-renders every 30s and
            // index-based state would reopen whatever moved into that slot.
            const [openSources, setOpenSources] = useState(() => new Set());
            const [openGrants, setOpenGrants] = useState(() => new Set());
            const [gapsOpen, setGapsOpen] = useState(false);


            
            // The host-wide skills and context lists used to be their own tabs.
            // Both are now read per-profile via /api/agents/<name>/{skills,context},
            // so the global copies — and the two poll fetchers behind them — are gone.

            // The Hermes profile browser (SOUL.md, skills, memories, per-profile
            // context) used to share this tab with the ADK teams. It was a
            // different kind of thing answering a different question, and it
            // crowded out the one this view is for. Its endpoints are untouched
            // — /api/agents and /api/agents/<name>/* still serve it — so it can
            // come back as its own tab whenever it earns one.

            // --- Review queue ---
            // Queue, filter, selection, the decision posts and the keyboard
            // map all live in useReview (70-review-view.jsx). App keeps only
            // `activeReviewId`, declared up with activeKanbanTaskId, because
            // that one is routing: as an index it could not be put in a URL,
            // so an item could be read but never linked or gone Back to — and
            // it drifted whenever the poll reordered the list under it. The
            // URL-sync effect's dependency array is evaluated during render
            // and has to see it.
            const review = useReview({
                activeTab, settingsSection, healthOpen,
                activeReviewId, setActiveReviewId,
            });



            // Fetch Cron Jobs list
            const fetchCronJobs = async () => {
                try {
                    const res = await fetch('/api/cron/jobs');
                    if (res.ok) {
                        const data = await res.json();
                        setCronJobs(data);
                        // No auto-select. A job is now reached through the agent
                        // it launches; the selection only picks out an unattached
                        // job, and picking one the user did not ask for would
                        // highlight a rail row nobody clicked.
                    }
                } catch (err) {
                    console.error("Error fetching cron jobs:", err);
                }
            };

            // Fire a job off-schedule. The gateway only *arms* it — the ticker
            // runs about once a minute — so this reports "queued" and lets the
            // 7s poll surface the actual result in Last run. Claiming it ran
            // would be a lie for up to a minute.
            const runCronJobNow = async (job) => {
                if (!job || cronRunning) return;
                // The Launch panel can show several jobs at once, so the notice
                // has to name which one it is about.
                setActiveCronJobId(job.id);
                setCronRunning(true);
                setCronRunNotice(null);
                try {
                    const res = await fetch(`/api/cron/jobs/${job.id}/run`, { method: 'POST' });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) {
                        setCronRunNotice({ ok: false, text: data.detail || `Trigger failed (${res.status})` });
                    } else {
                        setCronRunNotice({
                            ok: true,
                            text: 'Queued — the gateway fires it on the next tick (within ~1 min).',
                        });
                        fetchCronJobs();
                    }
                } catch (err) {
                    setCronRunNotice({ ok: false, text: `Could not reach the dashboard API: ${err}` });
                } finally {
                    setCronRunning(false);
                }
            };

            // --- ADK Teams ---

            // Teams come from the workflows server, falling back to parsing its
            // mounted source for roots app-info cannot describe. Kept out of the
            // global 7s loop and polled only while the tab is open (below).
            const fetchAdk = async () => {
                try {
                    const [tRes, hRes, fRes] = await Promise.all([
                        fetch('/api/adk/teams'),
                        fetch('/api/adk/health'),
                        fetch('/api/adk/fleet?days=3650'),
                    ]);
                    if (tRes.ok) {
                        const data = await tRes.json();
                        const apps = data.apps || [];
                        setAdkTeams(apps);
                        // Keep a live selection across a refresh, but never create
                        // one: null means the fleet view, and a poll must not
                        // navigate the user off it. If the overlord removed the
                        // selected agent, fall back to the same position in the
                        // same app rather than dumping them at the top.
                        setActiveTeamAgent(prev => {
                            if (!prev) return null;
                            const flat = [];
                            apps.forEach(a => (a.agents || []).forEach(g => flat.push(`${a.app}::${g.name}`)));
                            if (flat.includes(prev)) return prev;
                            const [app] = prev.split('::');
                            const sameApp = flat.filter(id => id.startsWith(app + '::'));
                            return sameApp[0] || null;
                        });
                    }
                    if (hRes.ok) setAdkHealth(await hRes.json());
                    if (fRes.ok) {
                        const data = await fRes.json();
                        setAdkFleet(data.apps || []);
                    }
                } catch (err) {
                    console.error("Error fetching ADK teams:", err);
                } finally {
                    setAdkLoading(false);
                }
            };

            // --- Hermes agents ---
            // The profile roster. Cheap and static enough to ride the Agents tab
            // poll alongside the teams: it is a directory listing plus a config
            // read per profile, not a trace scan.
            const fetchHermesAgents = async () => {
                try {
                    const res = await fetch('/api/agents');
                    if (res.ok) setHermesAgents(await res.json());
                } catch (err) {
                    console.error("Error fetching Hermes agents:", err);
                } finally {
                    setHermesLoading(false);
                }
            };


            // One app's full scorecard. Separate from fetchAdk because it reads
            // that app's entire trace history: worth paying for the app being
            // looked at, not for all of them on every poll.
            const fetchScorecard = async (appName) => {
                if (!appName) return;
                try {
                    const res = await fetch(
                        `/api/adk/scorecard?app=${encodeURIComponent(appName)}&days=3650`);
                    if (res.ok) {
                        const data = await res.json();
                        setAdkScores(prev => ({ ...prev, [appName]: data }));
                    }
                } catch (err) {
                    console.error("Error fetching scorecard:", err);
                }
            };

            // Every service in the stack, red/amber/green. The backend caches a
            // sweep for a few seconds, so polling this costs one round trip
            // rather than a dozen probes; `fresh` bypasses that cache, which is
            // what the modal's Refresh sends after someone has restarted
            // something and is watching for it to come back.
            //
            // A failure here is left in healthError rather than clearing
            // `health`: the last known roster is more use than an empty modal,
            // and this endpoint failing usually means this dashboard is the
            // thing restarting.
            const fetchHealth = useCallback(async (fresh = false) => {
                if (fresh) setHealthRefreshing(true);
                try {
                    const res = await fetch(`/api/health/services${fresh ? '?fresh=1' : ''}`);
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    setHealth(await res.json());
                    setHealthError(null);
                } catch (err) {
                    setHealthError(err.message);
                } finally {
                    if (fresh) setHealthRefreshing(false);
                }
            }, []);

            // Fetch the review queue.
            //
            // The index-preservation dance that used to live here is gone with
            // the index: selection is an id, so a reordered list re-finds it for
            // free and there is nothing to re-anchor.
            // Initial load & Polling Loop
            useEffect(() => {
                fetchCronJobs();

                // Polling for updates every 7 seconds to keep dashboard fresh
                const interval = setInterval(() => {
                    fetchCronJobs();
                }, 7000);

                return () => clearInterval(interval);
            }, []);

            // Stack health, on its own clock and never gated on the active tab:
            // the header light has to be right whichever screen you are on, and
            // it is the one indicator whose whole job is to be true while you
            // are looking somewhere else. Faster while the modal is open, where
            // someone is watching a service come back and the age counter is
            // ticking in front of them.
            useEffect(() => {
                fetchHealth();
                const interval = setInterval(fetchHealth, healthOpen ? 5000 : 15000);
                return () => clearInterval(interval);
            }, [fetchHealth, healthOpen]);

            // Esc closes the health modal, the same as the X. Bound only while
            // it is open, so it never competes with the approvals shortcuts.
            useEffect(() => {
                if (!healthOpen) return;
                const onKey = (e) => { if (e.key === 'Escape') closeHealth(); };
                document.addEventListener('keydown', onKey);
                return () => document.removeEventListener('keydown', onKey);
            }, [healthOpen, closeHealth]);



            // ADK teams poll on their own clock, and only while the tab is open.
            // The shared loop above already fires nine fetchers on every tick
            // regardless of the active tab; this view re-parses source files, so
            // adding it there would make every open browser tab pay for it.
            //
            // Automations needs the team list too, for the step flow an expanded
            // workflow row draws — without it adkTeams is empty there and the
            // flow silently renders nothing, which looks like "this workflow has
            // no steps" rather than "the data never arrived". Slower there: that
            // page is watching schedules, and agent structure changes on deploys,
            // not on the 5s beat the roster wants for drift.
            useEffect(() => {
                if (activeTab !== 'agents' && activeTab !== 'automations') return;
                const tick = () => { fetchAdk(); fetchHermesAgents(); };
                tick();
                const interval = setInterval(tick, activeTab === 'agents' ? 5000 : 30000);
                return () => clearInterval(interval);
            }, [activeTab]);

            // The selected agent's app scorecard, refetched when the selection
            // moves to a different app. Moving between agents of the same app
            // costs nothing: one scorecard covers the whole team.
            const activeApp = activeTeamAgent ? activeTeamAgent.split('::')[0] : null;
            useEffect(() => {
                if (activeTab !== 'agents' || !activeApp) return;
                fetchScorecard(activeApp);
                const interval = setInterval(() => fetchScorecard(activeApp), 15000);
                return () => clearInterval(interval);
            }, [activeTab, activeApp]);


            // Memory loads on tab open, not on the shared poll loop. It is a
            // directory read rather than a proxied call now, but the reason is
            // unchanged: the wiki only changes when a workflow refreshes a
            // contact, so polling it every 7s would be pure waste.
            const fetchWikiDocuments = async () => {
                setWikiLoading(true);
                setWikiError(null);
                try {
                    const res = await fetch('/api/wiki/documents?limit=200');
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                    setWikiDocuments(data.documents || []);
                } catch (e) {
                    setWikiError(String(e.message || e));
                    setWikiDocuments([]);
                } finally {
                    setWikiLoading(false);
                }
            };

            const runWikiSearch = async () => {
                if (!wikiQuery.trim()) return;
                setWikiLoading(true);
                setWikiError(null);
                setWikiSearched(true);
                try {
                    const res = await fetch('/api/wiki/search', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ query: wikiQuery, limit: 25 }),
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                    setWikiFacts(data.facts || []);
                } catch (e) {
                    setWikiError(String(e.message || e));
                    setWikiFacts([]);
                } finally {
                    setWikiLoading(false);
                }
            };

            // Opening a document is a navigation, not a fetch. The URL is what
            // decides which document is on screen, and the effect below is
            // what loads it — so arriving by click, by Back, and by pasted
            // link all take the same path, instead of the first one working
            // and the other two showing an empty page.
            const openWikiDocument = (slugOrKey) => navigateMemory('document', slugOrKey);

            // The document named by the URL. Refetched when it changes, and
            // cleared first so the previous document's facts are never on
            // screen under the new one's name while the request is in flight.
            useEffect(() => {
                if (activeTab !== 'memory' || memoryView !== 'document' || !memorySlug) return;
                let cancelled = false;
                setWikiLoading(true);
                setWikiError(null);
                setActiveWikiDoc(null);
                fetch(`/api/wiki/document/${encodeURIComponent(memorySlug)}`)
                    .then(async res => {
                        const data = await res.json();
                        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                        return data;
                    })
                    .then(data => { if (!cancelled) setActiveWikiDoc(data); })
                    .catch(e => { if (!cancelled) { setWikiError(String(e.message || e)); setActiveWikiDoc(null); } })
                    .finally(() => { if (!cancelled) setWikiLoading(false); });
                return () => { cancelled = true; };
            }, [activeTab, memoryView, memorySlug]);

            // The graph, whole or centred on one document. Same shape as the
            // document fetch and for the same reason — /memory/graph/<slug> is
            // an address, so it has to load from the URL rather than from
            // whatever click happened to produce it.
            useEffect(() => {
                if (activeTab !== 'memory' || memoryView !== 'graph') return;
                let cancelled = false;
                setWikiGraphLoading(true);
                setWikiGraphError(null);
                const query = memorySlug ? `?slug=${encodeURIComponent(memorySlug)}&depth=1` : '';
                fetch(`/api/wiki/graph${query}`)
                    .then(async res => {
                        const data = await res.json();
                        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                        return data;
                    })
                    .then(data => { if (!cancelled) setWikiGraph(data); })
                    .catch(e => { if (!cancelled) { setWikiGraphError(String(e.message || e)); setWikiGraph(null); } })
                    .finally(() => { if (!cancelled) setWikiGraphLoading(false); });
                return () => { cancelled = true; };
            }, [activeTab, memoryView, memorySlug]);

            // Integrations poll while the tab is open, unlike the config-derived
            // views that load once. This screen reports *last used*, so a
            // stationary "12m ago" that is really an hour old is a wrong answer
            // rather than a stale one. 30s, matching the rest of the app; the
            // poll stops when the tab does.
            //
            // Automations polls it too, and has to: now that Integrations has no
            // nav button, its failed-grant badge lives there, and a badge fed by
            // data only fetched on the screen it links to would read zero until
            // you had already gone and looked. Same 30s clock, same call — the
            // dashboard is host-networked against a local API, so the second
            // reader is cheap.
            useEffect(() => {
                if (activeTab !== 'integrations' && activeTab !== 'automations') return;
                let cancelled = false;
                const load = () => fetch('/api/integrations')
                    .then(r => r.json().then(d => {
                        if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
                        return d;
                    }))
                    .then(d => { if (!cancelled) { setIntegrations(d); setIntegrationsError(null); } })
                    .catch(e => { if (!cancelled) { setIntegrationsError(String(e.message || e)); setIntegrations(null); } });
                load();
                const timer = setInterval(load, 30000);
                return () => { cancelled = true; clearInterval(timer); };
            }, [activeTab]);

            // Memory loads on tab open, not on the shared poll loop: the wiki
            // only changes when a workflow refreshes a contact, so polling it
            // every 7s would be pure waste.
            useEffect(() => {
                if (activeTab !== 'memory') return;
                fetch('/api/wiki/health')
                    .then(r => r.json()).then(setWikiHealth)
                    .catch(() => setWikiHealth({ status: 'unavailable' }));
                if (memoryView === 'documents') fetchWikiDocuments();
            }, [activeTab, memoryView]);


            // Paint icons into the React-owned <i> elements (see renderIcons).
            // Cleared on re-run: a streaming reply re-renders on every token, and
            // an uncancelled timer per token would queue hundreds of
            // full-document icon sweeps.
            useEffect(() => {
                const paint = setTimeout(renderIcons, 50);
                return () => clearTimeout(paint);
            // `now` is in here because staleness is time-derived: a job can tip
            // from ok to stale on a clock tick with no new data, and that swaps
            // the status icon.
            // `health` and `healthOpen` are in here because the indicator swaps
            // between an icon button and a labelled alert as the stack's state
            // changes — a new <i> that no other state change would repaint.
            // `kanbanArchiving` and `kanbanArchiveError` are in here for the same
            // reason: the archive button swaps between the archive and
            // archive-restore glyphs as the task moves, and a refusal mounts a
            // triangle-alert that nothing else on the page would paint.
            // The three automation-detail states are in here because that page
            // is built almost entirely out of <i> glyphs that nothing else
            // mounts — the status mark, the flow nodes, one chevron per
            // execution — and an unrepainted page shows every one of them as
            // empty space where an icon should be.
            // `metricsView` is in here for exactly that reason: switching
            // between the Dashboard and its system page swaps one link for the
            // other, and each carries an arrow that nothing else would paint.
            // `activeTab` does not cover it — the tab never changes.
            }, [chat.messages, chat.chatSending, activeTab, metricsView, activeReviewId, review.reviewQueue, review.reviewAttention, review.reviewTypeFilter, activeCronJobId, activeKanbanTaskId, kanban.kanbanDetail, kanban.kanbanArchiving, kanban.kanbanArchiveError, activeTeamAgent, adkTeams, cronJobs, now, chat.ctxOpen, chat.ctxFiles, chat.ctxDoc, chat.ctxDocBody, integrations, openSources, openGrants, gapsOpen, chat.connOpen, chat.chatGrants, settingsSection, channelList, openChannelId, restartNeeded, mcpList, openMcpName, wfIntegrations, health, healthOpen, activeAutomationId]);



            // Esc closes settings, the same as the X. Bound only while it is
            // open, so it never competes with the approvals shortcuts.
            //
            // Stands down while the health modal is up. Esc has to close the
            // topmost thing and only the topmost thing: both handlers listen on
            // the document, so without this one press closed health *and* the
            // settings page underneath it, dumping the reader on the default
            // tab having asked to dismiss one overlay.
            useEffect(() => {
                if (!settingsSection || healthOpen) return;
                const onKey = (e) => { if (e.key === 'Escape') closeSettings(); };
                document.addEventListener('keydown', onKey);
                return () => document.removeEventListener('keydown', onKey);
            }, [settingsSection, healthOpen, closeSettings]);


            // --- Automations ---
            // Ages are rendered relative, so they go stale on screen even when
            // the data behind them is fresh. The 7s job poll already re-renders
            // this view; this clock only matters for a tab left open, so 30s is
            // plenty and keeps the whole page off a 1s render loop.
            const [now, setNow] = useState(() => Date.now());
            useEffect(() => {
                const t = setInterval(() => setNow(Date.now()), 30000);
                return () => clearInterval(t);
            }, []);

            // One flat list, ordered by how much it wants your attention:
            // broken first, then merely failing, then healthy, then switched
            // off. Sorting by name inside each band keeps the order stable
            // between polls instead of shuffling on every tick.
            const STATUS_RANK = { stale: 0, error: 1, never: 2, ok: 3, off: 4 };
            const rankedAutomations = cronJobs
                .map(job => ({ job, where: automationWhere(job), status: automationStatus(job, now) }))
                .sort((a, b) => {
                    const d = STATUS_RANK[a.status.kind] - STATUS_RANK[b.status.kind];
                    if (d !== 0) return d;
                    return String(a.job.name || a.job.id).localeCompare(String(b.job.name || b.job.id));
                });

            // The ranking above is computed from live state, so leaving it in
            // charge of the rendered order meant the list moved under the
            // reader: a ten-minute job that drifts past its stale grace climbs
            // from the bottom of the healthy band to the top of the table on a
            // 7s poll, and drops back when the run lands. The rank is still the
            // right order to *arrive* at — it is a bad order to keep re-deciding
            // while someone is reading, or reaching for a row.
            //
            // So the rank is taken once, when the view is entered, and held.
            // Status keeps updating in place — a row that goes stale says so in
            // its Status cell, it just says it where it already was. The order
            // is re-decided when the view is entered again: leaving the tab (or
            // reloading) drops the freeze, and coming back ranks from scratch.
            // Opening one automation and coming back does not, deliberately —
            // that is a drill-in and a return to the same list, and re-ranking
            // there would move the row you just came from.
            //
            // The reset lives in render rather than an effect because an effect
            // runs after the render that needs the answer: clearing there would
            // leave one frame ordered by a freeze taken on some other tab.
            const automationOrderRef = useRef(null);
            if (activeTab !== 'automations') {
                automationOrderRef.current = null;
            } else if (automationOrderRef.current === null) {
                automationOrderRef.current = rankedAutomations.map(a => a.job.id);
            } else {
                // A job scheduled after the freeze has no place in it. Appending
                // in rank order puts it at the end rather than dropping it, which
                // is the one thing the order must never do — and it is where a
                // reader looks for "what just appeared" anyway.
                const frozen = automationOrderRef.current;
                for (const a of rankedAutomations) {
                    if (!frozen.includes(a.job.id)) frozen.push(a.job.id);
                }
            }
            const automations = (() => {
                const frozen = automationOrderRef.current;
                if (!frozen) return rankedAutomations;
                const at = new Map(frozen.map((id, i) => [id, i]));
                // Deleted jobs simply fall out: they are absent from cronJobs, so
                // nothing indexes their frozen slot. The stale ids stay in the
                // array harmlessly until the next freeze.
                return rankedAutomations.slice().sort(
                    (a, b) => (at.get(a.job.id) ?? 0) - (at.get(b.job.id) ?? 0));
            })();
            const staleCount = automations.filter(
                a => a.status.kind === 'stale' || a.status.kind === 'error').length;
            const hasDrift = Object.values(adkScores).some(
                s => s && s.drift && s.drift.changed_since_last_run === true);

            // Only 'failed' counts. The other unhealthy-looking states are not
            // failures: 'never' is an unexercised grant, and 'unverified' means
            // the gateway saw a call but not how it ended — badging either would
            // put a permanent red number on the button and train you past it.
            const failedGrants = ((integrations && integrations.sources) || []).reduce(
                (n, src) => n + ((src.grants || []).filter(g => g.status === 'failed').length),
                0);

            // --- The join this whole view is built on ---
            // Cron jobs used to be grouped by the profile that owns the jobs.json
            // they came from, which said nothing about what they launch. Group
            // them by the ADK app instead: that is the association worth seeing.
            // Jobs that launch no app keep a group of their own rather than
            // vanishing — a backup script is still a scheduled thing on this host.
            const jobsForApp = (appId) => cronJobs.filter(j => jobMatchesApp(j, appId));
            const unattachedJobs = cronJobs.filter(
                j => !adkTeams.some(t => jobMatchesApp(j, t.app)));

            // "Unattached" was only ever true of ADK: every job is read out of some
            // Hermes profile's jobs.json, so it always has an owner — the profile
            // whose scheduler runs it. One group per Hermes agent, default first.
            //
            // The roster drives this, not the jobs: a profile that schedules
            // nothing is still an agent on this host, and grouping jobs alone
            // would show only the profiles that happen to run cron. A job whose
            // owner is not in the roster still gets a group, so nothing on the
            // host can be scheduled and invisible at the same time.
            const hermesJobsFor = (name) => unattachedJobs.filter(
                j => (j.agent || 'default') === name);
            const hermesGroups = (() => {
                const rows = hermesAgents.map(a => ({
                    agent: a.name,
                    isDefault: !!a.is_default,
                    summary: a,
                    jobs: hermesJobsFor(a.name),
                }));
                const known = new Set(rows.map(r => r.agent));
                for (const job of unattachedJobs) {
                    const owner = job.agent || 'default';
                    if (known.has(owner)) continue;
                    known.add(owner);
                    rows.push({
                        agent: owner,
                        isDefault: !!job.agent_is_default,
                        summary: null,
                        jobs: hermesJobsFor(owner),
                    });
                }
                return rows.sort((a, b) =>
                    (a.isDefault === b.isDefault)
                        ? a.agent.localeCompare(b.agent)
                        : (a.isDefault ? -1 : 1));
            })();

            // Selected Hermes profile, resolved against the roster. A name from
            // the URL that no profile answers to still selects: the group falls
            // back to whatever the cron jobs say about it.
            const activeHermes = activeHermesAgent
                ? hermesGroups.find(g => g.agent === activeHermesAgent) || null
                : null;

            // The root of a team is the node nothing else lists as a child. A
            // cron job launches the app, which means it launches that node; the
            // rest of the team runs because the root delegated to it.
            const rootAgentName = (team) => {
                const agents = team.agents || [];
                if (team.root && agents.some(a => a.name === team.root)) return team.root;
                const parented = agents.find(a => !a.parent);
                return parented ? parented.name : (agents[0] || {}).name;
            };

            // Where an expanded automation's "runs on" link should go. It follows
            // the Where column rather than picking its own target: the column
            // named a thing, and the link has to open that thing or the row is
            // telling you two different stories.
            //
            // A workflow row points at the team's scorecard, which needs the root
            // agent — a scorecard addresses one agent, and the root is the one the
            // job actually launches. If the team list has not loaded yet, or the
            // app name resolves to no known team, fall back to the owning profile
            // rather than writing a scorecard URL for an agent we cannot name.
            const automationAgentLink = (job, where) => {
                if (where.label === 'workflow') {
                    const team = adkTeams.find(t => jobMatchesApp(job, t.app));
                    const root = team && rootAgentName(team);
                    if (team && root) {
                        return { kind: 'scorecard', label: tail(team.app),
                                 onClick: () => navigateScorecard(`${team.app}::${root}`) };
                    }
                }
                if (!job.agent) return null;
                // The owner, and only the owner. This link is drawn on the
                // automation's own page, so "open this job" would go where the
                // reader already is — what it can still usefully offer is the
                // profile whose scheduler runs it.
                // `kind` rather than reading the Where column at the render site:
                // a workflow row whose team has not loaded falls through to here,
                // and a caller keying its wording on "this is a workflow" would
                // offer a scorecard while linking to a profile.
                return { kind: 'profile', label: job.agent,
                         onClick: () => navigateHermesAgent(job.agent, null) };
            };

            // Selection, resolved against the current team list.
            const activeTeam = activeTeamAgent
                ? adkTeams.find(t => t.app === activeTeamAgent.split('::')[0]) || null
                : null;
            const activeAdkAgent = activeTeam
                ? (activeTeam.agents || []).find(
                      g => `${activeTeam.app}::${g.name}` === activeTeamAgent) || null
                : null;


            // Extend strip strings for lowercase compare
            String.prototype.lower = function() {
                return this.toLowerCase();
            };

            // Land here when the install is not finished, or when asked for by
            // route. Placed above every other return so a half-configured box
            // cannot present a console that looks ready.
            if (activeTab === 'setup' || (setupState && !setupState.configured && !setupDismissed)) {
                return <SetupView onContinue={() => { dismissSetup(); setActiveTab('metrics'); }} />;
            }

            return (
                <div class="flex h-full overflow-hidden">
                    
                    {/* ADAPTIVE LEFT SIDEBAR */}
                    <aside class="w-80 bg-[#181825] border-r border-[#313244] flex flex-col h-full shrink-0">
                        
                        {/* Sidebar Header */}
                        <div class="p-4 border-b border-[#313244] space-y-3">
                            <div class="flex items-center justify-between">
                                <h1 class="text-xl font-bold text-[#b4befe] flex items-center gap-2">
                                    <i data-lucide="bot" class="w-6 h-6 text-[#b4befe]"></i>
                                    AI Steward
                                </h1>
                            </div>
                            
                            {/* DYNAMIC SEARCH BAR */}
                            {activeTab === 'chat' && (
                                <ChatSidebarSearch chat={chat} />
                            )}

                            {activeTab === 'kanban' && (
                                <KanbanSidebarSearch kanban={kanban} />
                            )}

                            {activeTab === 'chat' && (
                                <ChatNewButton chat={chat} />
                            )}
                            {activeTab === 'review' && (
                                <ReviewSidebarControls review={review} />
                            )}
                            
                            {/* IF KANBAN: Display status filters */}
                            {activeTab === 'kanban' && (
                                <KanbanSidebarFilters kanban={kanban} />
                            )}
                        </div>

                        {/* Sidebar Content (Dynamic list) */}
                        <div class="flex-1 overflow-y-auto p-3 space-y-2">
                            
                            {/* IF METRICS ACTIVE: Quick health glance */}
                            {activeTab === 'metrics' && (
                                <>
                                    <h2 class="text-xs font-semibold uppercase tracking-wider text-[#585b70] px-3 mb-2">At a Glance</h2>
                                    <div class="px-3 space-y-2">
                                        <div class="flex items-center justify-between text-xs">
                                            <span class="text-[#a6adc8]">Needs review</span>
                                            <span class="font-mono text-[#cdd6f4]">{review.reviewQueue.length + review.reviewAttention.length}</span>
                                        </div>
                                        <div class="flex items-center justify-between text-xs">
                                            <span class="text-[#a6adc8]">Kanban tasks</span>
                                            <span class="font-mono text-[#cdd6f4]">{kanban.kanbanTasks.length}</span>
                                        </div>
                                        <div class="flex items-center justify-between text-xs">
                                            <span class="text-[#a6adc8]">Cron jobs</span>
                                            <span class="font-mono text-[#cdd6f4]">{cronJobs.length}</span>
                                        </div>
                                        <div class="flex items-center justify-between text-xs">
                                            <span class="text-[#a6adc8]">Chat sessions</span>
                                            <span class="font-mono text-[#cdd6f4]">{chat.sessions.length}</span>
                                        </div>
                                    </div>
                                </>
                            )}

                            {/* IF AUTOMATIONS ACTIVE: the same list cut two ways —
                                by how it is doing, and by what runs it. The second
                                is deliberately a summary and not a filter: splitting
                                the table by "where" is exactly the sort-by-what-it-is
                                that having a column instead of sections avoids. */}
                            {activeTab === 'automations' && (
                                <>
                                    <h2 class="text-xs font-semibold uppercase tracking-wider text-[#585b70] px-3 mb-2">Status</h2>
                                    <div class="px-3 space-y-2 mb-5">
                                        {['stale', 'error', 'ok', 'never', 'off'].map(kind => {
                                            const n = automations.filter(a => a.status.kind === kind).length;
                                            if (!n) return null;
                                            const style = STATUS_STYLE[kind];
                                            return (
                                                <div key={kind} class="flex items-center justify-between text-xs">
                                                    <span class={`flex items-center gap-1.5 ${style.color}`}>
                                                        <i data-lucide={style.icon} class="w-3.5 h-3.5"></i>
                                                        {kind === 'never' ? 'never run' : kind}
                                                    </span>
                                                    <span class="font-mono text-[#cdd6f4]">{n}</span>
                                                </div>
                                            );
                                        })}
                                    </div>

                                    <h2 class="text-xs font-semibold uppercase tracking-wider text-[#585b70] px-3 mb-2">Where they run</h2>
                                    <div class="px-3 space-y-2">
                                        {Object.entries(
                                            automations.reduce((acc, a) => {
                                                acc[a.where.label] = (acc[a.where.label] || 0) + 1;
                                                return acc;
                                            }, {})
                                        ).sort((a, b) => b[1] - a[1]).map(([label, n]) => (
                                            <div key={label} class="flex items-center justify-between text-xs">
                                                <span class="flex items-center gap-1.5 text-[#a6adc8]">
                                                    <i
                                                        data-lucide={whereKind(label).icon}
                                                        class="w-3.5 h-3.5"
                                                        style={{ color: whereKind(label).color }}
                                                    ></i>
                                                    {label}
                                                </span>
                                                <span class="font-mono text-[#cdd6f4]">{n}</span>
                                            </div>
                                        ))}
                                    </div>
                                </>
                            )}

                            {/* IF AGENTS ACTIVE: the Hermes profiles that ship as
                                system defaults, then the ADK teams under them. */}
                            {activeTab === 'agents' && (
                                <>
                                    {/* Clearing the selection is how you get back to the
                                        fleet view, so it needs to be a visible control and
                                        not just "deselect somehow". */}
                                    <button
                                        onClick={() => {
                                            setActiveTeamAgent(null);
                                            setActiveHermesAgent(null);
                                            setActiveCronJobId(null);
                                        }}
                                        class={`w-full text-left px-3 py-2 mb-3 rounded-lg flex items-center gap-2 text-sm font-semibold transition ${
                                            activeTeamAgent === null && activeHermesAgent === null
                                                ? 'bg-[#b4befe] text-[#11111b]'
                                                : 'text-[#a6adc8] hover:bg-[#313244]'
                                        }`}
                                    >
                                        <i data-lucide="layout-grid" class="w-4 h-4 shrink-0"></i>
                                        Fleet
                                        <span class={`ml-auto text-[10px] font-mono ${
                                            activeTeamAgent === null && activeHermesAgent === null
                                                ? 'text-[#1e1e2e]' : 'text-[#585b70]'
                                        }`}>
                                            {adkTeams.length} team{adkTeams.length === 1 ? '' : 's'}
                                            {hermesGroups.length ? ` · ${hermesGroups.length} hermes` : ''}
                                        </span>
                                    </button>

                                    {/* One card for all of ADK, one row per team, and the
                                        row is the team's parent. The roster used to be
                                        listed here in full, which made a sub-agent look
                                        like a sibling of the thing that owns it — and
                                        buried the teams under their own internals. The
                                        members are still one click away, in the fleet
                                        pane, laid out in the order they run. */}
                                    <TeamCard
                                        title="Workflow Agentic Teams"
                                        subtitle={adkTeams.length
                                            ? `${adkTeams.length} team${adkTeams.length === 1 ? '' : 's'} on this host`
                                            : 'nothing loaded'}
                                    >
                                        {adkLoading && adkTeams.length === 0 ? (
                                            <div class="text-center py-4 text-sm text-[#585b70]">Loading teams...</div>
                                        ) : adkTeams.length === 0 ? (
                                            <div class="text-center py-4 text-sm text-[#585b70]">No ADK teams found.</div>
                                        ) : (
                                            adkTeams.map(team => {
                                                const roster = team.agents || [];
                                                const rootName = rootAgentName(team);
                                                const root = roster.find(g => g.name === rootName)
                                                    || roster.find(g => g.depth === 0);
                                                const steps = roster.filter(g => g.depth > 0).length;
                                                const jobs = jobsForApp(team.app);
                                                const id = `${team.app}::${rootName}`;
                                                // The whole team stays lit while any of its
                                                // members is selected — a child opened from the
                                                // fleet pane still belongs to this row.
                                                const selected = !!activeTeamAgent
                                                    && activeTeamAgent.split('::')[0] === team.app;
                                                return (
                                                    <AgentRow
                                                        key={team.app}
                                                        label={rootName || team.app}
                                                        subMono={team.app}
                                                        tag={team.source === 'live' ? 'live' : 'source'}
                                                        tagTitle={team.source === 'live'
                                                            ? 'Read live from the running ADK server — reflects what is loaded, not what is on disk'
                                                            : 'Parsed from agent.py on disk — reflects edits before the restart that loads them'}
                                                        warn={team.status !== 'ok'
                                                            ? (team.source === 'live' ? 'unreachable' : 'parse error')
                                                            : null}
                                                        meta={[
                                                            root && root.agent_class,
                                                            root && root.model,
                                                            `${steps || roster.length} agent${(steps || roster.length) === 1 ? '' : 's'}`,
                                                            jobs.length
                                                                ? `runs ${formatSchedule(jobs[0])}`
                                                                : 'no schedule',
                                                        ].filter(Boolean).join(' · ')}
                                                        active={selected}
                                                        onClick={() => {
                                                            setActiveHermesAgent(null);
                                                            setActiveTeamAgent(id);
                                                        }}
                                                    />
                                                );
                                            })
                                        )}
                                    </TeamCard>

                                    {/* One card per Hermes agent: the agent itself on the
                                        first row, its schedule under it. A Hermes profile
                                        is an agent on this host in the same sense a team
                                        is, so it gets a row you can open and an address
                                        you can send someone — its jobs used to be
                                        selectable but led nowhere. */}
                                    {hermesGroups.map(group => (
                                        <TeamCard
                                            key={group.agent}
                                            title={`Hermes · ${group.agent}`}
                                            subtitle={[
                                                group.isDefault ? 'default profile' : 'Hermes profile',
                                                group.jobs.length
                                                    ? `${group.jobs.length} job${group.jobs.length === 1 ? '' : 's'}`
                                                    : 'nothing scheduled',
                                            ].join(' · ')}
                                        >
                                            <AgentRow
                                                label={group.agent}
                                                model={group.summary && group.summary.model}
                                                locked={group.isDefault}
                                                meta={group.summary
                                                    ? [
                                                        group.summary.provider,
                                                        `${group.summary.skills_available} of ${group.summary.skills_total} skills`,
                                                        (group.summary.mcp_servers || []).length
                                                            ? `${(group.summary.mcp_servers || []).length} MCP`
                                                            : null,
                                                      ].filter(Boolean).join(' · ')
                                                    : 'known only from its cron jobs'}
                                                active={activeHermesAgent === group.agent}
                                                onClick={() => navigateHermesAgent(group.agent, null)}
                                            />
                                            {group.jobs.map(job => (
                                                <button
                                                    key={job.id}
                                                    onClick={() => navigateAutomation(job.id)}
                                                    style={{ marginLeft: '14px' }}
                                                    class={`w-full text-left px-2.5 py-2 rounded-lg flex flex-col gap-0.5 transition ${
                                                        activeAutomationId === job.id
                                                            ? 'bg-[#b4befe] text-[#11111b] font-medium'
                                                            : 'hover:bg-[#313244] text-[#a6adc8]'
                                                    }`}
                                                >
                                                    <span class="text-sm font-semibold flex items-center gap-1.5 min-w-0">
                                                        <i data-lucide="clock" class="w-3.5 h-3.5 shrink-0"></i>
                                                        <span class="truncate">{job.name || job.id}</span>
                                                        {job.enabled === false && (
                                                            <span class="text-[9px] uppercase font-bold px-1 rounded bg-[#585b70]/30 text-[#585b70] shrink-0 ml-auto">off</span>
                                                        )}
                                                        {job.state === 'paused' && (
                                                            <span class="text-[9px] uppercase font-bold px-1 rounded bg-[#f9e2af]/20 text-[#f9e2af] shrink-0 ml-auto">paused</span>
                                                        )}
                                                    </span>
                                                    <span class={`text-[10px] font-mono truncate ${
                                                        activeAutomationId === job.id ? 'text-[#1e1e2e]' : 'text-[#585b70]'
                                                    }`}>
                                                        {formatSchedule(job)}
                                                    </span>
                                                </button>
                                            ))}
                                        </TeamCard>
                                    ))}
                                </>
                            )}

                            {/* IF CHAT ACTIVE: List Chat Sessions */}
                            {activeTab === 'chat' && (
                                <ChatSessionRail
                                    chat={chat}
                                    kanban={kanban}
                                    activeSessionId={activeSessionId}
                                    navigateTab={navigateTab}
                                    setActiveKanbanTaskId={setActiveKanbanTaskId}
                                />
                            )}

                            {/* IF KANBAN ACTIVE: List Tasks with Status Badges */}
                            {activeTab === 'kanban' && (
                                <KanbanSidebarList
                                    kanban={kanban}
                                    activeKanbanTaskId={activeKanbanTaskId}
                                    setActiveKanbanTaskId={setActiveKanbanTaskId}
                                />
                            )}

                            {activeTab === 'review' && (
                                <ReviewSidebarList
                                    review={review}
                                    activeReviewId={activeReviewId}
                                    setActiveReviewId={setActiveReviewId}
                                />
                            )}

                        </div>

                        {/* CHAT CONFIGURATION: what this conversation is
                            made of — the markdown Hermes reads as itself,
                            and what it can reach outside itself.

                            Pegged to the bottom of the rail rather than
                            listed with the sessions above it, because it is
                            not a session. The list is what you are choosing
                            between; this is the setup every one of those
                            choices runs under, and it holds still while you
                            scroll them. Capped at half the rail so an open
                            accordion cannot squeeze the list it configures
                            down to nothing. */}
                        {activeTab === 'chat' && (
                            <ChatConfigPanel
                                chat={chat}
                                integrations={integrations}
                                navigateTab={navigateTab}
                                openSettings={openSettings}
                            />
                        )}
                    </aside>

                    {/* MAIN VIEWPORT PANEL */}
                    <main class="flex-1 flex flex-col h-full bg-[#11111b] overflow-hidden">
                        
                        {/* HEADER TAB NAVIGATION */}
                        <header class="h-16 border-b border-[#313244] px-6 flex items-center justify-between shrink-0">
                            <div class="flex gap-4">
                                <button
                                    onClick={() => navigateTab('metrics')}
                                    class={`py-2 px-4 rounded-lg font-medium text-sm flex items-center gap-2 transition ${
                                        activeTab === 'metrics'
                                            ? 'bg-[#313244] text-[#b4befe]'
                                            : 'text-[#a6adc8] hover:text-[#cdd6f4]'
                                    }`}
                                >
                                    <i data-lucide="layout-dashboard" class="w-4 h-4"></i>
                                    Dashboard
                                </button>
                                <button
                                    onClick={() => navigateTab('chat')}
                                    class={`py-2 px-4 rounded-lg font-medium text-sm flex items-center gap-2 transition ${
                                        activeTab === 'chat' 
                                            ? 'bg-[#313244] text-[#b4befe]' 
                                            : 'text-[#a6adc8] hover:text-[#cdd6f4]'
                                    }`}
                                >
                                    <i data-lucide="message-square" class="w-4 h-4"></i>
                                    Chat
                                    {/* Conversations with something new in them, not
                                        messages: a cron run that says twenty things is
                                        still one thing to go and look at. */}
                                    <TabBadge
                                        count={chat.unreadSessionCount}
                                        tone="bg-[#b4befe] text-[#11111b]"
                                        title={`${chat.unreadSessionCount} conversation${chat.unreadSessionCount === 1 ? '' : 's'} with unread replies`}
                                    />
                                </button>
                                {/* The backlog is not a permanent fixture: when
                                    every task is done there is nothing to show,
                                    so the tab itself goes away. */}
                                {kanban.hasBacklog && (
                                    <button
                                        onClick={() => navigateTab('kanban')}
                                        class={`py-2 px-4 rounded-lg font-medium text-sm flex items-center gap-2 transition ${
                                            activeTab === 'kanban'
                                                ? 'bg-[#313244] text-[#b4befe]'
                                                : 'text-[#a6adc8] hover:text-[#cdd6f4]'
                                        }`}
                                    >
                                        <i data-lucide="kanban" class="w-4 h-4"></i>
                                        Kanban
                                        <TabBadge
                                            count={kanban.backlogCount}
                                            tone="bg-[#fab387] text-[#11111b]"
                                            title={`${kanban.backlogCount} open task${kanban.backlogCount === 1 ? '' : 's'}`}
                                        />
                                    </button>
                                )}
                                {/* Automations took this slot from Agents. The roster
                                    is still reachable at /agents and from any row on
                                    this page — it is a drill-in for one automation,
                                    not a category of its own. Staying active-styled
                                    for 'agents' keeps the header from looking like
                                    nothing is selected once you have drilled in. */}
                                <button
                                    onClick={() => navigateTab('automations')}
                                    class={`py-2 px-4 rounded-lg font-medium text-sm flex items-center gap-2 transition ${
                                        activeTab === 'automations' || activeTab === 'agents' || activeTab === 'integrations'
                                            ? 'bg-[#313244] text-[#b4befe]'
                                            : 'text-[#a6adc8] hover:text-[#cdd6f4]'
                                    }`}
                                >
                                    <i data-lucide="zap" class="w-4 h-4"></i>
                                    Automations
                                    <TabBadge
                                        count={staleCount}
                                        tone="bg-[#fab387] text-[#11111b]"
                                        title={`${staleCount} automation${staleCount === 1 ? '' : 's'} needing attention`}
                                    />
                                </button>
                                {/* Integrations left the nav for the Automations header,
                                    alongside the agent roster: both answer a question
                                    you ask *about* an automation — what can it reach,
                                    and how well does it run — rather than being places
                                    you go on their own.

                                    The route stays live and so does the failed-grant
                                    badge on the button that replaced this one. A dead
                                    credential does not belong to any one automation
                                    and can break chat or the review queue with no job
                                    involved, so it still has to be visible without
                                    knowing where to click. */}
                                <button
                                    onClick={() => navigateTab('review')}
                                    class={`py-2 px-4 rounded-lg font-medium text-sm flex items-center gap-2 transition ${
                                        activeTab === 'review' 
                                            ? 'bg-[#313244] text-[#b4befe]' 
                                            : 'text-[#a6adc8] hover:text-[#cdd6f4]'
                                    }`}
                                >
                                    <i data-lucide="clipboard-check" class="w-4 h-4"></i>
                                    Review
                                    <TabBadge
                                        count={review.reviewQueue.length + review.reviewAttention.length}
                                        tone="bg-[#ef4444] text-white"
                                        title={`${review.reviewQueue.length} waiting for you${review.reviewAttention.length ? `, ${review.reviewAttention.length} needing attention` : ''}`}
                                    />
                                </button>
                                <button
                                    onClick={() => navigateTab('memory')}
                                    class={`py-2 px-4 rounded-lg font-medium text-sm flex items-center gap-2 transition ${
                                        activeTab === 'memory'
                                            ? 'bg-[#313244] text-[#b4befe]'
                                            : 'text-[#a6adc8] hover:text-[#cdd6f4]'
                                    }`}
                                >
                                    <i data-lucide="notebook-text" class="w-4 h-4"></i>
                                    Memory
                                </button>
                            </div>
                            {/* Silent while healthy. A pulsing green "Connected" was
                                on unconditionally — it never watched anything, so it
                                could not have gone red, and it read as reassurance
                                the page had not earned. Now it appears only when a
                                poll actually fails, and says what failed on hover. */}
                            <div class="flex items-center gap-2 shrink-0">
                                {chat.apiDown && (
                                    <div class="text-xs text-[#f38ba8] flex items-center gap-2 cursor-help mr-1" title={chat.apiDown}>
                                        <div class="w-2.5 h-2.5 bg-[#f38ba8] rounded-full"></div>
                                        Disconnected
                                    </div>
                                )}
                                {/* Leftmost of the icon cluster, because it is
                                    the one that can demand to be looked at.
                                    Everything to its right is a place you
                                    choose to go. */}
                                <HealthIndicator
                                    health={health}
                                    onOpen={openHealth}
                                />
                                {/* Docs open in a new tab, deliberately: you
                                    read them *while* doing the thing on this
                                    screen, and navigating away would discard an
                                    unsent chat message. */}
                                <a
                                    href={DOCS_URL}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    title="Documentation (opens in a new tab)"
                                    aria-label="Documentation (opens in a new tab)"
                                    class="w-9 h-9 rounded-lg flex items-center justify-center transition text-[#a6adc8] hover:text-[#cdd6f4] hover:bg-[#313244]"
                                >
                                    <i data-lucide="book-open" class="w-[18px] h-[18px]"></i>
                                </a>
                                {/* Settings sits left of the account button, in
                                    the order they are reached for: the gear is
                                    the thing you actually want, and the avatar
                                    is who you are while wanting it. */}
                                <button
                                    onClick={() => openSettings(DEFAULT_SETTINGS_SECTION)}
                                    title="Settings"
                                    aria-label="Settings"
                                    class={`w-9 h-9 rounded-lg flex items-center justify-center transition ${
                                        settingsSection
                                            ? 'bg-[#313244] text-[#b4befe]'
                                            : 'text-[#a6adc8] hover:text-[#cdd6f4] hover:bg-[#313244]'
                                    }`}
                                >
                                    <i data-lucide="settings" class="w-[18px] h-[18px]"></i>
                                </button>
                                <UserMenu
                                    theme={theme}
                                    onTheme={setTheme}
                                    onOpenSettings={openSettings}
                                />
                            </div>
                        </header>

                        {/* CONTENT VIEWS */}
                        <div class="flex-1 overflow-hidden relative">

                            {/* VIEW: INTEGRATIONS. "Can each part of the system
                                still reach what it needs, and when did it last do
                                so?" — the whole scope of this screen.

                                Grouped by source system, never by protocol. MCP
                                versus direct API client is an implementation
                                detail, and grouping by it splits Gmail across two
                                cards while answering a question nobody asked. A
                                source row carries the worst status among its
                                grants, because grants fail independently: the
                                triage job's credential can go stale while chat's
                                keeps working, and a source that reads "fine"
                                because most of it is fine would be a lie.

                                Read-only on purpose. Adding, authorising, editing
                                and removing a connection all stay in `hermes
                                dashboard` — putting a re-auth button here would
                                make this a control surface, and a control surface
                                is not something you can trust as a status
                                report. */}
                            {activeTab === 'integrations' && (
                                <div class="h-full overflow-y-auto p-6 space-y-6">
                                    {integrationsError && (
                                        <div class="bg-[#f38ba8]/10 border border-[#f38ba8]/40 text-[#f38ba8] rounded-xl px-4 py-3 text-sm">
                                            {integrationsError}
                                        </div>
                                    )}
                                    {!integrations && !integrationsError && (
                                        <div class="text-sm text-[#585b70]">Loading integrations…</div>
                                    )}
                                    {integrations && (() => {
                                        const sources = integrations.sources || [];
                                        const gaps = integrations.gaps || [];
                                        const worst = gaps.some(g => g.severity === 'error') ? 'error'
                                            : gaps.some(g => g.severity === 'warn') ? 'warn' : 'info';
                                        const gapColor = { error: 'var(--acc-red)', warn: 'var(--acc-yellow)', info: 'var(--txt-muted)' }[worst];
                                        return (
                                        <>
                                            {/* The legend is not decoration. Four of
                                                these five states are routinely
                                                misread — "never used" as an error,
                                                "used" as working — and the whole
                                                screen turns on the distinction. */}
                                            <div class="flex items-center justify-between gap-4 flex-wrap">
                                                <div class="flex items-center gap-4 flex-wrap text-[11px] text-[#585b70]">
                                                    {['working', 'failed', 'stale', 'unverified', 'never'].map(k => (
                                                        <span key={k} class="flex items-center gap-1.5">
                                                            <StatusMark status={k} />
                                                            <span>{GRANT_STATUS[k].label}</span>
                                                        </span>
                                                    ))}
                                                </div>
                                                {gaps.length > 0 && (
                                                    <button
                                                        onClick={() => setGapsOpen(o => !o)}
                                                        class="flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded border border-[#313244] hover:border-[#45475a] transition"
                                                        style={{ color: gapColor }}
                                                    >
                                                        <i data-lucide="info" class="w-3.5 h-3.5"></i>
                                                        {gaps.length} thing(s) this screen cannot tell you
                                                        <i data-lucide={gapsOpen ? 'chevron-up' : 'chevron-down'} class="w-3.5 h-3.5"></i>
                                                    </button>
                                                )}
                                            </div>

                                            {/* What is missing from the logs, named
                                                on the screen rather than buried in a
                                                doc. The alternative to saying "this
                                                signal does not exist" is quietly
                                                rendering a weaker one as if it did,
                                                which is how a status screen starts
                                                lying. Every fix named here is a fix
                                                in the logging, never a probe. */}
                                            {gapsOpen && gaps.length > 0 && (
                                                <div class="bg-[#181825] rounded-xl border border-[#313244] divide-y divide-[#313244]">
                                                    {gaps.map(g => (
                                                        <div key={g.id} class="px-4 py-3">
                                                            <div class="text-xs font-semibold flex items-center gap-2"
                                                                 style={{ color: { error: 'var(--acc-red)', warn: 'var(--acc-yellow)', info: 'var(--txt-subtle)' }[g.severity] || 'var(--txt-subtle)' }}>
                                                                <i data-lucide={g.severity === 'info' ? 'info' : 'alert-triangle'} class="w-3.5 h-3.5"></i>
                                                                {g.title}
                                                            </div>
                                                            <p class="text-[11px] text-[#585b70] mt-1 leading-relaxed">{g.detail}</p>
                                                        </div>
                                                    ))}
                                                </div>
                                            )}

                                            {sources.length === 0 ? (
                                                <div class="bg-[#181825] rounded-xl border border-dashed border-[#45475a] p-8 text-center">
                                                    <i data-lucide="unplug" class="w-16 h-16 mx-auto mb-3 opacity-30"></i>
                                                    <p class="text-sm text-[#585b70]">
                                                        Nothing outside this host is reachable, and nothing has tried.
                                                    </p>
                                                </div>
                                            ) : (
                                                <div class="bg-[#181825] rounded-xl border border-[#313244] divide-y divide-[#313244] overflow-hidden">
                                                    {sources.map(src => {
                                                        const open = openSources.has(src.key);
                                                        const st = grantStatus(src.status);
                                                        // The most recent moment anything
                                                        // under this source reached out.
                                                        // Rolled up like the status, so the
                                                        // collapsed row answers both
                                                        // questions without expanding.
                                                        const lastUsed = src.grants.reduce(
                                                            (acc, g) => Math.max(acc, g.last_used_at || 0), 0);
                                                        return (
                                                            <div key={src.key}>
                                                                <button
                                                                    onClick={() => setOpenSources(prev => {
                                                                        const next = new Set(prev);
                                                                        next.has(src.key) ? next.delete(src.key) : next.add(src.key);
                                                                        return next;
                                                                    })}
                                                                    class="w-full text-left px-4 py-3 flex items-center gap-3 hover:bg-[#313244]/30 transition"
                                                                >
                                                                    <i data-lucide={open ? 'chevron-down' : 'chevron-right'}
                                                                       class="w-3.5 h-3.5 text-[#585b70] shrink-0"></i>
                                                                    <StatusMark status={src.status} />
                                                                    <span class="text-sm font-semibold text-[#cdd6f4]">{src.label}</span>
                                                                    {src.unknown && (
                                                                        <span class="text-[10px] uppercase font-bold tracking-wider bg-[#313244] text-[#585b70] px-2 py-0.5 rounded"
                                                                              title="Seen in the call log but declared in no config this dashboard can read">
                                                                            undeclared
                                                                        </span>
                                                                    )}
                                                                    <span class="text-[10px] text-[#585b70]">
                                                                        {src.grants.length} grant{src.grants.length === 1 ? '' : 's'}
                                                                    </span>
                                                                    {/* Credential trouble is the one
                                                                        thing allowed to shout from a
                                                                        collapsed row: no grant below
                                                                        can work while it holds. */}
                                                                    {src.credential_note && (
                                                                        <span class="text-[10px] text-[#f9e2af] truncate">{src.credential_note}</span>
                                                                    )}
                                                                    <span class="flex-1"></span>
                                                                    <span class="text-xs" style={{ color: st.color }}>{st.label}</span>
                                                                    <span class="text-[11px] text-[#585b70] w-20 text-right shrink-0">
                                                                        {fmtAgo(lastUsed) || 'never'}
                                                                    </span>
                                                                </button>

                                                                {open && (
                                                                    <div class="pb-2">
                                                                        {src.grants.map((g, gi) => {
                                                                            const gkey = `${src.key}::${g.consumer}::${g.capability || '*'}`;
                                                                            const gopen = openGrants.has(gkey);
                                                                            const gst = grantStatus(g.status);
                                                                            return (
                                                                                <div key={gkey}>
                                                                                    <button
                                                                                        onClick={() => setOpenGrants(prev => {
                                                                                            const next = new Set(prev);
                                                                                            next.has(gkey) ? next.delete(gkey) : next.add(gkey);
                                                                                            return next;
                                                                                        })}
                                                                                        class="w-full text-left pl-12 pr-4 py-2 flex items-center gap-3 hover:bg-[#313244]/30 transition"
                                                                                    >
                                                                                        <StatusMark status={g.status} title={BASIS_NOTE[g.status_basis]} />
                                                                                        <span class="text-xs text-[#a6adc8]">
                                                                                            {g.consumer_label}
                                                                                            {g.capability && (
                                                                                                <span class="text-[#585b70]"> · </span>
                                                                                            )}
                                                                                            {g.capability && (
                                                                                                <span class="font-mono text-[11px] text-[#89b4fa]">{g.capability}</span>
                                                                                            )}
                                                                                        </span>
                                                                                        <span class="flex-1"></span>
                                                                                        {/* The error, inline. When
                                                                                            something is broken this
                                                                                            is the single most useful
                                                                                            thing on the screen, and
                                                                                            hiding it one click deep
                                                                                            wastes the click. */}
                                                                                        {g.last_error && (
                                                                                            <span class="text-[11px] text-[#f38ba8] font-mono truncate max-w-sm" title={g.last_error}>
                                                                                                {g.last_error}
                                                                                            </span>
                                                                                        )}
                                                                                        <span class="text-[11px]" style={{ color: gst.color }}>{gst.label}</span>
                                                                                        {/* On a failure, both stamps:
                                                                                            the gap between them is how
                                                                                            long you have been broken.
                                                                                            One line — wrapped, the two
                                                                                            read as unrelated times. */}
                                                                                        <span class="text-[11px] text-[#585b70] w-52 text-right shrink-0 whitespace-nowrap">
                                                                                            {g.status === 'failed' && g.last_success_at
                                                                                                ? `${fmtAgo(g.last_used_at)} · worked ${fmtAgo(g.last_success_at)}`
                                                                                                : (fmtAgo(g.last_used_at) || '—')}
                                                                                        </span>
                                                                                    </button>

                                                                                    {/* Quietly: scope, credential,
                                                                                        and the last three calls.
                                                                                        No test button, no re-auth
                                                                                        link, no configuration. */}
                                                                                    {gopen && (
                                                                                        <div class="pl-16 pr-4 pb-3 pt-1 space-y-2">
                                                                                            <div class="flex flex-wrap items-center gap-1.5">
                                                                                                {g.operations.length > 0 ? g.operations.map(op => (
                                                                                                    <span key={op} class="text-[10px] font-mono bg-[#313244]/60 text-[#89b4fa] px-1.5 py-0.5 rounded">{op}</span>
                                                                                                )) : (
                                                                                                    <span class="text-[10px] text-[#585b70] italic">no operations recorded</span>
                                                                                                )}
                                                                                            </div>
                                                                                            <div class="text-[11px] text-[#585b70] flex flex-wrap gap-x-4 gap-y-1">
                                                                                                <span>credential: {g.credential_type || 'unknown'}</span>
                                                                                                {g.expires_at && <span>{fmtExpiry(g.expires_at)}</span>}
                                                                                                {g.expected_interval && <span>expected every {g.expected_interval}</span>}
                                                                                                <span>{BASIS_NOTE[g.status_basis] || ''}</span>
                                                                                                {g.call_count > 0 && <span>{g.call_count} call(s) in the last 30d</span>}
                                                                                            </div>
                                                                                            {g.recent.length > 0 && (
                                                                                                <div class="space-y-0.5">
                                                                                                    {g.recent.map((r, i) => (
                                                                                                        <div key={i} class="text-[11px] font-mono flex items-center gap-2">
                                                                                                            <span class="text-[#45475a] w-20 shrink-0">{fmtAgo(r.at)}</span>
                                                                                                            <span style={{ color: r.ok === false ? 'var(--acc-red)' : r.ok === true ? 'var(--acc-green)' : 'var(--txt-muted)' }}>
                                                                                                                {r.ok === false ? '✗' : r.ok === true ? '✓' : '◌'}
                                                                                                            </span>
                                                                                                            <span class="text-[#a6adc8]">{r.operation}</span>
                                                                                                            {r.error && <span class="text-[#f38ba8] truncate">{r.error}</span>}
                                                                                                        </div>
                                                                                                    ))}
                                                                                                </div>
                                                                                            )}
                                                                                        </div>
                                                                                    )}
                                                                                </div>
                                                                            );
                                                                        })}

                                                                        {/* Source-level facts, once, under
                                                                            the grants rather than repeated
                                                                            on each: they describe the
                                                                            connection, not the grant. */}
                                                                        <div class="pl-12 pr-4 pt-2 text-[11px] text-[#585b70] space-y-1 border-t border-[#313244]/60 mt-1">
                                                                            {src.target && (
                                                                                <div class="font-mono break-all">{src.transport}: {src.target}</div>
                                                                            )}
                                                                            {src.summary && <div>{src.summary}</div>}
                                                                            <div class="flex flex-wrap items-center gap-1.5">
                                                                                {src.declared_by.map(a => (
                                                                                    <span key={a} class="text-[10px] font-mono bg-[#313244]/60 text-[#89b4fa] px-1.5 py-0.5 rounded">{a}</span>
                                                                                ))}
                                                                                {src.credential_keys.map(k => (
                                                                                    <span key={k} class="text-[10px] font-mono bg-[#313244]/60 text-[#f9e2af] px-1.5 py-0.5 rounded">{k}</span>
                                                                                ))}
                                                                            </div>
                                                                        </div>
                                                                    </div>
                                                                )}
                                                            </div>
                                                        );
                                                    })}
                                                </div>
                                            )}

                                            <p class="text-[11px] text-[#585b70]">
                                                Read-only. The default agent's MCP connections are added, edited
                                                and removed in{' '}
                                                <button onClick={() => openSettings('integrations')}
                                                        class="text-[#89b4fa] underline">Settings → Connections</button>;
                                                everything else here — the workflows' own API clients, and OAuth
                                                sign-in — stays in
                                                <span class="font-mono text-[#a6adc8]"> hermes dashboard</span>.
                                                {integrations.config && !integrations.config.present && (
                                                    <span> No <span class="font-mono">integrations.json</span> — copy
                                                        <span class="font-mono"> integrations.example.json</span> to
                                                        <span class="font-mono"> {integrations.config.path}</span> to name
                                                        sources and declare how often each grant should run.</span>
                                                )}
                                            </p>
                                        </>
                                        );
                                    })()}
                                </div>
                            )}

                            {/* VIEW: MEMORY. Read-only window onto the markdown the
                                workflows write. Four pages, each with an address:
                                the roster (/memory), search (/memory/search), one
                                record (/memory/document/<slug>) and the wikilink
                                graph (/memory/graph, optionally centred on a record).

                                Lists still lead, for the reason the old graph tab
                                gave when it was deleted: "who is on file" and "what
                                is known about X" are the questions, and a
                                force-directed layout answers neither. The graph is
                                back as a page rather than as the tab, because the
                                question it does answer — what is this connected to —
                                is real but occasional, and it is honest about being
                                empty when nothing has been linked. */}
                            {activeTab === 'memory' && (
                                <div class="h-full overflow-y-auto p-6">
                                    <div class="max-w-5xl mx-auto space-y-5">

                                        <div class="flex items-start justify-between gap-4">
                                            <div>
                                                <h2 class="text-xl font-semibold text-[#cdd6f4]">Memory</h2>
                                                <p class="text-sm text-[#a6adc8] mt-1">
                                                    One markdown file per person or organisation, written by the
                                                    workflows. The file is the record — what you see below is its
                                                    contents, not a view assembled from something else.
                                                </p>
                                            </div>
                                            {wikiHealth && (
                                                <span class={`text-xs px-2 py-1 rounded-full shrink-0 ${
                                                    wikiHealth.status === 'ok' ? 'bg-[#313244] text-[#a6e3a1]' : 'bg-[#313244] text-[#f38ba8]'
                                                }`}>
                                                    {wikiHealth.status === 'ok'
                                                        ? `${wikiHealth.documents} docs · ${wikiHealth.facts} facts`
                                                        : 'wiki unavailable'}
                                                </span>
                                            )}
                                        </div>

                                        {wikiHealth && wikiHealth.status === 'ok' && !wikiHealth.index_present && (
                                            <div class="text-xs text-[#f9e2af] bg-[#181825] border border-[#313244] rounded-lg p-3">
                                                No search index yet. It is built by the workflows service on first
                                                write; documents still list without it, but search returns nothing.
                                            </div>
                                        )}

                                        {/* Pages, not modes: each one navigates, so the
                                            address bar follows and Back steps between
                                            them. A document open under Documents keeps
                                            that button lit — it is the page you drilled
                                            in from, and Back returns to it. */}
                                        <div class="flex gap-2">
                                            {[
                                                { id: 'documents', label: 'Documents' },
                                                { id: 'search', label: 'Search' },
                                                { id: 'graph', label: 'Graph' },
                                            ].map(m => (
                                                <button key={m.id}
                                                    onClick={() => navigateMemory(m.id, m.id === 'graph' ? memorySlug : null)}
                                                    class={`px-3 py-1.5 rounded-lg text-sm transition ${
                                                        memoryView === m.id || (m.id === 'documents' && memoryView === 'document')
                                                            ? 'bg-[#313244] text-[#b4befe]'
                                                            : 'text-[#a6adc8] hover:text-[#cdd6f4]'
                                                    }`}>
                                                    {m.label}
                                                </button>
                                            ))}
                                        </div>

                                        {memoryView === 'search' && (
                                            <div class="flex gap-2">
                                                <input
                                                    value={wikiQuery}
                                                    onInput={e => setWikiQuery(e.target.value)}
                                                    onKeyDown={e => { if (e.key === 'Enter') runWikiSearch(); }}
                                                    placeholder="Search facts…"
                                                    class="flex-1 bg-[#181825] border border-[#313244] rounded-lg px-3 py-2 text-sm text-[#cdd6f4] placeholder-[#585b70] focus:outline-none focus:border-[#585b70]"
                                                />
                                                <button onClick={runWikiSearch}
                                                    class="px-4 py-2 rounded-lg text-sm bg-[#313244] text-[#b4befe] hover:bg-[#45475a] transition">
                                                    Search
                                                </button>
                                            </div>
                                        )}

                                        {wikiError && (
                                            <div class="text-sm text-[#f38ba8] bg-[#181825] border border-[#313244] rounded-lg p-3">
                                                {wikiError}
                                            </div>
                                        )}
                                        {wikiLoading && (
                                            <div class="text-sm text-[#585b70]">Loading…</div>
                                        )}

                                        {/* Documents, newest first. */}
                                        {memoryView === 'documents' && !wikiLoading && (
                                            <div class="space-y-2">
                                                {wikiDocuments.length === 0 && !wikiError && (
                                                    <div class="text-sm text-[#585b70]">
                                                        Nothing recorded yet. The workflows write here when they
                                                        refresh a contact.
                                                    </div>
                                                )}
                                                {wikiDocuments.map(doc => (
                                                    <div key={doc.slug}
                                                        onClick={() => openWikiDocument(doc.slug)}
                                                        class="bg-[#181825] border border-[#313244] rounded-lg p-3 cursor-pointer hover:border-[#45475a] transition">
                                                        <div class="flex items-center justify-between gap-3">
                                                            <span class="text-sm text-[#cdd6f4] font-medium truncate">{doc.title}</span>
                                                            <span class="text-xs text-[#585b70] shrink-0">{doc.last_refreshed}</span>
                                                        </div>
                                                        <div class="text-xs text-[#585b70] font-mono truncate mt-1">{doc.key}</div>
                                                    </div>
                                                ))}
                                            </div>
                                        )}

                                        {/* Search hits link back to the document that holds them. */}
                                        {memoryView === 'search' && !wikiLoading && (
                                            <div class="space-y-2">
                                                {wikiSearched && wikiFacts.length === 0 && !wikiError && (
                                                    <div class="text-sm text-[#585b70]">
                                                        No match. Unlike the graph this replaced, that is an answer:
                                                        nothing here shares a term with the query.
                                                    </div>
                                                )}
                                                {wikiFacts.map((f, i) => (
                                                    <div key={i}
                                                        onClick={() => openWikiDocument(f.slug)}
                                                        class="bg-[#181825] border border-[#313244] rounded-lg p-3 cursor-pointer hover:border-[#45475a] transition">
                                                        <div class="text-sm text-[#cdd6f4]">{f.fact}</div>
                                                        <div class="flex items-center gap-3 mt-2 text-xs text-[#585b70]">
                                                            <span class="font-mono truncate">{f.key}</span>
                                                            <span class="shrink-0">{f.valid_at}</span>
                                                            <span class="shrink-0 italic truncate">{f.source}</span>
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        )}

                                        {/* One document: its sections, and both link directions. The
                                            neighbourhood is what the old Cytoscape canvas drew from a
                                            single one-hop Cypher match; wikilinks answer the same
                                            question without a graph database under them. */}
                                        {memoryView === 'document' && activeWikiDoc && !wikiLoading && (
                                            <div class="space-y-4">
                                                <div class="flex items-center justify-between gap-3">
                                                    <button onClick={() => navigateMemory('documents', null)}
                                                        class="text-sm text-[#b4befe] hover:text-[#cdd6f4] transition">
                                                        ← Back
                                                    </button>
                                                    {/* The graph, centred here. Its own address, so
                                                        "what is this connected to" is a link rather
                                                        than a click someone has to be talked through. */}
                                                    <button onClick={() => navigateMemory('graph', activeWikiDoc.slug)}
                                                        class="text-sm text-[#a6adc8] hover:text-[#cdd6f4] transition inline-flex items-center gap-1.5">
                                                        <i data-lucide="git-fork" class="w-3.5 h-3.5"></i>
                                                        View in graph
                                                    </button>
                                                </div>

                                                <div class="bg-[#181825] border border-[#313244] rounded-lg p-4">
                                                    <h3 class="text-lg font-semibold text-[#cdd6f4]">{activeWikiDoc.title}</h3>
                                                    <div class="text-xs text-[#585b70] font-mono mt-1">{activeWikiDoc.key}</div>
                                                    {activeWikiDoc.aliases.length > 0 && (
                                                        <div class="text-xs text-[#a6adc8] mt-2">
                                                            Also known as: {activeWikiDoc.aliases.join(', ')}
                                                        </div>
                                                    )}
                                                </div>

                                                {activeWikiDoc.sections.map((section, i) => (
                                                    <div key={i} class="bg-[#181825] border border-[#313244] rounded-lg p-4">
                                                        <div class="flex items-center gap-3 text-xs text-[#585b70] mb-2">
                                                            <span>{section.when}</span>
                                                            <span class="italic truncate">{section.source}</span>
                                                        </div>
                                                        {/* Flat, and deliberately so — the store's
                                                            shape is one bullet per fact under a dated
                                                            heading, with no nesting to lose. A run of
                                                            bullets that reads like an outline is an
                                                            ingest that chopped a document into lines,
                                                            not a hierarchy this view flattened. */}
                                                        <ul class="space-y-1">
                                                            {section.lines.map((line, j) => (
                                                                <li key={j} class="text-sm text-[#cdd6f4] flex gap-2">
                                                                    <span class="text-[#585b70] shrink-0">•</span>
                                                                    <span class="min-w-0">
                                                                        <MemoryFactText text={line} onOpen={openWikiDocument} />
                                                                    </span>
                                                                </li>
                                                            ))}
                                                        </ul>
                                                    </div>
                                                ))}

                                                <div class="grid grid-cols-2 gap-4">
                                                    <div class="bg-[#181825] border border-[#313244] rounded-lg p-4">
                                                        <div class="text-xs text-[#a6adc8] mb-2">Links out</div>
                                                        {activeWikiDoc.links_out.length === 0 && (
                                                            <div class="text-xs text-[#585b70]">None</div>
                                                        )}
                                                        {activeWikiDoc.links_out.map(t => (
                                                            <div key={t} onClick={() => openWikiDocument(t)}
                                                                class="text-sm text-[#b4befe] hover:text-[#cdd6f4] cursor-pointer truncate">
                                                                {t}
                                                            </div>
                                                        ))}
                                                    </div>
                                                    <div class="bg-[#181825] border border-[#313244] rounded-lg p-4">
                                                        <div class="text-xs text-[#a6adc8] mb-2">Links in</div>
                                                        {activeWikiDoc.links_in.length === 0 && (
                                                            <div class="text-xs text-[#585b70]">None</div>
                                                        )}
                                                        {activeWikiDoc.links_in.map(t => (
                                                            <div key={t} onClick={() => openWikiDocument(t)}
                                                                class="text-sm text-[#b4befe] hover:text-[#cdd6f4] cursor-pointer truncate">
                                                                {t}
                                                            </div>
                                                        ))}
                                                    </div>
                                                </div>
                                            </div>
                                        )}

                                        {/* The graph. Centred on a document when the URL
                                            names one (/memory/graph/<slug>), the whole
                                            store when it does not. */}
                                        {memoryView === 'graph' && (
                                            <div class="space-y-4">
                                                {memorySlug && (
                                                    <div class="flex items-center justify-between gap-3">
                                                        <button onClick={() => navigateMemory('graph', null)}
                                                            class="text-sm text-[#b4befe] hover:text-[#cdd6f4] transition">
                                                            ← Whole graph
                                                        </button>
                                                        <button onClick={() => navigateMemory('document', memorySlug)}
                                                            class="text-sm text-[#a6adc8] hover:text-[#cdd6f4] transition inline-flex items-center gap-1.5">
                                                            <i data-lucide="file-text" class="w-3.5 h-3.5"></i>
                                                            Open the record
                                                        </button>
                                                    </div>
                                                )}
                                                {/* focus is the server's resolved slug rather than
                                                    the URL's segment: a wikilink puts a key in the
                                                    address ("Kestrel Underwriting") while node ids
                                                    are slugs, and comparing the raw segment would
                                                    leave the focused node undrawn on exactly those
                                                    links. */}
                                                <MemoryGraphView
                                                    graph={wikiGraph}
                                                    loading={wikiGraphLoading}
                                                    error={wikiGraphError}
                                                    focus={(wikiGraph && wikiGraph.focus) || memorySlug}
                                                    onOpenDocument={slug => navigateMemory('document', slug)}
                                                    onFocusNode={slug => navigateMemory('graph', slug)}
                                                    onClearFocus={() => navigateMemory('graph', null)}
                                                />
                                            </div>
                                        )}

                                    </div>
                                </div>
                            )}

                            {/* VIEW: AUTOMATIONS.
                                Every scheduled thing on this host as one flat
                                list, named by outcome and described by what it
                                does — not by what kind of thing runs it. Where
                                it runs is a column, which is what lets an ADK
                                workflow and a bare backup script sit in the
                                same table without either looking out of place.

                                Schedule earns its column by making staleness
                                legible: "14h ago" says nothing until "every 10m"
                                is sitting next to it. */}
                            {/* h-full, not flex-1. The pane holding every view is
                                `flex-1 overflow-hidden relative` — a *block*
                                container, so `flex-1` on a child sets no height
                                and the child grows to fit its content, which the
                                parent then clips with nothing to scroll. Every
                                other view here uses h-full for exactly this
                                reason; these two were the odd ones out, and the
                                symptom only appears once the content is taller
                                than the window. */}
                            {/* The list, and one automation in full. Both moved to
                                60-automations-view.jsx; what stays here is the
                                choice between them and the things only App can
                                give — the ranked list, the shared poll clock,
                                and the navigation out to other tabs. */}
                            {activeTab === 'automations' && !activeAutomationId && (
                                <AutomationsListView
                                    automations={automations}
                                    staleCount={staleCount}
                                    hasDrift={hasDrift}
                                    failedGrants={failedGrants}
                                    now={now}
                                    navigateTab={navigateTab}
                                    navigateMetricsView={navigateMetricsView}
                                    navigateAutomation={navigateAutomation}
                                />
                            )}

                            {/* VIEW: ONE AUTOMATION, WHOLE.
                                The answer to "why did that fail last night" used
                                to be spread over three pages that each held part
                                of it — the list knew the schedule, a scorecard
                                knew the runs, a profile page knew the config —
                                and the errors were on none of them. This is all
                                of it, at one address you can paste to someone. */}
                            {activeTab === 'automations' && activeAutomationId && (
                                <AutomationDetailView
                                    activeAutomationId={activeAutomationId}
                                    active={activeTab === 'automations'}
                                    cronJobs={cronJobs}
                                    adkTeams={adkTeams}
                                    now={now}
                                    automationAgentLink={automationAgentLink}
                                    navigateTab={navigateTab}
                                    navigateScorecard={navigateScorecard}
                                    navigateHermesAgent={navigateHermesAgent}
                                    runCronJobNow={runCronJobNow}
                                    cronRunning={cronRunning}
                                    cronRunNotice={cronRunNotice}
                                    selectSession={chat.selectSession}
                                    setActiveKanbanTaskId={setActiveKanbanTaskId}
                                    chatAboutAutomation={chat.chatAboutAutomation}
                                />
                            )}

                            {/* VIEW: THE MERGED AGENTS PANE.
                                Three questions used to live in two tabs: what is
                                this team, how well does it run, and when does it
                                run. They are one question about one thing, so
                                they are one pane. No selection = the fleet and
                                how it is wired; a selection = that agent in full. */}
                            {activeTab === 'agents' && (() => {
                                const team = activeTeam;
                                const agent = activeAdkAgent;
                                const sc = team ? adkScores[team.app] : null;
                                const util = sc && sc.utilization;
                                const drift = sc && sc.drift;
                                const isRoot = !!(team && agent && rootAgentName(team) === agent.name);
                                const jobs = team ? jobsForApp(team.app) : [];
                                const perAgent = (sc && agent)
                                    ? (sc.per_agent || []).find(a => a.name === agent.name) || null
                                    : null;
                                // Runs this agent actually took a turn in. A team
                                // member that never fired should not inherit the
                                // root's run list as if it had.
                                const agentRuns = (sc && agent)
                                    ? (sc.runs || []).filter(r => (r.agents || []).some(e => e.name === agent.name))
                                    : [];
                                const fleetFor = (appId) => adkFleet.find(f => f.app === appId) || null;
                                const stamp = (v) => fmtCronStamp(v);
                                // A Hermes profile is the third thing this pane can be
                                // about, and it wins over the ADK selection: the rail
                                // clears one when it sets the other.
                                const hermes = activeHermes;
                                return (
                                <div class="h-full overflow-y-auto p-6">
                                    <div class="max-w-5xl mx-auto space-y-6">

                                        <div class="flex items-start justify-between gap-4">
                                            <div>
                                                <h2 class="text-lg font-bold text-[#cdd6f4]">
                                                    {hermes ? hermes.agent : agent ? agent.name : 'Fleet'}
                                                </h2>
                                                <p class="text-xs text-[#585b70] mt-1">
                                                    {hermes
                                                        ? <>Hermes {hermes.isDefault ? 'default profile' : 'profile'} — what it is made of, and what it runs on a schedule.</>
                                                        : !agent
                                                        ? <>Every agent on this host — workflow teams and Hermes profiles — what they score, and what launches them.</>
                                                        : isRoot
                                                        ? <>Entry point of <span class="font-mono text-[#a6adc8]">{team.app}</span> — configuration, record and schedule.</>
                                                        : <>Part of <span class="font-mono text-[#a6adc8]">{team.app}</span> — configuration, record and schedule.</>}
                                                </p>
                                            </div>
                                            <div class="flex items-center gap-2 text-xs shrink-0">
                                                <span class={`w-2.5 h-2.5 rounded-full ${adkHealth && adkHealth.ok ? 'bg-[#a6e3a1]' : 'bg-[#ef4444]'}`}></span>
                                                <span class="text-[#a6adc8]">{adkHealth && adkHealth.ok ? 'runner up' : 'runner down'}</span>
                                            </div>
                                        </div>

                                        {/* ================= ONE HERMES AGENT ================= */}
                                        {hermes && (
                                            <HermesAgentPane
                                                group={hermes}
                                                onOpenJob={navigateAutomation}
                                            />
                                        )}

                                        {/* ================= FLEET ================= */}
                                        {!hermes && !agent && (
                                            <>
                                                {adkLoading && adkTeams.length === 0 && (
                                                    <div class="text-center py-10 text-sm text-[#585b70]">Loading teams...</div>
                                                )}
                                                {adkTeams.map(t => {
                                                    const roster = t.agents || [];
                                                    const summary = fleetFor(t.app);
                                                    const tJobs = jobsForApp(t.app);
                                                    const rootName = rootAgentName(t);
                                                    return (
                                                        <div key={t.app} class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                                            <SectionHeader
                                                                icon="users"
                                                                title={t.app}
                                                                right={t.source === 'live' ? 'read live from ADK' : 'parsed from agent.py'}
                                                            />
                                                            <div class="p-4 space-y-4">
                                                                {t.description && (
                                                                    <p class="text-sm text-[#a6adc8]">{t.description}</p>
                                                                )}

                                                                {/* The association, laid out in run order: this
                                                                    is the "how they are wired" the fleet view
                                                                    exists to show. */}
                                                                <div class="flex items-center gap-1.5 flex-wrap">
                                                                    {roster.length === 0 && (
                                                                        <span class="text-xs text-[#585b70] italic">no agents described</span>
                                                                    )}
                                                                    {roster.map((g, i) => (
                                                                        <React.Fragment key={g.name}>
                                                                            {i > 0 && g.depth > 0 && (
                                                                                <i data-lucide="chevron-right" class="w-3 h-3 text-[#45475a]"></i>
                                                                            )}
                                                                            {/* Pushes: fleet -> one agent is a jump between
                                                                                views, so Back returns to the fleet. */}
                                                                            <button
                                                                                onClick={() => navigateScorecard(`${t.app}::${g.name}`)}
                                                                                title={g.agent_class || undefined}
                                                                                class={`px-2.5 py-1 rounded-lg text-xs font-medium transition border ${
                                                                                    g.name === rootName
                                                                                        ? 'bg-[#b4befe]/15 border-[#b4befe]/40 text-[#b4befe]'
                                                                                        : 'bg-[#11111b] border-[#313244] text-[#a6adc8] hover:border-[#45475a]'
                                                                                }`}
                                                                            >
                                                                                {g.name}
                                                                            </button>
                                                                        </React.Fragment>
                                                                    ))}
                                                                </div>

                                                                <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
                                                                    <StatTile icon="play" label="Runs"
                                                                              value={fmtNum(summary ? summary.run_count : null)} />
                                                                    <StatTile icon="target" label="Success rate"
                                                                              muted={!summary || summary.success_rate === null}
                                                                              value={summary && summary.success_rate !== null
                                                                                     ? fmtPct(summary.success_rate) : 'never run'} />
                                                                    <StatTile icon="clock" label="Last run"
                                                                              muted={!(summary && summary.last_run_at)}
                                                                              value={summary && summary.last_run_at ? stamp(summary.last_run_at) : '—'}
                                                                              hint={summary && summary.last_status ? summary.last_status : null} />
                                                                    <StatTile icon="alarm-clock" label="Schedule"
                                                                              muted={tJobs.length === 0}
                                                                              value={tJobs.length ? formatSchedule(tJobs[0]) : 'unscheduled'}
                                                                              hint={tJobs.length > 1 ? `+${tJobs.length - 1} more job${tJobs.length === 2 ? '' : 's'}` : null} />
                                                                </div>

                                                                {/* A pipeline can succeed every run and still not be
                                                                    doing its job. Surfaced on the fleet card so that is
                                                                    visible without clicking into an agent. */}
                                                                {summary && summary.self_assessment_score !== null
                                                                    && summary.self_assessment_score !== undefined
                                                                    && summary.self_assessment_score < 1 && (
                                                                    <div class="text-xs bg-[#f9e2af]/10 border border-[#f9e2af]/30 rounded-lg px-3 py-2 text-[#f9e2af] flex items-start gap-2">
                                                                        <i data-lucide="stethoscope" class="w-3.5 h-3.5 mt-0.5 shrink-0"></i>
                                                                        <span>
                                                                            Self-assessment {fmtPct(summary.self_assessment_score)} —{' '}
                                                                            <span class="font-mono">{(summary.failing_stages || []).join(', ')}</span>
                                                                            {' '}not doing their job. Filed as development work.
                                                                        </span>
                                                                    </div>
                                                                )}

                                                                {tJobs.length > 0 && (
                                                                    <div class="space-y-1">
                                                                        {tJobs.map(j => (
                                                                            <div key={j.id} class="text-xs text-[#585b70] flex items-center gap-2 flex-wrap">
                                                                                <i data-lucide="alarm-clock" class="w-3 h-3 shrink-0"></i>
                                                                                <span>Launched by</span>
                                                                                <span class="font-mono text-[#cdd6f4]">{j.name || j.id}</span>
                                                                                <span class="text-[#a6e3a1] font-mono">{formatSchedule(j)}</span>
                                                                                {j.last_status === 'error' && (
                                                                                    <span class="text-[#f38ba8]">last run failed</span>
                                                                                )}
                                                                            </div>
                                                                        ))}
                                                                    </div>
                                                                )}
                                                            </div>
                                                        </div>
                                                    );
                                                })}

                                                {/* The other half of the fleet. A Hermes profile is
                                                    an agent this host runs, so the view of "every
                                                    agent here" is incomplete without it — and its
                                                    scheduled work is not ownerless, it belongs to
                                                    the profile whose scheduler runs it. */}
                                                {hermesGroups.length > 0 && (
                                                    <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                                        <SectionHeader
                                                            icon="bot"
                                                            title="Hermes agents"
                                                            right={`${hermesGroups.length} profile${hermesGroups.length === 1 ? '' : 's'}`}
                                                        />
                                                        <div class="p-4 space-y-2">
                                                            {hermesGroups.map(g => (
                                                                <button
                                                                    key={g.agent}
                                                                    onClick={() => navigateHermesAgent(g.agent, null)}
                                                                    class="w-full text-left px-3 py-2 rounded-lg flex items-center gap-2 text-xs transition border bg-[#11111b] border-[#313244] text-[#a6adc8] hover:border-[#45475a]"
                                                                >
                                                                    <i data-lucide="bot" class="w-3.5 h-3.5 shrink-0"></i>
                                                                    <span class="font-semibold text-[#cdd6f4]">{g.agent}</span>
                                                                    {g.isDefault && (
                                                                        <span class="text-[9px] uppercase font-bold px-1.5 py-0.5 rounded bg-[#b4befe]/15 text-[#b4befe] shrink-0">default</span>
                                                                    )}
                                                                    {g.summary && (
                                                                        <span class="font-mono text-[#a6e3a1] truncate">{g.summary.model}</span>
                                                                    )}
                                                                    <span class="ml-auto shrink-0 text-[#585b70]">
                                                                        {g.jobs.length
                                                                            ? `${g.jobs.length} job${g.jobs.length === 1 ? '' : 's'}`
                                                                            : 'nothing scheduled'}
                                                                    </span>
                                                                </button>
                                                            ))}
                                                            {/* A job that names an app no team describes is a
                                                                different problem from a job that names none:
                                                                the first means the team list is incomplete,
                                                                not that the job is unscheduled work. */}
                                                            {unattachedJobs.some(j => j.adk_app) && (
                                                                <div class="text-xs text-[#f9e2af] pt-1">
                                                                    {unattachedJobs.filter(j => j.adk_app).map(j => j.adk_app).join(', ')}
                                                                    {' '}named by a job but described by no team on this host
                                                                </div>
                                                            )}
                                                        </div>
                                                    </div>
                                                )}
                                            </>
                                        )}

                                        {/* ================= ONE AGENT ================= */}
                                        {!hermes && agent && (
                                            <>
                                                {/* The runner imports agent.py once at startup and has no
                                                    --reload, so an edited team is not the running team. */}
                                                {drift && drift.changed_since_last_run === true && (
                                                    <div class="bg-[#f9e2af]/10 border border-[#f9e2af] rounded-xl p-4 text-sm text-[#f9e2af] flex items-start gap-3">
                                                        <i data-lucide="triangle-alert" class="w-4 h-4 mt-0.5 shrink-0"></i>
                                                        <div>
                                                            <div class="font-semibold">agent.py changed since the last run</div>
                                                            <div class="text-xs mt-1 opacity-90">
                                                                ADK loads the team once at startup. Restart <span class="font-mono">hermes-workflows</span> for these edits to take effect.
                                                            </div>
                                                        </div>
                                                    </div>
                                                )}

                                                {team.status !== 'ok' && team.error && (
                                                    <div class="bg-[#f38ba8]/10 border border-[#f38ba8] rounded-xl p-4 text-sm text-[#f38ba8]">
                                                        <div class="font-semibold">{team.error.type} in {team.app}/agent.py</div>
                                                        <div class="font-mono text-xs mt-1">
                                                            {team.error.line ? `line ${team.error.line}: ` : ''}{team.error.message}
                                                        </div>
                                                        {team.stale && <div class="text-xs mt-2 opacity-80">Showing the last team that parsed cleanly.</div>}
                                                    </div>
                                                )}

                                                {/* ---------- 1. CONFIGURATION ---------- */}
                                                <div class="bg-[#181825] border border-[#313244] rounded-xl p-4">
                                                    <div class="flex items-center justify-between gap-3 flex-wrap">
                                                        <div class="flex items-center gap-2">
                                                            <i data-lucide="bot" class="w-4 h-4 text-[#b4befe]"></i>
                                                            <span class="text-base font-bold text-[#cdd6f4]">{agent.name}</span>
                                                            {agent.agent_class && (
                                                                <span class="text-[10px] uppercase font-bold tracking-wider text-[#9ca3af] bg-[#313244] px-2 py-0.5 rounded">{agent.agent_class}</span>
                                                            )}
                                                            {agent.model_tier && (
                                                                <span class="text-[10px] uppercase font-bold tracking-wider text-[#11111b] bg-[#a6e3a1] px-2 py-0.5 rounded">{agent.model_tier}</span>
                                                            )}
                                                            {isRoot && (
                                                                <span class="text-[10px] uppercase font-bold tracking-wider text-[#11111b] bg-[#b4befe] px-2 py-0.5 rounded">entry point</span>
                                                            )}
                                                        </div>
                                                        {/* No Scorecard button any more: selecting the agent
                                                            *is* the scorecard now, and the URL it deep-links
                                                            to is already in the address bar. */}
                                                        <span class="font-mono text-xs text-[#585b70]">
                                                            {team.source === 'live'
                                                                ? `live from ${team.app}`
                                                                : `${team.app}/agent.py:${agent.line}`}
                                                        </span>
                                                    </div>
                                                    {agent.description && <p class="text-sm text-[#a6adc8] mt-2">{agent.description}</p>}
                                                    <div class="flex flex-wrap gap-x-6 gap-y-1 mt-3 text-xs">
                                                        {/* app-info does not report the model, so say that rather than
                                                            render a dash that reads as "no model configured". */}
                                                        <span class="text-[#585b70]">model{' '}
                                                            {agent.model
                                                                ? <span class="font-mono text-[#a6e3a1]">{agent.model}</span>
                                                                : <span class="italic text-[#585b70]">
                                                                    {team.source === 'live' ? 'not reported by ADK' : '—'}
                                                                  </span>}
                                                        </span>
                                                        {agent.model_env_override && (
                                                            <span class="text-[#585b70]">override <span class="font-mono text-[#f9e2af]">${agent.model_env_override}</span></span>
                                                        )}
                                                        {agent.model_extra && agent.model_extra.api_base && (
                                                            <span class="text-[#585b70]">via <span class="font-mono text-[#cdd6f4]">{agent.model_extra.api_base}</span></span>
                                                        )}
                                                        {Object.entries(agent.config || {}).map(([k, v]) => (
                                                            <span key={k} class="text-[#585b70]">{k} <span class="font-mono text-[#cdd6f4]">{String(v)}</span></span>
                                                        ))}
                                                    </div>
                                                </div>

                                                {agent.sub_agents && agent.sub_agents.length > 0 && (
                                                    <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                                        <SectionHeader icon="git-branch" title={`Steps (${agent.sub_agents.length})`} />
                                                        <div class="divide-y divide-[#313244]">
                                                            {agent.sub_agents.map((name, i) => (
                                                                <button key={name} onClick={() => setActiveTeamAgent(`${team.app}::${name}`)}
                                                                    class="w-full px-4 py-2.5 flex items-center gap-3 text-left hover:bg-[#313244] transition">
                                                                    <span class="font-mono text-xs text-[#585b70]">{i + 1}</span>
                                                                    <span class="text-sm text-[#cdd6f4]">{name}</span>
                                                                </button>
                                                            ))}
                                                        </div>
                                                    </div>
                                                )}

                                                {/* Gated on having an instruction rather than on
                                                    being a leaf: a routing root agent has a real
                                                    system prompt worth reading. */}
                                                {(agent.instruction || agent.instruction_source) && (
                                                    <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                                        <SectionHeader icon="file-text" title="System Prompt"
                                                                       right={`${agent.instruction_chars} chars`} />
                                                        <pre class="p-4 text-[13px] leading-relaxed text-[#cdd6f4] font-mono" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
{agent.instruction || (agent.instruction_source ? agent.instruction_source : '— none —')}
                                                        </pre>
                                                        {!agent.instruction_resolved && (
                                                            <div class="px-4 pb-3 text-[10px] text-[#f9e2af]">
                                                                Not a literal string — showing the source expression.
                                                            </div>
                                                        )}
                                                    </div>
                                                )}

                                                {/* No card at all when the agent declares no tools —
                                                    an empty section is noise in a read-only view. */}
                                                {!agent.is_workflow && agent.tool_count > 0 && (
                                                    <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                                        <SectionHeader icon="wrench" title={`Tools (${agent.tool_count})`} />
                                                        <div class="divide-y divide-[#313244]">
                                                            {agent.tools.map(t => (
                                                                <details key={t.name} class="px-4 py-3">
                                                                    <summary class="cursor-pointer text-sm text-[#cdd6f4] font-mono flex items-center gap-2">
                                                                        <span class="text-[#b4befe]">{t.name}</span>
                                                                        <span class="text-[#585b70] text-xs">
                                                                            ({(t.params || []).map(p => p.name).join(', ')}){t.returns ? ` -> ${t.returns}` : ''}
                                                                        </span>
                                                                        {!t.resolved && <span class="text-[10px] text-[#f9e2af]">unresolved</span>}
                                                                    </summary>
                                                                    {t.docstring && (
                                                                        <pre class="mt-2 text-xs text-[#a6adc8]" style={{ whiteSpace: 'pre-wrap' }}>{t.docstring}</pre>
                                                                    )}
                                                                    {(t.params || []).length > 0 && (
                                                                        <div class="mt-2 space-y-1">
                                                                            {t.params.map(p => (
                                                                                <div key={p.name} class="text-xs font-mono text-[#585b70]">
                                                                                    <span class="text-[#cdd6f4]">{p.name}</span>
                                                                                    {p.type && <span class="text-[#a6e3a1]">: {p.type}</span>}
                                                                                    {!p.required && <span class="text-[#f9e2af]"> = {p.default}</span>}
                                                                                </div>
                                                                            ))}
                                                                        </div>
                                                                    )}
                                                                </details>
                                                            ))}
                                                        </div>
                                                    </div>
                                                )}

                                                {/* ---------- 2. SCORECARD ---------- */}
                                                <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                                    <SectionHeader icon="gauge" title="Scorecard"
                                                                   right={sc ? `${sc.run_count} run${sc.run_count === 1 ? '' : 's'} of ${sc.app}` : null} />
                                                    <div class="p-4 space-y-4">
                                                        {!sc ? (
                                                            <div class="text-center py-6 text-sm text-[#585b70]">Loading scorecard...</div>
                                                        ) : sc.run_count === 0 ? (
                                                            <div class="text-sm text-[#585b70]">
                                                                <span class="font-mono text-[#a6adc8]">{team.app}</span> has recorded no runs yet.
                                                                Traces are written by the wrapper that invokes it, so a team that has never
                                                                been launched has nothing to score.
                                                            </div>
                                                        ) : (
                                                            <>
                                                                {/* Pipeline health, a different question from the eval
                                                                    numbers below: those say whether runs succeeded, this
                                                                    says whether the pipeline's own stages are doing their
                                                                    jobs. A pipeline can pass every run and still score
                                                                    badly here — the disconnected-pipeline case. */}
                                                                {sc.self_assessment && sc.self_assessment.instrumented && (
                                                                    <div class={`rounded-lg border p-3 ${
                                                                        (sc.self_assessment.score_last ?? 1) < 1
                                                                            ? 'bg-[#f9e2af]/10 border-[#f9e2af]/40'
                                                                            : 'bg-[#11111b] border-[#313244]'
                                                                    }`}>
                                                                        <div class="flex items-center gap-2 flex-wrap text-xs">
                                                                            <i data-lucide="stethoscope" class="w-3.5 h-3.5 text-[#a6adc8]"></i>
                                                                            <span class="font-bold text-[#a6adc8]">Self-assessment</span>
                                                                            <span class="font-mono text-[#cdd6f4]">{fmtPct(sc.self_assessment.score_last)} last</span>
                                                                            <span class="text-[#585b70]">
                                                                                · {fmtPct(sc.self_assessment.score_avg)} over {sc.self_assessment.assessed_runs} run{sc.self_assessment.assessed_runs === 1 ? '' : 's'}
                                                                            </span>
                                                                        </div>
                                                                        {(sc.self_assessment.failing_stages || []).length > 0 && (
                                                                            <div class="mt-2 text-xs text-[#f9e2af]">
                                                                                not doing their job:{' '}
                                                                                {sc.self_assessment.failing_stages.map(f => (
                                                                                    <span key={f.stage} class="font-mono">{f.stage} ({f.runs}) </span>
                                                                                ))}
                                                                                <div class="text-[#585b70] mt-1">
                                                                                    A pipeline fault, not a data outcome — this is filed on the
                                                                                    Kanban board, not the review queue.
                                                                                </div>
                                                                            </div>
                                                                        )}
                                                                    </div>
                                                                )}
                                                                {/* Per-agent first: this pane is about THIS agent,
                                                                    and the team totals are context for it. Only
                                                                    when the traces actually name it — a workflow
                                                                    root takes no turns of its own, so four
                                                                    "not recorded" tiles would be noise standing
                                                                    where its real record (the team's) belongs. */}
                                                                {perAgent && (
                                                                    <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
                                                                        <StatTile icon="play" label="Runs took part in"
                                                                                  value={fmtNum(perAgent.runs)}
                                                                                  hint={`of ${sc.run_count} team runs`} />
                                                                        <StatTile icon="repeat" label="Turns"
                                                                                  value={fmtNum(perAgent.turns)} />
                                                                        <StatTile icon="wrench" label="Function calls"
                                                                                  value={fmtNum(perAgent.function_calls)} />
                                                                        <StatTile icon="coins" label="Tokens"
                                                                                  muted={perAgent.prompt_tokens === null}
                                                                                  hint={perAgent.model || undefined}
                                                                                  value={perAgent.prompt_tokens !== null
                                                                                         ? fmtNum((perAgent.prompt_tokens || 0) + (perAgent.completion_tokens || 0))
                                                                                         : 'not recorded'} />
                                                                    </div>
                                                                )}
                                                                {/* What those tokens were spent on. A token count with no
                                                                    model beside it cannot be priced or compared, which is
                                                                    the whole reason the run traces record the model per
                                                                    stage. Shown only for stages that called one — the
                                                                    deterministic stages take turns and spend nothing, and
                                                                    an empty "model: —" row on each of them would read as a
                                                                    gap in the data rather than the design. */}
                                                                {perAgent && perAgent.model && (
                                                                    <div class="rounded-lg border border-[#313244] bg-[#11111b] p-3 text-xs flex items-center gap-2 flex-wrap">
                                                                        <i data-lucide="cpu" class="w-3.5 h-3.5 text-[#585b70]"></i>
                                                                        <span class="text-[#585b70]">ran on</span>
                                                                        <span class="font-mono text-[#cdd6f4]">{perAgent.model}</span>
                                                                        {perAgent.api_call_count !== null && perAgent.api_call_count !== undefined && (
                                                                            <span class="text-[#585b70]">· {fmtNum(perAgent.api_call_count)} model call{perAgent.api_call_count === 1 ? '' : 's'}</span>
                                                                        )}
                                                                        {(perAgent.models || []).length > 1 && (
                                                                            <span class="text-[#f9e2af]">
                                                                                · totals span {perAgent.models.length} models: {perAgent.models.join(', ')}
                                                                            </span>
                                                                        )}
                                                                    </div>
                                                                )}
                                                                {/* The agent's own opinion of its turns. Kept visually
                                                                    apart from the tiles above and labelled as claimed,
                                                                    because it is the one number on this page the agent
                                                                    chose for itself. A stage that never offered one shows
                                                                    nothing rather than a zero. */}
                                                                {perAgent && perAgent.self_score !== null && perAgent.self_score !== undefined && (
                                                                    <div class="rounded-lg border border-[#313244] bg-[#11111b] p-3 space-y-1">
                                                                        <div class="flex items-center gap-2 text-xs flex-wrap">
                                                                            <i data-lucide="message-circle-question" class="w-3.5 h-3.5 text-[#585b70]"></i>
                                                                            <span class="font-bold text-[#a6adc8]">Self-reported</span>
                                                                            <span class="font-mono text-[#cdd6f4]">{fmtPct(perAgent.self_score)}</span>
                                                                            <span class="text-[#585b70]">
                                                                                over {perAgent.self_scored_runs} run{perAgent.self_scored_runs === 1 ? '' : 's'} — claimed by the agent, not measured
                                                                            </span>
                                                                        </div>
                                                                        {perAgent.could_improve && (
                                                                            <div class="text-xs text-[#a6adc8]">
                                                                                <span class="text-[#585b70]">could improve: </span>{perAgent.could_improve}
                                                                            </div>
                                                                        )}
                                                                    </div>
                                                                )}
                                                                {!perAgent && !isRoot && (
                                                                    <div class="text-sm text-[#585b70]">
                                                                        No instrumented run names <span class="font-mono text-[#a6adc8]">{agent.name}</span>,
                                                                        so it has no record of its own yet.
                                                                    </div>
                                                                )}

                                                                {/* Team-wide numbers belong to the app, so they
                                                                    show on the entry point and are a link
                                                                    elsewhere — repeating them under every member
                                                                    would read as four agents each scoring 75%. */}
                                                                {isRoot ? (
                                                                    <>
                                                                        {perAgent && (
                                                                            <div class="text-[10px] uppercase tracking-wider text-[#9ca3af] font-bold pt-1">Whole team</div>
                                                                        )}
                                                                        <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
                                                                            <StatTile icon="target" label="Success rate" value={fmtPct(sc.eval.success_rate)}
                                                                                      hint={`${sc.eval.ok + sc.eval.partial}/${sc.run_count} runs`} />
                                                                            <StatTile icon="timer" label="Duration p50" value={fmtMs(util.duration_ms.p50)}
                                                                                      hint={`p95 ${fmtMs(util.duration_ms.p95)}`} />
                                                                            <StatTile icon="activity" label="Model calls"
                                                                                      muted={!util.model_calls.instrumented}
                                                                                      value={util.model_calls.instrumented ? fmtNum(util.model_calls.total) : 'not recorded'}
                                                                                      hint={util.model_calls.instrumented ? `avg ${util.model_calls.avg}/run` : 'runs predate instrumentation'} />
                                                                            <StatTile icon="scale" label="Self-report accuracy"
                                                                                      muted={!sc.self_report.instrumented}
                                                                                      value={sc.self_report.instrumented ? fmtPct(sc.self_report.accuracy) : 'not recorded'}
                                                                                      hint={sc.self_report.instrumented
                                                                                            ? `${sc.self_report.agreed}/${sc.self_report.scored_runs} claims matched the tests`
                                                                                            : 'claimed vs measured not yet compared'} />
                                                                        </div>
                                                                        <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
                                                                            <StatTile icon="wrench" label="Tool calls" muted={!util.tool_calls.instrumented}
                                                                                      value={util.tool_calls.instrumented ? fmtNum(util.tool_calls.total) : 'not recorded'} />
                                                                            <StatTile icon="coins" label="Tokens" muted={!util.tokens.instrumented}
                                                                                      value={util.tokens.instrumented ? fmtNum(util.tokens.total) : 'not recorded'}
                                                                                      hint={util.tokens.instrumented ? null : 'Ollama often omits usage'} />
                                                                            <StatTile icon="repeat" label="Turns after pass" muted={!util.iterations.instrumented}
                                                                                      value={util.iterations.instrumented ? fmtNum(util.iterations.wasted_after_pass) : 'not recorded'}
                                                                                      hint="work done after tests went green" />
                                                                            <StatTile icon="hard-drive" label="Workspaces"
                                                                                      value={fmtNum(util.workspaces.count)}
                                                                                      hint={`${Math.round(util.workspaces.bytes / 1024)} KB on disk`} />
                                                                        </div>
                                                                    </>
                                                                ) : (
                                                                    <button
                                                                        onClick={() => setActiveTeamAgent(`${team.app}::${rootAgentName(team)}`)}
                                                                        class="text-xs text-[#585b70] hover:text-[#cdd6f4] flex items-center gap-1.5 transition text-left"
                                                                    >
                                                                        <i data-lucide="arrow-up-right" class="w-3.5 h-3.5 shrink-0"></i>
                                                                        <span>
                                                                            Success rate, duration and token totals belong to the whole team — see
                                                                            {' '}<span class="font-mono text-[#b4befe]">{rootAgentName(team)}</span>
                                                                        </span>
                                                                    </button>
                                                                )}

                                                                <div class="border border-[#313244] rounded-xl overflow-hidden">
                                                                    <SectionHeader icon="list"
                                                                                   title={isRoot ? `Recent runs (${sc.runs.length})` : `Runs including ${agent.name} (${agentRuns.length})`} />
                                                                    {(isRoot ? sc.runs : agentRuns).length === 0 ? (
                                                                        <div class="p-6 text-center text-sm text-[#585b70]">
                                                                            No instrumented run names this agent.
                                                                        </div>
                                                                    ) : (
                                                                        <div class="divide-y divide-[#313244]">
                                                                            {(isRoot ? sc.runs : agentRuns).map((r, i) => (
                                                                                <div key={r.run_id + i} class="px-4 py-3 flex items-center justify-between gap-4">
                                                                                    <div class="flex items-center gap-2.5 overflow-hidden">
                                                                                        <span class={`w-2.5 h-2.5 rounded-full shrink-0 ${
                                                                                            r.status === 'ok' ? 'bg-[#a6e3a1]'
                                                                                                : r.status === 'partial' ? 'bg-[#f9e2af]' : 'bg-[#ef4444]'
                                                                                        }`}></span>
                                                                                        <span class="text-sm text-[#cdd6f4] font-mono truncate">{r.run_id}</span>
                                                                                        {!r.trace_version && (
                                                                                            <span class="text-[10px] text-[#585b70] shrink-0">legacy</span>
                                                                                        )}
                                                                                    </div>
                                                                                    <div class="flex items-center gap-4 shrink-0 font-mono text-xs text-[#585b70]">
                                                                                        <span>{fmtMs(r.duration_ms)}</span>
                                                                                        <span>{stamp(r.started_at)}</span>
                                                                                    </div>
                                                                                </div>
                                                                            ))}
                                                                        </div>
                                                                    )}
                                                                </div>

                                                                {/* From the runs listed above, not the team's:
                                                                    on a sub-agent, a failure in a run it never
                                                                    took part in is not its error to answer for. */}
                                                                {(isRoot ? sc.runs : agentRuns).some(r => r.error) && (
                                                                    <div class="border border-[#313244] rounded-xl p-4">
                                                                        <div class="text-[10px] uppercase tracking-wider text-[#9ca3af] font-bold mb-2">Last error</div>
                                                                        <pre class="text-xs text-[#f38ba8] font-mono" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
{(isRoot ? sc.runs : agentRuns).find(r => r.error).error}
                                                                        </pre>
                                                                    </div>
                                                                )}
                                                            </>
                                                        )}
                                                    </div>
                                                </div>

                                                {/* ---------- 3. LAUNCH ---------- */}
                                                <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                                    <SectionHeader icon="alarm-clock" title="Launch"
                                                                   right={jobs.length ? `${jobs.length} scheduled job${jobs.length === 1 ? '' : 's'}` : null} />
                                                    <div class="p-4 space-y-3">
                                                        {jobs.length === 0 ? (
                                                            <div class="text-sm text-[#585b70]">
                                                                No scheduled job launches <span class="font-mono text-[#a6adc8]">{team.app}</span>.
                                                                It runs only when something invokes it directly.
                                                            </div>
                                                        ) : isRoot ? (
                                                            // A link, not a JobCard. This section answers
                                                            // "what launches this team"; the job's own
                                                            // settings, schedule and run history are the
                                                            // automation's page, and rendering them here
                                                            // as well is how the same job came to be
                                                            // described in three places at once.
                                                            jobs.map(j => {
                                                                const st = automationStatus(j, now);
                                                                const stStyle = STATUS_STYLE[st.kind] || STATUS_STYLE.never;
                                                                return (
                                                                <button
                                                                    key={j.id}
                                                                    onClick={() => navigateAutomation(j.id)}
                                                                    class="w-full text-left px-3 py-2.5 rounded-lg flex items-center gap-3 text-xs transition border bg-[#11111b] border-[#313244] hover:border-[#45475a]"
                                                                >
                                                                    <i data-lucide="alarm-clock" class="w-3.5 h-3.5 shrink-0 text-[#b4befe]"></i>
                                                                    <span class="font-semibold text-[#cdd6f4] truncate">{j.name || j.id}</span>
                                                                    <span class="font-mono text-[#a6e3a1] shrink-0">{formatSchedule(j)}</span>
                                                                    <span class={`ml-auto shrink-0 font-mono flex items-center gap-1.5 ${stStyle.color}`}>
                                                                        <i data-lucide={stStyle.icon} class="w-3.5 h-3.5"></i>
                                                                        {st.label}
                                                                    </span>
                                                                    <i data-lucide="chevron-right" class="w-3.5 h-3.5 shrink-0 text-[#585b70]"></i>
                                                                </button>
                                                                );
                                                            })
                                                        ) : (
                                                            <>
                                                                <div class="text-sm text-[#a6adc8]">
                                                                    Runs as part of <span class="font-mono text-[#cdd6f4]">{team.app}</span>, launched by
                                                                    {' '}{jobs.map(j => j.name || j.id).join(', ')}. Nothing schedules this agent on its own —
                                                                    it runs when <span class="font-mono text-[#cdd6f4]">{rootAgentName(team)}</span> delegates to it.
                                                                </div>
                                                                <button
                                                                    onClick={() => setActiveTeamAgent(`${team.app}::${rootAgentName(team)}`)}
                                                                    class="text-xs text-[#585b70] hover:text-[#cdd6f4] flex items-center gap-1.5 transition"
                                                                >
                                                                    <i data-lucide="arrow-up-right" class="w-3.5 h-3.5"></i>
                                                                    See the schedule on {rootAgentName(team)}
                                                                </button>
                                                            </>
                                                        )}
                                                    </div>
                                                </div>

                                                {/* Say plainly that there is no edit button, so nobody
                                                    hunts for one. Authoring and revising teams is the
                                                    worker's job — that is where the self-improving loop
                                                    already runs, and this port has no authentication. */}
                                                <div class="flex items-start gap-2 text-[11px] text-[#585b70] px-1">
                                                    <i data-lucide="lock" class="w-3.5 h-3.5 mt-0.5 shrink-0"></i>
                                                    <span>
                                                        Read-only view. Teams are revised by the worker agent, not edited here
                                                        {team.source === 'live'
                                                            ? '. This description is read live from the running server, so it reflects what is loaded — an edit on disk appears after a restart.'
                                                            : `. This is parsed from ${team.app}/agent.py, so an edit appears immediately, but ADK loads the file once at startup.`}
                                                    </span>
                                                </div>
                                            </>
                                        )}
                                    </div>
                                </div>
                                );
                            })()}

                            {/* VIEW 0: SUMMARY METRICS */}
                            {/* METRICS — OUTCOMES (the landing page).
                                What the fleet actually did, in the shared
                                vocabulary. System diagnostics live one click
                                away at /metrics/system: they matter when
                                something is wrong, and this page matters every
                                day, so the daily question gets the front page. */}
                            {activeTab === 'metrics' && metricsView !== 'system' && (
                                <MetricsOutcomesView
                                    metrics={metrics}
                                    metricsView={metricsView}
                                    navigateMetricsView={navigateMetricsView}
                                    review={review}
                                />
                            )}
                            {activeTab === 'metrics' && metricsView === 'system' && (
                                <MetricsSystemView
                                    metrics={metrics}
                                    metricsView={metricsView}
                                    navigateMetricsView={navigateMetricsView}
                                    navigateAutomation={navigateAutomation}
                                    navigateScorecard={navigateScorecard}
                                    automations={automations}
                                    cronJobs={cronJobs}
                                />
                            )}

                            {/* VIEW 1: CHAT CLIENT */}
                            {activeTab === 'chat' && (
                                <ChatMainPanel chat={chat} />
                            )}

                            {/* VIEW 2: UNIFIED KANBAN LIST-DETAIL PANEL */}
                            {activeTab === 'kanban' && (
                                <KanbanMainPanel
                                    kanban={kanban}
                                    activeKanbanTaskId={activeKanbanTaskId}
                                    now={now}
                                    selectSession={chat.selectSession}
                                    openReview={(id) => { setActiveReviewId(id); setActiveTab('review'); }}
                                />
                            )}

                            {activeTab === 'review' && (
                                <ReviewMainPanel
                                    review={review}
                                    activeReviewId={activeReviewId}
                                    setActiveReviewId={setActiveReviewId}
                                />
                            )}

                        </div>
                    </main>

                    {/* The review overlays: reject-with-reason, the
                        shortcut legend, and the alert toast. They live in
                        70-review-view.jsx with the state that drives them —
                        they used to be static divs rendered OUTSIDE <App/>,
                        toggled by window.* functions installed in a load
                        listener, while App kept its own rejectionModalId /
                        showHelp / showAlert that nothing rendered from. The
                        upshot was that clicking Reject did nothing at all. */}
                    <ReviewOverlays review={review} />

                    {/* Stack health. Sits above Settings in the stacking order:
                        the host being down is worth reading over a settings
                        page, and the reverse is never true. */}
                    {healthOpen && (
                        <HealthOverlay
                            health={health}
                            error={healthError}
                            refreshing={healthRefreshing}
                            onRefresh={() => fetchHealth(true)}
                            onClose={closeHealth}
                        />
                    )}

                    {/* Settings. Covers the whole shell — including the sidebar
                        and the tab bar — because it is its own place with its
                        own URL, not a panel over the tab you were on. */}
                    {settingsSection && (
                        <SettingsOverlay
                            section={settingsSection}
                            onSection={setSettingsSection}
                            onClose={closeSettings}
                            channels={{
                                list: channelList,
                                envPath: channelEnvPath,
                                loading: channelsLoading,
                                error: channelsError,
                                openId: openChannelId,
                                onOpen: setOpenChannelId,
                                onSave: saveChannel,
                                savingId: savingChannelId,
                                saveErrors: channelSaveErrors,
                                restartNeeded, restarting, restartDone,
                                onRestart: restartGateway,
                            }}
                            connections={{
                                list: mcpList,
                                autoReload: mcpAutoReload,
                                loading: mcpLoading,
                                error: mcpError,
                                openName: openMcpName,
                                onOpen: setOpenMcpName,
                                onSave: saveMcpServer,
                                onToggleEnabled: toggleMcpServer,
                                onRemove: removeMcpServer,
                                onAdd: addMcpServer,
                                onTest: testMcpServer,
                                savingName: savingMcpName,
                                testingName: testingMcpName,
                                testResults: mcpTestResults,
                                saveErrors: mcpSaveErrors,
                                adding: mcpAdding,
                                addError: mcpAddError,
                                onOpenIntegrations: () => navigateTab('integrations'),
                                // The read-only three sections, from one snapshot.
                                wf: wfIntegrations,
                                wfError: wfIntegrationsError,
                            }}
                            theme={theme}
                            onTheme={setTheme}
                            // The version block off the health snapshot, which is
                            // already polled for the header light. About needs no
                            // fetch of its own, and inherits the 15s refresh — so
                            // a migration finishing shows up without a reload.
                            version={health && health.version}
                        />
                    )}

                    {/* Reading pane for a context file picked in the sidebar */}
                    {/* Reading pane for a context file picked in the sidebar */}
                    <ChatDocOverlay chat={chat} />
                </div>
            );
        }

        // A render error used to unmount everything and leave a blank page with
        // no clue what happened. Show the error instead.
        class ErrorBoundary extends React.Component {
            constructor(props) {
                super(props);
                this.state = { error: null };
            }
            static getDerivedStateFromError(error) {
                return { error };
            }
            componentDidCatch(error, info) {
                console.error("Dashboard render error:", error, info);
            }
            render() {
                if (!this.state.error) return this.props.children;
                return (
                    <div class="h-full flex flex-col items-center justify-center gap-4 p-8 text-center">
                        <i data-lucide="triangle-alert" class="w-12 h-12 text-[#f38ba8]"></i>
                        <div class="text-[#cdd6f4] font-medium">The dashboard hit a rendering error.</div>
                        <pre class="text-xs text-[#f38ba8] font-mono bg-[#181825] border border-[#313244] rounded-lg p-4 max-w-2xl overflow-x-auto text-left whitespace-pre-wrap">
                            {String(this.state.error && this.state.error.stack || this.state.error)}
                        </pre>
                        <button
                            onClick={() => window.location.reload()}
                            class="bg-[#313244] hover:bg-[#45475a] text-[#cdd6f4] text-sm font-medium py-2 px-4 rounded-lg border border-[#45475a]"
                        >
                            Reload
                        </button>
                    </div>
                );
            }
        }

        // App Renderer mounting
        var root = ReactDOM.createRoot(document.getElementById('root'));
        root.render(
            <React.StrictMode>
                <ErrorBoundary><App /></ErrorBoundary>
                
            </React.StrictMode>
        );

