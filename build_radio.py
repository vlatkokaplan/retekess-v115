#!/usr/bin/env python3
"""Build the independent radio application for execution in loader RAM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT=Path(__file__).resolve().parent
CODE_START,CODE_END=0x2400,0x3A00


def tool(name):
    candidate=os.environ.get(name.upper()) or shutil.which(name)
    if not candidate:
        local=ROOT/'build/toolchain/root/usr/bin'/name
        if local.exists():candidate=str(local)
    if not candidate:raise ValueError(f'{name} is required; install SDCC or set {name.upper()}')
    return str(Path(candidate).resolve())


def read_hex(path):
    memory={}
    for line in path.read_text().splitlines():
        row=bytes.fromhex(line[1:]);size=row[0];address=int.from_bytes(row[1:3],'big');kind=row[3]
        if len(row)!=size+5 or sum(row)&255:raise ValueError('invalid Intel HEX checksum')
        if kind==1:break
        if kind!=0:raise ValueError('unexpected HEX address extension')
        for i,b in enumerate(row[4:-1]):
            if address+i in memory:raise ValueError('overlapping executable sections')
            memory[address+i]=b
    return memory


def build(output,simulation=False,autonomous=False,timer_batch=False):
    if timer_batch and not autonomous:raise ValueError('timer batches require the autonomous application')
    code_start,code_end=(0x4000,0x5800) if autonomous and not timer_batch else (CODE_START,CODE_END)
    scratch,state_address,page_address=(0x1780,0x17B0,0x2800) if autonomous and not timer_batch else (0x2280,0x2380,0x2300)
    scratch_limit=48 if scratch==0x1780 else 128
    output.mkdir(parents=True,exist_ok=True)
    # The fixed radio UI uses ASCII space through underscore (uppercase only).
    font=(ROOT/'assets/font-5x8.bin').read_bytes()[:64*5]
    if len(font)!=64*5:raise ValueError('radio font is incomplete')
    (output/'font.h').write_text('static const __code u8 font[]={'+','.join(str(v) for v in font)+'};\n')
    compiler=tool('sdcc');linker=tool('sdld')
    sources=[ROOT/'firmware/app.c',ROOT/('firmware/sim_hardware.c' if simulation else 'firmware/hardware.c')]
    if autonomous and not timer_batch:sources.append(ROOT/'firmware/standalone.c')
    defines=[f'-DRADIO_STATE={state_address}',f'-DRADIO_PAGE={page_address}']
    if autonomous:defines.append('-DRADIO_AUTONOMOUS')
    if autonomous and not timer_batch:defines.append('-DRADIO_NATIVE_AUDIO')
    objects=[]
    for source in sources:
        obj=output/(source.stem+'.rel');objects.append(obj)
        subprocess.run([compiler,'-c','-mmcs51','--model-large','--std-c99','--opt-code-size',
                        *defines,'-I'+str(ROOT/'firmware'),'-I'+str(output),str(source),'-o',str(obj)],check=True)
    stem=output/'radio'
    subprocess.run([linker,'-n','-i','-m','-w','-b',f'CSEG={code_start:#x}','-b',f'XSEG={scratch:#x}',
                    str(stem),*[str(p) for p in objects]],check=True)
    mapping=stem.with_suffix('.map').read_text()
    symbols={name:int(address,16) for address,name in re.findall(r'^\s*(?:C:)?\s*([0-9A-Fa-f]{8})\s+(\w+)\b',mapping,re.M)}
    for area,limit in [('l_DSEG',32),('l_BSEG',0),('l_OSEG',0),('l_ISEG',0),('l_PSEG',0),('l_XSEG',scratch_limit)]:
        if symbols.get(area,0)>limit:raise ValueError(f'{area} exceeds reserved RAM: {symbols.get(area)} > {limit}')
    memory=read_hex(stem.with_suffix('.ihx'))
    if not memory or min(memory)<code_start or max(memory)>=code_end:
        raise ValueError(f'executable exceeds loader RAM: {min(memory):#x}..{max(memory):#x}')
    code=bytes(memory.get(p,0) for p in range(code_start,max(memory)+1))
    stem.with_suffix('.bin').write_bytes(code)
    entries={name:symbols['_'+name] for name in ['app_init','app_tick']}
    if not autonomous:entries['app_stop']=symbols['_app_stop']
    if autonomous and not timer_batch:
        entries['radio_forever']=symbols['_radio_forever']
        entries['radio_timer_start']=symbols['_radio_timer_start']
    names=['size','mode','volume','frequency','entry','digits','editing','sleeping','muted','error',
           'cn','status','adc1','adc2','last_key','injected_key','command','frames','events','tunes','page_crc','pending','entry_bad','read_channel','standby','clock_ticks']
    fields={name:memory[symbols['_app_layout']+i] for i,name in enumerate(names)}
    report=dict(version='OPEN-0.2.3' if autonomous else 'OPEN-0.1',format='RAM executable; not an SPI flash image',
                autonomous=autonomous,timer_batch=timer_batch,
                code_address=code_start,code_bytes=len(code),code_end=code_start+len(code),
                tick_milliseconds=int(re.search(r'#define TICK_MS (\d+)',(ROOT/'firmware/radio.h').read_text())[1]),
                state_address=state_address,state_bytes=fields['size'],fields=fields,
                internal_data_bytes=symbols.get('l_DSEG',8),
                scratch_address=scratch,scratch_bytes=symbols.get('l_XSEG',0),
                page_buffer_address=page_address,entries=entries,simulation=simulation,
                sha256=hashlib.sha256(code).hexdigest(),
                compiler=subprocess.check_output([compiler,'--version'],text=True).splitlines()[0],
                source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in [*sources,ROOT/'firmware/radio.h',ROOT/'assets/font-5x8.bin',Path(__file__)]})
    stem.with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=ROOT/'build/radio-ram')
    p.add_argument('--simulation',action='store_true')
    p.add_argument('--autonomous',action='store_true',help='build the autonomous radio overlay')
    p.add_argument('--timer-batch',action='store_true',help='build the autonomous UI for bounded tests in loader RAM')
    a=p.parse_args();r=build(a.output.resolve(),a.simulation,a.autonomous,a.timer_batch)
    print(json.dumps(r,indent=2))


if __name__=='__main__':main()
