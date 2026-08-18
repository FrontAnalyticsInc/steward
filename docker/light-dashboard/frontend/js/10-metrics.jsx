// --- 10-metrics.jsx --------------------------------------------------------
// Charts and the palettes they agree on, plus the small display atoms
// (StatTile, AgentRow, TeamCard) that the metrics and agent panes share.
//
// Loaded as <script type="text/babel"> from index.html, in filename order.
// These are classic scripts, not modules: every top-level declaration lands in
// one shared global scope, so names must stay unique across all of them, and a
// file may only use what an earlier-numbered file has already defined.
        // --- Metrics time series ------------------------------------------
        //
        // Producer colours, assigned by entity and never cycled: a filter that
        // changes which producers are present must not repaint the survivors.
        // These three were checked with the palette validator against this
        // page's card surface rather than picked by eye — the catppuccin
        // greens and yellows already on the page fail badly together (green
        // against yellow is ΔE 2.9 under protanopia, and only 11 with normal
        // colour vision, i.e. hard to tell apart for everyone).
        var PRODUCER_COLORS = {
            chat_session:   '#3b82f6',
            automation_run: '#d97706',
            workflow_run:   '#a855f7',
        };
        var PRODUCER_ORDER = ['workflow_run', 'automation_run', 'chat_session'];
        var PRODUCER_LABELS = {
            workflow_run: 'ADK runs',
            automation_run: 'Automations',
            chat_session: 'Chat',
        };

        var compactNum = (n) => {
            if (n === null || n === undefined) return "—";
            if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
            if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
            if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
            return String(Math.round(n));
        };

        // Outcome-kind colours. Eight hues, validated together against this
        // page's card surface — the order is part of the result, because the
        // checks are on *adjacent* pairs and reordering is what got this from
        // four passing hues to eight. Assigned to kinds by fixed position below,
        // so filtering a kind out never repaints the ones that remain.
        var KIND_PALETTE = ['#3b82f6', '#d97706', '#059669', '#a855f7',
                              '#0891b2', '#be185d', '#6366f1', '#b45309'];
        var OTHER_COLOR = '#7f849c';

        // Fixed order, so a kind keeps its colour as the data changes. Kinds
        // past the eighth share the neutral "other" colour rather than getting a
        // generated hue — a ninth invented colour is one the validator never saw.
        // This order *is* the colour assignment, so it is ordered by how likely
        // a kind is to appear rather than by how the enum reads. `Produced` has
        // ten kinds and the validated palette has eight; the two that fall past
        // it share the neutral colour, so the ones that fall past it must be the
        // rarest. Putting review_item ninth — as the enum does — painted a kind
        // this fleet produces every day in "other" grey.
        //
        // Reordering later repaints series that people have learned, so it is a
        // decision to make deliberately, not a tidy-up.
        // approved_email sits with the other two mail kinds rather than at the
        // end, because the three are read together — how much mail left, and
        // who was watching — and reading them off three different colours in
        // three different places defeats the point. This pushes graph_node past
        // the palette; checked before moving it, and graph_node has produced
        // nothing on this fleet, so no series anyone has learned repaints.
        var PRODUCED_KINDS = ['draft_email', 'auto_email', 'approved_email',
                                'review_item', 'approval_item', 'document',
                                'crm_task', 'kanban_task',
                                // Beyond the palette: neutral, disambiguated by
                                // the tooltip and by filtering to one kind.
                                'graph_node', 'graph_edge', 'calendar_event'];
        var TOUCHED_KINDS = ['email', 'calendar_event', 'contact', 'company',
                               'web_page', 'graph_node', 'document', 'kanban_task'];

        var kindColor = (order) => (kind) => {
            const i = order.indexOf(kind);
            return i >= 0 && i < KIND_PALETTE.length ? KIND_PALETTE[i] : OTHER_COLOR;
        };
        var prettyKind = (k) => String(k || '').replace(/_/g, ' ');

        // Multi-series daily lines, NOT stacked. Stacking answers "what did the
        // total do"; these charts answer "what happened to each kind", and a
        // stacked band makes an individual series impossible to read because its
        // baseline moves with everything below it.
        function KindLines({ rows, order, selected, emptyNote }) {
            const [hover, setHover] = React.useState(null);

            const { days, series, max } = React.useMemo(() => {
                const dayset = new Set(), byKind = new Map();
                (rows || []).forEach(r => {
                    const day = String(r.day).slice(0, 10);
                    dayset.add(day);
                    if (!byKind.has(r.kind)) byKind.set(r.kind, new Map());
                    byKind.get(r.kind).set(day, (byKind.get(r.kind).get(day) || 0) + (r.total || 0));
                });
                const days = Array.from(dayset).sort();
                const wanted = (selected && selected.length)
                    ? Array.from(byKind.keys()).filter(k => selected.includes(k))
                    : Array.from(byKind.keys());
                // Draw in the canonical order so the legend and the lines agree.
                const series = order.filter(k => wanted.includes(k))
                    .concat(wanted.filter(k => !order.includes(k)))
                    .map(k => ({ kind: k, points: byKind.get(k) }));
                let max = 0;
                series.forEach(s => s.points.forEach(v => { if (v > max) max = v; }));
                return { days, series, max: max || 1 };
            }, [rows, order, selected]);

            if (!days.length || !series.length) {
                return (
                    <div class="p-8 text-center text-sm text-[#585b70]">
                        {emptyNote || 'Nothing recorded in this window.'}
                    </div>
                );
            }

            const W = 820, H = 200, padL = 44, padR = 12, padT = 12, padB = 22;
            const innerW = W - padL - padR, innerH = H - padT - padB;
            const colorOf = kindColor(order);
            const x = (i) => days.length === 1
                ? padL + innerW / 2
                : padL + (i / (days.length - 1)) * innerW;
            const y = (v) => padT + innerH - (v / max) * innerH;

            return (
                <div class="relative px-4 pb-3">
                    <svg viewBox={`0 0 ${W} ${H}`} class="w-full" style={{ height: '200px' }}
                         onMouseLeave={() => setHover(null)}>
                        {[0, 0.5, 1].map(f => (
                            <g key={f}>
                                <line x1={padL} x2={W - padR} y1={y(max * f)} y2={y(max * f)}
                                      stroke="#313244" strokeWidth="1" />
                                <text x={padL - 6} y={y(max * f) + 3} textAnchor="end"
                                      fill="#585b70" fontSize="9" fontFamily="monospace">
                                    {compactNum(max * f)}
                                </text>
                            </g>
                        ))}

                        {hover !== null && (
                            <line x1={x(hover)} x2={x(hover)} y1={padT} y2={padT + innerH}
                                  stroke="#cdd6f4" strokeWidth="1" opacity="0.18" />
                        )}

                        {series.map(s => {
                            // A day a kind has no row for is a gap, not a zero:
                            // the pipeline may simply not have run. Breaking the
                            // path says "unknown"; drawing through zero would
                            // assert it produced nothing that day.
                            let d = "", open = false;
                            days.forEach((day, i) => {
                                const v = s.points.get(day);
                                if (v === undefined) { open = false; return; }
                                d += `${open ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
                                open = true;
                            });
                            return (
                                <g key={s.kind}>
                                    <path d={d.trim()} fill="none" stroke={colorOf(s.kind)}
                                          strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
                                    {days.map((day, i) => {
                                        const v = s.points.get(day);
                                        if (v === undefined) return null;
                                        // A single point would otherwise be an
                                        // invisible zero-length line.
                                        const solo = days.length === 1 || hover === i;
                                        return solo ? (
                                            <circle key={day} cx={x(i)} cy={y(v)} r="3.5"
                                                    fill={colorOf(s.kind)} stroke="#181825"
                                                    strokeWidth="1.5" />
                                        ) : null;
                                    })}
                                </g>
                            );
                        })}

                        {days.map((day, i) => (
                            <rect key={day} x={x(i) - innerW / (2 * Math.max(1, days.length - 1)) - 1}
                                  y={padT} width={Math.max(6, innerW / Math.max(1, days.length))}
                                  height={innerH} fill="transparent"
                                  onMouseEnter={() => setHover(i)} />
                        ))}

                        <line x1={padL} x2={W - padR} y1={y(0)} y2={y(0)}
                              stroke="#45475a" strokeWidth="1" />
                        {[0, Math.floor((days.length - 1) / 2), days.length - 1]
                            .filter((v, i, a) => a.indexOf(v) === i && days[v] !== undefined)
                            .map(i => (
                                <text key={i} x={x(i)} y={H - 6} textAnchor="middle"
                                      fill="#585b70" fontSize="9" fontFamily="monospace">
                                    {days[i].slice(5)}
                                </text>
                            ))}
                    </svg>

                    {hover !== null && (
                        <div class="absolute top-2 pointer-events-none bg-[#11111b] border border-[#45475a] rounded-lg px-3 py-2 shadow-xl z-10"
                             style={{ left: `${Math.min(72, (x(hover) / W) * 100)}%`, minWidth: '160px' }}>
                            <div class="text-[11px] text-[#cdd6f4] font-mono mb-1">{days[hover]}</div>
                            {series.map(s => {
                                const v = s.points.get(days[hover]);
                                return (
                                    <div key={s.kind} class="flex items-center justify-between gap-3 text-[11px]">
                                        <span class="flex items-center gap-1.5 text-[#a6adc8]">
                                            <span class="w-2 h-2 rounded-full inline-block"
                                                  style={{ background: colorOf(s.kind) }}></span>
                                            {prettyKind(s.kind)}
                                        </span>
                                        <span class="font-mono text-[#cdd6f4]">
                                            {v === undefined ? '—' : compactNum(v)}
                                        </span>
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </div>
            );
        }

        // The filter row doubles as the legend, which is why it sits directly
        // above its chart and always shows every kind present — identity is never
        // left to colour alone, and turning a kind off does not remove it from
        // the key.
        function KindBubbles({ kinds, order, selected, onToggle }) {
            const colorOf = kindColor(order);
            if (!kinds.length) return null;
            const all = !selected.length;
            return (
                <div class="flex flex-wrap items-center gap-1.5 px-4 pt-3">
                    {kinds.map(k => {
                        const on = all || selected.includes(k);
                        return (
                            <button key={k} onClick={() => onToggle(k)}
                                    class={`flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-[11px] transition-colors ${
                                        on ? 'border-[#585b70] bg-[#313244] text-[#cdd6f4]'
                                           : 'border-[#313244] bg-transparent text-[#585b70] hover:text-[#a6adc8]'
                                    }`}>
                                <span class="w-2 h-2 rounded-full inline-block"
                                      style={{ background: on ? colorOf(k) : '#45475a' }}></span>
                                {prettyKind(k)}
                            </button>
                        );
                    })}
                    {!all && (
                        <button onClick={() => onToggle(null)}
                                class="px-2 py-1 text-[11px] text-[#585b70] hover:text-[#a6adc8] underline">
                            show all
                        </button>
                    )}
                </div>
            );
        }

        // Stacked daily bars. Deliberately two of these rather than one chart
        // with two y-scales: tokens and run counts differ by four orders of
        // magnitude, and a dual axis lets the reader infer a relationship from
        // whatever the scaling happens to make the lines do.
        function DailyStacks({ rows, valueOf, title, subtitle }) {
            const [hover, setHover] = React.useState(null);

            const days = React.useMemo(() => {
                const byDay = new Map();
                (rows || []).forEach(r => {
                    const day = String(r.day).slice(0, 10);
                    if (!byDay.has(day)) byDay.set(day, { day, total: 0, parts: {} });
                    const slot = byDay.get(day);
                    const v = valueOf(r) || 0;
                    slot.parts[r.kind] = (slot.parts[r.kind] || 0) + v;
                    slot.total += v;
                });
                return Array.from(byDay.values()).sort((a, b) => a.day.localeCompare(b.day));
            }, [rows, valueOf]);

            if (!days.length) {
                return (
                    <div class="p-6 text-center text-sm text-[#585b70]">
                        Nothing recorded in this window.
                    </div>
                );
            }

            const W = 820, H = 170, padL = 44, padR = 8, padT = 10, padB = 20;
            const innerW = W - padL - padR, innerH = H - padT - padB;
            const max = Math.max(...days.map(d => d.total)) || 1;
            const slot = innerW / days.length;
            const barW = Math.max(1, Math.min(slot - 2, 26));
            const y = (v) => padT + innerH - (v / max) * innerH;
            const present = PRODUCER_ORDER.filter(k => days.some(d => d.parts[k]));

            return (
                <div class="p-4">
                    <div class="flex items-baseline justify-between gap-4 mb-1">
                        <div>
                            <div class="text-sm text-[#cdd6f4]">{title}</div>
                            <div class="text-[11px] text-[#585b70]">{subtitle}</div>
                        </div>
                        {/* Legend is always present for >= 2 series, so identity
                            is never carried by colour alone. */}
                        <div class="flex gap-3 flex-wrap justify-end">
                            {present.map(k => (
                                <span key={k} class="flex items-center gap-1.5 text-[11px] text-[#a6adc8]">
                                    <span class="w-2.5 h-2.5 rounded-sm inline-block"
                                          style={{ background: PRODUCER_COLORS[k] }}></span>
                                    {PRODUCER_LABELS[k] || k}
                                </span>
                            ))}
                        </div>
                    </div>

                    <div class="relative">
                        <svg viewBox={`0 0 ${W} ${H}`} class="w-full" style={{ height: '170px' }}
                             onMouseLeave={() => setHover(null)}>
                            {/* Recessive grid: it locates values, it is not data. */}
                            {[0, 0.5, 1].map(f => (
                                <g key={f}>
                                    <line x1={padL} x2={W - padR} y1={y(max * f)} y2={y(max * f)}
                                          stroke="#313244" strokeWidth="1" />
                                    <text x={padL - 6} y={y(max * f) + 3} textAnchor="end"
                                          fill="#585b70" fontSize="9" fontFamily="monospace">
                                        {compactNum(max * f)}
                                    </text>
                                </g>
                            ))}

                            {days.map((d, i) => {
                                const x = padL + i * slot + (slot - barW) / 2;
                                let acc = 0;
                                return (
                                    <g key={d.day}>
                                        {hover === i && (
                                            <rect x={padL + i * slot} y={padT} width={slot} height={innerH}
                                                  fill="#cdd6f4" opacity="0.06" />
                                        )}
                                        {present.map(k => {
                                            const v = d.parts[k] || 0;
                                            if (!v) return null;
                                            const h = (v / max) * innerH;
                                            const yTop = y(acc + v);
                                            acc += v;
                                            // 2px surface gap between stacked
                                            // segments so adjacent fills read as
                                            // separate quantities. The 1px floor
                                            // keeps a very small value visible,
                                            // and the clamp stops that floor from
                                            // pushing the segment through the
                                            // baseline — a day with one run would
                                            // otherwise draw a bar hanging below
                                            // the axis.
                                            const segH = Math.max(1, h - 2);
                                            const segY = Math.min(yTop, padT + innerH - segH);
                                            return (
                                                <rect key={k} x={x} y={segY} width={barW}
                                                      height={segH} rx="2"
                                                      fill={PRODUCER_COLORS[k]} />
                                            );
                                        })}
                                        <rect x={padL + i * slot} y={padT} width={slot} height={innerH}
                                              fill="transparent"
                                              onMouseEnter={() => setHover(i)} />
                                    </g>
                                );
                            })}

                            <line x1={padL} x2={W - padR} y1={y(0)} y2={y(0)}
                                  stroke="#45475a" strokeWidth="1" />
                            {[0, Math.floor(days.length / 2), days.length - 1]
                                .filter((v, i, a) => a.indexOf(v) === i && days[v])
                                .map(i => (
                                    <text key={i} x={padL + i * slot + slot / 2} y={H - 6}
                                          textAnchor="middle" fill="#585b70" fontSize="9"
                                          fontFamily="monospace">
                                        {days[i].day.slice(5)}
                                    </text>
                                ))}
                        </svg>

                        {hover !== null && days[hover] && (
                            <div class="absolute top-0 pointer-events-none bg-[#11111b] border border-[#45475a] rounded-lg px-3 py-2 shadow-xl z-10"
                                 style={{
                                     left: `${Math.min(78, (padL + hover * slot) / W * 100)}%`,
                                     minWidth: '150px',
                                 }}>
                                <div class="text-[11px] text-[#cdd6f4] font-mono mb-1">{days[hover].day}</div>
                                {present.map(k => (
                                    <div key={k} class="flex items-center justify-between gap-3 text-[11px]">
                                        <span class="flex items-center gap-1.5 text-[#a6adc8]">
                                            <span class="w-2 h-2 rounded-sm inline-block"
                                                  style={{ background: PRODUCER_COLORS[k] }}></span>
                                            {PRODUCER_LABELS[k] || k}
                                        </span>
                                        <span class="font-mono text-[#cdd6f4]">
                                            {compactNum(days[hover].parts[k] || 0)}
                                        </span>
                                    </div>
                                ))}
                                <div class="flex items-center justify-between gap-3 text-[11px] mt-1 pt-1 border-t border-[#313244]">
                                    <span class="text-[#585b70]">Total</span>
                                    <span class="font-mono text-[#cdd6f4]">{compactNum(days[hover].total)}</span>
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            );
        }
        var jobMatchesApp = (job, appId) => {
            if (!job.adk_app || !appId) return false;
            return job.adk_app === appId || tail(job.adk_app) === tail(appId);
        };

        // Lay a flow diagram out in columns. Hand-rolled rather than pulling in
        // a charting library: this page has no build step and loads nothing from
        // a CDN, and the general Sankey problem (crossing minimisation, cycles)
        // is not the problem here — these graphs are small, acyclic and already
        // layered by meaning.
        //
        // A node's height is max(inflow, outflow), which for an interior node
        // are equal because the backend guarantees the diagram balances. Where
        // they differ the node is a source or a sink, and the larger side is the
        // one that describes it.
        function sankeyLayout(nodes, links, { width, height, nodeW, gap }) {
            const byId = {};
            nodes.forEach(n => { byId[n.id] = { ...n, in: 0, out: 0, col: 0 }; });
            links.forEach(l => {
                if (!byId[l.source] || !byId[l.target]) return;
                byId[l.source].out += l.value;
                byId[l.target].in += l.value;
            });

            // Longest path from any source fixes the column. Iterating to a
            // fixed point rather than recursing keeps a malformed payload from
            // blowing the stack — the node count bounds the passes.
            const order = nodes.map(n => n.id);
            for (let pass = 0; pass < order.length + 1; pass++) {
                let moved = false;
                links.forEach(l => {
                    const s = byId[l.source], t = byId[l.target];
                    if (!s || !t) return;
                    if (t.col < s.col + 1) { t.col = s.col + 1; moved = true; }
                });
                if (!moved) break;
            }

            const columns = {};
            Object.values(byId).forEach(n => {
                n.value = Math.max(n.in, n.out);
                if (!n.value) return;
                (columns[n.col] = columns[n.col] || []).push(n);
            });
            const colKeys = Object.keys(columns).map(Number).sort((a, b) => a - b);
            if (!colKeys.length) return { nodes: [], ribbons: [] };

            // One scale for every column, from the heaviest, so a bar's height
            // means the same thing wherever it sits. Scaling per column would
            // make a small column's bars as tall as a large one's and quietly
            // destroy the comparison the diagram exists to make.
            let heaviest = 0;
            colKeys.forEach(c => {
                const total = columns[c].reduce((sum, n) => sum + n.value, 0);
                heaviest = Math.max(heaviest, total);
            });
            const tallestCount = Math.max(...colKeys.map(c => columns[c].length));
            const usable = Math.max(20, height - gap * Math.max(0, tallestCount - 1));
            const scale = heaviest ? usable / heaviest : 0;

            const stepX = colKeys.length > 1
                ? (width - nodeW) / (colKeys.length - 1)
                : 0;
            const placed = {};
            colKeys.forEach((c, ci) => {
                const col = columns[c].sort((a, b) => b.value - a.value);
                const total = col.reduce((sum, n) => sum + n.value * scale, 0)
                    + gap * (col.length - 1);
                let y = Math.max(0, (height - total) / 2);
                col.forEach(n => {
                    const h = Math.max(2, n.value * scale);
                    placed[n.id] = {
                        ...n, x: ci * stepX, y, h, w: nodeW,
                        colIndex: ci, lastCol: ci === colKeys.length - 1,
                        outY: y, inY: y,
                    };
                    y += h + gap;
                });
            });

            // Ribbons leave a node stacked in the order the links were given,
            // which the backend sorts by size — so the fattest flow sits on top
            // and the eye follows it first.
            const ribbons = [];
            links.forEach(l => {
                const s = placed[l.source], t = placed[l.target];
                if (!s || !t || !l.value) return;
                const h = Math.max(1, l.value * scale);
                const y0 = s.outY, y1 = t.inY;
                s.outY += h;
                t.inY += h;
                const x0 = s.x + s.w, x1 = t.x;
                const mid = (x0 + x1) / 2;
                ribbons.push({
                    key: `${l.source}->${l.target}`,
                    source: l.source, target: l.target, value: l.value,
                    colIndex: s.colIndex,
                    d: `M${x0},${y0} C${mid},${y0} ${mid},${y1} ${x1},${y1}`
                        + ` L${x1},${y1 + h} C${mid},${y1 + h} ${mid},${y0 + h} ${x0},${y0 + h} Z`,
                });
            });
            return { nodes: Object.values(placed), ribbons };
        }

        function SankeyDiagram({ diagram }) {
            const [hover, setHover] = useState(null);
            const links = (diagram.links || []).filter(l => l.value > 0);
            if (!links.length) {
                return (
                    <div class="p-6 text-center text-sm text-[#585b70]">
                        Nothing flowed through this stage in the selected window.
                    </div>
                );
            }

            // The gap is set by the label, not by taste: a one-line label needs
            // ~12px, so anything less lets the labels of two thin neighbouring
            // flows overlap even though their bars do not.
            const W = 820, gap = 14, nodeW = 10;
            const rows = Math.max(3, (diagram.nodes || []).length);
            const H = Math.min(360, Math.max(150, rows * 34));
            const padL = 4, padR = 4, padT = 8, padB = 8;
            const laid = sankeyLayout(diagram.nodes || [], links, {
                width: W - padL - padR - 150,
                height: H - padT - padB,
                nodeW, gap,
            });

            return (
                <div class="relative px-4 pb-3">
                    <svg viewBox={`0 0 ${W} ${H}`} class="w-full"
                         style={{ height: `${H}px` }}
                         onMouseLeave={() => setHover(null)}>
                        <g transform={`translate(${padL},${padT})`}>
                            {laid.ribbons.map(r => (
                                <path key={r.key} d={r.d}
                                      fill={KIND_PALETTE[r.colIndex % KIND_PALETTE.length]}
                                      opacity={hover === null ? 0.28
                                          : (hover === r.key ? 0.6 : 0.08)}
                                      onMouseEnter={() => setHover(r.key)} />
                            ))}
                            {laid.nodes.map(n => (
                                <g key={n.id}>
                                    <rect x={n.x} y={n.y} width={n.w} height={n.h} rx="2"
                                          fill={KIND_PALETTE[n.colIndex % KIND_PALETTE.length]}
                                          opacity="0.9" />
                                    {/* Label and count on ONE line. Two lines need ~19px of
                                        vertical room, which a thin bar does not have — the
                                        smallest flows are exactly the ones whose labels
                                        collided with their neighbour's. */}
                                    <text x={n.x + n.w + 6} y={n.y + n.h / 2}
                                          fill="var(--txt-subtle)" fontSize="10"
                                          dominantBaseline="middle">
                                        {n.label}
                                        <tspan fill="var(--txt-muted)" fontFamily="monospace"
                                               fontSize="9" dx="5">
                                            {fmtExact(n.value)}
                                        </tspan>
                                    </text>
                                </g>
                            ))}
                        </g>
                    </svg>
                    {/* Always rendered, blank when nothing is hovered. Showing this
                        row only on hover reflows everything below the chart at the
                        exact moment the pointer is moving across it, so the diagram
                        jumps out from under the cursor. The row keeps its line box
                        either way; only the text inside changes. */}
                    <div class="text-[11px] text-[#a6adc8] font-mono px-1 leading-4 h-4 truncate">
                        {(() => {
                            const r = laid.ribbons.find(x => x.key === hover);
                            if (!r) return ' ';
                            const label = (id) => ((diagram.nodes || [])
                                .find(n => n.id === id) || {}).label || id;
                            return `${label(r.source)} → ${label(r.target)}: ${fmtExact(r.value)}`;
                        })()}
                    </div>
                </div>
            );
        }

        function StatTile({ icon, label, value, hint, muted }) {
            return (
                <div class={`bg-[#181825] rounded-xl p-4 ${muted ? 'border border-dashed border-[#45475a]' : 'border border-[#313244]'}`}>
                    <div class="text-[10px] uppercase text-[#9ca3af] mb-2 font-bold tracking-wider flex items-center gap-1.5">
                        <i data-lucide={icon} class="w-3.5 h-3.5"></i> {label}
                    </div>
                    <div class={`font-mono text-lg ${muted ? 'text-[#585b70]' : 'text-[#cdd6f4]'}`}>{value}</div>
                    {hint && <div class="text-[10px] text-[#585b70] mt-1">{hint}</div>}
                </div>
            );
        }

        // Every agent in the rail renders through this, so a Hermes profile and
        // an ADK agent are visually the same kind of thing — one robot icon,
        // one layout. The only mark that distinguishes them is the lock, which
        // means "system default: this one is not yours to edit here".
        // tag/warn ride along for the rail's team rows: one row now stands for a
        // whole team, so where its description came from — and whether reading it
        // failed — has to be legible on the row itself.
        // subMono replaces the model line for rows that stand for a team: a
        // workflow root holds no model of its own, and "model not reported" on
        // every team is noise. The app it belongs to is the useful identifier.
        function AgentRow({ label, model, subMono, meta, active, locked, step, indent, tag, tagTitle, warn, onClick }) {
            const dim = active ? 'text-[#1e1e2e]' : 'text-[#585b70]';
            return (
                <button
                    onClick={onClick}
                    style={indent ? { marginLeft: `${indent * 14}px` } : undefined}
                    class={`w-full text-left px-2.5 py-2 rounded-lg flex flex-col gap-0.5 transition ${
                        active ? 'bg-[#b4befe] text-[#11111b] font-medium' : 'hover:bg-[#313244] text-[#a6adc8]'
                    }`}
                >
                    <span class="text-sm font-semibold flex items-center gap-1.5 min-w-0">
                        <i data-lucide="bot" class="w-3.5 h-3.5 shrink-0"></i>
                        {step && <span class={`text-[10px] font-mono ${active ? 'text-[#1e1e2e]' : 'text-[#585b70]'}`}>{step}</span>}
                        <span class="truncate">{label}</span>
                        {locked && (
                            <i data-lucide="lock" class={`w-3 h-3 shrink-0 ml-auto ${active ? 'text-[#1e1e2e]' : 'text-[#585b70]'}`}
                               title="System default — edited by the worker, not here"></i>
                        )}
                        {tag && (
                            <span title={tagTitle || undefined}
                                  class={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded shrink-0 ml-auto ${
                                      active ? 'bg-[#11111b]/15 text-[#1e1e2e]'
                                             : tag === 'live' ? 'bg-[#a6e3a1]/15 text-[#a6e3a1]'
                                             : 'bg-[#89b4fa]/15 text-[#89b4fa]'}`}>
                                {tag}
                            </span>
                        )}
                    </span>
                    {warn && (
                        <span class={`text-[10px] ${active ? 'text-[#1e1e2e]' : 'text-[#f38ba8]'}`}>{warn}</span>
                    )}
                    <span class={`text-[10px] font-mono truncate ${
                        active ? 'text-[#1e1e2e]'
                               : subMono ? 'text-[#585b70]'
                               : (model ? 'text-[#a6e3a1]' : 'text-[#585b70] italic')
                    }`}>
                        {subMono || model || 'model not reported'}
                    </span>
                    {meta && <span class={`text-[10px] ${dim}`}>{meta}</span>}
                </button>
            );
        }

        // One team = one bordered card. The grouping has to survive two teams
        // that look nothing like each other, so the header always states where
        // the description came from: parsed off disk, or read live from ADK.
        function TeamCard({ title, subtitle, source, warn, children }) {
            const live = source === 'live';
            return (
                <div class="mb-3 rounded-xl border border-[#313244] bg-[#181825]/50 overflow-hidden">
                    <div class="px-3 py-2 bg-[#11111b]/60 border-b border-[#313244]">
                        <div class="flex items-center justify-between gap-2">
                            <span class="text-[11px] font-bold uppercase tracking-wider text-[#cdd6f4] truncate">{title}</span>
                            {source && (
                                <span class={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded shrink-0 ${
                                    live ? 'bg-[#a6e3a1]/15 text-[#a6e3a1]' : 'bg-[#89b4fa]/15 text-[#89b4fa]'
                                }`} title={live
                                    ? 'Read live from the running ADK server — reflects what is loaded, not what is on disk'
                                    : 'Parsed from agent.py on disk — reflects edits before the restart that loads them'}>
                                    {live ? 'live' : 'source'}
                                </span>
                            )}
                        </div>
                        <div class="flex items-center justify-between gap-2 mt-0.5">
                            <span class="text-[10px] text-[#585b70] truncate">{subtitle}</span>
                            {warn && <span class="text-[10px] text-[#f38ba8] shrink-0">{warn}</span>}
                        </div>
                    </div>
                    <div class="p-1.5 space-y-0.5">{children}</div>
                </div>
            );
        }

        // Metrics formatters. All three share one rule: null is not zero. The
        // store goes to real trouble to distinguish "nobody measured this" from
        // "this was zero", and rendering both as 0 here would throw that away at
        // the last step — which is where it would do the most damage, because a
        // number on a dashboard reads as a fact.
        //
        // They live here rather than with the Metrics tab because the Agents
        // scorecard formats its run counts and token totals with them too.
        var fmtNum = (n) => {
            if (n === null || n === undefined) return "—";
            if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
            if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
            if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
            return String(n);
        };

        var fmtUsd = (n) => {
            if (n === null || n === undefined) return "—";
            return "$" + Number(n).toFixed(2);
        };

        // What each cost class means, shown on the card itself. Without this the
        // three numbers look like they ought to add up.
        var COST_CLASS_META = {
            metered:  { label: "Metered spend",  hint: "real money at a published rate",   color: "#a6e3a1" },
            included: { label: "Subscription",   hint: "already paid for; $0 marginal",    color: "#b4befe" },
            unpriced: { label: "Unpriced",       hint: "local models; no rate exists",     color: "#9ca3af" },
        };

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
        window.prettyKind = prettyKind;
