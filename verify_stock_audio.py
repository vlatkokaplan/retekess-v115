#!/usr/bin/env python3
"""Execute the decoded stock volume wrapper and check its reconstructed behavior."""
import hashlib
import json
from pathlib import Path

from stock_audio import verify_stock_curve,FM_VOLUME,NATIVE_GAIN,VOLUME_REGISTERS
from lcd_draw import Asm
from emulation import run

ROOT=Path(__file__).resolve().parent


def main():
    source=(ROOT/'stock.bin').read_bytes();plain=verify_stock_curve(source)
    a=Asm(0xA000);a.movdi(0x81,0x8F)
    expected=[]
    def put(address,value):a.movdptr(address);a.movai(value);a.movxda()
    for task in (3,4):
        table=plain[0x7B45:0x7B66] if task==4 else FM_VOLUME
        for level in (*range(33),33,255):
            for offset in (0,1,5,255,128):
                put(0x3AB8,task);put(0x3B6E,offset);put(0x7000,0xEE)
                a.movri(1,level);a.emit(0x12,0x1A,0xF6)
                a.movdptr(0x7000);a.emit(0xE0);a.movdptr(0xB000+len(expected));a.movxda()
                value=0xEE
                if level<=32:
                    value=(table[level]+offset)&255
                    if level==0 or value>=128:value=0
                    else:value=min(value,65)
                expected.append(value)
    stop=a.base+len(a.code);a.emit(0x80,0xFE)
    sink=Asm(0x20C3);sink.movar(1);sink.movdptr(0x7000);sink.movxda();sink.ret()
    got=run([(0x1800,plain[0x7800:0x8000]),(0x100,b'\x22'),
             (sink.base,sink.assemble()),(a.base,a.assemble())],a.base,stop,0xB000,len(expected))
    assert got==bytes(expected)
    report=dict(stock_wrapper_instruction_cases=len(expected),stock_source_sha256=hashlib.sha256(source).hexdigest(),
                spi_function_offset=0x7AF6,bank=9,pc=0x1AF6,volume_curve=list(FM_VOLUME),
                native_gain_registers=list(NATIVE_GAIN),ui_gain_registers=list(VOLUME_REGISTERS),
                target_api=0x20C3,fade_api=0x20C6,gain_register=0x7893,control_register=0x7890,
                native_rom_implementation='Register endpoints observed on hardware; internal instructions not extracted')
    (ROOT/'build/stock-volume-decompile.json').write_text(json.dumps(report,indent=2)+'\n')
    print('Stock volume wrapper:',len(expected),'instruction cases passed; curve and queued DAC target reconstructed.')


if __name__=='__main__':main()
