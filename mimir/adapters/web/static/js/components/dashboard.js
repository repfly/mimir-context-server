/** Dashboard component — overview statistics. */

const Dashboard = {
    async render(container) {
        container.innerHTML = '<div class="loading">Loading stats</div>';

        try {
            const stats = await Api.getStats();
            container.innerHTML = '';

            // Stat cards
            const grid = Utils.el('div', { className: 'stats-grid' });
            const cards = [
                { label: 'Total Nodes', value: stats.total_nodes, color: 'blue' },
                { label: 'Total Edges', value: stats.total_edges, color: 'purple' },
                { label: 'Repositories', value: stats.repos.length, color: 'green' },
            ];
            for (const c of cards) {
                grid.appendChild(Utils.el('div', { className: 'stat-card' },
                    Utils.el('div', { className: 'stat-value' }, Utils.formatNumber(c.value)),
                    Utils.el('div', { className: 'stat-label' }, c.label),
                ));
            }
            container.appendChild(grid);

            // Nodes by kind table
            const kindCard = Utils.el('div', { className: 'card' });
            kindCard.appendChild(Utils.el('div', { className: 'card-header' }, 'Nodes by Kind'));
            const kindTable = Utils.el('table', { className: 'data-table' });
            kindTable.innerHTML = '<thead><tr><th>Kind</th><th>Count</th></tr></thead>';
            const tbody = Utils.el('tbody');
            for (const [kind, count] of Object.entries(stats.nodes_by_kind || {}).sort((a, b) => b[1] - a[1])) {
                tbody.appendChild(Utils.el('tr', {},
                    Utils.el('td', {}, Utils.kindBadge(kind)),
                    Utils.el('td', {}, Utils.formatNumber(count)),
                ));
            }
            kindTable.appendChild(tbody);
            kindCard.appendChild(kindTable);
            container.appendChild(kindCard);

            // Repos table
            const repoCard = Utils.el('div', { className: 'card' });
            repoCard.appendChild(Utils.el('div', { className: 'card-header' }, 'Repositories'));
            const repoTable = Utils.el('table', { className: 'data-table' });
            repoTable.innerHTML = '<thead><tr><th>Repository</th><th>Nodes</th></tr></thead>';
            const rtbody = Utils.el('tbody');
            for (const [repo, count] of Object.entries(stats.nodes_by_repo || {}).sort((a, b) => b[1] - a[1])) {
                rtbody.appendChild(Utils.el('tr', {},
                    Utils.el('td', { className: 'mono' }, repo),
                    Utils.el('td', {}, Utils.formatNumber(count)),
                ));
            }
            repoTable.appendChild(rtbody);
            repoCard.appendChild(repoTable);
            container.appendChild(repoCard);

            // Edges by kind
            if (stats.edges_by_kind) {
                const edgeCard = Utils.el('div', { className: 'card' });
                edgeCard.appendChild(Utils.el('div', { className: 'card-header' }, 'Edges by Kind'));
                const edgeTable = Utils.el('table', { className: 'data-table' });
                edgeTable.innerHTML = '<thead><tr><th>Kind</th><th>Count</th></tr></thead>';
                const etbody = Utils.el('tbody');
                for (const [kind, count] of Object.entries(stats.edges_by_kind).sort((a, b) => b[1] - a[1])) {
                    etbody.appendChild(Utils.el('tr', {},
                        Utils.el('td', { className: 'mono' }, kind),
                        Utils.el('td', {}, Utils.formatNumber(count)),
                    ));
                }
                edgeTable.appendChild(etbody);
                edgeCard.appendChild(edgeTable);
                container.appendChild(edgeCard);
            }
        } catch (err) {
            container.innerHTML = `<div class="card"><p style="color:var(--accent-red)">Failed to load: ${Utils.escapeHtml(err.message)}</p></div>`;
        }
    },
};
