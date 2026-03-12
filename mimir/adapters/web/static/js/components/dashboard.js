/** Dashboard component — overview with stats, quality health, and hotspots. */

const Dashboard = {
    async render(container) {
        container.innerHTML = '<div class="loading">Loading overview</div>';

        try {
            const [stats, quality, hotspots] = await Promise.all([
                Api.getStats(),
                Api.getQuality({ threshold: 0.3, topN: 5 }).catch(() => null),
                Api.getHotspots(5).catch(() => null),
            ]);

            container.innerHTML = '';

            // ── Stat cards ──
            const grid = Utils.el('div', { className: 'stats-grid' });
            const cards = [
                { label: 'Total Nodes', value: stats.total_nodes },
                { label: 'Total Edges', value: stats.total_edges },
                { label: 'Repositories', value: stats.repos.length },
            ];

            if (quality) {
                cards.push({ label: 'Avg Quality', value: quality.avg_quality.toFixed(2) });
                cards.push({ label: 'Gaps Detected', value: quality.gap_count, accent: quality.gap_count > 0 ? 'warn' : 'ok' });
            }

            for (const c of cards) {
                const card = Utils.el('div', { className: `stat-card ${c.accent === 'warn' ? 'stat-card-warn' : c.accent === 'ok' ? 'stat-card-ok' : ''}` },
                    Utils.el('div', { className: 'stat-value' }, typeof c.value === 'number' ? Utils.formatNumber(c.value) : c.value),
                    Utils.el('div', { className: 'stat-label' }, c.label),
                );
                grid.appendChild(card);
            }
            container.appendChild(grid);

            // ── Two-column layout for details ──
            const columns = Utils.el('div', { className: 'dashboard-columns' });

            // Left column: Quality health + Nodes by Kind
            const leftCol = Utils.el('div', { className: 'dashboard-col' });

            // Quality health card
            if (quality) {
                const qCard = Utils.el('div', { className: 'card' });
                const qHeader = Utils.el('div', { className: 'card-header-row' },
                    Utils.el('span', { className: 'card-header' }, 'Graph Health'),
                    Utils.el('button', {
                        className: 'btn btn-secondary btn-sm',
                        onclick: () => App.navigate('quality'),
                    }, 'View Details'),
                );
                qCard.appendChild(qHeader);

                // Distribution bar
                const dist = quality.quality_distribution || {};
                const total = (dist.good || 0) + (dist.moderate || 0) + (dist.poor || 0);
                if (total > 0) {
                    const bar = Utils.el('div', { className: 'quality-bar' });
                    const goodPct = ((dist.good || 0) / total * 100);
                    const modPct = ((dist.moderate || 0) / total * 100);
                    const poorPct = ((dist.poor || 0) / total * 100);

                    if (goodPct > 0) bar.appendChild(Utils.el('div', { className: 'quality-segment quality-good', style: `width:${goodPct}%` }));
                    if (modPct > 0) bar.appendChild(Utils.el('div', { className: 'quality-segment quality-moderate', style: `width:${modPct}%` }));
                    if (poorPct > 0) bar.appendChild(Utils.el('div', { className: 'quality-segment quality-poor', style: `width:${poorPct}%` }));
                    qCard.appendChild(bar);

                    const legend = Utils.el('div', { className: 'quality-legend' },
                        Utils.el('span', { className: 'quality-legend-item' },
                            Utils.el('span', { className: 'quality-dot quality-good' }), `Good: ${dist.good || 0}`),
                        Utils.el('span', { className: 'quality-legend-item' },
                            Utils.el('span', { className: 'quality-dot quality-moderate' }), `Moderate: ${dist.moderate || 0}`),
                        Utils.el('span', { className: 'quality-legend-item' },
                            Utils.el('span', { className: 'quality-dot quality-poor' }), `Poor: ${dist.poor || 0}`),
                    );
                    qCard.appendChild(legend);
                }

                // Top gaps preview
                if (quality.gaps && quality.gaps.length > 0) {
                    qCard.appendChild(Utils.el('div', { className: 'card-subheader' }, 'Top Gaps'));
                    for (const gap of quality.gaps.slice(0, 3)) {
                        const row = Utils.el('div', { className: 'gap-preview-row' },
                            Utils.el('span', { className: 'mono gap-name' }, Utils.truncate(gap.node_name, 30)),
                            Utils.el('span', { className: 'quality-score-pill quality-score-' + this._scoreBucket(gap.quality_score) },
                                gap.quality_score.toFixed(2)),
                            Utils.el('span', { className: 'gap-reason' }, Utils.truncate(gap.reason, 40)),
                        );
                        qCard.appendChild(row);
                    }
                    if (quality.gap_count > 3) {
                        qCard.appendChild(Utils.el('div', { className: 'gap-more' },
                            `+ ${quality.gap_count - 3} more gaps`));
                    }
                }

                leftCol.appendChild(qCard);
            }

            // Nodes by kind
            const kindCard = Utils.el('div', { className: 'card' });
            kindCard.appendChild(Utils.el('div', { className: 'card-header' }, 'Nodes by Kind'));
            const kindEntries = Object.entries(stats.nodes_by_kind || {}).sort((a, b) => b[1] - a[1]);
            const maxCount = kindEntries.length > 0 ? kindEntries[0][1] : 1;
            for (const [kind, count] of kindEntries) {
                const pct = (count / maxCount * 100).toFixed(0);
                const row = Utils.el('div', { className: 'bar-row' },
                    Utils.el('div', { className: 'bar-label' }, Utils.kindBadge(kind)),
                    Utils.el('div', { className: 'bar-track' },
                        Utils.el('div', { className: 'bar-fill bar-fill-' + kind, style: `width:${pct}%` }),
                    ),
                    Utils.el('div', { className: 'bar-value' }, Utils.formatNumber(count)),
                );
                kindCard.appendChild(row);
            }
            leftCol.appendChild(kindCard);

            // Right column: Hotspots + Repos + Edges
            const rightCol = Utils.el('div', { className: 'dashboard-col' });

            // Hotspots card
            if (hotspots && hotspots.length > 0) {
                const hCard = Utils.el('div', { className: 'card' });
                const hHeader = Utils.el('div', { className: 'card-header-row' },
                    Utils.el('span', { className: 'card-header' }, 'Active Hotspots'),
                    Utils.el('button', {
                        className: 'btn btn-secondary btn-sm',
                        onclick: () => App.navigate('hotspots'),
                    }, 'View All'),
                );
                hCard.appendChild(hHeader);

                for (const item of hotspots) {
                    const n = item.node;
                    const row = Utils.el('div', { className: 'hotspot-row' },
                        Utils.el('div', { className: 'hotspot-info' },
                            Utils.el('span', { className: 'mono hotspot-name' }, Utils.truncate(n.name, 28)),
                            Utils.el('span', { className: 'hotspot-meta' },
                                `${n.modification_count} changes`),
                        ),
                        Utils.el('div', { className: 'hotspot-score' },
                            Utils.el('div', { className: 'hotspot-bar-track' },
                                Utils.el('div', { className: 'hotspot-bar-fill', style: `width:${(item.score * 100).toFixed(0)}%` }),
                            ),
                            Utils.el('span', { className: 'hotspot-value' }, item.score.toFixed(2)),
                        ),
                    );
                    hCard.appendChild(row);
                }
                rightCol.appendChild(hCard);
            }

            // Repos
            const repoCard = Utils.el('div', { className: 'card' });
            repoCard.appendChild(Utils.el('div', { className: 'card-header' }, 'Repositories'));
            for (const [repo, count] of Object.entries(stats.nodes_by_repo || {}).sort((a, b) => b[1] - a[1])) {
                const row = Utils.el('div', { className: 'repo-row' },
                    Utils.el('span', { className: 'mono' }, repo),
                    Utils.el('span', { className: 'repo-count' }, Utils.formatNumber(count) + ' nodes'),
                );
                repoCard.appendChild(row);
            }
            rightCol.appendChild(repoCard);

            // Edges by kind
            if (stats.edges_by_kind) {
                const edgeCard = Utils.el('div', { className: 'card' });
                edgeCard.appendChild(Utils.el('div', { className: 'card-header' }, 'Edges by Kind'));
                const edgeEntries = Object.entries(stats.edges_by_kind).sort((a, b) => b[1] - a[1]);
                const maxEdge = edgeEntries.length > 0 ? edgeEntries[0][1] : 1;
                for (const [kind, count] of edgeEntries) {
                    const pct = (count / maxEdge * 100).toFixed(0);
                    const row = Utils.el('div', { className: 'bar-row' },
                        Utils.el('div', { className: 'bar-label mono' }, kind),
                        Utils.el('div', { className: 'bar-track' },
                            Utils.el('div', { className: 'bar-fill bar-fill-edge', style: `width:${pct}%` }),
                        ),
                        Utils.el('div', { className: 'bar-value' }, Utils.formatNumber(count)),
                    );
                    edgeCard.appendChild(row);
                }
                rightCol.appendChild(edgeCard);
            }

            columns.appendChild(leftCol);
            columns.appendChild(rightCol);
            container.appendChild(columns);

        } catch (err) {
            container.innerHTML = `<div class="card"><p style="color:var(--accent-red)">Failed to load: ${Utils.escapeHtml(err.message)}</p></div>`;
        }
    },

    _scoreBucket(score) {
        if (score >= 0.7) return 'good';
        if (score >= 0.4) return 'moderate';
        return 'poor';
    },
};
