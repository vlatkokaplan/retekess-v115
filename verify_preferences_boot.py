#!/usr/bin/env python3
"""Check cold initialization and tuner restoration without seeding owned state."""
import hashlib,json,re,subprocess,sys,tempfile
from env_tools import executable
from pathlib import Path
from analyze import intel_hex
from band_patch import decode,ROOT
from lcd_draw import Asm
import native_features as n
import verify_native_features as t


def main():
    candidate=Path(sys.argv[1]);raw=candidate.read_bytes();plain=decode(raw)
    for bank,fn in [(n.CONTROL_BANK,n.control),(n.SETTINGS_BANK,n.settings)]:
        code=fn()[0];start=0x3000+bank*0x800
        if plain[start:start+len(code)]!=code:raise ValueError('candidate bank differs')
    service,services=t.shim();glue,entries=n.common_glue()
    cases=[]
    for pref in range(64):
        for timeout in range(4):cases.append([0x52,0x33,1,pref,timeout,pref^255,timeout^255,0xA5])
    valid=[0x52,0x33,1,0x21,2,0xDE,0xFD,0xA5]
    for index in range(8):
        for value in (0,1,3,4,64,255):
            record=valid.copy();record[index]=value;cases.append(record)
    cases=[(record,mode) for record in cases for mode in (1,2,3)]
    for first in range(0,len(cases),8):
        batch=cases[first:first+8];a=Asm(0x8000)
        for index,(record,mode) in enumerate(batch):
            a.movdi(0x81,0x8F);a.movdi(0xD0,0)
            for addr,value in [(0x3BE2,0x40),(0x3BE3,0x13),(0x3B3A,0xA1),
                               (0x7F07,0xA1),(0x3AB8,3),(0x3CD0,0),(0x7002,0),(0x7000,0)]:n.write(a,addr,value)
            a.movdi(0x18,mode)
            for k,value in enumerate(record):n.write(a,0x7E00+k,value)
            a.emit(0x12,entries['init']>>8,entries['init']&255)
            a.emit(0x12,entries['wait']>>8,entries['wait']&255)
            for k,source in enumerate([n.STATE+n.PREF_REG,n.STATE+n.TIMEOUT,n.STATE+n.NV_DIRTY,
                                       0x3B3A,0x7F07,0x7002,0x7000,*range(n.STATE+n.PREF_RECORD,n.STATE+n.PREF_RECORD+8)]):
                n.read(a,source);a.movdptr(0xA000+index*15+k);a.movxda()
        stop=a.base+len(a.code);a.emit(0x80,0xFE)
        chunks=[(n.COMMON_PC,glue),(t.CTL_BASE,n.control(t.CTL_BASE)[0]),
                (t.SETTINGS_BASE,n.settings(t.SETTINGS_BASE)[0]),(t.SHIM,service),
                (0x100,bytes.fromhex('d0 e0 22')),(0x133,bytes([2,t.SHIM>>8,t.SHIM&255])),
                (0x208A,bytes([2,services['pref_read']>>8,services['pref_read']&255])),
                (0x20B1,bytes([2,services['get_msg']>>8,services['get_msg']&255])),(a.base,a.assemble())]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.hex';path.write_text(intel_hex(chunks))
            result=subprocess.run([executable('s51'),'-b','-q','-t','C52','-c','-'],
                 input=f'file "{path}"\nrun 0x8000 {stop:#x}\ndump /h xram 0xa000 {0xA000+len(batch)*15-1:#x} 16\nquit\n',
                 text=True,capture_output=True,timeout=30,check=True)
        data={}
        for line in result.stdout.splitlines():
            m=re.match(r'^0x([0-9a-fA-F]+)\s+((?:[0-9a-fA-F]{2}(?:\s+|$))+)',line)
            if m:
                for k,value in enumerate(bytes.fromhex(m[2])):data[int(m[1],16)+k]=value
        for index,(record,mode) in enumerate(batch):
            loaded=t.valid_record(record);pref=record[3] if loaded else 0x21;timeout=record[4] if loaded else 0
            reg=0x80|pref if mode==1 else 0xA1
            want=[pref,timeout,0,reg,reg,int(mode==1 and pref!=0x21),0,*record]
            got=[data.get(0xA000+index*15+k) for k in range(15)]
            if got!=want:raise ValueError(f'cold preferences mismatch {record}, mode {mode}: {got} != {want}')
    report=dict(candidate_sha256=hashlib.sha256(raw).hexdigest(),cases_passed=len(cases),
                cold_owned_state_not_preseeded=True,all_fm_preferences_and_timeouts=True,
                malformed_records_default_safely=True,fm_restore_and_am_preservation=True,
                scope='Actual initialization, settings parser and restoration instructions; ROM flash reads and tuner writes instrumented.',
                source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                               ('native_features.py','verify_native_features.py','verify_preferences_boot.py')})
    candidate.with_suffix('.preferences-boot-emulation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
