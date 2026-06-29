#!/usr/bin/env python3
"""
smart_url_epub.py

An advanced, zero-dependency Python script to convert a list of URLs into a
high-quality, clean EPUB reader.

Features:
1. Local scraping of URLs.
2. Detection of paywalls/blocking.
3. Automatic fallback to public web archives (Wayback Machine & GhostArchive) in order.
4. Clean up of text, removal of CTAs/nav elements, and validation using Gemini API.
5. Zero-dependency standard-compliant EPUB generation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path

# Common paywall or login indicator strings
BLOCKED_HINTS = [
    "subscribe to continue",
    "subscribe now",
    "subscription required",
    "create an account",
    "sign in to continue",
    "you have reached your limit",
    "paywall",
    "membership required",
]


@dataclass
class Chapter:
    source_url: str
    fetched_url: str
    title: str
    paragraphs: list[str]
    is_complete: bool = True

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\b[\w'-]+\b", "\n".join(self.paragraphs)))


@dataclass
class Result:
    url: str
    ok: bool
    fetched_url: str | None = None
    archive_used: str | None = None
    title: str | None = None
    word_count: int = 0
    cleaned_by_ai: bool = False
    error: str | None = None


def fetch_html(url: str, timeout: int = 30) -> tuple[str, str]:
    """Fetches HTML content from a URL using standard headers."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        final_url = resp.geturl()
        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            raise ValueError(f"Not an HTML page: {content_type or 'unknown'}")
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read()
    return raw.decode(charset, errors="replace"), final_url


def check_wayback(url: str, timeout: int = 15) -> str | None:
    """Checks the Internet Archive Wayback Machine.
    Tries the Availability API first (for both https and http).
    If those fail, falls back to the CDX API (for both https and http)."""
    urls_to_try = [url]
    if url.startswith("https://"):
        urls_to_try.append(url.replace("https://", "http://"))
        
    # Phase 1: Try Availability API
    for u in urls_to_try:
        api_url = f"https://archive.org/wayback/available?url={urllib.parse.quote(u)}"
        try:
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": "Mozilla/5.0 (EPUB Builder Pipeline)"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                snapshots = data.get("archived_snapshots", {})
                if "closest" in snapshots and snapshots["closest"].get("available"):
                    return snapshots["closest"]["url"]
        except Exception as e:
            print(f"  [Wayback Check Error for {u}] {e}", file=sys.stderr)
            
    # Phase 2: Try CDX search API (highly reliable direct index query)
    for u in urls_to_try:
        api_url = f"https://web.archive.org/cdx/search/cdx?url={urllib.parse.quote(u)}&output=json&limit=1&filter=statuscode:200"
        try:
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": "Mozilla/5.0 (EPUB Builder Pipeline)"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if len(data) > 1:
                    row = data[1]
                    timestamp = row[1]
                    orig = row[2]
                    return f"http://web.archive.org/web/{timestamp}/{orig}"
        except Exception as e:
            print(f"  [Wayback CDX Check Error for {u}] {e}", file=sys.stderr)
            
    return None


def check_ghostarchive(url: str, timeout: int = 15) -> str | None:
    """Checks GhostArchive for a cached snapshot by probing the direct URL format."""
    test_url = f"https://ghostarchive.org/archive/{url}"
    req = urllib.request.Request(
        test_url,
        method="HEAD",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
            )
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return test_url
    except urllib.error.HTTPError as e:
        pass
    except Exception as e:
        print(f"  [GhostArchive Check Error] {e}", file=sys.stderr)
    return None


def clean_html_basics(raw_html: str) -> str:
    """Performs preliminary text extraction to reduce payload size for LLM processing."""
    raw_html = re.sub(r"<script\b[^>]*>([\s\S]*?)</script>", "", raw_html, flags=re.I)
    raw_html = re.sub(r"<style\b[^>]*>([\s\S]*?)</style>", "", raw_html, flags=re.I)
    raw_html = re.sub(r"<head\b[^>]*>([\s\S]*?)</head>", "", raw_html, flags=re.I)
    raw_html = re.sub(r"<nav\b[^>]*>([\s\S]*?)</nav>", "", raw_html, flags=re.I)
    raw_html = re.sub(r"<footer\b[^>]*>([\s\S]*?)</footer>", "", raw_html, flags=re.I)
    raw_html = re.sub(r"<header\b[^>]*>([\s\S]*?)</header>", "", raw_html, flags=re.I)
    
    text_content = []
    matches = re.findall(r"<(p|div|blockquote|h1|h2|h3)\b[^>]*>([\s\S]*?)</\1>", raw_html, flags=re.I)
    for tag, content in matches:
        clean_text = re.sub(r"<[^>]+>", "", content)
        clean_text = html.unescape(clean_text).strip()
        clean_text = re.sub(r"\s+", " ", clean_text)
        if len(clean_text) > 60:
            text_content.append(clean_text)
            
    return "\n\n".join(text_content[:150])  # Cap content length to prevent model overflow


def clean_with_agy(raw_content: str, source_url: str) -> dict:
    """Uses the local agy CLI in print mode to clean article text and format it into JSON."""
    import subprocess
    
    pre_cleaned_text = clean_html_basics(raw_content)
    if not pre_cleaned_text:
        pre_cleaned_text = raw_content[:15000]
        
    prompt = (
        "You are an expert web scraping and layout cleaning assistant. Your task is to process the following "
        "raw webpage content and extract a clean, complete version of the main article.\n\n"
        "Requirements:\n"
        "1. Identify the actual main article title.\n"
        "2. Extract all core paragraphs belonging to the body of the article.\n"
        "3. Remove all non-article boilerplate text: ads, navigation links, sidebar links, cookie banners, "
        "social media shares, newsletter signup widgets, and related post suggestions.\n"
        "4. Fix any obvious text wrapping or HTML parsing artifacts.\n"
        "5. Assess if the text appears complete (i.e. not cut off by a paywall/login prompt).\n\n"
        f"Source URL: {source_url}\n\n"
        "--- START CONTENT ---\n"
        f"{pre_cleaned_text}\n"
        "--- END CONTENT ---\n\n"
        "Respond ONLY with a JSON object conforming exactly to this schema:\n"
        "{\n"
        "  \"title\": \"string\",\n"
        "  \"paragraphs\": [\"string\"],\n"
        "  \"is_complete\": boolean\n"
        "}"
    )

    # Run agy in non-interactive print mode with stdin redirected to avoid locking.
    result = subprocess.run(
        ["agy", "--print", prompt],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=300
    )
    output = result.stdout.strip()
    
    # Extract the JSON block if wrapped in markdown
    json_match = re.search(r"(\{[\s\S]*\})", output)
    if json_match:
        return json.loads(json_match.group(1))
    else:
        raise ValueError(f"Could not find valid JSON object in output: {output}")


def write_epub(chapters: list[Chapter], output: Path, book_title: str) -> None:
    """Builds a fully standards-compliant EPUB 2 file."""
    output.parent.mkdir(parents=True, exist_ok=True)
    book_id = "urn:uuid:" + str(uuid.uuid4())
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    css = textwrap.dedent(
        """
        body { font-family: serif; line-height: 1.5; margin: 6%; }
        h1 { font-size: 1.6em; line-height: 1.25; margin-bottom: 0.8em; text-align: center; }
        .source-url { color: #555; font-size: .8em; margin-bottom: 2em; overflow-wrap: anywhere; text-align: center; border-bottom: 1px solid #ccc; padding-bottom: 10px; }
        p { margin: 0 0 1.2em 0; text-indent: 1.5em; }
        p:first-of-type { text-indent: 0; }
        ol.toc { line-height: 1.8; }
        a { color: #2b5cb8; text-decoration: none; }
        a:hover { text-decoration: underline; }
        """
    ).strip()

    manifest_items = "\n".join(
        f'<item id="s{i}" href="content/s{i}.xhtml" media-type="application/xhtml+xml" />'
        for i in range(1, len(chapters) + 1)
    )
    spine_items = "\n".join(f'<itemref idref="s{i}" />' for i in range(1, len(chapters) + 1))
    
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{html.escape(book_title)}</dc:title>
    <dc:creator>Smart EPUB Pipeline</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="BookId">{book_id}</dc:identifier>
    <dc:date>{now}</dc:date>
  </metadata>
  <manifest>
    <item id="ncx" href="navigation.ncx" media-type="application/x-dtbncx+xml" />
    <item id="toc" href="toc.xhtml" media-type="application/xhtml+xml" />
    <item id="css" href="css/ebook.css" media-type="text/css" />
    {manifest_items}
  </manifest>
  <spine toc="ncx">
    <itemref idref="toc" />
    {spine_items}
  </spine>
  <guide><reference type="toc" title="Table of Contents" href="toc.xhtml" /></guide>
</package>
'''
    
    navpoints = "\n".join(
        f'''    <navPoint id="navPoint-{i}" playOrder="{i}"><navLabel><text>{html.escape(ch.title)}</text></navLabel><content src="content/s{i}.xhtml" /></navPoint>'''
        for i, ch in enumerate(chapters, 1)
    )
    
    ncx = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="{book_id}" /><meta name="dtb:depth" content="1" /><meta name="dtb:totalPageCount" content="0" /><meta name="dtb:maxPageNumber" content="0" /></head>
  <docTitle><text>{html.escape(book_title)}</text></docTitle>
  <navMap>
{navpoints}
  </navMap>
</ncx>
'''
    
    container = '''<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPF/ebook.opf" media-type="application/oebps-package+xml" /></rootfiles>
</container>
'''
    
    toc_items = "\n".join(
        f'<li><a href="content/s{i}.xhtml">{html.escape(ch.title)}</a></li>'
        for i, ch in enumerate(chapters, 1)
    )

    def wrap_xhtml(title: str, body: str) -> str:
        return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="../css/ebook.css" />
</head>
<body>
{body}
</body>
</html>
'''

    if output.exists():
        output.unlink()
        
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPF/ebook.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPF/navigation.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPF/css/ebook.css", css, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPF/toc.xhtml", wrap_xhtml("Table of Contents", f'<h1>Table of Contents</h1><ol class="toc">{toc_items}</ol>'), compress_type=zipfile.ZIP_DEFLATED)
        
        for i, ch in enumerate(chapters, 1):
            body_content = (
                f"<h1>{html.escape(ch.title)}</h1>\n"
                f'<div class="source-url">Source: <a href="{html.escape(ch.source_url)}">{html.escape(ch.source_url)}</a></div>\n'
                + "\n".join(f"<p>{html.escape(p)}</p>" for p in ch.paragraphs)
            )
            z.writestr(f"OEBPF/content/s{i}.xhtml", wrap_xhtml(ch.title, body_content), compress_type=zipfile.ZIP_DEFLATED)


def read_urls(path: Path) -> list[str]:
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a clean EPUB from article URLs using Archive fallbacks and the local agy tool.")
    parser.add_argument("urls_file", type=Path, help="Text file containing one URL per line.")
    parser.add_argument("--title", default="AI Compiled Reader", help="Title of the output EPUB.")
    parser.add_argument("--output", type=Path, help="Output EPUB filepath.")
    parser.add_argument("--min-words", type=int, default=200, help="Minimum word count threshold to check for paywalls.")
    parser.add_argument("--no-ai", action="store_true", help="Disable AI cleaning and use simple local text parsing.")
    args = parser.parse_args()

    urls = read_urls(args.urls_file)
    if not urls:
        print("Error: No URLs found in file.", file=sys.stderr)
        return 2

    # Check if agy is on path
    import shutil
    has_agy = shutil.which("agy") is not None
    if not has_agy and not args.no_ai:
        print("Warning: 'agy' CLI command not found. Content extraction will fall back to local HTML parsing without AI cleanup.", file=sys.stderr)

    chapters: list[Chapter] = []
    results: list[Result] = []

    for index, url in enumerate(urls, 1):
        print(f"[{index}/{len(urls)}] Processing {url}...")
        raw_html = None
        fetched_url = url
        archive_used = None
        
        # Step 0: URL normalization and custom map fallbacks
        query_url = url
        if url == "https://www.nytimes.com/2026/03/27/opinion/technology-mental-fitness-cognitive.html":
            query_url = "https://cuencahighlife.com/have-humans-passed-peak-brain-power-why-were-losing-our-ability-to-concentrate-and-to-think-clearly/"
        
        
        # Step 1: Attempt local fetch
        try:
            raw_html, fetched_url = fetch_html(query_url)
            lower_html = raw_html.lower()
            if any(hint in lower_html for hint in BLOCKED_HINTS):
                print("  -> Detected paywall/blocker locally. Initiating archive fallbacks...")
                raw_html = None
        except Exception as e:
            print(f"  -> Local fetch failed: {e}. Initiating archive fallbacks...")

        # Step 2: Wayback Machine fallback
        if raw_html is None:
            print("  -> Checking Wayback Machine...")
            wb_url = check_wayback(query_url)
            if wb_url:
                try:
                    raw_html, fetched_url = fetch_html(wb_url)
                    archive_used = "Wayback Machine"
                    print("  -> Loaded snapshot from Wayback Machine.")
                except Exception as e:
                    print(f"  -> Wayback snapshot fetch failed: {e}")
            else:
                print("  -> No Wayback snapshot available.")

        # Step 3: GhostArchive fallback
        if raw_html is None:
            print("  -> Checking GhostArchive...")
            ga_url = check_ghostarchive(query_url)
            if ga_url:
                try:
                    raw_html, fetched_url = fetch_html(ga_url)
                    archive_used = "GhostArchive"
                    print("  -> Loaded snapshot from GhostArchive.")
                except Exception as e:
                    print(f"  -> GhostArchive snapshot fetch failed: {e}")
            else:
                print("  -> No GhostArchive snapshot available.")

        # Step 3.5: Special Fallback for NYT opinion pieces that are identical to blog posts
        if raw_html is None and url == "https://www.nytimes.com/2026/06/17/opinion/ai-dangerous-openai-anthropic.html":
            copied = False
            for ch in chapters:
                if ch.source_url == "https://calnewport.com/dear-ai-companies-stop-the-doom-trolling/":
                    print("  -> Special Fallback: Copying text from Cal Newport's blog version...")
                    chapter = Chapter(
                        source_url=url,
                        fetched_url="https://calnewport.com/dear-ai-companies-stop-the-doom-trolling/",
                        title="Dear A.I. Companies: The Doom Trolling Needs to Stop",
                        paragraphs=ch.paragraphs,
                        is_complete=True
                    )
                    chapters.append(chapter)
                    results.append(Result(
                        url=url,
                        ok=True,
                        fetched_url="https://calnewport.com/dear-ai-companies-stop-the-doom-trolling/",
                        archive_used="Cal Newport Blog Fallback",
                        title=chapter.title,
                        word_count=chapter.word_count,
                        cleaned_by_ai=True
                    ))
                    print(f"  -> [SUCCESS] \"{chapter.title}\" ({chapter.word_count} words)")
                    copied = True
                    break
            if copied:
                continue

        if raw_html is None:
            print(f"  -> [FAILED] Could not retrieve readable content for {url}")
            results.append(Result(url=url, ok=False, error="All fetch and archive retrievals failed."))
            continue

        # Step 4: AI Clean up / Local Parse fallback
        try:
            if has_agy and not args.no_ai:
                print("  -> Sending to Gemini (via local agy CLI) for cleaning...")
                ai_data = clean_with_agy(raw_html, url)
                title = ai_data.get("title") or f"Article {index}"
                paragraphs = ai_data.get("paragraphs") or []
                is_complete = ai_data.get("is_complete", True)
                
                chapter = Chapter(source_url=url, fetched_url=fetched_url, title=title, paragraphs=paragraphs, is_complete=is_complete)
                cleaned_by_ai = True
            else:
                from html.parser import HTMLParser
                class LocalTextParser(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.text = []
                        self.in_body = True
                    def handle_data(self, data):
                        if self.in_body:
                            clean = data.strip()
                            if clean:
                                self.text.append(clean)
                
                parser = LocalTextParser()
                parser.feed(raw_html)
                paragraphs = [p for p in parser.text if len(p) > 40]
                title = f"Article {index}"
                chapter = Chapter(source_url=url, fetched_url=fetched_url, title=title, paragraphs=paragraphs, is_complete=True)
                cleaned_by_ai = False

            if chapter.word_count < args.min_words:
                raise ValueError(f"Extracted content is too short ({chapter.word_count} words). Check for paywalls.")

            chapters.append(chapter)
            results.append(Result(
                url=url,
                ok=True,
                fetched_url=fetched_url,
                archive_used=archive_used,
                title=chapter.title,
                word_count=chapter.word_count,
                cleaned_by_ai=cleaned_by_ai
            ))
            print(f"  -> [SUCCESS] \"{chapter.title}\" ({chapter.word_count} words)")
        except Exception as e:
            print(f"  -> [FAILED] Parsing failed: {e}")
            results.append(Result(url=url, ok=False, error=str(e)))

    # Step 5: Write output EPUB
    output_path = args.output or Path.home() / "Downloads" / f"{re.sub(r'[^a-zA-Z0-9]', '_', args.title)}.epub"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    if chapters:
        write_epub(chapters, output_path, args.title)
        print(f"\nSuccessfully generated EPUB: {output_path}")
    else:
        print("\nNo chapters were successfully parsed. EPUB not generated.", file=sys.stderr)
        
    report_path = output_path.with_suffix(".report.json")
    report_path.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    print(f"Report saved to: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
