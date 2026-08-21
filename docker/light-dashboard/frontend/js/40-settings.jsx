// --- 40-settings.jsx -------------------------------------------------------
// The settings overlay and everything reachable inside it: theme, messaging
// channels, MCP connections, and the read-only access/identity sections.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
        var THEME_CHOICES = [
            { id: 'light', label: 'Light', icon: 'sun' },
            { id: 'dark', label: 'Dark', icon: 'moon' },
            { id: 'system', label: 'System', icon: 'monitor' },
        ];
        var ThemeSwitch = ({ theme, onTheme }) => (
            <div class="flex gap-1 bg-[#11111b] border border-[#313244] rounded-lg p-1">
                {THEME_CHOICES.map(c => (
                    <button
                        key={c.id}
                        onClick={() => onTheme(c.id)}
                        aria-pressed={theme === c.id ? 'true' : 'false'}
                        class={`flex-1 py-1.5 rounded-md text-[11px] font-semibold flex items-center justify-center gap-1.5 transition ${
                            theme === c.id
                                ? 'bg-[#313244] text-[#b4befe]'
                                : 'text-[#585b70] hover:text-[#cdd6f4]'
                        }`}
                    >
                        <i data-lucide={c.icon} class="w-3.5 h-3.5"></i>
                        {c.label}
                    </button>
                ))}
            </div>
        );

        // The main model picker. Three providers, not Hermes's full catalog —
        // see backend/model_config.py for why. The draft mirrors what's on
        // disk once it loads, then goes stale on its own terms (typing) rather
        // than snapping back on every 15s poll elsewhere on the page — there
        // is none here, this section fetches once when opened.
        function ModelSection({ model }) {
            const cfg = model.config;
            const providers = (cfg && cfg.providers) || [];
            const [providerId, setProviderId] = useState('');
            const [modelName, setModelName] = useState('');
            const [baseUrl, setBaseUrl] = useState('');
            const [apiKey, setApiKey] = useState('');
            const [loadedFor, setLoadedFor] = useState(null);

            // Re-seed the draft once, when a fresh config arrives — not on
            // every render, or a keystroke would be stomped by the next poll.
            if (cfg && loadedFor !== cfg) {
                setLoadedFor(cfg);
                setProviderId(cfg.provider || (providers[0] && providers[0].id) || 'claude');
                setModelName(cfg.model || '');
                setBaseUrl(cfg.base_url || '');
                setApiKey('');
            }

            const selected = providers.find(p => p.id === providerId);
            const dirty = cfg && (
                providerId !== (cfg.provider || '') ||
                modelName.trim() !== (cfg.model || '') ||
                (providerId === 'local' && baseUrl.trim() !== (cfg.base_url || ''))
            );

            const submit = () => {
                model.onSave({
                    provider: providerId,
                    model: modelName.trim(),
                    base_url: providerId === 'local' ? baseUrl.trim() : '',
                    api_key: providerId === 'local' ? apiKey.trim() : '',
                    confirm_expensive_model: !!model.confirmMessage,
                });
            };

            if (model.loading && !cfg) {
                return <div class="text-sm text-[#585b70] mt-6">Loading the current model…</div>;
            }

            return (
                <div class="mt-6 space-y-4">
                    <p class="text-[11px] text-[#585b70] leading-relaxed max-w-xl">
                        Assigns the default agent's main model, the same as{' '}
                        <span class="font-mono">hermes model</span>. Saved changes take effect when
                        the gateway restarts.
                    </p>

                    {model.error && (
                        <div class="bg-[#f38ba8]/10 border border-[#f38ba8]/40 text-[#f38ba8] rounded-xl px-4 py-3 text-xs leading-relaxed">
                            {model.error}
                        </div>
                    )}

                    {cfg && !cfg.provider && (
                        <div class="bg-[#f9e2af]/10 border border-[#f9e2af]/30 text-[#f9e2af] rounded-xl px-4 py-3 text-xs leading-relaxed">
                            Currently running <span class="font-mono">{cfg.hermes_provider || 'an unrecognized provider'}</span>,
                            which is outside the three this page offers. Saving here switches away from
                            it — to manage it directly instead, use{' '}
                            <a href={HERMES_DASHBOARD_URL} target="_blank" rel="noreferrer"
                               class="underline hover:opacity-90">Hermes's own dashboard</a>.
                        </div>
                    )}

                    {model.restartNeeded && (
                        <div class="bg-[#f9e2af]/10 border border-[#f9e2af]/30 rounded-xl px-4 py-3 flex items-center gap-3">
                            <i data-lucide="refresh-cw" class="w-4 h-4 text-[#f9e2af] shrink-0"></i>
                            <div class="text-xs text-[#f9e2af] flex-1 leading-relaxed">
                                {model.restartDone
                                    ? 'Gateway restarting. It drains any turn in flight first, so give it a few seconds.'
                                    : 'Saved. The gateway picks this up on its next start.'}
                            </div>
                            {!model.restartDone && (
                                <button
                                    onClick={model.onRestart}
                                    disabled={model.restarting}
                                    class={`text-xs font-semibold py-1.5 px-3 rounded-lg shrink-0 transition ${
                                        model.restarting
                                            ? 'bg-[#313244] text-[#585b70]'
                                            : 'bg-[#f9e2af] text-[#11111b] hover:bg-[#f9e2af]/80'
                                    }`}
                                >
                                    {model.restarting ? 'Restarting…' : 'Restart gateway'}
                                </button>
                            )}
                        </div>
                    )}

                    <div class="bg-[#181825] border border-[#313244] rounded-xl p-4 space-y-4">
                        <div class="grid grid-cols-3 gap-2">
                            {providers.map(p => (
                                <button
                                    key={p.id}
                                    onClick={() => {
                                        setProviderId(p.id);
                                        if (!modelName.trim() || (selected && modelName.trim() === selected.default_model)) {
                                            setModelName(p.default_model);
                                        }
                                    }}
                                    class={`text-left px-3 py-2 rounded-lg border text-xs transition ${
                                        providerId === p.id
                                            ? 'border-[#89b4fa] bg-[#89b4fa]/10 text-[#cdd6f4]'
                                            : 'border-[#313244] text-[#a6adc8] hover:border-[#45475a]'
                                    }`}
                                >
                                    <div class="font-semibold">{p.label}</div>
                                    {p.needs_key && (
                                        <div class="text-[10px] text-[#585b70] mt-0.5">needs {p.key_env}</div>
                                    )}
                                </button>
                            ))}
                        </div>

                        <label class="block">
                            <span class="text-[11px] text-[#a6adc8]">Model</span>
                            <input
                                type="text"
                                value={modelName}
                                onChange={e => setModelName(e.target.value)}
                                placeholder={selected && selected.default_model}
                                class="mt-1 w-full bg-[#11111b] border border-[#313244] rounded-lg px-3 py-2 text-sm text-[#cdd6f4] focus:border-[#89b4fa] outline-none"
                            />
                        </label>

                        {providerId === 'local' && (
                            <>
                                <label class="block">
                                    <span class="text-[11px] text-[#a6adc8]">Base URL</span>
                                    <input
                                        type="text"
                                        value={baseUrl}
                                        onChange={e => setBaseUrl(e.target.value)}
                                        placeholder="http://host.docker.internal:11434/v1"
                                        class="mt-1 w-full bg-[#11111b] border border-[#313244] rounded-lg px-3 py-2 text-sm text-[#cdd6f4] focus:border-[#89b4fa] outline-none font-mono"
                                    />
                                </label>
                                <label class="block">
                                    <span class="text-[11px] text-[#a6adc8]">API key (only if the endpoint needs one)</span>
                                    <input
                                        type="password"
                                        value={apiKey}
                                        onChange={e => setApiKey(e.target.value)}
                                        placeholder="leave blank for an open endpoint"
                                        class="mt-1 w-full bg-[#11111b] border border-[#313244] rounded-lg px-3 py-2 text-sm text-[#cdd6f4] focus:border-[#89b4fa] outline-none"
                                    />
                                </label>
                            </>
                        )}

                        {model.confirmMessage && (
                            <div class="bg-[#f38ba8]/10 border border-[#f38ba8]/40 text-[#f38ba8] rounded-lg px-3 py-2 text-xs leading-relaxed">
                                {model.confirmMessage} Save again to confirm.
                            </div>
                        )}
                        {model.saveError && (
                            <div class="bg-[#f38ba8]/10 border border-[#f38ba8]/40 text-[#f38ba8] rounded-lg px-3 py-2 text-xs leading-relaxed">
                                {model.saveError}
                            </div>
                        )}

                        <div class="flex items-center gap-3">
                            <button
                                onClick={submit}
                                disabled={model.saving || (!dirty && !model.confirmMessage)}
                                class={`text-xs font-semibold py-1.5 px-3 rounded-lg transition ${
                                    model.saving || (!dirty && !model.confirmMessage)
                                        ? 'bg-[#313244] text-[#585b70]'
                                        : 'bg-[#89b4fa] text-[#11111b] hover:bg-[#89b4fa]/80'
                                }`}
                            >
                                {model.saving ? 'Saving…' : model.confirmMessage ? 'Confirm and save' : 'Save'}
                            </button>
                            <a href={HERMES_DASHBOARD_URL} target="_blank" rel="noreferrer"
                               class="text-xs text-[#89b4fa] hover:underline">
                                Need a different provider? Open Hermes's dashboard →
                            </a>
                        </div>
                    </div>
                </div>
            );
        }

        // One channel: its state, its live chats, and the form that configures
        // it. Expanded one at a time — a channel has up to six credential
        // fields and six of those open at once is a wall, not a page.
        //
        // The draft is local to this component and starts empty. Empty means
        // "unchanged", which is what makes a secret editable at all: the API
        // hands back tokens redacted ("8818...CJYE"), so a form pre-filled from
        // the payload would write the redaction over the real credential the
        // moment anything else on the row was saved. A key is sent only once
        // it has been typed into.
        function ChannelRow({ channel, open, onToggleOpen, onSave, saving, error }) {
            const [draft, setDraft] = useState({});
            const [enabled, setEnabled] = useState(channel.enabled);
            const [showAdvanced, setShowAdvanced] = useState(false);

            // Adopt the server's answer whenever it changes underneath — after
            // a save, or after another window changed the same channel.
            useEffect(() => { setEnabled(channel.enabled); setDraft({}); }, [
                channel.enabled, channel.updated_at, channel.state]);

            const st = channelState(channel.state);
            const vars = channel.env_vars || [];
            const shown = vars.filter(v => !v.advanced || showAdvanced);
            const missing = vars.filter(v => v.required && !v.is_set && !String(draft[v.key] || '').trim());
            // Turning a channel on with a required credential still blank is
            // the one combination that is always wrong: the gateway would
            // start the adapter and fail on connect. Everything else — saving
            // credentials while off, clearing an optional var — is allowed.
            const blocked = enabled && missing.length > 0;
            const dirty = Object.keys(draft).length > 0 || enabled !== channel.enabled;

            const setField = (key, value) => setDraft(d => ({ ...d, [key]: value }));

            const submit = () => {
                // A field typed and then emptied is a request to remove it,
                // which is a different operation from "leave it alone".
                const env = {};
                const clear = [];
                Object.entries(draft).forEach(([k, v]) => {
                    const val = String(v);
                    if (val.trim()) env[k] = val;
                    else if (vars.find(x => x.key === k && x.is_set)) clear.push(k);
                });
                onSave(channel.id, {
                    enabled: enabled !== channel.enabled ? enabled : undefined,
                    env,
                    clear_env: clear,
                });
            };

            return (
                <div>
                    <button
                        onClick={onToggleOpen}
                        class="w-full text-left px-4 py-3.5 flex items-center gap-3.5 hover:bg-[#313244]/30 transition"
                    >
                        <i data-lucide={open ? 'chevron-down' : 'chevron-right'}
                           class="w-3.5 h-3.5 text-[#585b70] shrink-0"></i>
                        {/* A monogram, not a brand logo: the icon set has no
                            mark for half of these, and a row where two of six
                            carry a logo reads as two kinds of channel. */}
                        <span
                            class="w-9 h-9 rounded-lg flex items-center justify-center text-sm font-bold shrink-0"
                            style={{ background: channel.tint, color: 'var(--on-accent)' }}
                            aria-hidden="true"
                        >{channel.name[0]}</span>
                        <div class="min-w-0 flex-1">
                            <div class="text-sm font-semibold text-[#cdd6f4] flex items-center gap-2">
                                {channel.name}
                                {channel.chats.length > 0 && (
                                    <span class="text-[10px] font-normal text-[#585b70]">
                                        · {channel.chats.length} chat{channel.chats.length === 1 ? '' : 's'}
                                    </span>
                                )}
                            </div>
                            <div class="text-[11px] text-[#585b70] leading-relaxed line-clamp-1">
                                {channel.description}
                            </div>
                        </div>
                        {channel.error_message && (
                            <span class="text-[11px] text-[#f38ba8] truncate max-w-[16rem]"
                                  title={channel.error_message}>{channel.error_message}</span>
                        )}
                        <span class="text-[11px] flex items-center gap-1.5 shrink-0"
                              style={{ color: st.color }}>
                            <span class="font-bold">{st.mark}</span>{st.label}
                        </span>
                    </button>

                    {open && (
                        <div class="px-4 pb-4 pt-1 pl-[4.6rem] space-y-4">
                            {/* Where the agent is actually being talked to.
                                The catalog cannot answer this — a channel can
                                be connected with nobody in it. */}
                            {channel.chats.length > 0 && (
                                <div class="flex flex-wrap gap-1.5">
                                    {channel.chats.map(c => (
                                        <span key={c.id}
                                              class="text-[10px] font-mono bg-[#313244] text-[#a6adc8] rounded px-2 py-0.5"
                                              title={`${c.type || 'chat'} · id ${c.id}`}>
                                            {c.name || c.id}
                                        </span>
                                    ))}
                                </div>
                            )}

                            {channel.unknown ? (
                                <div class="text-[11px] text-[#585b70]">
                                    This build of Hermes does not carry an adapter for {channel.name}.
                                </div>
                            ) : (
                                <>
                                    {shown.map(v => (
                                        <div key={v.key}>
                                            <label class="text-[11px] font-semibold text-[#a6adc8] flex items-center gap-2">
                                                {v.prompt || v.key}
                                                {v.required && <span class="text-[#f38ba8]">required</span>}
                                                {v.is_set && (
                                                    <span class="text-[10px] font-normal text-[#585b70] font-mono">
                                                        currently {v.redacted_value || 'set'}
                                                    </span>
                                                )}
                                            </label>
                                            <input
                                                type={v.is_password ? 'password' : 'text'}
                                                value={draft[v.key] !== undefined ? draft[v.key] : ''}
                                                onChange={e => setField(v.key, e.target.value)}
                                                placeholder={v.is_set ? 'leave blank to keep, or type to replace' : v.key}
                                                autoComplete="off"
                                                class="w-full mt-1 bg-[#11111b] border border-[#313244] focus:border-[#b4befe] rounded-lg px-3 py-2 text-xs text-[#cdd6f4] font-mono outline-none transition"
                                            />
                                            {(v.description || v.help) && (
                                                <p class="text-[10px] text-[#585b70] mt-1 leading-relaxed">
                                                    {v.help || v.description}
                                                    {v.url && (
                                                        <> <a href={v.url} target="_blank" rel="noreferrer"
                                                              class="text-[#89b4fa] underline">open</a></>
                                                    )}
                                                </p>
                                            )}
                                        </div>
                                    ))}

                                    {vars.some(v => v.advanced) && (
                                        <button
                                            onClick={() => setShowAdvanced(a => !a)}
                                            class="text-[11px] text-[#585b70] hover:text-[#cdd6f4] flex items-center gap-1.5 transition"
                                        >
                                            <i data-lucide={showAdvanced ? 'chevron-up' : 'chevron-down'} class="w-3 h-3"></i>
                                            {showAdvanced ? 'Fewer options' : 'Advanced options'}
                                        </button>
                                    )}

                                    <div class="flex items-center gap-3 pt-1">
                                        <Switch
                                            on={enabled}
                                            onToggle={() => setEnabled(e => !e)}
                                            label={`Enable ${channel.name}`}
                                        />
                                        <span class="text-xs text-[#a6adc8]">
                                            {enabled ? 'Enabled' : 'Disabled'}
                                        </span>
                                        <span class="flex-1"></span>
                                        {error && (
                                            <span class="text-[11px] text-[#f38ba8] truncate max-w-sm" title={error}>{error}</span>
                                        )}
                                        {blocked && (
                                            <span class="text-[11px] text-[#f9e2af]">
                                                needs {missing.map(m => m.key).join(', ')}
                                            </span>
                                        )}
                                        <button
                                            onClick={submit}
                                            disabled={!dirty || blocked || saving}
                                            class={`text-xs font-semibold py-2 px-4 rounded-lg transition ${
                                                !dirty || blocked || saving
                                                    ? 'bg-[#313244] text-[#585b70] cursor-not-allowed'
                                                    : 'bg-[#b4befe] text-[#11111b] hover:bg-[#b4befe]/80'
                                            }`}
                                        >
                                            {saving ? 'Saving…' : dirty ? 'Save' : 'Saved'}
                                        </button>
                                    </div>

                                    {channel.docs_url && (
                                        <p class="text-[10px] text-[#585b70]">
                                            Creating the bot and its token happens on {channel.name}'s own site —{' '}
                                            <a href={channel.docs_url} target="_blank" rel="noreferrer"
                                               class="text-[#89b4fa] underline">{channel.docs_url}</a>
                                        </p>
                                    )}
                                </>
                            )}
                        </div>
                    )}
                </div>
            );
        }

        // --- MCP connections ---
        // What a connection's auth state means, given `oauth` from the API:
        // authenticated is null for anything that is not an `auth: oauth`
        // server, and that is "not applicable", not "not signed in". A stdio
        // server holding its own credential paths and a local server needing
        // no credential at all are both fine; rendering either as
        // unauthenticated would put a permanent false alarm on the page.
        var connAuthState = (s) => {
            const o = s.oauth || {};
            if (o.authenticated === false) return { label: 'not signed in', color: 'var(--acc-yellow)' };
            if (o.expired === true) return { label: 'token expired', color: 'var(--acc-yellow)' };
            if (o.authenticated === true) return { label: 'signed in', color: 'var(--acc-green)' };
            return null;
        };

        // `KEY=VALUE` lines ↔ an env map. Used by the add form only: editing an
        // existing server's env goes field by field, because the values come
        // back redacted and a textarea round-trip would write the redaction.
        var parseEnvLines = (text) => {
            const out = {};
            String(text || '').split('\n').forEach(line => {
                const trimmed = line.trim();
                if (!trimmed || trimmed.startsWith('#')) return;
                const eq = trimmed.indexOf('=');
                if (eq <= 0) return;
                out[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1).trim();
            });
            return out;
        };

        // One connection: what it points at, whether it is on, whether it can
        // still be reached, and the fields that change it. Expanded one at a
        // time, like a channel row, and for the same reason.
        //
        // The env draft starts empty and empty means "unchanged" — the list
        // hands values back redacted ("sk-1…9f2c"), so a form pre-filled from
        // the payload would write the redaction over the real credential the
        // moment anything else on the row was saved.
        function ConnectionRow({ server, open, onToggleOpen, onSave, onToggleEnabled,
                                 onRemove, onTest, saving, testing, testResult, error }) {
            const [target, setTarget] = useState('');
            const [env, setEnv] = useState({});
            const [confirmRemove, setConfirmRemove] = useState(false);

            // Adopt the server's answer whenever it changes underneath — after
            // a save, or after someone changed the same connection elsewhere.
            useEffect(() => {
                setTarget(server.target || '');
                setEnv({});
                setConfirmRemove(false);
            }, [server.target, server.enabled, server.name]);

            const isHttp = server.transport === 'http';
            const auth = connAuthState(server);
            const envKeys = Object.keys(server.env || {});
            const targetChanged = target.trim() !== (server.target || '').trim();
            const dirty = targetChanged || Object.keys(env).length > 0;

            const submit = () => {
                const payload = {};
                if (targetChanged && target.trim()) {
                    if (isHttp) {
                        payload.url = target.trim();
                    } else {
                        // A stdio target is one command line; splitting on
                        // whitespace is how it was written and how Hermes
                        // stores it back — command first, the rest args.
                        const parts = target.trim().split(/\s+/);
                        payload.command = parts[0];
                        payload.args = parts.slice(1);
                    }
                }
                if (Object.keys(env).length > 0) payload.env = env;
                onSave(server.name, payload);
            };

            return (
                <div>
                    <button
                        onClick={onToggleOpen}
                        class="w-full text-left px-4 py-3.5 flex items-center gap-3.5 hover:bg-[#313244]/30 transition"
                    >
                        <i data-lucide={open ? 'chevron-down' : 'chevron-right'}
                           class="w-3.5 h-3.5 text-[#585b70] shrink-0"></i>
                        <span
                            class="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
                            style={{ background: server.enabled ? 'var(--acc-lavender)' : 'var(--bg-raised)',
                                     color: server.enabled ? 'var(--on-accent)' : 'var(--txt-muted)' }}
                            aria-hidden="true"
                        >
                            <i data-lucide="plug" class="w-4 h-4"></i>
                        </span>
                        <div class="min-w-0 flex-1">
                            <div class="text-sm font-semibold text-[#cdd6f4] flex items-center gap-2">
                                {server.name}
                                {/* stdio spawns a local process, http talks to a
                                    URL. Which one it is changes what "the agent
                                    has access to gmail" means, so it is on the
                                    collapsed row rather than inside. */}
                                <span class="text-[10px] font-mono font-normal text-[#89b4fa] bg-[#313244]/60 rounded px-1.5 py-0.5">
                                    {server.transport}
                                </span>
                                {auth && (
                                    <span class="text-[10px] font-normal" style={{ color: auth.color }}>
                                        {auth.label}
                                    </span>
                                )}
                            </div>
                            <div class="text-[11px] text-[#585b70] font-mono leading-relaxed truncate">
                                {server.target || '—'}
                            </div>
                        </div>
                        <span class="text-[11px] shrink-0"
                              style={{ color: server.enabled ? 'var(--acc-green)' : 'var(--txt-muted)' }}>
                            {server.enabled ? '✓ on' : '— off'}
                        </span>
                    </button>

                    {open && (
                        <div class="px-4 pb-4 pt-1 pl-[4.6rem] space-y-4">
                            <div>
                                <label class="text-[11px] font-semibold text-[#a6adc8]">
                                    {isHttp ? 'Server URL' : 'Command'}
                                </label>
                                <input
                                    type="text"
                                    value={target}
                                    onChange={e => setTarget(e.target.value)}
                                    autoComplete="off"
                                    spellcheck={false}
                                    class="w-full mt-1 bg-[#11111b] border border-[#313244] focus:border-[#b4befe] rounded-lg px-3 py-2 text-xs text-[#cdd6f4] font-mono outline-none transition"
                                />
                                <p class="text-[10px] text-[#585b70] mt-1 leading-relaxed">
                                    {isHttp
                                        ? 'Where the agent connects. Changing the host usually means signing in again.'
                                        : 'The command and its arguments, as one line — this is spawned on the gateway host.'}
                                </p>
                            </div>

                            {envKeys.length > 0 && (
                                <div class="space-y-3">
                                    <div class="text-[11px] font-semibold text-[#a6adc8]">Environment</div>
                                    {envKeys.map(k => (
                                        <div key={k}>
                                            <label class="text-[11px] text-[#a6adc8] font-mono flex items-center gap-2">
                                                {k}
                                                <span class="text-[10px] text-[#585b70] font-sans">
                                                    currently {server.env[k] || 'empty'}
                                                </span>
                                            </label>
                                            <input
                                                type="text"
                                                value={env[k] !== undefined ? env[k] : ''}
                                                onChange={e => setEnv(d => ({ ...d, [k]: e.target.value }))}
                                                placeholder="leave blank to keep"
                                                autoComplete="off"
                                                class="w-full mt-1 bg-[#11111b] border border-[#313244] focus:border-[#b4befe] rounded-lg px-3 py-2 text-xs text-[#cdd6f4] font-mono outline-none transition"
                                            />
                                        </div>
                                    ))}
                                    {/* Saving merges, so it can set a value but
                                        never unset a key. Said plainly rather
                                        than discovered: an operator who clears
                                        a field expecting the variable to go
                                        away would be left with an empty one. */}
                                    <p class="text-[10px] text-[#585b70] leading-relaxed">
                                        Values are written to the profile's config. Removing a variable
                                        entirely means removing the connection and adding it back.
                                    </p>
                                </div>
                            )}

                            {/* The probe is the only thing on this page that
                                answers "does it actually work" — everything
                                else reports what the config says. */}
                            {testResult && (
                                <div class={`rounded-lg px-3 py-2 text-[11px] leading-relaxed ${
                                    testResult.ok
                                        ? 'bg-[#a6e3a1]/10 border border-[#a6e3a1]/30 text-[#a6e3a1]'
                                        : 'bg-[#f38ba8]/10 border border-[#f38ba8]/30 text-[#f38ba8]'
                                }`}>
                                    {testResult.ok ? (
                                        <>
                                            Connected — {(testResult.tools || []).length} tool
                                            {(testResult.tools || []).length === 1 ? '' : 's'}
                                            {(testResult.tools || []).length > 0 && (
                                                <span class="font-mono text-[10px] text-[#a6adc8]">
                                                    : {(testResult.tools || []).slice(0, 8).map(t => t.name).join(', ')}
                                                    {(testResult.tools || []).length > 8 ? ' …' : ''}
                                                </span>
                                            )}
                                        </>
                                    ) : (testResult.error || 'Could not connect.')}
                                </div>
                            )}

                            <div class="flex items-center gap-3 pt-1 flex-wrap">
                                <Switch
                                    on={server.enabled}
                                    onToggle={() => onToggleEnabled(server.name, !server.enabled)}
                                    label={`Enable ${server.name}`}
                                />
                                <span class="text-xs text-[#a6adc8]">
                                    {server.enabled ? 'Enabled' : 'Disabled'}
                                </span>
                                <span class="flex-1"></span>
                                {error && (
                                    <span class="text-[11px] text-[#f38ba8] truncate max-w-sm" title={error}>{error}</span>
                                )}
                                <button
                                    onClick={() => onTest(server.name)}
                                    disabled={testing}
                                    class="text-xs font-semibold py-2 px-3 rounded-lg text-[#a6adc8] hover:text-[#cdd6f4] hover:bg-[#313244] transition"
                                >
                                    {testing ? 'Testing…' : 'Test'}
                                </button>
                                {confirmRemove ? (
                                    <>
                                        <span class="text-[11px] text-[#f38ba8]">Remove {server.name}?</span>
                                        <button
                                            onClick={() => setConfirmRemove(false)}
                                            class="text-xs py-2 px-3 rounded-lg text-[#a6adc8] hover:text-[#cdd6f4] transition"
                                        >Cancel</button>
                                        <button
                                            onClick={() => onRemove(server.name)}
                                            class="text-xs font-semibold py-2 px-3 rounded-lg bg-[#f38ba8] text-[#11111b] hover:bg-[#f38ba8]/80 transition"
                                        >Remove</button>
                                    </>
                                ) : (
                                    <button
                                        onClick={() => setConfirmRemove(true)}
                                        class="text-xs font-semibold py-2 px-3 rounded-lg text-[#f38ba8] hover:bg-[#f38ba8]/10 transition"
                                    >Remove</button>
                                )}
                                <button
                                    onClick={submit}
                                    disabled={!dirty || saving}
                                    class={`text-xs font-semibold py-2 px-4 rounded-lg transition ${
                                        !dirty || saving
                                            ? 'bg-[#313244] text-[#585b70] cursor-not-allowed'
                                            : 'bg-[#b4befe] text-[#11111b] hover:bg-[#b4befe]/80'
                                    }`}
                                >
                                    {saving ? 'Saving…' : dirty ? 'Save' : 'Saved'}
                                </button>
                            </div>

                            {server.auth === 'oauth' && (
                                <p class="text-[10px] text-[#585b70] leading-relaxed">
                                    Signing in to this server runs its OAuth flow, which needs a browser
                                    it can redirect back to —{' '}
                                    <span class="font-mono text-[#a6adc8]">hermes mcp login {server.name}</span>,
                                    or the same connection in{' '}
                                    <a href={HERMES_DASHBOARD_URL} target="_blank" rel="noreferrer"
                                       class="text-[#89b4fa] underline">Hermes's own dashboard</a>.
                                </p>
                            )}
                        </div>
                    )}
                </div>
            );
        }

        // --- The read-only sections -------------------------------------
        // Three of the four sections on this page describe another container's
        // environment, so they render facts and never a save button. What they
        // owe the reader instead is *why* each grant is shaped the way it is:
        // that the workflows' Attio token is read-only by issuance rather than
        // by convention is the whole reason it is a separate credential from
        // the assistant's, and a row that showed only "configured ✓" would have
        // dropped the one thing worth knowing.

        // Heading for a section of the page, with the anchor the contents rail
        // jumps to. A plain id rather than a route: these are parts of one
        // page, and giving each its own URL would undo the reason they were put
        // together.
        function SettingsSection({ id, title, blurb, children, count }) {
            return (
                <section id={id} class="scroll-mt-6">
                    <div class="flex items-baseline gap-2 border-b border-[#313244] pb-2 mb-4">
                        <h3 class="text-sm font-bold text-[#cdd6f4]">{title}</h3>
                        {typeof count === 'number' && (
                            <span class="text-[11px] font-mono text-[#585b70]">{count}</span>
                        )}
                    </div>
                    {blurb && (
                        <p class="text-[11px] text-[#585b70] leading-relaxed max-w-xl -mt-2 mb-4">{blurb}</p>
                    )}
                    {children}
                </section>
            );
        }

        // One credential or one output target. The same card for both, because
        // they are the same shape of fact — a thing reached, the scope it is
        // reached with, and what bounds it — and the difference is only which
        // direction the data moves.
        function AccessCard({ row, kindLabel }) {
            const [open, setOpen] = useState(false);
            const ok = row.configured;
            // A variable that is set but points at a file that is not there is
            // the failure this card exists to catch: every credential path in
            // the compose file defaults to empty, so a typo reads as configured
            // right up until the call that needs it.
            const missingFile = (row.vars || []).find(v => v.set && v.file_present === false);

            return (
                <div class="bg-[#181825] border border-[#313244] rounded-xl">
                    <button
                        onClick={() => setOpen(o => !o)}
                        class="w-full text-left px-4 py-3 flex items-center gap-3 hover:bg-[#313244]/30 transition rounded-xl"
                    >
                        <i data-lucide={open ? 'chevron-down' : 'chevron-right'}
                           class="w-3.5 h-3.5 text-[#585b70] shrink-0"></i>
                        <div class="min-w-0 flex-1">
                            <div class="text-sm font-semibold text-[#cdd6f4] flex items-center gap-2 flex-wrap">
                                {row.label}
                                {row.scope && (
                                    <span class="text-[10px] font-mono font-normal text-[#89b4fa] bg-[#313244]/60 rounded px-1.5 py-0.5">
                                        {row.scope}
                                    </span>
                                )}
                            </div>
                            {row.target && (
                                <div class="text-[11px] text-[#585b70] font-mono truncate mt-0.5">{row.target}</div>
                            )}
                        </div>
                        <span class="text-[11px] shrink-0"
                              style={{ color: missingFile ? 'var(--acc-yellow)' : ok ? 'var(--acc-green)' : 'var(--txt-muted)' }}>
                            {missingFile ? '! file missing' : ok ? '✓ configured' : '— not set'}
                        </span>
                    </button>

                    {open && (
                        <div class="px-4 pb-4 pl-[2.4rem] space-y-3">
                            {row.summary && (
                                <p class="text-[11px] text-[#a6adc8] leading-relaxed">{row.summary}</p>
                            )}
                            {row.guardrail && (
                                <p class="text-[11px] text-[#585b70] leading-relaxed border-l-2 border-[#45475a] pl-3">
                                    {row.guardrail}
                                </p>
                            )}

                            {(row.recipients || []).length > 0 && (
                                <div class="text-[11px] text-[#a6adc8]">
                                    May send to:{' '}
                                    {row.recipients.map(r => (
                                        <span key={r} class="font-mono text-[10px] bg-[#313244]/60 rounded px-1.5 py-0.5 mr-1">{r}</span>
                                    ))}
                                </div>
                            )}
                            {row.recipients && row.recipients.length === 0 && (
                                <div class="text-[11px] text-[#f9e2af]">
                                    No recipient allowlist — unattended mail goes nowhere.
                                </div>
                            )}

                            <div class="space-y-1">
                                {(row.vars || []).map(v => (
                                    <div key={v.name} class="text-[11px] font-mono flex items-center gap-2 flex-wrap">
                                        <span class="text-[#89b4fa]">{v.name}</span>
                                        {v.set ? (
                                            v.kind === 'secret'
                                                ? <span class="text-[#a6e3a1]">set</span>
                                                : <span class="text-[#a6adc8] break-all">{v.value}</span>
                                        ) : (
                                            <span class="text-[#585b70]">not set</span>
                                        )}
                                        {v.file_present === false && (
                                            <span class="text-[#f9e2af]">file not found</span>
                                        )}
                                        {v.file_present === true && (
                                            <span class="text-[#a6e3a1]">file present</span>
                                        )}
                                    </div>
                                ))}
                            </div>

                            <div class="text-[10px] text-[#585b70] flex items-center gap-2 flex-wrap">
                                <span>{kindLabel} · module <span class="font-mono">{row.module}</span></span>
                                {(row.used_by || []).length > 0 ? (
                                    <span>
                                        used by{' '}
                                        {row.used_by.map(u => (
                                            <span key={u} class="font-mono text-[#a6adc8]">{u} </span>
                                        ))}
                                    </span>
                                ) : (
                                    // Worth stating rather than leaving blank: a
                                    // credential nothing imports is usually one
                                    // issued for a reason that has since ended.
                                    <span class="text-[#f9e2af]">no automation currently uses this</span>
                                )}
                            </div>
                        </div>
                    )}
                </div>
            );
        }

        // Who each actor acts as. Flat rows rather than cards: the value of
        // this section is reading four identities in one glance and seeing that
        // they are four, so anything that made each one bigger would cost the
        // comparison it exists for.
        function IdentityRow({ row }) {
            const warn = row.needs_reauth;
            return (
                <div class="bg-[#181825] border border-[#313244] rounded-xl px-4 py-3">
                    <div class="flex items-baseline gap-2 flex-wrap">
                        <span class="text-[11px] text-[#585b70]">{row.label}</span>
                        <span class="text-sm font-semibold font-mono"
                              style={{ color: row.address ? 'var(--txt-primary)' : 'var(--txt-muted)' }}>
                            {row.address || (row.signed_in ? 'the signed-in Google account' : 'not configured')}
                        </span>
                        {warn && <span class="text-[11px] text-[#f9e2af]">needs sign-in</span>}
                    </div>
                    {row.how && <div class="text-[11px] text-[#a6adc8] mt-1">{row.how}</div>}
                    {row.note && <div class="text-[11px] text-[#585b70] mt-0.5 leading-relaxed">{row.note}</div>}
                    {(row.scopes || []).length > 0 && (
                        <div class="mt-1.5 flex flex-wrap gap-1">
                            {row.scopes.map(sc => (
                                <span key={sc} class="text-[10px] font-mono bg-[#313244]/60 text-[#89b4fa] rounded px-1.5 py-0.5">{sc}</span>
                            ))}
                        </div>
                    )}
                    {row.change_with && (
                        <div class="text-[10px] text-[#585b70] mt-2">
                            Change with <span class="font-mono text-[#a6adc8]">{row.change_with}</span>
                        </div>
                    )}
                </div>
            );
        }

        // Adding a connection. Collapsed to a single button until asked for:
        // this page is mostly read, and a permanently open six-field form
        // would make it look like the point of the page was filling it in.
        function AddConnection({ onAdd, adding, error }) {
            const [open, setOpen] = useState(false);
            const [transport, setTransport] = useState('http');
            const [name, setName] = useState('');
            const [target, setTarget] = useState('');
            const [auth, setAuth] = useState('none');
            const [token, setToken] = useState('');
            const [envText, setEnvText] = useState('');

            const reset = () => {
                setName(''); setTarget(''); setToken(''); setEnvText('');
                setAuth('none'); setTransport('http');
            };

            const submit = async () => {
                const parts = target.trim().split(/\s+/);
                const ok = await onAdd({
                    name: name.trim(),
                    url: transport === 'http' ? target.trim() : null,
                    command: transport === 'stdio' ? parts[0] : null,
                    args: transport === 'stdio' ? parts.slice(1) : [],
                    env: transport === 'stdio' ? parseEnvLines(envText) : {},
                    auth: transport === 'http' && auth === 'oauth' ? 'oauth' : null,
                    bearer_token: transport === 'http' && auth === 'bearer' && token ? token : null,
                });
                if (ok) { reset(); setOpen(false); }
            };

            const ready = name.trim() && target.trim() &&
                (!(transport === 'http' && auth === 'bearer') || token.trim());

            if (!open) {
                return (
                    <button
                        onClick={() => setOpen(true)}
                        class="w-full border border-dashed border-[#313244] hover:border-[#585b70] rounded-xl px-4 py-3 text-xs text-[#a6adc8] hover:text-[#cdd6f4] flex items-center justify-center gap-2 transition"
                    >
                        <i data-lucide="plus" class="w-3.5 h-3.5"></i>
                        Add a connection
                    </button>
                );
            }

            return (
                <div class="bg-[#181825] border border-[#313244] rounded-xl p-5 space-y-4">
                    <div class="text-sm font-semibold text-[#cdd6f4]">New connection</div>

                    <div class="flex gap-2">
                        {[{ id: 'http', label: 'Remote (http)' }, { id: 'stdio', label: 'Local (stdio)' }].map(t => (
                            <button
                                key={t.id}
                                onClick={() => setTransport(t.id)}
                                aria-pressed={transport === t.id ? 'true' : 'false'}
                                class={`flex-1 py-1.5 rounded-md text-[11px] font-semibold transition ${
                                    transport === t.id
                                        ? 'bg-[#313244] text-[#b4befe]'
                                        : 'text-[#585b70] hover:text-[#cdd6f4]'
                                }`}
                            >{t.label}</button>
                        ))}
                    </div>

                    <div>
                        <label class="text-[11px] font-semibold text-[#a6adc8]">Name</label>
                        <input
                            type="text" value={name} onChange={e => setName(e.target.value)}
                            placeholder="linear" autoComplete="off"
                            class="w-full mt-1 bg-[#11111b] border border-[#313244] focus:border-[#b4befe] rounded-lg px-3 py-2 text-xs text-[#cdd6f4] font-mono outline-none transition"
                        />
                        <p class="text-[10px] text-[#585b70] mt-1">
                            This is what the agent's tools are prefixed with — <span class="font-mono">mcp__{name.trim() || 'name'}__…</span>
                        </p>
                    </div>

                    <div>
                        <label class="text-[11px] font-semibold text-[#a6adc8]">
                            {transport === 'http' ? 'Server URL' : 'Command'}
                        </label>
                        <input
                            type="text" value={target} onChange={e => setTarget(e.target.value)}
                            placeholder={transport === 'http' ? 'https://mcp.example.com/mcp' : 'npx -y @scope/server'}
                            autoComplete="off" spellcheck={false}
                            class="w-full mt-1 bg-[#11111b] border border-[#313244] focus:border-[#b4befe] rounded-lg px-3 py-2 text-xs text-[#cdd6f4] font-mono outline-none transition"
                        />
                    </div>

                    {transport === 'http' ? (
                        <div>
                            <label class="text-[11px] font-semibold text-[#a6adc8]">Authentication</label>
                            <div class="flex gap-2 mt-1">
                                {[{ id: 'none', label: 'None' },
                                  { id: 'oauth', label: 'OAuth' },
                                  { id: 'bearer', label: 'API key' }].map(a => (
                                    <button
                                        key={a.id}
                                        onClick={() => setAuth(a.id)}
                                        aria-pressed={auth === a.id ? 'true' : 'false'}
                                        class={`flex-1 py-1.5 rounded-md text-[11px] font-semibold transition ${
                                            auth === a.id
                                                ? 'bg-[#313244] text-[#b4befe]'
                                                : 'text-[#585b70] hover:text-[#cdd6f4]'
                                        }`}
                                    >{a.label}</button>
                                ))}
                            </div>
                            {auth === 'bearer' && (
                                <input
                                    type="password" value={token} onChange={e => setToken(e.target.value)}
                                    placeholder="token — stored in the profile's .env, never shown again"
                                    autoComplete="off"
                                    class="w-full mt-2 bg-[#11111b] border border-[#313244] focus:border-[#b4befe] rounded-lg px-3 py-2 text-xs text-[#cdd6f4] font-mono outline-none transition"
                                />
                            )}
                            {auth === 'oauth' && (
                                <p class="text-[10px] text-[#585b70] mt-2 leading-relaxed">
                                    Saved here, signed in separately — the flow needs a browser redirect, so it
                                    runs from <span class="font-mono text-[#a6adc8]">hermes mcp login</span> or
                                    Hermes's own dashboard.
                                </p>
                            )}
                        </div>
                    ) : (
                        <div>
                            <label class="text-[11px] font-semibold text-[#a6adc8]">Environment (optional)</label>
                            <textarea
                                value={envText} onChange={e => setEnvText(e.target.value)}
                                rows="3" spellcheck={false}
                                placeholder={'API_KEY=…\nWORKSPACE=…'}
                                class="w-full mt-1 bg-[#11111b] border border-[#313244] focus:border-[#b4befe] rounded-lg px-3 py-2 text-xs text-[#cdd6f4] font-mono outline-none transition"
                            ></textarea>
                            <p class="text-[10px] text-[#585b70] mt-1 leading-relaxed">
                                One <span class="font-mono">KEY=VALUE</span> per line, passed to the process
                                the command spawns.
                            </p>
                        </div>
                    )}

                    <div class="flex items-center gap-3">
                        {error && (
                            <span class="text-[11px] text-[#f38ba8] flex-1 leading-relaxed">{error}</span>
                        )}
                        <span class="flex-1"></span>
                        <button
                            onClick={() => { reset(); setOpen(false); }}
                            class="text-xs py-2 px-3 rounded-lg text-[#a6adc8] hover:text-[#cdd6f4] transition"
                        >Cancel</button>
                        <button
                            onClick={submit}
                            disabled={!ready || adding}
                            class={`text-xs font-semibold py-2 px-4 rounded-lg transition ${
                                !ready || adding
                                    ? 'bg-[#313244] text-[#585b70] cursor-not-allowed'
                                    : 'bg-[#b4befe] text-[#11111b] hover:bg-[#b4befe]/80'
                            }`}
                        >{adding ? 'Adding…' : 'Add connection'}</button>
                    </div>
                </div>
            );
        }

        // The settings pages. A full page rather than a dialog: these are
        // sections you read down, and a centred box would have spent the width
        // on a scrim. It still behaves like a modal — Esc and the X close it,
        // and Back leaves it — because it is somewhere you step into and out
        // of, not a tab you live on.
        // --- About -----------------------------------------------------------

        // The marker's timestamps are ISO 8601 UTC. Rendered in the reader's own
        // zone, because the question this answers is "how long have I been on
        // this version", and that is not a question anyone asks in UTC.
        function aboutDate(iso) {
            if (!iso) return null;
            const d = new Date(iso);
            if (isNaN(d.getTime())) return iso;   // show it raw rather than "Invalid Date"
            return d.toLocaleDateString(undefined, {
                year: 'numeric', month: 'short', day: 'numeric',
            });
        }

        // Copy that works where this console is actually served.
        //
        // navigator.clipboard is undefined outside a secure context. 127.0.0.1
        // counts as one; `http://<host>.ts.net:9120` does NOT — and that is the
        // remote operator, the one person reading this panel who is not already
        // sitting at the host's shell. So the modern API alone would fail
        // silently for exactly the reader it exists for.
        //
        // Hence the execCommand fallback, and hence rendering nothing at all if
        // neither path is available. A button that quietly does nothing is worse
        // than no button: the text beside it is selectable either way.
        var CAN_COPY = (
            (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) ||
            (typeof document !== 'undefined' && document.queryCommandSupported &&
             document.queryCommandSupported('copy'))
        );

        function copyText(text) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                return navigator.clipboard.writeText(text);
            }
            const ta = document.createElement('textarea');
            ta.value = text;
            // Off-screen rather than hidden: a display:none textarea cannot be
            // selected, and the copy silently yields an empty clipboard.
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); } finally { document.body.removeChild(ta); }
            return Promise.resolve();
        }

        // One command, selectable, with a copy affordance when the browser has
        // one. `select-all` so a click-drag that overshoots still grabs exactly
        // the command and not the prose around it.
        function CommandLine({ command }) {
            const [copied, setCopied] = React.useState(false);
            // Repaint locally. The app's icon effect has an explicit dependency
            // list and `copied` is state inside this component, so nothing there
            // reruns when the glyph swaps to a tick — the button would keep
            // showing the copy icon and the copy would look like it failed.
            // Same reasoning as the automations view; renderIcons skips anything
            // already painted, so running it on every render is cheap.
            useEffect(() => { renderIcons(); });
            const onCopy = () => {
                copyText(command).then(() => {
                    setCopied(true);
                    setTimeout(() => setCopied(false), 2000);
                }).catch(() => {});
            };
            return (
                <div class="flex items-center gap-2">
                    <code class="flex-1 font-mono text-xs text-[#a6adc8] bg-[#11111b] border border-[#313244] rounded-lg px-3 py-2 select-all overflow-x-auto whitespace-nowrap">
                        {command}
                    </code>
                    {CAN_COPY && (
                        <button
                            onClick={onCopy}
                            title="Copy to clipboard"
                            aria-label={`Copy: ${command}`}
                            class="shrink-0 w-8 h-8 rounded-lg flex items-center justify-center text-[#a6adc8] hover:text-[#cdd6f4] hover:bg-[#313244] transition"
                        >
                            <i data-lucide={copied ? 'check' : 'copy'} class="w-4 h-4"></i>
                        </button>
                    )}
                </div>
            );
        }

        // One label/value row. Values are font-mono because every one of them is
        // an identifier someone will paste into a bug report.
        function AboutRow({ label, value, muted }) {
            return (
                <div class="flex items-baseline gap-4 py-1.5">
                    <div class="text-[11px] text-[#585b70] w-32 shrink-0">{label}</div>
                    <div class={`text-xs font-mono ${muted ? 'text-[#585b70]' : 'text-[#cdd6f4]'}`}>
                        {value}
                    </div>
                </div>
            );
        }

        function SettingsOverlay({ section, onSection, onClose, channels, model, connections, theme, onTheme, version }) {
            const current = SETTINGS_SECTIONS.find(s => s.id === section) || SETTINGS_SECTIONS[0];

            return (
                <div class="fixed inset-0 z-[150] bg-[#11111b] flex flex-col">
                    <header class="h-16 border-b border-[#313244] px-6 flex items-center gap-3 shrink-0">
                        <i data-lucide="settings" class="w-5 h-5 text-[#b4befe]"></i>
                        <h1 class="text-base font-bold text-[#cdd6f4]">Settings</h1>
                        <span class="flex-1"></span>
                        <button
                            onClick={onClose}
                            title="Close (Esc)"
                            aria-label="Close settings"
                            class="w-9 h-9 rounded-lg flex items-center justify-center text-[#a6adc8] hover:text-[#cdd6f4] hover:bg-[#313244] transition"
                        >
                            <i data-lucide="x" class="w-[18px] h-[18px]"></i>
                        </button>
                    </header>

                    <div class="flex-1 flex overflow-hidden">
                        <nav class="w-60 shrink-0 border-r border-[#313244] p-3 space-y-1 overflow-y-auto">
                            {SETTINGS_SECTIONS.map(s => (
                                <button
                                    key={s.id}
                                    onClick={() => onSection(s.id)}
                                    class={`w-full text-left px-3 py-2 rounded-lg text-sm flex items-center gap-2.5 transition ${
                                        s.id === current.id
                                            ? 'bg-[#313244] text-[#b4befe] font-medium'
                                            : 'text-[#a6adc8] hover:bg-[#313244]/50 hover:text-[#cdd6f4]'
                                    }`}
                                >
                                    <i data-lucide={s.icon} class="w-4 h-4"></i>
                                    {s.label}
                                </button>
                            ))}
                        </nav>

                        <div class="flex-1 overflow-y-auto p-8">
                            <div class="max-w-3xl mx-auto">
                                <h2 class="text-xl font-bold text-[#cdd6f4]">{current.label}</h2>
                                <p class="text-sm text-[#a6adc8] mt-1">{current.blurb}</p>

                                {current.id === 'channels' && (
                                    <div class="mt-6 space-y-4">
                                        <p class="text-[11px] text-[#585b70] leading-relaxed max-w-xl">
                                            These write straight to the gateway's own configuration — the same
                                            <span class="font-mono"> .env</span> and
                                            <span class="font-mono"> config.yaml</span> that
                                            <span class="font-mono"> hermes gateway setup</span> writes. Saved
                                            changes take effect when the gateway restarts.
                                        </p>

                                        {channels.error && (
                                            <div class="bg-[#f38ba8]/10 border border-[#f38ba8]/40 text-[#f38ba8] rounded-xl px-4 py-3 text-xs leading-relaxed">
                                                {channels.error}
                                            </div>
                                        )}

                                        {/* Offered only once something has been saved. A
                                            restart button standing there permanently is an
                                            invitation to bounce the gateway for no reason —
                                            it drops every in-flight turn. */}
                                        {channels.restartNeeded && (
                                            <div class="bg-[#f9e2af]/10 border border-[#f9e2af]/30 rounded-xl px-4 py-3 flex items-center gap-3">
                                                <i data-lucide="refresh-cw" class="w-4 h-4 text-[#f9e2af] shrink-0"></i>
                                                <div class="text-xs text-[#f9e2af] flex-1 leading-relaxed">
                                                    {channels.restartDone
                                                        ? 'Gateway restarting. It drains any turn in flight first, so give it a few seconds.'
                                                        : 'Saved. The gateway picks these up on its next start.'}
                                                </div>
                                                {!channels.restartDone && (
                                                    <button
                                                        onClick={channels.onRestart}
                                                        disabled={channels.restarting}
                                                        class={`text-xs font-semibold py-1.5 px-3 rounded-lg shrink-0 transition ${
                                                            channels.restarting
                                                                ? 'bg-[#313244] text-[#585b70]'
                                                                : 'bg-[#f9e2af] text-[#11111b] hover:bg-[#f9e2af]/80'
                                                        }`}
                                                    >
                                                        {channels.restarting ? 'Restarting…' : 'Restart gateway'}
                                                    </button>
                                                )}
                                            </div>
                                        )}

                                        {channels.loading && !channels.list.length ? (
                                            <div class="text-sm text-[#585b70]">Loading channels…</div>
                                        ) : (
                                            <div class="bg-[#181825] border border-[#313244] rounded-xl divide-y divide-[#313244]">
                                                {channels.list.map(ch => (
                                                    <ChannelRow
                                                        key={ch.id}
                                                        channel={ch}
                                                        open={channels.openId === ch.id}
                                                        onToggleOpen={() => channels.onOpen(
                                                            channels.openId === ch.id ? null : ch.id)}
                                                        onSave={channels.onSave}
                                                        saving={channels.savingId === ch.id}
                                                        error={channels.saveErrors[ch.id]}
                                                    />
                                                ))}
                                            </div>
                                        )}

                                        <p class="text-[10px] text-[#585b70] leading-relaxed">
                                            Credentials are written to {channels.envPath || 'the gateway .env'} and
                                            never sent back to this page — a token already set shows only its last
                                            few characters, and an empty field leaves it alone.
                                        </p>
                                    </div>
                                )}

                                {current.id === 'model' && <ModelSection model={model} />}

                                {current.id === 'integrations' && (
                                    <div class="mt-6 space-y-10">
                                        {/* Contents. Four sections is exactly the
                                            range where a rail earns itself: enough
                                            that the fourth is below the fold, few
                                            enough that naming them all is faster
                                            than scrolling to find out. */}
                                        <nav class="flex flex-wrap gap-x-4 gap-y-1 text-[11px] -mb-4">
                                            {[['sec-assistant', 'Assistant connections'],
                                              ['sec-access', 'Workflow access'],
                                              ['sec-outputs', 'Automation outputs'],
                                              ['sec-identity', 'Email identity']].map(([id, label]) => (
                                                <a key={id} href={`#${id}`}
                                                   class="text-[#585b70] hover:text-[#89b4fa] transition">{label}</a>
                                            ))}
                                        </nav>

                                        {/* 1 — the assistant's own connections, and
                                            the only editable section on the page.
                                            These are Hermes configuration reachable
                                            through Hermes's own API; everything
                                            below is another container's compose
                                            environment. */}
                                        <SettingsSection
                                            id="sec-assistant"
                                            title="Assistant connections (MCP)"
                                            count={connections.list.length}
                                        >
                                            <div class="space-y-4">

                                        <p class="text-[11px] text-[#585b70] leading-relaxed max-w-xl">
                                            MCP servers on the default profile — the agent you talk to on the
                                            Chat tab, and the same list its Connections panel shows. Written
                                            through Hermes's own configuration, so
                                            <span class="font-mono"> hermes mcp list</span> and this page can
                                            never disagree.
                                            {connections.autoReload === false && (
                                                <span> This profile has auto-reload turned off, so a saved change
                                                    reaches the running conversation only after
                                                    <span class="font-mono"> /reload-mcp</span>.</span>
                                            )}
                                            {connections.autoReload !== false && (
                                                <span> A saved change reaches the running conversation within a
                                                    few seconds — no restart.</span>
                                            )}
                                        </p>

                                        {connections.error && (
                                            <div class="bg-[#f38ba8]/10 border border-[#f38ba8]/40 text-[#f38ba8] rounded-xl px-4 py-3 text-xs leading-relaxed">
                                                {connections.error}
                                            </div>
                                        )}

                                        {connections.loading && !connections.list.length ? (
                                            <div class="text-sm text-[#585b70]">Loading connections…</div>
                                        ) : (
                                            <>
                                                {connections.list.length > 0 && (
                                                    <div class="bg-[#181825] border border-[#313244] rounded-xl divide-y divide-[#313244]">
                                                        {connections.list.map(s => (
                                                            <ConnectionRow
                                                                key={s.name}
                                                                server={s}
                                                                open={connections.openName === s.name}
                                                                onToggleOpen={() => connections.onOpen(
                                                                    connections.openName === s.name ? null : s.name)}
                                                                onSave={connections.onSave}
                                                                onToggleEnabled={connections.onToggleEnabled}
                                                                onRemove={connections.onRemove}
                                                                onTest={connections.onTest}
                                                                saving={connections.savingName === s.name}
                                                                testing={connections.testingName === s.name}
                                                                testResult={connections.testResults[s.name]}
                                                                error={connections.saveErrors[s.name]}
                                                            />
                                                        ))}
                                                    </div>
                                                )}

                                                {!connections.list.length && !connections.error && (
                                                    <div class="bg-[#181825] border border-[#313244] rounded-xl px-4 py-6 text-center text-xs text-[#585b70]">
                                                        No MCP servers — this agent reaches nothing outside the host.
                                                    </div>
                                                )}

                                                <AddConnection
                                                    onAdd={connections.onAdd}
                                                    adding={connections.adding}
                                                    error={connections.addError}
                                                />
                                            </>
                                        )}

                                        <p class="text-[10px] text-[#585b70] leading-relaxed">
                                            Whether each connection is actually being used, and by which
                                            consumer, is the{' '}
                                            <button onClick={() => { onClose(); connections.onOpenIntegrations(); }}
                                                    class="text-[#89b4fa] underline">Integrations</button>{' '}
                                            view — this page is what is configured, that one is what happened.
                                        </p>
                                            </div>
                                        </SettingsSection>

                                        {/* 2 — what the automations may reach. A
                                            different question from the section
                                            above and deliberately not merged with
                                            it: each of these is a separate
                                            credential with a narrower scope, and
                                            that separation is the security model
                                            rather than an accident of deployment. */}
                                        <SettingsSection
                                            id="sec-access"
                                            title="Workflow access"
                                            count={(connections.wf?.workflow_access || []).length}
                                            blurb="API credentials held by the workflows service — separate from the assistant's, and scoped narrower on purpose. An unattended pipeline reading hostile input should not hold the grant a person consented to interactively."
                                        >
                                            {connections.wfError && (
                                                <div class="bg-[#f38ba8]/10 border border-[#f38ba8]/40 text-[#f38ba8] rounded-xl px-4 py-3 text-xs leading-relaxed mb-4">
                                                    {connections.wfError}
                                                </div>
                                            )}
                                            {/* Unreachable is not "nothing is
                                                configured". They look identical in
                                                an empty list and only one of them
                                                means a container needs looking at. */}
                                            {connections.wf && !connections.wf.workflows.reachable && (
                                                <div class="bg-[#f9e2af]/10 border border-[#f9e2af]/30 text-[#f9e2af] rounded-xl px-4 py-3 text-xs leading-relaxed mb-4">
                                                    {connections.wf.workflows.error}
                                                </div>
                                            )}
                                            <div class="space-y-3">
                                                {(connections.wf?.workflow_access || []).map(row => (
                                                    <AccessCard key={row.key} row={row} kindLabel="reads" />
                                                ))}
                                            </div>
                                        </SettingsSection>

                                        {/* 3 — where an automation's results land.
                                            Separate from access because writing
                                            somewhere is a different permission from
                                            reading it, and because this is the list
                                            you want when asking where a result went. */}
                                        <SettingsSection
                                            id="sec-outputs"
                                            title="Automation outputs"
                                            count={(connections.wf?.outputs || []).length}
                                            blurb="Where data from an automation can land. Each target carries its own bound — an allowlist, a directory the container mounts, an append-only file — and the bound is what makes an unattended pipeline safe to run."
                                        >
                                            <div class="space-y-3">
                                                {(connections.wf?.outputs || []).map(row => (
                                                    <AccessCard key={row.key} row={row} kindLabel="writes" />
                                                ))}
                                            </div>
                                        </SettingsSection>

                                        {/* 4 — the identities, gathered from both
                                            sides. Rows that only mean anything
                                            together: the assistant's interactive
                                            grant, the unattended reader, the
                                            send-only sender, and the read-only
                                            calendar. */}
                                        <SettingsSection
                                            id="sec-identity"
                                            title="Email identity"
                                            blurb="Which mailbox each actor acts as. Mail sent as a person goes through the approval queue; mail the assistant sends on its own behalf goes out under its own address, so nothing here can put words in someone's mouth."
                                        >
                                            <div class="space-y-3">
                                                {(connections.wf?.identities || []).map(row => (
                                                    <IdentityRow key={row.key} row={row} />
                                                ))}
                                            </div>
                                        </SettingsSection>

                                        {/* One statement of where the read-only
                                            three come from, at the bottom rather
                                            than repeated per section. */}
                                        {connections.wf?.workflows?.source && (
                                            <p class="text-[10px] text-[#585b70] leading-relaxed border-t border-[#313244] pt-4">
                                                Workflow access, outputs and the workflow identities are read-only
                                                here: they come from{' '}
                                                <span class="font-mono text-[#a6adc8]">{connections.wf.workflows.source.file}</span>{' '}
                                                and take effect when that service is recreated —{' '}
                                                <span class="font-mono text-[#a6adc8]">{connections.wf.workflows.source.apply}</span>.
                                                A save button here would be describing a future state of a process
                                                that had not read it yet. What each one has actually done lately is
                                                the{' '}
                                                <button onClick={() => { onClose(); connections.onOpenIntegrations(); }}
                                                        class="text-[#89b4fa] underline">Integrations</button>{' '}
                                                view.
                                            </p>
                                        )}
                                    </div>
                                )}

                                {current.id === 'appearance' && (
                                    <div class="mt-6 space-y-6">
                                        <div class="bg-[#181825] border border-[#313244] rounded-xl p-5">
                                            <div class="text-sm font-semibold text-[#cdd6f4]">Theme</div>
                                            <p class="text-[11px] text-[#585b70] mt-1 mb-4 leading-relaxed">
                                                System follows the operating system and keeps following it, so a
                                                machine that goes light at sunrise takes this page with it.
                                            </p>
                                            <div class="grid grid-cols-3 gap-3">
                                                {THEME_CHOICES.map(c => (
                                                    <button
                                                        key={c.id}
                                                        onClick={() => onTheme(c.id)}
                                                        aria-pressed={theme === c.id ? 'true' : 'false'}
                                                        class={`rounded-lg border p-3 text-left transition ${
                                                            theme === c.id
                                                                ? 'border-[#b4befe] bg-[#313244]'
                                                                : 'border-[#313244] hover:border-[#45475a]'
                                                        }`}
                                                    >
                                                        {/* A swatch of the thing itself, in fixed
                                                            colours: this is the one place on the
                                                            page that has to show what the *other*
                                                            theme looks like. System shows both,
                                                            split down the middle — showing it as a
                                                            dark card would make it look like a
                                                            second way of saying Dark. */}
                                                        <span class="block h-12 rounded-md border mb-2.5 overflow-hidden flex"
                                                              style={{ borderColor: c.id === 'light' ? '#ccd0da' : '#313244' }}>
                                                            {(c.id === 'system' ? ['light', 'dark'] : [c.id]).map(half => (
                                                                <span
                                                                    key={half}
                                                                    class="block flex-1 h-full flex flex-col"
                                                                    style={{ background: half === 'light' ? '#eff1f5' : '#11111b' }}
                                                                >
                                                                    <span class="block h-3 w-full"
                                                                          style={{ background: half === 'light' ? '#e6e9ef' : '#181825' }}></span>
                                                                    <span class="block h-1.5 w-2/3 mt-2 ml-2 rounded-full"
                                                                          style={{ background: half === 'light' ? '#7287fd' : '#b4befe' }}></span>
                                                                    <span class="block h-1.5 w-1/2 mt-1.5 ml-2 rounded-full"
                                                                          style={{ background: half === 'light' ? '#bcc0cc' : '#45475a' }}></span>
                                                                </span>
                                                            ))}
                                                        </span>
                                                        <span class={`text-xs font-semibold flex items-center gap-1.5 ${
                                                            theme === c.id ? 'text-[#b4befe]' : 'text-[#a6adc8]'
                                                        }`}>
                                                            <i data-lucide={c.icon} class="w-3.5 h-3.5"></i>
                                                            {c.label}
                                                        </span>
                                                    </button>
                                                ))}
                                            </div>
                                        </div>
                                        <p class="text-[11px] text-[#585b70] leading-relaxed">
                                            Stored in this browser, not on the host. Another browser, or another
                                            person on the same host, gets their own.
                                        </p>
                                    </div>
                                )}

                                {current.id === 'about' && (() => {
                                    // `version` is health.version, already polled
                                    // every 15s by the shell — this section makes
                                    // no request of its own and has no loading
                                    // state worth showing.
                                    const v = version || {};
                                    const home = v.steward_home || '/srv/steward';
                                    const pending = v.pending_migrations || [];
                                    const applied = v.last_migration || null;
                                    return (
                                        <div class="mt-6 space-y-6">
                                            <div class="bg-[#181825] border border-[#313244] rounded-xl p-5">
                                                <div class="text-sm font-semibold text-[#cdd6f4]">This deployment</div>
                                                <p class="text-[11px] text-[#585b70] mt-1 mb-4 leading-relaxed">
                                                    Read from{' '}
                                                    <span class="font-mono">.steward-version</span> on the data
                                                    disk, which is written by the installer and updated by an
                                                    upgrade only after the new stack passes its health checks.
                                                </p>

                                                <AboutRow
                                                    label="Running"
                                                    value={v.version || 'Unknown'}
                                                    muted={!v.version}
                                                />
                                                <AboutRow
                                                    label="First installed"
                                                    value={v.seeded_version || 'Unknown'}
                                                    muted={!v.seeded_version}
                                                />
                                                <AboutRow
                                                    label="Last updated"
                                                    value={aboutDate(v.last_update_at) || 'Never'}
                                                    muted={!v.last_update_at}
                                                />
                                                {/* Three states, not two. No marker at
                                                    all is Unknown; a marker saying
                                                    "0000" is a definite none-yet, which
                                                    is what every v0.1.0 install reads
                                                    since that release ships none. */}
                                                <AboutRow
                                                    label="Migrations"
                                                    value={!applied ? 'Unknown'
                                                        : applied === '0000' ? 'none applied yet'
                                                        : `applied through ${applied}`}
                                                    muted={!applied || applied === '0000'}
                                                />

                                                {!v.version && (
                                                    <p class="text-[11px] text-[#585b70] mt-4 leading-relaxed">
                                                        Unknown here means the marker file is missing, which is
                                                        normal for an install that predates version tracking. It
                                                        is not a fault, and the upgrade below still works — the
                                                        upgrade writes the marker on its way through.
                                                    </p>
                                                )}
                                            </div>

                                            {/* Only when there is something to say. A
                                                permanent "0 pending" row would train
                                                the eye to skip the one place this
                                                panel has to be read. */}
                                            {pending.length > 0 && (
                                                <div class="bg-[#f9e2af]/10 border border-[#f9e2af]/30 rounded-xl px-4 py-3 flex items-start gap-3">
                                                    <i data-lucide="triangle-alert" class="w-4 h-4 text-[#f9e2af] shrink-0 mt-0.5"></i>
                                                    <div class="text-xs text-[#f9e2af] leading-relaxed">
                                                        <span class="font-semibold">
                                                            {pending.length} migration{pending.length === 1 ? '' : 's'} pending
                                                            {' '}({pending.join(', ')}).
                                                        </span>{' '}
                                                        An upgrade applied the new images but did not finish, so
                                                        this stack is running newer code against a data disk that
                                                        has not been migrated to match. Re-run the upgrade below:
                                                        migrations are written to be safe to repeat, so it picks
                                                        up from here rather than starting over.
                                                    </div>
                                                </div>
                                            )}

                                            <div class="bg-[#181825] border border-[#313244] rounded-xl p-5">
                                                <div class="text-sm font-semibold text-[#cdd6f4]">Upgrading</div>
                                                <p class="text-[11px] text-[#585b70] mt-1 mb-4 leading-relaxed">
                                                    Run these on the host, over SSH — not from this page. An
                                                    upgrade stops the whole stack, including this console, so
                                                    there is no way for a button here to survive its own click.
                                                    The dry run changes nothing and is worth reading first.
                                                </p>
                                                <p class="text-[11px] text-[#585b70] mt-1 mb-4 leading-relaxed">
                                                    Replace <span class="font-mono text-[#a6adc8]">vX.Y.Z</span> with
                                                    the release you want. Steward does not check for updates, and{' '}
                                                    <span class="font-mono text-[#a6adc8]">--to</span> is required —
                                                    without it the upgrade runner would reinstall the version you
                                                    are already on. Releases are listed on GitHub.
                                                </p>
                                                <div class="space-y-2">
                                                    <CommandLine command={`${home}/hermes-update --to vX.Y.Z --dry-run`} />
                                                    <CommandLine command={`${home}/hermes-update --to vX.Y.Z`} />
                                                </div>
                                                <p class="text-[11px] text-[#585b70] mt-4 leading-relaxed">
                                                    It snapshots the data disk first, then stops the stack, pulls,
                                                    migrates, and brings it back up — several minutes, most of it
                                                    the pull. If any step fails it restores the snapshot and puts
                                                    the previous version back. This page will be unreachable
                                                    until it finishes.
                                                </p>
                                            </div>
                                        </div>
                                    );
                                })()}
                            </div>
                        </div>
                    </div>
                </div>
            );
        }

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
        window.parseEnvLines = parseEnvLines;
