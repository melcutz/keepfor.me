# GitHub Social Preview Image Specification

## Overview

- **Asset Path**: `docs/images/social-preview.png`
- **Destination**: GitHub Repository **Settings** > **General** > **Social preview** (`https://github.com/melcutz/keepfor.me/settings`)
- **Dimensions**: 1280 × 640 pixels (2:1 aspect ratio, exact GitHub Open Graph standard)
- **Format**: PNG (24-bit RGB) or high-quality JPEG (< 1 MB)

---

## Design Grid & Safe Margins

When rendered on social media feeds (X/Twitter summary large image, Discord embeds, LinkedIn, OpenGraph crawlers), margins are often cropped or overlaid with badge icons.

- **Total Canvas**: 1280px width × 640px height
- **Safe Zone**: 1160px width × 520px height (60px inner padding on all four edges)
- **Visual Balance**: Two-column layout (left 52%: brand & core messaging; right 48%: genuine library card preview or floating interface element)

---

## Color Palette & Theme Tokens

Matches Keepfor.me dark theme design tokens (`static/css/` and Tailwind tokens):

| Token | Hex Code | Usage |
|---|---|---|
| Background Deep | `#0B0F19` / `#0F172A` | Slate-950 canvas with subtle radial glow |
| Brand Primary | `#3B82F6` (Blue 500) | Primary accents, highlights, key badges |
| Brand Amber | `#F59E0B` (Amber 500) | Pinned item indicators, star/tag badges |
| Text Primary | `#F8FAFC` (Slate 50) | Main heading, high-contrast titles |
| Text Muted | `#94A3B8` (Slate 400) | Subtitles, descriptions, captions |
| Card Surface | `#1E293B` (Slate 800) | Elevated panels, preview card backgrounds |
| Border | `#334155` (Slate 700) | Subtle container outlines |

---

## Layout Composition

```
+---------------------------------------------------------------------------------------+
|  [60px Safe Margin]                                                                   |
|                                                                                       |
|  [Logo & Wordmark]                                  [Right Preview Column]            |
|  🔖 Keepfor.me                                       +-----------------------------+  |
|                                                      | Real Library Feed Card      |  |
|  [Main Title]                                        | [Cover Thumbnail / Favicon] |  |
|  Read-it-later & personal library                    | "Building AI Agents at      |  |
|  running entirely at the edge.                       |  the Edge with Fast MCP"    |  |
|                                                      | #ai  #edge  #python         |  |
|  [Pill Badges]                                       | [Pinned 📌]                 |  |
|  ( Cloudflare Python Workers ) ( 46 MCP Tools )      +-----------------------------+  |
|  ( D1 FTS5 + Vectorize Hybrid ) ( Installable PWA )                                   |
|                                                                                       |
|  [Footer Badges]                                                                      |
|  AGPL-3.0 • Self-Hostable • Zero Vendor Lock-in                                       |
|                                                                                       |
|  [60px Safe Margin]                                                                   |
+---------------------------------------------------------------------------------------+
```

### Left Column: Value Proposition & Identity
1. **Logo & Wordmark**:
   - Icon: Keepfor.me dynamic bookmark icon (from `keepfor/pwa_icons.py` / `browser-extension/icon128.png`).
   - Title: `Keepfor.me` in 48pt bold sans-serif font (Inter / system sans).
2. **Headline**:
   - `Read-it-later & personal library running at the edge.` (32pt semi-bold, white `#F8FAFC`).
3. **Key Capability Pills**:
   - `⚡ Cloudflare Python Workers (Pyodide)`
   - `🤖 46 Streamable HTTP MCP Tools`
   - `🔍 D1 FTS5 + Vectorize Hybrid Search`
   - `📱 Installable PWA & Share Sheet`
4. **Subtext / Credo**:
   - `AGPL-3.0 • Free & Self-Hostable • AI Agent Ready` (16pt, `#94A3B8`).

### Right Column: Genuine Interface Snippet
- An authentic, crisp desktop rendering of a Keepfor.me library card (such as Cards view with amber pinned border, thumbnail, title, clean excerpt, and tag chips).
- Note: Must use genuine capture from the live UI (see Issue 2 and Issue 3 in `/home/melcutz/work/logs/kme121-starter-issues.md`), never fabricated mockups.

---

## Setup in GitHub Repository Settings

Once `docs/images/social-preview.png` is created:
1. Navigate to repository **Settings**: `https://github.com/melcutz/keepfor.me/settings`.
2. Under **General**, scroll to the **Social preview** section.
3. Click **Edit** > **Upload an image...**.
4. Select `docs/images/social-preview.png` (verified 1280×640 px).
5. Verify on social share debuggers (e.g., [OpenGraph.xyz](https://www.opengraph.xyz) or Twitter Card Validator) that the crop and resolution are sharp.
