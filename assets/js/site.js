/**
 * Keepfor.me — Interactive 2026 Context Vault Scripts
 *
 * 1. Signature Context Showcase (Simulates live MCP retrieval from kept items)
 * 2. Reader Mode Theme Switcher
 * 3. Copy-to-clipboard terminal helpers
 * 4. Mobile navigation toggle
 */

document.addEventListener('DOMContentLoaded', () => {
  initContextShowcase();
  initReaderPreview();
  initCopyHelpers();
  initMobileNav();
});

/* ==========================================================================
   1. Interactive Context Showcase
   Shows how anything you keep (notes, links, recipes, research) instantly
   becomes active context in any AI agent (Claude, Cursor, ChatGPT, etc.)
   ========================================================================== */

const CONTEXT_ITEMS = {
  pricing: {
    userQuery: 'Hey Claude, what pricing model did I propose in my notes, and what was the main margin risk?',
    aiCitation: 'Keepfor.me Vault · "Freemium vs Usage-Based Note" (#product #pricing)',
    aiResponse: `Based on your saved notes from earlier this week, you proposed a <strong>$12/month base tier</strong> for everyday usage, paired with metered credits for heavy AI tool executions.<br><br>The primary margin risk you highlighted: <div class="ai-highlight-quote">"An unmetered flat rate leaves us vulnerable to power users consuming hundreds of background LLM agent queries at our expense."</div>`,
    targetApp: 'Claude 3.7 Sonnet',
    toolCall: 'mcp.keepfor.me/search(query="pricing tier margin risk")'
  },
  serverActions: {
    userQuery: 'Cursor, what security check did that article recommend before writing to the database in Next.js Server Actions?',
    aiCitation: 'Keepfor.me Vault · "Next.js 15 Server Actions" (#dev #security)',
    aiResponse: `According to your saved bookmark on Next.js 15 security, you should treat server actions like open public endpoints:<br><br><div class="ai-highlight-quote">"Always verify user authentication and authorization inside the action handler body itself before initiating any database mutation—do not rely solely on middleware."</div>`,
    targetApp: 'Cursor / Copilot Agent',
    toolCall: 'mcp.keepfor.me/get_item(id="item_sec_9182")'
  },
  sourdough: {
    userQuery: 'What hydration ratio and cold ferment time did I save for Sunday\'s focaccia bake?',
    aiCitation: 'Keepfor.me Vault · "Grandma\'s Rustic Sourdough Focaccia" (#recipes #baking)',
    aiResponse: `In your saved recipe, the parameters are:<br>• <strong>Hydration:</strong> 80% with 3% extra virgin olive oil.<br>• <strong>Fermentation:</strong> 4 sets of stretch-and-folds every 30 minutes, followed by a <strong>72-hour cold retard</strong> in the refrigerator.<br>• Finish with flaky sea salt and fresh rosemary before dimpling.`,
    targetApp: 'ChatGPT / Raycast AI',
    toolCall: 'mcp.keepfor.me/search(query="sourdough focaccia hydration cold ferment")'
  },
  travel: {
    userQuery: 'Plan a relaxing Saturday morning in Tokyo using the quiet neighborhood spots I saved in my vault.',
    aiCitation: 'Keepfor.me Vault · "Quiet Coffee Shops & Bookstores in Yanaka" (#travel #japan)',
    aiResponse: `Here is your morning itinerary straight from your saved Yanaka notes:<br>1. <strong>9:00 AM:</strong> Coffee and egg toast at <em>Kayaba Coffee</em> (peaceful historic kissaten).<br>2. <strong>10:30 AM:</strong> Browse vintage art prints and architecture titles at <em>Ogawa Books</em>.<br>3. <strong>11:45 AM:</strong> Walk through the quiet residential temple alleys to <em>Hagiso</em> cultural cafe.`,
    targetApp: 'Apple Intelligence / Agent',
    toolCall: 'mcp.keepfor.me/search(query="Yanaka Tokyo quiet coffee bookstores")'
  }
};

function initContextShowcase() {
  const cards = document.querySelectorAll('.kept-item-card');
  const userBubble = document.getElementById('ai-user-query');
  const aiTag = document.getElementById('ai-citation-tag');
  const aiBody = document.getElementById('ai-response-body');
  const aiAppLabel = document.getElementById('ai-target-app-label');
  const mcpBadge = document.getElementById('mcp-bridge-badge');

  if (!cards.length || !userBubble || !aiBody) return;

  cards.forEach((card) => {
    card.addEventListener('click', () => {
      const key = card.dataset.itemKey;
      const data = CONTEXT_ITEMS[key];
      if (!data) return;

      // Update active card state
      cards.forEach((c) => c.classList.remove('active'));
      card.classList.add('active');

      // Subtle animation state
      aiBody.style.opacity = '0.3';
      userBubble.style.opacity = '0.3';

      setTimeout(() => {
        userBubble.textContent = data.userQuery;
        aiTag.textContent = data.aiCitation;
        aiBody.innerHTML = data.aiResponse;
        if (aiAppLabel) aiAppLabel.textContent = data.targetApp;
        if (mcpBadge) mcpBadge.textContent = data.toolCall;

        aiBody.style.opacity = '1';
        userBubble.style.opacity = '1';
      }, 140);
    });
  });
}

/* ==========================================================================
   2. Reader Preview Box Theme Switcher
   ========================================================================== */

function initReaderPreview() {
  const box = document.getElementById('reader-preview-box');
  const pills = document.querySelectorAll('.theme-pill');
  if (!box || !pills.length) return;

  pills.forEach((pill) => {
    pill.addEventListener('click', () => {
      const theme = pill.dataset.theme; // 'light', 'sepia', 'dark'
      box.classList.remove('theme-light', 'theme-sepia', 'theme-dark');
      box.classList.add(`theme-${theme}`);

      pills.forEach((p) => p.classList.remove('active'));
      pill.classList.add('active');
    });
  });
}

/* ==========================================================================
   3. Terminal & Copy Helpers
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
   4. Mobile Navigation
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
    nav.style.top = '72px';
    nav.style.left = '0';
    nav.style.right = '0';
    nav.style.backgroundColor = '#ffffff';
    nav.style.padding = '24px';
    nav.style.borderBottom = '1px solid var(--border)';
    nav.style.boxShadow = 'var(--shadow-lg)';
    nav.style.gap = '20px';
  });
}
