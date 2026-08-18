// --- 70-review-view.jsx -------------------------------------------------
// The Review tab: the queue rail, the item on screen, and the three overlays
// that belong to it (reject-with-reason, the shortcut legend, the alert toast).
// All of it used to be inline in App, along with seventeen pieces of state and
// the keyboard map — which is why the reject modal spent a while rendering from
// state nothing displayed.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
//
// Props and returned fields keep the names the moved code already used
// (`shownReviewQueue`, `bodyForActive`, `actOnActiveReview`, ...). The bodies
// below are the same code that ran inside App, moved and not rewritten, so a
// rename here would be a second change hiding inside the first.

        // Everything the Review tab knows, in one hook. App keeps only
        // `activeReviewId` — that is routing, and the URL owns it.
        //
        // Note what is NOT gated on the tab being open: the queue poll. The
        // Metrics sidebar and the tab bar both show the waiting count, so the
        // numbers have to be true while you are looking somewhere else. The
        // keyboard map is gated, because a stray `a` on another tab must not
        // approve anything.
        function useReview({ activeTab, settingsSection, healthOpen, activeReviewId, setActiveReviewId }) {
            const [reviewQueue, setReviewQueue] = useState([]);
            const [reviewAttention, setReviewAttention] = useState([]);
            const [approvalsLoading, setApprovalsLoading] = useState(true);
            // Which review_type the rail is showing, or 'all'. A queue that
            // mixes drafts, filters and whatever a producer invents next is
            // read one kind at a time; the filter is what makes that possible
            // without hunting. It never touches the detail pane — an item
            // reached by URL still renders while its type is filtered out.
            const [reviewTypeFilter, setReviewTypeFilter] = useState('all');
            const matchesReviewType = (item) =>
                reviewTypeFilter === 'all'
                || ((item && item.review_type) || 'unknown') === reviewTypeFilter;
            // An item addressed by URL that is no longer pending — already sent,
            // or failed. Fetched one at a time from /api/review/item/<id>.
            const [reviewFetched, setReviewFetched] = useState(null);
            const [reviewFetchState, setReviewFetchState] = useState('idle'); // idle | loading | missing
            const [reviewBusy, setReviewBusy] = useState(false);
            const [approvalHealth, setApprovalsHealth] = useState({});
            const [clientEdits, setClientEdits] = useState({}); // Keep pending text changes safe
            const [rejectionModalId, setRejectionModalId] = useState(null);
            const [rejectionReasonInput, setRejectionReasonInput] = useState('');
            const [confirmAction, setConfirmAction] = useState(null); // a destructive action awaiting a second press
            const [showAlert, setShowAlert] = useState(false);
            const [alertMessage, setAlertMessage] = useState('');
            const [showHelp, setShowHelp] = useState(false);

            const fetchApprovalsQueue = async () => {
                try {
                    const res = await fetch('/api/review/queue');
                    if (res.ok) {
                        const data = await res.json();
                        setReviewQueue(data.pending || []);
                        setReviewAttention(data.attention || []);
                    }
                } catch (err) {
                    console.error("Error fetching review queue:", err);
                } finally {
                    setApprovalsLoading(false);
                }
            };

            const fetchApprovalsHealth = async () => {
                try {
                    const res = await fetch('/api/review/health');
                    if (res.ok) {
                        const data = await res.json();
                        setApprovalsHealth(data);
                    }
                } catch (err) {
                    console.error("Error fetching review health status:", err);
                }
            };

            // The same 7s beat these two ran on inside App's shared polling
            // loop, now on their own interval rather than nine fetchers deep in
            // someone else's.
            useEffect(() => {
                fetchApprovalsQueue();
                fetchApprovalsHealth();
                const interval = setInterval(() => {
                    fetchApprovalsQueue();
                    fetchApprovalsHealth();
                }, 7000);
                return () => clearInterval(interval);
            }, []);

            const REVIEW_HEADERS = {
                'Content-Type': 'application/json',
                'X-Review-Confirm': '1',
            };

            const submitReviewDecision = async (id, decision, extra = {}) => {
                if (!id) return;
                setReviewBusy(true);
                try {
                    const res = await fetch('/api/review/decision', {
                        method: 'POST',
                        headers: REVIEW_HEADERS,
                        body: JSON.stringify({ id, decision, ...extra }),
                    });

                    if (res.ok) {
                        const data = await res.json();
                        setClientEdits(prev => {
                            const copy = { ...prev };
                            delete copy[id];
                            return copy;
                        });
                        // Say what happened, because the two approve verbs have
                        // different outcomes and "done" would not tell you which
                        // one you just did.
                        if (data.execution === 'queued') {
                            triggerAlert(data.action === 'send' ? 'Sending…' : 'Working on it…');
                        }
                        // Move to the next item rather than leaving the pane on
                        // one that is no longer in the list.
                        const remaining = reviewQueue.filter(i => i.id !== id);
                        setActiveReviewId(remaining.length ? remaining[0].id : null);
                        await fetchApprovalsQueue();
                        await fetchApprovalsHealth();
                    } else {
                        let message = `Decision failed (${res.status}).`;
                        try {
                            const data = await res.json();
                            message = data.message || data.detail || message;
                        } catch (e) {}
                        triggerAlert(message);
                    }
                } catch (err) {
                    console.error("Decision post failure:", err);
                    triggerAlert("Could not reach the review backend.");
                } finally {
                    setReviewBusy(false);
                }
            };

            const submitReviewRecovery = async (id, what) => {
                setReviewBusy(true);
                try {
                    const res = await fetch(`/api/review/${what}`, {
                        method: 'POST',
                        headers: REVIEW_HEADERS,
                        body: JSON.stringify({ id }),
                    });
                    if (!res.ok) {
                        let message = `Could not ${what} (${res.status}).`;
                        try { message = (await res.json()).detail || message; } catch (e) {}
                        triggerAlert(message);
                    } else {
                        setReviewFetched(null);
                        await fetchApprovalsQueue();
                    }
                } catch (err) {
                    triggerAlert("Could not reach the review backend.");
                } finally {
                    setReviewBusy(false);
                }
            };

            const triggerAlert = (msg) => {
                setAlertMessage(msg);
                setShowAlert(true);
                setTimeout(() => setShowAlert(false), 6000);
            };

            const registerApprovalEdit = (id, val) => {
                setClientEdits(prev => ({ ...prev, [id]: val }));
            };

            // The item on screen: normally from the list, but from the one-item
            // fetch when the URL names something already decided.
            const activeReviewItem = React.useMemo(() => {
                if (!activeReviewId) return null;
                return reviewQueue.find(i => i.id === activeReviewId)
                    || reviewAttention.find(i => i.id === activeReviewId)
                    || (reviewFetched && reviewFetched.id === activeReviewId ? reviewFetched : null);
            }, [activeReviewId, reviewQueue, reviewAttention, reviewFetched]);

            // The filter's own options, read off the queue rather than off the
            // template registry: a type with nothing waiting is a button that
            // can only ever empty the rail, and an unregistered type still has
            // items and so still gets a button (drawn with the generic icon).
            const reviewTypeOptions = React.useMemo(() => {
                const all = [...reviewAttention, ...reviewQueue];
                const byType = new Map();
                for (const item of all) {
                    const key = item.review_type || 'unknown';
                    const existing = byType.get(key);
                    if (existing) { existing.count += 1; continue; }
                    const template = templateFor(item);
                    byType.set(key, {
                        key,
                        icon: template.icon,
                        label: item.review_type_label || template.label,
                        count: 1,
                    });
                }
                return [
                    { key: 'all', icon: null, label: 'All types', count: all.length },
                    ...Array.from(byType.values()).sort((a, b) => a.label.localeCompare(b.label)),
                ];
            }, [reviewQueue, reviewAttention]);

            // A filter left pointing at a type the queue no longer holds hides
            // everything, with no button lit to explain why. Fall back to All.
            useEffect(() => {
                if (reviewTypeFilter === 'all') return;
                if (reviewTypeOptions.some(o => o.key === reviewTypeFilter)) return;
                setReviewTypeFilter('all');
            }, [reviewTypeOptions, reviewTypeFilter]);

            const shownReviewQueue = reviewQueue.filter(matchesReviewType);
            const shownReviewAttention = reviewAttention.filter(matchesReviewType);

            const bodyForActive = activeReviewItem
                ? (clientEdits[activeReviewItem.id] !== undefined
                    ? clientEdits[activeReviewItem.id]
                    : activeReviewItem.body)
                : '';

            // Perform one of the item's actions. A destructive one asks twice —
            // the first press arms it, the second commits — because "send this
            // email, as me, now" should not be one click away from a list you
            // are skimming.
            const actOnActiveReview = (action) => {
                const item = activeReviewItem;
                if (!item || !action || !action.available) return;
                if (action.destructive && confirmAction !== action.id) {
                    setConfirmAction(action.id);
                    triggerAlert(`${action.label} — press again to confirm.`);
                    setTimeout(() => setConfirmAction(null), 5000);
                    return;
                }
                setConfirmAction(null);
                const edited = clientEdits[item.id];
                const changed =
                    edited !== undefined && edited.trim() !== (item.body || '').trim();
                submitReviewDecision(item.id, 'approve', {
                    action: action.id,
                    ...(changed ? { edited_body: edited } : {}),
                });
            };

            const openRejectModal = () => {
                if (!activeReviewItem) return;
                setRejectionModalId(activeReviewItem.id);
                setRejectionReasonInput('');
            };

            const closeRejectModal = () => {
                setRejectionModalId(null);
            };

            const submitRejectionWithReason = () => {
                const reason = rejectionReasonInput.trim();
                if (!reason) return; // the modal's own button is disabled; this is the keyboard path
                const id = rejectionModalId;
                closeRejectModal();
                submitReviewDecision(id, 'reject', { rejection_reason: reason });
            };

            // With nothing addressed, open the top of the queue. Only when the
            // URL named nothing — otherwise a cold load of /review/<id> would be
            // snatched away by the first poll before its item arrived.
            useEffect(() => {
                if (activeTab !== 'review') return;
                if (activeReviewId) return;
                if (reviewQueue.length > 0) setActiveReviewId(reviewQueue[0].id);
            }, [activeTab, activeReviewId, reviewQueue]);

            // The URL names an item that is not in the queue. Either it was
            // decided already — the reload-after-approve case this endpoint
            // exists for — or it never existed. Ask, and let the pane say which.
            useEffect(() => {
                if (!activeReviewId) { setReviewFetched(null); setReviewFetchState('idle'); return; }
                if (reviewQueue.some(i => i.id === activeReviewId)) return;
                if (reviewAttention.some(i => i.id === activeReviewId)) return;
                if (reviewFetched && reviewFetched.id === activeReviewId) return;

                let cancelled = false;
                setReviewFetchState('loading');
                (async () => {
                    try {
                        const res = await fetch(`/api/review/item/${encodeURIComponent(activeReviewId)}`);
                        if (cancelled) return;
                        if (res.ok) {
                            setReviewFetched(await res.json());
                            setReviewFetchState('idle');
                        } else {
                            setReviewFetched(null);
                            setReviewFetchState('missing');
                        }
                    } catch (err) {
                        if (!cancelled) { setReviewFetched(null); setReviewFetchState('missing'); }
                    }
                })();
                return () => { cancelled = true; };
            }, [activeReviewId, reviewQueue, reviewAttention]);

            // Review shortcuts. These were advertised in the help legend and
            // never actually implemented — the legend was documenting an
            // intention. Bound only while the review tab is the thing on screen
            // and nothing is layered over it, matching the one-overlay-per-Esc
            // discipline the other handlers keep.
            //
            // The action keys are the answer to a type having more than one way
            // to approve: `a` is always the type's primary, safe action (create
            // a draft, file the labels), and anything destructive is a numbered
            // key AND asks a second time. Sending mail as yourself should not be
            // one unmodified keystroke away from a list you are skimming.
            useEffect(() => {
                if (activeTab !== 'review') return;
                if (settingsSection || healthOpen || rejectionModalId) return;

                const onKey = (e) => {
                    if (e.metaKey || e.ctrlKey || e.altKey) return;
                    const el = document.activeElement;
                    const typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA');
                    if (typing) {
                        // Esc is the way back out of the editor, and the only
                        // key that means anything while the cursor is in it.
                        if (e.key === 'Escape') { el.blur(); e.preventDefault(); }
                        return;
                    }

                    if (e.key === '?') { setShowHelp(v => !v); e.preventDefault(); return; }
                    if (e.key === 'Escape') { setShowHelp(false); return; }

                    // j/k walk what the rail is actually showing. Stepping onto
                    // a row the filter has hidden would move the selection with
                    // nothing on screen to show for it.
                    const list = [...reviewAttention, ...reviewQueue].filter(matchesReviewType);
                    if (e.key === 'j' || e.key === 'k') {
                        if (!list.length) return;
                        const at = list.findIndex(i => i.id === activeReviewId);
                        const next = e.key === 'j'
                            ? Math.min(list.length - 1, at + 1)
                            : Math.max(0, at <= 0 ? 0 : at - 1);
                        setActiveReviewId(list[next].id);
                        e.preventDefault();
                        return;
                    }

                    const item = activeReviewItem;
                    if (!item || (item.state && item.state !== 'pending')) return;

                    if (e.key === 'e') {
                        const box = document.getElementById('draft-body-editor');
                        if (box) { box.focus(); e.preventDefault(); }
                        return;
                    }
                    if (e.key === 'r') { openRejectModal(); e.preventDefault(); return; }

                    const actions = item.actions || [];
                    const ordered = [...actions.filter(a => a.primary), ...actions.filter(a => !a.primary)];
                    if (e.key === 'a') {
                        const primary = ordered.find(a => a.primary);
                        if (primary) { actOnActiveReview(primary); e.preventDefault(); }
                        return;
                    }
                    if (/^[1-9]$/.test(e.key)) {
                        const chosen = ordered[Number(e.key) - 1];
                        if (chosen) { actOnActiveReview(chosen); e.preventDefault(); }
                    }
                };

                document.addEventListener('keydown', onKey);
                return () => document.removeEventListener('keydown', onKey);
            }, [activeTab, settingsSection, healthOpen, rejectionModalId,
                activeReviewId, activeReviewItem, reviewQueue, reviewAttention,
                reviewTypeFilter, clientEdits, confirmAction]);

            return {
                reviewQueue, reviewAttention, approvalsLoading, approvalHealth,
                reviewTypeFilter, setReviewTypeFilter, reviewTypeOptions,
                shownReviewQueue, shownReviewAttention,
                activeReviewItem, reviewFetchState, reviewBusy,
                bodyForActive, registerApprovalEdit,
                actOnActiveReview, submitReviewRecovery,
                rejectionModalId, rejectionReasonInput, setRejectionReasonInput,
                openRejectModal, closeRejectModal, submitRejectionWithReason,
                showHelp, setShowHelp, showAlert, alertMessage,
            };
        }

        // The sidebar header's review controls: the legend toggle, and the type
        // filter drawn from what is actually queued — so it disappears entirely
        // on an empty queue and otherwise always shows All plus the types on
        // hand, including when that is one type, because the row is also the
        // queue's composition at a glance. Icons and counts, no labels: the same
        // icon the rows carry, so the button and the rows it keeps are one
        // symbol. The name is on hover.
        function ReviewSidebarControls({ review }) {
            const { reviewTypeOptions, reviewTypeFilter, setReviewTypeFilter, setShowHelp } = review;
            return (
                <>
                    <button
                        onClick={() => setShowHelp(prev => !prev)}
                        class="w-full bg-[#313244] hover:bg-[#45475a] text-[#cdd6f4] font-medium py-2 px-4 rounded-lg flex items-center justify-center gap-2 border border-[#45475a] text-xs uppercase font-mono"
                    >
                        <i data-lucide="help-circle" class="w-4 h-4"></i> Show Legend (?)
                    </button>

                    {reviewTypeOptions.length > 1 && (
                        <div class="flex items-stretch gap-0.5 p-0.5 bg-[#11111b] rounded-lg border border-[#313244]">
                            {reviewTypeOptions.map(opt => (
                                <button
                                    key={opt.key}
                                    onClick={() => setReviewTypeFilter(opt.key)}
                                    title={`${opt.label} (${opt.count})`}
                                    class={`flex-1 flex items-center justify-center gap-1 py-1 px-1 rounded transition ${
                                        reviewTypeFilter === opt.key
                                            ? 'bg-[#313244] text-[#b4befe]'
                                            : 'text-[#9ca3af] hover:text-[#cdd6f4]'
                                    }`}
                                >
                                    {opt.icon
                                        ? <i data-lucide={opt.icon} class="w-3.5 h-3.5"></i>
                                        : <span class="text-[9px] font-bold uppercase tracking-wider">All</span>}
                                    <span class={`text-[9px] font-bold ${
                                        reviewTypeFilter === opt.key ? 'text-[#b4befe]/70' : 'text-[#585b70]'
                                    }`}>{opt.count}</span>
                                </button>
                            ))}
                        </div>
                    )}
                </>
            );
        }

        // What is waiting for a human. Rows are drawn by the item's own template
        // — a filter rendered through the email row reads "Anonymous — —", which
        // is what made a mixed queue unreadable and kept filter_proposal()
        // switched off in the producer.
        function ReviewSidebarList({ review, activeReviewId, setActiveReviewId }) {
            const { shownReviewAttention, shownReviewQueue, reviewQueue, approvalsLoading } = review;
            return (
                <>
                    {/* Failures first, and in red. "This was supposed to have
                        gone out and did not" outranks every pending item. */}
                    {shownReviewAttention.length > 0 && (
                        <>
                            <h2 class="text-xs font-semibold uppercase tracking-wider text-[#f38ba8] px-3 mb-2">
                                Needs attention ({shownReviewAttention.length})
                            </h2>
                            {shownReviewAttention.map(item => {
                                const isSelected = item.id === activeReviewId;
                                const template = templateFor(item);
                                const Row = template.Row;
                                return (
                                    <button
                                        key={item.id}
                                        onClick={() => setActiveReviewId(item.id)}
                                        class={`w-full text-left p-3 rounded-lg flex items-start gap-2.5 transition mb-1 border-l-2 border-[#f38ba8] ${
                                            isSelected
                                                ? 'bg-[#f38ba8] text-[#11111b] font-medium'
                                                : 'hover:bg-[#313244] text-[#a6adc8]'
                                        }`}
                                    >
                                        <ReviewRowIcon item={item} selected={isSelected} />
                                        <div class="flex-1 min-w-0 flex flex-col gap-1">
                                            <Row item={item} selected={isSelected} />
                                            <span class={`text-[10px] line-clamp-1 ${isSelected ? 'text-[#11111b]' : 'text-[#f38ba8]'}`}>
                                                {((item.execution || {}).error || {}).message || 'Execution failed'}
                                            </span>
                                        </div>
                                    </button>
                                );
                            })}
                        </>
                    )}

                    <h2 class="text-xs font-semibold uppercase tracking-wider text-[#585b70] px-3 mb-2 mt-2">
                        Waiting for you{shownReviewQueue.length > 0 ? ` (${shownReviewQueue.length})` : ''}
                    </h2>
                    {approvalsLoading ? (
                        <div class="text-center py-6 text-sm text-[#585b70]">Loading queue…</div>
                    ) : shownReviewQueue.length === 0 ? (
                        <div class="text-center py-6 text-sm text-[#585b70]">
                            {reviewQueue.length === 0
                                ? 'Nothing needs reviewing.'
                                : 'Nothing of this type is waiting.'}
                        </div>
                    ) : (
                        shownReviewQueue.map(item => {
                            const isSelected = item.id === activeReviewId;
                            const Row = templateFor(item).Row;
                            return (
                                <button
                                    key={item.id}
                                    onClick={() => setActiveReviewId(item.id)}
                                    class={`w-full text-left p-3 rounded-lg flex items-start gap-2.5 transition ${
                                        isSelected
                                            ? 'bg-[#b4befe] text-[#11111b] font-medium'
                                            : 'hover:bg-[#313244] text-[#a6adc8]'
                                    }`}
                                >
                                    <ReviewRowIcon item={item} selected={isSelected} />
                                    <div class="flex-1 min-w-0 flex flex-col gap-1">
                                        <Row item={item} selected={isSelected} />
                                        <span class={`text-[10px] italic line-clamp-1 ${isSelected ? 'text-[#11111b]' : 'text-[#6b7280]'}`}>
                                            {cleanStr(item.summary || item.reason, "")}
                                        </span>
                                    </div>
                                </button>
                            );
                        })
                    )}
                </>
            );
        }

        // The review item, drawn by whichever template its review_type names,
        // with the type stated at the top so you always know what kind of thing
        // you are about to approve — and, since the actions differ by type, what
        // approving it will actually do.
        function ReviewMainPanel({ review, activeReviewId, setActiveReviewId }) {
            const {
                activeReviewItem, reviewFetchState, reviewBusy, bodyForActive,
                registerApprovalEdit, actOnActiveReview, submitReviewRecovery,
                openRejectModal,
            } = review;
            usePaintedIcons();
            return (
                <div class="flex flex-col h-full bg-[#000000]">
                    <div class="flex-1 overflow-hidden">
                        {!activeReviewId ? (
                            <div class="h-full flex items-center justify-center text-[#6b7280] font-medium italic">
                                Nothing selected. Move with J / K.
                            </div>
                        ) : reviewFetchState === 'loading' ? (
                            /* A skeleton, not the empty state: a cold load of
                               /review/<id> would otherwise flash "nothing
                               selected" and then the item, which reads as a bug
                               rather than a fetch. */
                            <div class="h-full flex items-center justify-center text-[#585b70] text-sm">
                                Loading item…
                            </div>
                        ) : !activeReviewItem ? (
                            <div class="h-full flex flex-col items-center justify-center gap-3 text-center px-8">
                                <div class="text-sm text-[#a6adc8]">
                                    No review item <span class="font-mono text-[#cdd6f4]">{activeReviewId}</span>.
                                </div>
                                <div class="text-xs text-[#6b7280] max-w-md">
                                    It may have been decided before this queue kept its history, or the
                                    link may be wrong.
                                </div>
                                <button
                                    class="text-xs uppercase tracking-wider border border-[#45475a] px-3 py-2 text-[#cdd6f4] hover:bg-[#313244]"
                                    onClick={() => setActiveReviewId(null)}
                                >
                                    Back to the queue
                                </button>
                            </div>
                        ) : (
                            (() => {
                                const item = activeReviewItem;
                                const Detail = templateFor(item).Detail;
                                // Anything not pending is history: it has
                                // already been acted on, so it is shown rather
                                // than offered.
                                const decided = item.state && item.state !== 'pending';
                                const execution = item.execution || {};
                                const failed = item.state === 'failed' || execution.state === 'failed';

                                return (
                                    <div class="flex flex-col h-full">
                                        {decided && (
                                            <div class={`px-5 py-3 text-xs border-b flex items-center justify-between gap-4 ${
                                                failed
                                                    ? 'bg-[#2d1418] border-[#f38ba8] text-[#f38ba8]'
                                                    : 'bg-[#11111b] border-[#2d3748] text-[#a6adc8]'
                                            }`}>
                                                <div class="flex flex-col gap-0.5">
                                                    <span class="font-bold uppercase tracking-wider">
                                                        {failed ? 'Execution failed'
                                                            : item.decision === 'reject' ? 'Rejected'
                                                            : execution.state === 'done' ? 'Approved and done'
                                                            : 'Approved'}
                                                        {item.action && item.decision !== 'reject' && (
                                                            <span class="opacity-70"> · {item.action}</span>
                                                        )}
                                                    </span>
                                                    <span class="opacity-80">
                                                        {failed
                                                            ? (execution.error || {}).message
                                                            : item.decision === 'reject'
                                                                ? item.rejection_reason
                                                                : item.decided_at}
                                                    </span>
                                                </div>
                                                {failed && (
                                                    <div class="flex gap-2 shrink-0">
                                                        {/* Retry is offered only when the
                                                            failure is one we can be sure
                                                            did not already take effect.
                                                            An ambiguous send is not. */}
                                                        {(execution.error || {}).retryable !== false && (
                                                            <button
                                                                disabled={reviewBusy}
                                                                onClick={() => submitReviewRecovery(item.id, 'retry')}
                                                                class="px-3 py-1.5 border border-[#f38ba8] text-[10px] uppercase tracking-wider hover:bg-[#f38ba8] hover:text-[#11111b] disabled:opacity-40"
                                                            >
                                                                Retry
                                                            </button>
                                                        )}
                                                        <button
                                                            disabled={reviewBusy}
                                                            onClick={() => submitReviewRecovery(item.id, 'dismiss')}
                                                            class="px-3 py-1.5 border border-[#45475a] text-[10px] uppercase tracking-wider text-[#a6adc8] hover:bg-[#313244] disabled:opacity-40"
                                                        >
                                                            Dismiss
                                                        </button>
                                                    </div>
                                                )}
                                            </div>
                                        )}
                                        <div class="flex flex-1 overflow-hidden">
                                            <Detail
                                                item={item}
                                                body={bodyForActive}
                                                onBody={(v) => registerApprovalEdit(item.id, v)}
                                                busy={reviewBusy}
                                                readOnly={decided}
                                                onAct={actOnActiveReview}
                                                onReject={openRejectModal}
                                            />
                                            <ProvenancePanel item={item} />
                                        </div>
                                    </div>
                                );
                            })()
                        )}
                    </div>
                </div>
            );
        }

        // The review overlays. These used to be static divs rendered OUTSIDE
        // <App/>, toggled by window.* functions installed in a load listener —
        // while App kept its own rejectionModalId / showHelp / showAlert state
        // that nothing rendered from. The upshot was that clicking Reject did
        // nothing at all. They are driven by the state that was always there.
        function ReviewOverlays({ review }) {
            const {
                rejectionModalId, rejectionReasonInput, setRejectionReasonInput,
                closeRejectModal, submitRejectionWithReason, reviewBusy,
                showHelp, setShowHelp, activeReviewItem, showAlert, alertMessage,
            } = review;
            return (
                <>
                    {rejectionModalId && (
                        <div class="fixed inset-0 bg-black/85 flex items-center justify-center z-[100]"
                             onClick={(e) => { if (e.target === e.currentTarget) closeRejectModal(); }}>
                            <div class="bg-[#111827] border-2 border-[#ef4444] w-96 p-5">
                                <div class="text-sm font-bold text-[#ef4444] mb-2 uppercase">Confirm rejection</div>
                                <p class="text-[11px] text-[#9399b2] mb-3 leading-normal">
                                    A reason is required. It is the only feedback the pipeline that
                                    produced this ever gets, so "no" without one teaches it nothing.
                                </p>
                                <input
                                    type="text"
                                    autoFocus
                                    value={rejectionReasonInput}
                                    onInput={(e) => setRejectionReasonInput(e.target.value)}
                                    onKeyDown={(e) => {
                                        if (e.key === 'Enter') submitRejectionWithReason();
                                        if (e.key === 'Escape') closeRejectModal();
                                    }}
                                    class="w-full bg-[#11111b] text-[#cdd6f4] border border-[#374151] p-2 text-xs outline-none focus:border-[#ef4444] mb-4 font-sans"
                                    placeholder="e.g. Tone too formal for this connection"
                                />
                                <div class="flex justify-end gap-3 font-sans">
                                    <button onClick={closeRejectModal}
                                            class="py-2 px-4 bg-[#313244] hover:bg-[#45475a] text-[#cdd6f4] text-xs font-bold uppercase transition">
                                        Cancel
                                    </button>
                                    <button onClick={submitRejectionWithReason}
                                            disabled={!rejectionReasonInput.trim() || reviewBusy}
                                            class="py-2 px-4 bg-[#ef4444] hover:bg-[#f87171] text-[#11111b] text-xs font-bold uppercase transition disabled:opacity-40">
                                        Reject
                                    </button>
                                </div>
                            </div>
                        </div>
                    )}

                    {/* The legend lists the selected item's ACTUAL actions.
                        The old one hardcoded "a — Approve selected", which was
                        wrong twice over once a type could offer two different
                        approvals — and was documenting shortcuts that had never
                        been implemented. */}
                    {showHelp && (
                        <div class="fixed bottom-5 right-5 bg-[#111827] border-2 border-[#374151] p-4 w-80 z-50 text-xs text-[#bac2de] font-sans">
                            <div class="font-bold text-[#cdd6f4] border-b border-[#374151] pb-1.5 mb-2.5 flex justify-between uppercase">
                                <span>Shortcuts (review)</span>
                                <span class="text-[#6b7280] cursor-pointer" onClick={() => setShowHelp(false)}>ESC</span>
                            </div>
                            <div class="flex justify-between py-0.5 font-mono"><span class="text-[#89b4fa] font-bold">j / k</span> <span>Next / previous item</span></div>
                            {(activeReviewItem?.actions || []).map((a, i) => (
                                <div key={a.id} class="flex justify-between py-0.5 font-mono gap-3">
                                    <span class="text-[#89b4fa] font-bold shrink-0">{a.primary ? 'a' : i + 1}</span>
                                    <span class="text-right">{a.label}{a.destructive ? ' (confirms)' : ''}</span>
                                </div>
                            ))}
                            <div class="flex justify-between py-0.5 font-mono"><span class="text-[#89b4fa] font-bold">r</span> <span>Reject</span></div>
                            <div class="flex justify-between py-0.5 font-mono"><span class="text-[#89b4fa] font-bold">e</span> <span>Edit the body</span></div>
                            <div class="flex justify-between py-0.5 font-mono"><span class="text-[#89b4fa] font-bold">?</span> <span>Toggle this help</span></div>
                        </div>
                    )}

                    {showAlert && (
                        <div class="fixed top-5 left-1/2 -translate-x-1/2 bg-[#ef4444] text-[#000000] py-3 px-6 text-sm font-bold uppercase z-[200]">
                            {alertMessage}
                        </div>
                    )}
                </>
            );
        }
