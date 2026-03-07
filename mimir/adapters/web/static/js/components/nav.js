/** Navigation component. */

const Nav = {
    items: [
        { id: 'dashboard', label: 'Dashboard', icon: '📊' },
        { id: 'nodes', label: 'Nodes', icon: '🔗' },
        { id: 'graph', label: 'Graph', icon: '🕸️' },
        { id: 'search', label: 'Search', icon: '🔍' },
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
                Utils.el('span', { className: 'icon' }, item.icon),
                Utils.el('span', {}, item.label),
            );
            container.appendChild(el);
        }
    },
};
