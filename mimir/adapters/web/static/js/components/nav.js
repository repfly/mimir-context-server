/** Navigation component. */

const Nav = {
    items: [
        { id: 'dashboard', label: 'Overview' },
        { id: 'search', label: 'Search' },
        { id: 'nodes', label: 'Nodes' },
        { id: 'graph', label: 'Graph' },
        { id: 'quality', label: 'Quality' },
        { id: 'hotspots', label: 'Hotspots' },
    ],

    render(container) {
        container.innerHTML = '';

        // Logo
        const logo = Utils.el('div', { className: 'nav-logo' });
        logo.innerHTML = '<h1>Mimir</h1><span>Context Engine Inspector</span>';
        container.appendChild(logo);

        // Nav items
        for (const item of this.items) {
            const el = Utils.el('div', {
                className: `nav-item ${item.id === App.currentView ? 'active' : ''}`,
                onclick: () => App.navigate(item.id),
            },
                Utils.el('span', {}, item.label),
            );
            container.appendChild(el);
        }
    },
};
