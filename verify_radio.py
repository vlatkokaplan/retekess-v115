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
from stock_audio import VOLUME_REGISTERS,verify_stock_curve


MENU_LABELS=['MP3 PLAYER','MP3 EQ','BAND','VOLUME','FM FILTER','STEREO','BASS BOOST','DE-EMPHASIS',
             'SCREEN OFF','STANDBY','POWER OFF']
(M_MP3,M_EQ,M_BAND,M_VOLUME,M_FILTER,M_STEREO,M_BASS,M_DEEMPH,M_SCREEN,M_STANDBY,M_POWEROFF)=range(len(MENU_LABELS))
FILTER_NAMES=['150K','200K','50K','100K']
EQ_NAMES=['NORMAL','POP','ROCK','JAZZ','CLASSIC','COUNTRY','BASS']
# Simulation XDATA: media presence (SD, USB, ready), banked stock-call log, board registers;
# STOCK_EQ is the stock player's preset, which the stock eq_set (09:1A54) would update.
MEDIA,CALL_LOG,TASK_ID,STOCK_EQ=0x7043,0x7060,0x3AB8,0x3B6A
EQ_SET=(0x09,0x1A,0x54)
BOARD_XDATA=[0x7809,0x7823,0x7825,0x7841,0x784E,0x7880,0x7890,0x7893,0x7850,0x7851,0x7852,0x7853,0x785B,0x785A]
# P2 is excluded: the context wrapper itself restores it around every entry.
BOARD_SFRS=[0xBA,0xBB,0xBC,0xBD,0x90,0xB0,0xFD,0xD3,0xDA,0xD9,0x9C,0xDB]


def render(rows,inverted=None):
    data=bytearray(1024);font=(ROOT/'assets/font-5x8.bin').read_bytes()
    for y,row in enumerate(rows):
        for x,line in row:
            for c in line:
                assert x<=122,('text exceeds the row',line)
                data[y*128+x:y*128+x+5]=font[(ord(c)-32)*5:(ord(c)-31)*5];x+=6
        if y==inverted:data[y*128:y*128+128]=bytes(b^255 for b in data[y*128:y*128+128])
    return bytes(data)


def menu_frame(state):
    item=state['item'];top=item-5 if item>5 else 0;reg7=state['reg7']
    values=['',EQ_NAMES[state['eq']],['FM','MW','SW'][state['mode']],str(state['volume']-24),FILTER_NAMES[reg7&3],
            'MONO' if reg7&4 else 'AUTO','ON' if reg7&0x10 else 'OFF','75US' if reg7&0x20 else '50US','','','']
    rows=[[(2,'SETTINGS'),(96,f'{item+1}/{len(MENU_LABELS)}')]]
    rows+=[[(0,' '+MENU_LABELS[i]),(78,values[i])] for i in range(top,top+6)]
    rows.append([(4,'PLAY OK   MENU EXIT')])
    data=bytearray(render(rows,1+item-top))
    for x in range(128):data[x]|=0x80;data[7*128+x]^=0xFF
    return bytes(data)


def segment_columns(glyph):
    """12 columns (32-bit, LSB = top row) of a seven-segment glyph: horizontals on rows
    1-3/14-16/27-29 over columns 1-10, verticals 3 wide on rows 2-14 and 16-28."""
    rows=lambda r:sum(1<<y for y in r)
    bits=[0x3F,0x06,0x5B,0x4F,0x66,0x6D,0x7D,0x07,0x7F,0x6F][glyph] if glyph!='cursor' else 0x08
    h=(rows(range(1,4)) if bits&1 else 0)|(rows(range(14,17)) if bits&0x40 else 0)|(rows(range(27,30)) if bits&8 else 0)
    left=(rows(range(2,15)) if bits&0x20 else 0)|(rows(range(16,29)) if bits&0x10 else 0)
    right=(rows(range(2,15)) if bits&2 else 0)|(rows(range(16,29)) if bits&4 else 0)
    return [(left if c<3 else 0)|(right if c>8 else 0)|(h if 0<c<11 else 0) for c in range(12)]


# Dial: (low, shift, divisor) per band; FM shows the 87.5-108 MHz broadcast band.
DIAL=[(3500,0,7),(522,0,10),(150,2,63)]


def radio_frame(state):
    """Graphical OPEN 0.3 radio screen: status bar, seven-segment frequency, dial,
    signal/message line and key hints."""
    data=bytearray(1024);font=(ROOT/'assets/font-5x8.bin').read_bytes()
    def put(page,x,line):
        for c in line:
            assert 32<=ord(c)<=ord('Z') and x<=122,('text exceeds the row or font',line)
            data[page*128+x:page*128+x+5]=font[(ord(c)-32)*5:(ord(c)-31)*5];x+=6
    def invert(page,width):
        for x in range(width):data[page*128+x]^=255
    mode,freq,cn,pending,reg7=state['mode'],state['frequency'],state['cn'],state['pending'],state['reg7']
    put(0,2,['FM','MW','SW'][mode]);invert(0,15)
    level=0 if pending or cn==255 else cn
    for i,mask in enumerate([0xE0,0xF0,0xF8,0xFC,0xFE]):
        data[20+3*i]=data[21+3*i]=mask if level>=6*(i+1) else 0x80
    if mode==0 and not pending:put(0,40,'MONO' if reg7&4 else 'STEREO' if state['stereo'] else '')
    data[108:112]=bytes([0x1C,0x1C,0x3E,0x7F])
    put(0,114,'X' if state['muted'] or state['volume']==24 else str(state['volume']-24))
    if state['editing']:glyphs=[*map(int,str(state['entry']) if state['digits'] else ''),'cursor']
    elif mode==0:glyphs=[*map(int,str(freq//40)),'.',*map(int,f'{freq%40*25:03}')]
    else:glyphs=[*map(int,str(freq))]
    x=107
    for glyph in reversed(glyphs):
        if glyph=='.':
            x-=5;data[4*128+x:4*128+x+3]=b'\x38\x38\x38';continue
        x-=15
        for c,column in enumerate(segment_columns(glyph)):
            for page in range(4):data[(1+page)*128+x+c]=column>>(8*page)&255
    assert x>=0,('frequency exceeds the screen',glyphs)
    if not state['editing']:put(4,110,'MHZ' if mode==0 else 'KHZ')
    for x in range(4,125):data[5*128+x]=0x38 if (x-4)%10==0 else 0x10
    low,shift,divisor=DIAL[mode]
    needle=3 if freq<low else 3+min(120,((freq-low)>>shift)//divisor)
    data[5*128+needle:5*128+needle+3]=b'\x7e\x7e\x7e'
    error=state['error']
    info=('NO SD CARD OR USB' if state['message'] else 'TUNER READ ERROR' if error&1 else
          'CHECK FREQUENCY' if error&16 else 'HARDWARE ERROR' if error else
          'PLAY OK  DEL BACK' if state['editing'] else 'TUNING' if pending else None)
    if info:put(6,0,info)
    else:
        put(6,0,'SNR '+('--' if cn==255 else str(cn))+' DB')
        if state['status']&0x20:put(6,66,'LOCK')
        if reg7&0x10:put(6,98,'BASS')
    put(7,4,'MENU SETUP  MODE MP3');invert(7,128)
    return bytes(data)


def frame(state,manifest):
    menu=bool(manifest.get('banks'));autonomous=manifest['autonomous']
    if menu:return menu_frame(state) if state['menu'] else radio_frame(state)
    freq=state['frequency'];mode=state['mode']
    value=(f'{freq//40}.{freq%40*25:03} MHZ' if mode==0 else f'{freq} KHZ')
    if state['editing']:value='ENTRY TOO LARGE' if state['entry_bad'] else f"ENTER {state['entry']}_"
    hint=('NO SD CARD OR USB' if menu and state['message'] else
          'TUNER READ ERROR' if state['error']&1 else 'CHECK FREQUENCY' if state['error']&16 else
          'HARDWARE ERROR' if state['error'] else 'PLAY OK  DEL BACK' if state['editing'] else 'FM/AM  SW  +/- TUNE')
    label,power=(('0.3.0','MENU SETUP  MODE MP3') if menu else ('0.2.6','MENU LCD  POWER STBY') if autonomous
                 else ('0.1','MENU LCD  POWER EXIT'))
    lines=['RETEKESS OPEN '+label,['FM','MW','SW'][mode]+(' SLEEP' if state['sleeping'] else ' RADIO'),value,
           'C/N '+('--' if state['cn']==255 else str(state['cn']))+' DB '+('TUNING' if state['pending'] else 'LOCK' if state['status']&32 else '----'),
           f"VOL {state['volume']-24} "+('MUTED' if state['muted'] or state['volume']==24 else 'SOUND'),hint,
           '0-9 FREQ  PLAY MUTE',power]
    return render([[(0,line)] for line in lines])


def stock_stubs(system_pc):
    """Banked-call trampoline, banked return and media services standing in for stock code.
    Calls into the two OPEN banks run them (the system bank is linked at system_pc);
    any other banked call is logged as (bank, DPH, DPL, R1) and returns."""
    t=Asm(0x0133)
    t.cjne_ai(0x2C,'system');t.push(0x82);t.push(0x83);t.ret()
    t.L('system');t.cjne_ai(0x2D,'stock')
    t.movad(0x83);t.emit(0x24,(system_pc-0x1800)>>8);t.movda(0x83);t.push(0x82);t.push(0x83);t.ret()
    t.L('stock');t.push(0xF0);t.push(0x00);t.emit(0xF5,0xF0);t.push(0x82);t.push(0x83)
    t.movdptr(CALL_LOG);t.emit(0xE0,0xF8,0x24,4,0xF0)
    t.movdi(0x83,(CALL_LOG+1)>>8);t.emit(0xE8,0x24,(CALL_LOG+1)&255,0xF5,0x82)
    t.emit(0xE5,0xF0,0xF0,0xA3);t.pop(0xE0);t.emit(0xF0,0xA3);t.pop(0xE0);t.emit(0xF0,0xA3);t.emit(0xE9,0xF0)
    t.pop(0x00);t.pop(0xF0);t.ret()
    present=bytes([0xE9,0x24,MEDIA&255,0xF5,0x82,0x75,0x83,MEDIA>>8,0xE0,0x13,0x22])
    ready=bytes([0x90,(MEDIA+2)>>8,(MEDIA+2)&255,0xE0,0x13,0x22])
    return [(0x0100,b'\x22'),(0x0133,t.assemble()),(0x2111,present),(0x119C,ready)]


def bank_chunks(manifest):
    from build_radio import SIM_SYSTEM_PC
    banks={(b['bank'],b['address']):b for b in manifest.get('banks',[])}
    if not banks:return [],None
    menu,system=banks[(0x2C,0x1800)],banks[(0x2D,SIM_SYSTEM_PC)]
    chunks=[(b['address'],Path(b['path']).read_bytes()) for b in (menu,system)]
    return chunks+stock_stubs(SIM_SYSTEM_PC),system['entries']


def execute(manifest,code,ops):
    f=manifest['fields'];a=Asm(0xA000)
    state_address=manifest['state_address']
    left,right=manifest['scratch_address']-1,state_address+manifest['state_bytes']
    banks,system=bank_chunks(manifest)
    def put(addr,value):a.movdptr(addr);a.movai(value);a.movxda()
    put(0x7042,0);put(TASK_ID,3);put(CALL_LOG,0)
    put(STOCK_EQ,next((op['boot_eq'] for op in ops if 'boot_eq' in op),0))
    for i in range(3):put(MEDIA+i,0)
    if any(op.get('clobber') for op in ops):
        # Distinct stock values the radio must hand back unchanged when it leaves.
        for i,address in enumerate(BOARD_XDATA):put(address,0x40+i)
        for i,reg in enumerate(BOARD_SFRS):a.movdi(reg,0x60+i)
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
        if op.get('reset_audio_counts'):
            for address in range(0x7018,0x701B):put(address,0)
        if 'inject' in op:put(state_address+f['injected_key'],op['inject'])
        if 'held' in op:put(0x7040,op['held'])
        if 'fault' in op:put(0x7041,op['fault'])
        if 'media' in op:
            for i,value in enumerate(op['media']):put(MEDIA+i,value)
        if op.get('clobber'):
            for address in BOARD_XDATA:put(address,0xEE)
            for reg in BOARD_SFRS:a.movdi(reg,0xEE)
        if 'stock_eq' in op:put(STOCK_EQ,op['stock_eq'])
        for reg,value in op.get('regs',{}).items():put(0x7000+reg,value)
        if op.get('leave'):a.emit(0x12,0x62,0)
        if op.get('handoff'):a.emit(0x12,0x63,0)
        if op.get('reenter'):a.emit(0x12,0x60,0)
        if op.get('leave') or op.get('handoff') or op.get('reenter') or op.get('clobber') or 'stock_eq' in op or 'boot_eq' in op or 'regs' in op:continue
        for k in range(op.get('ticks',1)):a.emit(0x12,0x61,0)
    copy_xram(a,state_address,0xB000,manifest['state_bytes'],'state')
    copy_xram(a,0x8000,0xB100,1024,'frame')
    copy_xram(a,0x9000,0xB500,23,'context')
    copy_xram(a,left,0xB517,1,'left');copy_xram(a,right,0xB518,1,'right')
    copy_xram(a,0x7000,0xB520,32,'tuner')
    copy_xram(a,0x8400,0xB600,manifest['state_bytes'],'shown')
    copy_xram(a,CALL_LOG,0xB680,65,'calls');copy_xram(a,TASK_ID,0xB6C1,1,'task')
    for i,address in enumerate(BOARD_XDATA):a.movdptr(address);a.emit(0xE0);a.movdptr(0xB6C2+i);a.movxda()
    for i,reg in enumerate(BOARD_SFRS):a.movad(reg);a.movdptr(0xB6D0+i);a.movxda()
    stop=a.base+len(a.code);a.emit(0x80,0xFE)
    chunks=[(manifest['code_address'],code),(a.base,a.assemble()),
            (0x6000,wrapper(manifest['entries']['app_init'],manifest['internal_data_bytes'],0x6000)),
            (0x6100,wrapper(manifest['entries']['app_tick'],manifest['internal_data_bytes'],0x6100))]
    if banks:
        chunks+=banks+[(0x6200,wrapper(system['radio_leave'],manifest['internal_data_bytes'],0x6200)),
                       (0x6300,wrapper(system['stock_handoff'],manifest['internal_data_bytes'],0x6300))]
    output=run(chunks,a.base,stop,0xB000,0x6E0,timeout=30)
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
        expected=frame(shown,manifest)
        assert pixels==expected,('pixel mismatch',result,next(i for i,(x,y) in enumerate(zip(pixels,expected)) if x!=y))
    log=output[0x681:0x681+output[0x680]]
    extra=dict(pixels=pixels,calls=[tuple(log[i:i+4]) for i in range(0,len(log),4)],task=output[0x6C1],
               board=output[0x6C2:0x6C2+len(BOARD_XDATA)],sfrs=output[0x6D0:0x6D0+len(BOARD_SFRS)])
    return result,output[0x520:0x540],extra


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
    volume_entry=int(re.search(r'C:\s+([0-9A-F]+)\s+_hardware_volume\s',mapping)[1],16)
    for initial in (0,7,0xFE,0xFF):
        for level in (*range(33),33,255):
            a=Asm(0xA000);a.movdi(0x81,0x8F);a.movdi(0x86,0)
            a.movdptr(0x7890);a.movai(initial);a.movxda()
            for address in range(0x7891,0x7895):a.movdptr(address);a.movai(0xA5);a.movxda()
            a.movdi(0x82,level);a.emit(0x12,0x60,0)
            copy_xram(a,0x7890,0xB000,5,'volume_registers')
            stop=a.base+len(a.code);a.emit(0x80,0xFE)
            got=run([(manifest['code_address'],code),(a.base,a.assemble()),
                     (0x6000,wrapper(volume_entry,manifest['internal_data_bytes'],0x6000))],a.base,stop,0xB000,5)
            valid=level if level<=32 else 0
            expected=bytes([(initial&0xFE)|(not valid),0xA5,0xA5,VOLUME_REGISTERS[valid],0xA5])
            assert got==expected,('real analogue gain preserves route/bias and adjacent registers',initial,level,got.hex())
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
    menu=bool(manifest.get('banks'))
    # Without the menu, MENU sleeps the LCD; with it, the menu's SCREEN OFF item does.
    sleep=[dict(inject=KEY_MENU),dict(inject=0x17+M_SCREEN),dict(inject=2)] if menu else [dict(inject=KEY_MENU)]
    cases=[([],dict(mode=0,frequency=3812,error=0)),
           ([dict(ticks=10)],dict(cn=18,status=106)),
           ([dict(inject=3)],dict(frequency=3813)),
           ([dict(inject=3),dict(inject=1)],dict(frequency=3812)),
           ([dict(inject=4)],dict(mode=1,frequency=999)),
           ([dict(inject=0x12)],dict(mode=2,frequency=6000)),
           ([dict(inject=2)],dict(muted=1)),
           (sleep,dict(sleeping=1)),
           (sleep+[dict(inject=0x17)],dict(sleeping=0,editing=0)),
           ([dict(held=0x17,ticks=1)],dict(events=0)),
           ([dict(held=0x17,ticks=2)],dict(events=1,entry=1,editing=1)),
           ([dict(inject=9)]*45,dict(volume=56)),([dict(inject=8)]*25,dict(volume=24)),
           ([dict(inject=0x17),dict(inject=KEY_MENU)],dict(editing=0,error=0)),
           ([dict(inject=0x0A),dict(inject=5)],dict(sleeping=0,standby=0,command=0,**(dict(handoff=1) if menu else {})))]
    cases.extend([
        ([dict(ticks=6),dict(fault=2,inject=3),dict(ticks=8)],
         dict(frequency=3813,read_channel=2612,pending=1,error=0)),
        ([dict(ticks=6),dict(fault=2,inject=3),dict(ticks=150)],
         dict(frequency=3813,read_channel=2612,pending=0,error=16)),
        ([dict(ticks=6),dict(fault=1,inject=3),dict(ticks=150)],
         dict(frequency=3813,pending=0,error=17,cn=255)),
        (sleep+[dict(held=9,ticks=35)],
         dict(sleeping=0,volume=25,events=len(sleep)+1)),
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
    for level in range(33):
        changes=[dict(inject=8)] if level==0 else [dict(inject=9)]*(level-1)
        cases.append(([dict(ticks=6)]+changes,dict(volume=24+level,pending=0,error=0)))
    cases.extend([
        ([dict(ticks=6),dict(inject=2)],dict(muted=1,pending=0)),
        ([dict(ticks=6),dict(inject=2),dict(inject=2)],dict(muted=0,pending=0)),
        ([dict(ticks=6),dict(inject=8),dict(inject=3),dict(ticks=6)],dict(volume=24,pending=0)),
        ([dict(ticks=6),dict(inject=8),dict(inject=9)],dict(volume=25,pending=0)),
        ([dict(ticks=6),dict(fault=2,inject=3),dict(ticks=150),dict(inject=9)],dict(error=16,pending=0,volume=26)),
        ([dict(ticks=6),dict(fault=2,inject=3),dict(ticks=150),dict(inject=2),dict(inject=2)],dict(error=16,pending=0,muted=0)),
        ([dict(held=9,ticks=320)],dict(volume=56)),
        ([dict(held=8,ticks=200)],dict(volume=24)),
    ])
    cases.extend([
        ([dict(ticks=10),dict(reset_audio_counts=True,inject=9),dict(inject=8)],
         dict(volume=25,audio_io=(0,0,0))),
        ([dict(ticks=10),dict(reset_audio_counts=True)]+[dict(inject=9)]*31+[dict(inject=8)]*31+[dict(inject=9)]*31,
         dict(volume=56,audio_io=(0,0,0))),
        ([dict(ticks=10),dict(reset_audio_counts=True,inject=8),dict(inject=9)],
         dict(volume=25,audio_io=(2,0,2))),
    ])
    if a.autonomous:
        cases.extend([
            ([dict(ticks=6),dict(inject=8),dict(inject=KEY_POWER),dict(inject=KEY_POWER),dict(ticks=6)],
             dict(volume=24,standby=0,pending=0)),
            ([dict(ticks=6),dict(inject=KEY_POWER)],dict(standby=1,sleeping=1,pending=0)),
            ([dict(inject=KEY_POWER),dict(inject=9),dict(inject=0x17)],dict(standby=1,volume=25,editing=0)),
            ([dict(inject=KEY_POWER),dict(inject=KEY_POWER),dict(ticks=6)],dict(standby=0,sleeping=0,pending=0)),
            ([dict(inject=4),dict(inject=3),dict(inject=9),dict(inject=2),dict(inject=KEY_POWER),dict(inject=KEY_POWER),dict(ticks=6)],
             dict(mode=1,frequency=1008,volume=26,muted=1,standby=0,pending=0)),
            ([dict(held=KEY_POWER,ticks=40)],dict(standby=1,events=1)),
            ([dict(held=KEY_POWER,ticks=40),dict(held=0,ticks=2),dict(held=KEY_POWER,ticks=10)],dict(standby=0,events=2,pending=0)),
            ([dict(inject=KEY_MENU),dict(inject=KEY_POWER)],dict(standby=1,sleeping=1,menu=0)),
        ])
    menu_cases=0
    if menu:
        def item(n):
            # Digits 1-9 select items 0-8, digit 0 the tenth; the last item wraps from the top.
            return [dict(inject=KEY_MENU)]+([dict(inject=0x17+n)] if n<9 else [dict(inject=0x16)] if n==9 else [dict(inject=1)])
        def keys(*k):return [dict(inject=x) for x in k]
        leave=[dict(leave=True),dict(handoff=True)]
        select_sd,select_usb=(0x0C,0x1F,0xD1,0),(0x0C,0x1F,0xD1,1)
        start=[(0,0x1D,0xC9,2),(4,0x1E,0x81,None),(0,0x1D,0xC9,0)]
        def eq(preset):return (*EQ_SET,preset)
        extra=[
            (keys(KEY_MENU),dict(menu=1,item=0,sleeping=0)),
            (keys(KEY_MENU,KEY_MENU),dict(menu=0,item=0)),
            (keys(KEY_MENU,3,3),dict(menu=1,item=2)),
            (keys(KEY_MENU,1),dict(menu=1,item=M_POWEROFF)),
            (keys(KEY_MENU,*[3]*len(MENU_LABELS)),dict(menu=1,item=0)),
            (keys(KEY_MENU,*[3]*7),dict(menu=1,item=7)),
            (keys(KEY_MENU,*[3]*10),dict(menu=1,item=10)),
            ([dict(inject=KEY_MENU),dict(held=3,ticks=60)],dict(menu=1,item=6)),
            (item(9),dict(menu=1,item=9)),
            (item(M_STEREO)+keys(0x0A,4,0x12,KEY_DELETE,5),dict(menu=1,item=M_STEREO,mode=0,handoff=0,reg7=0xA1)),
            # MP3 EQ: cycles the stock presets; the radio's own audio is unaffected.
            (item(M_EQ)+keys(9),dict(eq=1,menu=1)),
            (item(M_EQ)+keys(2,2),dict(eq=2,menu=1)),
            (item(M_EQ)+keys(9,9,9,9),dict(eq=4,menu=1)),
            (item(M_EQ)+keys(8,8),dict(eq=5,menu=1)),
            (item(M_EQ)+keys(8),dict(eq=6,menu=1)),
            (item(M_EQ)+keys(*[9]*7),dict(eq=0,menu=1)),
            (item(M_EQ)+keys(9,KEY_MENU,3),dict(eq=1,menu=0,frequency=3813,reg7=0xA1)),
            ([dict(boot_eq=4)],dict(eq=4)),
            ([dict(boot_eq=9)],dict(eq=0)),
            ([dict(boot_eq=6)]+item(M_EQ)+keys(9),dict(eq=0)),
            (item(M_BAND)+keys(9),dict(mode=1,frequency=999,menu=1)),
            (item(M_BAND)+keys(8),dict(mode=2,frequency=6000,menu=1)),
            (item(M_BAND)+keys(9,9,9),dict(mode=0,frequency=3812)),
            (item(M_BAND)+keys(9,KEY_MENU,3,3,KEY_MENU,8,KEY_MENU,4),dict(mode=1,frequency=1017,menu=0)),
            (item(M_VOLUME)+keys(9,9),dict(volume=27,menu=1)),
            (item(M_VOLUME)+keys(8),dict(volume=24,menu=1)),
            (item(M_VOLUME)+keys(2),dict(volume=25,muted=0,menu=1)),
            ([dict(ticks=6)]+item(M_VOLUME)+keys(8,9),dict(volume=25,pending=0,menu=1)),
            (item(M_FILTER)+keys(9),dict(reg7=0xA0)),(item(M_FILTER)+keys(8),dict(reg7=0xA2)),
            (item(M_FILTER)+keys(9,9),dict(reg7=0xA3)),(item(M_FILTER)+keys(9,9,9,9),dict(reg7=0xA1)),
            (item(M_STEREO)+keys(2),dict(reg7=0xA5)),(item(M_STEREO)+keys(2,9),dict(reg7=0xA1)),
            (item(M_BASS)+keys(9),dict(reg7=0xB1)),(item(M_DEEMPH)+keys(2),dict(reg7=0x81)),
            (item(M_BASS)+keys(2,KEY_MENU,4),dict(reg7=0xB1,mode=1,menu=0)),
            (item(M_SCREEN)+keys(2),dict(sleeping=1,menu=0)),
            (item(M_SCREEN)+keys(2,3),dict(sleeping=0,menu=0,frequency=3812)),
            (item(M_STANDBY)+keys(2),dict(standby=1,sleeping=1,menu=0)),
            (item(M_STANDBY)+keys(2,KEY_POWER)+[dict(ticks=6)],dict(standby=0,sleeping=0,menu=0,pending=0)),
            (item(M_POWEROFF)+keys(2),dict(handoff=0x13,menu=0)),
            (item(M_POWEROFF)+keys(9,8),dict(handoff=0,menu=1)),
            (keys(KEY_MENU,2),dict(handoff=1,menu=0)),
            (keys(0x0A,3,KEY_MENU),dict(handoff=1,frequency=3812,menu=0)),
            (keys(0x17,0x0A),dict(handoff=1,editing=0)),
            # Leaving for the stock player: stock eq_set, then the stock MODE sequence.
            ([dict(media=(1,0,1))]+keys(0x0A)+leave,dict(handoff=1,left=True,task=1,calls=[eq(0),select_sd,*start])),
            ([dict(media=(1,1,1))]+keys(0x0A)+leave,dict(handoff=1,left=True,task=1,calls=[eq(0),select_usb,*start])),
            ([dict(media=(0,1,0))]+keys(0x0A)+leave,dict(handoff=1,left=True,task=1,calls=[eq(0),select_usb])),
            ([dict(media=(0,0,1))]+keys(0x0A)+leave,dict(handoff=1,left=True,task=3,message=1,calls=[])),
            ([dict(boot_eq=3,media=(1,0,1))]+item(M_EQ)+keys(9,KEY_MENU,0x0A)+leave,
             dict(eq=4,handoff=1,left=True,task=1,calls=[eq(4),select_sd,*start])),
            ([dict(media=(1,0,1))]+item(M_POWEROFF)+keys(2)+leave,dict(handoff=0x13,left=True,task=0x13,calls=[])),
            ([dict(clobber=True)]+keys(0x0A)+[dict(leave=True)],dict(left=True,board=True)),
            # Back in the radio task: resume the band, frequencies, volume, mute and tuner options,
            # and pick up an EQ preset changed in the stock player.
            ([dict(media=(0,0,0))]+keys(0x0A)+leave+[dict(reenter=True)],dict(handoff=0,message=1,menu=0,task=3)),
            ([dict(media=(0,0,0))]+keys(0x0A)+leave+[dict(reenter=True)]+keys(3),dict(message=0,frequency=3813)),
            ([dict(media=(0,0,0))]+keys(0x0A)+leave+[dict(reenter=True),dict(inject=KEY_MENU)],dict(message=0,menu=1)),
            ([dict(media=(1,0,1))]+item(M_EQ)+keys(9,KEY_MENU,0x0A)+leave+[dict(stock_eq=5),dict(reenter=True)]+item(M_EQ),
             dict(eq=5,handoff=0,menu=1,item=M_EQ)),
            (keys(4,3,9,2)+item(M_BASS)+keys(9,KEY_MENU)+[dict(media=(1,0,1))]+keys(0x0A)+leave+[dict(reenter=True),dict(ticks=6)],
             dict(mode=1,frequency=1008,volume=26,muted=1,reg7=0xB1,handoff=0,menu=0,pending=0,task=1)),
            (keys(4,3,0x12,3)+item(M_POWEROFF)+keys(2)+leave+[dict(reenter=True),dict(ticks=6),dict(inject=4)],
             dict(mode=1,frequency=1008,handoff=0,pending=1)),
            (keys(0x12,3)+item(M_POWEROFF)+keys(2)+leave+[dict(reenter=True),dict(ticks=6),dict(inject=0x12)],
             dict(mode=2,frequency=6005,handoff=0)),
        ]
        # Graphical screen: signal bars, stereo flag, widest frequencies, dial ends, entry.
        settle=[dict(ticks=26)]
        extra+=[
            ([dict(regs={23:5})]+settle,dict(cn=5,stereo=0,pending=0)),
            ([dict(regs={23:0x80|6})]+settle,dict(cn=6,stereo=1)),
            ([dict(regs={23:0x80|29})]+settle,dict(cn=29,stereo=1)),
            ([dict(regs={23:0x80|30})]+settle,dict(cn=30,stereo=1)),
            ([dict(regs={23:0x80|100})]+settle,dict(cn=100,stereo=1)),
            ([dict(regs={23:255})]+settle,dict(cn=255,stereo=0,error=0)),
            ([dict(regs={23:0x80|30})]+settle+item(M_STEREO)+keys(2,KEY_MENU),dict(reg7=0xA5,stereo=1,menu=0)),
            ([dict(ticks=10),dict(regs={20:0x4A})]+settle,dict(status=0x4A,pending=0)),
            (keys(4)+[dict(regs={22:0x80|20})]+settle,dict(mode=1,cn=20,stereo=0)),
            (digits(2300)+settle,dict(frequency=9200,pending=0)),
            (digits(300)+settle,dict(frequency=1200)),
            (digits(875),dict(frequency=3500)),
            (digits(1080),dict(frequency=4320)),
            (digits(1081),dict(frequency=4324)),
            (digits(1500),dict(frequency=6000)),
            (keys(4)+digits(522),dict(mode=1,frequency=522)),
            (keys(4)+digits(1710),dict(mode=1,frequency=1710)),
            (keys(0x12)+digits(150),dict(mode=2,frequency=150)),
            (keys(0x12)+digits(30000)+settle,dict(mode=2,frequency=30000,pending=0)),
            (keys(0x12)+digits(1900),dict(mode=2,frequency=1900)),
            (keys(0x17),dict(editing=1,digits=1,entry=1)),
            (keys(0x17,0x16,0x1F,0x1E,0x1D),dict(editing=1,digits=5,entry=10987)),
            (keys(0x17,KEY_DELETE),dict(editing=1,digits=0)),
            (keys(0x1F,0x1F,0x1F,0x1F,0x1F,0x1F),dict(editing=1,entry_bad=1)),
            (keys(2)+settle,dict(muted=1)),
            (keys(8)+settle,dict(volume=24,muted=0)),
            (keys(*[9]*31)+settle,dict(volume=56)),
            (item(M_BASS)+keys(9,KEY_MENU)+settle,dict(reg7=0xB1,menu=0)),
        ]
        cases.extend(extra);menu_cases=len(extra)
    for index,(ops,want) in enumerate(cases):
        got,tuner,extra=execute(manifest,code,ops)
        for name,value in want.items():
            if name=='audio_io':assert tuple(tuner[24:27])==value,('volume must not interrupt amplifier or rewrite tuner',index,tuner.hex(),value)
            elif name=='task':assert extra['task']==value,('stock task id',index,extra,want)
            elif name=='calls':
                calls=extra['calls']
                assert len(calls)==len(value) and all(c[:3]==v[:3] and (v[3] is None or c[3]==v[3]) for c,v in zip(calls,value)),\
                    ('stock banked calls',index,calls,value)
            elif name=='board':
                assert extra['board']==bytes(0x40+i for i in range(len(BOARD_XDATA))),('board XDATA restore',index,extra['board'].hex())
                assert extra['sfrs']==bytes(0x60+i for i in range(len(BOARD_SFRS))),('board SFR restore',index,extra['sfrs'].hex())
            elif name=='left':pass
            else:assert got[name]==value,(index,name,got,want)
        assert tuner[7]==got.get('reg7',0xA1),('tuner options follow the state',index,got,tuner.hex())
        if not got['error'] and not got['pending'] and not got['standby']:
            chan=((tuner[20]&31)<<8)|tuner[21]
            actual=chan+1200 if got['mode']==0 else chan*(3 if got['mode']==1 else 5)
            assert actual==got['frequency'],('channel conversion',got,actual)
        if got['pending'] or got['error']&16 and not got['editing']:
            assert tuner[0]&4,('unsettled or failed tune must remain muted',got,tuner.hex())
            assert tuner[31]==0,('unsettled or failed tune must disable speaker',got,tuner.hex())
        if want.get('left'):
            # Handed back to stock code: speaker off, tuner muted/idle, LCD powered.
            assert tuner[31]==0 and tuner[30]==1 and tuner[0]==(0x44 if got['mode']==0 else 4),('leave state',index,got,tuner.hex())
        elif got['standby']:assert tuner[0]&0x84==4 and tuner[30]==0 and tuner[31]==0,('standby power',got,tuner.hex())
        elif not got['pending'] and not got['error']:
            audible=not got['muted'] and got['volume']>24
            assert tuner[31]==audible,('settled speaker follows mute and volume zero',got,tuner.hex())
            assert bool(tuner[0]&4)==(not audible),('tuner mute follows speaker mute',got,tuner.hex())
        assert tuner[29]==got['volume']-24,('analogue gain follows displayed volume',got,tuner.hex())
        assert tuner[6]==0x8C,('tuner input volume stays at stock level',got,tuner.hex())
        assert tuner[13]==0x8C,('stock tuner output pre-gain',got,tuner.hex())
    verify_stock_curve((ROOT/'stock.bin').read_bytes())
    hardware_sha=board_init_checks(a.output/'hardware',a.autonomous)
    audio_start_checks()
    report=dict(cases_passed=len(cases),version=manifest['version'],autonomous=a.autonomous,
                menu=menu,menu_cases_passed=menu_cases,
                **(dict(menu_frames_verified=True,stock_music_handoff_verified=True,state_resume_verified=True,mp3_eq_verified=True,
                        board_context_restore_verified=True,banks={f"{b['bank']:02x}@{b['address']:04x}":b['sha256'] for b in manifest['banks']})
                   if menu else {}),board_initialization_cases_passed=3,power_gpio_cases_passed=2,
                amplifier_gpio_cases_passed=4,volume_gpio_cases_passed=140,analogue_volume_levels_verified=33,stock_volume_curve_verified=True,volume_roundtrip_no_audio_interrupt=True,constant_tuner_volume=True,
                zero_volume_and_mute_disable_speaker=True,stock_tuner_pre_gain_preserved=True,loader_pullups_cleared=True,lcd_timeout_bounded=True,hardware_sha256=hardware_sha,
                audio_start_context_preserved=True,audio_transfer_timeout_bounded=True,
                audio_source_sha256=hashlib.sha256(Path(radio_audio.__file__).read_bytes()).hexdigest(),
                cpu_context_preserved=True,frame_pixels_verified=True,
                allocation_sentinels_preserved=True,frequency_entry_and_grids_verified=True,
                fm_25khz_steps_verified=True,scope='Compiled app.c instructions with instrumented board services',
                stale_channel_rejected=True,tune_timeout_muted=True,held_wake_consumed=True,
                simulation_sha256=manifest['sha256'],source_sha256=manifest['source_sha256'])
    (a.output/'validation.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
