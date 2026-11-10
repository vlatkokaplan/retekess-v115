#!/usr/bin/env python3
"""Build OPEN-0.2 with the source-built radio and the established boot path."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from band_patch import ROOT,decode,is_supported
from build_radio import build as compile_radio
from firmware_integrity import check,finalize,preserve_original,balance_application_crc
from lcd_draw import Asm
import boot_version

VERSION='OPEN-0.2.2'
OVERLAY,OVERLAY_SIZE=0x1D800,0x1800
HANDOFF=0xEA0A
LOAD_COUNT=0xE9F8
DISPATCH=0x31C9
SHIM_PC=0x16EC
SHIM_FLASH=0x1800+SHIM_PC
BALANCE=OVERLAY+OVERLAY_SIZE-2


def startup():
    a=Asm(SHIM_PC)
    a.L('radio');a.movdptr(0x3AB8);a.movai(3);a.movxda()
    a.movdptr(0x19D3);a.movai(0x17);a.emit(0x12,1,0x33)
    a.sjmp('radio')
    return a.assemble()


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
    shim=startup()
    if SHIM_PC+len(shim)>compiled['scratch_address'] or any(plain[SHIM_FLASH:0x3000]):
        raise ValueError('startup shim/state allocation is not empty stock padding')
    target_crc=struct.unpack_from('<I',plain,0x3C)[0]
    plain[OVERLAY:OVERLAY+OVERLAY_SIZE]=code.ljust(OVERLAY_SIZE,b'\0')
    entry=compiled['entries']['radio_forever']
    plain[HANDOFF:HANDOFF+3]=bytes([0x12,entry>>8,entry&255])
    plain[LOAD_COUNT]=3  # Native load_code accepts 1..3 blocks of 2 KiB.
    plain[DISPATCH:DISPATCH+8]=bytes([2,SHIM_PC>>8,SHIM_PC&255])+bytes(5)
    plain[SHIM_FLASH:SHIM_FLASH+len(shim)]=shim
    plain,welcome=boot_version.patch(plain,VERSION,(ROOT/'assets/font-5x8.bin').read_bytes())
    plain,balance=balance_application_crc(plain,target_crc,BALANCE)
    raw,integrity=finalize(plain)
    preserve_original(source,raw,[(boot_version.OFFSET,boot_version.SIZE)])
    return raw,dict(version=VERSION,compiled=compiled,integrity=integrity,welcome=welcome,
                    crc_balance=balance,source_sha256=hashlib.sha256(source).hexdigest(),
                    startup_shim=dict(pc=SHIM_PC,flash=SHIM_FLASH,bytes=shim.hex()),
                    radio_handoff=HANDOFF,overlay_blocks=3,dispatcher=DISPATCH,
                    first_8k_identical=True,settings_preserved=True,flash_written=False,
                    scope='Existing startup initializes clocks/DAC; the source-built radio owns the main loop and peripherals')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=ROOT/'stock.bin')
    p.add_argument('--output',type=Path,default=ROOT/'build/v115-open-0.2.2.bin')
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
