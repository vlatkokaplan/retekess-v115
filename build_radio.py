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
VERSION='OPEN-0.3.0'
# The settings menu and the system/handoff code run from two empty 2 KiB stock
# banks through the stock 0x0133 trampoline, at PC 0x1800 like every stock bank.
BANK_PC,BANK_SIZE=0x1800,0x800
# Bank statics live in the zero padding of the RAM-resident common code (PC 0x16EC-0x17FF),
# below the radio scratch area; bank DSEG follows the overlay's register/sloc bytes 0x00-0x09.
BANK_DSEG,BANK_DSEG_LIMIT=0x0A,6
BANKS=[dict(name='menu',source='firmware/menu.c',bank=0x2C,xseg=0x16F0,xseg_limit=0x10,
            entries=['menu_key','menu_draw']),
       dict(name='system',source='firmware/system.c',bank=0x2D,xseg=0x1700,xseg_limit=0x30,
            entries=['radio_enter','radio_leave','stock_handoff'])]
# Simulation cannot page code, so it links the system bank a second time here.
SIM_SYSTEM_PC=0x2800


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


def link_symbols(mapping):
    return {name:int(address,16) for address,name in re.findall(r'^\s*(?:C:)?\s*([0-9A-Fa-f]{8})\s+(\w+)\b',mapping,re.M)}


def build_bank(output,spec,overlay_symbols,defines,compiler,linker,pc=BANK_PC):
    """Compile one bank against the overlay's exported functions and parameters."""
    name=spec['name'];bank=output/('bank-'+name+('' if pc==BANK_PC else f'-{pc:04x}'));bank.mkdir(parents=True,exist_ok=True)
    obj=bank/(name+'.rel')
    subprocess.run([compiler,'-c','-mmcs51','--model-large','--std-c99','--opt-code-size',
                    *defines,'-I'+str(ROOT/'firmware'),'-I'+str(output),str(ROOT/spec['source']),'-o',str(obj)],check=True)
    stem=bank/name
    def link(definitions):
        return subprocess.run([linker,'-n','-i','-m','-w','-b',f'CSEG={pc:#x}','-b',f'XSEG={spec["xseg"]:#x}',
                               '-b',f'DSEG={BANK_DSEG:#x}',*[x for d in definitions for x in ('-g',d)],str(stem),str(obj)],
                              capture_output=True,text=True)
    # First pass names exactly the overlay functions/parameters the bank uses.
    needed=sorted(set(re.findall(r"Undefined Global '(\w+)'",(lambda r:r.stdout+r.stderr)(link([])))))
    missing=[symbol for symbol in needed if symbol not in overlay_symbols]
    if missing:raise ValueError(f'{name} bank references symbols absent from the overlay: {missing}')
    result=link([f'{symbol}={overlay_symbols[symbol]:#x}' for symbol in needed])
    if result.returncode or 'Undefined' in result.stdout+result.stderr:
        raise ValueError(f'{name} bank link failed: '+result.stdout+result.stderr)
    mapping=stem.with_suffix('.map').read_text();symbols=link_symbols(mapping)
    if symbols.get('_bank_entries')!=pc:raise ValueError(f'{name} bank entry table is not at the window start')
    xseg=re.search(r'^XSEG\s+([0-9A-F]{8})\s+([0-9A-F]{8})',mapping,re.M)
    xseg_bytes=int(xseg[2],16) if xseg else 0
    if xseg_bytes>spec['xseg_limit']:raise ValueError(f'{name} bank statics exceed their reservation')
    # sdld reports area ends, so count the module's own DATA reservations instead.
    assembly=(bank/(name+'.asm')).read_text()
    dseg_bytes=sum(int(n) for block in re.findall(r'\.area DSEG\s+\(DATA\)(.*?)\.area',assembly,re.S)
                   for n in re.findall(r'\.ds\s+(\d+)',block))
    if dseg_bytes>BANK_DSEG_LIMIT:raise ValueError(f'{name} bank internal data exceeds 0x0A-0x0F')
    memory=read_hex(stem.with_suffix('.ihx'))
    if min(memory)<pc or max(memory)>=pc+BANK_SIZE:raise ValueError(f'{name} bank exceeds its 2 KiB window')
    code=bytes(memory.get(p,0) for p in range(pc,max(memory)+1))
    stem.with_suffix('.bin').write_bytes(code)
    exported={k:symbols[k] for k in ('_saved',) if k in symbols}
    return dict(bank=spec['bank'],address=pc,bytes=len(code),xseg_address=spec['xseg'],imports=needed,
                xseg_bytes=xseg_bytes,dseg_bytes=dseg_bytes,symbols=exported,path=str(stem.with_suffix('.bin')),
                entries={entry:pc+6*i for i,entry in enumerate(spec['entries'])},
                sha256=hashlib.sha256(code).hexdigest())


def build(output,simulation=False,autonomous=False,timer_batch=False):
    if timer_batch and not autonomous:raise ValueError('timer batches require the autonomous application')
    menu=autonomous and not timer_batch
    code_start,code_end=(0x4000,0x5800) if autonomous and not timer_batch else (CODE_START,CODE_END)
    # Menu builds: overlay statics and locals fill 0x1730-0x17AF, after the bank statics.
    scratch,state_address,page_address=(0x1730,0x17B0,0x2800) if menu else (0x2280,0x2380,0x2300)
    scratch_limit=state_address-scratch if menu else 128
    output.mkdir(parents=True,exist_ok=True)
    # The fixed radio UI uses ASCII space through underscore (uppercase only); the menu
    # build's graphical screen needs no glyph after 'Z' (FONT_LAST in app.c).
    glyphs=59 if menu else 64
    font=(ROOT/'assets/font-5x8.bin').read_bytes()[:glyphs*5]
    if len(font)!=glyphs*5:raise ValueError('radio font is incomplete')
    (output/'font.h').write_text('static const __code u8 font[]={'+','.join(str(v) for v in font)+'};\n')
    compiler=tool('sdcc');linker=tool('sdld')
    sources=[ROOT/'firmware/app.c',ROOT/('firmware/sim_hardware.c' if simulation else 'firmware/hardware.c')]
    if autonomous and not timer_batch:sources.append(ROOT/'firmware/standalone.c')
    defines=[f'-DRADIO_STATE={state_address}',f'-DRADIO_PAGE={page_address}']
    if autonomous:defines.append('-DRADIO_AUTONOMOUS')
    if autonomous and not timer_batch:defines.append('-DRADIO_NATIVE_AUDIO')
    if menu:defines.append('-DRADIO_MENU')
    objects=[]
    for source in sources:
        obj=output/(source.stem+'.rel');objects.append(obj)
        subprocess.run([compiler,'-c','-mmcs51','--model-large','--std-c99','--opt-code-size',
                        *defines,'-I'+str(ROOT/'firmware'),'-I'+str(output),str(source),'-o',str(obj)],check=True)
    stem=output/'radio'
    subprocess.run([linker,'-n','-i','-m','-w','-b',f'CSEG={code_start:#x}','-b',f'XSEG={scratch:#x}',
                    str(stem),*[str(p) for p in objects]],check=True)
    mapping=stem.with_suffix('.map').read_text()
    symbols=link_symbols(mapping)
    # Menu builds share internal RAM 0x0A-0x0F with the banks; stock code owns the rest.
    for area,limit in [('l_DSEG',BANK_DSEG if menu else 32),('l_BSEG',0),('l_OSEG',0),('l_ISEG',0),('l_PSEG',0),('l_XSEG',scratch_limit)]:
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
           'cn','status','adc1','adc2','last_key','injected_key','command','frames','events','tunes','page_crc','pending','entry_bad','read_channel','standby','clock_ticks',
           *(['menu','item','handoff','message','reg7','band_frequency','eq','stereo'] if menu else [])]
    fields={name:memory[symbols['_app_layout']+i] for i,name in enumerate(names)}
    exports={name:int(address,16) for address,name in re.findall(r'^\s*(?:[A-Z]:)?\s*([0-9A-Fa-f]{8})\s+(_\w+)\b',mapping,re.M)}
    banks=[build_bank(output,spec,exports,defines,compiler,linker) for spec in BANKS] if menu else []
    if menu and simulation:
        banks.append(build_bank(output,BANKS[1],exports,defines,compiler,linker,pc=SIM_SYSTEM_PC))
    report=dict(version=VERSION if menu else 'OPEN-0.2.6' if autonomous else 'OPEN-0.1',format='RAM executable; not an SPI flash image',
                autonomous=autonomous,timer_batch=timer_batch,
                code_address=code_start,code_bytes=len(code),code_end=code_start+len(code),
                tick_milliseconds=int(re.search(r'#define TICK_MS (\d+)',(ROOT/'firmware/radio.h').read_text())[1]),
                state_address=state_address,state_bytes=fields['size'],fields=fields,
                internal_data_bytes=BANK_DSEG+BANK_DSEG_LIMIT if menu else symbols.get('l_DSEG',8),
                scratch_address=scratch,scratch_bytes=symbols.get('l_XSEG',0),
                page_buffer_address=page_address,entries=entries,simulation=simulation,banks=banks,
                sha256=hashlib.sha256(code).hexdigest(),
                compiler=subprocess.check_output([compiler,'--version'],text=True).splitlines()[0],
                source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in [*sources,*([ROOT/spec['source'] for spec in BANKS] if menu else []),
                                         ROOT/'firmware/radio.h',ROOT/'assets/font-5x8.bin',Path(__file__)]})
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
