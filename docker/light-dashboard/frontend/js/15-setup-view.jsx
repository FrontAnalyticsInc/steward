// First-run setup. Read-only on purpose — see the note on /api/setup/state in
// backend/main.py: this console has no login, so a page that could write .env
// or restart the stack would hand those powers to anything on the tailnet.
// It reports what is missing and prints the commands the operator runs.

function SetupChecklistItem({ item }) {
    const tone = {
        ok:      { dot: '#a6e3a1', label: 'done' },
        todo:    { dot: '#f9e2af', label: 'optional' },
        blocked: { dot: '#f38ba8', label: 'needed' },
    }[item.status] || { dot: '#6c7086', label: item.status };

    return (
        <div class="rounded-lg border border-[#313244] bg-[#181825] p-4 mb-3">
            <div class="flex items-center gap-2 mb-1">
                <span class="inline-block w-2 h-2 rounded-full"
                      style={{ backgroundColor: tone.dot }}></span>
                <h3 class="font-semibold text-[#cdd6f4]">{item.title}</h3>
                <span class="text-xs text-[#6c7086] uppercase tracking-wide">{tone.label}</span>
            </div>
            <p class="text-sm text-[#bac2de] mb-2">{item.detail}</p>
            {item.fix && (
                <pre class="text-xs bg-[#11111b] text-[#a6adc8] rounded p-3 overflow-x-auto mb-2">
{item.fix.join('\n')}
                </pre>
            )}
            {item.why && <p class="text-xs text-[#9399b2] italic">{item.why}</p>}
        </div>
    );
}

// --- Delivery channels ------------------------------------------------------
// The one part of this page that is about the OUTCOME rather than the install.
// See backend/delivery.py: a channel can be connected, enabled, and green in
// Settings while every scheduled delivery to it is silently discarded, because
// cron resolves `deliver: telegram` through TELEGRAM_HOME_CHANNEL in .env and
// through nothing else. That case gets the loudest treatment on the page.
//
// Still read-only in the sense that matters: nothing here writes .env, writes
// config.yaml or restarts anything. The one thing it can do is send a message,
// with a body it does not choose to an address it does not choose.

const DELIVERY_TONE = {
    verified:      { dot: '#a6e3a1', label: 'delivering' },
    unproven:      { dot: '#f9e2af', label: 'untested' },
    no_destination:{ dot: '#f38ba8', label: 'nowhere to go' },
    disabled:      { dot: '#f38ba8', label: 'switched off' },
    no_credential: { dot: '#6c7086', label: 'not connected' },
};

// A failed send is not one thing. Each of these is a different action for a
// person, which is why the reason is rendered rather than the word "failed".
const SEND_REASON_LABEL = {
    delivered:     'delivered',
    bad_token:     'the token was rejected',
    not_a_member:  'the bot is not in that chat',
    unknown_chat:  'that chat does not exist',
    unknown_thread:'that topic does not exist',
    missing_scope: 'a permission is missing',
    rate_limited:  'the platform is rate-limiting',
    network:       'the platform could not be reached',
    rejected:      'the platform rejected it',
};

function ago(seconds) {
    if (!seconds && seconds !== 0) return '';
    const delta = Math.max(0, Date.now() / 1000 - seconds);
    if (delta < 90) return 'just now';
    if (delta < 5400) return Math.round(delta / 60) + ' minutes ago';
    if (delta < 172800) return Math.round(delta / 3600) + ' hours ago';
    return Math.round(delta / 86400) + ' days ago';
}

function ChannelCard({ channel, envPath, onTested }) {
    const [sending, setSending] = React.useState(false);
    const [result, setResult] = React.useState(null);
    const [refused, setRefused] = React.useState(null);

    const tone = DELIVERY_TONE[channel.status] || { dot: '#6c7086', label: channel.status };
    const shown = result || channel.last_test;

    const sendTest = async () => {
        setSending(true); setResult(null); setRefused(null);
        try {
            const r = await fetch('/api/channels/' + channel.id + '/test', { method: 'POST' });
            const body = await r.json().catch(() => ({}));
            if (r.status === 409 || r.status === 502 || r.status === 503) {
                // Nothing was sent. That is a fact about this box, not about
                // the platform, and showing it as a delivery failure would
                // blame Telegram for an unset env var.
                setRefused(body.detail || ('HTTP ' + r.status));
            } else if (!r.ok) {
                setRefused('HTTP ' + r.status);
            } else {
                setResult(body);
                if (onTested) onTested();
            }
        } catch (e) {
            setRefused(e.message);
        } finally {
            setSending(false);
        }
    };

    return (
        <div class="rounded-lg border border-[#313244] bg-[#181825] p-4 mb-3">
            <div class="flex items-center gap-2 mb-1">
                <span class="inline-block w-2 h-2 rounded-full"
                      style={{ backgroundColor: tone.dot }}></span>
                <h3 class="font-semibold text-[#cdd6f4]">{channel.name}</h3>
                <span class="text-xs text-[#6c7086] uppercase tracking-wide">{tone.label}</span>
            </div>

            <p class="text-sm text-[#bac2de] mb-2">{channel.headline}</p>

            {/* The trap, and the only thing on this page that gets a border of
                its own: everything else about this channel reads as healthy. */}
            {channel.destination_unwired && (
                <div class="rounded border border-[#f38ba8] bg-[#f38ba81a] p-3 mb-2">
                    <p class="text-xs text-[#f38ba8]">
                        A home channel is recorded in config.yaml
                        {channel.config_home_channel && channel.config_home_channel.name
                            ? <> (<span class="font-mono">{channel.config_home_channel.name}</span>)</>
                            : ''}, so the Settings page, the platform catalog and the
                        assistant's own prompt all treat this channel as finished.
                        Scheduled delivery does not read config.yaml — it reads{' '}
                        <span class="font-mono">{channel.home_env_var}</span> from .env,
                        and that is unset. Every automation delivering here is being
                        discarded with no error recorded anywhere.
                    </p>
                </div>
            )}

            <dl class="text-xs text-[#9399b2] mb-3">
                <div class="flex gap-2">
                    <dt class="w-32 shrink-0 text-[#6c7086]">Credential</dt>
                    <dd class="font-mono text-[#bac2de]">
                        {channel.credential
                            ? channel.required_env.join(', ') + ' set'
                            : 'missing: ' + (channel.missing_env.join(', ') || channel.required_env.join(', '))}
                    </dd>
                </div>
                <div class="flex gap-2">
                    <dt class="w-32 shrink-0 text-[#6c7086]">Scheduled output</dt>
                    <dd class="font-mono text-[#bac2de]">
                        {channel.destination
                            ? channel.destination.chat_id
                              + (channel.destination.thread_id ? ' / topic ' + channel.destination.thread_id : '')
                              + '  (' + channel.destination.env_var + ')'
                            : 'discarded — ' + channel.home_env_var + ' is unset'}
                    </dd>
                </div>
            </dl>

            {channel.status !== 'verified' && (
                <pre class="text-xs bg-[#11111b] text-[#a6adc8] rounded p-3 overflow-x-auto mb-2">
{setupSteps(channel, envPath).join('\n')}
                </pre>
            )}

            {channel.can_test && (
                <div class="flex items-center gap-3 mb-2">
                    <button
                        onClick={sendTest}
                        disabled={sending}
                        class="px-3 py-1.5 rounded bg-[#89b4fa] text-[#11111b] text-sm font-semibold hover:opacity-90 disabled:opacity-50">
                        {sending ? 'Sending…' : 'Send a test message'}
                    </button>
                    <span class="text-xs text-[#6c7086]">
                        Goes to {channel.destination.chat_id}, the same address a
                        scheduled job resolves to.
                    </span>
                </div>
            )}

            {refused && (
                <p class="text-xs text-[#f9e2af] mb-1">Nothing was sent. {refused}</p>
            )}

            {shown && (
                <p class="text-xs mb-1" style={{ color: shown.ok ? '#a6e3a1' : '#f38ba8' }}>
                    {shown.ok ? '✓ ' : '✗ '}
                    {SEND_REASON_LABEL[shown.reason] || shown.reason}
                    {shown.at ? ' · ' + ago(shown.at) : ''}
                    {' — '}{shown.message}
                </p>
            )}

            {channel.last_test_stale && !result && (
                <p class="text-xs text-[#f9e2af]">
                    That success was against{' '}
                    <span class="font-mono">{channel.last_test.chat_id}</span>, and the
                    home channel has been repointed since. It proves nothing about where
                    output goes now.
                </p>
            )}

            <p class="text-xs text-[#6c7086] mt-2">{channel.cost}</p>
        </div>
    );
}

// The commands, per channel and per state. Deliberately shell and console
// instructions rather than a form: this page has no login, so a field that
// could write a bot token into .env would hand that to anything on the
// tailnet. Settings → Channels is offered first because it already exists,
// validates the value and writes the file Hermes expects.
function setupSteps(channel, envPath) {
    const steps = [];
    if (channel.credential && !channel.enabled) {
        steps.push('# Switch it on: Settings → Channels → ' + channel.name + ' → enable.');
        steps.push('# A credential without a running adapter delivers nothing.');
        steps.push('');
    }
    if (!channel.credential) {
        steps.push('# 1. Give it a credential — ' + channel.cost);
        steps.push('#    Easiest: this console, Settings → Channels → ' + channel.name + '.');
        steps.push('#    Or by hand, then restart the gateway so it is read:');
        steps.push('$EDITOR ' + envPath + '    # ' + channel.required_env.map(k => k + '=').join('  '));
        steps.push('docker restart hermes-gateway');
        steps.push('');
    }
    if (!channel.destination) {
        const n = channel.credential ? '1' : '2';
        steps.push('# ' + n + '. Tell it WHERE to deliver. This is the step that gets missed,');
        steps.push('#    and the one nothing else on this box checks.');
        if (channel.destination_unwired) {
            steps.push('#    ' + channel.sethome + ' has already run once — it wrote config.yaml and');
            steps.push('#    its .env write did not land (that half only logs a warning).');
            steps.push('#    Running it again is the fix, and so is setting the var by hand.');
        }
        steps.push('#    From ' + channel.name + ', in the chat you want the output in, send:');
        steps.push('       ' + channel.sethome);
        steps.push('#    Or set it by hand and restart:');
        steps.push('$EDITOR ' + envPath + '    # ' + channel.home_env_var + '=<chat id>');
        steps.push('docker restart hermes-gateway');
    } else {
        steps.push('# Ready. Send a test message below — it is the only thing that');
        steps.push('# distinguishes a working channel from a plausible one.');
    }
    return steps;
}

function DeliverySection({ delivery, onRefresh }) {
    if (!delivery) return null;

    if (!delivery.reachable) {
        return (
            <div class="rounded-lg border border-[#313244] bg-[#181825] p-4 mb-3">
                <h3 class="font-semibold text-[#cdd6f4] mb-1">Somewhere for output to go</h3>
                <p class="text-sm text-[#f9e2af]">
                    Channel configuration could not be read, so this page cannot say
                    whether finished work reaches anyone. {delivery.error}
                </p>
            </div>
        );
    }

    return (
        <div class="mb-3">
            <h2 class="text-sm font-semibold text-[#cdd6f4] uppercase tracking-wide mb-1">
                Where finished work goes
            </h2>
            <p class="text-xs text-[#9399b2] mb-3">
                An automation that delivers only to this screen is an automation
                nobody reads. A channel counts as working here when it has a
                credential, a home channel that scheduled jobs actually resolve,
                and a message that has arrived — not before.
            </p>
            {(delivery.channels || []).map(c => (
                <ChannelCard key={c.id} channel={c} envPath={delivery.env_path}
                             onTested={onRefresh} />
            ))}
            <p class="text-xs text-[#6c7086]">
                Credentials are entered from{' '}
                <a href="/settings/channels"
                   class="text-[#89b4fa] underline hover:opacity-90">Settings → Channels</a>,
                which validates them and writes the file the gateway reads. This page
                writes nothing.
            </p>
        </div>
    );
}

function SetupView({ onContinue }) {
    const [state, setState] = React.useState(null);
    const [error, setError] = React.useState(null);

    // Re-runnable, because a successful test send changes the verdict at the
    // top of the page and leaving it stale would be the same dishonesty in a
    // smaller place.
    const reload = React.useCallback(() => {
        fetch('/api/setup/state')
            .then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)))
            .then(d => setState(d))
            .catch(e => setError(e.message));
    }, []);

    React.useEffect(() => {
        let alive = true;
        fetch('/api/setup/state')
            .then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)))
            .then(d => { if (alive) setState(d); })
            .catch(e => { if (alive) setError(e.message); });
        return () => { alive = false; };
    }, []);

    if (error) {
        return (
            <div class="p-8 text-[#f38ba8]">
                Could not read the setup state: {error}
            </div>
        );
    }
    if (!state) {
        return <div class="p-8 text-[#6c7086]">Checking this install…</div>;
    }

    const health = state.health || {};
    const services = health.services || [];
    // The renderer is behind a compose profile and absent from a default
    // install, so it reports down on a perfectly good box. Saying that here is
    // the difference between a fresh install looking broken and looking new.
    const expectedDown = services.filter(
        s => s.status !== 'ok' && (s.id === 'renderer' || s.id === 'memory'));

    const model = state.model;

    return (
        <div class="h-full overflow-y-auto">
        <div class="max-w-3xl mx-auto p-8">
            <h1 class="text-2xl font-bold text-[#cdd6f4] mb-1">Finish setting up Steward</h1>
            <p class="text-sm text-[#9399b2] mb-6">
                The installer does what it can from a shell. These are the things
                that can only be decided afterwards.
            </p>

            {model && (
                <div class="rounded-lg border border-[#313244] bg-[#181825] p-4 mb-3">
                    <h3 class="font-semibold text-[#cdd6f4] mb-1">Model</h3>
                    <p class="text-sm text-[#bac2de] mb-2">
                        {model.provider
                            ? <>The default agent runs on <span class="text-[#cdd6f4]">{model.model || '(unset)'}</span>
                                {' '}via {(model.providers.find(p => p.id === model.provider) || {}).label || model.provider}.</>
                            : <>Running a provider outside the three this page picks from
                                {model.hermes_provider ? <> (<span class="font-mono">{model.hermes_provider}</span>)</> : ''}.</>}
                    </p>
                    <p class="text-xs text-[#9399b2]">
                        Change it from this console's Settings → Model, or from{' '}
                        <a href={HERMES_DASHBOARD_URL} target="_blank" rel="noreferrer"
                           class="text-[#89b4fa] underline hover:opacity-90">
                            Hermes's own dashboard
                        </a>{' '}for any provider beyond Claude, OpenAI, or a local endpoint.
                    </p>
                </div>
            )}

            {(state.items || []).map(item => (
                <SetupChecklistItem key={item.id} item={item} />
            ))}

            <DeliverySection delivery={state.delivery} onRefresh={reload} />

            {expectedDown.length > 0 && (
                <div class="rounded-lg border border-[#313244] bg-[#181825] p-4 mb-3">
                    <h3 class="font-semibold text-[#cdd6f4] mb-1">
                        Services reporting less than healthy — expected here
                    </h3>
                    <ul class="text-sm text-[#bac2de] list-disc ml-5">
                        {expectedDown.map(s => (
                            <li key={s.id}>
                                <span class="text-[#cdd6f4]">{s.label}</span>
                                {s.detail ? ' — ' + s.detail : ''}
                            </li>
                        ))}
                    </ul>
                    <p class="text-xs text-[#9399b2] italic mt-2">
                        The renderer sits behind a compose profile a default install
                        does not enable, and memory is empty until something writes
                        to it. Neither means the install went wrong.
                    </p>
                </div>
            )}

            <button
                onClick={onContinue}
                class="mt-4 px-4 py-2 rounded bg-[#89b4fa] text-[#11111b] font-semibold hover:opacity-90">
                Continue to the console
            </button>
            <p class="text-xs text-[#6c7086] mt-2">
                This page appears until nothing is marked “needed”. It is always at /setup.
            </p>
        </div>
        </div>
    );
}
