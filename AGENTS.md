# Repository Guidelines

This repository is a small multi-package workspace for EpubPress clients: a Chrome extension, a JS library, and publisher widgets. In practice, day-to-day work is focused on the Chrome extension; the other packages are mostly legacy and rarely updated. Most work happens inside `packages/`.

## Project Structure & Module Organization
- `packages/epub-press-chrome/`: Chrome extension. UI assets live in `app/` (manifest, popup HTML, styles), extension logic in `scripts/`, tests in `tests/`, and icons in `images/`.
- `packages/epub-press-js/`: core client library (legacy). Source is `epub-press.js`, bundled output in `build/`, tests in `tests/`.
- `packages/epub-press-widgets/`: widget bundle (legacy). Entry is `widgets.js`, tests in `tests/`.
- `packages/local-url-epub/`: Local Python-based command-line utility for compiling web URLs into clean, paywall-free EPUB files using the `agy` CLI as a local AI cleaner.
- `screenshots/`: marketing and documentation images.

## Build, Test, and Development Commands
Run commands from the package you are working in.
- Install deps: `npm install`.
- Chrome extension: `npm start` (watch), `npm run build` (dev bundle), `npm run build-prod` (production), `npm test` (webpack-dev-server + opens `http://localhost:5001/tests`).
- JS library: `npm start` (watch), `npm run build`, `npm run build-prod`, `npm test` (browser runner), `node tests/nodeTest.js` for Node coverage.
- Widgets: `npm test` is a placeholder; no build script is defined in `package.json`.

## Testing Guidelines
- Tests use Mocha + Chai with webpack/mocha-loader in browser packages.
- Place tests under each package’s `tests/` folder; `tests/index.js` auto-loads files matching `*-test.js`.
- For node-only checks, use `packages/epub-press-js/tests/nodeTest.js`.

## Local URL EPUB Workflow
The `local-url-epub` utility allows compiling a list of web article URLs (e.g. blog posts, NYT opinions, New Yorker pieces) into a single, clean EPUB book.
- **Run Command:** `/path/to/smart_url_epub.py <urls_file> --title "<book_title>"`
- **Paywall Bypassing:**
  - Performs local fetch first; if blocked, falls back to Wayback Machine and GhostArchive.
  - Queries both `https://` and `http://` versions on Wayback.
  - If Availability API fails (common due to caching), uses the Wayback **CDX Search API** to fetch the raw indices.
  - Supports custom fallback mappings (e.g. mapping paywalled NYT opinion pieces to open reprints, or copying identical syndicated content).
- **AI-Based Cleaning:**
  - Uses the local `agy` daemon CLI (`agy --print`) to parse fetched HTML, extracting clean article titles and paragraphs.
  - Implements a pre-cleaning payload cap of 150 blocks to speed up AI parsing and prevent prompt size limits or timeouts.
