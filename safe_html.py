"""
Small, dependency-free helpers for user-written content (admin articles).

* markdown_to_html(md): a safe Markdown subset -> HTML. Raw HTML in the input
  is escaped (never passed through), links and images must be http(s) or
  site-relative, and links to other sites get rel="nofollow noopener".
  Supported: # headings (1-4), paragraphs, **bold**, *italic*, `code`,
  fenced code blocks, [links](url), ![images](url), - / * / 1. lists,
  > quotes, --- rules, and simple | tables |.
* strip_tags(html) -> plain text; slugify(text) -> url slug.
"""
import html
import re
import unicodedata

_URL_OK = re.compile(r"^(https?://[^\s<>\"']+|/[^\s<>\"']*)$", re.I)


def slugify(text, max_len=80):
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return (t[:max_len].rstrip("-")) or "article"


def strip_tags(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", str(s or ""), flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _safe_url(u):
    u = (u or "").strip()
    return u if _URL_OK.match(u) else None


def _inline(text):
    """Inline Markdown on ALREADY-ESCAPED text."""
    codes = []

    def keep_code(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    t = re.sub(r"`([^`]+)`", keep_code, text)

    def img(m):
        alt, url = m.group(1), _safe_url(html.unescape(m.group(2)))
        if not url:
            return alt
        return f'<img src="{html.escape(url, quote=True)}" alt="{alt}" loading="lazy">'

    def link(m):
        label, url = m.group(1), _safe_url(html.unescape(m.group(2)))
        if not url:
            return label
        ext = url.lower().startswith("http")
        rel = ' rel="nofollow noopener" target="_blank"' if ext else ""
        return f'<a href="{html.escape(url, quote=True)}"{rel}>{label}</a>'

    t = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", img, t)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"<em>\1</em>", t)
    t = re.sub(r"(?<![_\w])_([^_\n]+)_(?![_\w])", r"<em>\1</em>", t)
    t = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", t)
    return t


def markdown_to_html(md):
    lines = html.escape(str(md or "").replace("\r\n", "\n"), quote=True).split("\n")
    out, i, n = [], 0, len(lines)
    para = []

    def flush():
        if para:
            out.append("<p>" + _inline(" ".join(x.strip() for x in para)) + "</p>")
            para.clear()

    while i < n:
        ln = lines[i]
        s = ln.strip()
        if s.startswith("```"):
            flush()
            j = i + 1
            code = []
            while j < n and not lines[j].strip().startswith("```"):
                code.append(lines[j])
                j += 1
            out.append("<pre><code>" + "\n".join(code) + "</code></pre>")
            i = j + 1
            continue
        if not s:
            flush()
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            flush()
            lvl = len(m.group(1)) + 1 if len(m.group(1)) < 4 else 4    # '#' -> h2 (the page has the h1)
            out.append(f"<h{lvl}>{_inline(m.group(2).strip())}</h{lvl}>")
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,})$", s):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if s.startswith("&gt;"):
            flush()
            q = []
            while i < n and lines[i].strip().startswith("&gt;"):
                q.append(lines[i].strip()[4:].strip())
                i += 1
            out.append("<blockquote><p>" + _inline(" ".join(q)) + "</p></blockquote>")
            continue
        if re.match(r"^([-*+])\s+", s) or re.match(r"^\d+[.)]\s+", s):
            flush()
            ordered = bool(re.match(r"^\d+[.)]\s+", s))
            items = []
            while i < n:
                t = lines[i].strip()
                m2 = re.match(r"^\d+[.)]\s+(.*)$", t) if ordered else re.match(r"^[-*+]\s+(.*)$", t)
                if not m2:
                    break
                items.append(f"<li>{_inline(m2.group(1))}</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        if s.startswith("|") and i + 1 < n and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush()
            head = [c.strip() for c in s.strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{_inline(c)}</th>" for c in head)
            tb = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<div class=\"table-wrap\"><table><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table></div>")
            continue
        para.append(ln)
        i += 1
    flush()
    return "\n".join(out)
