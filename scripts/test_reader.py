import unittest
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from batches import inventory, merge, read, split, write
from ingest import html_paper, paper_urls, IngestError, Node, TreeParser, nodes
from render import render


class IDs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
    def handle_starttag(self, tag, attrs):
        if anchor := dict(attrs).get('id'):
            self.ids.append(anchor)


class ReaderTest(unittest.TestCase):
    def setUp(self):
        source, html, _ = paper_urls('https://arxiv.org/pdf/2606.13108v1')
        self.assertEqual(urlsplit(html).path, '/html/2606.13108v1')
        self.paper = html_paper('''<!doctype html><html><head><title>OCR report</title></head><body>
<article class="ltx_document"><h1 id="title">OCR report</h1><section id="S1"><h2>Introduction</h2>
<p id="p">The kernel is <math id="m"><mn>3</mn></math>. See <a href="#f">the table</a>.
<script>throw new Error('source script');</script></p>
<figure id="f"><table><tr><td>86.2</td></tr></table><figcaption id="caption">Detection accuracy.</figcaption></figure>
</section></article></body></html>''', source, html)
        self.translations = {
            'title': [[['OCR report', 'OCR 报告']]],
            'S1': [[['Introduction', '引言']]],
            'p': [[['The kernel size is {{1}}.', '卷积核大小为 {{1}}。'], ['See {{2}}.', '见{{2}}。']]],
            'caption': [[['Detection accuracy.', '检测准确率。']]],
        }

    def test_modes_and_source_document(self):
        result = render(self.paper, self.translations)
        parser = IDs()
        parser.feed(result)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertIn('href="#S1"', result)
        self.assertIn('href="#f"', result)
        self.assertIn('<td>86.2</td>', result)
        self.assertEqual(result.count('<mn>3</mn>'), 2)
        self.assertIn('检测准确率。', result)
        self.assertNotIn('source script', result)
        for value in ('en', 'zh', 'both'):
            self.assertIn(f'data-mode="{value}"', result)

    def test_reject_incomplete_translation(self):
        del self.translations['caption']
        with self.assertRaisesRegex(ValueError, 'Missing IDs'):
            render(self.paper, self.translations)

    def test_reject_missing_protected_element(self):
        self.translations['p'][0][0][1] = '卷积核大小为 3。'
        with self.assertRaisesRegex(ValueError, 'placeholder mismatch'):
            render(self.paper, self.translations)

    def test_reject_non_arxiv_url(self):
        with self.assertRaises(IngestError):
            paper_urls('https://arxiv.org.example/abs/2606.13108v1')


class ListReaderTest(unittest.TestCase):
    def setUp(self):
        source, html, _ = paper_urls('https://arxiv.org/html/2606.13108v1')
        self.paper = html_paper('''<article class="ltx_document"><h1 id="title">List example</h1>
<p id="intro">See <a href="#steps">the steps</a>, <a href="#first">the first step</a>,
<a href="#paragraph">its explanation</a>, and <a href="#marker">its number</a>.</p>
<ol id="steps" start="-2" reversed>
  <li id="first"><span id="marker" class="ltx_tag ltx_tag_item">-2.</span>
    <div id="paragraph" class="ltx_para"><p id="body">Cite <cite>[<a href="#bib">1</a>]</cite>.</p></div>
    <p id="continuation">A second paragraph.</p>
    <ul id="substeps"><li id="sub"><span class="ltx_tag ltx_tag_item">•</span>Nested text.
      <ol id="letters" type="a" start="4"><li id="leaf">Deep text.</li></ol>
    </li></ul>
  <li id="last" value="-8"><p><span class="ltx_tag ltx_tag_item">-8.</span>Final text.</p>
</ol>
<ol class="ltx_biblist"><li id="bib" class="ltx_bibitem">Reference.</li></ol>
</article>''', source, html)
        self.translations = {block['id']: [[[block['text'], block['text']]]]
                             for block in inventory(self.paper)}
        self.translations['body'] = [[['Cite {{1}}.', '引用 {{1}}。']]]

    def test_nested_lists_and_backlinks(self):
        result = render(self.paper, self.translations)
        parser = TreeParser()
        parser.feed(result)
        parser.close()
        anchors = {node.attrs['id']: node for node in nodes(parser.root) if 'id' in node.attrs}
        outer = anchors['steps']
        self.assertEqual((outer.tag, outer.attrs['start']), ('ol', '-2'))
        self.assertIn('reversed', outer.attrs)
        self.assertEqual([node for node in outer.children if isinstance(node, Node)],
                         [anchors['first'], anchors['last']])
        first = anchors['first']
        content = [node for node in first.children if isinstance(node, Node) and node.tag != 'span']
        self.assertEqual(content, [anchors['body'], anchors['continuation'], anchors['substeps']])
        self.assertIn(anchors['sub'], anchors['substeps'].children)
        self.assertIn(anchors['letters'], anchors['sub'].children)
        self.assertIn(anchors['leaf'], anchors['letters'].children)
        self.assertEqual((anchors['letters'].attrs['type'], anchors['letters'].attrs['start']), ('a', '4'))
        self.assertEqual(anchors['last'].attrs['value'], '-8')
        self.assertEqual({item.attrs['lang'] for item in nodes(anchors['body']) if 'lang' in item.attrs},
                         {'en', 'zh-CN'})
        self.assertIn('引用', result)
        self.assertEqual(result.count('href="#bib"'), 2)
        for anchor in ('steps', 'first', 'paragraph', 'marker'):
            self.assertIn(f'href="#{anchor}"', result)
            self.assertEqual(result.count(f'id="{anchor}"'), 1)
        self.assertNotIn('ltx_tag_item', result)
        ids = IDs()
        ids.feed(result)
        self.assertEqual(len(ids.ids), len(set(ids.ids)))

    def test_split_and_merge_include_prose_in_list_order(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp) / 'batches'
            split(self.paper, directory, 80)
            paths = []
            order = []
            for path in sorted(directory.glob('*.source.json')):
                batch = read(path)
                order.extend(block['text'] for block in batch)
                translated = path.with_suffix('.zh.json')
                write(translated, {block['id']: self.translations[block['id']] for block in batch})
                paths.append(translated)
            self.assertEqual(order[2:], ['Cite {{1}}.', 'A second paragraph.', 'Nested text.',
                                         'Deep text.', 'Final text.'])
            output = directory / 'translations.json'
            merge(self.paper, output, paths[::-1])
            self.assertEqual(list(read(output)), list(self.translations))

    def test_reject_unclosed_list_encoding(self):
        del self.paper['blocks'][-2]
        with self.assertRaisesRegex(ValueError, 'Invalid list structure'):
            render(self.paper, self.translations)


if __name__ == '__main__':
    unittest.main()
