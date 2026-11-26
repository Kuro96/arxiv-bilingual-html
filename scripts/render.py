#!/usr/bin/env python3
"""Render a local arXiv extraction and its aligned rewrite to a single HTML file."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
import sys

from batches import inventory
from ingest import IngestError, LIST_ATTRIBUTES, LIST_CONTAINERS, attr_html, fetch, safe_url

HERE = Path(__file__).resolve().parent
TOKEN = re.compile(r'\{\{([1-9][0-9]*)\}\}')
ID = re.compile(r'\s+id="[^"]*"')


def escape(value):
    return html.escape(value, quote=True)


def words(en, zh, cls='pair'):
    return (f'<span class="{cls}"><span class="lang-en" lang="en">{en}</span>'
            f'<span class="lang-zh" lang="zh-CN">{zh}</span></span>')


def label(en, zh):
    return words(escape(en), escape(zh), 'ui-pair')


def expand(value, fragments, chinese=False):
    return TOKEN.sub(lambda m: ID.sub('', fragments[int(m[1])-1]) if chinese
                     else fragments[int(m[1])-1], escape(value))


def pairs_for(block, paragraphs):
    where = block['id']
    if not isinstance(paragraphs, list) or not paragraphs:
        raise ValueError(f'{where}: expected a non-empty list of paragraphs')
    tokens = block.get('tokens', [])
    all_refs = []
    rendered = []
    for paragraph in paragraphs:
        if not isinstance(paragraph, list) or not paragraph:
            raise ValueError(f'{where}: expected a non-empty list of sentence pairs')
        group = []
        for pair in paragraph:
            if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(x, str) and x.strip() for x in pair):
                raise ValueError(f'{where}: each pair must contain non-empty English and Chinese strings')
            en, zh = pair
            expected = sorted(map(int, TOKEN.findall(en)))
            if expected != sorted(map(int, TOKEN.findall(zh))):
                raise ValueError(f'{where}: placeholder mismatch between languages: {en!r}')
            if any(i > len(tokens) for i in expected):
                raise ValueError(f'{where}: unknown placeholder in {en!r}')
            all_refs.extend(expected)
            group.append(words(expand(en, tokens), expand(zh, tokens, True)))
        rendered.append('<span class="en-space"> </span>'.join(group))
    if sorted(all_refs) != list(range(1, len(tokens)+1)):
        raise ValueError(f'{where}: every protected token must appear exactly once per language')
    return rendered


IMAGES = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")')
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.svg': 'image/svg+xml',
               '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif'}


def embed_images(document):
    urls = sorted({html.unescape(m[2]) for m in IMAGES.finditer(document) if m[2].startswith('https://')})

    def load(url):
        if safe_url(url, url, image=True) != url:
            return url, None
        mime = IMAGE_TYPES[Path(urlsplit(url).path).suffix.lower()]
        try:
            data, _, target = fetch(url, limit=20 * 1024 * 1024)
        except IngestError as error:
            print(f'Image remains linked: {error}', file=sys.stderr)
            return url, None
        target_mime = IMAGE_TYPES.get(Path(urlsplit(target).path).suffix.lower())
        if target_mime != mime:
            print(f'Image remains linked: unexpected redirect format for {url}', file=sys.stderr)
            return url, None
        return url, f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')

    with ThreadPoolExecutor(max_workers=6) as pool:
        local = {url: data for url, data in pool.map(load, urls) if data}
    return IMAGES.sub(lambda m: m[1] + local.get(html.unescape(m[2]), m[2]) + m[3], document)


def source_url(paper):
    url = paper['source']
    u = urlsplit(url)
    if u.scheme != 'https' or u.netloc not in ('arxiv.org', 'www.arxiv.org', 'export.arxiv.org', 'static.arxiv.org'):
        raise ValueError('Expected an HTTPS arXiv source URL')
    return url


def render(paper, translated):
    url = source_url(paper)
    blocks = paper['blocks']
    ids = [b['id'] for b in blocks if 'id' in b]
    ids.extend(alias for b in blocks for alias in b.get('aliases', []))
    if len(ids) != len(set(ids)):
        raise ValueError('Source extraction contains duplicate block IDs')
    required = {b['id'] for b in inventory(paper)}
    if not required or required != translated.keys():
        raise ValueError(f'Missing IDs: {sorted(required - translated.keys())}; extra IDs: {sorted(translated.keys() - required)}')
    toc, body, list_stack = [], [], []
    title_shown = False
    for block in blocks:
        kind = block['kind']
        aliases = ''.join(f'<span id="{escape(alias)}" class="paper-anchor"></span>'
                          for alias in block.get('aliases', []))
        parent = list_stack[-1] if list_stack else None
        if kind == 'list_start':
            tag = block['tag']
            if tag not in LIST_ATTRIBUTES or (tag == 'li') != (parent in LIST_CONTAINERS):
                raise ValueError(f'Invalid list structure: {tag!r} inside {parent!r}')
            attrs = {'class': 'paper-list'} if tag in LIST_CONTAINERS else {}
            if 'id' in block:
                attrs['id'] = block['id']
            attrs.update((key, value) for key, value in block['attrs'].items() if key in LIST_ATTRIBUTES[tag])
            opening = f'<{tag}{attr_html(attrs)}>'
            body.append(opening + aliases if tag == 'li' else aliases + opening)
            list_stack.append(tag)
            continue
        if kind == 'list_end':
            if parent is None or parent != block['tag']:
                raise ValueError(f'Invalid list structure: unexpected end of {block["tag"]!r}')
            body.append(f'</{list_stack.pop()}>')
            continue
        if parent in LIST_CONTAINERS:
            raise ValueError(f'Invalid list structure: {kind!r} outside a list item')
        key = block['id']
        location = escape(key)
        if kind == 'shared':
            body.append(f'<div id="{location}" class="shared">{block["html"]}</div>')
            continue
        values = translated[key]
        groups = pairs_for(block, values)
        if kind == 'heading':
            if len(groups) != 1 or len(values[0]) != 1:
                raise ValueError(f'{key}: a heading requires a single pair')
            level = int(block.get('level', 2))
            if not 1 <= level <= 6:
                raise ValueError(f'{key}: invalid heading level')
            title_shown |= level == 1
            body.append(f'<h{level} id="{location}">{aliases}{groups[0]}</h{level}>')
            def toc_text(value):
                def tag_text(match):
                    fragment = block['tokens'][int(match[1])-1]
                    if 'ltx_tag' in fragment and '<math' not in fragment:
                        return html.unescape(re.sub(r'<[^>]*>', '', fragment))
                    return ''
                return TOKEN.sub(tag_text, value).strip()
            en, zh = map(toc_text, values[0][0])
            if level >= 2:
                toc.append(f'<li class="level-{level}"><a href="#{location}">{label(en, zh)}</a></li>')
        elif kind in ('prose', 'caption'):
            attrs = f' id="{location}" class="{kind}"'
            continuation = f' class="{kind}"'
            body.append(f'<p{attrs}>{aliases}{groups[0]}</p>')
            body.extend(f'<p{continuation}>{text}</p>' for text in groups[1:])
        else:
            raise ValueError(f'{key}: unsupported block kind {kind!r}')
    if list_stack:
        raise ValueError(f'Invalid list structure: unclosed {list_stack[-1]!r}')
    license_text = escape(paper.get('license') or '')
    header = (f'<h1>{escape(paper["title"])}</h1>' if not title_shown else '')
    authors = paper.get('authors') or ''
    meta = (f'<p class="byline">{escape(authors)}</p>' if authors else '')
    meta += f'<p><a href="{escape(url)}">{label("Original paper", "原始论文")}</a>' + (f' · {license_text}' if license_text else '') + '</p>'
    note = (label('Simplified English rewrite and Chinese translation.', '简明英语改写与中文翻译。'))
    if paper.get('format') == 'pdf':
        note += '<p>' + label('This edition uses text extracted from the PDF. For original layout, tables, and equations, follow the PDF page links.', '本版根据 PDF 提取的文字制作。原始排版、表格与公式请参阅各页的 PDF 链接。') + '</p>'
    css = (HERE/'reader.css').read_text(encoding='utf8')
    js = (HERE/'reader.js').read_text(encoding='utf8')
    digest = base64.b64encode(hashlib.sha256(js.encode()).digest()).decode()
    csp = f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; img-src data: https://arxiv.org https://www.arxiv.org https://export.arxiv.org https://static.arxiv.org; base-uri 'none'; form-action 'none'"
    return f'''<!doctype html>
<html lang="en" data-view="both"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="{escape(csp)}"><title>{escape(paper['title'])} — English / 中文</title><style>{css}</style></head>
<body><a class="skip" href="#paper">{label('Skip to paper','跳转到正文')}</a><div class="mode-bar" role="group" aria-label="Reading mode / 阅读模式"><button type="button" data-mode="en" aria-pressed="false" lang="en">English</button><button type="button" data-mode="zh" aria-pressed="false" lang="zh-CN">中文</button><button type="button" data-mode="both" aria-pressed="true"><span lang="zh-CN">逐句双语</span> / <span lang="en">Bilingual</span></button></div>
<div class="layout"><aside><details open><summary>{label('Contents','目录')}</summary><nav aria-label="Contents / 目录"><ol>{''.join(toc)}</ol></nav></details></aside><main id="paper"><header class="source-note">{note}{meta}</header><article>{header}{''.join(body)}</article></main></div><script>{js}</script></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paper', type=Path)
    parser.add_argument('translations', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--link-images', action='store_true', help='Keep figure URLs without downloading them')
    args = parser.parse_args()
    paper = json.loads(args.paper.read_text(encoding='utf-8-sig'))
    translations = json.loads(args.translations.read_text(encoding='utf-8-sig'))
    result = render(paper, translations)
    if not args.link_images:
        result = embed_images(result)
    args.output.write_text(result, encoding='utf-8')
    print(f'{args.output}: {len(translations)} translated blocks; {len(result.encode()):,} bytes')


if __name__ == '__main__':
    main()
