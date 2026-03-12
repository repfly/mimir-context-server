/** Hotspots component — recently and frequently changed code. */

const Hotspots = {
    state: { topN: 30 },

    async render(container) {
        container.innerHTML = '';

        // Controls
        const controls = Utils.el('div', { className: 'filter-bar' });
        const label = Utils.el('span', { className: 'filter-label' }, 'Show top:');
        const topSelect = Utils.el('select', { id: 'hotspot-top' });
        for (const n of [10, 20, 30, 50, 100]) {
            topSelect.innerHTML += `<option value="${n}" ${n === this.state.topN ? 'selected' : ''}>${n}</option>`;
        }
        topSelect.onchange = () => { this.state.topN = parseInt(topSelect.value); this._loadData(container); };
        controls.append(label, topSelect);
        container.appendChild(controls);

        const results = Utils.el('div', { id: 'hotspot-results' });
        container.appendChild(results);

        await this._loadData(container);
    },

    async _loadData(container) {
        const results = container.querySelector('#hotspot-results');
        if (!results) return;
        results.innerHTML = '<div class="loading">Loading hotspots</div>';

        try {
            const data = await Api.getHotspots(this.state.topN);
            results.innerHTML = '';

            if (data.length === 0) {
                results.innerHTML = '<div class="card"><p style="color:var(--text-secondary)">No hotspots found. Code needs git metadata (modification counts) to detect hotspots.</p></div>';
                return;
            }

            const maxScore = data.length > 0 ? data[0].score : 1;

            const card = Utils.el('div', { className: 'card' });
            card.appendChild(Utils.el('div', { className: 'card-header' },
                `${data.length} hotspots — ranked by recency + change frequency`));

            const table = Utils.el('table', { className: 'data-table' });
            table.innerHTML = '<thead><tr><th>#</th><th>Symbol</th><th>Kind</th><th>Repo</th><th>Changes</th><th>Score</th><th></th></tr></thead>';
            const tbody = Utils.el('tbody');

            for (let i = 0; i < data.length; i++) {
                const item = data[i];
                const n = item.node;
                const barPct = (item.score / maxScore * 100).toFixed(0);

                const row = Utils.el('tr', {
                    onclick: () => { NodeBrowser.state.selectedNodeId = n.id; App.navigate('nodes'); },
                    style: 'cursor:pointer',
                },
                    Utils.el('td', { className: 'hotspot-rank' }, `${i + 1}`),
                    Utils.el('td', { className: 'mono' }, Utils.truncate(n.name, 35)),
                    Utils.el('td', {}, Utils.kindBadge(n.kind)),
                    Utils.el('td', {}, n.repo),
                    Utils.el('td', {}, Utils.formatNumber(n.modification_count)),
                    Utils.el('td', {},
                        Utils.el('span', { className: 'hotspot-score-value' }, item.score.toFixed(3)),
                    ),
                    Utils.el('td', { className: 'hotspot-bar-cell' },
                        Utils.el('div', { className: 'hotspot-bar-track' },
                            Utils.el('div', { className: 'hotspot-bar-fill', style: `width:${barPct}%` }),
                        ),
                    ),
                );
                tbody.appendChild(row);
            }
            table.appendChild(tbody);
            card.appendChild(table);
            results.appendChild(card);

        } catch (err) {
            results.innerHTML = `<div class="card"><p style="color:var(--accent-red)">Failed to load: ${Utils.escapeHtml(err.message)}</p></div>`;
        }
    },
};
