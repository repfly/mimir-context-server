/** Utility functions for the Mimir Inspector. */

const Utils = {
    /** Format large numbers with comma separators. */
    formatNumber(n) {
        return n.toLocaleString();
    },

    /** Truncate text to a max length. */
    truncate(text, maxLen = 80) {
        if (!text || text.length <= maxLen) return text || '';
        return text.slice(0, maxLen) + '…';
    },

    /** Create a DOM element with attributes and children. */
    el(tag, attrs = {}, ...children) {
        const element = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (key === 'className') element.className = value;
            else if (key === 'onclick') element.onclick = value;
            else if (key === 'innerHTML') element.innerHTML = value;
            else element.setAttribute(key, value);
        }
        for (const child of children) {
            if (typeof child === 'string') element.appendChild(document.createTextNode(child));
            else if (child) element.appendChild(child);
        }
        return element;
    },

    /** Create a badge element for a node kind. */
    kindBadge(kind) {
        return Utils.el('span', { className: `badge badge-${kind}` }, kind);
    },

    /** Debounce a function call. */
    debounce(fn, ms = 300) {
        let timer;
        return (...args) => {
            clearTimeout(timer);
            timer = setTimeout(() => fn(...args), ms);
        };
    },

    /** Escape HTML special characters. */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },
};
