# Repository Visual Assets Directory

This directory hosts documentation images, screenshots, and visual media referenced by `README.md` and repo settings.

## Assets Specification

### 1. `library-hero.png`
- **Location**: `docs/images/library-hero.png`
- **Purpose**: Hero screenshot displayed above the fold in `README.md`.
- **Requirements**:
  - Genuine capture of the Keepfor.me library interface (desktop viewport, e.g. 1280px wide).
  - Populated with realistic, high-quality bookmarks across diverse domains (tech, essays, documentation, news).
  - Demonstrates the Cards view, tag chips, pinned items (amber badge/border), and reading status chips.
  - Zero mock data or fabricated UI: must be taken from an authentic user library.

### 2. `demo.gif`
- **Location**: `docs/images/demo.gif`
- **Purpose**: Animated 20–30s walk-through demonstrating core flow.
- **Workflow**:
  1. User saves an article URL via browser extension / web UI.
  2. Brief flash showing extraction / status.
  3. User switches to an MCP client (Claude Desktop, Cursor, or OpenCode).
  4. Client invokes Keepfor.me MCP (`search_library` or `get_item`) to retrieve and cite the saved article.
- **Requirements**:
  - Duration: 20 to 30 seconds.
  - Frame rate: 15–20 fps, optimized palette via `gifski` or `ffmpeg` (< 5 MB).
  - Authentic capture: genuine interaction between real browser and real MCP client.

### 3. `social-preview.png`
- **Location**: `docs/images/social-preview.png`
- **Purpose**: GitHub repository social preview card (Open Graph preview).
- **Dimensions**: 1280 × 640 px (2:1 aspect ratio).
- **Specification**: See [social-preview-spec.md](../social-preview-spec.md).
