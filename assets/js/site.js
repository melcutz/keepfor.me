/**
 * Keepfor.me — Presentation Site Interactive Controllers
 *
 * 1. Reader Demo Widget Controller (Isolates theme & font toggling to widget only)
 * 2. Hybrid Search Interactive Simulator (Reciprocal Rank Fusion BM25 + Vectorize)
 * 3. Clipboard & Toast Feedback
 * 4. Code Tab Switcher
 */

document.addEventListener('DOMContentLoaded', () => {
  initReaderWidget();
  initHybridSearchSimulator();
  initCopyHelpers();
  initCodeTabs();
  initMobileNav();
});

/* ==========================================================================
   1. Reader Demo Widget Controller
   Only touches #reader-demo-widget — site branding remains completely steady!
   ========================================================================== */

function initReaderWidget() {
  const widget = document.getElementById('reader-demo-widget');
  if (!widget) return;

  const themeBtns = document.querySelectorAll('.theme-pill-btn');
  const fontBtns = document.querySelectorAll('.font-pill-btn');

  themeBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      const theme = btn.dataset.theme; // 'light', 'sepia', 'dark'
      
      // Remove all theme classes on widget
      widget.classList.remove('reader-theme-light', 'reader-theme-sepia', 'reader-theme-dark');
      widget.classList.add(`reader-theme-${theme}`);

      // Update button active state
      themeBtns.forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });

  fontBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      const font = btn.dataset.font; // 'serif', 'sans'

      widget.classList.remove('reader-font-serif', 'reader-font-sans');
      widget.classList.add(`reader-font-${font}`);

      fontBtns.forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });
}

/* ==========================================================================
   2. Hybrid Search Interactive Simulator
   Simulates Reciprocal Rank Fusion: 1/(60 + r_bm25) + 1/(60 + r_vec)
   ========================================================================== */

const SEARCH_DEMO_DATABASE = [
  {
    title: 'Optimizing SQLite & FTS5 in Serverless Pyodide',
    snippet: 'Benchmarking composite indexes, full-text virtual tables, and single-roundtrip latency on Cloudflare Workers edge nodes.',
    date: 'Oct 02, 2026',
    category: 'Architecture',
    keywords: ['sqlite', 'performance', 'database', 'fts5', 'edge', 'latency', 'pyodide', 'd1'],
    bm25Ranks: { 'sqlite performance': 1, 'neural embeddings': 5, 'pwa offline': 4, 'mcp agents': 6 },
    vecRanks: { 'sqlite performance': 2, 'neural embeddings': 4, 'pwa offline': 5, 'mcp agents': 5 },
  },
  {
    title: 'Hybrid Information Retrieval: Reciprocal Rank Fusion',
    snippet: 'Why combining sparse keyword BM25 scoring with dense cosine semantic embeddings beats single-engine search by 34%.',
    date: 'Sep 28, 2026',
    category: 'Search Engine',
    keywords: ['hybrid', 'search', 'rrf', 'bm25', 'vectorize', 'embeddings', 'rank', 'fusion'],
    bm25Ranks: { 'sqlite performance': 3, 'neural embeddings': 1, 'pwa offline': 6, 'mcp agents': 3 },
    vecRanks: { 'sqlite performance': 3, 'neural embeddings': 2, 'pwa offline': 4, 'mcp agents': 2 },
  },
  {
    title: 'Edge AI Embeddings with Cloudflare Workers AI & Vectorize',
    snippet: 'Generating 384-dimensional dense vectors on the edge using BAAI/bge-small-en-v1.5 and querying Vectorize index in <30ms.',
    date: 'Sep 24, 2026',
    category: 'AI & Vectors',
    keywords: ['neural', 'embeddings', 'vectorize', 'workers ai', 'ai', 'dense', 'semantic', 'models'],
    bm25Ranks: { 'sqlite performance': 4, 'neural embeddings': 2, 'pwa offline': 7, 'mcp agents': 4 },
    vecRanks: { 'sqlite performance': 1, 'neural embeddings': 1, 'pwa offline': 6, 'mcp agents': 4 },
  },
  {
    title: 'Model Context Protocol (MCP) Streamable HTTP Deep Dive',
    snippet: 'Exposing personal document vaults to Claude Desktop and Cursor via JSON-RPC over Streamable HTTP without persistent stdio pipes.',
    date: 'Oct 05, 2026',
    category: 'MCP & Agents',
    keywords: ['mcp', 'agents', 'claude', 'cursor', 'llm', 'streamable', 'http', 'json-rpc'],
    bm25Ranks: { 'sqlite performance': 6, 'neural embeddings': 4, 'pwa offline': 8, 'mcp agents': 1 },
    vecRanks: { 'sqlite performance': 5, 'neural embeddings': 3, 'pwa offline': 7, 'mcp agents': 1 },
  },
  {
    title: 'Modern PWA Offline Architecture & Web Share Target',
    snippet: 'Zero-latency mobile captures with Android share target sheets, Safari iOS shortcuts, and offline-first service worker caches.',
    date: 'Sep 19, 2026',
    category: 'Mobile & PWA',
    keywords: ['pwa', 'offline', 'mobile', 'share', 'cache', 'ios', 'android', 'shortcut'],
    bm25Ranks: { 'sqlite performance': 5, 'neural embeddings': 6, 'pwa offline': 1, 'mcp agents': 7 },
    vecRanks: { 'sqlite performance': 6, 'neural embeddings': 6, 'pwa offline': 1, 'mcp agents': 6 },
  },
  {
    title: 'Distraction-Free Web Content Extraction with Trafilatura',
    snippet: 'Stripping navbars, tracker scripts, ads, and cookie banners to produce clean raw Markdown and HTML snapshots in R2.',
    date: 'Sep 12, 2026',
    category: 'Extraction',
    keywords: ['extraction', 'markdown', 'clean', 'trafilatura', 'r2', 'reader', 'content'],
    bm25Ranks: { 'sqlite performance': 2, 'neural embeddings': 7, 'pwa offline': 2, 'mcp agents': 5 },
    vecRanks: { 'sqlite performance': 4, 'neural embeddings': 5, 'pwa offline': 2, 'mcp agents': 7 },
  }
];

function initHybridSearchSimulator() {
  const input = document.getElementById('search-demo-input');
  const resultsContainer = document.getElementById('search-results-list');
  const chips = document.querySelectorAll('.preset-chip');
  if (!input || !resultsContainer) return;

  function runSearch(queryText) {
    const q = queryText.toLowerCase().trim();
    if (!q) {
      renderResults(SEARCH_DEMO_DATABASE.slice(0, 3), 'default');
      return;
    }

    // Determine ranking based on predefined presets or keyword match
    const scored = SEARCH_DEMO_DATABASE.map((item, idx) => {
      let bm25Rank = item.bm25Ranks[q] || (item.keywords.some(k => q.includes(k)) ? 1 + (idx % 3) : 6 + idx);
      let vecRank = item.vecRanks[q] || (item.keywords.some(k => q.includes(k)) ? 2 + (idx % 2) : 5 + idx);

      // Reciprocal Rank Fusion Formula: 1 / (60 + r_bm25) + 1 / (60 + r_vec)
      const rrfScore = (1 / (60 + bm25Rank)) + (1 / (60 + vecRank));

      return {
        ...item,
        bm25Rank,
        vecRank,
        rrfScore
      };
    });

    // Sort descending by RRF score
    scored.sort((a, b) => b.rrfScore - a.rrfScore);
    renderResults(scored.slice(0, 3), q);
  }

  function renderResults(items, query) {
    resultsContainer.innerHTML = '';
    items.forEach((doc, index) => {
      const scoreStr = doc.rrfScore ? doc.rrfScore.toFixed(4) : (0.0325 - index * 0.003).toFixed(4);
      const bm25Rank = doc.bm25Rank || (index + 1);
      const vecRank = doc.vecRank || (index + 2);

      const card = document.createElement('div');
      card.className = 'search-result-item';
      card.innerHTML = `
        <div class="res-header">
          <span class="res-title">${escapeHtml(doc.title)}</span>
          <span class="rrf-score-badge" title="RRF Score = 1/(60+${bm25Rank}) + 1/(60+${vecRank})">RRF ${scoreStr}</span>
        </div>
        <p style="font-size: 0.825rem; color: var(--muted); margin-bottom: 10px; line-height: 1.5;">${escapeHtml(doc.snippet)}</p>
        <div class="res-meta">
          <span class="rank-pill bm25">BM25 Rank #${bm25Rank}</span>
          <span class="rank-pill vector">Vector Rank #${vecRank}</span>
          <span>${escapeHtml(doc.category)}</span>
          <span>${escapeHtml(doc.date)}</span>
        </div>
      `;
      resultsContainer.appendChild(card);
    });
  }

  // Preset chips click
  chips.forEach((chip) => {
    chip.addEventListener('click', () => {
      const query = chip.dataset.query;
      input.value = query;
      chips.forEach((c) => c.classList.remove('active'));
      chip.classList.add('active');
      runSearch(query);
    });
  });

  // Input debouncing
  let debounceTimeout;
  input.addEventListener('input', (e) => {
    clearTimeout(debounceTimeout);
    debounceTimeout = setTimeout(() => {
      chips.forEach((c) => c.classList.remove('active'));
      runSearch(e.target.value);
    }, 150);
  });

  // Initial render
  runSearch('sqlite performance');
}

/* ==========================================================================
   3. Clipboard & Toast Feedback
   ========================================================================== */

function initCopyHelpers() {
  const copyButtons = document.querySelectorAll('[data-copy-target]');
  const toast = document.getElementById('copy-toast');
  const toastMsg = document.getElementById('copy-toast-msg');

  function showToast(text) {
    if (!toast) return;
    if (toastMsg) toastMsg.textContent = text;
    toast.classList.add('show');
    setTimeout(() => {
      toast.classList.remove('show');
    }, 2400);
  }

  copyButtons.forEach((btn) => {
    btn.addEventListener('click', async () => {
      const targetId = btn.dataset.copyTarget;
      const targetElem = document.getElementById(targetId);
      if (!targetElem) return;

      const text = targetElem.innerText || targetElem.textContent;
      try {
        await navigator.clipboard.writeText(text.trim());
        const originalText = btn.innerHTML;
        btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg> Copied!`;
        showToast('Copied snippet to clipboard!');
        setTimeout(() => {
          btn.innerHTML = originalText;
        }, 2000);
      } catch (err) {
        showToast('Press Ctrl+C to copy snippet');
      }
    });
  });

  // Generic direct copy buttons (e.g. terminal command copy)
  const cmdButtons = document.querySelectorAll('[data-copy-text]');
  cmdButtons.forEach((btn) => {
    btn.addEventListener('click', async () => {
      const text = btn.dataset.copyText;
      try {
        await navigator.clipboard.writeText(text);
        showToast(`Copied: "${text}"`);
      } catch (err) {
        showToast('Failed to copy to clipboard');
      }
    });
  });
}

/* ==========================================================================
   4. Code Tab Switcher
   ========================================================================== */

const CODE_SNIPPETS = {
  claude: `{
  "mcpServers": {
    "keepfor-me": {
      "type": "http",
      "url": "https://keepfor.me/api/mcp",
      "headers": {
        "Authorization": "Bearer kfm_live_YOUR_PERSONAL_TOKEN"
      }
    }
  }
}`,
  cursor: `{
  "mcp": {
    "servers": {
      "keepfor-me": {
        "url": "https://keepfor.me/api/mcp",
        "headers": {
          "Authorization": "Bearer kfm_live_YOUR_PERSONAL_TOKEN"
        }
      }
    }
  }
}`,
  curl: `curl -X POST https://keepfor.me/api/mcp \\
  -H "Authorization: Bearer kfm_live_YOUR_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "search_saved_items",
      "arguments": { "query": "edge computing sqlite" }
    }
  }'`
};

function initCodeTabs() {
  const tabs = document.querySelectorAll('.code-tab-btn');
  const codeBlock = document.getElementById('mcp-code-block');
  const codeFileName = document.getElementById('code-file-name');
  if (!tabs.length || !codeBlock) return;

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab; // 'claude', 'cursor', 'curl'
      tabs.forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');

      if (CODE_SNIPPETS[target]) {
        codeBlock.textContent = CODE_SNIPPETS[target];
      }

      if (codeFileName) {
        if (target === 'claude') codeFileName.textContent = 'claude_desktop_config.json';
        else if (target === 'cursor') codeFileName.textContent = '.cursor/mcp.json';
        else if (target === 'curl') codeFileName.textContent = 'bash (terminal)';
      }
    });
  });
}

/* ==========================================================================
   5. Mobile Navigation Toggle
   ========================================================================== */

function initMobileNav() {
  const toggleBtn = document.querySelector('.mobile-toggle');
  const nav = document.querySelector('.nav-links');
  if (!toggleBtn || !nav) return;

  toggleBtn.addEventListener('click', () => {
    const isShown = nav.style.display === 'flex';
    nav.style.display = isShown ? 'none' : 'flex';
    nav.style.flexDirection = 'column';
    nav.style.position = 'absolute';
    nav.style.top = '68px';
    nav.style.left = '0';
    nav.style.right = '0';
    nav.style.backgroundColor = 'var(--canvas)';
    nav.style.padding = '20px 24px';
    nav.style.borderBottom = '1px solid var(--border)';
    nav.style.boxShadow = 'var(--shadow-md)';
  });
}

function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
