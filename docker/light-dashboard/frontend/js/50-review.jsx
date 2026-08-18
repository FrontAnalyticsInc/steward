// --- 50-review.jsx ---------------------------------------------------------
// The review queue: one row and one detail panel per item type, chosen by
// `templateFor`, so a new reviewable type is a template and not a new branch.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
        // ---------------------------------------------------------------------
        // Review queue
        // ---------------------------------------------------------------------
        // Both helpers were declared inside App. They are used by the row and
        // detail components below, which are top-level functions and cannot see
        // App's scope, so they live out here now. Nothing about them changed.
        function cleanStr(val, fallback = "—") {
            if (val === undefined || val === null || val === '') return fallback;
            return val;
        }

        function getHighestScoreVal(item) {
            const scores = item && item.evidence && item.evidence.scores;
            if (!scores || typeof scores !== 'object' || Object.keys(scores).length === 0) return null;
            let maxLabel = '';
            let maxVal = -1;
            for (const [k, v] of Object.entries(scores)) {
                if (typeof v === 'number' && v > maxVal) { maxVal = v; maxLabel = k; }
            }
            return maxVal !== -1 ? { label: maxLabel, score: maxVal } : null;
        }

        // Not everything that needs a human eye is an email. A proposed Gmail
        // filter, a fact about to enter the graph, a todo about to land on
        // someone's CRM record, a page or post about to go out — they arrive in
        // one queue and they are not one kind of thing, so they do not render
        // through one template.
        //
        // The registry maps review_type -> how to draw it. Everything unknown
        // falls through to GENERIC_TEMPLATE, which renders title/summary/fields.
        // That fallback is the contract: a producer can invent a type, fill in
        // those three, and get a usable review page with no change here at all.
        // A hand-written template comes later, when the shape has earned one —
        // what it must never do is stand between the item and a human.
        //
        // Declared as `function` rather than `const` for the reason the state
        // block below repeats: under this in-browser transpile a module-scope
        // `const` is hoisted as `var` and reads `undefined` from anything
        // evaluated earlier, which fails silently and at a distance.

        function ReviewTypeBadge({ item }) {
            const template = templateFor(item);
            return (
                <div class="flex items-center gap-2.5 mb-4 pb-3 border-b border-[#2d3748]">
                    {/* A title, not a control. It used to be boxed in a filled,
                        bordered chip, which in a pane full of buttons reads as
                        one more button you can press. Same icon and same words,
                        set as a heading instead. */}
                    <span class="inline-flex items-center gap-2 text-xs uppercase font-bold tracking-wider text-[#cdd6f4]">
                        <i data-lucide={template.icon} class="w-5 h-5 text-[#b4befe]"></i>
                        {item.review_type_label || template.label}
                    </span>
                    {/* The id, in mono and selectable. This is the thing people
                        paste to each other, and the URL it belongs to is the
                        whole reason the tab was rebuilt. */}
                    <span class="text-[10px] font-mono text-[#6b7280] select-all">{item.id}</span>
                    {item.state && item.state !== 'pending' && (
                        <span class="text-[10px] uppercase tracking-wider text-[#f9e2af]">{item.state}</span>
                    )}
                </div>
            );
        }

        function ReviewField({ label, children, mono }) {
            return (
                <div>
                    <div class="text-[10px] uppercase font-bold text-[#9ca3af] mb-1.5 tracking-wider">{label}</div>
                    <div class={`bg-[#111827] border border-[#374151] p-3 text-xs text-[#a6adc8] break-words whitespace-pre-wrap ${mono ? 'font-mono' : ''}`}>
                        {children}
                    </div>
                </div>
            );
        }

        // Shared by every template. Extracted from the email detail pane, where
        // it was inline — and where the scores block was built by concatenating
        // producer-controlled keys into an HTML string and injecting it with
        // dangerouslySetInnerHTML. Those keys come from a model reading a
        // stranger's email. Rendering them as JSX is not a style preference: it
        // is what stops that text executing in the page that can now send mail.
        function ProvenancePanel({ item }) {
            const evidence = item.evidence || {};
            const scores = evidence.scores || {};
            const scoreKeys = Object.keys(scores);
            return (
                <div class="w-[45%] bg-[#0b0f19] p-5 h-full overflow-y-auto flex flex-col gap-4">
                    <h3 class="text-xs font-bold uppercase text-[#cdd6f4] tracking-wider border-b border-[#2d3748] pb-2 mb-1">
                        Provenance Evidence
                    </h3>

                    {/* WHAT PRODUCED THIS, and what it could reach. The
                        Integrations tab answers "what can reach what"; this
                        answers "what produced this", and it belongs here
                        because this is the moment someone decides whether to
                        trust the output.

                        Absent on items written before app/approvals.py recorded
                        a producer, and it says so rather than falling back to
                        `evidence.source` — that field is free text already used
                        for other things, and a confident wrong agent name
                        beside a decision about sending mail is worse than a
                        blank. */}
                    <div>
                        <div class="text-[10px] uppercase font-bold text-[#9ca3af] mb-1.5 tracking-wider">Produced by</div>
                        {item.producer ? (
                            <div class="bg-[#111827] border border-[#374151] p-3 flex flex-col gap-2">
                                <div class="text-xs text-[#bac2de] font-mono flex items-center gap-2">
                                    <i data-lucide="bot" class="w-3.5 h-3.5 text-[#b4befe]"></i>
                                    {item.producer.agent}
                                    {item.producer.stage && (
                                        <span class="text-[10px] text-[#6b7280]">· {item.producer.stage}</span>
                                    )}
                                </div>
                                {item.producer_access === null ? (
                                    <div class="text-[10px] text-[#6b7280] italic">
                                        Access for this agent could not be resolved.
                                    </div>
                                ) : item.producer_access.length === 0 ? (
                                    <div class="text-[10px] text-[#6b7280] italic">
                                        No recorded access to any external system.
                                    </div>
                                ) : (
                                    <div class="flex flex-col gap-1">
                                        <div class="text-[10px] uppercase tracking-wider text-[#6b7280]">Access it held</div>
                                        {item.producer_access.map(a => (
                                            <div key={`${a.source_key}::${a.capability || '*'}`}
                                                 class="text-[11px] font-mono flex items-center gap-2 text-[#a6adc8]">
                                                <StatusMark status={a.status} />
                                                <span>{a.source}</span>
                                                {a.capability && <span class="text-[#89b4fa]">{a.capability}</span>}
                                                <span class="text-[#6b7280]">{fmtAgo(a.last_used_at) || 'never used'}</span>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        ) : (
                            <div class="bg-[#111827] border border-[#374151] p-3 text-xs text-[#6b7280] italic">
                                No producer recorded — this item predates producer attribution.
                            </div>
                        )}
                    </div>

                    <ReviewField label="Source" mono>
                        {cleanStr(evidence.source, "No source recorded")}
                    </ReviewField>
                    {evidence.conversation_notes && (
                        <ReviewField label="Conversation Notes">{evidence.conversation_notes}</ReviewField>
                    )}
                    {evidence.enrichment && (
                        <ReviewField label="Enrichment Context">{evidence.enrichment}</ReviewField>
                    )}
                    {scoreKeys.length > 0 && (
                        <div>
                            <div class="text-[10px] uppercase font-bold text-[#9ca3af] mb-1.5 tracking-wider">Scores</div>
                            <div class="bg-[#111827] border border-[#374151] p-3 flex flex-col gap-1.5">
                                {scoreKeys.map(k => (
                                    <div key={k} class="flex justify-between items-center py-1 font-mono text-xs">
                                        <span class="uppercase font-bold text-[#cdd6f4]">{k}</span>
                                        <span class="text-[#10b981] font-bold">{String(scores[k])} / 10</span>
                                    </div>
                                ))}
                            </div>
                        </div>
                    )}
                </div>
            );
        }

        // The action bar renders whatever the server said this item's type
        // allows — it does not decide, and it cannot offer an action the
        // backend would refuse. Unavailable actions stay visible with their
        // reason, because "you cannot do this here, and here is why" is more
        // useful than a button that is simply missing.
        function ReviewActionBar({ item, busy, onAct, onReject }) {
            const actions = item.actions || [];
            const primary = actions.filter(a => a.primary);
            const secondary = actions.filter(a => !a.primary);
            const ordered = [...primary, ...secondary];

            return (
                <div class="flex flex-col gap-2">
                    <div class="flex gap-3 flex-wrap">
                        <button
                            class="py-3 px-5 bg-[#ef4444] text-[#000000] text-xs font-bold uppercase transition hover:bg-[#f87171] disabled:opacity-40"
                            disabled={busy}
                            onClick={onReject}
                        >
                            {/* The server names this button, because what
                                rejecting does differs by type: a draft is
                                finished, a task goes back to the board with the
                                reason attached. The key is still R. */}
                            {((item.reject_action || {}).label || 'Reject')} (R)
                        </button>
                        {ordered.map((action, i) => (
                            <button
                                key={action.id}
                                disabled={busy || !action.available}
                                title={action.unavailable_reason || action.hint || ''}
                                onClick={() => onAct(action)}
                                class={`py-3 px-5 text-xs font-bold uppercase transition disabled:cursor-not-allowed disabled:opacity-40 ${
                                    action.primary
                                        ? 'flex-1 bg-[#10b981] text-[#000000] hover:bg-[#34d399]'
                                        : action.destructive
                                            ? 'border border-[#ef4444] text-[#f87171] hover:bg-[#ef4444] hover:text-[#000000]'
                                            : 'border border-[#45475a] text-[#cdd6f4] hover:bg-[#313244]'
                                }`}
                            >
                                {action.label}
                                <span class="ml-2 opacity-60">({action.primary ? 'A' : i + 1})</span>
                            </button>
                        ))}
                    </div>
                    {/* Say what the buttons will do. Two approve verbs on one
                        item is only clear if the difference is written down. */}
                    <div class="flex flex-col gap-0.5">
                        {ordered.filter(a => a.hint || a.unavailable_reason).map(a => (
                            <div key={a.id} class={`text-[10px] ${a.available ? 'text-[#6b7280]' : 'text-[#f9e2af]'}`}>
                                <span class="font-bold uppercase">{a.label}</span> — {a.unavailable_reason || a.hint}
                            </div>
                        ))}
                    </div>
                </div>
            );
        }

        function EmailDraftDetail({ item, body, onBody, busy, onAct, onReject, readOnly }) {
            const recipient = item.recipient || {};
            const who = [recipient.name, recipient.address].filter(Boolean).join(' · ');
            return (
                <div class="w-[55%] border-r border-[#2d3748] flex flex-col p-5 h-full overflow-y-auto">
                    <ReviewTypeBadge item={item} />
                    <div class="text-[10px] uppercase font-bold text-[#9ca3af] mb-1.5 tracking-wider">To</div>
                    <div class="text-xs text-[#a6adc8] font-mono bg-[#111827] border border-[#374151] p-2.5 mb-4 break-all select-all">
                        {cleanStr(who || recipient.address, "No recipient recorded")}
                        {recipient.org && <span class="text-[#6b7280]"> ({recipient.org})</span>}
                    </div>

                    <div class="text-[10px] uppercase font-bold text-[#9ca3af] mb-1.5 tracking-wider">Subject Line</div>
                    <div class="text-sm font-bold text-[#cdd6f4] bg-[#111827] border border-[#374151] p-3 mb-4 select-all break-words">
                        {cleanStr(item.subject, "(No Subject)")}
                    </div>

                    <div class="text-[10px] uppercase font-bold text-[#9ca3af] mb-1.5 tracking-wider">
                        Email Body {readOnly ? '' : '(Edits automatically kept)'}
                    </div>
                    <textarea
                        id="draft-body-editor"
                        readOnly={readOnly}
                        // `?? ''` rather than the raw value: a controlled
                        // textarea handed undefined warns and then goes
                        // uncontrolled, and body is genuinely absent on some
                        // items in this queue.
                        value={body ?? ''}
                        onInput={(e) => onBody(e.target.value)}
                        class="flex-1 min-h-[220px] w-full bg-[#111827] text-[#cdd6f4] border border-[#374151] p-3.5 text-sm body-textarea resize-none outline-none focus:border-[#3b82f6] mb-4"
                    />

                    {!readOnly && (
                        <ReviewActionBar item={item} busy={busy} onAct={onAct} onReject={onReject} />
                    )}
                </div>
            );
        }

        function FilterDetail({ item, busy, onAct, onReject, readOnly }) {
            const rule = item.rule || {};
            const examples = rule.example_message_ids || [];
            // Deliberately no textarea. The rule is structured data and nothing
            // parses an edited version back, so an editable box here would let
            // someone change a filter in a way that changed nothing and said
            // nothing about changing nothing.
            const rows = [
                ['From', rule.from_pattern],
                ['Subject', rule.subject_pattern],
                ['Has words', rule.has_words],
                ['Apply label', rule.add_label],
                ['Remove from inbox', rule.remove_from_inbox ? 'Yes' : 'No'],
            ].filter(([, v]) => v !== undefined && v !== null && v !== '');

            return (
                <div class="w-[55%] border-r border-[#2d3748] flex flex-col p-5 h-full overflow-y-auto">
                    <ReviewTypeBadge item={item} />
                    <div class="text-sm font-bold text-[#cdd6f4] mb-4 break-words">
                        {cleanStr(item.title || item.subject, "Proposed filter")}
                    </div>

                    <div class="text-[10px] uppercase font-bold text-[#9ca3af] mb-1.5 tracking-wider">The rule</div>
                    <div class="border border-[#374151] mb-4">
                        {rows.length === 0 ? (
                            <div class="p-3 text-xs text-[#6b7280] italic">This proposal carries no rule.</div>
                        ) : rows.map(([label, value]) => (
                            <div key={label} class="flex border-b border-[#374151] last:border-b-0">
                                <div class="w-40 shrink-0 bg-[#111827] p-2.5 text-[10px] uppercase tracking-wider text-[#9ca3af] font-bold">{label}</div>
                                <div class="p-2.5 text-xs text-[#cdd6f4] font-mono break-all">{String(value)}</div>
                            </div>
                        ))}
                    </div>

                    <div class="text-[10px] uppercase font-bold text-[#9ca3af] mb-1.5 tracking-wider">
                        Example messages ({examples.length})
                    </div>
                    <div class="bg-[#111827] border border-[#374151] p-3 mb-4 max-h-32 overflow-y-auto">
                        {examples.length === 0 ? (
                            <div class="text-xs text-[#6b7280] italic">None recorded.</div>
                        ) : (
                            <div class="flex flex-col gap-1">
                                {examples.map(id => (
                                    <span key={id} class="text-[11px] font-mono text-[#a6adc8] select-all">{id}</span>
                                ))}
                            </div>
                        )}
                    </div>

                    {item.reason && (
                        <div class="mb-4">
                            <ReviewField label="Why">{item.reason}</ReviewField>
                        </div>
                    )}

                    {!readOnly && (
                        <ReviewActionBar item={item} busy={busy} onAct={onAct} onReject={onReject} />
                    )}
                </div>
            );
        }

        // The fallback, and the thing that makes the taxonomy open. Anything
        // with a title, a summary and a list of fields renders here — which is
        // what app/approvals.review_item() writes, so a new kind of review item
        // is reviewable the day it is invented rather than the day someone gets
        // round to designing a page for it.
        function GenericDetail({ item, busy, onAct, onReject, readOnly }) {
            const fields = item.fields || [];
            return (
                <div class="w-[55%] border-r border-[#2d3748] flex flex-col p-5 h-full overflow-y-auto">
                    <ReviewTypeBadge item={item} />
                    <div class="text-sm font-bold text-[#cdd6f4] mb-2 break-words">
                        {cleanStr(item.title, "Untitled review item")}
                    </div>
                    {item.summary && (
                        <div class="text-xs text-[#a6adc8] mb-4 leading-relaxed">{item.summary}</div>
                    )}

                    {fields.length > 0 && (
                        <div class="border border-[#374151] mb-4">
                            {fields.map((f, i) => (
                                <div key={`${f.label}-${i}`} class="flex border-b border-[#374151] last:border-b-0">
                                    <div class="w-40 shrink-0 bg-[#111827] p-2.5 text-[10px] uppercase tracking-wider text-[#9ca3af] font-bold">{f.label}</div>
                                    <div class="p-2.5 text-xs text-[#cdd6f4] break-all">{String(f.value)}</div>
                                </div>
                            ))}
                        </div>
                    )}

                    {item.body && (
                        <div class="mb-4">
                            <ReviewField label="Detail">{item.body}</ReviewField>
                        </div>
                    )}

                    {!readOnly && (
                        <ReviewActionBar item={item} busy={busy} onAct={onAct} onReject={onReject} />
                    )}
                </div>
            );
        }

        // --- List rows. One per type, because a row that reads "Anonymous — —"
        // for a filter is a row that makes a mixed queue unreadable.

        // What kind of thing a row is, as the icon alone, in the row's top-left
        // corner. Spelling the type out cost a line of every row to repeat what
        // the row's own shape already says — and in a rail this narrow that line
        // was competing with the subject. The word is still there on hover, and
        // in the legend, and on the item's own page.
        function ReviewRowIcon({ item, selected }) {
            const template = templateFor(item);
            return (
                <i
                    data-lucide={template.icon}
                    title={item.review_type_label || template.label}
                    class={`w-4 h-4 mt-0.5 shrink-0 ${selected ? 'text-[#11111b]' : 'text-[#b4befe]'}`}
                ></i>
            );
        }

        function EmailDraftRow({ item, selected }) {
            const r = item.recipient || {};
            const name = cleanStr(r.name, "");
            const org = cleanStr(r.org, "");
            let who = name;
            if (name && org) who = `${name} (${org})`;
            else if (!name && org) who = org;
            else if (!name && !org) who = cleanStr(r.address, "Anonymous");
            const highest = getHighestScoreVal(item);
            return (
                <>
                    <div class="flex justify-between items-baseline gap-2">
                        <span class="text-sm font-bold line-clamp-1">{who}</span>
                        <span class={`text-[10px] font-mono font-bold shrink-0 ${selected ? 'text-[#11111b]' : 'text-[#10b981]'}`}>
                            {highest ? `${highest.label} ${highest.score}` : ''}
                        </span>
                    </div>
                    <span class={`text-xs line-clamp-1 ${selected ? 'text-[#1e1e2e]' : 'text-[#9ca3af]'}`}>
                        {cleanStr(item.subject, "(No Subject)")}
                    </span>
                </>
            );
        }

        function FilterRow({ item, selected }) {
            const rule = item.rule || {};
            const count = (rule.example_message_ids || []).length;
            return (
                <>
                    <div class="flex justify-between items-baseline gap-2">
                        <span class="text-sm font-bold line-clamp-1">{cleanStr(rule.name, "Proposed filter")}</span>
                        <span class={`text-[10px] font-mono shrink-0 ${selected ? 'text-[#11111b]' : 'text-[#6b7280]'}`}>
                            {count} example{count === 1 ? '' : 's'}
                        </span>
                    </div>
                    <span class={`text-xs line-clamp-1 font-mono ${selected ? 'text-[#1e1e2e]' : 'text-[#9ca3af]'}`}>
                        → {cleanStr(rule.add_label, "no label")}
                    </span>
                </>
            );
        }

        // A kanban task whose worker stopped and asked for a human. The thing
        // that makes this a template rather than a generic item is the code:
        // "needs review" cannot be answered from a summary, and a reviewer who
        // has to go and find the files in another window will approve without
        // reading them. So the changed files are here, in full, under the
        // question — and the buttons are at the top as well as the bottom,
        // because after scrolling a 300-line file the bottom is a long way from
        // where the decision gets made.
        function CodeReviewRow({ item, selected }) {
            const files = item.changed_files || [];
            return (
                <>
                    <div class="flex justify-between items-baseline gap-2">
                        <span class="text-sm font-bold line-clamp-1">{cleanStr(item.title, item.id)}</span>
                        <span class={`text-[10px] font-mono shrink-0 ${selected ? 'text-[#11111b]' : 'text-[#6b7280]'}`}>
                            {files.length} file{files.length === 1 ? '' : 's'}
                        </span>
                    </div>
                    <span class={`text-xs line-clamp-1 font-mono ${selected ? 'text-[#1e1e2e]' : 'text-[#9ca3af]'}`}>
                        {cleanStr((item.task || {}).assignee, 'worker')} · {item.id}
                    </span>
                </>
            );
        }

        // One changed file, collapsed to its path until asked for. A dozen
        // files expanded at once is a wall nobody reads; the path list is the
        // thing a reviewer scans first to decide which ones matter.
        function ChangedFile({ file, startOpen }) {
            const [open, setOpen] = React.useState(!!startOpen);
            const lines = file.content ? file.content.split('\n').length : 0;
            return (
                <div class="border border-[#374151] mb-2">
                    <button
                        class="w-full flex items-center justify-between gap-3 px-3 py-2 bg-[#111827] hover:bg-[#1a2233] text-left"
                        onClick={() => setOpen(!open)}
                    >
                        <span class="text-[11px] font-mono text-[#cdd6f4] break-all">{file.path}</span>
                        <span class="text-[10px] text-[#6b7280] shrink-0">
                            {file.unavailable ? 'unreadable' : `${lines} lines`}
                        </span>
                    </button>
                    {open && (
                        file.unavailable ? (
                            <div class="p-3 text-xs text-[#f9e2af] italic">{file.unavailable}</div>
                        ) : (
                            <>
                                {/* Rendered as text, never as markup. This is
                                    source code from a repository the agent
                                    searched, in a page that can complete tasks
                                    and send mail. */}
                                <pre class="p-3 overflow-x-auto text-[11px] leading-relaxed font-mono text-[#a6adc8] bg-[#0b0f19] max-h-[32rem]">{file.content}</pre>
                                {file.truncated && (
                                    <div class="px-3 py-2 text-[10px] text-[#f9e2af] border-t border-[#374151]">
                                        Truncated. Open the file to read the rest.
                                    </div>
                                )}
                            </>
                        )
                    )}
                </div>
            );
        }

        function CodeReviewDetail({ item, busy, onAct, onReject, readOnly }) {
            const files = item.changed_files || [];
            const task = item.task || {};
            const dropped = item.changed_files_dropped || 0;
            return (
                <div class="w-[55%] border-r border-[#2d3748] flex flex-col p-5 h-full overflow-y-auto">
                    <ReviewTypeBadge item={item} />
                    <div class="text-sm font-bold text-[#cdd6f4] mb-2 break-words">
                        {cleanStr(item.title, item.id)}
                    </div>

                    {/* What the worker said when it stopped — the question this
                        page is asking. */}
                    {item.summary && (
                        <div class="text-xs text-[#a6adc8] mb-4 leading-relaxed border-l-2 border-[#b4befe] pl-3">
                            {item.summary}
                        </div>
                    )}

                    {!readOnly && (
                        <div class="mb-5">
                            <ReviewActionBar item={item} busy={busy} onAct={onAct} onReject={onReject} />
                        </div>
                    )}

                    <div class="border border-[#374151] mb-4">
                        {[
                            ['Task', item.id],
                            ['Worker', task.assignee || '—'],
                            ['Workspace', task.workspace_path || '—'],
                            ['Branch', task.branch_name || '—'],
                        ].map(([label, value]) => (
                            <div key={label} class="flex border-b border-[#374151] last:border-b-0">
                                <div class="w-32 shrink-0 bg-[#111827] p-2.5 text-[10px] uppercase tracking-wider text-[#9ca3af] font-bold">{label}</div>
                                <div class="p-2.5 text-xs text-[#cdd6f4] break-all font-mono">{String(value)}</div>
                            </div>
                        ))}
                    </div>

                    <div class="text-[10px] uppercase font-bold text-[#9ca3af] mb-1.5 tracking-wider">
                        Changed files ({files.length + dropped})
                    </div>
                    {files.length === 0 ? (
                        <div class="bg-[#111827] border border-[#374151] p-3 mb-4 text-xs text-[#6b7280] italic">
                            The worker named no files. Read its notes below, and the task's own
                            page, before approving.
                        </div>
                    ) : (
                        <div class="mb-4">
                            {/* One file open by default: enough to show that
                                this pane really does contain the code, without
                                unrolling everything. */}
                            {files.map((f, i) => (
                                <ChangedFile key={f.path} file={f} startOpen={files.length === 1 || i === 0} />
                            ))}
                            {dropped > 0 && (
                                <div class="text-[10px] text-[#f9e2af] mb-2">
                                    {dropped} more file{dropped === 1 ? '' : 's'} named but not shown.
                                </div>
                            )}
                        </div>
                    )}

                    {item.body && (
                        <div class="mb-4">
                            <ReviewField label="Worker's notes">{item.body}</ReviewField>
                        </div>
                    )}

                    {task.body && (
                        <div class="mb-4">
                            <ReviewField label="What was asked for">{task.body}</ReviewField>
                        </div>
                    )}

                    {!readOnly && (
                        <ReviewActionBar item={item} busy={busy} onAct={onAct} onReject={onReject} />
                    )}
                </div>
            );
        }

        function GenericRow({ item, selected }) {
            return (
                <>
                    <div class="flex justify-between items-baseline gap-2">
                        <span class="text-sm font-bold line-clamp-1">{cleanStr(item.title, item.id)}</span>
                    </div>
                    <span class={`text-xs line-clamp-1 ${selected ? 'text-[#1e1e2e]' : 'text-[#9ca3af]'}`}>
                        {cleanStr(item.summary, "")}
                    </span>
                </>
            );
        }

        // Icon per type, and every type that actually reaches the queue gets its
        // own. A type without an entry here still renders — through
        // GENERIC_TEMPLATE — but it renders as an inbox tray, identical to every
        // other unregistered type, which in the rail is the same as having no
        // icon at all. The generic *row* is the right fallback; the generic
        // *icon* is only right for a type nobody has named yet.
        //
        // So the types below that have no bespoke Row/Detail still list one:
        // they reuse the generic renderers and differ only in glyph and label,
        // which is exactly the amount of per-type work they have earned.
        var REVIEW_TEMPLATES = {
            email_draft:     { label: 'Email draft',  icon: 'mail',            Row: EmailDraftRow, Detail: EmailDraftDetail },
            gmail_filter:    { label: 'Gmail filter', icon: 'filter',          Row: FilterRow,     Detail: FilterDetail },
            code_review:     { label: 'Code review',  icon: 'code',            Row: CodeReviewRow, Detail: CodeReviewDetail },
            crm_task:        { label: 'CRM task',     icon: 'clipboard-check', Row: GenericRow,    Detail: GenericDetail },
            crm_todo:        { label: 'CRM todo',     icon: 'clipboard-check', Row: GenericRow,    Detail: GenericDetail },
            crm_update:      { label: 'CRM update',   icon: 'contact',         Row: GenericRow,    Detail: GenericDetail },
            // notebook-text, matching the Memory tab in the nav rather than
            // picking a second glyph for the same subject: an item proposing a
            // fact and the tab that fact lands in should read as one thing.
            memory_fact:     { label: 'Memory fact',  icon: 'notebook-text',   Row: GenericRow,    Detail: GenericDetail },
            content_publish: { label: 'Content',      icon: 'megaphone',       Row: GenericRow,    Detail: GenericDetail },
        };
        var GENERIC_TEMPLATE = { label: 'Review item', icon: 'inbox', Row: GenericRow, Detail: GenericDetail };
        function templateFor(item) {
            return REVIEW_TEMPLATES[item && item.review_type] || GENERIC_TEMPLATE;
        }

