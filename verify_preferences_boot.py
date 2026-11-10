#!/usr/bin/env python3
"""Execute cold preference loading, V1 migration and V2 scan-option validation."""
import argparse,hashlib,json
from pathlib import Path
from lcd_draw import Asm
from bandwidth_patch import farcall
from band_patch import ROOT
from emulation import run,verify_image_banks
import native_features as n
import verify_native_features as t


def records():
    from itertools import product
    items=[]
    for pref in range(64):
        for timeout in range(4):items.append([0x52,0x33,1,pref,timeout,pref^255,timeout^255,0xA5]+[0]*8)
    def v2(config):return [0x52,0x33,2,0x21,2,0xDE,0xFD,0xA5,*config,*[x^255 for x in config]]
    for config in product(range(4),range(3),range(2),range(4)):items.append(v2(config))
    for good,fields in [(items[0],range(8)),(v2([1,1,1,0]),range(16))]:
        for index in fields:
            for value in (0,1,3,4,64,255):
                record=good.copy();record[index]=value;items.append(record)
    return items


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('candidate',type=Path);args=p.parse_args()
    sha=verify_image_banks(args.candidate,[(n.CONTROL_BANK,n.control),(n.SETTINGS_BANK,n.settings)])
    service,services=t.shim();glue,entries=n.common_glue()
    cases=[(record,mode) for record in records() for mode in (1,2,3)]
    sources=[n.STATE+n.PREF_REG,n.STATE+n.TIMEOUT,n.STATE+n.NV_DIRTY,0x3B3A,0x7F07,0x7002,0x7000,
             n.STATE+n.SCAN_THRESHOLD,n.STATE+n.SCAN_DWELL,n.STATE+n.SCAN_DIRECTION,n.STATE+n.SCAN_RANGE,
             *range(n.STATE+n.PREF_RECORD,n.STATE+n.PREF_RECORD+n.PREF_SIZE)]
    width=len(sources)
    for first in range(0,len(cases),12):
        batch=cases[first:first+12];a=Asm(0x8000)
        for index,(record,mode) in enumerate(batch):
            a.movdi(0x81,0x8F);a.movdi(0xD0,0)
            for addr,value in [(0x3BE2,0x40),(0x3BE3,0x13),(0x3B3A,0xA1),(0x7F07,0xA1),(0x7002,0),(0x7000,0)]:n.write(a,addr,value)
            a.movdi(0x18,mode)
            for k,value in enumerate(record):n.write(a,0x7E00+k,value)
            a.emit(0x12,entries['init']>>8,entries['init']&255)
            farcall(a,n.SETTINGS_BANK,0x1803)
            for k,source in enumerate(sources):n.read(a,source);a.movdptr(0xA000+index*width+k);a.movxda()
        stop=a.base+len(a.code);a.emit(0x80,0xFE)
        chunks=[(n.COMMON_PC,glue),(t.CTL_BASE,n.control(t.CTL_BASE)[0]),(t.SETTINGS_BASE,n.settings(t.SETTINGS_BASE)[0]),
                (t.SHIM,service),(0x100,bytes.fromhex('d0 e0 22')),(0x133,bytes([2,t.SHIM>>8,t.SHIM&255])),
                (0x208A,bytes([2,services['pref_read']>>8,services['pref_read']&255])),(a.base,a.assemble())]
        data=run(chunks,a.base,stop,0xA000,len(batch)*width)
        for index,(record,mode) in enumerate(batch):
            loaded=t.valid_record(record);pref=record[3] if loaded else 0x21;timeout=record[4] if loaded else 0
            config=record[8:12] if loaded and record[2]==2 else [1,1,1,0]
            reg=0x80|pref if mode==1 else 0xA1
            want=bytes([pref,timeout,0,reg,reg,int(mode==1 and pref!=0x21),0,*config,*record])
            got=data[index*width:(index+1)*width]
            if got!=want:raise ValueError(f'cold preferences mismatch {record}, mode={mode}: {got.hex()} != {want.hex()}')
    report=dict(candidate_sha256=sha,cases_passed=len(cases),cold_owned_state_not_preseeded=True,
                all_fm_preferences_and_timeouts=True,v1_preferences_preserved=True,v2_scan_preferences_verified=True,
                malformed_records_default_safely=True,fm_restore_and_am_preservation=True,
                scope='Actual initialization and restore instructions; ROM reads and tuner writes instrumented',
                source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                               ('native_features.py','verify_native_features.py','verify_preferences_boot.py','emulation.py')})
    args.candidate.with_suffix('.preferences-boot-emulation.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
