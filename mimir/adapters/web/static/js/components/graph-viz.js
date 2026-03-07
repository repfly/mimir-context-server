/** Graph visualization component — force-directed layout with zoom/pan support. */

const GraphViz = {
    async render(container) {
        container.innerHTML = '';

        // Controls
        const controls = Utils.el('div', { className: 'filter-bar' });
        const repoSelect = Utils.el('select', { id: 'graph-repo-filter' });
        repoSelect.innerHTML = '<option value="">All Repos</option>';
        try {
            const stats = await Api.getStats();
            for (const repo of stats.repos || []) {
                repoSelect.innerHTML += `<option value="${repo}">${repo}</option>`;
            }
        } catch (_) { }

        const maxInput = Utils.el('select', { id: 'graph-max-nodes' });
        for (const n of [50, 100, 200, 500]) {
            maxInput.innerHTML += `<option value="${n}" ${n === 200 ? 'selected' : ''}>${n} nodes</option>`;
        }

        const loadBtn = Utils.el('button', {
            className: 'btn btn-primary',
            onclick: () => this.loadGraph(container),
        }, 'Load Graph');

        const zoomInfo = Utils.el('span', { id: 'zoom-info', style: 'color:var(--text-secondary);font-size:12px;margin-left:12px' }, 'Scroll to zoom, drag to pan');

        controls.appendChild(repoSelect);
        controls.appendChild(maxInput);
        controls.appendChild(loadBtn);
        controls.appendChild(zoomInfo);
        container.appendChild(controls);

        // Canvas
        const canvas = Utils.el('canvas', { id: 'graph-canvas' });
        container.appendChild(canvas);

        await this.loadGraph(container);
    },

    async loadGraph(container) {
        const canvas = container.querySelector('#graph-canvas');
        if (!canvas) return;

        const repo = container.querySelector('#graph-repo-filter')?.value || '';
        const max = parseInt(container.querySelector('#graph-max-nodes')?.value || '200');

        const ctx = canvas.getContext('2d');
        const rect = canvas.parentElement.getBoundingClientRect();
        canvas.width = rect.width - 48;
        canvas.height = Math.max(600, window.innerHeight - 200);

        // Loading
        ctx.fillStyle = '#1c2128';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#8b949e';
        ctx.font = '14px Inter, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('Loading graph...', canvas.width / 2, canvas.height / 2);
        ctx.textAlign = 'left';

        try {
            const data = await Api.getGraphData({ repo, max });
            this.renderForceGraph(canvas, data, container);
        } catch (err) {
            ctx.fillStyle = '#f85149';
            ctx.fillText(`Error: ${err.message}`, 20, 30);
        }
    },

    renderForceGraph(canvas, data, container) {
        const ctx = canvas.getContext('2d');
        const W = canvas.width;
        const H = canvas.height;
        const CX = W / 2;
        const CY = H / 2;

        if (!data.nodes.length) {
            ctx.fillStyle = '#1c2128';
            ctx.fillRect(0, 0, W, H);
            ctx.fillStyle = '#8b949e';
            ctx.font = '16px Inter, sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('No nodes to display', CX, CY);
            return;
        }

        const kindColors = {
            function: '#58a6ff', method: '#3fb950', class: '#bc8cff',
            file: '#d29922', module: '#db6d28', repository: '#79c0ff',
            type: '#f85149', constant: '#d29922', api_endpoint: '#3fb950',
            config: '#db6d28',
        };

        const kindRadius = {
            repository: 16, module: 10, file: 7, class: 8,
            function: 4, method: 4, type: 6, constant: 4,
        };

        const N = data.nodes.length;

        // Initialize positions in a circle
        const nodes = data.nodes.map((n, i) => {
            const angle = (2 * Math.PI * i) / N;
            const radius = Math.min(W, H) * 0.35;
            return {
                ...n,
                x: CX + radius * Math.cos(angle) + (Math.random() - 0.5) * 20,
                y: CY + radius * Math.sin(angle) + (Math.random() - 0.5) * 20,
                vx: 0, vy: 0,
                radius: kindRadius[n.kind] || 4,
            };
        });

        const nodeMap = {};
        nodes.forEach(n => nodeMap[n.id] = n);
        const links = data.links.filter(l => nodeMap[l.source] && nodeMap[l.target]);

        // Force simulation
        const repulsion = 800 + N * 3;
        const attraction = 0.003;
        const centerGravity = 0.01;
        const damping = 0.9;
        const iterations = Math.min(300, 100 + N);

        for (let iter = 0; iter < iterations; iter++) {
            const cooling = 1 - (iter / iterations) * 0.7;

            for (let i = 0; i < N; i++) {
                const ni = nodes[i];
                for (let j = i + 1; j < N; j++) {
                    const nj = nodes[j];
                    const dx = nj.x - ni.x;
                    const dy = nj.y - ni.y;
                    const distSq = dx * dx + dy * dy;
                    const dist = Math.max(1, Math.sqrt(distSq));
                    const force = Math.min(repulsion / distSq, 5) * cooling;
                    const fx = (dx / dist) * force;
                    const fy = (dy / dist) * force;
                    ni.vx -= fx; ni.vy -= fy;
                    nj.vx += fx; nj.vy += fy;
                }
            }

            for (const link of links) {
                const src = nodeMap[link.source];
                const tgt = nodeMap[link.target];
                if (!src || !tgt) continue;
                const dx = tgt.x - src.x;
                const dy = tgt.y - src.y;
                const dist = Math.sqrt(dx * dx + dy * dy);
                const force = (dist - 50) * attraction * cooling;
                const fx = (dx / Math.max(1, dist)) * force;
                const fy = (dy / Math.max(1, dist)) * force;
                src.vx += fx; src.vy += fy;
                tgt.vx -= fx; tgt.vy -= fy;
            }

            for (const node of nodes) {
                node.vx += (CX - node.x) * centerGravity * cooling;
                node.vy += (CY - node.y) * centerGravity * cooling;
                node.vx *= damping; node.vy *= damping;
                const maxV = 10 * cooling;
                node.vx = Math.max(-maxV, Math.min(maxV, node.vx));
                node.vy = Math.max(-maxV, Math.min(maxV, node.vy));
                node.x += node.vx; node.y += node.vy;
            }
        }

        // ---- ZOOM / PAN STATE ----
        let scale = 1;
        let offsetX = 0;
        let offsetY = 0;
        let isDragging = false;
        let dragStartX = 0;
        let dragStartY = 0;
        let dragOffsetX = 0;
        let dragOffsetY = 0;

        const toScreen = (x, y) => [(x + offsetX) * scale, (y + offsetY) * scale];
        const toWorld = (sx, sy) => [sx / scale - offsetX, sy / scale - offsetY];

        const draw = () => {
            ctx.fillStyle = '#1c2128';
            ctx.fillRect(0, 0, W, H);

            ctx.save();
            ctx.setTransform(scale, 0, 0, scale, offsetX * scale, offsetY * scale);

            // Edges
            ctx.lineWidth = 0.5 / scale;
            ctx.globalAlpha = 0.3;
            for (const link of links) {
                const src = nodeMap[link.source];
                const tgt = nodeMap[link.target];
                if (!src || !tgt) continue;
                ctx.strokeStyle = '#484f58';
                ctx.beginPath();
                ctx.moveTo(src.x, src.y);
                ctx.lineTo(tgt.x, tgt.y);
                ctx.stroke();
            }
            ctx.globalAlpha = 1;

            // Nodes
            for (const node of nodes) {
                const color = kindColors[node.kind] || '#8b949e';

                if (node.radius >= 8) {
                    ctx.beginPath();
                    ctx.arc(node.x, node.y, node.radius + 4, 0, Math.PI * 2);
                    ctx.fillStyle = color + '15';
                    ctx.fill();
                }

                ctx.beginPath();
                ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
                ctx.fillStyle = color;
                ctx.fill();

                // Labels — show more as you zoom in
                const labelThreshold = Math.max(3, 6 / scale);
                if (node.radius >= labelThreshold) {
                    ctx.fillStyle = '#e6edf3';
                    const fontSize = Math.max(8, Math.min(12, node.radius)) / Math.max(1, scale * 0.5);
                    ctx.font = `${fontSize}px Inter, sans-serif`;
                    ctx.fillText(Utils.truncate(node.name, 24), node.x + node.radius + 4, node.y + 3);
                }
            }

            ctx.restore();

            // Legend (screen-space, not affected by zoom)
            ctx.font = '11px Inter, sans-serif';
            let ly = 20;
            for (const [kind, color] of Object.entries(kindColors)) {
                ctx.fillStyle = color;
                ctx.beginPath();
                ctx.arc(W - 130, ly + 5, 4, 0, Math.PI * 2);
                ctx.fill();
                ctx.fillStyle = '#8b949e';
                ctx.fillText(kind, W - 120, ly + 9);
                ly += 18;
            }

            // Zoom indicator
            ctx.fillStyle = '#484f58';
            ctx.font = '11px Inter, sans-serif';
            ctx.fillText(`Zoom: ${(scale * 100).toFixed(0)}%`, 12, H - 12);
        };

        draw();

        // ---- EVENT HANDLERS ----

        // Zoom with mouse wheel
        canvas.onwheel = (e) => {
            e.preventDefault();
            const rect = canvas.getBoundingClientRect();
            const mx = e.clientX - rect.left;
            const my = e.clientY - rect.top;

            const zoomFactor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
            const newScale = Math.max(0.1, Math.min(10, scale * zoomFactor));

            // Zoom toward mouse position
            const [wx, wy] = toWorld(mx, my);
            scale = newScale;
            offsetX = mx / scale - wx;
            offsetY = my / scale - wy;

            draw();
            updateZoomInfo();
        };

        // Pan with mouse drag
        canvas.onmousedown = (e) => {
            isDragging = true;
            dragStartX = e.clientX;
            dragStartY = e.clientY;
            dragOffsetX = offsetX;
            dragOffsetY = offsetY;
            canvas.style.cursor = 'grabbing';
        };

        canvas.onmousemove = (e) => {
            if (isDragging) {
                const dx = e.clientX - dragStartX;
                const dy = e.clientY - dragStartY;
                offsetX = dragOffsetX + dx / scale;
                offsetY = dragOffsetY + dy / scale;
                draw();
            } else {
                // Hover tooltip
                const rect = canvas.getBoundingClientRect();
                const [wx, wy] = toWorld(e.clientX - rect.left, e.clientY - rect.top);
                let hovered = null;
                for (const node of nodes) {
                    const dx = wx - node.x;
                    const dy = wy - node.y;
                    if (dx * dx + dy * dy < (node.radius + 4) * (node.radius + 4)) {
                        hovered = node;
                        break;
                    }
                }
                canvas.title = hovered ? `${hovered.kind}: ${hovered.id}` : '';
                canvas.style.cursor = hovered ? 'pointer' : 'grab';
            }
        };

        canvas.onmouseup = () => {
            isDragging = false;
            canvas.style.cursor = 'grab';
        };

        canvas.onmouseleave = () => {
            isDragging = false;
            canvas.style.cursor = 'grab';
        };

        // Double-click to reset zoom
        canvas.ondblclick = () => {
            scale = 1;
            offsetX = 0;
            offsetY = 0;
            draw();
            updateZoomInfo();
        };

        canvas.style.cursor = 'grab';

        const updateZoomInfo = () => {
            const info = container?.querySelector('#zoom-info');
            if (info) info.textContent = `Zoom: ${(scale * 100).toFixed(0)}% — Scroll to zoom, drag to pan, double-click to reset`;
        };
    },
};
