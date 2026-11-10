#!/usr/bin/env python3
"""Reproducible V115 full-feature image builder. Offline; never flashes a device."""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from band_patch import ROOT, is_supported, decode, patch_decoded
from bandwidth_patch import HANDLER_FLASH, handler
from firmware_integrity import finalize, preserve_original, check, balance_application_crc
import native_features
import boot_version


def build(original):
    sha = hashlib.sha256(original).hexdigest()
    if not is_supported(original):
        raise ValueError('unsupported stock executable; expected the verified v1.4 code layout')
    check(original)
    original_plain=decode(original)
    target_crc=struct.unpack_from('<I',original_plain,0x3C)[0]
    plain, band_changes = patch_decoded(original_plain)
    bw, bw_layout = handler()
    if any(plain[HANDLER_FLASH:HANDLER_FLASH+len(bw)]):
        raise ValueError('bandwidth handler padding is not empty')
    plain[HANDLER_FLASH:HANDLER_FLASH+len(bw)] = bw
    plain, layout = native_features.apply(plain)
    plain, boot=boot_version.patch(plain,native_features.VERSION,native_features.font_bytes())
    compensation=native_features.DISPLAY_FLASH+0x7FE
    if layout['display_bytes']>0x7FE:
        raise ValueError('renderer overlaps reserved CRC balancing bytes')
    plain,balance=balance_application_crc(plain,target_crc,compensation)
    raw, integrity = finalize(plain)
    preserve_original(original, raw,[(boot_version.OFFSET,boot_version.SIZE)])
    return raw, dict(source_sha256=sha, integrity=integrity,
                     band_changes=band_changes, bandwidth_layout=bw_layout,
                     native_layout=layout,
                     version=native_features.VERSION,boot_image=boot,
                     crc_balance=balance,boot_and_header_first_8k_identical=True,
                     features=['FM 30–230 MHz; SW-mode AM 150–30000 kHz; original MW grids',
                               'hold 1: FM 200/150/100/50 kHz bandwidth',
                               'hold 2: native AM/FM signal diagnostics',
                               'FM diagnostic short 2: auto/mono; short 3: exit',
                               'hold 3: FM auto/mono; hold 4: LCD and backlight off, consume wake gesture',
                               'hold 7: FM bass boost; hold 6: FM 50/75 us de-emphasis; original hold 5 retained',
                               'diagnostic short 4/5/6: screen off / bass / de-emphasis',
                               'hold 8: settings menu and shortcut help',
                               'journaled FM and screen timeout preferences; 3-second shortcut save debounce',
                               'automatic screen sleep Off/10/30/60 seconds; suspended in menus/scans',
                               'versioned welcome image; quiet-clock gating removed'],
                     defaults=dict(diagnostics=False,display_sleep=False,screen_timeout_seconds=0),
                     status='Build artifact; see the image-bound validation report and README for hardware-test scope',
                     flash_written=False)


def sector_plan(original, candidate, sector_size=4096):
    if len(original)!=0x200000 or len(candidate)!=len(original):
        raise ValueError('sector plan requires two complete 2 MiB images')
    if sector_size<=0 or sector_size & (sector_size-1) or len(original)%sector_size:
        raise ValueError('sector size must be a power of two dividing the image')
    diffs = [i for i,(before,after) in enumerate(zip(original,candidate)) if before!=after]
    sectors = []
    for start in sorted({i//sector_size*sector_size for i in diffs}):
        before, after = original[start:start+sector_size], candidate[start:start+sector_size]
        changed = [i for i in range(sector_size) if before[i]!=after[i]]
        sectors.append(dict(start=start,changed_bytes=len(changed),
                            requires_erase=any((after[i] & ~before[i]) for i in changed),
                            overlaps_boot=start<0x1800 and start+sector_size>0x800,
                            original_sha256=hashlib.sha256(before).hexdigest(),
                            candidate_sha256=hashlib.sha256(after).hexdigest()))
    return dict(changed_bytes=len(diffs),sector_size_assumed=sector_size,
                geometry_verified=False,program_and_restore_verified=False,
                first_8k_identical=original[:0x2000]==candidate[:0x2000],
                protected_prefix_bytes=0x2000,
                sectors=sectors,
                proposed_order=[s['start'] for s in sectors if not s['overlaps_boot']]+[s['start'] for s in sectors if s['overlaps_boot']],
                note='Offline sector plan only; erase geometry remains assumed. Reconstruct and verify complete sectors. The release image preserves the first 8 KiB; a future writer must refuse any erase intersecting that prefix.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=ROOT/'stock.bin')
    p.add_argument('--output',type=Path,default=ROOT/'build/v115-full-features-rc3.bin')
    args=p.parse_args()
    targets=[args.output,args.output.with_suffix('.manifest.json'),args.output.with_suffix('.decoded.bin')]
    if any(t.exists() for t in targets):
        p.error('refusing to overwrite existing release artifacts')
    original=args.source.read_bytes()
    raw,metadata=build(original)
    # Rebuild from the original a second time and require identical output.
    again,_=build(original)
    if raw!=again:
        raise ValueError('release build is not deterministic')
    metadata['deterministic_rebuild']=True
    metadata['flash_plan']=sector_plan(original,raw)
    args.output.parent.mkdir(exist_ok=True,parents=True)
    targets[0].write_bytes(raw)
    targets[1].write_text(json.dumps(metadata,indent=2)+'\n')
    targets[2].write_bytes(decode(raw))
    boot_version.preview(raw,args.output.with_suffix('.boot-preview.png'))
    print('Built:',args.output)
    print('SHA-256:',metadata['integrity']['sha256'])
    print('Changed bytes:',metadata['flash_plan']['changed_bytes'])
    print('Native code bytes:',{k:v for k,v in metadata['native_layout'].items() if k.endswith('_bytes')})


if __name__=='__main__':
    main()
