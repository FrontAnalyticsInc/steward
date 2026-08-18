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

function SetupView({ onContinue }) {
    const [state, setState] = React.useState(null);
    const [error, setError] = React.useState(null);

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

    return (
        <div class="max-w-3xl mx-auto p-8">
            <h1 class="text-2xl font-bold text-[#cdd6f4] mb-1">Finish setting up Steward</h1>
            <p class="text-sm text-[#9399b2] mb-6">
                The installer does what it can from a shell. These are the things
                that can only be decided afterwards.
            </p>

            {(state.items || []).map(item => (
                <SetupChecklistItem key={item.id} item={item} />
            ))}

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
    );
}
