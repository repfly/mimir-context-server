/** App entry point — router and initialisation. */

const App = {
    currentView: 'dashboard',

    views: {
        dashboard: { container: 'dashboard-view', render: () => Dashboard.render(document.getElementById('dashboard-view')) },
        nodes: { container: 'nodes-view', render: () => NodeBrowser.render(document.getElementById('nodes-view')) },
        graph: { container: 'graph-view', render: () => GraphViz.render(document.getElementById('graph-view')) },
        search: { container: 'search-view', render: () => Search.render(document.getElementById('search-view')) },
    },

    navigate(viewId) {
        this.currentView = viewId;

        // Hide all views
        for (const view of Object.values(this.views)) {
            document.getElementById(view.container).classList.add('hidden');
        }

        // Show target view
        const target = this.views[viewId];
        if (target) {
            document.getElementById(target.container).classList.remove('hidden');
            target.render();
        }

        // Update nav
        Nav.render(document.getElementById('nav-container'));
    },

    init() {
        Nav.render(document.getElementById('nav-container'));
        this.navigate('dashboard');
    },
};

// Boot
document.addEventListener('DOMContentLoaded', () => App.init());
