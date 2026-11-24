#!/usr/bin/env python3
"""Build OPEN with the source-built radio inside the stock task loop.

The stock task loop (bank 11 PC 0x1AB6) runs unchanged except that its boot-time
mode choice always selects the radio task. The stock radio task (bank 17) loads
the 6 KiB OPEN overlay and calls it in place of its own overlay main. OPEN returns
with task 1 to enter the stock music player; any later task change re-enters it."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from band_patch import ROOT,decode,is_supported
from build_radio import build as compile_radio,VERSION
from firmware_integrity import check,finalize,preserve_original,balance_application_crc
import boot_version

OVERLAY,OVERLAY_SIZE=0x1D800,0x1800
HANDOFF=0xEA0A
LOAD_COUNT=0xE9F8
DISPATCH=0x31C9
# Bank 11 PC 0x1ACE: R6=saved last task (setting 0x3A), clipped to 1..3. OPEN boots
# the radio task regardless of the saved mode or inserted media.
BOOT_MODE=0x3000+0x11*0x800+(0x1ACE-0x1800)
BOOT_MODE_STOCK=bytes.fromhex('fe c3 94 04 40 02 7e 01')
BOOT_MODE_RADIO=bytes.fromhex('7e 03 00 00 00 00 00 00')
# The stock PC/USB mass-storage task (task 4, bank 1B) loads a 3-block overlay from
# 0x1E800, whose first block the 6 KiB radio overlay occupies. Keep that overlay
# byte-identical in three empty banks and point its single loader there.
USB_OVERLAY,USB_OVERLAY_SIZE=0x1E800,0x1800
USB_RELOCATED=0x3000+0x2E*0x800
USB_LOADER=0x3000+0x1B*0x800+(0x1852-0x1800)
USB_LOADER_STOCK=bytes.fromhex('79 03 7a d0 7b 01')
def overlay_page(flash):return (flash-0x1800)>>8
# RAM-resident common code ends at PC 0x16EC; its zero padding holds bank statics.
COMMON_PADDING=0x1800+0x16EC
BALANCE=OVERLAY+OVERLAY_SIZE-2


def bank_flash(bank):return 0x3000+bank*0x800


def build(source,output):
    if not is_supported(source):raise ValueError('verified stock v1.4 source is required')
    check(source)
    compiled=compile_radio(output,autonomous=True)
    code=(output/'radio.bin').read_bytes()
    if compiled['code_address']!=0x4000 or len(code)>OVERLAY_SIZE-2:
        raise ValueError('radio does not fit the native overlay and CRC padding')
    plain=decode(source)
    if plain[HANDOFF-5:HANDOFF+5]!=bytes.fromhex('c0 8b 43 8b 10 12 4a 12 d0 8b'):
        raise ValueError('native radio handoff differs from the reviewed instructions')
    if plain[LOAD_COUNT-1:LOAD_COUNT+5]!=bytes.fromhex('79 02 7a c0 7b 01'):
        raise ValueError('native two-block radio overlay load differs')
    if plain[DISPATCH:DISPATCH+8]!=bytes.fromhex('90 1a b6 74 11 12 01 33'):
        raise ValueError('post-initialization task dispatcher differs')
    if plain[BOOT_MODE:BOOT_MODE+8]!=BOOT_MODE_STOCK:
        raise ValueError('boot-time task selection differs from the reviewed instructions')
    if any(plain[COMMON_PADDING:0x3000]):
        raise ValueError('bank/radio state allocation is not empty common-code padding')
    if plain[USB_LOADER:USB_LOADER+6]!=USB_LOADER_STOCK or overlay_page(USB_OVERLAY)!=0x1D0:
        raise ValueError('USB overlay loader differs from the reviewed instructions')
    if any(plain[USB_RELOCATED:USB_RELOCATED+USB_OVERLAY_SIZE]):raise ValueError('USB overlay destination is not empty')
    banks=compiled['banks']
    for bank in banks:
        start=bank_flash(bank['bank'])
        if any(plain[start:start+0x800]):raise ValueError(f'bank {bank["bank"]:#x} is not empty in the source image')
        if start<USB_RELOCATED+USB_OVERLAY_SIZE and USB_RELOCATED<start+0x800:raise ValueError('bank overlaps USB overlay copy')
    target_crc=struct.unpack_from('<I',plain,0x3C)[0]
    plain[USB_RELOCATED:USB_RELOCATED+USB_OVERLAY_SIZE]=plain[USB_OVERLAY:USB_OVERLAY+USB_OVERLAY_SIZE]
    page=overlay_page(USB_RELOCATED);plain[USB_LOADER+3]=page&255;plain[USB_LOADER+5]=page>>8
    plain[OVERLAY:OVERLAY+OVERLAY_SIZE]=code.ljust(OVERLAY_SIZE,b'\0')
    entry=compiled['entries']['radio_forever']
    plain[HANDOFF:HANDOFF+3]=bytes([0x12,entry>>8,entry&255])
    plain[LOAD_COUNT]=3  # Native load_code accepts 1..3 blocks of 2 KiB.
    plain[BOOT_MODE:BOOT_MODE+8]=BOOT_MODE_RADIO
    for bank in banks:
        start=bank_flash(bank['bank']);data=Path(bank['path']).read_bytes()
        plain[start:start+len(data)]=data
    plain,welcome=boot_version.patch(plain,VERSION,(ROOT/'assets/font-5x8.bin').read_bytes())
    plain,balance=balance_application_crc(plain,target_crc,BALANCE)
    raw,integrity=finalize(plain)
    preserve_original(source,raw,[(boot_version.OFFSET,boot_version.SIZE)])
    return raw,dict(version=VERSION,compiled=compiled,integrity=integrity,welcome=welcome,
                    crc_balance=balance,source_sha256=hashlib.sha256(source).hexdigest(),
                    boot_mode=dict(flash=BOOT_MODE,bytes=BOOT_MODE_RADIO.hex()),
                    banks=[dict(bank=b['bank'],flash=bank_flash(b['bank']),bytes=b['bytes'],sha256=b['sha256']) for b in banks],
                    usb_overlay=dict(original=USB_OVERLAY,relocated=USB_RELOCATED,loader=USB_LOADER,page=overlay_page(USB_RELOCATED)),
                    radio_handoff=HANDOFF,overlay_blocks=3,stock_task_loop=True,
                    first_8k_identical=True,settings_preserved=True,flash_written=False,
                    scope='Stock task loop; source-built radio task with stock music-player handoff')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=ROOT/'stock.bin')
    p.add_argument('--output',type=Path,default=ROOT/f'build/v115-{VERSION.lower()}.bin')
    a=p.parse_args()
    if a.output.exists():p.error('refusing to overwrite an existing image')
    raw,manifest=build(a.source.read_bytes(),a.output.parent/'radio-autonomous')
    again,_=build(a.source.read_bytes(),a.output.parent/'radio-autonomous-rebuild')
    if raw!=again:raise ValueError('autonomous image build is not deterministic')
    manifest['deterministic_rebuild']=True
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_bytes(raw)
    a.output.with_suffix('.manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    a.output.with_suffix('.decoded.bin').write_bytes(decode(raw))
    boot_version.preview(raw,a.output.with_suffix('.boot-preview.png'))
    print('Built',a.output,'SHA256',manifest['integrity']['sha256'],'radio bytes',manifest['compiled']['code_bytes'])


if __name__=='__main__':main()
