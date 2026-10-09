/**
 * Keepfor.me — Friendly Presentation Site Interactive Scripts
 *
 * 1. Reader Demo Widget Controller (Light, Sepia, Dark + Serif/Sans)
 * 2. Everyday Smart Search Simulator (Natural topic search matching)
 * 3. Copy-to-clipboard helpers
 * 4. Mobile navigation toggle
 */

document.addEventListener('DOMContentLoaded', () => {
  initReaderWidget();
  initEverydaySearchSimulator();
  initCopyHelpers();
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
      
      widget.classList.remove('reader-theme-light', 'reader-theme-sepia', 'reader-theme-dark');
      widget.classList.add(`reader-theme-${theme}`);

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
   2. Everyday Smart Search Simulator
   ========================================================================== */

const EVERYDAY_ARTICLES = [
  {
    title: 'The Quiet Magic of a 20-Minute Morning Routine',
    snippet: 'Why starting your day with natural light, hydration, and ten minutes of silence sets the foundation for calm focus.',
    category: 'Daily Rituals',
    readTime: '4 min read',
    keywords: ['morning', 'routine', 'habits', 'focus', 'light', 'calm', 'coffee'],
    matches: {
      'morning routine': '99% Match · Exact Topic',
      'better sleep habits': '85% Match · Related Concept',
      'slow productivity': '89% Match · Daily Rhythm'
    }
  },
  {
    title: 'A Gentle Beginner’s Guide to Sourdough Bread',
    snippet: 'Everything you need to know about keeping a starter alive, folding dough, and baking your first crusty rustic loaf.',
    category: 'Kitchen & Craft',
    readTime: '7 min read',
    keywords: ['sourdough', 'baking', 'bread', 'starter', 'flour', 'kitchen', 'food'],
    matches: {
      'sourdough baking': '99% Match · Exact Topic',
      'morning routine': '78% Match · Kitchen Rituals',
      'slow productivity': '82% Match · Craft & Patience'
    }
  },
  {
    title: 'Slow Productivity: The Antidote to Daily Overwhelm',
    snippet: 'Accomplishing deeply meaningful work by doing fewer things, working at a natural pace, and obsessing over quality.',
    category: 'Work & Mindset',
    readTime: '6 min read',
    keywords: ['slow', 'productivity', 'focus', 'burnout', 'quality', 'work'],
    matches: {
      'slow productivity': '99% Match · Exact Topic',
      'morning routine': '91% Match · Intentional Days',
      'better sleep habits': '84% Match · Stress Reduction'
    }
  },
  {
    title: 'Mastering Your Sleep Cycles Naturally',
    snippet: 'How cooler room temperatures, morning sunlight, and consistent sleep windows transform your daily energy and mood.',
    category: 'Health & Wellness',
    readTime: '5 min read',
    keywords: ['sleep', 'habits', 'rest', 'cycles', 'energy', 'health', 'circadian'],
    matches: {
      'better sleep habits': '99% Match · Exact Topic',
      'morning routine': '92% Match · Circadian Rhythm',
      'slow productivity': '86% Match · Recovery'
    }
  },
  {
    title: 'The Art of the Slow Sunday',
    snippet: 'Why carving out a few hours without screens, errands, or to-do lists rejuvenates how you feel all week long.',
    category: 'Essays & Living',
    readTime: '5 min read',
    keywords: ['slow', 'sunday', 'rest', 'peace', 'unplug', 'reading'],
    matches: {
      'slow productivity': '94% Match · Rest as Fuel',
      'better sleep habits': '88% Match · Deep Rest',
      'morning routine': '86% Match · Quiet Mornings',
      'sourdough baking': '80% Match · Weekend Projects'
    }
  }
];

function initEverydaySearchSimulator() {
  const input = document.getElementById('search-demo-input');
  const resultsContainer = document.getElementById('search-results-list');
  const chips = document.querySelectorAll('.preset-chip');
  if (!input || !resultsContainer) return;

  function runSearch(queryText) {
    const q = queryText.toLowerCase().trim();
    if (!q) {
      renderResults(EVERYDAY_ARTICLES.slice(0, 3), 'default');
      return;
    }

    // Rank matching articles
    const scored = EVERYDAY_ARTICLES.map((article) => {
      let matchLabel = article.matches[q];
      let score = 0;

      if (matchLabel) {
        score = parseInt(matchLabel, 10) || 80;
      } else {
        const matchesKeyword = article.keywords.some((k) => q.includes(k) || k.includes(q));
        if (matchesKeyword) {
          score = 88;
          matchLabel = '88% Match · Concept Match';
        } else {
          score = 65;
          matchLabel = '65% Match · Related Reading';
        }
      }

      return { ...article, score, matchLabel };
    });

    scored.sort((a, b) => b.score - a.score);
    renderResults(scored.slice(0, 3), q);
  }

  function renderResults(items, query) {
    resultsContainer.innerHTML = '';
    items.forEach((doc) => {
      const card = document.createElement('div');
      card.className = 'search-result-card';
      card.innerHTML = `
        <div class="result-top-line">
          <span class="result-article-title">${escapeHtml(doc.title)}</span>
          <span class="result-match-badge">${escapeHtml(doc.matchLabel || 'Relevant Match')}</span>
        </div>
        <p class="result-snippet">${escapeHtml(doc.snippet)}</p>
        <div class="result-footer-meta">
          <span>${escapeHtml(doc.category)}</span> · <span>${escapeHtml(doc.readTime)}</span>
        </div>
      `;
      resultsContainer.appendChild(card);
    });
  }

  chips.forEach((chip) => {
    chip.addEventListener('click', () => {
      const query = chip.dataset.query;
      input.value = query;
      chips.forEach((c) => c.classList.remove('active'));
      chip.classList.add('active');
      runSearch(query);
    });
  });

  let debounceTimeout;
  input.addEventListener('input', (e) => {
    clearTimeout(debounceTimeout);
    debounceTimeout = setTimeout(() => {
      chips.forEach((c) => c.classList.remove('active'));
      runSearch(e.target.value);
    }, 150);
  });

  // Initial render
  runSearch('morning routine');
}

/* ==========================================================================
   3. Clipboard & Toast Helpers
   ========================================================================== */

function initCopyHelpers() {
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

  const cmdButtons = document.querySelectorAll('[data-copy-text]');
  cmdButtons.forEach((btn) => {
    btn.addEventListener('click', async () => {
      const text = btn.dataset.copyText;
      try {
        await navigator.clipboard.writeText(text);
        showToast('Copied to clipboard!');
      } catch (err) {
        showToast('Press Ctrl+C to copy');
      }
    });
  });
}

/* ==========================================================================
   4. Mobile Navigation Toggle
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
    nav.style.boxShadow = 'var(--shadow-card)';
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
