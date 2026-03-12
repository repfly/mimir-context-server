/** API client module for Mimir Inspector. */

const Api = {
    BASE: '',

    async get(path) {
        const response = await fetch(`${this.BASE}${path}`);
        if (!response.ok) {
            throw new Error(`API error ${response.status}: ${await response.text()}`);
        }
        return response.json();
    },

    async getStats() {
        return this.get('/api/stats');
    },

    async getNodes({ kind, repo, limit = 100, offset = 0 } = {}) {
        const params = new URLSearchParams();
        if (kind) params.set('kind', kind);
        if (repo) params.set('repo', repo);
        params.set('limit', limit.toString());
        params.set('offset', offset.toString());
        return this.get(`/api/nodes?${params}`);
    },

    async getNode(nodeId) {
        // Use query param instead of path to avoid URL encoding issues with :: and /
        const params = new URLSearchParams({ id: nodeId });
        return this.get(`/api/node-detail?${params}`);
    },

    async getGraphData({ repo, max = 200 } = {}) {
        const params = new URLSearchParams();
        if (repo) params.set('repo', repo);
        params.set('max', max.toString());
        return this.get(`/api/graph-data?${params}`);
    },

    async search(query, { budget = 4000, repo } = {}) {
        const params = new URLSearchParams({ q: query, budget: budget.toString() });
        if (repo) params.set('repo', repo);
        return this.get(`/api/search?${params}`);
    },

    async getHotspots(topN = 20) {
        return this.get(`/api/hotspots?top=${topN}`);
    },

    async getQuality({ repos, threshold = 0.3, topN = 50 } = {}) {
        const params = new URLSearchParams();
        if (repos) params.set('repos', repos);
        params.set('threshold', threshold.toString());
        params.set('top_n', topN.toString());
        return this.get(`/api/quality?${params}`);
    },
};
