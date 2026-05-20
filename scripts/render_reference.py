"""Render the reference markdown files into styled HTML pages.

The reference source lives at docs/reference/*.md so contributors edit
markdown. This renderer produces site/reference/*.html with the same
chrome the rest of the site uses (sidebar nav, prose-heavy main), so
visitors get a navigable reference section on the deployed page
rather than raw GitHub-rendered markdown.

The markdown subset the reference files use is small: h1-h4, code
fences with language hints, ordered/unordered lists, paragraphs,
inline code, inline links, and the occasional bold/italic. A
hand-rolled converter handles it; no third-party dependency.
"""

from __future__ import annotations

import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "docs" / "reference"
DEST = ROOT / "site" / "reference"


# Ordering controls both the sidebar nav and the index display order.
PAGES: List[Tuple[str, str]] = [
    ("index", "Reference home"),
    ("world-api", "World API"),
    ("scenarios", "Scenarios"),
    ("tools", "Tools"),
    ("predicates", "Predicates"),
    ("personas", "Personas"),
    ("cli", "CLI"),
    ("runtime", "Runtime"),
    ("traces", "Traces"),
]


@dataclass
class Section:
    """One H2 inside a rendered page, used to build the in-page nav."""

    anchor: str
    title: str


def slugify(text: str) -> str:
    """Turn an H2/H3 title into a stable anchor id.

    Underscores and whitespace fold to hyphens so a title like
    ``register_world`` becomes ``register-world`` rather than
    ``registerworld``. Dots, parens, and other punctuation drop
    out, matching the convention the existing site uses for
    section ids. Markdown link syntax in the heading is reduced
    to its label before slugging so ``## [World API](world-api.md)``
    becomes ``world-api`` rather than ``world-apiworld-apimd``."""
    # Strip markdown link syntax, keeping just the label.
    slug = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Drop inline code backticks so anchors don't carry them.
    slug = slug.replace("`", "")
    slug = slug.strip().lower()
    slug = slug.replace("_", "-")
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


def render_inline(text: str) -> str:
    """Convert the inline markdown subset to HTML on a single line.

    The order matters: code spans win over links, so a literal
    backticked URL doesn't get treated as a markdown link. We tokenise
    code spans first, replace them with placeholders, then run the
    other replacements on the surrounding prose, then splice the
    code spans back in.
    """
    spans: List[str] = []

    def stash_code(match: re.Match) -> str:
        spans.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash_code, text)
    text = html.escape(text)

    # Restore the stashed placeholders before further substitution so
    # the angle brackets in <code>...</code> don't get escaped twice.
    text = re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)

    # Links: rewrite .md targets to .html so cross-references inside
    # the reference section land on the rendered pages, not the raw
    # markdown sources.
    def rewrite_link(match: re.Match) -> str:
        label = match.group(1)
        target = match.group(2)
        if target.endswith(".md"):
            target = target[: -len(".md")] + ".html"
        elif "#" in target and target.split("#", 1)[0].endswith(".md"):
            base, frag = target.split("#", 1)
            target = base[: -len(".md")] + ".html#" + frag
        return f'<a href="{html.escape(target)}">{label}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", rewrite_link, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", text)
    return text


def render_body(md: str) -> Tuple[str, List[Section]]:
    """Convert the markdown body to HTML and return the H2 outline."""
    lines = md.splitlines()
    out: List[str] = []
    sections: List[Section] = []
    i = 0

    def close_list(stack: List[str]) -> None:
        while stack:
            out.append(f"</{stack.pop()}>")

    list_stack: List[str] = []
    paragraph: List[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            joined = " ".join(paragraph).strip()
            if joined:
                out.append(f"<p>{render_inline(joined)}</p>")
            paragraph.clear()

    while i < len(lines):
        line = lines[i]

        # Fenced code block.
        fence = re.match(r"^```\s*([A-Za-z0-9_+.\-]*)\s*$", line)
        if fence:
            close_list(list_stack)
            flush_paragraph()
            lang = fence.group(1)
            body: List[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            klass = f' class="language-{html.escape(lang)}"' if lang else ""
            content = html.escape("\n".join(body))
            out.append(f"<pre><code{klass}>{content}</code></pre>")
            continue

        # Headings.
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            close_list(list_stack)
            flush_paragraph()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if level == 1:
                out.append(f"<h1>{render_inline(title)}</h1>")
            else:
                anchor = slugify(title)
                if level == 2:
                    sections.append(Section(anchor=anchor, title=title))
                out.append(
                    f'<h{level} id="{anchor}">{render_inline(title)}</h{level}>'
                )
            i += 1
            continue

        # List item: unordered with `-` or ordered with `\d+\.`.
        unordered = re.match(r"^(\s*)-\s+(.*)$", line)
        ordered = re.match(r"^(\s*)\d+\.\s+(.*)$", line)
        if unordered or ordered:
            flush_paragraph()
            tag = "ul" if unordered else "ol"
            content = (unordered or ordered).group(2).strip()
            if not list_stack or list_stack[-1] != tag:
                close_list(list_stack)
                out.append(f"<{tag}>")
                list_stack.append(tag)
            # Look ahead for continuation lines belonging to the same
            # item; markdown lets a paragraph wrap across lines so long
            # as the next line isn't blank, a new bullet, or a fence.
            pieces = [content]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if (
                    nxt.strip() == ""
                    or re.match(r"^(\s*)-\s+", nxt)
                    or re.match(r"^(\s*)\d+\.\s+", nxt)
                    or nxt.startswith("```")
                    or re.match(r"^#{1,4}\s+", nxt)
                ):
                    break
                pieces.append(nxt.strip())
                j += 1
            out.append(f"<li>{render_inline(' '.join(pieces))}</li>")
            i = j
            continue

        # Blank line ends a paragraph; everything else accumulates.
        if line.strip() == "":
            close_list(list_stack)
            flush_paragraph()
            i += 1
            continue

        paragraph.append(line.strip())
        i += 1

    close_list(list_stack)
    flush_paragraph()
    return "\n".join(out), sections


def render_sidebar(active_slug: str, sections: List[Section]) -> str:
    """Build the sidebar nav for a reference page.

    Three groups: a link back to each main site page, the list of
    reference pages with the current one highlighted, and the H2
    outline of the current page so a reader can jump within it.
    """
    site_links = [
        ("Overview", "../index.html"),
        ("Docs", "../docs.html"),
        ("Scenarios overview", "../scenarios.html"),
        ("Plank", "../plank.html"),
        ("Personas overview", "../personas.html"),
        ("MCP", "../mcp.html"),
    ]
    site_html = "\n".join(
        f'        <a href="{html.escape(href)}">{html.escape(label)}</a>'
        for label, href in site_links
    )

    ref_html_lines: List[str] = []
    for slug, label in PAGES:
        href = f"{slug}.html"
        active = ' class="active"' if slug == active_slug else ""
        ref_html_lines.append(
            f'        <a href="{html.escape(href)}"{active}>{html.escape(label)}</a>'
        )
    ref_html = "\n".join(ref_html_lines)

    in_page_html = ""
    if sections:
        section_lines = [
            f'        <a href="#{html.escape(s.anchor)}">{html.escape(s.title)}</a>'
            for s in sections
        ]
        in_page_html = f"""
      <div class="nav-group">
        <div class="nav-group-title">On this page</div>
{chr(10).join(section_lines)}
      </div>"""

    return f"""<aside class="sidebar">
    <a class="wordmark" href="../index.html">ensemble</a>
    <p class="tagline">Multi-user, multi-agent RL environments.</p>
    <nav>
      <div class="nav-group">
        <div class="nav-group-title">Site</div>
{site_html}
      </div>
      <div class="nav-group">
        <div class="nav-group-title">Reference</div>
{ref_html}
      </div>{in_page_html}
    </nav>
  </aside>"""


HLJS_SCRIPTS = """  <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/highlight.min.js"></script>
  <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/languages/rust.min.js"></script>
  <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/languages/python.min.js"></script>
  <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/languages/bash.min.js"></script>
  <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/languages/json.min.js"></script>
  <script src="https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.10.0/build/languages/ini.min.js"></script>
  <script>hljs.highlightAll();</script>"""


def wrap(slug: str, title: str, body_html: str, sections: List[Section]) -> str:
    sidebar = render_sidebar(slug, sections)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ensemble, {html.escape(title.lower())} reference</title>
  <link rel="stylesheet" href="https://use.typekit.net/bsk0vur.css">
  <link rel="stylesheet" href="../style.css">
</head>
<body>
  {sidebar}

  <main>
{body_html}
  </main>

{HLJS_SCRIPTS}
</body>
</html>
"""


def render_one(slug: str, title: str) -> Path:
    src_path = SRC / f"{slug}.md"
    if not src_path.exists():
        raise FileNotFoundError(src_path)
    md = src_path.read_text(encoding="utf-8")
    body_html, sections = render_body(md)
    page = wrap(slug, title, body_html, sections)
    DEST.mkdir(parents=True, exist_ok=True)
    out = DEST / f"{slug}.html"
    out.write_text(page, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    written: List[Path] = []
    for slug, title in PAGES:
        written.append(render_one(slug, title))
    for p in written:
        print(f"wrote {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
