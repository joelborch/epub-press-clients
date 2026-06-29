#!/usr/bin/env python3
"""
Build a local EPUB from accessible article URLs.

The script keeps the workflow intentionally boring: fetch HTML, extract the most
article-like content with stdlib HTML parsing, validate word counts, then write a
small EPUB 2 package. It does not use archive/paywall bypass services.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


BLOCKED_HINTS = [
    "subscribe to continue",
    "subscribe now",
    "subscription required",
    "create an account",
    "sign in to continue",
    "you have reached your limit",
    "paywall",
]

REMOVE_TAGS = {
    "script",
    "style",
    "noscript",
    "svg",
    "canvas",
    "iframe",
    "object",
    "form",
    "button",
    "nav",
    "aside",
    "footer",
    "header",
}

BLOCK_TAGS = {
    "article",
    "main",
    "section",
    "div",
    "p",
    "blockquote",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
}

VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


@dataclass
class Chapter:
    source_url: str
    fetched_url: str
    title: str
    paragraphs: list[str]

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\b[\w'-]+\b", "\n".join(self.paragraphs)))


@dataclass
class Result:
    url: str
    ok: bool
    fetched_url: str | None = None
    title: str | None = None
    word_count: int = 0
    error: str | None = None


class ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta_title: str | None = None
        self.meta_description: str | None = None
        self._in_title = False
        self._skip_depth = 0
        self._stack: list[str] = []
        self._current: list[str] = []
        self.blocks: list[tuple[str, str, tuple[str, ...]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}

        if tag in REMOVE_TAGS:
            self._skip_depth += 1
            return

        if self._skip_depth:
            return

        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            prop = attrs_dict.get("property") or attrs_dict.get("name")
            content = attrs_dict.get("content")
            if content and prop in {"og:title", "twitter:title"}:
                self.meta_title = content.strip()
            elif content and prop in {"description", "og:description", "twitter:description"}:
                self.meta_description = content.strip()

        if tag in BLOCK_TAGS:
            self._flush()

        ident = " ".join(
            part
            for part in [
                tag,
                attrs_dict.get("id", ""),
                attrs_dict.get("class", ""),
                attrs_dict.get("itemprop", ""),
                attrs_dict.get("role", ""),
            ]
            if part
        ).lower()
        if tag not in VOID_TAGS:
            self._stack.append(ident)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag in REMOVE_TAGS:
                self._skip_depth -= 1
            return

        if tag == "title":
            self._in_title = False

        if tag in BLOCK_TAGS:
            self._flush(tag)

        if self._stack and tag not in VOID_TAGS:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self._current.append(data)

    def _flush(self, tag: str = "div") -> None:
        text = normalize_text(" ".join(self._current))
        self._current = []
        if len(text) < 35:
            return
        self.blocks.append((tag, text, tuple(self._stack)))


def normalize_text(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def clean_title(value: str | None, fallback: str) -> str:
    title = normalize_text(value or "")
    title = re.sub(r"\s+[|-]\s+.+$", "", title).strip()
    return title or fallback


def read_urls(path: Path) -> list[str]:
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def fetch_html(url: str, timeout: int = 30) -> tuple[str, str]:
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
            raise ValueError(f"not an HTML page: {content_type or 'unknown content type'}")
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read()
    return raw.decode(charset, errors="replace"), final_url


def score_block(text: str, stack: Iterable[str]) -> int:
    words = len(re.findall(r"\b[\w'-]+\b", text))
    score = min(words, 220)
    stack_text = " ".join(stack)
    if re.search(r"article|content|entry|post|story|main", stack_text):
        score += 80
    if re.search(r"comment|promo|related|share|sidebar|footer|nav|menu|advert", stack_text):
        score -= 120
    if len(text) > 250:
        score += 30
    return score


def extract_article(raw_html: str, source_url: str, final_url: str, min_words: int) -> Chapter:
    parser = ArticleParser()
    parser.feed(raw_html)
    parser._flush()

    title = clean_title(
        parser.meta_title or " ".join(parser.title_parts),
        urllib.parse.urlparse(source_url).path.rstrip("/").rsplit("/", 1)[-1] or source_url,
    )

    lower = raw_html[:200000].lower()
    blocked = [hint for hint in BLOCKED_HINTS if hint in lower]

    scored = sorted(
        ((score_block(text, stack), text) for _tag, text, stack in parser.blocks),
        key=lambda item: item[0],
        reverse=True,
    )
    selected: list[str] = []
    seen: set[str] = set()
    for score, text in scored:
        if score < 20:
            continue
        key = text[:120].lower()
        if key in seen:
            continue
        seen.add(key)
        selected.append(text)

    selected = order_paragraphs(selected, parser.blocks)
    chapter = Chapter(source_url=source_url, fetched_url=final_url, title=title, paragraphs=selected)

    if blocked and chapter.word_count < min_words * 2:
        raise ValueError(f"page appears blocked: {', '.join(blocked[:3])}")
    if chapter.word_count < min_words:
        raise ValueError(f"extracted only {chapter.word_count} words, below minimum {min_words}")
    return chapter


def order_paragraphs(selected: list[str], blocks: list[tuple[str, str, tuple[str, ...]]]) -> list[str]:
    selected_set = set(selected)
    ordered: list[str] = []
    for _tag, text, _stack in blocks:
        if text in selected_set and text not in ordered:
            ordered.append(text)
    return ordered


def xhtml_escape(value: str) -> str:
    return html.escape(value, quote=True)


def paragraph_html(text: str) -> str:
    return f"<p>{xhtml_escape(text)}</p>"


def page(title: str, body: str) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{xhtml_escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="../css/ebook.css" />
</head>
<body>
{body}
</body>
</html>
'''


def write_epub(chapters: list[Chapter], output: Path, book_title: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    book_id = "urn:uuid:" + str(uuid.uuid4())
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    css = textwrap.dedent(
        """
        body { font-family: serif; line-height: 1.45; margin: 5%; }
        h1 { font-size: 1.5em; line-height: 1.2; margin-bottom: 1em; }
        .source-url { color: #666; font-size: .85em; margin-bottom: 1.5em; overflow-wrap: anywhere; }
        p { margin: 0 0 1em 0; }
        ol.toc { line-height: 1.6; }
        a { color: #2455a6; }
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
    <dc:title>{xhtml_escape(book_title)}</dc:title>
    <dc:creator>Local URL EPUB Builder</dc:creator>
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
        f'''    <navPoint id="navPoint-{i}" playOrder="{i}"><navLabel><text>{xhtml_escape(ch.title)}</text></navLabel><content src="content/s{i}.xhtml" /></navPoint>'''
        for i, ch in enumerate(chapters, 1)
    )
    ncx = f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="{book_id}" /><meta name="dtb:depth" content="1" /><meta name="dtb:totalPageCount" content="0" /><meta name="dtb:maxPageNumber" content="0" /></head>
  <docTitle><text>{xhtml_escape(book_title)}</text></docTitle>
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
        f'<li><a href="content/s{i}.xhtml">{xhtml_escape(ch.title)}</a></li>'
        for i, ch in enumerate(chapters, 1)
    )

    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPF/ebook.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPF/navigation.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPF/css/ebook.css", css, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPF/toc.xhtml", page("Table of Contents", f'<h1>Table of Contents</h1><ol class="toc">{toc_items}</ol>'), compress_type=zipfile.ZIP_DEFLATED)
        for i, ch in enumerate(chapters, 1):
            body = (
                f"<h1>{xhtml_escape(ch.title)}</h1>\n"
                f'<div class="source-url">Source: <a href="{xhtml_escape(ch.source_url)}">{xhtml_escape(ch.source_url)}</a></div>\n'
                + "\n".join(paragraph_html(p) for p in ch.paragraphs)
            )
            z.writestr(f"OEBPF/content/s{i}.xhtml", page(ch.title, body), compress_type=zipfile.ZIP_DEFLATED)


def default_output(title: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "", title).strip() or "local-reader"
    return Path.home() / "Downloads" / "local-url-epubs" / f"{safe}.epub"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build a local EPUB from accessible article URLs.")
    parser.add_argument("urls_file", type=Path)
    parser.add_argument("--title", default="Local URL Reader")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-words", type=int, default=350)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args(argv)

    urls = read_urls(args.urls_file)
    if not urls:
        print("No URLs found.", file=sys.stderr)
        return 2

    chapters: list[Chapter] = []
    results: list[Result] = []
    for index, url in enumerate(urls, 1):
        print(f"[{index}/{len(urls)}] fetching {url}")
        try:
            raw, final_url = fetch_html(url)
            chapter = extract_article(raw, url, final_url, args.min_words)
            chapters.append(chapter)
            results.append(Result(url=url, ok=True, fetched_url=final_url, title=chapter.title, word_count=chapter.word_count))
            print(f"  ok: {chapter.title} ({chapter.word_count} words)")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            results.append(Result(url=url, ok=False, error=str(exc)))
            print(f"  failed: {exc}")

    output = args.output or default_output(args.title)
    report_path = output.with_suffix(".report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")

    failures = [r for r in results if not r.ok]
    if failures and not args.allow_partial:
        print(f"Failed {len(failures)} URL(s); no EPUB written. Report: {report_path}", file=sys.stderr)
        return 1
    if not chapters:
        print(f"No chapters extracted; no EPUB written. Report: {report_path}", file=sys.stderr)
        return 1

    write_epub(chapters, output, args.title)
    with zipfile.ZipFile(output) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError(f"EPUB zip validation failed at {bad}")
    print(f"Wrote {output}")
    print(f"Report {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
