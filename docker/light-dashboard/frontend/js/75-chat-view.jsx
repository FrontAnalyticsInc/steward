// --- 75-chat-view.jsx ---------------------------------------------------
// The Chat tab: the session rail, the conversation, the composer, the context
// and connections panel pinned under the rail, and the reading pane for a
// context file. This was the largest single thing left in App — the transcript
// renderer alone ran to nearly two hundred lines, and the scroll model that
// keeps a streaming turn pinned to the bottom without fighting the reader is
// another hundred.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
//
// Props and returned fields keep the names the moved code already used. The
// bodies are the same code that ran inside App, moved and not rewritten.

        // Everything the Chat tab is. App keeps `activeSessionId`, because that
        // is routing and the URL owns it, and reads five things back out:
        // `sessions` for the Metrics sidebar count, `unreadSessionCount` for the
        // tab badge, `apiDown` for the header, and `selectSession` /
        // `chatAboutAutomation`, which the Kanban and Automations panes call to
        // send you here.
        //
        // The session poll is not gated on the tab: the unread badge and the
        // "is the API answering" indicator in the header are both read from it,
        // and both have to be true while you are looking somewhere else.
        function useChat({
            activeTab, activeSessionId, setActiveSessionId,
            navigateTab, setActiveKanbanTaskId, healthOpen,
        }) {

            // --- Shared Data States ---
            const [sessions, setSessions] = useState([]);
            const [archivedSessions, setArchivedSessions] = useState([]);
            const [showArchived, setShowArchived] = useState(false); // Collapsed by default
            const [messages, setMessages] = useState([]);

            // --- Context (chat sidebar) ---
            // The markdown that makes Hermes itself: SOUL.md, memories/USER.md.
            // Hardcoded to the default agent — this sits in the chat sidebar, and
            // chat always talks to default. Per-profile context stays on the
            // Agents tab, which has its own selection.
            const [ctxFiles, setCtxFiles] = useState([]);
            const [ctxLoading, setCtxLoading] = useState(true);
            const [ctxOpen, setCtxOpen] = useState(false);
            const [ctxDoc, setCtxDoc] = useState(null);      // {name, rel_path}
            const [ctxDocBody, setCtxDocBody] = useState('');
            const [ctxDocLoading, setCtxDocLoading] = useState(false);

            // --- Chat connections ---
            // What the conversation on this screen can reach, from the same
            // grant data the Integrations tab shows. It lives beside Context
            // because it is the other half of the same question — what Hermes
            // reads as itself, and what it can touch outside itself.
            const [chatGrants, setChatGrants] = useState([]);
            const [chatGrantsLoading, setChatGrantsLoading] = useState(true);
            const [connOpen, setConnOpen] = useState(false);

            // --- Incremental Search States ---
            const [chatSearch, setChatSearch] = useState('');

            // --- Loading States ---
            const [chatLoading, setChatLoading] = useState(false);
            const [sessionsLoading, setSessionsLoading] = useState(true);

            const [chatSending, setChatSending] = useState(false);
            const [chatInput, setChatInput] = useState('');
            // So a seeded message can put the cursor where you would have to
            // click otherwise — see chatAboutAutomation.
            const chatInputRef = useRef(null);
            // Which tool chips / reasoning traces the reader has opened, keyed by
            // entry id. Collapsed is the default: a working turn produces a dozen
            // steps, and the point of the chips is that they stay one line each.
            const [openSteps, setOpenSteps] = useState({});

            const chatScrollRef = useRef(null);

            // Where the reader was in each conversation, keyed by session id.
            // The chat pane is mounted only while its tab is open, so leaving
            // for Kanban and coming back destroys the browser's own scroll
            // position — this is what makes returning resume instead of replay.
            const chatScrollMemoryRef = useRef({});
            // A chat with no session id yet is still a place you can be sitting.
            const CHAT_DRAFT_KEY = '__new__';
            const chatMemoryKey = () => activeSessionId || CHAT_DRAFT_KEY;
            // Which session the loaded `messages` belong to. Set on the id
            // BEFORE the state lands, so scroll restoration can tell "the new
            // session's history" from "the old session's, still on screen" —
            // selecting a session changes the id a fetch earlier than the list.
            const messagesSessionRef = useRef(null);
            // The memory key whose position has been applied to the live
            // viewport. Restoring is once per conversation per mount; while it
            // still disagrees with the current key, no automatic scrolling of
            // any kind may run, because the reader's position is not yet known.
            const restoredKeyRef = useRef(null);
            // Set by the restore below and consumed by the follow effect in the
            // same commit, so the render that restores a position does not then
            // animate away from it.
            const justRestoredRef = useRef(false);

            // The id a draft chat will claim when it is first sent. Minted here
            // rather than left to the gateway, which derives an id by hashing
            // the system prompt and the first user message — so every "New Chat"
            // opened with a message it has seen before silently resumes that
            // conversation instead of starting one. Asking the same opening
            // question twice is normal, and it was collapsing months of
            // unrelated chats into a handful of very long sessions.
            const pendingSessionIdRef = useRef(null);
            // Minted at send, not at click: opening a draft and abandoning it
            // should cost nothing, and the id has to survive until the turn
            // that creates the session server-side.
            const chatSessionIdForSend = () => {
                if (activeSessionId) return activeSessionId;
                if (!pendingSessionIdRef.current) {
                    const rand = Math.random().toString(16).slice(2, 10);
                    pendingSessionIdRef.current = `dash-${Date.now().toString(36)}-${rand}`;
                }
                return pendingSessionIdRef.current;
            };

            // Emptying the transcript back to a blank draft. The stored offset
            // for the old draft would otherwise be restored into this one, and
            // the pending id has to go too — reusing it would append the next
            // conversation to the one just cleared.
            const resetChatPosition = () => {
                delete chatScrollMemoryRef.current[CHAT_DRAFT_KEY];
                messagesSessionRef.current = null;
                restoredKeyRef.current = null;
                followChatRef.current = true;
                pendingSessionIdRef.current = null;
            };

            // A draft chat becomes a real session mid-turn. Carry the position
            // across the rename rather than letting it read as a switch, which
            // would stop the transcript following its own streaming reply.
            const adoptChatSession = (sessionId) => {
                const draft = chatScrollMemoryRef.current[CHAT_DRAFT_KEY];
                if (draft) chatScrollMemoryRef.current[sessionId] = draft;
                delete chatScrollMemoryRef.current[CHAT_DRAFT_KEY];
                if (restoredKeyRef.current === CHAT_DRAFT_KEY) restoredKeyRef.current = sessionId;
                messagesSessionRef.current = sessionId;
                // The draft has a real session now; the next one mints its own.
                pendingSessionIdRef.current = null;
                setActiveSessionId(sessionId);
            };
            // The chip being opened or closed, and where it sat on screen when
            // it was clicked. Read back after layout to hold it exactly there.
            const stepAnchorRef = useRef(null);

            // Opening a step changes the height of the document above whatever
            // the reader is looking at, which would slide the transcript under
            // them. Measure the clicked row first, then put it back where it
            // was once the new height is laid out — so expanding and collapsing
            // move nothing but the row's own disclosure.
            const toggleStep = (id, e) => {
                const row = e && e.currentTarget;
                stepAnchorRef.current = row ? { row, top: row.getBoundingClientRect().top } : null;
                setOpenSteps(prev => ({ ...prev, [id]: !prev[id] }));
            };

            // Answer the approval a running turn is blocked on. The turn is
            // still live: the reply here only reports whether the decision
            // landed, and the stream itself shows what the agent then did.
            const decideApproval = async (id, token, choice) => {
                setMessages(prev => prev.map(m =>
                    m.id === id ? { ...m, busy: true, note: '' } : m));
                try {
                    const res = await fetch(`/api/chat/approvals/${encodeURIComponent(token)}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ choice })
                    });
                    if (!res.ok) {
                        const body = await res.json().catch(() => ({}));
                        // A lapsed approval is already a refusal upstream, so
                        // the card has to stop offering to allow it.
                        const gone = res.status === 404 || res.status === 409;
                        setMessages(prev => prev.map(m => m.id === id ? {
                            ...m, busy: false, decided: gone ? 'expired' : '',
                            note: body.detail || 'Could not send that decision.'
                        } : m));
                        return;
                    }
                    setMessages(prev => prev.map(m =>
                        m.id === id ? { ...m, busy: false, decided: choice, note: '' } : m));
                } catch (err) {
                    console.error('Approval decision failed:', err);
                    setMessages(prev => prev.map(m => m.id === id ? {
                        ...m, busy: false, note: 'Could not reach the dashboard.'
                    } : m));
                }
            };

            // A pending approval expires on a clock the user cannot see, and
            // letting it lapse counts as a refusal — so the countdown has to
            // tick rather than showing whatever it said on arrival. Only runs
            // while something is actually waiting.
            const hasPendingApproval = messages.some(
                m => m.kind === 'approval' && !m.decided
            );
            const [, forceTick] = useState(0);
            useEffect(() => {
                if (!hasPendingApproval) return;
                const t = setInterval(() => forceTick(n => n + 1), 1000);
                return () => clearInterval(t);
            }, [hasPendingApproval]);

            // Runs before the browser paints, so the correction is never seen.
            useLayoutEffect(() => {
                const anchor = stepAnchorRef.current;
                stepAnchorRef.current = null;
                const viewport = chatScrollRef.current;
                if (!anchor || !viewport || !anchor.row.isConnected) return;
                const drift = anchor.row.getBoundingClientRect().top - anchor.top;
                if (drift) viewport.scrollTop += drift;
            }, [openSteps]);

            // Icons for the chevron that just flipped. Deliberately separate from
            // the scroll effect below: that one jumps to the bottom, and a
            // disclosure toggle must not move the transcript at all.
            useEffect(() => {
                const paint = setTimeout(renderIcons, 50);
                return () => clearTimeout(paint);
            }, [openSteps]);

            // Null while the API is answering. A working connection is the
            // expected case and says nothing worth a permanent badge, so this
            // renders only when it is NOT null — and then carries the reason.
            const [apiDown, setApiDown] = useState(null);

            // --- API Poll Callers ---

            // What the rail lists and what opening the dashboard drops you into
            // are two different questions. The rail shows every session there
            // is — a cron run, a kanban dispatcher, a Telegram thread — because
            // seeing what the agent has been doing without you is the point of
            // it. Landing in one of those is another matter: a cron session
            // carries "you are running as a scheduled cron job" in its history,
            // and every message typed into it is answered under that framing.
            //
            // Only sessions this dashboard itself created are somewhere you can
            // be returned to. That is `api_server` — the source the gateway
            // stamps on a turn arriving over /v1/chat/completions, which is the
            // only way this tab talks to it.
            const isOwnChat = (s) => s && s.source === 'api_server';

            // The auto-select below reads `activeSessionId` from the closure it
            // was called in, and the call that matters is the one on mount —
            // whose closure is always null. Clicking New Chat while that request is
            // still in flight would therefore be overwritten by it. The ref is
            // the current answer rather than the mount-time one.
            const activeSessionIdRef = useRef(activeSessionId);
            activeSessionIdRef.current = activeSessionId;
            // Set once the landing selection has been made (or declined), so a
            // later poll can never re-run it and pull the reader out of a draft.
            const autoSelectedRef = useRef(false);

            // Fetch recent active chat sessions (supports search)
            const fetchSessions = async (selectLatest = false, searchTerm = chatSearch) => {
                try {
                    let url = '/api/sessions';
                    if (searchTerm) {
                        url += `?search=${encodeURIComponent(searchTerm)}`;
                    }
                    const res = await fetch(url);
                    if (res.ok) {
                        const data = await res.json();
                        setSessions(data);
                        setApiDown(null);
                        if (selectLatest && !autoSelectedRef.current && !activeSessionIdRef.current) {
                            autoSelectedRef.current = true;
                            // The newest conversation you actually had here,
                            // not the newest row — which for a machine that
                            // runs cron all day is almost never yours.
                            const landing = data.find(isOwnChat);
                            if (landing) {
                                setActiveSessionId(landing.id);
                                fetchMessages(landing.id);
                            }
                            // Nothing of yours yet: stay on the empty draft.
                        }
                    } else {
                        setApiDown(`The dashboard API answered ${res.status} ${res.statusText || ''} for /api/sessions.`);
                    }
                } catch (err) {
                    console.error("Error fetching sessions:", err);
                    setApiDown(`Could not reach the dashboard API: ${err.message}. The backend may be restarting.`);
                } finally {
                    setSessionsLoading(false);
                }
            };

            // Fetch archived chat sessions (supports search)
            const fetchArchivedSessions = async (searchTerm = chatSearch) => {
                try {
                    let url = '/api/sessions/archived';
                    if (searchTerm) {
                        url += `?search=${encodeURIComponent(searchTerm)}`;
                    }
                    const res = await fetch(url);
                    if (res.ok) {
                        const data = await res.json();
                        setArchivedSessions(data);
                    }
                } catch (err) {
                    console.error("Error fetching archived sessions:", err);
                }
            };

            // Fetch messages for active session
            const fetchMessages = async (sessionId) => {
                try {
                    const res = await fetch(`/api/sessions/${sessionId}/messages`);
                    if (res.ok) {
                        const data = await res.json();
                        // Stamped with the render, not after it: the effect that
                        // places the viewport runs on this state change and has
                        // to know the history it is measuring is the right one.
                        messagesSessionRef.current = sessionId;
                        setMessages(data);
                    }
                } catch (err) {
                    console.error("Error fetching messages:", err);
                }
            };

            // Swap a just-streamed turn for the stored one, once. The stream is
            // assembled in the browser; the transcript is what the gateway
            // actually wrote, and a reload will show that. Only replaces when the
            // stored copy already ends in the reply we just watched arrive —
            // otherwise the write has not landed yet and the live render is the
            // better of the two.
            const reconcileMessages = async (sessionId) => {
                try {
                    const res = await fetch(`/api/sessions/${sessionId}/messages`);
                    if (!res.ok) return;
                    const data = await res.json();
                    if (!Array.isArray(data) || data.length === 0) return;
                    const last = data[data.length - 1];
                    if (last.kind === 'assistant' && (last.content || '').trim()) setMessages(data);
                } catch (err) {
                    console.error("Error reconciling messages:", err);
                }
            };

            // The default agent's context markdown. Loaded once on mount rather
            // than on the 7s loop: these files change when a human edits them,
            // not on a timer.
            const fetchHermesContext = async () => {
                try {
                    const res = await fetch('/api/agents/default/context');
                    if (res.ok) {
                        const data = await res.json();
                        setCtxFiles(Array.isArray(data) ? data : []);
                    }
                } catch (err) {
                    console.error("Error fetching Hermes context:", err);
                } finally {
                    setCtxLoading(false);
                }
            };

            // Open one context file in the reading pane overlay
            const openHermesDoc = async (f) => {
                setCtxDoc(f);
                setCtxDocBody('');
                setCtxDocLoading(true);
                try {
                    const res = await fetch(
                        `/api/agents/default/content?rel_path=${encodeURIComponent(f.rel_path)}`);
                    if (res.ok) {
                        const data = await res.json();
                        setCtxDocBody(data.content || '');
                    } else {
                        setCtxDocBody('Could not load this file.');
                    }
                } catch (err) {
                    console.error("Error loading context doc:", err);
                    setCtxDocBody('Could not load this file.');
                } finally {
                    setCtxDocLoading(false);
                }
            };

            // The same 7s beat these ran on inside App's shared polling loop.
            // Not gated on the tab: the unread badge in the tab bar and the
            // "is the API answering" indicator in the header both read from
            // this, so it has to stay true while you are looking elsewhere.
            //
            // The context files are fetched once rather than polled — they are
            // markdown on disk that changes when someone edits it, not state.
            useEffect(() => {
                fetchSessions(true);
                fetchArchivedSessions();
                fetchHermesContext();
                const interval = setInterval(() => {
                    fetchSessions(false);
                    fetchArchivedSessions();
                }, 7000);
                return () => clearInterval(interval);
            }, []);

            // What the chat consumer can reach. Same grants as the Integrations
            // tab, read from the consumer end, so the sidebar can never claim a
            // connection the tab calls broken.
            const fetchChatGrants = async () => {
                try {
                    const res = await fetch('/api/integrations/consumer/api_server?origin=mcp');
                    if (res.ok) {
                        const data = await res.json();
                        setChatGrants(Array.isArray(data.grants) ? data.grants : []);
                    }
                } catch (err) {
                    console.error("Error fetching chat connections:", err);
                } finally {
                    setChatGrantsLoading(false);
                }
            };

            // Follow a growing conversation — but only for a reader who is
            // already at the bottom. This used to scroll unconditionally, on a
            // dependency list that includes every polled dataset in the app, so
            // any 7-second refresh yanked the viewport to the newest message
            // while someone was reading back through the history.
            // True while the reader is parked at the newest message. Updated as
            // they scroll, so the decision to follow is always based on where
            // they were before the next message landed. Starts true: a fresh
            // chat is at its bottom by definition.
            const followChatRef = useRef(true);
            // True while a scroll this code performed is still settling. The
            // handler below must not read those events as the reader's own
            // position, or following becomes self-sustaining: every jump to the
            // bottom would observe itself at the bottom and re-arm.
            const programmaticScrollRef = useRef(false);

            // Jump to the newest message. Instant, by assignment — a smooth
            // scrollIntoView animates over many frames, and a streaming reply
            // restarts that animation on every token. The result was a scroll
            // that could not be escaped: the running animation dragged the
            // viewport back down mid-gesture, and each of its own frames landed
            // near the bottom and re-armed following. Reading a long reply while
            // it streamed was impossible. An instant jump has no frames to fight.
            const jumpChatToEnd = () => {
                const el = chatScrollRef.current;
                if (!el) return;
                programmaticScrollRef.current = true;
                el.scrollTop = el.scrollHeight;
                // Released after the event this assignment queues has been
                // delivered. Scroll events land before the next animation frame,
                // and if the viewport was already at the bottom none fires at
                // all — so the frame is the reliable place to let go.
                requestAnimationFrame(() => { programmaticScrollRef.current = false; });
            };

            const onChatScroll = (e) => {
                const el = e.currentTarget;
                const atBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 160;
                if (!programmaticScrollRef.current) followChatRef.current = atBottom;
                // Remembered as an offset AND as a verdict, because a scrollTop
                // replayed into a transcript that has grown since is no longer
                // the bottom. Someone who left at the newest message wants the
                // newest message, not the pixel it used to be at. The verdict is
                // taken from the follow state so a jump's own event cannot
                // record "at the bottom" over a reader who is not.
                chatScrollMemoryRef.current[chatMemoryKey()] = {
                    top: el.scrollTop,
                    atBottom: programmaticScrollRef.current ? followChatRef.current : atBottom,
                };
            };

            // Turning the wheel up, or reaching for the keys that page back, is
            // an unambiguous request to stop being dragged along — acted on
            // directly rather than inferred from the resulting position, so it
            // takes effect on the first notch instead of after the next token
            // has already pulled the viewport back down.
            const onChatWheel = (e) => {
                if (e.deltaY < 0) followChatRef.current = false;
            };
            const CHAT_BACK_KEYS = ['PageUp', 'ArrowUp', 'Home'];
            const onChatKeyDown = (e) => {
                if (CHAT_BACK_KEYS.includes(e.key)) followChatRef.current = false;
            };
            // Touch has no direction until the finger moves, and the scroll
            // handler reads that correctly on its own. Lifting the follow on
            // first contact is what matters: it stops the next token arriving
            // mid-drag from yanking the transcript out from under the finger.
            const onChatTouchStart = () => {
                followChatRef.current = false;
            };

            // Put the reader back where they were, before the browser paints —
            // so opening a conversation shows its position rather than
            // travelling to it. A smooth scroll here is what produced the long
            // crawl from the top of the history on every tab switch: the
            // remounted viewport starts at zero and animates the whole way down.
            useLayoutEffect(() => {
                // Chat is unmounted; the next open has to place itself again.
                if (activeTab !== 'chat') { restoredKeyRef.current = null; return; }
                const viewport = chatScrollRef.current;
                if (!viewport) return;
                // The on-screen history still belongs to the session being left.
                // Measuring it would restore one conversation into another.
                if (messagesSessionRef.current !== activeSessionId) return;
                const key = chatMemoryKey();
                if (restoredKeyRef.current === key) return;
                restoredKeyRef.current = key;
                const saved = chatScrollMemoryRef.current[key];
                if (saved && !saved.atBottom) {
                    programmaticScrollRef.current = true;
                    viewport.scrollTop = saved.top;
                    requestAnimationFrame(() => { programmaticScrollRef.current = false; });
                    followChatRef.current = false;
                } else {
                    // Never opened, or left at the newest message: the bottom.
                    // Assigned rather than scrolled into view, so it is a paint
                    // at the right place instead of a visible journey to it.
                    followChatRef.current = true;
                    jumpChatToEnd();
                }
                justRestoredRef.current = true;
            }, [activeTab, activeSessionId, messages]);

            useEffect(() => {
                if (activeTab !== 'chat') return;
                // Position not established yet — either the history for this
                // session has not arrived or the restore above has not run.
                // Following now would scroll a transcript the reader has not
                // been placed in.
                if (restoredKeyRef.current !== chatMemoryKey()) return;
                // Layout effects run before this one in the same commit, so a
                // just-restored position is still the newest fact about where
                // the reader is. Honour it and wait for real new content.
                if (justRestoredRef.current) { justRestoredRef.current = false; return; }
                // Whether to follow is decided by where the reader was BEFORE
                // this content arrived, which is what the scroll handler records.
                // Measuring here instead would ask the question after the new
                // message has already pushed the bottom away — a long reply
                // would look like "scrolled up" and streaming would stop
                // following on its own first paragraph.
                if (followChatRef.current) jumpChatToEnd();
            }, [messages, chatSending, activeTab, activeSessionId]);

            // Chat connections, on the same 30s clock as the Integrations tab and
            // only while chat is open. Kept off the 7s loop deliberately: the
            // call re-parses the workflows source, and paying that eight times a
            // minute for a collapsed sidebar accordion is not worth it.
            useEffect(() => {
                if (activeTab !== 'chat') return;
                fetchChatGrants();
                const interval = setInterval(fetchChatGrants, 30000);
                return () => clearInterval(interval);
            }, [activeTab]);

            // --- Action Submitters ---

            // Open one conversation. A conversation is a place rather than a
            // step inside the chat tab, so this pushes: Back returns to the
            // thread you were reading, not to whichever tab you were on before
            // chat. null is the draft — it has an address too, so leaving a
            // conversation to start a new one is also something Back undoes.
            //
            // The transcript is not loaded here. It follows activeSessionId
            // from the effect below, which is the only way Back, Forward, a
            // click in the rail and a cold load of /chat/<id> can be relied on
            // to land in the same state.
            const selectSession = useCallback((sessionId) => {
                const want = pathForRoute({ tab: 'chat', sessionId });
                if (window.location.pathname !== want) {
                    window.history.pushState({ tab: 'chat', sessionId }, '', want);
                }
                setSettingsSection(null);
                setActiveTab('chat');
                setActiveSessionId(sessionId);
            }, []);

            // Whatever is selected is what is on screen. Guarded on
            // messagesSessionRef — the record of which session `messages`
            // belongs to — so a re-render does not refetch a history already
            // loaded, and skipped mid-turn so a streaming reply is never
            // replaced by the stored copy before it has finished arriving.
            useEffect(() => {
                if (chatSending) return;
                if (messagesSessionRef.current === activeSessionId) return;
                if (!activeSessionId) {
                    resetChatPosition();
                    setMessages([]);
                    return;
                }
                fetchMessages(activeSessionId);
            }, [activeSessionId, chatSending]);

            // Tell the backend this conversation has been looked at, and drop the
            // marks here rather than waiting for the next poll — clicking a bold
            // session and watching it stay bold for seven seconds reads as a
            // broken feature, not as a slow one.
            const markSessionRead = async (sessionId) => {
                if (!sessionId) return;
                setSessions(prev => prev.map(s => s.id === sessionId ? { ...s, unread_count: 0 } : s));
                try {
                    await fetch(`/api/sessions/${sessionId}/read`, { method: 'POST' });
                } catch (err) {
                    console.error("Error marking session read:", err);
                }
            };

            // The session on screen is being read by definition. Held until the
            // turn finishes so a streaming reply does not post on every token,
            // and keyed on what has actually been seen so a poll that changes
            // nothing does not repost.
            const lastReadMarkRef = useRef('');
            useEffect(() => {
                if (activeTab !== 'chat' || !activeSessionId || chatSending) return;
                const seen = `${activeSessionId}:${messages.length}`;
                if (lastReadMarkRef.current === seen) return;
                lastReadMarkRef.current = seen;
                markSessionRead(activeSessionId);
            }, [activeTab, activeSessionId, chatSending, messages.length]);

            // The open conversation is excluded: it is being read right now, and
            // counting it would leave a badge sitting next to the tab you are
            // already looking at.
            const unreadSessionCount = sessions.filter(
                s => s.unread_count > 0 && s.id !== activeSessionId
            ).length;

            // Cleared here as well as by the effect above, not instead of it: a
            // draft that never became a session is already at activeSessionId
            // null, so the selection does not change and the effect has nothing
            // to react to — but the half-typed turn on screen still has to go.
            const startNewChat = () => {
                resetChatPosition();
                setMessages([]);
                selectSession(null);
            };

            // "Chat about this automation": a fresh conversation that opens
            // already knowing which job you mean.
            //
            // It seeds the box rather than sending it. A button that fires an
            // agent turn on one click spends a model call on a question nobody
            // has asked yet — and the useful question ("why did it fail", "can
            // this run hourly") is the part only you can supply. So the identity
            // goes in, the cursor lands after it, and the turn is still yours to
            // send.
            //
            // What goes in is what the agent cannot look up from a name: the job
            // id, whose profile owns it, and what it actually executes. Those are
            // the handles for `hermes cron` and for finding the script.
            const chatAboutAutomation = (job) => {
                const bits = [`job ${job.id}`];
                if (job.agent) bits.push(`profile ${job.agent}`);
                const sched = formatSchedule(job);
                if (sched) bits.push(sched);
                if (job.adk_app) bits.push(`app ${job.adk_app}`);
                else if (job.script_path || job.script) bits.push(`runs ${job.script_path || job.script}`);
                if (job.last_status) bits.push(`last run ${job.last_status}`);
                startNewChat();
                setChatInput(`About the automation "${job.name || job.id}" (${bits.join(', ')}): `);
                navigateTab('chat');
                // After the tab has actually rendered, or there is no input yet
                // to put the cursor in.
                requestAnimationFrame(() => {
                    const el = chatInputRef.current;
                    if (!el) return;
                    el.focus();
                    el.setSelectionRange(el.value.length, el.value.length);
                });
            };

            // The turn is streamed: /api/chat/stream proxies the gateway's SSE
            // chat endpoint, so text lands token by token and the reasoning and
            // tool calls behind it appear while they happen rather than being
            // reconstructed from the transcript minutes later. If the stream
            // cannot be opened at all we fall back to the original single POST,
            // which is slower to watch but identical in what it runs.
            const handleSendChat = async (e) => {
                e.preventDefault();
                if (!chatInput.trim() || chatSending) return;

                const text = chatInput;
                setChatInput('');
                setChatSending(true);

                const stamp = Date.now();
                let seq = 0;
                const nextId = (prefix) => `${prefix}-${stamp}-${++seq}`;

                setMessages(prev => [...prev, {
                    id: nextId('u'),
                    kind: 'user',
                    role: 'user',
                    content: text,
                    timestamp: new Date().toISOString()
                }]);

                const push = (entry) => setMessages(prev => [...prev, entry]);
                const patch = (id, fn) => setMessages(prev => prev.map(m => m.id === id ? fn(m) : m));

                // The assistant bubble currently being written into. A turn can
                // open several: text, then a tool run, then more text.
                let openId = null;
                const openAssistant = () => {
                    if (openId) return openId;
                    openId = nextId('a');
                    push({
                        id: openId, kind: 'assistant', role: 'assistant',
                        content: '', thinking: '', streaming: true,
                        timestamp: new Date().toISOString()
                    });
                    return openId;
                };
                const closeAssistant = () => {
                    if (!openId) return;
                    const id = openId;
                    openId = null;
                    patch(id, m => ({ ...m, streaming: false }));
                };

                // Reasoning arrives a scratchpad block at a time rather than
                // token by token, and several blocks can precede one reply.
                // Consecutive blocks accumulate into a single collapsed step
                // so a long turn does not become a wall of them; anything the
                // agent then says or does starts a new one.
                let openThinkingId = null;
                const closeThinking = () => { openThinkingId = null; };
                const appendThinking = (text) => {
                    if (openThinkingId) {
                        patch(openThinkingId, m => ({
                            ...m, thinking: (m.thinking ? m.thinking + '\n\n' : '') + text
                        }));
                        return;
                    }
                    closeAssistant();
                    openThinkingId = nextId('r');
                    push({
                        id: openThinkingId, kind: 'thinking', role: 'assistant',
                        thinking: text, timestamp: new Date().toISOString()
                    });
                };

                // Fixed once for the whole turn, so the streaming attempt and the
                // non-streaming fallback below cannot land in two different
                // sessions if the stream dies after the gateway has the message.
                const sendSessionId = chatSessionIdForSend();
                let sessionId = sendSessionId;
                let sawEvent = false;
                // Once the stream is open the turn is already running on the
                // agent. Anything that goes wrong after this point is reported,
                // never retried — a retry would run the turn a second time.
                let streamOpened = false;

                const handleEvent = (evt) => {
                    if (evt.type === 'session') {
                        sessionId = evt.session_id || sessionId;
                        if (!activeSessionId && evt.session_id) adoptChatSession(evt.session_id);
                        return;
                    }
                    sawEvent = true;
                    if (evt.type === 'delta') {
                        closeThinking();
                        patch(openAssistant(), m => ({ ...m, content: (m.content || '') + evt.text }));
                    } else if (evt.type === 'thinking') {
                        appendThinking(evt.text);
                    } else if (evt.type === 'approval') {
                        // The turn is not over — a tool is parked on the
                        // gateway waiting for this answer, and the stream
                        // stays open while the card sits here.
                        closeThinking();
                        closeAssistant();
                        push({
                            id: nextId('ap'), kind: 'approval', role: 'assistant',
                            token: evt.token, command: evt.command || '',
                            description: evt.description || '',
                            choices: evt.choices || ['once', 'deny'],
                            expiresAt: Date.now() + (evt.timeout_seconds || 60) * 1000,
                            decided: '', busy: false, note: '',
                            timestamp: new Date().toISOString()
                        });
                    } else if (evt.type === 'tool') {
                        if (evt.phase === 'started') {
                            closeThinking();
                            closeAssistant();
                            push({
                                id: nextId('t'), kind: 'tool', role: 'tool',
                                tool_name: evt.tool_name, args: evt.args || '',
                                call_id: evt.call_id || '',
                                status: 'pending', summary: '', detail: '', pending: true,
                                timestamp: new Date().toISOString()
                            });
                        } else {
                            // Settle by tool_call id where the gateway gave one,
                            // falling back to the newest still-running chip for
                            // the same tool.
                            setMessages(prev => {
                                for (let i = prev.length - 1; i >= 0; i--) {
                                    const m = prev[i];
                                    if (m.kind !== 'tool' || !m.pending) continue;
                                    const hit = evt.call_id ? m.call_id === evt.call_id : m.tool_name === evt.tool_name;
                                    if (!hit) continue;
                                    const copy = prev.slice();
                                    copy[i] = {
                                        ...m,
                                        pending: false,
                                        status: evt.status || 'ok',
                                        summary: evt.summary || m.summary,
                                        detail: evt.detail || m.detail
                                    };
                                    return copy;
                                }
                                return prev;
                            });
                        }
                    } else if (evt.type === 'error') {
                        closeThinking();
                        closeAssistant();
                        push({
                            id: nextId('e'), kind: 'assistant', role: 'assistant',
                            content: '⚠️ ' + evt.message, error: true,
                            timestamp: new Date().toISOString()
                        });
                    }
                };

                try {
                    const res = await fetch('/api/chat/stream', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ message: text, session_id: sendSessionId })
                    });
                    if (!res.ok || !res.body) throw new Error('stream unavailable');
                    streamOpened = true;

                    const reader = res.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    while (true) {
                        const { value, done } = await reader.read();
                        if (done) break;
                        buffer += decoder.decode(value, { stream: true });
                        let split;
                        while ((split = buffer.indexOf('\n\n')) !== -1) {
                            const frame = buffer.slice(0, split);
                            buffer = buffer.slice(split + 2);
                            const line = frame.split('\n').find(l => l.startsWith('data:'));
                            if (!line) continue;
                            try { handleEvent(JSON.parse(line.slice(5).trim())); }
                            catch (parseErr) { console.warn('Bad chat frame:', frame); }
                        }
                    }
                    closeAssistant();

                    if (!sawEvent) throw new Error('stream produced no reply');

                    fetchSessions();
                    fetchArchivedSessions();
                    // Replace the live render with the stored transcript, so what
                    // stays on screen is exactly what a reload would show.
                    if (sessionId) reconcileMessages(sessionId);
                } catch (err) {
                    console.error("Chat stream error:", err);
                    if (streamOpened) {
                        push({
                            id: nextId('e'), kind: 'assistant', role: 'assistant',
                            content: "⚠️ The reply stopped early. Reopen the session to see what was saved.",
                            error: true, timestamp: new Date().toISOString()
                        });
                        if (sessionId) reconcileMessages(sessionId);
                    } else {
                        await sendChatUnstreamed(text, nextId);
                    }
                } finally {
                    setChatSending(false);
                }
            };

            // The pre-streaming path, kept for when the SSE proxy is unreachable
            // (an older backend behind this frontend, a proxy that buffers it).
            const sendChatUnstreamed = async (text, nextId) => {
                try {
                    const res = await fetch('/api/chat', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ message: text, session_id: sendSessionId })
                    });
                    if (!res.ok) throw new Error("Failed to deliver message");
                    const data = await res.json();
                    if (!activeSessionId && data.session_id) adoptChatSession(data.session_id);
                    setMessages(prev => [...prev, {
                        id: nextId('a'),
                        kind: 'assistant',
                        role: 'assistant',
                        content: data.response,
                        timestamp: new Date().toISOString()
                    }]);
                    fetchSessions();
                    fetchArchivedSessions();
                } catch (err) {
                    console.error("Chat error:", err);
                    setMessages(prev => [...prev, {
                        id: nextId('e'),
                        kind: 'assistant',
                        role: 'assistant',
                        content: "⚠️ Error communicating with Hermes. Verify that the api server is active on port 8642.",
                        error: true,
                        timestamp: new Date().toISOString()
                    }]);
                }
            };

            const archiveChatSession = async (e, sessionId) => {
                e.stopPropagation(); // Avoid selecting the chat when clicking archive

                try {
                    const res = await fetch(`/api/sessions/${sessionId}/archive`, {
                        method: 'POST'
                    });
                    if (res.ok) {
                        if (activeSessionId === sessionId) {
                            resetChatPosition();
                            setActiveSessionId(null);
                            setMessages([]);
                        }
                        delete chatScrollMemoryRef.current[sessionId];
                        fetchSessions();
                        fetchArchivedSessions();
                    } else {
                        alert("Failed to archive chat session.");
                    }
                } catch (err) {
                    console.error("Archive error:", err);
                    alert("Could not contact the backend server.");
                }
            };

            // Every write route requires this header. It is not a token — it
            // proves nothing about who is asking — but a custom header cannot be
            // set by a form post or a simple cross-origin fetch without a
            // preflight, and the preflight is what the server's origin list
            // turns away. That is what stops another page in this browser
            // approving something, which now means sending mail.

            // getHighestScoreVal and cleanStr moved to module scope — the review
            // row and detail components are top-level functions and cannot see
            // anything declared in here.


            // Render agent replies as sanitized markdown. Falls back to raw text
            // if the CDN libs did not load so messages are never lost.
            // Links in rendered markdown always open in a new tab — the chat panel
            // holds live streaming state, so navigating it away loses the turn.
            // Installed lazily because DOMPurify arrives from a CDN.
            let purifyLinkHookInstalled = false;
            const installLinkTargetHook = () => {
                if (purifyLinkHookInstalled || !window.DOMPurify) return;
                window.DOMPurify.addHook('afterSanitizeAttributes', (node) => {
                    if (node.tagName === 'A' && node.hasAttribute('href')) {
                        node.setAttribute('target', '_blank');
                        node.setAttribute('rel', 'noopener noreferrer');
                    }
                });
                purifyLinkHookInstalled = true;
            };

            const renderMarkdown = (text) => {
                const raw = text || '';
                if (!window.marked || !window.DOMPurify) {
                    return { __html: raw.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])) };
                }
                installLinkTargetHook();
                const html = window.marked.parse(raw, { breaks: true, gfm: true });
                return { __html: window.DOMPurify.sanitize(html, { ADD_ATTR: ['target', 'rel'] }) };
            };

            // One transcript entry. Prose gets a bubble; everything the agent
            // *did* gets a single collapsed line it can be opened from. A busy
            // turn is a dozen steps, and the panel has to stay readable at that
            // length — which is exactly what dumping raw tool JSON destroyed.
            const renderChatEntry = (m) => {
                const kind = m.kind || (m.role === 'user' ? 'user' : 'assistant');
                const open = !!openSteps[m.id];

                if (kind === 'user') {
                    return (
                        <div key={m.id} class="flex justify-end">
                            <div class="max-w-2xl rounded-2xl rounded-tr-none px-4 py-3 shadow-md bg-[#b4befe] text-[#11111b] font-medium">
                                <div class="text-[10px] opacity-60 mb-1 font-mono">YOU</div>
                                <div class="text-sm leading-relaxed whitespace-pre-wrap break-words">{m.content}</div>
                            </div>
                        </div>
                    );
                }

                if (kind === 'thinking') {
                    return (
                        <div key={m.id} class="flex justify-start">
                            <div class="max-w-2xl w-full">
                                <button
                                    onClick={(e) => toggleStep(m.id, e)}
                                    class="w-full flex items-center gap-2 text-left px-3 py-1.5 rounded-lg text-xs text-[#7f849c] hover:text-[#bac2de] hover:bg-[#181825] transition-colors"
                                >
                                    <i data-lucide="brain" class="w-3.5 h-3.5 shrink-0"></i>
                                    <span class="italic truncate">{firstLine(m.thinking)}</span>
                                    <i data-lucide={open ? 'chevron-down' : 'chevron-right'} class="w-3.5 h-3.5 ml-auto shrink-0 opacity-60"></i>
                                </button>
                                {open && (
                                    <div class="markdown-body text-xs text-[#9399b2] px-3 py-2 ml-5 border-l border-[#313244]"
                                         dangerouslySetInnerHTML={renderMarkdown(m.thinking)} />
                                )}
                            </div>
                        </div>
                    );
                }

                if (kind === 'approval') {
                    const secondsLeft = Math.max(
                        0, Math.ceil(((m.expiresAt || 0) - Date.now()) / 1000)
                    );
                    // Lapsed and refused are the same outcome upstream, so the
                    // card must not keep offering a decision it can no longer
                    // deliver.
                    const lapsed = m.decided === 'expired' || (!m.decided && secondsLeft <= 0);
                    const settled = !!m.decided || lapsed;
                    const label = { once: 'Allow once', session: 'Allow this chat',
                                    always: 'Always allow', deny: 'Refuse' };
                    return (
                        <div key={m.id} class="flex justify-start">
                            <div class="max-w-2xl w-full rounded-xl border border-[#f9e2af]/50 bg-[#1e1e2e] p-3">
                                <div class="flex items-center gap-2 text-[11px] font-mono text-[#f9e2af] mb-2">
                                    <i data-lucide="shield-alert" class="w-3.5 h-3.5 shrink-0"></i>
                                    <span>APPROVAL NEEDED</span>
                                    {!settled && (
                                        <span class="ml-auto text-[#7f849c]">
                                            {secondsLeft}s — no answer counts as a refusal
                                        </span>
                                    )}
                                </div>
                                {m.description ? (
                                    <div class="text-xs text-[#bac2de] mb-2">{m.description}</div>
                                ) : null}
                                {m.command ? (
                                    <pre class="text-[11px] text-[#cdd6f4] bg-[#11111b] border border-[#313244] rounded-lg p-2 mb-2 overflow-x-auto max-h-56 overflow-y-auto whitespace-pre-wrap break-words">{m.command}</pre>
                                ) : null}
                                {settled ? (
                                    <div class="text-xs text-[#7f849c]">
                                        {lapsed
                                            ? 'Expired — treated as a refusal.'
                                            : `You chose: ${label[m.decided] || m.decided}.`}
                                    </div>
                                ) : (
                                    <div class="flex flex-wrap gap-2">
                                        {(m.choices || []).map(choice => (
                                            <button
                                                key={choice}
                                                disabled={m.busy}
                                                onClick={() => decideApproval(m.id, m.token, choice)}
                                                class={`px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors disabled:opacity-50 ${
                                                    choice === 'deny'
                                                        ? 'border-[#f38ba8]/60 text-[#f38ba8] hover:bg-[#f38ba8]/10'
                                                        : 'border-[#a6e3a1]/60 text-[#a6e3a1] hover:bg-[#a6e3a1]/10'
                                                }`}
                                            >
                                                {label[choice] || choice}
                                            </button>
                                        ))}
                                    </div>
                                )}
                                {m.note ? (
                                    <div class="text-[11px] text-[#f38ba8] mt-2">{m.note}</div>
                                ) : null}
                            </div>
                        </div>
                    );
                }

                if (kind === 'tool') {
                    const failed = m.status === 'error';
                    const icon = m.pending ? 'loader' : (failed ? 'x-circle' : 'check-circle-2');
                    const tone = m.pending ? 'text-[#f9e2af]' : (failed ? 'text-[#f38ba8]' : 'text-[#a6e3a1]');
                    const taskId = kanbanToolTaskId(m);
                    return (
                        <div key={m.id} class="flex justify-start">
                            <div class="max-w-2xl w-full">
                                <div class="flex items-stretch gap-1">
                                    <button
                                        onClick={(e) => toggleStep(m.id, e)}
                                        class={`flex-1 min-w-0 flex items-center gap-2 text-left px-3 py-1.5 rounded-lg text-xs bg-[#181825] border border-[#313244] hover:border-[#45475a] transition-colors ${failed ? 'border-[#f38ba8]/40' : ''}`}
                                    >
                                        <i data-lucide={icon} class={`w-3.5 h-3.5 shrink-0 ${tone} ${m.pending ? 'animate-spin' : ''}`}></i>
                                        <span class="font-mono text-[#cdd6f4] shrink-0">{m.tool_name}</span>
                                        {m.args ? <span class="text-[#6c7086] truncate">{m.args}</span> : null}
                                        <span class={`ml-auto pl-2 shrink-0 truncate max-w-[45%] ${failed ? 'text-[#f38ba8]' : 'text-[#7f849c]'}`}>
                                            {m.pending ? 'running…' : (m.summary || 'done')}
                                        </span>
                                        <i data-lucide={open ? 'chevron-down' : 'chevron-right'} class="w-3.5 h-3.5 shrink-0 opacity-50"></i>
                                    </button>
                                    {/* The task the call acted on, as somewhere to go rather than
                                        an id to copy out. A kanban_comment is a handoff — the
                                        next thing you want is the task it landed on, and the
                                        transcript was the one surface naming a task you could
                                        not open. Beside the disclosure control rather than
                                        inside it: a button nested in a button is not something
                                        the browser will render, and opening the board is not a
                                        way of expanding this call's output. */}
                                    {taskId ? (
                                        <button
                                            onClick={() => { setActiveKanbanTaskId(taskId); navigateTab('kanban'); }}
                                            title={`Open task ${taskId}`}
                                            class="shrink-0 inline-flex items-center gap-1 px-2 rounded-lg text-[11px] font-mono bg-[#181825] border border-[#313244] text-[#b4befe] hover:border-[#b4befe]/60 hover:bg-[#b4befe]/10 transition-colors"
                                        >
                                            <i data-lucide="clipboard" class="w-3.5 h-3.5 shrink-0"></i>
                                            <span class="hidden sm:inline">{taskId}</span>
                                        </button>
                                    ) : null}
                                </div>
                                {open && (
                                    <pre class="text-[11px] text-[#9399b2] bg-[#11111b] border border-[#313244] rounded-lg mt-1 p-3 overflow-x-auto max-h-80 overflow-y-auto whitespace-pre-wrap break-words">
                                        {formatToolDetail(m.detail) || '(no output recorded)'}
                                    </pre>
                                )}
                            </div>
                        </div>
                    );
                }

                return (
                    <div key={m.id} class="flex justify-start">
                        <div class={`max-w-2xl rounded-2xl rounded-tl-none px-4 py-3 shadow-md bg-[#181825] text-[#cdd6f4] border ${m.error ? 'border-[#f38ba8]/50' : 'border-[#313244]'}`}>
                            <div class="text-[10px] opacity-60 mb-1 font-mono">HERMES</div>
                            {m.thinking ? (
                                <div class="mb-2">
                                    <button
                                        onClick={(e) => toggleStep(m.id, e)}
                                        class="flex items-center gap-1.5 text-[10px] text-[#7f849c] hover:text-[#bac2de] font-mono transition-colors"
                                    >
                                        <i data-lucide="brain" class="w-3 h-3"></i>
                                        <span>THINKING</span>
                                        <i data-lucide={open ? 'chevron-down' : 'chevron-right'} class="w-3 h-3"></i>
                                    </button>
                                    {open && (
                                        <div class="markdown-body text-xs text-[#9399b2] mt-1 pl-2 border-l border-[#313244]"
                                             dangerouslySetInnerHTML={renderMarkdown(m.thinking)} />
                                    )}
                                </div>
                            ) : null}
                            <div class="markdown-body text-sm leading-relaxed break-words"
                                 dangerouslySetInnerHTML={renderMarkdown(m.content)} />
                            {m.streaming && !m.content ? (
                                <span class="text-xs text-[#6c7086] italic">…</span>
                            ) : null}
                        </div>
                    </div>
                );
            };

            // A reasoning trace's opening line, as the label for the step it
            // belongs to. Hermes writes these as a bold markdown heading.
            const firstLine = (text) => {
                const line = (text || '').split('\n').find(l => l.trim());
                return (line || '').replace(/^[#*\s]+|[*\s]+$/g, '') || 'thinking';
            };

            // The board task a kanban_* call acted on, or null. Read from the
            // tool's own result first and only then from its arguments: the
            // result is what the board actually accepted, and a call that was
            // rejected should not offer a shortcut to a task it never touched.
            // Arguments are the fallback so the link is there while the call is
            // still running, which is when a comment is worth following.
            //
            // Deliberately every kanban_* tool rather than kanban_comment alone.
            // The id is the same id and the destination is the same page, and a
            // chip that links its task under one tool name but not the next is
            // harder to learn than one that always does.
            const kanbanToolTaskId = (m) => {
                if (!/^kanban_/.test(m.tool_name || '') || m.status === 'error') return null;
                const detail = (m.detail || '').trim();
                if (detail[0] === '{') {
                    try {
                        const id = JSON.parse(detail).task_id;
                        if (typeof id === 'string' && id) return id;
                    } catch (err) { /* not settled JSON yet — try the arguments */ }
                }
                const hit = /\btask_id\s*[=:]\s*"?([A-Za-z0-9_-]+)"?/.exec(`${m.args || ''}\n${detail}`);
                return hit ? hit[1] : null;
            };

            // Tool output opened for reading. Indented when it is JSON — which is
            // most of it, and unreadable as one long line.
            const formatToolDetail = (raw) => {
                const text = (raw || '').trim();
                if (!text || (text[0] !== '{' && text[0] !== '[')) return text;
                try { return JSON.stringify(JSON.parse(text), null, 2); }
                catch (err) { return text; }
            };

            // Handle Incremental Chat Search typing
            const handleChatSearchChange = (val) => {
                setChatSearch(val);
                fetchSessions(false, val);
                fetchArchivedSessions(val);
            };

            // Esc closes the context reading pane. Bound only while it is open so
            // it never competes with the approvals shortcuts, and not at all
            // while the health modal covers it — same one-overlay-per-Esc rule
            // as settings above.
            useEffect(() => {
                if (!ctxDoc || healthOpen) return;
                const onKey = (e) => { if (e.key === 'Escape') setCtxDoc(null); };
                document.addEventListener('keydown', onKey);
                return () => document.removeEventListener('keydown', onKey);
            }, [ctxDoc, healthOpen]);


            return {
                sessions, archivedSessions, showArchived, setShowArchived,
                messages, sessionsLoading, chatLoading, apiDown,
                chatSearch, handleChatSearchChange,
                chatSending, chatInput, setChatInput, chatInputRef,
                chatScrollRef, onChatScroll, onChatWheel, onChatKeyDown, onChatTouchStart,
                handleSendChat, renderChatEntry, renderMarkdown,
                selectSession, startNewChat, archiveChatSession, chatAboutAutomation,
                unreadSessionCount,
                ctxFiles, ctxLoading, ctxOpen, setCtxOpen, openHermesDoc,
                ctxDoc, setCtxDoc, ctxDocBody, ctxDocLoading,
                chatGrants, chatGrantsLoading, connOpen, setConnOpen,
            };
        }

        // The session search box.
        function ChatSidebarSearch({ chat }) {
            const { chatSearch, handleChatSearchChange } = chat;
            return (
                <div class="relative flex items-center shrink-0">
                    <input 
                        type="text" 
                        value={chatSearch}
                        onChange={(e) => handleChatSearchChange(e.target.value)}
                        placeholder="Search chat titles..." 
                        class="w-full bg-[#11111b] border border-[#313244] rounded-lg py-1.5 pl-8 pr-3 text-xs text-[#cdd6f4] placeholder-[#585b70] focus:outline-none focus:border-[#b4befe]"
                    />
                    <i data-lucide="search" class="w-3.5 h-3.5 text-[#585b70] absolute left-2.5"></i>
                </div>
            );
        }

        // New Chat.
        function ChatNewButton({ chat }) {
            const { startNewChat } = chat;
            return (
                <button 
                    onClick={startNewChat}
                    class="w-full bg-[#313244] hover:bg-[#45475a] text-[#cdd6f4] font-medium py-2 px-4 rounded-lg flex items-center justify-center gap-2 border border-[#45475a] text-xs"
                >
                    <i data-lucide="plus" class="w-4 h-4"></i> New Chat
                </button>
            );
        }

        // The conversation rail: what is live, and what has been archived.
        function ChatSessionRail({ chat, kanban, activeSessionId, navigateTab, setActiveKanbanTaskId }) {
            const {
                sessions, archivedSessions, showArchived, setShowArchived,
                sessionsLoading, messages, selectSession, archiveChatSession,
            } = chat;
            return (
                <>
                    <h2 class="text-xs font-semibold uppercase tracking-wider text-[#585b70] px-3 mb-2">Chat Sessions</h2>
                    {sessionsLoading ? (
                        <div class="text-center py-6 text-sm text-[#585b70]">Loading sessions...</div>
                    ) : sessions.length === 0 ? (
                        <div class="text-center py-6 text-sm text-[#585b70]">No recent sessions.</div>
                    ) : (
                        sessions.map(s => (
                            <div
                                key={s.id}
                                onClick={() => selectSession(s.id)}
                                class={`w-full group text-left p-3 rounded-lg flex items-center justify-between gap-2 cursor-pointer transition ${
                                    s.id === activeSessionId 
                                        ? 'bg-[#b4befe] text-[#11111b] font-medium' 
                                        : 'hover:bg-[#313244] text-[#a6adc8]'
                                }`}
                            >
                                {/* The unread dot sits outside the text
                                    column so it holds the same place on
                                    every row, whatever the title does. */}
                                {s.unread_count > 0 && s.id !== activeSessionId && (
                                    <span
                                        title={`${s.unread_count} unread repl${s.unread_count === 1 ? 'y' : 'ies'}`}
                                        class="w-2 h-2 rounded-full bg-[#b4befe] shrink-0"
                                    ></span>
                                )}
                                <div class="flex flex-col gap-1 overflow-hidden flex-1">
                                    <span class={`text-sm line-clamp-1 break-all pr-1 ${
                                        s.unread_count > 0 && s.id !== activeSessionId
                                            ? 'font-semibold text-[#cdd6f4]'
                                            : ''
                                    }`}>
                                        {s.title || s.id.substring(0, 8)}
                                    </span>
                                    <span class={`text-[10px] ${s.id === activeSessionId ? 'text-[#1e1e2e]' : 'text-[#585b70]'}`}>
                                        {s.message_count || 0} messages
                                    </span>
                                    {/* A dispatcher session is an agent working a board item, not a
                                        conversation someone had. Untitled and undated it is
                                        indistinguishable from an empty chat, so it names its task
                                        and links back to it. */}
                                    {s.kanban_task_id && (
                                        <span
                                            onClick={(e) => { e.stopPropagation(); setActiveKanbanTaskId(s.kanban_task_id); navigateTab('kanban'); }}
                                            title={`Open task ${s.kanban_task_id}`}
                                            class={`text-[10px] font-mono inline-flex items-center gap-1 hover:underline ${
                                                s.id === activeSessionId ? 'text-[#1e1e2e]' : 'text-[#b4befe]'
                                            }`}
                                        >
                                            <i data-lucide="clipboard" class="w-3 h-3"></i>
                                            {s.kanban_task_id}
                                        </span>
                                    )}
                                </div>

                                {/* Archive, in one click. Deliberately not in
                                    the delete red it used to wear: nothing is
                                    destroyed, the conversation moves to the
                                    accordion at the bottom of this list, and a
                                    colour that says otherwise makes a
                                    reversible action look like one to think
                                    twice about. */}
                                <button
                                    onClick={(e) => archiveChatSession(e, s.id)}
                                    class={`p-1.5 rounded transition shrink-0 opacity-0 group-hover:opacity-100 ${
                                        s.id === activeSessionId
                                            ? 'opacity-100 text-[#11111b] hover:bg-black/20'
                                            : 'text-[#585b70] hover:text-[#cdd6f4] hover:bg-[#45475a]'
                                    }`}
                                    title="Archive chat"
                                >
                                    <i data-lucide="archive" class="w-3.5 h-3.5"></i>
                                </button>
                            </div>
                        ))
                    )}


                    {/* COLLAPSIBLE ACCORDION FOR ARCHIVED SESSIONS (NEW FEATURE!) */}
                    <div class="border-t border-[#313244] pt-4 mt-4">
                        <button
                            onClick={() => setShowArchived(prev => !prev)}
                            class="w-full flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-[#585b70] hover:text-[#a6adc8] px-3 py-1.5 rounded transition"
                        >
                            <span class="flex items-center gap-1.5">
                                <i data-lucide="archive" class="w-3.5 h-3.5"></i>
                                Archived Sessions ({archivedSessions.length})
                            </span>
                            <i data-lucide={showArchived ? "chevron-down" : "chevron-right"} class="w-3.5 h-3.5"></i>
                        </button>

                        {showArchived && (
                            <div class="space-y-2 mt-2 pl-1 max-h-56 overflow-y-auto">
                                {archivedSessions.length === 0 ? (
                                    <div class="text-center py-4 text-xs italic text-[#585b70]">No archived sessions found.</div>
                                ) : (
                                    archivedSessions.map(as => (
                                        <button
                                            key={as.id}
                                            onClick={() => selectSession(as.id)}
                                            class={`w-full text-left p-2.5 rounded-lg flex flex-col gap-1 transition ${
                                                as.id === activeSessionId 
                                                    ? 'bg-[#b4befe]/80 text-[#11111b] font-medium' 
                                                    : 'hover:bg-[#313244]/50 text-[#585b70] hover:text-[#a6adc8]'
                                            }`}
                                        >
                                            <span class="text-xs line-clamp-1 break-all pr-1">
                                                {as.title || as.id.substring(0, 8)}
                                            </span>
                                            <span class={`text-[9px] ${as.id === activeSessionId ? 'text-[#1e1e2e]' : 'text-[#45475a]'}`}>
                                                {as.message_count || 0} messages (Archived)
                                            </span>
                                        </button>
                                    ))
                                )}
                            </div>
                        )}
                    </div>
                </>
            );
        }

        // What this conversation is made of — the markdown Hermes reads as
        // itself, and what it can reach outside itself. Pegged to the bottom of
        // the rail rather than listed with the sessions above it, because it is
        // not a session.
        function ChatConfigPanel({ chat, integrations, navigateTab, openSettings }) {
            const {
                ctxFiles, ctxLoading, ctxOpen, setCtxOpen, openHermesDoc,
                chatGrants, chatGrantsLoading, connOpen, setConnOpen,
            } = chat;
            return (
                <div class="shrink-0 max-h-[50%] overflow-y-auto border-t border-[#313244] p-3">
                    {/* CONTEXT: the markdown Hermes reads as itself. */}
                    <div>
                        <button
                            onClick={() => setCtxOpen(prev => !prev)}
                            class="w-full flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-[#585b70] hover:text-[#a6adc8] px-3 py-1.5 rounded transition"
                        >
                            <span class="flex items-center gap-1.5">
                                <i data-lucide="file-text" class="w-3.5 h-3.5"></i>
                                Context ({ctxFiles.length})
                            </span>
                            <i data-lucide={ctxOpen ? "chevron-down" : "chevron-right"} class="w-3.5 h-3.5"></i>
                        </button>

                        {ctxOpen && (
                            <div class="space-y-2 mt-2 pl-1 max-h-56 overflow-y-auto">
                                {ctxLoading ? (
                                    <div class="text-center py-4 text-xs italic text-[#585b70]">Loading context...</div>
                                ) : ctxFiles.length === 0 ? (
                                    <div class="text-center py-4 text-xs italic text-[#585b70]">No context files found.</div>
                                ) : (
                                    ctxFiles.map(f => (
                                        <button
                                            key={f.rel_path}
                                            onClick={() => openHermesDoc(f)}
                                            class="w-full text-left p-2.5 rounded-lg flex flex-col gap-1 transition hover:bg-[#313244]/50 text-[#585b70] hover:text-[#a6adc8]"
                                        >
                                            <span class="text-xs flex items-center gap-1.5 text-[#a6adc8]">
                                                <i data-lucide={f.is_soul ? 'heart' : 'file-text'} class="w-3 h-3"></i>
                                                {f.name}
                                                {f.is_soul && (
                                                    <span class="text-[9px] uppercase font-bold px-1.5 py-0.5 rounded-full bg-[#313244] text-[#f9e2af]">identity</span>
                                                )}
                                            </span>
                                            <span class="text-[9px] font-mono text-[#45475a] truncate">{f.rel_path}</span>
                                        </button>
                                    ))
                                )}
                            </div>
                        )}
                    </div>

                    {/* CONNECTIONS: the MCP servers this conversation
                        can reach.
                        Sits directly under Context because it is the
                        other half of the same question — Context is
                        what Hermes reads as itself, this is what it
                        can touch outside itself, and both are things
                        you want to know before you trust an answer in
                        the thread beside them.

                        Narrowed twice, and both narrowings matter.
                        To the chat consumer: a scheduled job's Gmail
                        grant is real but irrelevant to what you are
                        typing. And to MCP: that is the only way chat
                        reaches anything, so listing a workflow's
                        direct API client here would imply this
                        conversation could use it.

                        The complete picture — every consumer, and the
                        direct API clients the workflow pipelines hold
                        — is the Integrations tab, off the link at the
                        bottom of this list. Same endpoint, same
                        grants, read from the other end. */}
                    <div class="border-t border-[#313244] pt-3 mt-3">
                        <div class="w-full flex items-center group">
                            <button
                                onClick={() => setConnOpen(prev => !prev)}
                                class="flex-1 min-w-0 flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-[#585b70] hover:text-[#a6adc8] px-3 py-1.5 rounded transition"
                            >
                                <span class="flex items-center gap-1.5">
                                    <i data-lucide="plug" class="w-3.5 h-3.5"></i>
                                    Connections ({chatGrants.length})
                                </span>
                                <span class="flex items-center gap-1.5">
                                    {/* A broken grant is worth seeing with
                                        the accordion shut — it explains why
                                        the reply you just got was thin. */}
                                    {chatGrants.some(g => g.status === 'failed') && (
                                        <StatusMark status="failed" />
                                    )}
                                    <i data-lucide={connOpen ? "chevron-down" : "chevron-right"} class="w-3.5 h-3.5"></i>
                                </span>
                            </button>
                            {/* Its own control, not the header itself:
                                expanding the list and leaving the page
                                are different intentions, and one click
                                target that guesses between them gets it
                                wrong half the time. Settings edits the
                                default profile, which is the profile
                                this conversation runs on — so what is
                                listed here is exactly what that page
                                changes. */}
                            <button
                                onClick={() => openSettings('integrations')}
                                title="Edit connections"
                                aria-label="Edit connections"
                                class="shrink-0 mr-1 w-6 h-6 rounded flex items-center justify-center text-[#45475a] opacity-0 group-hover:opacity-100 focus:opacity-100 hover:text-[#cdd6f4] hover:bg-[#313244] transition"
                            >
                                <i data-lucide="settings" class="w-3.5 h-3.5"></i>
                            </button>
                        </div>

                        {connOpen && (
                            <div class="space-y-1 mt-2 pl-1 max-h-56 overflow-y-auto">
                                {chatGrantsLoading ? (
                                    <div class="text-center py-4 text-xs italic text-[#585b70]">Loading connections...</div>
                                ) : chatGrants.length === 0 ? (
                                    <div class="text-center py-4 px-2 space-y-2">
                                        <div class="text-xs italic text-[#585b70]">
                                            No MCP servers — this conversation reaches nothing outside the host.
                                        </div>
                                        <button
                                            onClick={() => openSettings('integrations')}
                                            class="text-[10px] text-[#89b4fa] hover:underline"
                                        >
                                            Add a connection →
                                        </button>
                                    </div>
                                ) : (
                                    <>
                                        {chatGrants.map(g => (
                                            <div
                                                key={`${g.source_key}::${g.capability || '*'}`}
                                                class="px-2.5 py-1 rounded-lg flex items-center gap-1.5 text-xs text-[#a6adc8]"
                                                title={g.last_error || BASIS_NOTE[g.status_basis]}
                                            >
                                                <StatusMark status={g.status} />
                                                <span class="truncate">{g.source}</span>
                                                {g.capability && (
                                                    <span class="text-[10px] font-mono text-[#89b4fa]">{g.capability}</span>
                                                )}
                                                <span class="flex-1"></span>
                                                <span class="text-[9px] font-mono text-[#45475a] shrink-0">
                                                    {fmtAgo(g.last_used_at) || 'never'}
                                                </span>
                                            </div>
                                        ))}
                                        {/* Two ways out of this list, and they
                                            are not the same question. Settings
                                            changes what this conversation can
                                            reach; Integrations reports how every
                                            consumer's grants are actually
                                            behaving. Keeping both, in that order,
                                            because the one you want after reading
                                            a connection list is usually the first. */}
                                        <button
                                            onClick={() => openSettings('integrations')}
                                            class="w-full text-left px-2.5 py-1.5 text-[10px] text-[#89b4fa] hover:text-[#b4befe] transition"
                                        >
                                            Edit these connections →
                                        </button>
                                        <button
                                            onClick={() => navigateTab('integrations')}
                                            class="w-full text-left px-2.5 py-1.5 text-[10px] text-[#585b70] hover:text-[#a6adc8] transition"
                                        >
                                            All integrations, including what the workflows reach →
                                        </button>
                                    </>
                                )}
                            </div>
                        )}
                    </div>
                </div>
            );
        }

        // The transcript and the composer.
        function ChatMainPanel({ chat }) {
            const {
                messages, chatSending, chatInput, setChatInput, chatInputRef,
                chatScrollRef, onChatScroll, onChatWheel, onChatKeyDown,
                onChatTouchStart, handleSendChat, renderChatEntry,
            } = chat;
            usePaintedIcons();
            return (
                <div class="flex flex-col h-full">
                    <div
                        ref={chatScrollRef}
                        onScroll={onChatScroll}
                        onWheel={onChatWheel}
                        onKeyDown={onChatKeyDown}
                        onTouchStart={onChatTouchStart}
                        tabIndex={-1}
                        class="flex-1 overflow-y-auto p-6 space-y-4 outline-none"
                    >
                        {messages.length === 0 ? (
                            <div class="h-full flex flex-col items-center justify-center text-[#585b70] gap-3">
                                <i data-lucide="bot" class="w-16 h-16 opacity-30 text-[#b4befe]"></i>
                                <p class="text-sm">Start a new message to awaken Hermes Agent.</p>
                            </div>
                        ) : (
                            messages.map(renderChatEntry)
                        )}
                        {chatSending && (
                            <div class="flex justify-start">
                                <div class="bg-[#181825] text-[#cdd6f4] border border-[#313244] max-w-2xl rounded-2xl rounded-tl-none px-4 py-3 flex items-center gap-3 shadow-md">
                                    <div class="flex gap-1.5">
                                        <span class="w-2 h-2 bg-[#b4befe] rounded-full animate-bounce"></span>
                                        <span class="w-2 h-2 bg-[#b4befe] rounded-full animate-bounce [animation-delay:0.2s]"></span>
                                        <span class="w-2 h-2 bg-[#b4befe] rounded-full animate-bounce [animation-delay:0.4s]"></span>
                                    </div>
                                    <span class="text-xs text-[#585b70] font-mono">Hermes is thinking...</span>
                                </div>
                            </div>
                        )}
                    </div>

                    {/* Chat Input form */}
                    <form onSubmit={handleSendChat} class="p-4 border-t border-[#313244] bg-[#181825] shrink-0">
                        <div class="relative flex items-center">
                            <input
                                type="text"
                                ref={chatInputRef}
                                value={chatInput}
                                onChange={e => setChatInput(e.target.value)}
                                placeholder={chatSending ? "Hermes is busy..." : "Send a message to Hermes..."}
                                disabled={chatSending}
                                class="w-full bg-[#11111b] border border-[#313244] rounded-xl py-3.5 pl-4 pr-12 text-sm text-[#cdd6f4] placeholder-[#585b70] focus:outline-none focus:border-[#b4befe] disabled:opacity-50"
                            />
                            <button 
                                type="submit"
                                disabled={chatSending || !chatInput.trim()}
                                class="absolute right-3 p-2 bg-[#b4befe] disabled:bg-[#313244] text-[#11111b] disabled:text-[#585b70] rounded-lg hover:scale-105 transition active:scale-95 flex items-center justify-center"
                            >
                                <i data-lucide="send-horizontal" class="w-4 h-4"></i>
                            </button>
                        </div>
                    </form>
                </div>
            );
        }

        // Reading pane for a context file picked in the sidebar.
        function ChatDocOverlay({ chat }) {
            const { ctxDoc, setCtxDoc, ctxDocBody, ctxDocLoading, renderMarkdown } = chat;
            if (!ctxDoc) return null;
            return (
                <div
                    class="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-6"
                    onClick={() => setCtxDoc(null)}
                >
                    <div
                        class="bg-[#11111b] border border-[#313244] rounded-2xl w-full max-w-3xl max-h-[85vh] flex flex-col overflow-hidden shadow-2xl"
                        onClick={e => e.stopPropagation()}
                    >
                        <div class="px-5 py-4 border-b border-[#313244] flex items-center justify-between gap-4 shrink-0">
                            <div class="min-w-0">
                                <h2 class="text-base font-bold text-[#cdd6f4] flex items-center gap-2">
                                    <i data-lucide={ctxDoc.is_soul ? 'heart' : 'file-text'} class="w-4 h-4"></i>
                                    {ctxDoc.name}
                                </h2>
                                <div class="text-[11px] text-[#45475a] font-mono mt-0.5">{ctxDoc.rel_path}</div>
                            </div>
                            <button
                                onClick={() => setCtxDoc(null)}
                                class="p-2 rounded-lg text-[#a6adc8] hover:bg-[#313244] hover:text-[#cdd6f4] transition shrink-0"
                                title="Close (Esc)"
                            >
                                <i data-lucide="x" class="w-4 h-4"></i>
                            </button>
                        </div>
                        <div class="flex-1 overflow-y-auto p-5 space-y-4">
                            {ctxDocLoading ? (
                                <div class="text-sm text-[#585b70]">Loading...</div>
                            ) : (
                                <>
                                    {splitFrontmatter(ctxDocBody).frontmatter && (
                                        <div class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                            <div class="px-4 py-2 border-b border-[#313244] text-[10px] font-semibold uppercase tracking-wider text-[#585b70]">
                                                Frontmatter
                                            </div>
                                            <pre class="p-4 text-xs font-mono text-[#a6adc8] overflow-x-auto whitespace-pre">
                                                {splitFrontmatter(ctxDocBody).frontmatter}
                                            </pre>
                                        </div>
                                    )}
                                    <div class="bg-[#181825] border border-[#313244] rounded-xl p-5">
                                        <div
                                            class="markdown-body text-sm leading-relaxed break-words"
                                            dangerouslySetInnerHTML={renderMarkdown(splitFrontmatter(ctxDocBody).body)}
                                        />
                                    </div>
                                </>
                            )}
                        </div>
                    </div>
                </div>
            );
        }
