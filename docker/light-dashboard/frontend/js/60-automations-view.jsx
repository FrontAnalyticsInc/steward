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

        // --- The automation library -----------------------------------------
        // Four templates ship with every box and, until this panel existed,
        // nothing could see them: the tab said "Nothing scheduled on this host"
        // on a machine that arrived with four automations in it.
        //
        // A template is not a job. It has no id in cron/jobs.json, no
        // next_run_at, and required parameters with no values — the scheduler
        // never reads the directory at all. So this is not a list of switched-
        // off jobs with an on switch; it is a list of questions, and answering
        // them is what produces a job. The backend keeps that property: the one
        // route from here to the scheduler is `render_job`, which refuses while
        // a required answer is missing and names every missing one at once.
        // See backend/automation_library.py.
        function useAutomationLibrary(active) {
            const [library, setLibrary] = useState(null);
            const [error, setError] = useState(null);

            const load = useCallback(async () => {
                try {
                    const res = await fetch('/api/automations/library');
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) {
                        setError(data.detail || `Could not read the library (${res.status})`);
                        return;
                    }
                    setLibrary(data);
                    setError(null);
                } catch (err) {
                    setError(`Could not reach the dashboard API: ${err}`);
                }
            }, []);

            // Once per visit to the tab, not on the 7s poll: a directory of
            // YAML files does not change while someone is reading it, and a
            // form being filled in must not be re-rendered underneath them.
            useEffect(() => { if (active) load(); }, [active, load]);
            return { library, libraryError: error, reloadLibrary: load };
        }

        // Which tier badge a template carries, in the vocabulary 20-automations
        // already owns. The library's `tier` is written in the format's own
        // words (`prompt-cron` / `adk-pipeline`); `automationWhere` labels a job
        // (`agent` / `workflow`). Mapping one onto the other here means the
        // template and the job it becomes wear the same badge without either
        // classifier being special-cased: the created job has no adk_app and no
        // no_agent/script — the format forbids both — so it classifies as a
        // prompt cron on its own, from the job alone.
        var templateWhere = (tier) => ({
            label: tier === 'adk-pipeline' ? 'workflow' : 'agent',
        });

        // What a parameter's answer looks like on screen. The question itself is
        // never written here: every parameter carries its own `ask`, written by
        // the template's author to be read by whoever fills the form, and a
        // label invented in the frontend would quietly replace it.
        function ParameterField({ param, value, onChange }) {
            const id = `tpl-param-${param.name}`;
            const common = 'w-full bg-[#11111b] border border-[#313244] rounded-lg px-3 py-2 text-sm text-[#cdd6f4] placeholder-[#585b70] focus:outline-none focus:border-[#585b70]';
            const example = param.example === undefined || param.example === null
                ? null
                : (Array.isArray(param.example) ? param.example.join(', ') : String(param.example));
            let field;
            if (param.type === 'boolean') {
                field = (
                    <label class="flex items-center gap-2 text-sm text-[#cdd6f4]">
                        <input
                            id={id}
                            type="checkbox"
                            checked={value === true}
                            onChange={(e) => onChange(e.target.checked)}
                        />
                        {value === true ? 'Yes' : 'No'}
                    </label>
                );
            } else if (param.type === 'integer') {
                field = (
                    <input
                        id={id} type="number" class={common}
                        value={value === undefined || value === null ? '' : value}
                        placeholder={example || ''}
                        onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
                    />
                );
            } else {
                field = (
                    <input
                        id={id}
                        type={param.type === 'url' ? 'url' : 'text'}
                        class={common}
                        value={value === undefined || value === null ? '' : value}
                        placeholder={example || ''}
                        onChange={(e) => onChange(e.target.value)}
                    />
                );
            }
            return (
                <div class="mb-4">
                    <label for={id} class="block text-sm font-semibold text-[#cdd6f4] mb-1">
                        {param.ask}
                        {param.required
                            ? <span class="ml-2 text-[10px] uppercase tracking-wider text-[#f38ba8]">required</span>
                            : <span class="ml-2 text-[10px] uppercase tracking-wider text-[#585b70]">optional</span>}
                    </label>
                    {/* The author's own note about what a good answer looks
                        like. Templates that bothered to write one are the ones
                        whose parameters are easiest to get subtly wrong. */}
                    {param.help && (
                        <p class="text-xs text-[#9ca3af] mb-1.5 whitespace-pre-line">{param.help}</p>
                    )}
                    {param.type === 'list' && (
                        <p class="text-xs text-[#585b70] mb-1.5">Separate answers with commas.</p>
                    )}
                    {field}
                </div>
            );
        }

        // One template, filled in. Everything the operator is asked here comes
        // from the template; everything they are told when it will not go comes
        // from the backend's refusal, verbatim.
        function TemplateForm({ template, deliveryChoices, onCancel, onCreated }) {
            usePaintedIcons();
            const [values, setValues] = useState(() => {
                // Optional parameters start at their declared default, so the
                // form shows what will actually be used rather than a blank
                // that silently becomes something else. Required ones start
                // empty by definition — the format forbids them a default.
                const out = {};
                for (const p of template.parameters || []) {
                    if (!p.required && p.default !== undefined) {
                        out[p.name] = Array.isArray(p.default) ? p.default.join(', ') : p.default;
                    }
                }
                return out;
            });
            const [schedule, setSchedule] = useState(template.schedule.default || '');
            const [deliver, setDeliver] = useState(template.delivery.default || 'local');
            const [problems, setProblems] = useState(null);
            const [message, setMessage] = useState(null);
            const [saving, setSaving] = useState(false);

            const set = (name, v) => setValues(prev => ({ ...prev, [name]: v }));

            const submit = async () => {
                if (saving) return;
                setSaving(true);
                setProblems(null);
                setMessage(null);
                // Empty is not an answer: an omitted optional parameter takes
                // its default, and an omitted required one is what the refusal
                // below is about. Sending "" instead would turn "you have not
                // answered this" into "you answered with an empty string",
                // which is a different and much worse error message.
                const payload = {};
                for (const [k, v] of Object.entries(values)) {
                    if (v === '' || v === undefined || v === null) continue;
                    payload[k] = v;
                }
                try {
                    const res = await fetch(
                        `/api/automations/library/${template.id}/create`,
                        {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                values: payload,
                                schedule: template.schedule.editable ? schedule : null,
                                deliver: template.delivery.editable ? deliver : null,
                            }),
                        },
                    );
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) {
                        const detail = data.detail;
                        if (detail && detail.problems) {
                            setProblems(detail.problems);
                            setMessage(detail.message || null);
                        } else {
                            setMessage(typeof detail === 'string' ? detail : `Could not create the job (${res.status})`);
                        }
                        return;
                    }
                    onCreated(data.job || {});
                } catch (err) {
                    setMessage(`Could not reach the dashboard API: ${err}`);
                } finally {
                    setSaving(false);
                }
            };

            return (
                <div class="bg-[#181825] border border-[#313244] rounded-xl p-5 mb-5">
                    <div class="flex items-start justify-between gap-4 mb-4">
                        <div>
                            <h3 class="text-base font-bold text-[#cdd6f4]">{template.title}</h3>
                            <p class="text-xs text-[#9ca3af] mt-1">{template.summary}</p>
                        </div>
                        <button
                            onClick={onCancel}
                            class="text-xs font-semibold py-1.5 px-3 rounded-lg border border-[#313244] text-[#a6adc8] hover:text-[#cdd6f4] hover:border-[#45475a] transition"
                        >
                            Cancel
                        </button>
                    </div>

                    {(template.parameters || []).map(p => (
                        <ParameterField
                            key={p.name}
                            param={p}
                            value={values[p.name]}
                            onChange={(v) => set(p.name, v)}
                        />
                    ))}

                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
                        <div>
                            <label class="block text-sm font-semibold text-[#cdd6f4] mb-1">
                                How often
                            </label>
                            {template.schedule.note && (
                                <p class="text-xs text-[#9ca3af] mb-1.5">{template.schedule.note}</p>
                            )}
                            <input
                                type="text"
                                disabled={!template.schedule.editable}
                                value={schedule}
                                onChange={(e) => setSchedule(e.target.value)}
                                class="w-full bg-[#11111b] border border-[#313244] rounded-lg px-3 py-2 text-sm font-mono text-[#a6e3a1] disabled:opacity-60 focus:outline-none focus:border-[#585b70]"
                            />
                            <p class="text-xs text-[#585b70] mt-1">
                                A cron expression, or "every 2 days".
                            </p>
                        </div>
                        <div>
                            <label class="block text-sm font-semibold text-[#cdd6f4] mb-1">
                                Where the output goes
                            </label>
                            {/* The steering position, on screen: an automation
                                that delivers only to a page someone has to
                                remember to open is the behaviour this product
                                exists to remove. A channel that cannot deliver
                                today still appears, saying so, rather than
                                being hidden — hiding it looks like the box does
                                not support Telegram at all. */}
                            <select
                                disabled={!template.delivery.editable}
                                value={deliver}
                                onChange={(e) => setDeliver(e.target.value)}
                                class="w-full bg-[#11111b] border border-[#313244] rounded-lg px-3 py-2 text-sm text-[#cdd6f4] disabled:opacity-60 focus:outline-none focus:border-[#585b70]"
                            >
                                {(deliveryChoices || []).map(c => (
                                    <option key={c.id} value={c.id}>
                                        {c.label}{c.deliverable ? '' : ' — not deliverable yet'}
                                    </option>
                                ))}
                                {/* The template's own default, when it is
                                    something no channel row describes (`origin`
                                    — back to whoever asked for it). */}
                                {!(deliveryChoices || []).some(c => c.id === template.delivery.default) && (
                                    <option value={template.delivery.default}>
                                        {template.delivery.default === 'origin'
                                            ? 'Back to where it was created (origin)'
                                            : template.delivery.default}
                                    </option>
                                )}
                            </select>
                            <p class="text-xs text-[#585b70] mt-1">
                                {deliver === 'origin'
                                    // What `origin` resolves to for a job made
                                    // here, rather than the word itself: this
                                    // console is the surface that created it,
                                    // so the output comes back to the chat
                                    // transcript and nowhere else.
                                    ? 'Back to where it was created — the console’s chat. Pick a channel if it should reach you without opening this page.'
                                    : (((deliveryChoices || []).find(c => c.id === deliver) || {}).detail || '')}
                            </p>
                        </div>
                    </div>

                    {/* The refusal, in the template's own words. `render_job`
                        returns every problem at once — four empty fields are
                        four lines here, not four attempts. */}
                    {problems && (
                        <div class="border border-[#f38ba8]/50 bg-[#f38ba8]/10 rounded-lg p-3 mb-4">
                            <p class="text-sm font-semibold text-[#f38ba8] mb-1">
                                {message || 'This automation is not ready to run yet.'}
                            </p>
                            <ul class="text-xs text-[#f5c2e7] list-disc pl-5 space-y-1">
                                {problems.map((p, i) => <li key={i}>{p}</li>)}
                            </ul>
                        </div>
                    )}
                    {!problems && message && (
                        <div class="border border-[#f38ba8]/50 bg-[#f38ba8]/10 rounded-lg p-3 mb-4 text-sm text-[#f38ba8]">
                            {message}
                        </div>
                    )}

                    <div class="flex items-center gap-3">
                        <button
                            onClick={submit}
                            disabled={saving}
                            class="text-xs font-semibold py-2 px-4 rounded-lg bg-[#89b4fa] text-[#11111b] hover:bg-[#b4befe] disabled:opacity-50 transition"
                        >
                            {saving ? 'Creating…' : 'Create it, switched off'}
                        </button>
                        <span class="text-xs text-[#585b70]">
                            It is created disabled. Nothing runs until you switch it on.
                        </span>
                    </div>
                </div>
            );
        }

        // The library as a set of offers. Deliberately not a table: these are
        // not things that ran, so every column the automations table earns —
        // status, last run, schedule — would be empty or a lie here.
        function AutomationLibrary({ library, libraryError, onCreated }) {
            usePaintedIcons();
            const [openId, setOpenId] = useState(null);
            const templates = (library && library.templates) || [];
            const open = templates.find(t => t.id === openId) || null;

            if (libraryError) {
                return (
                    <div class="mt-6 text-xs text-[#f38ba8]">
                        {String(libraryError)}
                    </div>
                );
            }
            if (!library) return null;

            return (
                <div class="mt-8">
                    <h3 class="text-sm font-bold text-[#cdd6f4]">Ready to set up</h3>
                    <p class="text-xs text-[#585b70] mt-0.5 mb-4">
                        {templates.length === 0
                            ? `No templates in ${library.dir}.`
                            : 'Answer each one’s questions and it becomes a scheduled automation — created switched off.'}
                    </p>

                    {open && (
                        <TemplateForm
                            template={open}
                            deliveryChoices={library.delivery_choices}
                            onCancel={() => setOpenId(null)}
                            onCreated={(job) => { setOpenId(null); onCreated(job); }}
                        />
                    )}

                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {templates.filter(t => t.id !== openId).map(t => (
                            <div key={t.id} class="bg-[#181825] border border-[#313244] rounded-xl p-4 flex flex-col">
                                <div class="flex items-start justify-between gap-3 mb-2">
                                    <h4 class="text-sm font-bold text-[#cdd6f4]">{t.title}</h4>
                                    {/* No badge for a file that did not parse:
                                        its tier is unknown, and the default
                                        would state a tier nobody wrote. */}
                                    {t.tier && <TierBadge where={templateWhere(t.tier)} size="sm" />}
                                </div>
                                <p class="text-xs text-[#9ca3af] flex-1">{t.summary}</p>
                                <div class="flex items-center justify-between gap-3 mt-3">
                                    {/* What it asks for and what stops it
                                        repeating itself — the two things that
                                        decide whether this template is worth
                                        starting, before any form opens. */}
                                    <span class="text-[10px] font-mono text-[#585b70]">
                                        {(t.parameters || []).filter(p => p.required).length} question
                                        {(t.parameters || []).filter(p => p.required).length === 1 ? '' : 's'}
                                        {(t.change_detection || {}).mechanism && t.change_detection.mechanism !== 'none'
                                            ? ` · only when something changes (${t.change_detection.mechanism})`
                                            : ''}
                                    </span>
                                    <button
                                        onClick={() => setOpenId(t.id)}
                                        disabled={(t.problems || []).length > 0}
                                        class="text-xs font-semibold py-1.5 px-3 rounded-lg border border-[#313244] text-[#a6adc8] hover:text-[#cdd6f4] hover:border-[#45475a] disabled:opacity-40 transition"
                                    >
                                        Set it up
                                    </button>
                                </div>
                                {/* A template someone edited on the box into
                                    something `render_job` will not render. Said
                                    here rather than at the end of a filled-in
                                    form. */}
                                {(t.problems || []).length > 0 && (
                                    <ul class="mt-2 text-[10px] text-[#f38ba8] list-disc pl-4">
                                        {t.problems.map((p, i) => <li key={i}>{p}</li>)}
                                    </ul>
                                )}
                            </div>
                        ))}
                    </div>
                </div>
            );
        }

        // Everything scheduled on this host, one row each. The rows are ranked
        // and frozen by App — the order must not change under a reader who is
        // looking at it — so this renders what it is given and sorts nothing.
        function AutomationsListView({
            automations, staleCount, hasDrift, failedGrants, now,
            navigateTab, navigateMetricsView, navigateAutomation,
            active, refreshCronJobs,
        }) {
            usePaintedIcons();
            const { library, libraryError, reloadLibrary } = useAutomationLibrary(active);
            // What just happened, said once at the top of the list rather than
            // inside the card that has since closed.
            const [notice, setNotice] = useState(null);
            // Which job's on/off switch is mid-flight. Keyed by id: the list
            // polls every 7s, and a switch that reverted for a frame while the
            // gateway answered would read as the change not having taken.
            const [toggling, setToggling] = useState(null);

            const setEnabled = async (job, enabled) => {
                if (toggling) return;
                setToggling(job.id);
                setNotice(null);
                try {
                    const res = await fetch(`/api/cron/jobs/${job.id}/enabled`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ enabled }),
                    });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) {
                        setNotice({ ok: false, text: data.detail || `Could not switch it ${enabled ? 'on' : 'off'} (${res.status})` });
                    } else {
                        setNotice({
                            ok: true,
                            text: enabled
                                ? `${job.name || job.id} is on. It runs on its own schedule from now on.`
                                : `${job.name || job.id} is off. Nothing will run until you switch it back on.`,
                        });
                        if (refreshCronJobs) refreshCronJobs();
                    }
                } catch (err) {
                    setNotice({ ok: false, text: `Could not reach the dashboard API: ${err}` });
                } finally {
                    setToggling(null);
                }
            };

            return (
                        <div class="h-full overflow-y-auto p-6">
                            <div class="flex items-center justify-between gap-4 mb-5 flex-wrap">
                                <div>
                                    <h2 class="text-lg font-bold text-[#cdd6f4]">Automations</h2>
                                    <p class="text-xs text-[#585b70] mt-0.5">
                                        {automations.length === 0
                                            ? 'Nothing scheduled yet — the library below is where the first one comes from.'
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

                            {notice && (
                                <div class={`mb-4 rounded-lg border px-3 py-2 text-sm ${
                                    notice.ok
                                        ? 'border-[#a6e3a1]/50 bg-[#a6e3a1]/10 text-[#a6e3a1]'
                                        : 'border-[#f38ba8]/50 bg-[#f38ba8]/10 text-[#f38ba8]'
                                }`}>
                                    {notice.text}
                                </div>
                            )}

                            {automations.length === 0 ? (
                                <div class="text-center py-10 text-sm text-[#585b70]">
                                    Nothing is scheduled on this host yet.
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
                                                    {/* Kind, not "Where". The column used to answer
                                                        where a job runs, in a vocabulary
                                                        ("workflow"/"agent") the reader had to already
                                                        know. What an operator needs first is which of
                                                        the two ways of making an automation this is:
                                                        a prompt cron and an ADK pipeline cost
                                                        differently, fail differently and are debugged
                                                        differently, and presenting them identically
                                                        hides that. Same classifier, said in the words
                                                        the docs and the assistant use. */}
                                                    <th class="text-left px-4 py-3 font-bold">Kind</th>
                                                    <th class="text-left px-4 py-3 font-bold">Schedule</th>
                                                    <th class="text-left px-4 py-3 font-bold">Last run</th>
                                                    {/* One click, no confirmation and no detour
                                                        through a detail page: an automation the
                                                        box shipped switched off is worth nothing
                                                        until switching it on is the easiest thing
                                                        on the screen. */}
                                                    <th class="text-right px-4 py-3 font-bold">On</th>
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
                                                                    <TierBadge where={where} size="sm" />
                                                                    {where.detail && (
                                                                        <span class="block mt-1 text-[10px] font-mono text-[#585b70] truncate" style={{ maxWidth: '18rem' }}>
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
                                                                <td class="px-4 py-3 text-right">
                                                                    <button
                                                                        onClick={(e) => {
                                                                            // The row navigates; this must not.
                                                                            e.stopPropagation();
                                                                            setEnabled(job, job.enabled === false);
                                                                        }}
                                                                        disabled={toggling === job.id}
                                                                        title={job.enabled === false
                                                                            ? 'Switch this automation on'
                                                                            : 'Switch this automation off'}
                                                                        class={`text-[10px] font-bold uppercase tracking-wider py-1 px-2.5 rounded-full border transition disabled:opacity-50 ${
                                                                            job.enabled === false
                                                                                ? 'border-[#313244] text-[#585b70] hover:text-[#a6e3a1] hover:border-[#a6e3a1]/50'
                                                                                : 'border-[#a6e3a1]/50 bg-[#a6e3a1]/10 text-[#a6e3a1] hover:border-[#f38ba8]/50 hover:text-[#f38ba8]'
                                                                        }`}
                                                                    >
                                                                        {toggling === job.id
                                                                            ? '…'
                                                                            : (job.enabled === false ? 'off' : 'on')}
                                                                    </button>
                                                                </td>
                                                            </tr>
                                                    );
                                                })}
                                            </tbody>
                                        </table>
                                    </div>
                                </div>
                            )}

                            {/* The four templates every box ships with. They sit
                                under the list rather than beside it because the
                                list is what you came for once anything is
                                scheduled — and above the fold on a fresh box,
                                where the list is one line of text. */}
                            <AutomationLibrary
                                library={library}
                                libraryError={libraryError}
                                onCreated={(job) => {
                                    setNotice({
                                        ok: true,
                                        text: `${job.name || 'The automation'} was created, switched off. `
                                            + 'It is in the list above — switch it on when you are ready.',
                                    });
                                    if (refreshCronJobs) refreshCronJobs();
                                    reloadLibrary();
                                }}
                            />
                        </div>
            );
        }

        // What the steps row says when there are no steps to draw.
        //
        // It used to say "Loading steps…" unconditionally, which was true for
        // about two seconds after a cold load and a lie every second after
        // that. The state it actually described — the job names a pipeline, the
        // team list came back, and nothing in it matched — is permanent, and it
        // was reached by every tenant-authored pipeline on the box because the
        // console had no source to read them from. A spinner that never
        // resolves is worse than an error: it sends the reader hunting a slow
        // network instead of a missing mount.
        //
        // So: only say "loading" while something is genuinely in flight, and
        // otherwise say what is true and, where the answer is knowable, why.
        function UndescribedSteps({ job, adkTeams, adkLoading }) {
            const teams = adkTeams || [];
            if (adkLoading && teams.length === 0) {
                return <span class="text-[11px] text-[#585b70]">Loading steps…</span>;
            }
            // fetch_teams answers with one error card rather than an empty list
            // when it cannot reach the runner, so this is a distinguishable
            // state and not a guess.
            const down = teams.find(t => t.status === 'error');
            const reason = !job.adk_app
                ? 'this automation does not name a pipeline'
                : down
                    ? 'the runner is not answering'
                    : `the runner does not describe a pipeline named ${job.adk_app}`;
            return (
                <div class="flex items-start gap-1.5 text-[11px] text-[#f9e2af] max-w-[22rem]">
                    <i data-lucide="alert-triangle" class="w-3.5 h-3.5 shrink-0 mt-px"></i>
                    <span>
                        <span class="font-semibold">Steps unavailable</span>
                        {' — '}{reason}.
                        {' '}
                        <span class="text-[#585b70]">
                            The automation still runs; only this diagram is missing.
                        </span>
                    </span>
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
            activeAutomationId, active, cronJobs, adkTeams, adkLoading, now,
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
                                            {/* The same badge the list row carries, so the
                                                tier a reader picked the row out by is still
                                                named on the page they land on. */}
                                            <TierBadge where={where} size="sm" />
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
                                                    <UndescribedSteps
                                                        job={job}
                                                        adkTeams={adkTeams}
                                                        adkLoading={adkLoading}
                                                    />
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
