/** Quality analysis component — graph health overview and gap detection. */

const Quality = {
    state: { threshold: 0.3, topN: 50, repos: '' },

    async render(container) {
        container.innerHTML = '';

        // Controls
        const controls = Utils.el('div', { className: 'filter-bar' });

        const threshLabel = Utils.el('span', { className: 'filter-label' }, 'Threshold:');
        const threshSelect = Utils.el('select', { id: 'quality-threshold' });
        for (const t of [0.2, 0.3, 0.4, 0.5, 0.6]) {
            threshSelect.innerHTML += `<option value="${t}" ${t === this.state.threshold ? 'selected' : ''}>${t}</option>`;
        }
        threshSelect.onchange = () => { this.state.threshold = parseFloat(threshSelect.value); this._loadData(container); };

        const topLabel = Utils.el('span', { className: 'filter-label' }, 'Max gaps:');
        const topSelect = Utils.el('select', { id: 'quality-top' });
        for (const n of [20, 50, 100]) {
            topSelect.innerHTML += `<option value="${n}" ${n === this.state.topN ? 'selected' : ''}>${n}</option>`;
        }
        topSelect.onchange = () => { this.state.topN = parseInt(topSelect.value); this._loadData(container); };

        controls.append(threshLabel, threshSelect, topLabel, topSelect);
        container.appendChild(controls);

        // Results area
        const results = Utils.el('div', { id: 'quality-results' });
        container.appendChild(results);

        await this._loadData(container);
    },

    async _loadData(container) {
        const results = container.querySelector('#quality-results');
        if (!results) return;
        results.innerHTML = '<div class="loading">Analyzing graph quality</div>';

        try {
            const data = await Api.getQuality({
                repos: this.state.repos || undefined,
                threshold: this.state.threshold,
                topN: this.state.topN,
            });

            results.innerHTML = '';

            // ── Summary cards ──
            const grid = Utils.el('div', { className: 'stats-grid' });
            grid.appendChild(Utils.el('div', { className: 'stat-card' },
                Utils.el('div', { className: 'stat-value' }, Utils.formatNumber(data.scored_nodes)),
                Utils.el('div', { className: 'stat-label' }, 'Scored Nodes'),
            ));
            grid.appendChild(Utils.el('div', { className: 'stat-card' },
                Utils.el('div', { className: 'stat-value' }, data.avg_quality.toFixed(3)),
                Utils.el('div', { className: 'stat-label' }, 'Avg Quality'),
            ));
            grid.appendChild(Utils.el('div', { className: `stat-card ${data.gap_count > 0 ? 'stat-card-warn' : 'stat-card-ok'}` },
                Utils.el('div', { className: 'stat-value' }, Utils.formatNumber(data.gap_count)),
                Utils.el('div', { className: 'stat-label' }, 'Gaps Found'),
            ));
            results.appendChild(grid);

            // ── Distribution ──
            const dist = data.quality_distribution || {};
            const total = (dist.good || 0) + (dist.moderate || 0) + (dist.poor || 0);

            if (total > 0) {
                const distCard = Utils.el('div', { className: 'card' });
                distCard.appendChild(Utils.el('div', { className: 'card-header' }, 'Quality Distribution'));

                const bar = Utils.el('div', { className: 'quality-bar quality-bar-lg' });
                const goodPct = ((dist.good || 0) / total * 100);
                const modPct = ((dist.moderate || 0) / total * 100);
                const poorPct = ((dist.poor || 0) / total * 100);

                if (goodPct > 0) bar.appendChild(Utils.el('div', {
                    className: 'quality-segment quality-good',
                    style: `width:${goodPct}%`,
                    title: `Good: ${dist.good || 0} (${goodPct.toFixed(1)}%)`,
                }));
                if (modPct > 0) bar.appendChild(Utils.el('div', {
                    className: 'quality-segment quality-moderate',
                    style: `width:${modPct}%`,
                    title: `Moderate: ${dist.moderate || 0} (${modPct.toFixed(1)}%)`,
                }));
                if (poorPct > 0) bar.appendChild(Utils.el('div', {
                    className: 'quality-segment quality-poor',
                    style: `width:${poorPct}%`,
                    title: `Poor: ${dist.poor || 0} (${poorPct.toFixed(1)}%)`,
                }));
                distCard.appendChild(bar);

                const legend = Utils.el('div', { className: 'quality-legend quality-legend-lg' });
                const items = [
                    ['good', dist.good || 0, goodPct, 'Score >= 0.7'],
                    ['moderate', dist.moderate || 0, modPct, 'Score 0.4 - 0.7'],
                    ['poor', dist.poor || 0, poorPct, 'Score < 0.4'],
                ];
                for (const [cls, count, pct, desc] of items) {
                    legend.appendChild(Utils.el('div', { className: 'quality-legend-block' },
                        Utils.el('div', { className: 'quality-legend-row' },
                            Utils.el('span', { className: `quality-dot quality-${cls}` }),
                            Utils.el('span', { className: 'quality-legend-label' }, cls.charAt(0).toUpperCase() + cls.slice(1)),
                        ),
                        Utils.el('div', { className: 'quality-legend-stats' },
                            `${Utils.formatNumber(count)} nodes (${pct.toFixed(1)}%) — ${desc}`),
                    ));
                }
                distCard.appendChild(legend);
                results.appendChild(distCard);
            }

            // ── Gap table ──
            if (data.gaps && data.gaps.length > 0) {
                const gapCard = Utils.el('div', { className: 'card' });
                gapCard.appendChild(Utils.el('div', { className: 'card-header' },
                    `Gaps (${data.gaps.length} of ${data.gap_count} below threshold ${this.state.threshold})`));

                const table = Utils.el('table', { className: 'data-table' });
                table.innerHTML = '<thead><tr><th>Node</th><th>Kind</th><th>Repo</th><th>Score</th><th>Reason</th></tr></thead>';
                const tbody = Utils.el('tbody');

                for (const gap of data.gaps) {
                    const scoreClass = gap.quality_score < 0.15 ? 'quality-score-poor' :
                        gap.quality_score < 0.25 ? 'quality-score-moderate' : 'quality-score-moderate';

                    const row = Utils.el('tr', {
                        onclick: () => { NodeBrowser.state.selectedNodeId = gap.node_id; App.navigate('nodes'); },
                        style: 'cursor:pointer',
                    },
                        Utils.el('td', { className: 'mono' }, Utils.truncate(gap.node_name, 35)),
                        Utils.el('td', {}, Utils.kindBadge(gap.node_kind)),
                        Utils.el('td', {}, gap.repo),
                        Utils.el('td', {},
                            Utils.el('span', { className: `quality-score-pill ${scoreClass}` }, gap.quality_score.toFixed(3)),
                        ),
                        Utils.el('td', { className: 'gap-reason-cell' }, gap.reason),
                    );
                    tbody.appendChild(row);
                }
                table.appendChild(tbody);
                gapCard.appendChild(table);
                results.appendChild(gapCard);
            } else if (data.gap_count === 0) {
                const okCard = Utils.el('div', { className: 'card card-ok' });
                okCard.appendChild(Utils.el('div', { className: 'card-ok-text' },
                    'No gaps detected — all nodes meet the quality threshold.'));
                results.appendChild(okCard);
            }

        } catch (err) {
            results.innerHTML = `<div class="card"><p style="color:var(--accent-red)">Failed to load: ${Utils.escapeHtml(err.message)}</p></div>`;
        }
    },
};
