// --- 20-automations.jsx ----------------------------------------------------
// One automation, drawn: the pipeline as steps, the trigger/work/delivery
// flow, the job card, and the execution history with its ADK traces.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
        // --- What an automation actually does, as steps ---
        // ADK reports a team as a flat agent list with `parent` and `order`, so
        // the pipeline shape is there but has to be rebuilt before it can be
        // drawn. Order comes from `order` rather than array position: the list
        // arrives in declaration order, which for a nested pipeline is not
        // execution order.
        var stepTree = (agents, parent) => (agents || [])
            .filter(a => (a.parent || null) === (parent || null))
            .sort((a, b) => (a.order || 0) - (b.order || 0));

        // What kind of thing a step is, in the only terms that matter when you
        // are reading a pipeline for safety: does it run a model, or is it
        // deterministic code? ADK's own class names carry this — LlmAgent runs a
        // model, a Sequential/Loop/Parallel agent is a container that runs
        // others, and anything else is a hand-written deterministic stage.
        var STEP_KINDS = {
            model:         { color: 'var(--acc-mauve)', icon: 'sparkles', label: 'model' },
            container:     { color: 'var(--acc-blue)', icon: 'layers', label: 'group' },
            deterministic: { color: 'var(--acc-green)', icon: 'code', label: 'code' },
        };
        // A named model counts as well as the class: a subclass of LlmAgent is
        // still a model stage, and calling it deterministic would understate what
        // that step can be talked into doing.
        var stepKind = (agent) => {
            if (agent.is_workflow) return 'container';
            if (agent.agent_class === 'LlmAgent' || agent.model) return 'model';
            return 'deterministic';
        };

        // One step. Clickable when the caller can say where it goes — every step
        // is an agent, so this is the per-step version of "link to the agent".
        function StepNode({ agent, onOpen }) {
            const kind = STEP_KINDS[stepKind(agent)];
            const Tag = onOpen ? 'button' : 'div';
            return (
                <Tag
                    onClick={onOpen || undefined}
                    title={`${agent.name} — ${agent.agent_class || 'agent'}`}
                    class={`bg-[#181825] border border-[#313244] rounded-lg px-2.5 py-1.5 flex items-center gap-1.5 shrink-0 text-left ${
                        onOpen ? 'hover:border-[#585b70] transition cursor-pointer' : ''
                    }`}
                >
                    <i data-lucide={kind.icon} class="w-3 h-3 shrink-0" style={{ color: kind.color }}></i>
                    <span class="text-[11px] font-mono text-[#cdd6f4] whitespace-nowrap">{agent.name}</span>
                </Tag>
            );
        }

        // How a parent's children relate to each other, which decides whether an
        // arrow between them is true. Getting this from the parent's class is the
        // whole point: a SequentialAgent's children run in order, but a router's
        // are alternatives and only one of them runs. Drawing arrows between
        // those would state, in the most legible part of the diagram, something
        // that is not true of the pipeline.
        var childRelation = (parentAgent) => {
            switch (parentAgent && parentAgent.agent_class) {
                case 'SequentialAgent': return { connector: 'arrow', note: null };
                case 'LoopAgent':       return { connector: 'arrow', note: 'repeats until done' };
                case 'ParallelAgent':   return { connector: 'none',  note: 'all at once' };
                // A routing root (LlmAgent/Agent holding sub_agents) picks one.
                default:                return { connector: 'none',  note: 'routes to one of' };
            }
        };

        // The chain itself. Steps wrap rather than scroll, so a seven-stage
        // pipeline stays legible inside an expanded table row without a
        // horizontal scrollbar of its own.
        //
        // A container step recurses: its children are drawn boxed beneath it, so
        // nesting stays visible instead of being flattened into one chain that
        // implies an order the pipeline does not have.
        function StepFlow({ agents, parent, parentAgent, onOpenAgent, depth }) {
            const steps = stepTree(agents, parent);
            if (!steps.length) return null;
            const rel = childRelation(parentAgent);
            return (
                <div class="flex flex-col gap-1">
                    {rel.note && steps.length > 1 && (
                        <span class="text-[9px] uppercase tracking-wider text-[#585b70] font-bold">{rel.note}</span>
                    )}
                    <div class="flex items-center gap-1.5 flex-wrap">
                        {steps.map((a, i) => {
                            const children = stepTree(agents, a.name);
                            return (
                                // The arrow and the step it points at are one flex
                                // item, so a wrap moves them together. As siblings
                                // the row could break between them, leaving an
                                // arrow pointing off the end of a line and its
                                // target orphaned at the start of the next.
                                <div key={a.name || i} class="flex items-center gap-1.5 min-w-0">
                                    {i > 0 && rel.connector === 'arrow' && (
                                        <i data-lucide="arrow-right" class="w-3 h-3 text-[#45475a] shrink-0"></i>
                                    )}
                                    {children.length ? (
                                        // A container is a box its steps sit *inside*,
                                        // with its own name as the label on that box.
                                        // Drawn as a node stacked above them it read as
                                        // one more step in the sequence, which is the
                                        // one thing a container is not.
                                        <div class="border border-dashed border-[#45475a] rounded-lg overflow-hidden">
                                            <div class="px-2 py-1 bg-[#181825]/60 border-b border-dashed border-[#45475a] flex items-center gap-1.5">
                                                <i data-lucide={STEP_KINDS[stepKind(a)].icon} class="w-3 h-3 shrink-0" style={{ color: STEP_KINDS[stepKind(a)].color }}></i>
                                                <button
                                                    onClick={onOpenAgent && (() => onOpenAgent(a))}
                                                    class="text-[11px] font-mono text-[#cdd6f4] hover:underline"
                                                >{a.name}</button>
                                                <span class="text-[9px] text-[#585b70]">{a.agent_class}</span>
                                            </div>
                                            <div class="p-2">
                                                <StepFlow
                                                    agents={agents}
                                                    parent={a.name}
                                                    parentAgent={a}
                                                    onOpenAgent={onOpenAgent}
                                                    depth={(depth || 0) + 1}
                                                />
                                            </div>
                                        </div>
                                    ) : (
                                        <StepNode agent={a} onOpen={onOpenAgent && (() => onOpenAgent(a))} />
                                    )}
                                </div>
                            );
                        })}
                    </div>
                </div>
            );
        }

        // --- The ends of the flow: what starts a run, and where it goes ---
        // Every automation has this shape, including the ones with no pipeline
        // inside. A bare prompt job is still something fired by a clock that
        // reports somewhere afterwards, and drawing only the middle left the two
        // questions the diagram most obviously raises unanswered.

        // A flow endpoint. Same visual weight as a step, different colour, so a
        // trigger cannot be mistaken for the first stage of the work.
        //
        // Given an onClick the node becomes a button: the endpoints of a flow name
        // real things, and a node that names the chat a job reports into should
        // open it rather than making you go find it in the rail. Rendered as a
        // <button> and not a styled <div> so it is reachable by keyboard, and the
        // hover border is the only visual difference — a node that leads somewhere
        // should still read as the same node.
        //
        // `primary` marks the node as the work itself rather than one of its ends.
        // Drawn at the same size as the trigger and the delivery node, a script sat
        // in the middle of the chain read as one of three equal boxes, when it is
        // the one thing the automation exists to run. It gets a floor on its size —
        // wider and taller than an endpoint can ever be — and stacks its subtitle
        // beneath the label rather than trailing it, so the extra height is used
        // rather than padded.
        function FlowNode({ icon, color, label, sub, tone, title, onClick, primary }) {
            const Tag = onClick ? 'button' : 'div';
            return (
                <Tag
                    title={title || undefined}
                    onClick={onClick || undefined}
                    type={onClick ? 'button' : undefined}
                    class={`rounded-lg flex shrink-0 border ${
                        primary
                            ? 'px-4 py-3 min-w-[150px] min-h-[64px] flex-col items-center justify-center gap-1 text-center'
                            : 'px-2.5 py-1.5 items-center gap-1.5'
                    } ${
                        tone === 'bad'
                            ? 'bg-[#f38ba8]/10 border-[#f38ba8]/40'
                            : primary
                                ? 'bg-[#11111b] border-[#45475a]'
                                : 'bg-[#11111b] border-[#313244]'
                    } ${onClick ? 'cursor-pointer hover:border-[#89b4fa] transition-colors' : ''}`}
                >
                    {primary ? (
                        <>
                            <i data-lucide={icon} class="w-4 h-4 shrink-0" style={{ color }}></i>
                            <span class="text-xs font-mono text-[#cdd6f4] whitespace-nowrap">{label}</span>
                            {sub && <span class="text-[9px] text-[#585b70] whitespace-nowrap">{sub}</span>}
                        </>
                    ) : (
                        <>
                            <i data-lucide={icon} class="w-3 h-3 shrink-0" style={{ color }}></i>
                            <span class="text-[11px] font-mono text-[#cdd6f4] whitespace-nowrap">{label}</span>
                            {sub && <span class="text-[9px] text-[#585b70] whitespace-nowrap">{sub}</span>}
                        </>
                    )}
                </Tag>
            );
        }

        // What starts a run. The schedule is the trigger — and whether a model is
        // in the loop is worth saying here rather than inferred from the steps,
        // because a `no_agent` job is plumbing that invokes the work, not an
        // agent deciding to.
        function TriggerNode({ job }) {
            return (
                <FlowNode
                    icon="alarm-clock"
                    color="var(--acc-yellow)"
                    label={formatSchedule(job)}
                    sub={job.no_agent || job.runs_agent === false ? 'no model' : null}
                    title="The schedule that fires this automation"
                />
            );
        }

        // Where a run's output goes. Derived from the job's own delivery config
        // and from whether its runs are recorded — not from the pipeline, which
        // cannot know. A job that reports to a chat and fails to deliver is the
        // case worth drawing loudly: the work succeeded and nobody was told, and
        // nothing else on this page would show it.
        var outputNodes = (job) => {
            const out = [];
            const deliver = job.deliver;
            if (deliver === 'origin') {
                const failed = !!job.last_delivery_error;
                // The origin chat id IS the session id (the dashboard mints
                // `dash-<base36>-<rand>` and hands it to cron as the origin), so it
                // can address the chat directly. Carried on the node rather than
                // resolved here because this function runs outside the component and
                // has no navigation to call — the render site attaches the handler.
                //
                // A job whose origin was never captured has nothing to open; it gets
                // an unlinked node rather than a link to a session that isn't there.
                const sessionId = (job.origin || {}).chat_id || null;
                out.push({
                    icon: failed ? 'message-square-x' : 'message-square',
                    color: failed ? 'var(--acc-red)' : 'var(--acc-blue)',
                    label: 'chat',
                    sub: failed ? 'delivery failing' : sessionId,
                    tone: failed ? 'bad' : null,
                    sessionId,
                    title: failed
                        ? `Reports back to the chat that created it, but delivery is failing:\n${job.last_delivery_error}`
                        : sessionId
                            ? 'stdout is delivered to the chat session this job came from — click to open it'
                            : 'stdout is delivered to the chat session this job came from',
                });
            } else if (deliver === 'local') {
                out.push({
                    icon: 'terminal', color: 'var(--txt-subtle)', label: 'local',
                    title: 'Output is kept on the host, not delivered to a chat',
                });
            }
            // Only ADK jobs routed through invoke_workflow write the run record
            // the scorecard reads; the backend already worked that out.
            if (job.records_runs) {
                out.push({
                    icon: 'line-chart', color: 'var(--acc-green)', label: 'run record',
                    sub: 'scorecard',
                    title: 'Each run is written to the trace log the scorecard reads',
                });
            } else if (job.adk_app) {
                out.push({
                    icon: 'line-chart', color: 'var(--txt-muted)', label: 'not recorded',
                    title: 'This wrapper bypasses invoke_workflow, so its runs never reach the scorecard',
                });
            }
            return out;
        };

        // One scheduled job, in full. The old cron pane fetched next_run_at,
        // last_run_at, last_status and last_error and rendered none of them —
        // which is exactly the half that says whether the schedule is working.
        //
        // onRunNow/running/notice come from App: firing a job off-schedule is the
        // one write this port makes, and only for jobs the default profile owns —
        // the gateway speaks for its own home and cannot address another
        // profile's scheduler.
        // onOpenOwner is optional: the owner names a profile that has a page of its
        // own, and naming it without a way through made the reader go find it in the
        // rail. Left off, the owner stays plain text — on the profile pane the owner
        // is the page you are already on.
        // healthTask/onOpenTask are the failure half: the watchdog's open card for
        // this job, and a way to open it. Optional, because a caller that has not
        // fetched the detail has no card to pass — it just gets the pointer line.
        function JobCard({ job, onRunNow, running, notice, onOpenOwner, healthTask, onOpenTask }) {
            const off = job.enabled === false || !!job.paused_at;
            const status = job.last_status;
            const statusColor = status === 'ok' ? 'text-[#a6e3a1]'
                : status === 'error' ? 'text-[#f38ba8]' : 'text-[#585b70]';
            const stamp = (v) => fmtCronStamp(v);
            return (
                <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                    <div class="px-4 py-3 border-b border-[#313244] flex items-center justify-between gap-3 flex-wrap">
                        <div class="flex items-center gap-2 min-w-0">
                            <i data-lucide="alarm-clock" class="w-4 h-4 text-[#b4befe] shrink-0"></i>
                            <span class="text-base font-bold text-[#cdd6f4] truncate">{job.name || job.id}</span>
                            {off && (
                                <span class="text-[9px] uppercase font-bold tracking-wider px-1.5 py-0.5 rounded bg-[#313244] text-[#f38ba8] shrink-0">
                                    {job.paused_at ? 'paused' : 'off'}
                                </span>
                            )}
                        </div>
                        {/* Schedule then action, left to right: the schedule is a
                            fact about the job and the button is something you do to
                            it, so the destructive-ish one sits at the far edge where
                            it cannot be hit reaching for anything else. The schedule
                            carries a label — bare "every 10m" beside a "Run now"
                            button read as a description of the button. */}
                        <div class="flex items-center gap-3 shrink-0">
                            <span class="flex items-center gap-1.5" title="The schedule this job fires on">
                                <span class="text-[10px] uppercase tracking-wider text-[#9ca3af] font-bold">Schedule</span>
                                <span class="font-mono text-xs text-[#a6e3a1]">{formatSchedule(job)}</span>
                            </span>
                            {onRunNow && (
                                <button
                                    onClick={() => onRunNow(job)}
                                    disabled={running || !job.agent_is_default}
                                    title={job.agent_is_default
                                        ? 'Fire this job off-schedule, on the next tick'
                                        : `Owned by the ${job.agent} profile — run it with hermes cron run`}
                                    class={`font-semibold text-[11px] py-1 px-2.5 rounded-lg border flex items-center gap-1.5 transition ${
                                        running || !job.agent_is_default
                                            ? 'bg-[#313244]/40 text-[#585b70] border-[#313244] cursor-not-allowed'
                                            : 'bg-[#b4befe] text-[#11111b] border-[#b4befe] hover:bg-[#89b4fa]'
                                    }`}
                                >
                                    <i data-lucide="play" class="w-3 h-3"></i>
                                    {running ? 'Queueing…' : 'Run now'}
                                </button>
                            )}
                        </div>
                    </div>

                    {notice && (
                        <div class={`mx-4 mt-3 text-[11px] rounded-lg px-3 py-2 border ${
                            notice.ok
                                ? 'bg-[#a6e3a1]/10 border-[#a6e3a1]/30 text-[#a6e3a1]'
                                : 'bg-[#f38ba8]/10 border-[#f38ba8]/30 text-[#f38ba8]'
                        }`}>
                            {notice.text}
                        </div>
                    )}

                    <div class="grid grid-cols-1 sm:grid-cols-3 divide-y sm:divide-y-0 sm:divide-x divide-[#313244]">
                        <div class="px-4 py-3">
                            <div class="text-[10px] uppercase tracking-wider text-[#9ca3af] font-bold mb-1">Next run</div>
                            <div class="font-mono text-xs text-[#cdd6f4]">{stamp(job.next_run_at)}</div>
                        </div>
                        <div class="px-4 py-3">
                            <div class="text-[10px] uppercase tracking-wider text-[#9ca3af] font-bold mb-1">Last run</div>
                            <div class="font-mono text-xs text-[#cdd6f4]">{stamp(job.last_run_at)}</div>
                        </div>
                        <div class="px-4 py-3">
                            <div class="text-[10px] uppercase tracking-wider text-[#9ca3af] font-bold mb-1">Outcome</div>
                            <div class={`font-mono text-xs ${statusColor}`}>{status || 'never run'}</div>
                        </div>
                    </div>

                    {/* Only the repair, and only when there is one.

                        This slot has been narrowing for a reason. It began as
                        the whole traceback, which the failing execution below
                        already carries; then as a line saying no card had been
                        filed, which is a fact about the watchdog and not about
                        this job. What is left is the one thing this page cannot
                        get from anywhere else: that an agent is on it, and where
                        that work is. Nothing to say means nothing rendered — the
                        outcome cell above already says "error", and the
                        executions below already carry why. */}
                    {job.last_status === 'error' && healthTask && (
                        <div class="px-4 py-2.5 border-t border-[#313244] text-xs flex items-center gap-2 flex-wrap">
                            <i data-lucide="wrench" class="w-3.5 h-3.5 text-[#f9e2af] shrink-0"></i>
                            <span class="text-[#585b70]">Being worked as</span>
                            {onOpenTask ? (
                                <button
                                    type="button"
                                    onClick={() => onOpenTask(healthTask.id)}
                                    class="text-[#b4befe] hover:underline text-left"
                                    title={healthTask.title}
                                >{healthTask.title}</button>
                            ) : (
                                <span class="text-[#cdd6f4]">{healthTask.title}</span>
                            )}
                            <span class="font-mono text-[10px] text-[#585b70]">
                                {healthTask.status}{healthTask.assignee ? ` · ${healthTask.assignee}` : ''}
                            </span>
                        </div>
                    )}

                    {/* What this job executes is NOT here. "runs <path>" and
                        "launches <app>" moved to the What it runs card, which
                        draws the same thing as a diagram — saying it twice, in
                        two vocabularies, a card apart, made the reader check
                        whether the two agreed. This card is now the schedule,
                        the outcome, and who owns it. */}
                    <div class="px-4 py-3 border-t border-[#313244] space-y-2">
                        {/* A wrapper that speaks HTTP itself still runs the app, it just
                            leaves no run record — so the scorecard shows "never run" for
                            an app that ran fine. Say which of the two it is. */}
                        {job.adk_app && job.records_runs === false && (
                            <div class="text-xs bg-[#f9e2af]/10 border border-[#f9e2af]/30 rounded-lg px-3 py-2 text-[#f9e2af]">
                                Runs are not recorded — this script calls the ADK server directly
                                instead of going through <span class="font-mono">invoke_workflow</span>,
                                so its runs never reach the scorecard. Nothing else keeps them
                                either, now that no trace store is configured.
                            </div>
                        )}
                        {/* Only for a job that actually sends it. A `no_agent` job
                            runs a script and never reads its prompt, so printing it
                            here described the job in words nothing executes — and
                            the ones written by hand drift, naming a path the job no
                            longer runs directly above the path it does. */}
                        {job.prompt && !job.no_agent && job.runs_agent !== false && (
                            <pre class="text-xs text-[#a6adc8] font-mono bg-[#11111b] rounded-lg p-3" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{job.prompt}</pre>
                        )}
                        <div class="flex flex-wrap gap-x-5 gap-y-1 text-xs text-[#585b70]">
                            <span>owner{' '}
                                {onOpenOwner && job.agent ? (
                                    <button
                                        type="button"
                                        onClick={onOpenOwner}
                                        title={`Open the ${job.agent} profile`}
                                        class="font-mono text-[#b4befe] hover:underline"
                                    >{job.agent}</button>
                                ) : (
                                    <span class="font-mono text-[#cdd6f4]">{job.agent}</span>
                                )}
                            </span>
                            {job.effective_model
                                ? <span>model <span class="font-mono text-[#a6e3a1]">{job.effective_model}</span>
                                    <span class="text-[#f9e2af]"> {job.model_source === 'override' ? 'pinned' : `from ${job.agent}`}</span>
                                  </span>
                                : <span>no model — runs the script directly</span>}
                            {job.deliver && <span>delivers <span class="font-mono text-[#cdd6f4]">{job.deliver}</span></span>}
                            <span class="font-mono text-[#45475a]">{job.id}</span>
                        </div>
                    </div>
                </div>
            );
        }

        // --- One automation's run history ---
        // The page this whole route exists for. Every other surface reported an
        // automation in aggregate — "78 failed" on the metrics ledger, "error"
        // in the status column — and none of them could show the one string that
        // ends the investigation, which is what the execution wrote to `error`.
        //
        // Executions come from the cron store, which is the only thing that
        // records every run. An ADK trace is attached where one matches, and it
        // is the richer record by far — but a workflow that crashed before the
        // invoker opened a trace has no trace, and those are precisely the runs
        // worth reading. So the cron execution is the row, and the trace is
        // detail hanging off it, never the other way around.

        // UTC, shown as UTC, matching every other timestamp on this console.
        // The cron store and the traces both write UTC, and converting only
        // here would make the run history disagree with the Next run beside it.
        var runStamp = (v) => fmtCronStamp(v);

        var EXEC_STYLE = {
            completed: { dot: 'bg-[#a6e3a1]', text: 'text-[#a6e3a1]' },
            failed:    { dot: 'bg-[#f38ba8]', text: 'text-[#f38ba8]' },
            running:   { dot: 'bg-[#89b4fa]', text: 'text-[#89b4fa]' },
            claimed:   { dot: 'bg-[#585b70]', text: 'text-[#a6adc8]' },
            unknown:   { dot: 'bg-[#f9e2af]', text: 'text-[#f9e2af]' },
        };
        var execStyle = (s) => EXEC_STYLE[s] || EXEC_STYLE.unknown;

        // The ADK trace for one execution, when there is one. Rendered as facts
        // about that run and not as a scorecard: an average over one run is the
        // run, and presenting it in scorecard clothing would invite comparing it
        // against the team numbers, which are computed over a different window.
        function ExecutionTrace({ run }) {
            const cost = typeof run.estimated_cost_usd === 'number' && run.estimated_cost_usd > 0
                ? `$${run.estimated_cost_usd.toFixed(4)}` : null;
            const facts = [
                ['duration', fmtMs(run.duration_ms)],
                ['model calls', fmtNum(run.model_calls)],
                ['tool calls', fmtNum(run.tool_calls)],
                ['tokens', run.total_tokens ? fmtNum(run.total_tokens) : '—'],
                ['cost', cost || '—'],
                ['attempt', fmtNum(run.attempt)],
            ];
            return (
                <div class="rounded-lg border border-[#313244] bg-[#181825] overflow-hidden">
                    <div class="px-3 py-2 border-b border-[#313244] flex items-center justify-between gap-2 flex-wrap">
                        <span class="text-[10px] uppercase tracking-wider font-bold text-[#9ca3af] flex items-center gap-1.5">
                            <i data-lucide="git-branch" class="w-3.5 h-3.5"></i>
                            Workflow trace
                        </span>
                        <span class="font-mono text-[10px] text-[#585b70] truncate">{run.run_id}</span>
                    </div>
                    <div class="px-3 py-2 flex flex-wrap gap-x-5 gap-y-1 text-[11px]">
                        {facts.map(([label, value]) => (
                            <span key={label} class="text-[#585b70]">
                                {label} <span class="font-mono text-[#cdd6f4]">{value}</span>
                            </span>
                        ))}
                    </div>
                    {/* Which agents actually took a turn, and how many. The one
                        thing a stack trace cannot tell you: where in the pipeline
                        the run got to before it stopped. */}
                    {(run.agents || []).length > 0 && (
                        <div class="px-3 pb-2 flex flex-wrap gap-1.5">
                            {run.agents.map((a, i) => (
                                <span key={(a.name || '') + i}
                                      class="text-[10px] font-mono bg-[#11111b] border border-[#313244] rounded px-1.5 py-0.5 text-[#a6adc8]">
                                    {a.name}
                                    <span class="text-[#585b70]"> ×{fmtNum(a.turns)}</span>
                                </span>
                            ))}
                        </div>
                    )}
                    {/* A trace can carry an error the cron store never saw: the
                        wrapper caught it, reported the failure and exited 0, so
                        cron recorded a clean completion of a run that did not
                        work. Shown whenever it is there, regardless of status. */}
                    {run.error && (
                        <pre class="mx-3 mb-2 text-[11px] text-[#f38ba8] font-mono bg-[#f38ba8]/5 border border-[#f38ba8]/20 rounded-lg px-3 py-2"
                             style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{run.error}</pre>
                    )}
                    {/* The model's own verdict, kept next to the measured one and
                        never merged with it — the same rule the scorecard holds
                        to. They disagree often enough to be worth seeing. */}
                    {run.self_reported_status && (
                        <div class="px-3 pb-2 text-[10px] text-[#585b70]">
                            reported <span class="font-mono text-[#a6adc8]">{run.self_reported_status}</span>
                            {run.measured_passed !== undefined && run.measured_passed !== null && (
                                <> · measured <span class={`font-mono ${run.measured_passed ? 'text-[#a6e3a1]' : 'text-[#f38ba8]'}`}>
                                    {run.measured_passed ? 'passed' : 'failed'}
                                </span></>
                            )}
                        </div>
                    )}
                </div>
            );
        }

        // One execution. Collapsed it is a line you can scan for the red dot;
        // expanded it is everything recorded about that run.
        //
        // Rows without an error are expandable too. "It succeeded, but what did
        // it do" is a question the collapsed row cannot answer either, and a
        // list where only the broken rows open trains you to read the others as
        // having nothing to show.
        function ExecutionRow({ execution, open, onToggle }) {
            const style = execStyle(execution.status);
            const start = execution.started_at || execution.claimed_at;
            // A run that was claimed and never started is not a run that took
            // zero milliseconds. The store has no finish time for it, and a
            // dash is the honest reading.
            const duration = execution.duration_ms === null || execution.duration_ms === undefined
                ? null : fmtMs(execution.duration_ms);
            const firstLine = String(execution.error || '').split('\n')[0];
            return (
                <div class={open ? 'bg-[#11111b]' : ''}>
                    <button
                        onClick={onToggle}
                        class="w-full text-left px-4 py-2.5 flex items-center justify-between gap-4 hover:bg-[#313244]/20 transition"
                    >
                        <div class="flex items-center gap-2.5 overflow-hidden">
                            <i data-lucide={open ? 'chevron-down' : 'chevron-right'}
                               class="w-3.5 h-3.5 text-[#585b70] shrink-0"></i>
                            <span class={`w-2 h-2 rounded-full shrink-0 ${style.dot}`}></span>
                            <span class="font-mono text-xs text-[#cdd6f4] shrink-0">{runStamp(start)}</span>
                            <span class={`text-[10px] uppercase tracking-wider shrink-0 ${style.text}`}>
                                {execution.status}
                            </span>
                            {/* The error's first line, on the collapsed row. Most
                                failures on this host are one line long — "Script
                                not found: ..." — and making that a click away
                                would be hiding the answer behind a chevron. */}
                            {firstLine && (
                                <span class="text-[11px] text-[#f38ba8]/80 font-mono truncate">{firstLine}</span>
                            )}
                        </div>
                        <div class="flex items-center gap-4 shrink-0 font-mono text-[11px] text-[#585b70]">
                            {execution.source !== 'builtin' && (
                                <span class="text-[10px] uppercase tracking-wider" title="How this run was triggered">
                                    {execution.source}
                                </span>
                            )}
                            <span>{duration || '—'}</span>
                        </div>
                    </button>

                    {open && (
                        <div class="px-4 pb-4 pt-1 space-y-3">
                            <div class="flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-[#585b70]">
                                <span>claimed <span class="font-mono text-[#a6adc8]">{runStamp(execution.claimed_at)}</span></span>
                                <span>started <span class="font-mono text-[#a6adc8]">{runStamp(execution.started_at)}</span></span>
                                <span>finished <span class="font-mono text-[#a6adc8]">{runStamp(execution.finished_at)}</span></span>
                                <span>profile <span class="font-mono text-[#a6adc8]">{execution.profile}</span></span>
                                <span class="font-mono text-[#45475a]">{execution.execution_id}</span>
                            </div>

                            {execution.error ? (
                                <pre class="text-[11px] text-[#f38ba8] font-mono bg-[#f38ba8]/5 border border-[#f38ba8]/20 rounded-lg px-3 py-2"
                                     style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{execution.error}</pre>
                            ) : execution.status === 'failed' ? (
                                // Failed with nothing written. Worth stating: the
                                // absence is itself the finding, and a blank space
                                // reads as a rendering bug instead.
                                <div class="text-[11px] text-[#f9e2af]">
                                    Recorded as failed with no error message — the process died without reporting why.
                                </div>
                            ) : null}

                            {execution.adk_run
                                ? <ExecutionTrace run={execution.adk_run} />
                                : execution.adk_run === null && (
                                    <div class="text-[11px] text-[#585b70]">
                                        No workflow trace covers this execution — it stopped before
                                        <span class="font-mono"> invoke_workflow</span> opened one, or the
                                        script never reached the invoker.
                                    </div>
                                )}
                        </div>
                    )}
                </div>
            );
        }

        // One Hermes agent, in full. The counterpart to the ADK agent pane: same
        // three questions — what is this agent, what is it made of, and what does
        // it run on a schedule — asked of a profile instead of a team.
        //
        // What it can answer is bounded by what a profile actually is. There is no
        // scorecard here because a Hermes profile leaves no per-run trace the way
        // an ADK app does; its record is its jobs' last outcomes, which is what
        // the schedule section shows.
        // onOpenJob leaves this pane rather than expanding inside it: a job's
        // configuration used to render here, below the list, on a page that had
        // no access to its runs. The automation page has both, so a job in the
        // schedule is a link out and not a selection.
        function HermesAgentPane({ group, onOpenJob }) {
            const s = group.summary;
            const jobs = group.jobs || [];
            const mcp = (s && s.mcp_servers) || [];
            const failing = jobs.filter(j => j.last_status === 'error').length;
            return (
                <>
                    {/* A profile read from cron alone is a real state, not an error:
                        the jobs name an owner the roster does not list. Say so
                        rather than rendering an agent with every field empty. */}
                    {!s && (
                        <div class="bg-[#f9e2af]/10 border border-[#f9e2af]/30 rounded-xl px-4 py-3 text-xs text-[#f9e2af]">
                            No profile named <span class="font-mono">{group.agent}</span> is
                            installed on this host — it is known only because its jobs name
                            it as their owner.
                        </div>
                    )}

                    <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
                        <StatTile icon="cpu" label="Model" muted={!(s && s.model)}
                                  value={(s && s.model) || 'not reported'}
                                  hint={s ? s.provider : null} />
                        <StatTile icon="wrench" label="Skills" muted={!s}
                                  value={s ? `${s.skills_available} / ${s.skills_total}` : '—'}
                                  hint={s ? 'available / installed' : null} />
                        <StatTile icon="plug" label="MCP servers" muted={!mcp.length}
                                  value={mcp.length ? String(mcp.length) : 'none'}
                                  hint={(s && (s.toolsets || []).length)
                                      ? `toolsets: ${s.toolsets.join(', ')}` : null} />
                        <StatTile icon="alarm-clock" label="Scheduled" muted={jobs.length === 0}
                                  value={jobs.length ? `${jobs.length} job${jobs.length === 1 ? '' : 's'}` : 'nothing'}
                                  hint={failing ? `${failing} failing` : null} />
                    </div>

                    {s && (
                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                            <SectionHeader icon="bot" title="Profile"
                                           right={s.is_default ? 'default profile' : null} />
                            <div class="p-4 space-y-3">
                                {/* The role description. This is also what the kanban
                                    decomposer routes on, so an empty one is a real
                                    finding about the host — say so rather than
                                    rendering a blank field. */}
                                {s.description
                                    ? <div class="text-sm text-[#cdd6f4] leading-relaxed">
                                          {s.description}
                                          {s.description_auto && (
                                              <span class="ml-2 text-[10px] uppercase tracking-wider text-[#585b70] align-middle">auto-generated</span>
                                          )}
                                      </div>
                                    : <div class="text-xs text-[#f9e2af]">
                                          No description set — the kanban decomposer can only
                                          route to this profile by name.{' '}
                                          <span class="font-mono text-[#cdd6f4]">hermes profile describe {group.agent} --text "…"</span>
                                      </div>}
                                <div class="flex flex-wrap gap-x-5 gap-y-1 text-xs text-[#585b70]">
                                    <span>home <span class="font-mono text-[#cdd6f4]">{s.path}</span></span>
                                    <span>SOUL.md {s.has_soul
                                        ? <span class="text-[#a6e3a1]">present</span>
                                        : <span class="text-[#f9e2af]">missing</span>}</span>
                                    <span>context <span class="font-mono text-[#cdd6f4]">{s.context_count}</span> file{s.context_count === 1 ? '' : 's'}</span>
                                </div>
                                {(s.memory_files || []).length > 0 && (
                                    <div class="text-xs text-[#585b70]">
                                        memories{' '}
                                        {s.memory_files.map(f => (
                                            <span key={f} class="font-mono text-[#cdd6f4] mr-2">{f}</span>
                                        ))}
                                    </div>
                                )}
                                {(s.disabled_toolsets || []).length > 0 && (
                                    <div class="text-xs text-[#f9e2af]">
                                        disabled toolsets: {s.disabled_toolsets.join(', ')}
                                    </div>
                                )}
                                {mcp.length > 0 && (
                                    <div class="space-y-1.5">
                                        {mcp.map(m => (
                                            <div key={m.name} class="flex items-center gap-2 text-xs">
                                                <span class={`w-1.5 h-1.5 rounded-full shrink-0 ${
                                                    m.enabled === false ? 'bg-[#585b70]' : 'bg-[#a6e3a1]'}`}></span>
                                                <span class="font-mono text-[#cdd6f4]">{m.name}</span>
                                                <span class="text-[#585b70]">{m.transport}</span>
                                                <span class="font-mono text-[#585b70] truncate">{m.target}</span>
                                                {m.auth && (
                                                    <span class="text-[9px] uppercase font-bold px-1.5 py-0.5 rounded bg-[#89b4fa]/15 text-[#89b4fa] shrink-0">{m.auth}</span>
                                                )}
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        </div>
                    )}

                    <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                        <SectionHeader icon="alarm-clock" title="Schedule"
                                       right={jobs.length ? `${jobs.length} job${jobs.length === 1 ? '' : 's'}` : null} />
                        <div class="p-4 space-y-2">
                            {jobs.length === 0 ? (
                                <p class="text-xs text-[#585b70]">
                                    Nothing scheduled under this profile. It still answers chat and
                                    runs whatever is asked of it directly.
                                </p>
                            ) : (
                                jobs.map(j => (
                                    <button
                                        key={j.id}
                                        onClick={() => onOpenJob(j.id)}
                                        class="w-full text-left px-3 py-2 rounded-lg flex items-center gap-2 text-xs transition border bg-[#11111b] border-[#313244] text-[#a6adc8] hover:border-[#45475a]"
                                    >
                                        <i data-lucide="clock" class="w-3.5 h-3.5 shrink-0"></i>
                                        <span class="font-semibold truncate">{j.name || j.id}</span>
                                        <span class="font-mono text-[#585b70] ml-auto shrink-0">{formatSchedule(j)}</span>
                                        <span class={`shrink-0 ${
                                            j.last_status === 'ok' ? 'text-[#a6e3a1]'
                                                : j.last_status === 'error' ? 'text-[#f38ba8]' : 'text-[#585b70]'
                                        }`}>{j.last_status || 'never run'}</span>
                                        <i data-lucide="chevron-right" class="w-3.5 h-3.5 shrink-0 text-[#585b70]"></i>
                                    </button>
                                ))
                            )}
                        </div>
                    </div>
                </>
            );
        }

        // The count beside a tab. One fixed circle for all of them: these badges
        // sit in a row a few pixels apart, where any difference in size reads as
        // a difference in kind rather than as the four separate styles they
        // actually were — each tab had grown its own padding, and a badge's
        // width moved with the number inside it, so the row changed shape as the
        // counts did. Colour is the only thing left varying, which is the part
        // that carries meaning.
        //
        // Nothing renders at zero: a tab with nothing waiting should look like a
        // tab, not like a badge showing 0.

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
        window.stepKind = stepKind;
        window.childRelation = childRelation;
        window.execStyle = execStyle;
