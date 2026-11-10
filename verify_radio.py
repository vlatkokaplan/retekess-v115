#!/usr/bin/env python3
"""Instruction tests for the independently compiled C radio application."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from build_radio import build,ROOT
from lcd_draw import Asm
from emulation import run,copy_xram
from run_radio import wrapper
KEY_POWER,KEY_MENU,KEY_DELETE=0x14,0x0C,0x11
import radio_audio


def frame(state,autonomous=False):
    freq=state['frequency'];mode=state['mode']
    value=(f'{freq//40}.{freq%40*25:03} MHZ' if mode==0 else f'{freq} KHZ')
    if state['editing']:value='ENTRY TOO LARGE' if state['entry_bad'] else f"ENTER {state['entry']}_"
    hint=('TUNER READ ERROR' if state['error']&1 else 'CHECK FREQUENCY' if state['error']&16 else
          'HARDWARE ERROR' if state['error'] else 'PLAY OK  DEL BACK' if state['editing'] else 'FM/AM  SW  +/- TUNE')
    lines=['RETEKESS OPEN '+('0.2.3' if autonomous else '0.1'),['FM','MW','SW'][mode]+(' SLEEP' if state['sleeping'] else ' RADIO'),value,
           'C/N '+('--' if state['cn']==255 else str(state['cn']))+' DB '+('TUNING' if state['pending'] else 'LOCK' if state['status']&32 else '----'),
           f"VOL {state['volume']-24} "+('MUTED' if state['muted'] else 'SOUND'),hint,
           '0-9 FREQ  PLAY MUTE','MENU LCD  POWER '+('STBY' if autonomous else 'EXIT')]
    data=bytearray(1024);font=(ROOT/'assets/font-5x8.bin').read_bytes()
    for y,line in enumerate(lines):
        assert len(line)<=21
        for x,c in enumerate(line):data[y*128+x*6:y*128+x*6+5]=font[(ord(c)-32)*5:(ord(c)-31)*5]
    return bytes(data)


def execute(manifest,code,ops):
    f=manifest['fields'];a=Asm(0xA000)
    state_address=manifest['state_address']
    left,right=manifest['scratch_address']-1,state_address+manifest['state_bytes']
    def put(addr,value):a.movdptr(addr);a.movai(value);a.movxda()
    put(0x7042,0)
    a.movdi(0x81,0x8F);a.movdi(0xA8,0x80);a.movdi(0x86,0)
    for direct in range(16):a.movdi(direct,0x30+direct)
    a.movdi(0xD0,8);a.movdi(0xF0,0x77);a.movdptr(0x1234);a.movai(0x44)
    a.emit(0x12,0x60,0)
    # Capture context before copy helpers or comparisons can disturb it.
    for dest,source in [(0x70,0xE0),(0x71,0x82),(0x72,0x83),(0x73,0xD0)]:a.emit(0x85,source,dest)
    a.movdptr(0x9000)
    for source in [0x70,0x71,0x72,0x73,0xF0,0x81,0xA8,*range(16)]:a.movad(source);a.movxda();a.emit(0xA3)
    put(left,0xA5);put(right,0xB6)
    for index,op in enumerate(ops):
        if 'inject' in op:put(state_address+f['injected_key'],op['inject'])
        if 'held' in op:put(0x7040,op['held'])
        if 'fault' in op:put(0x7041,op['fault'])
        for k in range(op.get('ticks',1)):a.emit(0x12,0x61,0)
    copy_xram(a,state_address,0xB000,manifest['state_bytes'],'state')
    copy_xram(a,0x8000,0xB100,1024,'frame')
    copy_xram(a,0x9000,0xB500,23,'context')
    copy_xram(a,left,0xB517,1,'left');copy_xram(a,right,0xB518,1,'right')
    copy_xram(a,0x7000,0xB520,32,'tuner')
    copy_xram(a,0x8400,0xB600,manifest['state_bytes'],'shown')
    stop=a.base+len(a.code);a.emit(0x80,0xFE)
    output=run([(manifest['code_address'],code),(a.base,a.assemble()),
                (0x6000,wrapper(manifest['entries']['app_init'],manifest['internal_data_bytes'],0x6000)),
                (0x6100,wrapper(manifest['entries']['app_tick'],manifest['internal_data_bytes'],0x6100))],
               a.base,stop,0xB000,0x680,timeout=30)
    raw=output[:manifest['state_bytes']];words={'frequency','entry','frames','events','tunes','read_channel','clock_ticks'}
    result={name:int.from_bytes(raw[offset:offset+(2 if name in words else 1)],'little')
            for name,offset in f.items() if name not in ('size','page_crc')}
    wanted=bytes([0x44,0x34,0x12,8,0x77,0x8F,0x80,*range(0x30,0x40)])
    assert output[0x500:0x517]==wanted,('CPU context',output[0x500:0x517].hex(),wanted.hex())
    assert output[0x517:0x519]==b'\xA5\xB6','scratch/state allocation crossed'
    pixels=output[0x100:0x500]
    if not result['sleeping']:
        raw_shown=output[0x600:0x600+manifest['state_bytes']]
        shown={name:int.from_bytes(raw_shown[offset:offset+(2 if name in words else 1)],'little')
               for name,offset in f.items() if name not in ('size','page_crc')}
        # RF metrics can advance between scheduled redraws; controls must not lag.
        for name in ('mode','frequency','entry','entry_bad','editing','volume','muted','error','pending'):
            assert shown[name]==result[name],('stale control display',name,shown,result)
        expected=frame(shown,manifest['autonomous'])
        assert pixels==expected,('pixel mismatch',result,next(i for i,(x,y) in enumerate(zip(pixels,expected)) if x!=y))
    return result,output[0x520:0x540]


def digits(value):return [dict(inject=0x16+int(c)) for c in str(value)]+[dict(inject=2)]


def board_init_checks(output,autonomous=False):
    """Execute the real board driver with loader-style pull-ups initially set."""
    manifest=build(output,False,autonomous);code=(output/'radio.bin').read_bytes()
    mapping=(output/'radio.map').read_text()
    entry=int(re.search(r'C:\s+([0-9A-F]+)\s+_hardware_init\s',mapping)[1],16)
    for first,second in [(0x50,8),(0,0),(255,255)]:
        a=Asm(0xA000)
        def put(address,value):a.movdptr(address);a.movai(value);a.movxda()
        a.movdi(0x81,0x8F);a.movdi(0x86,0);a.movdi(0xA8,0x80)
        for reg,value in [(0xBA,0),(0xBB,0),(0xBD,0),(0xD3,255),(0xDA,255)]:a.movdi(reg,value)
        put(0x7825,first);put(0x7841,second);put(manifest['state_address']+manifest['fields']['error'],0)
        a.emit(0x12,0x60,0)
        for i,address in enumerate([0x7825,0x7841,0x7880,manifest['state_address']+manifest['fields']['error']]):
            a.movdptr(address);a.emit(0xE0);a.movdptr(0xB000+i);a.movxda()
        a.movdptr(0xB004)
        for reg in [0xBA,0xBB,0xBD,0xD3,0xDA]:a.movad(reg);a.movxda();a.emit(0xA3)
        stop=a.base+len(a.code);a.emit(0x80,0xFE)
        got=run([(manifest['code_address'],code),(a.base,a.assemble()),
                 (0x6000,wrapper(entry,manifest['internal_data_bytes'],0x6000))],
                a.base,stop,0xB000,9)
        # SPI completion is deliberately absent: the real driver must time out.
        expected=bytes([first&0xBF,second&0xF7,23,4,0x40,8,8,0xFE,0])
        assert got==expected,('real GPIO initialization',got.hex(),expected.hex())
    key_entry=int(re.search(r'C:\s+([0-9A-F]+)\s+_hardware_key\s',mapping)[1],16)
    for pin,want,error in [(4,KEY_POWER,0),(0,0,2)]:
        a=Asm(0xA000);a.movdi(0x81,0x8F);a.movdi(0x86,0);a.movdi(0xA0,pin)
        a.movdptr(manifest['state_address']+manifest['fields']['error']);a.movai(0);a.movxda()
        a.emit(0x12,0x60,0)
        copy_xram(a,manifest['state_address']+manifest['fields']['error'],0xB001,1,'error')
        stop=a.base+len(a.code);a.emit(0x80,0xFE)
        wrapped=wrapper(key_entry,manifest['internal_data_bytes'],0x6000)
        call=bytes([0x12,key_entry>>8,key_entry&255])
        wrapped=wrapped.replace(call,call+bytes.fromhex('e5 82 90 b0 00 f0'))
        got=run([(manifest['code_address'],code),(a.base,a.assemble()),(0x6000,wrapped)],a.base,stop,0xB000,2)
        assert got==bytes([want,error]),('physical POWER input and bounded ADC fallback',pin,got.hex())
    amp_entry=int(re.search(r'C:\s+([0-9A-F]+)\s+_hardware_amplifier\s',mapping)[1],16)
    for initial in (0,255):
        for enabled in (0,1):
            a=Asm(0xA000);a.movdi(0x81,0x8F);a.movdi(0x86,0)
            a.movdi(0xB0,initial);a.movdi(0xBD,initial)
            a.movdptr(0x7809);a.movai(initial);a.movxda()
            a.movdi(0x82,enabled);a.emit(0x12,0x60,0)
            a.movdptr(0xB000)
            for reg in (0xB0,0xBD):a.movad(reg);a.movxda();a.emit(0xA3)
            a.movdptr(0x7809);a.emit(0xE0);a.movdptr(0xB002);a.movxda()
            stop=a.base+len(a.code);a.emit(0x80,0xFE)
            got=run([(manifest['code_address'],code),(a.base,a.assemble()),
                     (0x6000,wrapper(amp_entry,manifest['internal_data_bytes'],0x6000))],a.base,stop,0xB000,3)
            expected=bytes([(initial&0xDF)|(enabled<<5),initial&0xDF,initial&0xF7])
            assert got==expected,('active-high speaker enable preserves unrelated pins',initial,enabled,got.hex())
    return manifest['sha256']


def audio_start_checks():
    operations=[('xwrite',0x7890,255),('xmodify',0x7890,0xF7,2),
                ('smodify',0x9C,0x7F,0),('atwrite',3,0),('atread',7)]
    code,size=radio_audio.program(operations)
    a=Asm(0xA000)
    a.movdi(0x81,0x8F);a.movdi(0xA8,0x80);a.movdi(0x86,0);a.movdi(0x9C,255)
    for direct in range(16):a.movdi(direct,0x30+direct)
    a.movdi(0xD0,8);a.movdi(0xF0,0x77);a.movdptr(0x1234);a.movai(0x44)
    a.emit(0x12,0x22,0)
    for dest,source in [(0x70,0xE0),(0x71,0x82),(0x72,0x83),(0x73,0xD0)]:a.emit(0x85,source,dest)
    a.movdptr(0xB010)
    for source in [0x70,0x71,0x72,0x73,0xF0,0x81,0xA8,*range(16)]:a.movad(source);a.movxda();a.emit(0xA3)
    copy_xram(a,radio_audio.RESULT,0xB000,size,'audio_results')
    stop=a.base+len(a.code);a.emit(0x80,0xFE)
    got=run([(radio_audio.BASE,code),(a.base,a.assemble())],a.base,stop,0xB000,0x27)
    assert got[:size]==bytes([255,0xF7,0x7F,0xC3,0,0x87,0]),('audio operations/timeout',got.hex())
    wanted=bytes([0x44,0x34,0x12,8,0x77,0x8F,0x80,*range(0x30,0x40)])
    assert got[0x10:]==wanted,('audio CPU context',got.hex())


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,default=ROOT/'build/radio-sim')
    p.add_argument('--autonomous',action='store_true');a=p.parse_args()
    manifest=build(a.output.resolve(),True,a.autonomous);code=(a.output/'radio.bin').read_bytes()
    cases=[([],dict(mode=0,frequency=3812,error=0)),
           ([dict(ticks=10)],dict(cn=18,status=106)),
           ([dict(inject=3)],dict(frequency=3813)),
           ([dict(inject=3),dict(inject=1)],dict(frequency=3812)),
           ([dict(inject=4)],dict(mode=1,frequency=999)),
           ([dict(inject=0x12)],dict(mode=2,frequency=6000)),
           ([dict(inject=2)],dict(muted=1)),
           ([dict(inject=KEY_MENU)],dict(sleeping=1)),
           ([dict(inject=KEY_MENU),dict(inject=0x17)],dict(sleeping=0,editing=0)),
           ([dict(held=0x17,ticks=1)],dict(events=0)),
           ([dict(held=0x17,ticks=2)],dict(events=1,entry=1,editing=1)),
           ([dict(inject=9)]*25,dict(volume=43)),([dict(inject=8)]*25,dict(volume=24)),
           ([dict(inject=0x17),dict(inject=KEY_MENU)],dict(editing=0,error=0)),
           ([dict(inject=0x0A),dict(inject=5)],dict(sleeping=0,standby=0,command=0))]
    cases.extend([
        ([dict(ticks=6),dict(fault=2,inject=3),dict(ticks=8)],
         dict(frequency=3813,read_channel=2612,pending=1,error=0)),
        ([dict(ticks=6),dict(fault=2,inject=3),dict(ticks=150)],
         dict(frequency=3813,read_channel=2612,pending=0,error=16)),
        ([dict(ticks=6),dict(fault=1,inject=3),dict(ticks=150)],
         dict(frequency=3813,pending=0,error=17,cn=255)),
        ([dict(inject=KEY_MENU),dict(held=9,ticks=35)],
         dict(sleeping=0,volume=27,events=2)),
        ([dict(inject=0x12)]+digits(30000)[:-1]+[dict(inject=0x16),dict(inject=KEY_DELETE),dict(inject=2)],
         dict(mode=2,frequency=30000,editing=0,error=0)),
        ([dict(held=0x17),dict(held=0),dict(held=0x17,ticks=2)],dict(entry=1,events=1)),
        ([dict(held=0x1F,ticks=3),dict(held=0,ticks=2),dict(held=0x1B,ticks=3),
          dict(held=0,ticks=2),dict(held=0x19,ticks=3),dict(held=0,ticks=2),
          dict(held=2,ticks=3),dict(held=0,ticks=7)],dict(frequency=3812,editing=0,events=4,pending=0)),
    ])
    for value in (300,953,1017,2300):cases.append((digits(value),dict(frequency=value*4,editing=0,error=0)))
    for value in (0,299,2301,18000,30000,70001,90000):cases.append((digits(value),dict(frequency=3812,editing=1,error=16)))
    for mode,key in [(1,4),(2,0x12)]:
        for value in ((522,999,1710) if mode==1 else (150,6000,30000)):
            cases.append(([dict(inject=key)]+digits(value),dict(mode=mode,frequency=value,editing=0,error=0)))
        for value in ((521,1000,1711,70001) if mode==1 else (149,6001,30001,70001,90000)):
            cases.append(([dict(inject=key)]+digits(value),dict(mode=mode,editing=1,error=16)))
    if a.autonomous:
        cases.extend([
            ([dict(ticks=6),dict(inject=KEY_POWER)],dict(standby=1,sleeping=1,pending=0)),
            ([dict(inject=KEY_POWER),dict(inject=9),dict(inject=0x17)],dict(standby=1,volume=27,editing=0)),
            ([dict(inject=KEY_POWER),dict(inject=KEY_POWER),dict(ticks=6)],dict(standby=0,sleeping=0,pending=0)),
            ([dict(inject=4),dict(inject=3),dict(inject=9),dict(inject=2),dict(inject=KEY_POWER),dict(inject=KEY_POWER),dict(ticks=6)],
             dict(mode=1,frequency=1008,volume=28,muted=1,standby=0,pending=0)),
            ([dict(held=KEY_POWER,ticks=40)],dict(standby=1,events=1)),
            ([dict(held=KEY_POWER,ticks=40),dict(held=0,ticks=2),dict(held=KEY_POWER,ticks=10)],dict(standby=0,events=2,pending=0)),
            ([dict(inject=KEY_MENU),dict(inject=KEY_POWER)],dict(standby=1,sleeping=1)),
        ])
    for index,(ops,want) in enumerate(cases):
        got,tuner=execute(manifest,code,ops)
        for name,value in want.items():assert got[name]==value,(index,name,got,want)
        if not got['error'] and not got['pending'] and not got['standby']:
            chan=((tuner[20]&31)<<8)|tuner[21]
            actual=chan+1200 if got['mode']==0 else chan*(3 if got['mode']==1 else 5)
            assert actual==got['frequency'],('channel conversion',got,actual)
        if got['pending'] or got['error']&16 and not got['editing']:
            assert tuner[0]&4,('unsettled or failed tune must remain muted',got,tuner.hex())
            assert tuner[31]==0,('unsettled or failed tune must disable speaker',got,tuner.hex())
        if got['standby']:assert tuner[0]&0x84==4 and tuner[30]==0 and tuner[31]==0,('standby power',got,tuner.hex())
        elif not got['pending'] and not got['error']:assert tuner[31]==1,('settled tune enables speaker',got,tuner.hex())
    hardware_sha=board_init_checks(a.output/'hardware',a.autonomous)
    audio_start_checks()
    report=dict(cases_passed=len(cases),version=manifest['version'],autonomous=a.autonomous,board_initialization_cases_passed=3,power_gpio_cases_passed=2,
                amplifier_gpio_cases_passed=4,loader_pullups_cleared=True,lcd_timeout_bounded=True,hardware_sha256=hardware_sha,
                audio_start_context_preserved=True,audio_transfer_timeout_bounded=True,
                audio_source_sha256=hashlib.sha256(Path(radio_audio.__file__).read_bytes()).hexdigest(),
                cpu_context_preserved=True,frame_pixels_verified=True,
                allocation_sentinels_preserved=True,frequency_entry_and_grids_verified=True,
                fm_25khz_steps_verified=True,scope='Compiled app.c instructions with instrumented board services',
                stale_channel_rejected=True,tune_timeout_muted=True,held_wake_consumed=True,
                simulation_sha256=manifest['sha256'],source_sha256=manifest['source_sha256'])
    (a.output/'validation.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
