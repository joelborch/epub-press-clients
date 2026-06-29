# Local URL EPUB Builder

Build a local EPUB from a plain-text list of URLs without sending article
content to the EpubPress service.

This tool intentionally does not automate paywall or subscription bypasses. It
fetches pages that are accessible to this machine, extracts article-like content,
and fails closed when a page looks blocked or too thin.

## Usage

Create a URL list:

```text
https://example.com/article-one
https://example.com/article-two
```

Run:

```bash
python3 build_url_epub.py urls.txt --title "My Reader"
```

By default, the EPUB and report are written to `~/Downloads/local-url-epubs/`.

Useful options:

```bash
python3 build_url_epub.py urls.txt --title "My Reader" --min-words 500
python3 build_url_epub.py urls.txt --title "My Reader" --allow-partial
python3 build_url_epub.py urls.txt --output ~/Downloads/my-reader.epub
```

The script writes:

- An `.epub` file when all required URLs pass extraction.
- A `.report.json` file with status, title, word count, fetch URL, and any error
  for each input URL.

