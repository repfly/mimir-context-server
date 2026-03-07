/** Node browser component — paginated, filterable node list with detail view. */

const NodeBrowser = {
    state: { kind: '', repo: '', offset: 0, limit: 50, selectedNodeId: null },

    async render(container) {
        container.innerHTML = '';

        // Filter bar
        const filterBar = Utils.el('div', { className: 'filter-bar' });

        const kindSelect = Utils.el('select', { id: 'filter-kind' });
        kindSelect.innerHTML = '<option value="">All Kinds</option>';
        const kinds = ['function', 'method', 'class', 'file', 'module', 'repository', 'type', 'constant', 'api_endpoint', 'config'];
        for (const k of kinds) {
            kindSelect.innerHTML += `<option value="${k}" ${this.state.kind === k ? 'selected' : ''}>${k}</option>`;
        }
        kindSelect.onchange = () => { this.state.kind = kindSelect.value; this.state.offset = 0; this.render(container); };
        filterBar.appendChild(kindSelect);

        // Repo filter — populated dynamically
        const repoSelect = Utils.el('select', { id: 'filter-repo' });
        repoSelect.innerHTML = '<option value="">All Repos</option>';
        try {
            const stats = await Api.getStats();
            for (const repo of stats.repos || []) {
                repoSelect.innerHTML += `<option value="${repo}" ${this.state.repo === repo ? 'selected' : ''}>${repo}</option>`;
            }
        } catch (_) { }
        repoSelect.onchange = () => { this.state.repo = repoSelect.value; this.state.offset = 0; this.render(container); };
        filterBar.appendChild(repoSelect);
        container.appendChild(filterBar);

        // Load nodes
        const listContainer = Utils.el('div', { className: 'card' });
        listContainer.innerHTML = '<div class="loading">Loading nodes</div>';
        container.appendChild(listContainer);

        try {
            const data = await Api.getNodes({
                kind: this.state.kind,
                repo: this.state.repo,
                limit: this.state.limit,
                offset: this.state.offset,
            });

            listContainer.innerHTML = '';
            listContainer.appendChild(Utils.el('div', { className: 'card-header' }, `${Utils.formatNumber(data.total)} nodes`));

            const table = Utils.el('table', { className: 'data-table' });
            table.innerHTML = '<thead><tr><th>Name</th><th>Kind</th><th>Repo</th><th>Path</th></tr></thead>';
            const tbody = Utils.el('tbody');

            for (const node of data.nodes) {
                const row = Utils.el('tr', {
                    onclick: () => this.showDetail(container, node.id),
                    style: 'cursor:pointer',
                },
                    Utils.el('td', { className: 'mono' }, Utils.truncate(node.name, 40)),
                    Utils.el('td', {}, Utils.kindBadge(node.kind)),
                    Utils.el('td', {}, node.repo),
                    Utils.el('td', { className: 'mono' }, Utils.truncate(node.path || '', 50)),
                );
                tbody.appendChild(row);
            }
            table.appendChild(tbody);
            listContainer.appendChild(table);

            // Pagination
            const pag = Utils.el('div', { className: 'pagination' });
            const prevBtn = Utils.el('button', {
                onclick: () => { this.state.offset = Math.max(0, this.state.offset - this.state.limit); this.render(container); },
            }, '← Prev');
            if (this.state.offset === 0) prevBtn.disabled = true;

            const nextBtn = Utils.el('button', {
                onclick: () => { this.state.offset += this.state.limit; this.render(container); },
            }, 'Next →');
            if (this.state.offset + this.state.limit >= data.total) nextBtn.disabled = true;

            const info = Utils.el('span', {},
                `${this.state.offset + 1}–${Math.min(this.state.offset + this.state.limit, data.total)} of ${data.total}`
            );
            pag.appendChild(prevBtn);
            pag.appendChild(info);
            pag.appendChild(nextBtn);
            listContainer.appendChild(pag);
        } catch (err) {
            listContainer.innerHTML = `<p style="color:var(--accent-red)">Error: ${Utils.escapeHtml(err.message)}</p>`;
        }

        // Detail panel
        const detail = Utils.el('div', { id: 'node-detail-panel' });
        container.appendChild(detail);

        if (this.state.selectedNodeId) {
            await this.showDetail(container, this.state.selectedNodeId);
        }
    },

    async showDetail(container, nodeId) {
        this.state.selectedNodeId = nodeId;
        const panel = container.querySelector('#node-detail-panel') || Utils.el('div', { id: 'node-detail-panel' });
        panel.innerHTML = '<div class="loading">Loading node</div>';

        try {
            const data = await Api.getNode(nodeId);
            const node = data.node;
            panel.innerHTML = '';

            const card = Utils.el('div', { className: 'card' });
            card.appendChild(Utils.el('div', { className: 'card-header' }, `Node: ${node.name}`));

            const grid = Utils.el('div', { className: 'node-detail' });
            const fields = [
                ['ID', node.id],
                ['Kind', node.kind],
                ['Repo', node.repo],
                ['Path', node.path || '—'],
                ['Lines', node.start_line ? `${node.start_line}–${node.end_line}` : '—'],
                ['Signature', node.signature || '—'],
                ['Embedding', node.has_embedding ? `${node.embedding_dim}-dim vector` : 'None'],
                ['Modified', node.last_modified || '—'],
                ['Changes', node.modification_count?.toString() || '0'],
                ['Retrieved', node.retrieval_count?.toString() || '0'],
            ];
            for (const [label, value] of fields) {
                grid.appendChild(Utils.el('div', { className: 'detail-label' }, label));
                grid.appendChild(Utils.el('div', { className: 'detail-value mono' }, value));
            }
            card.appendChild(grid);

            // Summary
            if (node.summary) {
                card.appendChild(Utils.el('div', { className: 'card-header', style: 'margin-top:16px' }, 'Summary'));
                card.appendChild(Utils.el('div', { style: 'font-size:13px;color:var(--text-secondary)' }, node.summary));
            }

            // Code
            if (node.raw_code) {
                card.appendChild(Utils.el('div', { className: 'card-header', style: 'margin-top:16px' }, 'Code'));
                const code = Utils.el('pre', { className: 'code-block' }, node.raw_code);
                card.appendChild(code);
            }

            // Edges
            if (data.outgoing_edges.length > 0) {
                card.appendChild(Utils.el('div', { className: 'card-header', style: 'margin-top:16px' },
                    `Outgoing Edges (${data.outgoing_edges.length})`));
                for (const e of data.outgoing_edges.slice(0, 20)) {
                    card.appendChild(Utils.el('div', { style: 'font-size:12px;font-family:var(--font-mono);padding:4px 0' },
                        `→ ${e.kind} → ${e.target}`));
                }
            }
            if (data.incoming_edges.length > 0) {
                card.appendChild(Utils.el('div', { className: 'card-header', style: 'margin-top:16px' },
                    `Incoming Edges (${data.incoming_edges.length})`));
                for (const e of data.incoming_edges.slice(0, 20)) {
                    card.appendChild(Utils.el('div', { style: 'font-size:12px;font-family:var(--font-mono);padding:4px 0' },
                        `← ${e.kind} ← ${e.source}`));
                }
            }

            panel.appendChild(card);
        } catch (err) {
            panel.innerHTML = `<p style="color:var(--accent-red)">Error: ${Utils.escapeHtml(err.message)}</p>`;
        }
    },
};
