/** Graph visualization — force-directed layout with DPI-aware canvas,
 *  live simulation, node dragging, rich tooltip, detail sidebar,
 *  directional arrows, and edge highlighting on hover.
 */

const GraphViz = {
    sim: null,
    animId: null,

    kindColors: {
        function: '#58a6ff', method: '#3fb950', class: '#bc8cff',
        file: '#d29922', module: '#db6d28', repository: '#79c0ff',
        type: '#f85149', constant: '#d29922', api_endpoint: '#3fb950',
        config: '#db6d28',
    },

    kindBaseRadius: {
        repository: 18, module: 12, file: 8, class: 10,
        function: 5, method: 5, type: 7, constant: 5, api_endpoint: 6, config: 6,
    },

    edgeKindColors: {
        CALLS: '#58a6ff', IMPORTS: '#d29922', INHERITS: '#bc8cff',
        USES_TYPE: '#f85149', CONTAINS: '#484f58', READS_CONFIG: '#db6d28',
    },

    /* ── Render entry ──────────────────────────────────── */
    async render(container) {
        this.cleanup();
        container.innerHTML = '';

        /* toolbar */
        const toolbar = Utils.el('div', { className: 'graph-toolbar' });
        const repoSel = Utils.el('select', { id: 'graph-repo-filter' });
        repoSel.innerHTML = '<option value="">All Repos</option>';
        try {
            const stats = await Api.getStats();
            for (const r of stats.repos || [])
                repoSel.innerHTML += `<option value="${r}">${r}</option>`;
        } catch (_) {}

        const maxSel = Utils.el('select', { id: 'graph-max-nodes' });
        for (const n of [50, 100, 200, 500])
            maxSel.innerHTML += `<option value="${n}" ${n === 200 ? 'selected' : ''}>${n}</option>`;

        const load  = Utils.el('button', { className: 'btn btn-primary btn-sm', onclick: () => this.loadGraph(container) }, 'Load');
        const sep   = () => Utils.el('div', { className: 'toolbar-sep' });
        const zIn   = Utils.el('button', { className: 'btn btn-secondary btn-sm', onclick: () => this.zoomBy(1.3) }, '+');
        const zOut  = Utils.el('button', { className: 'btn btn-secondary btn-sm', onclick: () => this.zoomBy(1/1.3) }, '−');
        const fit   = Utils.el('button', { className: 'btn btn-secondary btn-sm', onclick: () => this.fitToScreen() }, 'Fit');
        const reset = Utils.el('button', { className: 'btn btn-secondary btn-sm', onclick: () => this.resetView() }, 'Reset');
        const pause = Utils.el('button', { className: 'btn btn-secondary btn-sm', id: 'graph-pause-btn', onclick: () => this.togglePause() }, '⏸ Pause');
        const info  = Utils.el('span', { id: 'graph-info', className: 'graph-info' });

        toolbar.append(repoSel, maxSel, load, sep(), zIn, zOut, fit, reset, sep(), pause, sep(), info);
        container.appendChild(toolbar);

        /* canvas wrapper */
        const wrap = Utils.el('div', { className: 'graph-canvas-container' });
        wrap.appendChild(Utils.el('canvas', { id: 'graph-canvas' }));
        wrap.appendChild(Utils.el('div', { className: 'graph-tooltip hidden', id: 'graph-tooltip' }));
        wrap.appendChild(Utils.el('div', { className: 'graph-sidebar hidden', id: 'graph-sidebar' }));
        container.appendChild(wrap);

        await this.loadGraph(container);
    },

    cleanup() {
        if (this.animId) cancelAnimationFrame(this.animId);
        this.animId = null;
        this.sim = null;
    },

    /* ── Load data & start ─────────────────────────────── */
    async loadGraph(container) {
        this.cleanup();
        const canvas = container.querySelector('#graph-canvas');
        if (!canvas) return;

        const repo = container.querySelector('#graph-repo-filter')?.value || '';
        const max  = parseInt(container.querySelector('#graph-max-nodes')?.value || '200');

        /* DPI-aware sizing */
        const pRect = canvas.parentElement.getBoundingClientRect();
        const W = pRect.width;
        const H = Math.max(500, window.innerHeight - 180);
        const dpr = window.devicePixelRatio || 1;
        canvas.width  = W * dpr;
        canvas.height = H * dpr;
        canvas.style.width  = W + 'px';
        canvas.style.height = H + 'px';

        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        /* loading message */
        ctx.fillStyle = '#1c2128'; ctx.fillRect(0, 0, W, H);
        ctx.fillStyle = '#8b949e'; ctx.font = '14px Inter, sans-serif';
        ctx.textAlign = 'center'; ctx.fillText('Loading graph…', W/2, H/2);

        try {
            const data = await Api.getGraphData({ repo, max });
            if (!data.nodes.length) {
                ctx.fillStyle = '#1c2128'; ctx.fillRect(0, 0, W, H);
                ctx.fillStyle = '#8b949e'; ctx.font = '16px Inter, sans-serif';
                ctx.textAlign = 'center'; ctx.fillText('No nodes to display', W/2, H/2);
                return;
            }
            this._run(canvas, data, container, W, H);
        } catch (err) {
            ctx.fillStyle = '#1c2128'; ctx.fillRect(0, 0, W, H);
            ctx.fillStyle = '#f85149'; ctx.font = '14px Inter, sans-serif';
            ctx.textAlign = 'left'; ctx.fillText('Error: ' + err.message, 20, 30);
        }
    },

    /* ── Main simulation ───────────────────────────────── */
    _run(canvas, data, container, W, H) {
        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        const CX = W / 2, CY = H / 2;
        const N = data.nodes.length;

        /* degree map */
        const deg = {};
        for (const l of data.links) {
            deg[l.source] = (deg[l.source] || 0) + 1;
            deg[l.target] = (deg[l.target] || 0) + 1;
        }

        /* init nodes */
        const nodes = data.nodes.map((n, i) => {
            const a = (2 * Math.PI * i) / N;
            const r = Math.min(W, H) * 0.25;
            const d = deg[n.id] || 0;
            const br = this.kindBaseRadius[n.kind] || 5;
            return {
                ...n, degree: d,
                x: CX + r * Math.cos(a) + (Math.random() - 0.5) * 30,
                y: CY + r * Math.sin(a) + (Math.random() - 0.5) * 30,
                vx: 0, vy: 0,
                radius: Math.min(br + Math.sqrt(d) * 1.5, 24),
                fixed: false,
            };
        });

        const nMap = {};
        nodes.forEach(n => nMap[n.id] = n);
        const links = data.links.filter(l => nMap[l.source] && nMap[l.target]);

        /* physics — tighter clustering */
        const REP = 600 + N * 2;
        const ATT = 0.008;
        const GRAV = 0.02;
        const DAMP = 0.82;
        let alpha = 1.0;
        let paused = false;

        /* view */
        let scale = 1, offX = 0, offY = 0;

        /* interaction */
        let hovered = null, selected = null, dragN = null;
        let dragging = false, panning = false;
        let panSX = 0, panSY = 0, panOX = 0, panOY = 0;

        const toWorld = (sx, sy) => [sx / scale - offX, sy / scale - offY];
        const hit = (wx, wy) => {
            for (let i = nodes.length - 1; i >= 0; i--) {
                const n = nodes[i];
                const dx = wx - n.x, dy = wy - n.y;
                if (dx*dx + dy*dy < (n.radius + 3) * (n.radius + 3)) return n;
            }
            return null;
        };
        const mWorld = (e) => {
            const r = canvas.getBoundingClientRect();
            return toWorld(e.clientX - r.left, e.clientY - r.top);
        };

        /* ── tick ── */
        const tick = () => {
            if (paused || alpha < 0.001) return;
            alpha *= 0.995;

            for (let i = 0; i < N; i++) {
                const a = nodes[i];
                if (a.fixed) continue;
                for (let j = i + 1; j < N; j++) {
                    const b = nodes[j];
                    const dx = b.x - a.x, dy = b.y - a.y;
                    const d2 = dx*dx + dy*dy;
                    const d = Math.max(1, Math.sqrt(d2));
                    const f = Math.min(REP / d2, 8) * alpha;
                    const fx = dx/d*f, fy = dy/d*f;
                    a.vx -= fx; a.vy -= fy;
                    if (!b.fixed) { b.vx += fx; b.vy += fy; }
                }
            }

            for (const l of links) {
                const s = nMap[l.source], t = nMap[l.target];
                if (!s || !t) continue;
                const dx = t.x - s.x, dy = t.y - s.y;
                const d = Math.max(1, Math.sqrt(dx*dx + dy*dy));
                const ideal = 40 + s.radius + t.radius;
                const f = (d - ideal) * ATT * alpha;
                const fx = dx/d*f, fy = dy/d*f;
                if (!s.fixed) { s.vx += fx; s.vy += fy; }
                if (!t.fixed) { t.vx -= fx; t.vy -= fy; }
            }

            for (const n of nodes) {
                if (n.fixed) continue;
                n.vx += (CX - n.x) * GRAV * alpha;
                n.vy += (CY - n.y) * GRAV * alpha;
                n.vx *= DAMP; n.vy *= DAMP;
                const mv = 12 * alpha;
                n.vx = Math.max(-mv, Math.min(mv, n.vx));
                n.vy = Math.max(-mv, Math.min(mv, n.vy));
                n.x += n.vx; n.y += n.vy;
            }
        };

        /* ── draw ── */
        const draw = () => {
            /* clear */
            ctx.save();
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.fillStyle = '#1c2128';
            ctx.fillRect(0, 0, W, H);
            ctx.restore();

            /* world transform */
            ctx.save();
            ctx.setTransform(scale*dpr, 0, 0, scale*dpr, offX*scale*dpr, offY*scale*dpr);
            ctx.textAlign = 'left';
            ctx.textBaseline = 'alphabetic';

            const hl = hovered != null;
            const conn = new Set();
            if (hl) {
                conn.add(hovered.id);
                for (const l of links) {
                    if (l.source === hovered.id || l.target === hovered.id) {
                        conn.add(l.source); conn.add(l.target);
                    }
                }
            }

            /* edges */
            for (const l of links) {
                const s = nMap[l.source], t = nMap[l.target];
                if (!s || !t) continue;
                const active = !hl || l.source === hovered?.id || l.target === hovered?.id;
                ctx.globalAlpha = hl ? (active ? 0.7 : 0.05) : 0.2;
                const ec = active ? (this.edgeKindColors[l.kind] || '#484f58') : '#484f58';
                ctx.strokeStyle = ec;
                ctx.lineWidth = (active && hl ? 1.5 : 0.6) / scale;

                ctx.beginPath();
                ctx.moveTo(s.x, s.y);
                ctx.lineTo(t.x, t.y);
                ctx.stroke();

                /* arrow */
                const dx = t.x - s.x, dy = t.y - s.y;
                const d = Math.sqrt(dx*dx + dy*dy);
                if (scale > 0.4 && d > 25) {
                    const sz = Math.min(6, 4/scale);
                    const ang = Math.atan2(dy, dx);
                    const ax = t.x - dx/d*(t.radius+2), ay = t.y - dy/d*(t.radius+2);
                    ctx.fillStyle = ec;
                    ctx.beginPath();
                    ctx.moveTo(ax, ay);
                    ctx.lineTo(ax - sz*Math.cos(ang-0.4), ay - sz*Math.sin(ang-0.4));
                    ctx.lineTo(ax - sz*Math.cos(ang+0.4), ay - sz*Math.sin(ang+0.4));
                    ctx.closePath();
                    ctx.fill();
                }
            }
            ctx.globalAlpha = 1;

            /* nodes */
            for (const n of nodes) {
                const c = this.kindColors[n.kind] || '#8b949e';
                const isConn = !hl || conn.has(n.id);
                const isHov  = n === hovered;
                const isSel  = n === selected;
                ctx.globalAlpha = hl ? (isConn ? 1 : 0.1) : 1;

                if (n.radius >= 12 && isConn) {
                    ctx.beginPath();
                    ctx.arc(n.x, n.y, n.radius + 3, 0, Math.PI * 2);
                    ctx.fillStyle = c + '10';
                    ctx.fill();
                }

                ctx.beginPath();
                ctx.arc(n.x, n.y, n.radius, 0, Math.PI * 2);
                ctx.fillStyle = isHov ? this._bright(c, 40) : c;
                ctx.fill();
                ctx.strokeStyle = isSel ? '#fff' : (isHov ? '#e6edf3' : c + '60');
                ctx.lineWidth = (isSel ? 2.5 : isHov ? 1.5 : 0.5) / scale;
                ctx.stroke();

                if (isSel) {
                    ctx.beginPath();
                    ctx.arc(n.x, n.y, n.radius + 4, 0, Math.PI * 2);
                    ctx.strokeStyle = '#58a6ff80';
                    ctx.lineWidth = 2 / scale;
                    ctx.stroke();
                }
            }
            ctx.globalAlpha = 1;

            /* labels — only show for important nodes; cull overlaps */
            const labelMinRadius = scale < 0.5 ? 14 : scale < 0.8 ? 10 : 7;
            const labelCandidates = nodes
                .filter(n => {
                    if (n === hovered || n === selected) return true;
                    if (hl && !conn.has(n.id)) return false;
                    return n.radius >= labelMinRadius;
                })
                .sort((a, b) => b.radius - a.radius);

            /* simple overlap culling in screen space */
            const placed = [];
            for (const n of labelCandidates) {
                const fs = Math.max(9, Math.min(12, n.radius + 1)) / Math.max(0.9, scale * 0.7);
                const lab = Utils.truncate(n.name, 18);
                ctx.font = `500 ${fs}px Inter, sans-serif`;
                const tw = ctx.measureText(lab).width;

                const sx = (n.x + offX) * scale + n.radius * scale + 5;
                const sy = (n.y + offY) * scale;
                const box = { x: sx, y: sy - fs/2, w: tw + 10, h: fs + 6 };

                const skip = n !== hovered && n !== selected &&
                    placed.some(p => p.x < box.x + box.w && p.x + p.w > box.x &&
                                     p.y < box.y + box.h && p.y + p.h > box.y);
                if (skip) continue;
                placed.push(box);

                const lx = n.x + n.radius + 4;
                const ly = n.y + fs * 0.35;
                const pad = 2 / scale;

                ctx.fillStyle = '#0d111790';
                this._rrect(ctx, lx-pad, ly-fs*0.7-pad, tw+pad*2, fs+pad*2, 3/scale);
                ctx.fill();

                ctx.fillStyle = n === hovered ? '#fff' : '#c9d1d9';
                ctx.fillText(lab, lx, ly);
            }

            ctx.restore();

            /* legend (screen space) */
            ctx.save();
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';
            ctx.font = '11px Inter, sans-serif';
            let ly2 = 20;
            for (const [k, c] of Object.entries(this.kindColors)) {
                ctx.fillStyle = c;
                ctx.beginPath(); ctx.arc(W - 120, ly2, 4, 0, Math.PI * 2); ctx.fill();
                ctx.fillStyle = '#8b949e'; ctx.fillText(k, W - 112, ly2);
                ly2 += 18;
            }
            ctx.fillStyle = '#484f58';
            ctx.textBaseline = 'alphabetic';
            ctx.fillText('Zoom: ' + (scale * 100).toFixed(0) + '%', 12, H - 12);
            ctx.restore();
        };

        /* ── loop ── */
        const loop = () => { tick(); draw(); this.animId = requestAnimationFrame(loop); };

        /* store sim ref */
        this.sim = {
            nodes, links, nodeMap: nMap, canvas, container, W, H,
            get scale() { return scale; }, set scale(v) { scale = v; },
            get offsetX() { return offX; }, set offsetX(v) { offX = v; },
            get offsetY() { return offY; }, set offsetY(v) { offY = v; },
            get alpha() { return alpha; }, set alpha(v) { alpha = v; },
            get paused() { return paused; }, set paused(v) { paused = v; },
        };

        const nfo = container.querySelector('#graph-info');
        if (nfo) nfo.textContent = nodes.length + ' nodes · ' + links.length + ' edges';

        /* ── events ── */
        canvas.onwheel = (e) => {
            e.preventDefault();
            const r = canvas.getBoundingClientRect();
            const mx = e.clientX - r.left, my = e.clientY - r.top;
            const f = e.deltaY < 0 ? 1.12 : 1/1.12;
            const ns = Math.max(0.05, Math.min(15, scale * f));
            const [wx, wy] = toWorld(mx, my);
            scale = ns;
            offX = mx/scale - wx; offY = my/scale - wy;
        };

        canvas.onmousedown = (e) => {
            const [wx, wy] = mWorld(e);
            const h = hit(wx, wy);
            if (h) {
                dragN = h; h.fixed = true; dragging = true;
                canvas.style.cursor = 'grabbing';
                alpha = Math.max(alpha, 0.3);
            } else {
                panning = true;
                panSX = e.clientX; panSY = e.clientY;
                panOX = offX; panOY = offY;
                canvas.style.cursor = 'grabbing';
            }
        };

        canvas.onmousemove = (e) => {
            if (dragging && dragN) {
                const [wx, wy] = mWorld(e);
                dragN.x = wx; dragN.y = wy; dragN.vx = 0; dragN.vy = 0;
            } else if (panning) {
                offX = panOX + (e.clientX - panSX)/scale;
                offY = panOY + (e.clientY - panSY)/scale;
            } else {
                const [wx, wy] = mWorld(e);
                const h = hit(wx, wy);
                if (h !== hovered) {
                    hovered = h;
                    canvas.style.cursor = h ? 'pointer' : 'grab';
                    this._showTip(container, h, e);
                } else if (h) {
                    this._moveTip(container, e);
                }
            }
        };

        canvas.onmouseup = () => {
            if (dragging && dragN) { dragN.fixed = false; dragN = null; dragging = false; }
            panning = false;
            canvas.style.cursor = hovered ? 'pointer' : 'grab';
        };

        canvas.onclick = (e) => {
            const [wx, wy] = mWorld(e);
            const h = hit(wx, wy);
            selected = h === selected ? null : h;
            this._sidebar(container, selected);
            if (h) alpha = Math.max(alpha, 0.1);
        };

        canvas.ondblclick = (e) => {
            const [wx, wy] = mWorld(e);
            const h = hit(wx, wy);
            if (h) { NodeBrowser.state.selectedNodeId = h.id; App.navigate('nodes'); }
        };

        canvas.onmouseleave = () => {
            panning = false; dragging = false;
            if (dragN) { dragN.fixed = false; dragN = null; }
            hovered = null;
            this._hideTip(container);
            canvas.style.cursor = 'grab';
        };

        canvas.style.cursor = 'grab';
        loop();
    },

    /* ── Tooltip ────────────────────────────────────────── */
    _showTip(ctr, node, ev) {
        const t = ctr.querySelector('#graph-tooltip');
        if (!t) return;
        if (!node) { this._hideTip(ctr); return; }
        t.innerHTML =
            '<div class="tooltip-header">' +
                '<span class="badge badge-' + node.kind + '">' + node.kind + '</span>' +
                '<span class="tooltip-name">' + Utils.escapeHtml(node.name) + '</span>' +
            '</div>' +
            '<div class="tooltip-meta">' + node.repo +
                (node.degree ? ' · ' + node.degree + ' connections' : '') +
            '</div>';
        t.classList.remove('hidden');
        this._moveTip(ctr, ev);
    },
    _moveTip(ctr, ev) {
        const t = ctr.querySelector('#graph-tooltip');
        if (!t || t.classList.contains('hidden')) return;
        const w = ctr.querySelector('.graph-canvas-container');
        const r = w.getBoundingClientRect();
        let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
        if (x + t.offsetWidth > r.width - 8) x = ev.clientX - r.left - t.offsetWidth - 8;
        if (y + t.offsetHeight > r.height - 8) y = ev.clientY - r.top - t.offsetHeight - 8;
        t.style.left = x + 'px'; t.style.top = y + 'px';
    },
    _hideTip(ctr) {
        const t = ctr.querySelector('#graph-tooltip');
        if (t) t.classList.add('hidden');
    },

    /* ── Sidebar ───────────────────────────────────────── */
    async _sidebar(ctr, node) {
        const sb = ctr.querySelector('#graph-sidebar');
        if (!sb) return;
        if (!node) { sb.classList.add('hidden'); return; }
        sb.classList.remove('hidden');
        try {
            const d = await Api.getNode(node.id);
            const n = d.node;
            sb.innerHTML =
                '<div class="sidebar-header">' +
                    '<span class="sidebar-title">' + Utils.escapeHtml(n.name) + '</span>' +
                    '<button class="sidebar-close" onclick="GraphViz.closeSidebar()">✕</button>' +
                '</div>' +
                '<div class="sidebar-body">' +
                    '<div class="sidebar-field"><span class="detail-label">Kind</span><span class="badge badge-' + n.kind + '">' + n.kind + '</span></div>' +
                    '<div class="sidebar-field"><span class="detail-label">Repo</span><span class="detail-value">' + n.repo + '</span></div>' +
                    (n.path ? '<div class="sidebar-field"><span class="detail-label">Path</span><span class="detail-value mono" title="' + Utils.escapeHtml(n.path) + '">' + Utils.truncate(n.path, 35) + '</span></div>' : '') +
                    (n.signature ? '<div class="sidebar-field"><span class="detail-label">Signature</span><span class="detail-value mono">' + Utils.escapeHtml(Utils.truncate(n.signature, 60)) + '</span></div>' : '') +
                    '<div class="sidebar-field"><span class="detail-label">Edges</span><span class="detail-value">' + d.outgoing_edges.length + ' out · ' + d.incoming_edges.length + ' in</span></div>' +
                    (n.summary ? '<div class="sidebar-summary">' + Utils.escapeHtml(n.summary) + '</div>' : '') +
                    '<button class="btn btn-secondary btn-sm sidebar-view-btn" onclick="NodeBrowser.state.selectedNodeId=\'' + n.id + '\';App.navigate(\'nodes\');">View full detail →</button>' +
                '</div>';
        } catch (err) {
            sb.innerHTML = '<div class="sidebar-body"><p style="color:var(--accent-red)">Error: ' + err.message + '</p></div>';
        }
    },
    closeSidebar() {
        const s = document.querySelector('#graph-sidebar');
        if (s) s.classList.add('hidden');
    },

    /* ── Controls ──────────────────────────────────────── */
    zoomBy(f) {
        if (!this.sim) return;
        const ns = Math.max(0.05, Math.min(15, this.sim.scale * f));
        const cx = this.sim.W/2, cy = this.sim.H/2;
        const wx = cx/this.sim.scale - this.sim.offsetX;
        const wy = cy/this.sim.scale - this.sim.offsetY;
        this.sim.scale = ns;
        this.sim.offsetX = cx/ns - wx;
        this.sim.offsetY = cy/ns - wy;
    },
    fitToScreen() {
        if (!this.sim || !this.sim.nodes.length) return;
        let x0=Infinity, x1=-Infinity, y0=Infinity, y1=-Infinity;
        for (const n of this.sim.nodes) {
            x0 = Math.min(x0, n.x-n.radius); x1 = Math.max(x1, n.x+n.radius);
            y0 = Math.min(y0, n.y-n.radius); y1 = Math.max(y1, n.y+n.radius);
        }
        const gw = x1-x0+60, gh = y1-y0+60;
        const s = Math.min(this.sim.W/gw, this.sim.H/gh, 3);
        this.sim.scale = s;
        this.sim.offsetX = this.sim.W/2/s - (x0+x1)/2;
        this.sim.offsetY = this.sim.H/2/s - (y0+y1)/2;
    },
    resetView() {
        if (!this.sim) return;
        this.sim.scale = 1; this.sim.offsetX = 0; this.sim.offsetY = 0;
    },
    togglePause() {
        if (!this.sim) return;
        this.sim.paused = !this.sim.paused;
        if (!this.sim.paused) this.sim.alpha = Math.max(this.sim.alpha, 0.3);
        const b = this.sim.container.querySelector('#graph-pause-btn');
        if (b) b.textContent = this.sim.paused ? '▶ Resume' : '⏸ Pause';
    },

    /* ── helpers ───────────────────────────────────────── */
    _bright(hex, a) {
        const r = Math.min(255, parseInt(hex.slice(1,3),16)+a);
        const g = Math.min(255, parseInt(hex.slice(3,5),16)+a);
        const b = Math.min(255, parseInt(hex.slice(5,7),16)+a);
        return 'rgb('+r+','+g+','+b+')';
    },
    _rrect(ctx, x, y, w, h, r) {
        ctx.beginPath();
        ctx.moveTo(x+r, y);
        ctx.lineTo(x+w-r, y); ctx.quadraticCurveTo(x+w, y, x+w, y+r);
        ctx.lineTo(x+w, y+h-r); ctx.quadraticCurveTo(x+w, y+h, x+w-r, y+h);
        ctx.lineTo(x+r, y+h); ctx.quadraticCurveTo(x, y+h, x, y+h-r);
        ctx.lineTo(x, y+r); ctx.quadraticCurveTo(x, y, x+r, y);
        ctx.closePath();
    },
};
