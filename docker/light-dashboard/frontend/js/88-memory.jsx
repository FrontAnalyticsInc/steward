// --- 70-memory.jsx ---------------------------------------------------------
// The Memory tab's two pieces that are worth having on their own: the fact
// renderer that makes a [[wikilink]] clickable, and the graph page.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.

// A fact, with its wikilinks live.
//
// The store's link syntax is [[Target]], and until now the tab rendered it as
// literal brackets — so the one piece of structure the format carries was
// visible and inert. Making it clickable is what turns "links out" from a
// panel at the bottom into something you can follow mid-sentence, and it is
// what gives the graph page anything to draw.
function MemoryFactText({ text, onOpen }) {
    const raw = String(text == null ? '' : text);
    // Split on the link rather than scanning for it, so the surrounding text
    // survives verbatim — a fact is a sentence someone will read, and losing a
    // character of it to a parser is worse than leaving a link unrendered.
    const parts = raw.split(/(\[\[[^\]]+\]\])/g);
    return (
        <span>
            {parts.map((part, i) => {
                const match = /^\[\[([^\]]+)\]\]$/.exec(part);
                if (!match) return <span key={i}>{part}</span>;
                const target = match[1].trim();
                return (
                    <button key={i}
                        onClick={() => onOpen && onOpen(target)}
                        class="text-[#b4befe] hover:text-[#cdd6f4] underline decoration-dotted underline-offset-2 transition">
                        {target}
                    </button>
                );
            })}
        </span>
    );
}


// Where each node sits, as an SVG viewBox coordinate.
//
// Deterministic, not simulated. A force layout on a graph this size buys
// nothing but a different picture every visit — and a picture that moves is a
// picture you cannot point at in a message. The focused form is a wheel: the
// document you asked about at the centre, everything one hop out around it, in
// the order the server returned (which is degree first). The unfocused form is
// the same wheel with no centre, plus concentric rings once one ring would be
// too crowded to label.
//
// `focus` is a slug or null. Returns { positions, radius } in a 1000x1000 box.
function memoryGraphLayout(nodes, focus) {
    const positions = {};
    const CENTRE = 500;
    if (!nodes.length) return { positions, span: 1000 };

    const ring = (members, radius, phase) => {
        members.forEach((node, i) => {
            const angle = phase + (2 * Math.PI * i) / members.length;
            positions[node.slug] = {
                x: CENTRE + radius * Math.cos(angle),
                y: CENTRE + radius * Math.sin(angle),
            };
        });
    };

    const others = focus ? nodes.filter(n => n.slug !== focus) : nodes.slice();
    if (focus && nodes.some(n => n.slug === focus)) {
        positions[focus] = { x: CENTRE, y: CENTRE };
        // One ring while it stays legible, then a second further out. The
        // break is at 12 because that is where labels at this radius begin to
        // collide, not because it is a round number.
        if (others.length <= 12) {
            ring(others, 300, -Math.PI / 2);
        } else {
            const inner = others.slice(0, 12);
            const outer = others.slice(12);
            ring(inner, 250, -Math.PI / 2);
            ring(outer, 420, -Math.PI / 2 + Math.PI / Math.max(outer.length, 1));
        }
        return { positions, span: 1000 };
    }

    // No focus: rings from the middle out, densest first. An isolated store —
    // which is what this one is until the writer starts emitting wikilinks —
    // lays out as a single even circle, and that reads correctly: nothing is
    // near anything else because nothing is linked to anything else.
    let remaining = others.slice();
    let radius = others.length <= 8 ? 260 : 200;
    let hop = 0;
    while (remaining.length) {
        const capacity = Math.max(6, Math.floor((2 * Math.PI * radius) / 90));
        ring(remaining.slice(0, capacity), radius, -Math.PI / 2 + hop * 0.4);
        remaining = remaining.slice(capacity);
        radius += 150;
        hop += 1;
    }
    return { positions, span: 1000 };
}


// The graph page. Plain SVG, no library: the whole drawing is circles, lines
// and text, and the tab was carrying a Cytoscape bundle for exactly that
// before it was deleted. A dependency that has to be loaded from a CDN before
// the page can say "nothing is linked yet" is a poor trade.
function MemoryGraphView({ graph, loading, error, focus, onOpenDocument, onFocusNode, onClearFocus }) {
    const nodes = (graph && graph.nodes) || [];
    const edges = (graph && graph.edges) || [];
    const { positions, span } = React.useMemo(
        () => memoryGraphLayout(nodes, focus || null),
        [nodes, focus]
    );

    if (loading) return <div class="text-sm text-[#585b70]">Loading graph…</div>;
    if (error) {
        return (
            <div class="text-sm text-[#f38ba8] bg-[#181825] border border-[#313244] rounded-lg p-3">
                {error}
            </div>
        );
    }
    if (graph && graph.found === false) {
        return (
            <div class="space-y-3">
                <div class="text-sm text-[#f9e2af] bg-[#181825] border border-[#313244] rounded-lg p-3">
                    No document called <span class="font-mono">{focus}</span>. The graph is drawn
                    from the files on disk, so a name that is not one of them has no
                    neighbourhood to show — rather than quietly widening to the whole store.
                </div>
                <button onClick={onClearFocus}
                    class="text-sm text-[#b4befe] hover:text-[#cdd6f4] transition">
                    Show the whole graph
                </button>
            </div>
        );
    }
    if (!nodes.length) {
        return (
            <div class="text-sm text-[#585b70] bg-[#181825] border border-[#313244] rounded-lg p-3">
                Nothing recorded yet. The workflows write here when they refresh a contact.
            </div>
        );
    }

    const isolated = edges.length === 0;
    return (
        <div class="space-y-3">
            {/* Said plainly, because an empty graph looks identical to a broken
                one. This store has documents and no [[wikilinks]] between them,
                which is a fact about what the writer emits — not about whether
                this page is working. */}
            {isolated && (
                <div class="text-xs text-[#f9e2af] bg-[#181825] border border-[#313244] rounded-lg p-3">
                    {nodes.length} {nodes.length === 1 ? 'document' : 'documents'}, no links between
                    them. Edges here are <span class="font-mono">[[wikilinks]]</span> written into the
                    markdown, and nothing has written one yet — so the nodes below are correct and
                    unconnected rather than missing.
                </div>
            )}

            <div class="bg-[#181825] border border-[#313244] rounded-lg p-2">
                <svg viewBox={`0 0 ${span} ${span}`} class="w-full" style={{ maxHeight: '62vh' }}
                     role="img" aria-label="Memory wikilink graph">
                    {edges.map((edge, i) => {
                        const a = positions[edge.source];
                        const b = positions[edge.target];
                        if (!a || !b) return null;
                        return (
                            <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                                  stroke="#45475a" stroke-width="2" />
                        );
                    })}
                    {nodes.map(node => {
                        const at = positions[node.slug];
                        if (!at) return null;
                        const isFocus = focus && node.slug === focus;
                        // Size carries degree, so the well-connected records
                        // read first — the one thing a node-link picture is
                        // genuinely better at than the list next to it.
                        const r = isFocus ? 26 : Math.min(22, 12 + node.degree * 2);
                        const fill = node.missing ? '#11111b' : (isFocus ? '#b4befe' : '#585b70');
                        return (
                            <g key={node.slug} class="cursor-pointer"
                               onClick={() => (isFocus ? onOpenDocument(node.slug) : onFocusNode(node.slug))}>
                                <title>
                                    {node.missing
                                        ? `${node.title} — referred to, but no file of its own`
                                        : `${node.title} — ${node.degree} ${node.degree === 1 ? 'link' : 'links'}`}
                                </title>
                                <circle cx={at.x} cy={at.y} r={r}
                                        fill={fill}
                                        stroke={node.missing ? '#f9e2af' : (isFocus ? '#cdd6f4' : '#45475a')}
                                        stroke-width="3"
                                        stroke-dasharray={node.missing ? '6 5' : undefined} />
                                <text x={at.x} y={at.y + r + 22} text-anchor="middle"
                                      fill={isFocus ? '#cdd6f4' : '#a6adc8'} font-size="20">
                                    {node.title.length > 26 ? node.title.slice(0, 25) + '…' : node.title}
                                </text>
                            </g>
                        );
                    })}
                </svg>
            </div>

            <div class="flex flex-wrap items-center gap-4 text-xs text-[#585b70]">
                <span>{nodes.length} {nodes.length === 1 ? 'node' : 'nodes'}</span>
                <span>{edges.length} {edges.length === 1 ? 'link' : 'links'}</span>
                <span>
                    {focus
                        ? 'Click a neighbour to centre it; click the centre to open the record.'
                        : 'Click a node to centre the graph on it.'}
                </span>
            </div>

            {/* The same graph as a list. Not a fallback — it is the readable
                form for anything past a dozen nodes, and it is the only form
                that works for someone who cannot use the picture. */}
            <div class="bg-[#181825] border border-[#313244] rounded-lg divide-y divide-[#313244]">
                {nodes.map(node => (
                    <div key={node.slug} class="flex items-center justify-between gap-3 p-3">
                        <button onClick={() => onOpenDocument(node.slug)}
                            class="text-sm text-[#b4befe] hover:text-[#cdd6f4] transition truncate text-left">
                            {node.title}
                            {node.missing && (
                                <span class="ml-2 text-xs text-[#f9e2af]">no file yet</span>
                            )}
                        </button>
                        <div class="flex items-center gap-3 shrink-0">
                            <span class="text-xs text-[#585b70]">
                                {node.degree} {node.degree === 1 ? 'link' : 'links'}
                            </span>
                            <button onClick={() => onFocusNode(node.slug)}
                                class="text-xs text-[#a6adc8] hover:text-[#cdd6f4] transition">
                                Centre
                            </button>
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}
