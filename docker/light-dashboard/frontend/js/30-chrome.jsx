// --- 30-chrome.jsx ---------------------------------------------------------
// Page furniture - tab badges, the user menu, and the stack health
// indicator with its overlay.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
        var TabBadge = ({ count, tone, title }) => {
            if (!count) return null;
            // Three digits do not fit a circle this size, and widening it for the
            // rare case would undo the point of a fixed one.
            const label = count > 99 ? '99+' : String(count);
            return (
                <span
                    title={title}
                    class={`w-5 h-5 shrink-0 rounded-full font-bold leading-none inline-flex items-center justify-center ${
                        label.length > 2 ? 'text-[8px]' : 'text-[10px]'
                    } ${tone}`}
                >
                    {label}
                </span>
            );
        };

        // A switch, for a preference that is on or off. `role="switch"` rather
        // than a styled checkbox: the knob is the whole control, and a screen
        // reader should hear its state, not the word "checkbox".
        //
        // The knob is painted with --on-accent, which is dark on a dark page
        // and light on a light one — the same colour that sits on top of an
        // accent fill elsewhere. It reads against both the lavender "on" track
        // and the grey "off" one without a second variable.
        var Switch = ({ on, onToggle, label }) => (
            <button
                type="button"
                role="switch"
                aria-checked={on ? 'true' : 'false'}
                aria-label={label}
                onClick={onToggle}
                class={`w-11 h-6 rounded-full p-[3px] shrink-0 transition-colors ${
                    on ? 'bg-[#b4befe]' : 'bg-[#45475a] hover:bg-[#585b70]'
                }`}
            >
                <span
                    class={`block w-[18px] h-[18px] rounded-full transition-transform ${on ? 'translate-x-5' : ''}`}
                    style={{ background: 'var(--on-accent)' }}
                ></span>
            </button>
        );

        // The account button in the header and what drops out of it.
        //
        // There is no sign-in on this host, so this deliberately does not
        // render a name or an avatar photo it would have to invent. What it
        // does hold is everything that is genuinely per-person here: the theme,
        // and the way into the rest of the settings. Saying "this browser" out
        // loud is the honest version of an account menu on a dashboard that has
        // no accounts.
        function UserMenu({ theme, onTheme, onOpenSettings }) {
            const [open, setOpen] = useState(false);
            const ref = useRef(null);

            useEffect(() => {
                if (!open) return;
                // Icons inside the menu only exist once it is open, and the
                // page-wide sweep does not know about this component's state.
                const paint = setTimeout(renderIcons, 20);
                const onDown = (e) => {
                    if (ref.current && !ref.current.contains(e.target)) setOpen(false);
                };
                const onKey = (e) => { if (e.key === 'Escape') setOpen(false); };
                document.addEventListener('mousedown', onDown);
                document.addEventListener('keydown', onKey);
                return () => {
                    clearTimeout(paint);
                    document.removeEventListener('mousedown', onDown);
                    document.removeEventListener('keydown', onKey);
                };
            }, [open]);

            return (
                <div class="relative" ref={ref}>
                    <button
                        onClick={() => setOpen(o => !o)}
                        title="Account"
                        aria-label="Account"
                        aria-haspopup="true"
                        aria-expanded={open ? 'true' : 'false'}
                        class={`w-9 h-9 rounded-full border flex items-center justify-center transition ${
                            open
                                ? 'border-[#b4befe] text-[#b4befe] bg-[#313244]'
                                : 'border-[#45475a] text-[#a6adc8] hover:text-[#cdd6f4] hover:border-[#585b70]'
                        }`}
                    >
                        <i data-lucide="user" class="w-[18px] h-[18px]"></i>
                    </button>

                    {open && (
                        <div class="absolute right-0 top-11 w-72 bg-[#181825] border border-[#313244] rounded-xl shadow-xl z-[120] overflow-hidden">
                            <div class="px-4 py-3 border-b border-[#313244]">
                                <div class="text-sm font-semibold text-[#cdd6f4]">This browser</div>
                                <p class="text-[11px] text-[#585b70] leading-relaxed mt-0.5">
                                    No sign-in on this host — preferences are stored locally.
                                </p>
                            </div>
                            <div class="px-4 py-3 border-b border-[#313244]">
                                <div class="text-[10px] uppercase tracking-wider font-bold text-[#585b70] mb-2">Theme</div>
                                <ThemeSwitch theme={theme} onTheme={onTheme} />
                            </div>
                            <button
                                onClick={() => { setOpen(false); onOpenSettings(DEFAULT_SETTINGS_SECTION); }}
                                class="w-full text-left px-4 py-2.5 text-sm text-[#a6adc8] hover:bg-[#313244] hover:text-[#cdd6f4] flex items-center gap-2.5 transition"
                            >
                                <i data-lucide="settings" class="w-4 h-4"></i>
                                Settings
                            </button>
                        </div>
                    )}
                </div>
            );
        }

        // --- Stack health ---------------------------------------------------
        // Every service this host runs, from /api/health/services. The rest of
        // this page notices its own dependency failing and nothing else's: the
        // Memory tab knows the wiki is gone, chat knows the gateway is gone, and
        // neither tells the other. This is the one place that asks all of them,
        // and the one indicator that is allowed to interrupt whatever tab you
        // are on.
        //
        // Three states, kept distinct on purpose. `degraded` is the useful one:
        // a service answering slowly, or answering with something other than
        // health, is a different call from a service that is gone, and folding
        // the two together is how a light stops meaning anything.
        //
        // Colours are the CSS variables, never literal hexes: these are set
        // from style={{}} rather than a class, so the generated light-theme
        // block cannot reach them, and a Mocha green on a Latte page is
        // unreadable. See :root at the top of this file.
        var HEALTH_STATES = {
            ok:       { label: 'Healthy',  color: 'var(--acc-green)' },
            degraded: { label: 'Degraded', color: 'var(--acc-yellow)' },
            down:     { label: 'Down',     color: 'var(--acc-red)' },
            unknown:  { label: 'Unknown',  color: 'var(--txt-muted)' },
        };
        var healthState = (s) => HEALTH_STATES[s] || HEALTH_STATES.unknown;

        // What the indicator says on hover, and what the modal's banner says in
        // full. Written from the counts rather than the overall status so it
        // names the number of things wrong, which is the first question asked.
        function healthSummary(health) {
            if (!health) return 'Checking services…';
            const c = health.counts || {};
            const total = (health.services || []).length;
            const parts = [];
            if (c.down) parts.push(`${c.down} down`);
            if (c.degraded) parts.push(`${c.degraded} degraded`);
            if (!parts.length) return `All ${total} services healthy`;
            return `${parts.join(', ')} of ${total} services`;
        }

        var HealthDot = ({ status, className }) => (
            <span
                class={`inline-block w-2.5 h-2.5 rounded-full shrink-0 ${className || ''}`}
                style={{ backgroundColor: healthState(status).color }}
            ></span>
        );

        // The header light. Quiet while healthy — a 9x9 icon button like the
        // ones beside it, carrying a small green dot and nothing else — and it
        // grows a label the moment there is something to say. A permanent
        // "12 services OK" pill would be the same reassurance the old
        // unconditional "Connected" badge gave, which is to say none: it was on
        // whether or not anything had been checked.
        //
        // Down is the only state that flashes (see .health-alarm). Degraded
        // goes amber and holds still: it wants to be read, not obeyed.
        function HealthIndicator({ health, onOpen }) {
            const overall = (health && health.overall) || 'unknown';
            const counts = (health && health.counts) || {};
            const title = `${healthSummary(health)} — click for detail`;

            if (overall === 'down' || overall === 'degraded') {
                const bad = overall === 'down'
                    ? `${counts.down} down`
                    : `${counts.degraded} degraded`;
                return (
                    <button
                        onClick={onOpen}
                        title={title}
                        aria-label={title}
                        class={`h-9 px-3 rounded-lg flex items-center gap-2 text-xs font-semibold transition ${
                            overall === 'down'
                                ? 'health-alarm'
                                : 'border border-[#f9e2af] text-[#f9e2af] bg-[#f9e2af]/10 hover:bg-[#f9e2af]/20'
                        }`}
                    >
                        <i data-lucide="triangle-alert" class="w-4 h-4"></i>
                        {bad}
                    </button>
                );
            }

            return (
                <button
                    onClick={onOpen}
                    title={title}
                    aria-label={title}
                    class="w-9 h-9 rounded-lg flex items-center justify-center relative transition text-[#a6adc8] hover:text-[#cdd6f4] hover:bg-[#313244]"
                >
                    <i data-lucide="activity" class="w-[18px] h-[18px]"></i>
                    <HealthDot status={overall} className="absolute bottom-1 right-1" />
                </button>
            );
        }

        // One service. The note under the name is fixed text describing what
        // that service is for — the reader deciding whether a red row matters
        // should not have to already know what each service does. The detail
        // line above it is the probe's own words, and appears only when there
        // is something wrong to say.
        function HealthRow({ service }) {
            const s = healthState(service.status);
            return (
                <div class="px-4 py-3 flex items-start gap-3">
                    <HealthDot status={service.status} className="mt-1.5" />
                    <div class="flex-1 min-w-0">
                        <div class="text-sm font-medium text-[#cdd6f4]">{service.label}</div>
                        {service.detail && (
                            <div class="text-xs mt-0.5" style={{ color: s.color }}>{service.detail}</div>
                        )}
                        {service.note && (
                            <div class="text-[11px] text-[#585b70] mt-0.5 leading-relaxed">{service.note}</div>
                        )}
                        {service.target && (
                            <div class="text-[10px] font-mono text-[#45475a] mt-1 truncate">{service.target}</div>
                        )}
                    </div>
                    <div class="text-right shrink-0">
                        <div class="text-[11px] font-semibold" style={{ color: s.color }}>{s.label}</div>
                        {/* Latency is shown for anything that answered, healthy
                            included: it is the only number here that moves
                            before something breaks. */}
                        {service.status !== 'down' && (
                            <div class="text-[10px] text-[#585b70] mt-0.5">{service.latency_ms} ms</div>
                        )}
                    </div>
                </div>
            );
        }

        // Full screen, like Settings, and for the same reason: it is not a panel
        // over the tab you were on. You come here because something is wrong
        // with the host, and the tab underneath is not the context you need.
        function HealthOverlay({ health, error, refreshing, onRefresh, onClose }) {
            const services = (health && health.services) || [];
            const overall = (health && health.overall) || 'unknown';
            const state = healthState(overall);
            // Declaration order from the backend, which groups by what is at
            // risk rather than by container — a reader who sees the LLM gateway
            // red wants to know no agent can think, not which image is down.
            const groups = [];
            services.forEach(s => { if (!groups.includes(s.group)) groups.push(s.group); });
            const failing = services.filter(s => s.status !== 'ok');

            return (
                <div class="fixed inset-0 z-[160] bg-[#11111b] flex flex-col">
                    <header class="h-16 border-b border-[#313244] px-6 flex items-center gap-3 shrink-0">
                        <i data-lucide="activity" class="w-5 h-5" style={{ color: state.color }}></i>
                        <h1 class="text-base font-bold text-[#cdd6f4]">System health</h1>
                        <span class="text-xs" style={{ color: state.color }}>{healthSummary(health)}</span>
                        <span class="flex-1"></span>
                        {health && (
                            <span class="text-[11px] text-[#585b70]">
                                checked {health.age_s < 1 ? 'just now' : `${Math.round(health.age_s)}s ago`}
                            </span>
                        )}
                        <button
                            onClick={onRefresh}
                            disabled={refreshing}
                            title="Probe every service now"
                            aria-label="Probe every service now"
                            class={`h-9 px-3 rounded-lg text-xs font-semibold flex items-center gap-2 transition ${
                                refreshing
                                    ? 'text-[#585b70]'
                                    : 'text-[#a6adc8] hover:text-[#cdd6f4] hover:bg-[#313244]'
                            }`}
                        >
                            <i data-lucide="refresh-cw" class="w-4 h-4"></i>
                            {refreshing ? 'Checking…' : 'Refresh'}
                        </button>
                        <button
                            onClick={onClose}
                            title="Close (Esc)"
                            aria-label="Close system health"
                            class="w-9 h-9 rounded-lg flex items-center justify-center text-[#a6adc8] hover:text-[#cdd6f4] hover:bg-[#313244] transition"
                        >
                            <i data-lucide="x" class="w-[18px] h-[18px]"></i>
                        </button>
                    </header>

                    <div class="flex-1 overflow-y-auto p-8">
                        <div class="max-w-3xl mx-auto space-y-6">
                            {/* The whole reason someone opened this. Repeating
                                the bad rows above the roster means the answer is
                                the first thing on screen, not something to be
                                found by scanning eleven green dots.

                                The tint is a class rather than a style: a
                                translucent literal would stay Mocha on a Latte
                                page, and these two utilities already have
                                light-theme mappings generated for them. The
                                border reads the variable. */}
                            {failing.length > 0 && (
                                <div
                                    class={`rounded-xl px-4 py-3 border ${
                                        overall === 'down' ? 'bg-[#f38ba8]/10' : 'bg-[#f9e2af]/10'
                                    }`}
                                    style={{ borderColor: state.color }}
                                >
                                    <div class="text-xs font-semibold flex items-center gap-2" style={{ color: state.color }}>
                                        <i data-lucide="triangle-alert" class="w-4 h-4"></i>
                                        Needs attention
                                    </div>
                                    <div class="mt-2 space-y-1">
                                        {failing.map(s => (
                                            <div key={s.id} class="text-xs text-[#cdd6f4] flex items-start gap-2">
                                                <HealthDot status={s.status} className="mt-1" />
                                                <span class="font-medium">{s.label}</span>
                                                <span class="text-[#a6adc8]">{s.detail || healthState(s.status).label}</span>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {error && (
                                <div class="bg-[#f38ba8]/10 border border-[#f38ba8]/40 text-[#f38ba8] rounded-xl px-4 py-3 text-xs leading-relaxed">
                                    Could not reach this dashboard's own health endpoint: {error}
                                </div>
                            )}

                            {!health && !error && (
                                <div class="text-sm text-[#585b70]">Probing services…</div>
                            )}

                            {groups.map(group => (
                                <div key={group} class="bg-[#181825] border border-[#313244] rounded-xl overflow-hidden">
                                    <SectionHeader
                                        title={group}
                                        right={`${services.filter(s => s.group === group && s.status === 'ok').length}/${
                                            services.filter(s => s.group === group).length} healthy`}
                                    />
                                    <div class="divide-y divide-[#313244]">
                                        {services.filter(s => s.group === group).map(s => (
                                            <HealthRow key={s.id} service={s} />
                                        ))}
                                    </div>
                                </div>
                            ))}

                            {/* What a colour here does and does not prove. Left
                                on the page rather than in a tooltip because the
                                limits are the part that gets misread: a green
                                dot means the service answered this dashboard,
                                not that it is doing its job correctly. */}
                            {services.length > 0 && (
                                <p class="text-[11px] text-[#585b70] leading-relaxed">
                                    Each service is asked over loopback from this container — green means it
                                    answered promptly and said what it was supposed to say, amber means it
                                    answered slowly or said something else, red means it did not answer.
                                    A green dot is proof the process is serving, not that its work is correct.
                                </p>
                            )}
                        </div>
                    </div>
                </div>
            );
        }

        // Light / Dark / System as one segmented control. Shared by the account
        // menu and the Appearance section so the two can never drift into
        // showing different current values.
