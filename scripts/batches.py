#!/usr/bin/env python3
"""Split the translation inventory into ordered batches, or merge completed batches."""
import argparse
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, data):
    with path.open('x', encoding='utf-8') as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write('\n')


def inventory(paper):
    return [b for b in paper['blocks'] if b['kind'] not in {'shared', 'list_start', 'list_end'}]


def split(paper, directory, size):
    if size < 1:
        raise ValueError('--max-chars must be positive')
    directory.mkdir(parents=True, exist_ok=False)
    chunks, current, count = [], [], 0
    for block in inventory(paper):
        length = len(block['text'])
        if current and count + length > size:
            chunks.append(current)
            current, count = [], 0
        current.append({'id': block['id'], 'kind': block['kind'], 'text': block['text'], 'tokens': len(block.get('tokens', []))})
        count += length
    if current:
        chunks.append(current)
    for i, batch in enumerate(chunks, 1):
        write(directory/f'{i:03d}.source.json', batch)
    print(f'{len(chunks)} source batches in {directory}')


def merge(paper, output, paths):
    result = {}
    for path in paths:
        entries = read(path)
        if overlap := result.keys() & entries.keys():
            raise ValueError(f'Duplicate translations in {path}: {sorted(overlap)}')
        result.update(entries)
    required = [block['id'] for block in inventory(paper)]
    if set(required) != result.keys():
        raise ValueError(f'Missing IDs: {sorted(set(required)-result.keys())}; extra IDs: {sorted(result.keys()-set(required))}')
    write(output, {key: result[key] for key in required})
    print(f'{len(result)} translated blocks in {output}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    s = actions.add_parser('split')
    s.add_argument('paper', type=Path)
    s.add_argument('directory', type=Path)
    s.add_argument('--max-chars', type=int, default=7500)
    m = actions.add_parser('merge')
    m.add_argument('paper', type=Path)
    m.add_argument('output', type=Path)
    m.add_argument('inputs', type=Path, nargs='+')
    args = parser.parse_args()
    paper = read(args.paper)
    if args.action == 'split':
        split(paper, args.directory, args.max_chars)
    else:
        merge(paper, args.output, args.inputs)


if __name__ == '__main__':
    main()
