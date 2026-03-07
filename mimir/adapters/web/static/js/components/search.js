/** Search tester component. */

const Search = {
    async render(container) {
        container.innerHTML = '';

        // Search bar
        const bar = Utils.el('div', { className: 'search-bar' });
        const input = Utils.el('input', {
            id: 'search-input',
            type: 'text',
            placeholder: 'Enter a natural language query…',
        });
        input.onkeydown = (e) => { if (e.key === 'Enter') this.doSearch(container); };

        const btn = Utils.el('button', {
            className: 'btn btn-primary',
            onclick: () => this.doSearch(container),
        }, 'Search');

        bar.appendChild(input);
        bar.appendChild(btn);
        container.appendChild(bar);

        // Budget control
        const controls = Utils.el('div', { className: 'filter-bar' });
        const budgetLabel = Utils.el('span', { style: 'color:var(--text-secondary);font-size:13px;line-height:36px' }, 'Budget:');
        const budgetSelect = Utils.el('select', { id: 'search-budget' });
        for (const b of [2000, 4000, 8000, 16000]) {
            budgetSelect.innerHTML += `<option value="${b}" ${b === 4000 ? 'selected' : ''}>${b} tokens</option>`;
        }
        controls.appendChild(budgetLabel);
        controls.appendChild(budgetSelect);
        container.appendChild(controls);

        // Results area
        container.appendChild(Utils.el('div', { id: 'search-results' }));
    },

    async doSearch(container) {
        const query = container.querySelector('#search-input')?.value;
        if (!query) return;

        const budget = parseInt(container.querySelector('#search-budget')?.value || '4000');
        const results = container.querySelector('#search-results');
        results.innerHTML = '<div class="loading">Searching</div>';

        try {
            const data = await Api.search(query, { budget });

            results.innerHTML = '';

            // Summary card
            const summaryCard = Utils.el('div', { className: 'card' });
            summaryCard.appendChild(Utils.el('div', { className: 'card-header' }, 'Results'));
            summaryCard.appendChild(Utils.el('div', { style: 'margin-bottom:8px' }, data.summary));
            summaryCard.appendChild(Utils.el('div', { style: 'font-size:12px;color:var(--text-secondary)' },
                `Tokens: ${data.token_count} | Repos: ${data.repos.join(', ')}`));
            results.appendChild(summaryCard);

            // Nodes
            if (data.nodes.length > 0) {
                const nodesCard = Utils.el('div', { className: 'card' });
                nodesCard.appendChild(Utils.el('div', { className: 'card-header' }, `${data.nodes.length} nodes`));

                const table = Utils.el('table', { className: 'data-table' });
                table.innerHTML = '<thead><tr><th>Name</th><th>Kind</th><th>Path</th></tr></thead>';
                const tbody = Utils.el('tbody');
                for (const node of data.nodes) {
                    tbody.appendChild(Utils.el('tr', {},
                        Utils.el('td', { className: 'mono' }, node.name),
                        Utils.el('td', {}, Utils.kindBadge(node.kind)),
                        Utils.el('td', { className: 'mono' }, Utils.truncate(node.path || '', 50)),
                    ));
                }
                table.appendChild(tbody);
                nodesCard.appendChild(table);
                results.appendChild(nodesCard);
            }

            // Formatted output
            if (data.formatted) {
                const fmtCard = Utils.el('div', { className: 'card' });
                fmtCard.appendChild(Utils.el('div', { className: 'card-header' }, 'LLM Context Output'));
                fmtCard.appendChild(Utils.el('pre', { className: 'code-block' }, data.formatted));
                results.appendChild(fmtCard);
            }
        } catch (err) {
            results.innerHTML = `<div class="card"><p style="color:var(--accent-red)">Search failed: ${Utils.escapeHtml(err.message)}</p></div>`;
        }
    },
};
