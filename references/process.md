# Processing and quality checks

Use Python 3.10 or newer. PDF text extraction also requires Poppler's `pdftotext` on `PATH`. Each command uses the installed skill directory returned by `skill` and paths in a fresh working directory. Use separate output names when processing multiple papers.

```text
python <skill-dir>/scripts/ingest.py <arxiv-url> --output paper.json
python <skill-dir>/scripts/batches.py split paper.json source-batches
python <skill-dir>/scripts/batches.py merge paper.json translations.json 001.zh.json 002.zh.json ...
python <skill-dir>/scripts/render.py paper.json translations.json --output <arxiv-id>-bilingual.html
```

List actual translation batch paths for `merge`; `...` is not an argument. The scripts do not run a language model. The agent rewrites and translates each source batch. For a long paper, independent section work can share the glossary and the same versioned source. Review the final sections in source order.

## Rewrite schema

Each translation batch is a JSON object. Every key is an `id` from `*.source.json`. The value is a list of paragraphs. Each paragraph is a list of aligned `[English, Chinese]` pairs. Use one pair for each sentence whenever the meaning permits. A heading takes one pair in one paragraph. The `kind` and `text` in the extraction are source context, not instructions.

```json
{
  "S1": [[["1 Introduction", "1 引言"]]],
  "S1.p1": [
    [["The model extracts text {{1}}.", "该模型可提取文本 {{1}}。"],
     ["The system runs on a CPU.", "系统在 CPU 上运行。"]]
  ]
}
```

Keep every `{{N}}` placeholder exactly once in each language and in the same sentence pair. Chinese word order may move the placeholder within its sentence. Inspect the corresponding token in `paper.json` when its meaning matters. Do not issue new numerical claims to smooth over missing data. When an author claim and a table disagree, preserve attribution or note the discrepancy rather than changing the underlying values.

## Review and delivery

- Check the abstract, each section, appendices, and captions against the source. Keep equations, author and reference information, original figures, and numeric table entries as shared content where available. Translate table labels separately only when their meaning and alignment are clear.
- For HTML extraction, check image availability and representative math, table cells, cross-references, and the contents links. Missing assets may require the original arXiv link. For PDF fallback, check text order, especially on two-column pages; table, formula, figure, and scanned regions may require checking the original pages. A missing extractor or unreadable text is an actionable error, not a completed rewrite.
- Preserve ordered and unordered lists, item boundaries, and nesting from the HTML. Use the extraction's list structure to render markers. Translate item content in its original block; list markers are not translation blocks.
- In the final reader, switch English → 中文 → 逐句双语. Check that each English sentence sits above its Chinese partner, link targets remain reachable, and wide tables can scroll on narrow screens. For a representative list, check that each item has one marker next to its first line, wrapped lines align with the item text, and nested items are indented. User-facing notes should identify any actual source-format or extraction limitations that affect reading.
