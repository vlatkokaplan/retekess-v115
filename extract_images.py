#!/usr/bin/env python3
"""Extract V115 LCD images and menu text from a local SPI-flash backup.

No device access. Resources are unencrypted; only the resource-directory header
needs decoding. LCD bytes run across each 8-row page, bit 0 at the top. A set
bit is a dark pixel, confirmed by the RAM display driver and camera captures.
"""
import argparse
import hashlib
import html
import json
from pathlib import Path
import struct
import zipfile

from analyze import firmware_xor


def load_resources(path):
    raw = Path(path).read_bytes()
    if len(raw) < 0x800:
        raise ValueError("Firmware is shorter than its header")
    header = bytearray(firmware_xor(raw[:0x800], 0x50AF))
    header[:0x20] = firmware_xor(raw[:0x20], 0xB6B3)
    sections = {}
    for kind, entry in [('image', 0x70), ('menu', 0x80)]:
        flags, base, size, _ = struct.unpack_from('<4I', header, entry)
        if flags != 0x100 or size < 16 or base < 0x800 or base + size > len(raw):
            raise ValueError(f"Invalid {kind} section in decoded firmware header")
        sections[kind] = {'offset': base, 'size': size}

    assets = []
    for kind, section in sections.items():
        base, size = section['offset'], section['size']
        if kind == 'image':
            count = struct.unpack_from('<I', raw, base)[0]
            table_start = 4
        else:
            table_end = struct.unpack_from('<I', raw, base + 4)[0]
            if table_end % 16:
                raise ValueError("Menu table size is not a multiple of 16")
            count, table_start = table_end // 16, 0
        table_end = table_start + count * 16
        if not 0 < count <= 4096 or table_end >= size:
            raise ValueError(f"Invalid {kind} record count")
        previous_end = table_end
        for index in range(count):
            entry = base + table_start + index * 16
            width, height, f1, f2, f3 = struct.unpack_from('<HHIII', raw, entry)
            if kind == 'image':
                if f1 != 0:
                    raise ValueError(f"Unsupported image flags: {f1:#x}")
                offset, widths = f2, [width]
                language_id = group_id = None
            else:
                offset = f1
                widths = [width, f2] if f3 & 0x100 else [width]
                language_count = f3 & 0xff
                if language_count != 3 or f3 & ~0x1ff:
                    raise ValueError(f"Unsupported menu flags: {f3:#x}")
                language_id, group_id = index % language_count, index // language_count
            if height % 8 or not 0 < height <= 64 or any(not 0 < w <= 128 for w in widths):
                raise ValueError(f"Invalid dimensions in {kind} {index}")
            length = sum(widths) * (height // 8)
            if kind == 'image' and length != f3:
                raise ValueError(f"Unexpected byte count in image {index}")
            if offset != previous_end or offset + length > size:
                raise ValueError(f"Noncontiguous or out-of-bounds data in {kind} {index}")
            previous_end = offset + length
            lines, cursor = [], base + offset
            for w in widths:
                n = w * (height // 8)
                lines.append({'width': w, 'height': height, 'offset': cursor,
                              'data': raw[cursor:cursor + n]})
                cursor += n
            assets.append({'kind': kind, 'index': index, 'record_offset': entry,
                           'language_id': language_id, 'group_id': group_id,
                           'flags': f1 if kind == 'image' else f3, 'lines': lines})
        section['count'] = count
        section['data_end'] = base + previous_end
    metadata = {'source': str(Path(path).resolve()), 'source_bytes': len(raw),
                'source_sha256': hashlib.sha256(raw).hexdigest(), 'sections': sections,
                'pixel_format': '8-row LCD pages, left to right, LSB at top; 1=dark',
                'menu_note': 'Two-line entries are stacked in the PNG. Firmware may '
                             'also join the two lines horizontally in some display modes.'}
    return assets, metadata


def render(asset):
    from PIL import Image
    width = max(line['width'] for line in asset['lines'])
    height = sum(line['height'] for line in asset['lines'])
    image = Image.new('1', (width, height), 1)
    pixels, top = image.load(), 0
    for line in asset['lines']:
        w, h, data = line['width'], line['height'], line['data']
        for y in range(h):
            for x in range(w):
                pixels[x, top + y] = 0 if data[(y // 8) * w + x] & (1 << (y % 8)) else 1
        top += h
    return image


def export(assets, metadata, output):
    from PIL import Image, ImageDraw
    output.mkdir(parents=True, exist_ok=True)
    manifest, cards = [], []
    for asset in assets:
        image = render(asset)
        kind, index = asset['kind'], asset['index']
        stem = f"{kind}_{index:03d}_{image.width}x{image.height}"
        if kind == 'menu':
            stem += f"_entry{asset['group_id']:03d}_lang{asset['language_id']}"
        rel = Path('images' if kind == 'image' else 'menus') / (stem + '.png')
        (output / rel).parent.mkdir(exist_ok=True)
        image.save(output / rel)
        # Verify every exported pixel against the firmware page bytes.
        check, top = Image.open(output / rel).load(), 0
        for line in asset['lines']:
            rebuilt = bytes(sum((not check[x, top + page * 8 + bit]) << bit
                                for bit in range(8))
                            for page in range(line['height'] // 8)
                            for x in range(line['width']))
            if rebuilt != line['data']:
                raise ValueError(f"PNG round-trip failed: {rel}")
            top += line['height']
        record = {key: value for key, value in asset.items() if key != 'lines'}
        record.update({'file': rel.as_posix(), 'width': image.width, 'height': image.height,
                       'lines': [{k: v for k, v in line.items() if k != 'data'} |
                                 {'bytes': len(line['data']),
                                  'sha256': hashlib.sha256(line['data']).hexdigest()}
                                 for line in asset['lines']]})
        manifest.append(record)
        label = f"{kind} {index:03d} · {image.width}×{image.height}"
        if kind == 'menu':
            label += f" · entry {asset['group_id']} · language {asset['language_id']}"
        cards.append(f'<figure data-kind="{kind}" data-lang="{asset["language_id"]}" '
                     f'data-search="{html.escape(label)}"><a href="{rel.as_posix()}">'
                     f'<img loading="lazy" width="{image.width * 3}" height="{image.height * 3}" '
                     f'src="{rel.as_posix()}" alt="{html.escape(label)}"></a>'
                     f'<figcaption>{html.escape(label)}</figcaption></figure>')

    for kind in ('image', 'menu'):
        selected = [asset for asset in assets if asset['kind'] == kind]
        # Separate menu sheets keep the 621 text entries readable at 2x scale.
        page_size = 90 if kind == 'menu' else len(selected)
        for page_start in range(0, len(selected), page_size):
            page = selected[page_start:page_start + page_size]
            columns, cell_w, cell_h = (6, 280, 162)
            sheet = Image.new('RGB', (columns * cell_w, ((len(page) + columns - 1) // columns) * cell_h), '#eceff3')
            draw = ImageDraw.Draw(sheet)
            for pos, asset in enumerate(page):
                x, y = pos % columns * cell_w, pos // columns * cell_h
                bitmap = render(asset)
                bitmap = bitmap.resize((bitmap.width * 2, bitmap.height * 2), Image.Resampling.NEAREST)
                sheet.paste(bitmap, (x + 6, y + 23))
                label = f"{kind} {asset['index']:03d}"
                if kind == 'menu':
                    label += f" / entry {asset['group_id']} / lang {asset['language_id']}"
                draw.text((x + 6, y + 5), label, fill='black')
            suffix = f'-{page_start // page_size + 1:02d}' if kind == 'menu' else ''
            sheet.save(output / f'{kind}-contact-sheet{suffix}.png')
    metadata['assets'] = manifest
    metadata['verified_png_round_trips'] = len(manifest)
    (output / 'manifest.json').write_text(json.dumps(metadata, indent=2) + '\n')
    title = f'V115 firmware: {len(manifest)} LCD assets'
    page = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>body{font:15px system-ui;background:#eceff3;color:#172234;margin:24px}
header{position:sticky;top:0;background:#eceff3;padding:10px 0;z-index:1}
h1{font-size:23px;margin:0 0 10px}input,select,button{font:inherit;padding:6px;margin:3px}
main{display:flex;flex-wrap:wrap;align-items:stretch;gap:12px}figure{margin:0;padding:14px;
background:white;border:1px solid #ccd3dd;min-width:200px;max-width:390px;overflow:auto}
figure[hidden]{display:none}img{image-rendering:pixelated;object-fit:contain;display:block;margin:8px 0}
figcaption{font-size:12px;color:#47556b}.invert img{filter:invert(1)}a{color:#2159ad}</style>
<header><h1>__TITLE__</h1><p>Native LCD pixels, 3× preview. Click an image for its original PNG.
Menu language numbers follow firmware order; two-line entries are stacked.</p>
<input id="search" placeholder="Search index, entry, dimensions…"><select id="kind">
<option value="">All assets</option><option value="image">Images</option><option value="menu">Menu text</option>
</select><select id="lang"><option value="">All languages</option><option>0</option><option>1</option>
<option>2</option></select><button id="invert">Invert preview</button> <span id="count"></span>
<p><a href="manifest.json">Offsets, dimensions and checksums</a> ·
<a href="image-contact-sheet.png">Image contact sheet</a></p></header><main>__CARDS__</main>
<script>const $=id=>document.getElementById(id);function filter(){let n=0;
document.querySelectorAll('figure').forEach(f=>{f.hidden=!!(($('kind').value&&f.dataset.kind!==$('kind').value)||
($('lang').value&&f.dataset.lang!==$('lang').value)||!f.dataset.search.toLowerCase().includes($('search').value.toLowerCase()));
if(!f.hidden)n++});$('count').textContent=n+' assets'}['search','kind','lang'].forEach(id=>$(id).addEventListener('input',filter));
$('invert').onclick=()=>document.body.classList.toggle('invert');filter();</script></html>'''
    (output / 'index.html').write_text(page.replace('__TITLE__', title).replace('__CARDS__', '\n'.join(cards)))
    (output / 'README.txt').write_text(
        title + '\n\nOpen index.html locally. PNGs use 1=dark LCD polarity.\n'
        'Image and menu data is copied from the local backup; no USB commands are used.\n'
        'manifest.json records the source SHA-256, every flash address, and dimensions.\n'
        f'All {len(manifest)} PNGs were decoded back into page bytes and matched the source.\n'
        'The old images/ export outside this folder uses the opposite pixel polarity.\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument('--firmware', type=Path, default=root / 'stock.bin')
    parser.add_argument('--output', type=Path, default=root / 'firmware-images')
    parser.add_argument('--zip', action='store_true', help='also package the output directory')
    args = parser.parse_args()
    assets, metadata = load_resources(args.firmware)
    manifest = export(assets, metadata, args.output)
    if args.zip:
        archive = args.output.with_suffix('.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            for path in sorted(args.output.rglob('*')):
                if path.is_file(): z.write(path, path.relative_to(args.output.parent))
        print(f'Archive: {archive}')
    counts = {kind: sum(a['kind'] == kind for a in manifest) for kind in ('image', 'menu')}
    print(f'Extracted {counts["image"]} images + {counts["menu"]} menu entries.')
    print(f'All {len(manifest)} PNGs match source bytes after pixel round-trip.')
    print(f'Gallery: {args.output / "index.html"}')


if __name__ == '__main__':
    main()
