#!/usr/bin/env python3
"""Extract ordered LaTeXML blocks, or text from the original arXiv PDF."""

import argparse
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org", "static.arxiv.org"}
PAPER_ID = r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})(?:v[1-9]\d*)?"
VOID = set("area base br col embed hr img input link meta param source track wbr".split())
DROP = set("script style template iframe frame frameset noscript svg audio video canvas embed form input textarea select button title base link meta".split())
HTML = set("a abbr b bdi blockquote br caption cite code col colgroup dd del details dfn div dl dt em figcaption figure h1 h2 h3 h4 h5 h6 hr i img kbd li mark ol p pre q s samp small span strong sub summary sup table tbody td tfoot th thead tr u ul var wbr".split())
MATH = set("math semantics annotation mrow mi mn mo mtext mspace ms mglyph mfrac msqrt mroot mstyle merror mpadded mphantom mfenced menclose msub msup msubsup munder mover munderover mmultiscripts mprescripts none mtable mlabeledtr mtr mtd maligngroup malignmark mstack mlongdiv msgroup msrow mscarries mscarry msline".split())
MATH_ATTRS = set("alttext display intent arg encoding accent accentunder align bevelled charalign columnalign columnlines columnspacing columnspan depth dir displaystyle fence form frame framespacing height largeop length linebreak linethickness location lspace mathbackground mathcolor mathsize mathvariant maxsize minsize movablelimits notation numalign denomalign open close separators position rowalign rowlines rowspacing rowspan rspace scriptlevel scriptminsize scriptsizemultiplier selection separator shift side stretchy symmetric voffset width".split())
ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,159}\Z")
PLACEHOLDER = re.compile(r"\{\{\d+\}\}")
IMAGE_SUFFIX = re.compile(r"\.(?:png|jpe?g|gif|webp|avif|svg)\Z", re.I)
HEADING = re.compile(r"h([1-6])\Z")
CAPTION = {"caption", "figcaption"}
LIST_CONTAINERS = {"ol", "ul"}
LIST_ATTRIBUTES = {"ol": ("start", "type", "reversed"), "ul": (), "li": ("value",)}
SHARED_TAG = {"table", "figure", "pre", "img", "object", "hr"}
SHARED_CLASS = {"ltx_equation", "ltx_equationgroup", "ltx_listing", "ltx_biblist", "ltx_bibitem"}
SKIP_CLASS = {"ltx_authors", "ltx_date", "ltx_pagination", "ltx_page_footer", "ltx_page_header", "ltx_TOC", "ltx_ERROR"}


class IngestError(Exception):
    """A paper or dependency could not be read."""


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    @property
    def classes(self):
        return set(self.attrs.get("class", "").split())


class TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            for index in range(len(self.stack) - 1, 0, -1):
                open_tag = self.stack[index].tag
                if open_tag in LIST_CONTAINERS:
                    break
                if open_tag == "li":
                    del self.stack[index:]
                    break
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def nodes(root):
    yield root
    for child in root.children:
        if isinstance(child, Node):
            yield from nodes(child)


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()


def plain(node, omit=frozenset()):
    if isinstance(node, str):
        return node
    if node.tag in omit or node.tag in DROP - {"title"}:
        return ""
    if node.tag == "br":
        return "\n"
    return "".join(plain(child, omit) for child in node.children)


def paper_urls(url):
    parts = urlsplit(url)
    match = re.fullmatch(rf"/(?:abs|html|pdf)/({PAPER_ID})(?:\.pdf)?/?", parts.path)
    if parts.scheme not in {"http", "https"} or parts.netloc.lower() not in ARXIV_HOSTS or not match:
        raise IngestError("Use an arXiv paper URL in /abs/, /html/, or /pdf/ form.")
    paper_id = match[1]
    return tuple(f"https://arxiv.org/{kind}/{paper_id}" for kind in ("abs", "html", "pdf"))


def safe_url(raw, base, *, image=False):
    raw = raw.strip()
    if not raw or re.search(r"[\x00-\x20\x7f\\]", raw):
        return None
    if raw.startswith("#") and not image:
        fragment = unquote(raw[1:])
        return f"#{fragment}" if ID.fullmatch(fragment) else None
    try:
        parts = urlsplit(urljoin(base, raw))
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            return None
        if image and (parts.scheme != "https" or parts.netloc.lower() not in ARXIV_HOSTS
                      or not IMAGE_SUFFIX.search(parts.path)):
            return None
    except ValueError:
        return None
    return urlunsplit(parts)


def attr_html(attrs):
    return "".join(f' {key}="{escape(value, quote=True)}"' for key, value in attrs.items())


class Extractor:
    def __init__(self, base):
        self.base = base
        self.blocks = []
        self.ids = set()
        self.reserved = set()
        self.counter = 0

    def claim(self, candidate):
        if candidate and ID.fullmatch(candidate) and candidate not in self.ids:
            self.ids.add(candidate)
            return candidate
        return None

    def block_id(self, node, inherited=None):
        found = self.claim(node.attrs.get("id")) or self.claim(inherited)
        if found:
            return found
        while True:
            self.counter += 1
            candidate = f"paper-block-{self.counter}"
            if candidate not in self.reserved:
                return self.claim(candidate)

    def attributes(self, node, tag, *, root=False, math=False):
        out = {}
        if not root and (anchor := self.claim(node.attrs.get("id"))):
            out["id"] = anchor
        classes = [name for name in node.attrs.get("class", "").split()
                   if re.fullmatch(r"ltx_[a-zA-Z0-9_-]+", name)]
        if classes:
            out["class"] = " ".join(classes)
        for key in ("title", "aria-label"):
            if key in node.attrs:
                out[key] = node.attrs[key]
        if tag == "a" and (href := safe_url(node.attrs.get("href", ""), self.base)):
            out["href"] = href
        if tag == "img":
            out["src"] = safe_url(node.attrs.get("data" if node.tag == "object" else "src", ""),
                                  self.base, image=True)
            if not out["src"]:
                return None
            out.update(alt=node.attrs.get("alt", "Figure"), loading="lazy", referrerpolicy="no-referrer")
        numeric = {"img": ("width", "height"), "td": ("rowspan", "colspan"),
                   "th": ("rowspan", "colspan"), "col": ("span",), "colgroup": ("span",)}
        for key in numeric.get(tag, ()):
            if re.fullmatch(r"[0-9]{1,5}", value := node.attrs.get(key, "")):
                out[key] = value
        if tag in {"ol", "li"}:
            key = "start" if tag == "ol" else "value"
            if re.fullmatch(r"[+-]?[0-9]+", value := node.attrs.get(key, "").strip()):
                out[key] = value
        if tag == "ol":
            if node.attrs.get("type") in {"1", "a", "A", "i", "I"}:
                out["type"] = node.attrs["type"]
            if "reversed" in node.attrs:
                out["reversed"] = ""
        if tag == "th" and node.attrs.get("scope") in {"row", "col", "rowgroup", "colgroup"}:
            out["scope"] = node.attrs["scope"]
        # LaTeXML uses these properties for meaningful highlights in comparison tables.
        if tag in {"td", "th", "span"}:
            colors = re.findall(r"(?:^|;)\s*(--ltx-bg-color|--ltx-text-color|background-color|color)\s*:\s*(#[0-9a-fA-F]{3,8})\s*(?=;|$)", node.attrs.get("style", ""))
            if colors:
                out["style"] = ";".join(f"{key} : {value}".replace("--ltx-bg-color", "background-color").replace("--ltx-text-color", "color") for key, value in colors)
        if math:
            out.update((key, value) for key, value in node.attrs.items() if key in MATH_ATTRS)
        return out

    def fragment(self, node, *, root=False, math=False, captions=None):
        if isinstance(node, str):
            return escape(node)
        if node.tag in DROP or node.classes & SKIP_CLASS or node.tag == "annotation-xml":
            return ""
        if node.tag in CAPTION and captions is not None and not root:
            captions.append(node)
            return ""
        if node.tag == "annotation":
            if not math or node.attrs.get("encoding") != "application/x-tex":
                return ""
            return f'<annotation encoding="application/x-tex">{escape(plain(node))}</annotation>'
        math = math or node.tag == "math"
        tag = node.tag
        if tag == "object":
            if node.attrs.get("type") not in {"image/svg+xml", "image/png", "image/jpeg", "image/gif"}:
                return ""
            tag = "img"
        if tag not in (MATH if math else HTML):
            return "".join(self.fragment(child, math=math, captions=captions) for child in node.children)
        attrs = self.attributes(node, tag, root=root, math=math)
        if attrs is None:
            return ""
        if tag in VOID:
            return f"<{tag}{attr_html(attrs)}>"
        inner = "".join(self.fragment(child, math=math, captions=captions) for child in node.children)
        return f"<{tag}{attr_html(attrs)}>{inner}</{tag}>"

    def translated(self, node, kind, inherited=None):
        for key in (node.attrs.get("id"), inherited):
            if key and ID.fullmatch(key):
                self.reserved.add(key)
        anchor = self.block_id(node, inherited)
        tokens = []

        def token(fragment):
            tokens.append(fragment)
            return "{{%d}}" % len(tokens)

        def inline(child):
            if isinstance(child, str):
                return PLACEHOLDER.sub(lambda match: token(escape(match[0])), child)
            if child.tag in DROP or child.classes & SKIP_CLASS:
                return ""
            if child.tag in {"a", "cite", "code", "math", "img", "object", "sup", "sub"} or "ltx_tag" in child.classes:
                return token(fragment) if (fragment := self.fragment(child)) else ""
            if child.tag == "br":
                return " "
            return "".join(inline(item) for item in child.children)

        text = clean_text("".join(inline(child) for child in node.children))
        if text:
            block = dict(id=anchor, kind=kind, text=text, tokens=tokens)
            if alias := self.claim(inherited):
                block["aliases"] = [alias]
            if kind == "heading":
                block["level"] = int(HEADING.fullmatch(node.tag)[1])
            self.blocks.append(block)
        else:
            self.ids.remove(anchor)

    def shared(self, node):
        anchor = self.block_id(node)
        captions = []
        fragment = self.fragment(node, root=True, captions=captions)
        if fragment.strip():
            self.blocks.append(dict(id=anchor, kind="shared", tokens=[], html=fragment))
        else:
            self.ids.remove(anchor)
        for caption in captions:
            self.translated(caption, "caption")

    def list_start(self, node, inherited=None):
        attrs = self.attributes(node, node.tag)
        event = dict(kind="list_start", tag=node.tag,
                     attrs={key: attrs[key] for key in LIST_ATTRIBUTES[node.tag] if key in attrs})
        anchor = attrs.get("id") or self.claim(inherited)
        if anchor:
            event["id"] = anchor
        if alias := self.claim(inherited):
            event["aliases"] = [alias]
        self.blocks.append(event)
        return event

    def list_item(self, node):
        event = self.list_start(node)
        aliases = []

        def content(parent):
            children = []
            for child in parent.children:
                if isinstance(child, str) or child.tag in LIST_CONTAINERS:
                    children.append(child)
                elif "ltx_tag_item" in child.classes:
                    for marker in nodes(child):
                        if alias := self.claim(marker.attrs.get("id")):
                            aliases.append(alias)
                else:
                    children.append(content(child))
            return Node(parent.tag, parent.attrs, children)

        item = content(node)
        if aliases:
            event["aliases"] = aliases
        self.visit_children(item)
        self.blocks.append(dict(kind="list_end", tag="li"))

    def visit(self, node, inherited=None):
        if node.tag in DROP or node.tag in {"nav", "footer"} or node.classes & SKIP_CLASS:
            return
        if HEADING.fullmatch(node.tag):
            self.translated(node, "heading", inherited)
        elif node.tag in CAPTION:
            self.translated(node, "caption")
        elif (node.tag in SHARED_TAG or node.classes & SHARED_CLASS
              or node.tag == "math" and node.attrs.get("display") == "block"):
            self.shared(node)
        elif node.tag in LIST_CONTAINERS:
            self.list_start(node, inherited)
            for child in node.children:
                if isinstance(child, Node) and child.tag == "li" and not child.classes & SKIP_CLASS:
                    self.list_item(child)
            self.blocks.append(dict(kind="list_end", tag=node.tag))
        elif node.tag == "p" or node.tag == "blockquote" and not any(
            child.tag in {"p", *LIST_CONTAINERS} for child in nodes(node)
        ):
            self.translated(node, "prose", inherited)
        else:
            self.visit_children(node, node.attrs.get("id") or inherited)

    def visit_children(self, node, alias=None):
        pending = []
        for child in node.children:
            if isinstance(child, str) or child.tag in {"a", "b", "br", "cite", "code", "em", "i", "span", "strong", "sub", "sup"} or (child.tag == "math" and child.attrs.get("display") != "block"):
                pending.append(child)
                continue
            if pending and self.translated_inline(pending, alias):
                alias = None
            pending = []
            self.visit(child, alias)
            if alias in self.ids:
                alias = None
        if pending:
            self.translated_inline(pending, alias)

    def translated_inline(self, pending, alias):
        if any(isinstance(item, Node) or item.strip() for item in pending):
            self.translated(Node("p", children=pending), "prose", alias)
        return alias in self.ids

    def extract(self, root):
        self.reserved.update(node.attrs["id"] for node in nodes(root) if "id" in node.attrs)
        self.visit(root)

        def fix_links(fragment):
            return re.sub(r'href="#([A-Za-z0-9_.:-]+)"', lambda match: match[0] if match[1] in self.ids
                          else f'href="{escape(self.base, quote=True)}#{match[1]}"', fragment)

        for block in self.blocks:
            if "html" in block:
                block["html"] = fix_links(block["html"])
            if "tokens" in block:
                block["tokens"] = [fix_links(fragment) for fragment in block["tokens"]]
        return self.blocks


def metadata(root):
    data = {}
    for node in nodes(root):
        if node.tag == "meta" and node.attrs.get("content"):
            key = (node.attrs.get("name") or node.attrs.get("property", "")).lower()
            data.setdefault(key, []).append(clean_text(node.attrs["content"]))
        if node.tag in {"link", "a"} and "license" in node.attrs.get("rel", "").lower().split():
            if value := node.attrs.get("href"):
                data.setdefault("license", []).append(value)
    return data


def author_names(document):
    names = []
    for node in nodes(document):
        if "ltx_personname" in node.classes:
            lines = []
            for line in plain(node, {"sup"}).split("\n"):
                line = clean_text(line)
                if not line:
                    continue
                if lines and not lines[-1].endswith(","):
                    break
                lines.append(line)
            if lines:
                names.append(" ".join(lines))
    return ", ".join(names)


def paper_license(meta, base):
    for key in ("license", "dc.rights", "dcterms.license", "citation_license"):
        for value in meta.get(key, []):
            if value.startswith(("http://", "https://", "/")):
                if url := safe_url(value, base):
                    return url
            elif value:
                return value
    return None


def html_paper(html, source, base):
    parser = TreeParser()
    parser.feed(html)
    parser.close()
    tree = parser.root
    document = next((node for node in nodes(tree) if "ltx_document" in node.classes), None)
    if document is None:
        document = next((node for node in nodes(tree) if node.tag == "article"
                         and any(HEADING.fullmatch(item.tag) for item in nodes(node))), None)
    if document is None:
        raise IngestError("The HTML response has no paper article.")
    meta = metadata(tree)
    title = next((clean_text(plain(node)) for node in nodes(document)
                  if "ltx_title_document" in node.classes), "")
    if not title:
        title = (next(iter(meta.get("citation_title", [])), "")
                 or next((clean_text(plain(node)) for node in nodes(document) if node.tag == "h1"), "")
                 or next((clean_text(plain(node)) for node in nodes(tree) if node.tag == "title"), ""))
    blocks = Extractor(base).extract(document)
    if not title or not any(block["kind"] == "prose" for block in blocks):
        raise IngestError("The HTML article has no title or extractable prose.")
    authors = ", ".join(meta.get("citation_author", [])) or author_names(document)
    paper = dict(title=title, authors=authors, source=source, format="html", blocks=blocks)
    if license_value := paper_license(meta, base):
        paper["license"] = license_value
    return paper


class ArxivRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlsplit(newurl)
        if parts.scheme != "https" or parts.netloc.lower() not in ARXIV_HOSTS:
            raise IngestError(f"arXiv redirected to an unsupported URL: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, *, limit):
    try:
        request = Request(url, headers={"User-Agent": "arxiv-bilingual-html/1.0"})
        with build_opener(ArxivRedirectHandler()).open(request, timeout=30) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise IngestError(f"Response exceeds the {limit // (1024 * 1024)} MiB limit: {url}")
            return data, response.headers.get_content_charset() or "utf-8", response.url
    except (HTTPError, URLError, TimeoutError) as error:
        raise IngestError(f"Could not retrieve {url}: {error}") from error


def literal_text(text):
    tokens = []

    def preserve(match):
        tokens.append(escape(match[0]))
        return "{{%d}}" % len(tokens)

    return dict(text=PLACEHOLDER.sub(preserve, text), tokens=tokens)


def pdf_paper(source, pdf, *, html_error=None, pdf_file=None):
    executable = shutil.which("pdftotext")
    if not executable:
        raise IngestError(f"Install Poppler and put pdftotext on PATH to extract PDF text. Original PDF: {pdf}")
    if pdf_file is None:
        data, _, _ = fetch(pdf, limit=100 * 1024 * 1024)
    else:
        data = pdf_file.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise IngestError(f"arXiv returned an unexpected response for the original PDF: {pdf}")
    with tempfile.TemporaryDirectory() as temp:
        local_pdf = Path(temp) / "paper.pdf"
        local_pdf.write_bytes(data)
        try:
            result = subprocess.run([executable, "-enc", "UTF-8", str(local_pdf), "-"],
                                    capture_output=True, check=True, timeout=120)
        except subprocess.CalledProcessError as error:
            detail = clean_text(error.stderr.decode("utf-8", errors="replace"))
            raise IngestError(f"pdftotext failed (status {error.returncode}): {detail}. Original PDF: {pdf}") from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise IngestError(f"Could not run pdftotext: {error}. Original PDF: {pdf}") from error
    pages = result.stdout.decode("utf-8-sig").split("\f")
    blocks = []
    for number, content in enumerate(pages, 1):
        paragraphs = [text for part in re.split(r"\n\s*\n", content) if (text := clean_text(part))]
        if not paragraphs:
            continue
        blocks.append(dict(id=f"pdf-page-{number}", kind="heading", level=2, text=f"Page {number} {{{{1}}}}",
                           tokens=[f'<a href="{escape(pdf, quote=True)}#page={number}">PDF</a>']))
        for index, text in enumerate(paragraphs, 1):
            blocks.append(dict(id=f"pdf-page-{number}-p{index}", kind="prose", **literal_text(text)))
    if not blocks:
        raise IngestError(f"The PDF has no extractable text. Original PDF: {pdf}")
    # Abstract metadata supplies the complete title, author list and license when reachable.
    meta = {}
    if pdf_file is None:
        try:
            abstract, charset, _ = fetch(source, limit=4 * 1024 * 1024)
        except IngestError as error:
            metadata_note = f" Abstract metadata unavailable: {error}"
        else:
            parser = TreeParser()
            parser.feed(abstract.decode(charset, errors="replace"))
            parser.close()
            meta = metadata(parser.root)
            metadata_note = ""
    else:
        metadata_note = ""
    note = "PDF text extraction; figures and layout are available in the original PDF."
    if html_error:
        note += f" HTML unavailable: {html_error}"
    title = next(iter(meta.get("citation_title", [])), "") or f"arXiv:{source.split('/abs/', 1)[1]}"
    blocks.insert(0, dict(id="pdf-title", kind="heading", level=1, **literal_text(title)))
    paper = dict(title=title, authors=", ".join(meta.get("citation_author", [])),
                 source=source, format="pdf", blocks=blocks, pdf_url=pdf, extraction_note=note + metadata_note)
    if license_value := paper_license(meta, source):
        paper["license"] = license_value
    return paper


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="arXiv /abs/, /html/, or /pdf/ paper URL")
    parser.add_argument("--output", required=True, type=Path, help="Destination JSON file")
    local = parser.add_mutually_exclusive_group()
    local.add_argument("--html-file", type=Path, help="Read a local LaTeXML HTML snapshot")
    local.add_argument("--pdf-file", type=Path, help="Extract a local PDF with pdftotext")
    args = parser.parse_args()
    try:
        source, html_url, pdf = paper_urls(args.url)
        if args.html_file:
            paper = html_paper(args.html_file.read_text(encoding="utf-8-sig"), source, html_url)
        elif args.pdf_file:
            paper = pdf_paper(source, pdf, pdf_file=args.pdf_file)
        else:
            try:
                data, charset, base = fetch(html_url, limit=32 * 1024 * 1024)
                paper = html_paper(data.decode(charset, errors="replace"), source, base)
            except IngestError as error:
                paper = pdf_paper(source, pdf, error)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(paper, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (IngestError, OSError, UnicodeError, ValueError) as error:
        parser.exit(1, f"ingest: {error}\n")


if __name__ == "__main__":
    main()
