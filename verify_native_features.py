#!/usr/bin/env python3
"""Run native feature instructions in uCsim with instrumented board services."""
import argparse
import hashlib
import json
import re
import subprocess
from env_tools import executable
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from analyze import intel_hex
from band_patch import ROOT, decode
from bandwidth_patch import handler as bandwidth_handler
from lcd_draw import Asm
import native_features as n
from emulation import copy_xram,run as emulate
from signal_graph import history_control,graph_renderer
from radio_ui import home_renderer
from radio_scan import scan_control,scan_renderer

CTL_BASE, DISPLAY_BASE, SHIM = 0x6000, 0x6800, 0x5000
SETTINGS_BASE, MENU_BASE = 0x3000, 0x3800
GRAPH_BASE, GRAPH_DISPLAY_BASE, HOME_BASE = 0x5800, 0xB000, 0xB800
SCAN_BASE, SCAN_DISPLAY_BASE = 0xA000, 0xA800
FRAME = 0x9000
RECORD = n.STATE_SIZE + 32 + 16 + 1024


def shim():
    a = Asm(SHIM)

    def bump(addr):
        n.read(a, addr)
        a.emit(0x04)
        a.movxda()

    def error(code):
        n.write(a, 0x7000, code)
        a.ret()

    for bank,target in [(n.GRAPH_BANK,GRAPH_BASE),(n.GRAPH_DISPLAY_BANK,GRAPH_DISPLAY_BASE),
                        (n.HOME_BANK,HOME_BASE),(n.SCAN_BANK,SCAN_BASE),(n.SCAN_DISPLAY_BANK,SCAN_DISPLAY_BASE)]:
        nxt='bank_'+str(bank);a.cjne_ai(bank,nxt);a.movdi(0x83,target>>8);n.jump(a,'enter_bank');a.L(nxt)
    a.cjne_ai(n.SETTINGS_BANK,'menu_bank')
    a.movdi(0x83,SETTINGS_BASE>>8);n.jump(a,'enter_bank')
    a.L('menu_bank');a.cjne_ai(n.MENU_BANK,'settings_write')
    a.movdptr(MENU_BASE);n.jump(a,'enter_bank')
    a.L('settings_write');a.cjne_ai(0x0E,'control_bank')
    a.movad(0x83);a.cjne_ai(0x1C,'bad_settings')
    a.movad(0x82);a.cjne_ai(0x84,'bad_settings')
    a.movar(1);a.cjne_ai(0xE0,'bad_settings')
    a.movar(4);a.cjne_ai(n.PREF_SIZE,'bad_settings')
    a.movar(5);n.jnz(a,'bad_settings')
    a.emit(0xA8,0x81,0x18,0x18,0xE6);n.jnz(a,'bad_settings')
    bump(0x700E)
    a.movri(7,n.PREF_SIZE);a.movri(6,0)
    a.L('save_pref_byte')
    a.emit(0x8A,0x82,0x8B,0x83,0xE0,0xFC,0xA3,0xAA,0x82,0xAB,0x83)
    a.movdi(0x83,0x7E);a.movar(6);a.movda(0x82);a.movar(4);a.movxda()
    a.incr(6);a.djnz(7,'save_pref_byte');a.ret()
    a.L('bad_settings');error(4)
    a.L('control_bank')
    a.cjne_ai(n.CONTROL_BANK, 'display')
    a.movdi(0x83, CTL_BASE >> 8)
    n.jump(a, 'enter_bank')
    a.L('display')
    a.cjne_ai(n.DISPLAY_BANK, 'tuner')
    a.movdptr(DISPLAY_BASE)
    n.jump(a, 'enter_bank')
    a.L('tuner')
    a.cjne_ai(0x1D, 'lcd')
    a.movad(0x82);a.cjne_ai(0x8B,'not_tune')
    bump(0x7004);a.ret()
    a.L('not_tune')
    a.cjne_ai(0xB0, 'read')
    n.jump(a, 'enter_bank')
    a.L('read')
    a.cjne_ai(0x3E, 'write')
    a.movar(2)
    a.cjne_ai(4, 'bad_read')
    bump(0x7001)
    a.movdptr(0x7F00)
    a.movar(1)
    a.movda(0x82)
    for reg in (2, 3, 4, 5):
        a.emit(0xE0, 0xF8 + reg, 0xA3)
    a.ret()
    a.L('bad_read')
    error(1)
    a.L('write')
    a.cjne_ai(0x17, 'bad_write')
    a.movar(1)
    a.cjne_ai(7, 'bad_write')
    a.emit(0xA8, 0x81, 0x18, 0x18, 0xE6)
    a.cjne_ai(1, 'bad_write')
    a.movar(3)
    a.emit(0x4C, 0x4D)
    n.jnz(a, 'bad_write')
    bump(0x7002)
    a.movdptr(0x7F07)
    a.movar(2)
    a.movxda()
    a.ret()
    a.L('bad_write')
    error(2)

    a.L('lcd')
    a.cjne_ai(2, 'bad_bank')
    a.movad(0x83)
    a.cjne_ai(0x18,'lcd_draw')
    a.movad(0x82);a.cjne_ai(0x7E,'bad_bank')
    bump(0x700F)
    a.emit(0x24,0x0F,0xF5,0x82);a.movdi(0x83,0x70)
    a.movar(1);a.movxda();a.ret()
    a.L('lcd_draw')
    a.cjne_ai(0x1A, 'line')
    a.movad(0x82)
    a.cjne_ai(4, 'bad_bank')
    bump(0x7004)
    a.ret()
    a.L('line')
    a.movad(0x83)
    a.cjne_ai(0x19, 'bad_bank')
    a.movad(0x82)
    a.cjne_ai(0x96, 'bad_bank')
    bump(0x7003)
    # line_update transfers 256 bytes at page*128, matching the native ABI.
    a.movri(6, 0)
    a.L('line_copy')
    a.movdptr(0x2800)
    a.movar(6)
    a.movda(0x82)
    a.emit(0xE0, 0xFC)
    a.movar(1)
    a.movdi(0xF0, 128)
    a.emit(0xA4, 0x2E, 0xF5, 0x82)
    a.movad(0xF0)
    a.emit(0x34, FRAME >> 8, 0xF5, 0x83)
    a.movar(4)
    a.movxda()
    a.incr(6)
    a.movar(6)
    n.jnz(a, 'line_copy')
    a.ret()
    a.L('bad_bank')
    error(3)

    a.L('enter_bank')
    a.movai(0)
    a.push(0xE0)
    a.emit(0x73)  # fake IAR bank-return byte, JMP @A+DPTR

    a.L('pref_read')
    a.movar(3);a.cjne_ai(0x40,'bad_pref_read')
    a.movar(4);a.cjne_ai(0x13,'bad_pref_read')
    a.movar(5);n.jnz(a,'bad_pref_read')
    a.movar(2);a.emit(0xC3,0x94,0xE0);a.movda(0x82)
    a.emit(0xC3,0x94,n.PREF_SIZE);a.jnc('bad_pref_read')
    a.movdi(0x83,0x7E);a.emit(0xE0,0xF9);a.ret()
    a.L('bad_pref_read');error(5)
    a.L('stock_draw');bump(0x7005);a.ret()
    a.L('get_msg')
    a.movdptr(0x7007)
    a.movad(0x9C)
    a.movxda()
    a.movri(1, 0)
    a.ret()
    a.L('stock_key')
    a.movdptr(0x7006)
    a.movar(1)
    a.movxda()
    a.emit(0xA3)
    a.emit(0xA3)
    a.movad(0x9C)
    a.movxda()
    a.ret()
    a.L('delay')
    bump(0x7005)
    a.ret()
    return a.assemble(), a.labels


def normalized_record(record):
    return list(record)+[0]*(n.PREF_SIZE-len(record))


def valid_record(record):
    if len(record)<8 or record[:2]!=[0x52,0x33] or record[7]!=0xA5:return False
    if record[3]>=64 or record[4]>=4 or record[5]!=record[3]^255 or record[6]!=record[4]^255:return False
    if record[2]==1:return True
    if record[2]!=2 or len(record)!=16:return False
    return all(record[index]<limit and record[index+4]==record[index]^255 for index,limit in [(8,4),(9,3),(10,2),(11,4)])


def expected(case):
    mode,lock,scan,task=(case.get(k,v) for k,v in [('mode',1),('lock',0),('scan',0),('task',3)])
    flags=case.get('flags',0);reg7=case.get('reg7',0xA1)
    record=normalized_record(case.get('stored',[0]*8))
    loaded=valid_record(record)
    scan_cfg=record[8:12] if loaded and record[2]==2 else [1,1,1,0]
    scanner=None;scanner_case=None;scanner_ops=[]
    pref=case.get('pref_reg',record[3] if valid_record(record) else reg7&63)
    timeout=case.get('timeout',record[4] if valid_record(record) else 0)
    idle=case.get('idle',0);nv_dirty=case.get('nv_dirty',0);save_ticks=case.get('save_ticks',0)
    dirty=case.get('dirty',0);block=case.get('wake_block',0);last=case.get('last_key',0)
    writes=frames=popups=saves=0;commands=[];output=0
    count=case.get('bl_count',45);level=case.get('bl_level',0)
    saved_count=case.get('saved_bl_count',45);saved_level=case.get('saved_bl_level',0)
    safe=task==3 and scan==0 and mode in (1,2,3)
    messages=case.get('messages',[case.get('msg',0)])
    def release(msg):return msg&0xE0 in (0x80,0xC0)
    def commit():
        nonlocal nv_dirty,record,saves
        if nv_dirty:
            if not valid_record(record) or record[2]!=2 or record[3:5]!=[pref,timeout] or record[8:12]!=scan_cfg:
                record=[0x52,0x33,2,pref,timeout,pref^255,timeout^255,0xA5,*scan_cfg,*[x^255 for x in scan_cfg]];saves+=1
            nv_dirty=0
    def close():
        nonlocal flags,dirty
        commit();flags&=0xC2;dirty=1
    for msg in messages:
        output=msg;handled=False;done=False;action=0;auto=False
        physical=0<msg<0xE0
        if block and (msg&31==last or physical):
            if release(msg):block=0
            handled=done=True
        if not done and flags&2:
            if msg in (0,0xF8,0xFE):done=True
            else:
                flags&=~2;commands += [0x2F,0xAF];dirty=1
                count,level=saved_count,saved_level
                if physical:
                    block=0 if release(msg) else 1;last=msg&31
                    handled=done=True
        if not done and msg==0xFE:dirty=1
        if not done and safe:
            if msg==0xFE and not flags&n.MENU:
                if nv_dirty:
                    save_ticks+=1
                    if save_ticks==3:commit()
                if timeout:
                    idle+=1
                    if idle>=[0,10,30,60][timeout]:
                        commit();auto=True;action=5
            elif msg not in (0,0xFE,0xF8):
                idle=0
                if not lock:
                    if flags&n.SCAN_UI:
                        from verify_radio_scan import model as scan_model
                        scanner_ops.append(dict(key=msg,lock=lock))
                        scanner=scan_model(dict(scanner_case,ops=scanner_ops))[-1]
                        flags=scanner['flags'];scan_cfg[:]=[scanner['threshold'],scanner['dwell'],scanner['direction'],scanner['range']]
                        nv_dirty|=scanner['nv'];dirty=1
                        if not flags&n.SCAN_UI or msg==n.LONG_SCREEN:commit()
                        if msg==n.LONG_SCREEN:action=5
                        elif msg&31>=0x16 and msg<0xF8:action=7
                    elif msg==n.LONG_MENU:
                        if flags&n.MENU:close()
                        else:flags=(flags&0xC2)|n.MENU
                        block=1;last=n.LONG_MENU&31;action=7
                    elif flags&n.MENU:
                        if msg==0x96:close();action=7
                        elif msg==0x9F:flags^=n.HELP;action=7
                        elif msg==0x9B:
                            if not flags&n.HELP:
                                timeout=(timeout+1)&3;nv_dirty=1;save_ticks=0
                            action=7
                        elif msg==0x9C:close();action=5
                        elif msg==0x9D:close();action=6
                        elif msg==0x9E:
                            from verify_radio_scan import model as scan_model
                            flags=(flags&0xC2)|n.MENU|n.SCAN_UI
                            scanner_case=dict(mode=mode,freq=case.get('freq',953),mw10=case.get('mw10',0),
                                              threshold=scan_cfg[0],dwell=scan_cfg[1],direction=scan_cfg[2],range=scan_cfg[3])
                            scanner_ops=[];scanner=scan_model(scanner_case)[0];action=7
                        elif flags&n.HELP:action=7
                        elif msg in (0x97,0x98,0x99,0x9A):action=msg-0x96 if mode==1 else 7
                        elif msg&31>=0x16:action=7
                        elif 0x80<=msg<0xC0:close()
                    elif msg==n.LONG_SCREEN:commit()
            if action==7:handled=done=True
            elif action==5:
                saved_count,saved_level=count,level;flags|=2
                if not auto:block=0 if release(msg) else 1;last=msg&31
                count,level=0,32;commands += [0xAE,0x28];handled=done=True
            elif action==6:
                flags^=1;handled=done=True
            if not done and not lock:
                before=reg7
                if msg==n.LONG_DIAG or flags&1 and msg==n.SHORT_3:
                    flags^=1;handled=True
                elif flags&1 and msg==0x9D:
                    flags^=n.GRAPH;handled=True
                elif flags&1 and flags&n.GRAPH and msg==0x96:
                    handled=True
                elif msg==n.LONG_SCREEN or flags&1 and msg==n.SHORT_4:
                    saved_count,saved_level=count,level
                    flags|=2;block=0 if release(msg) else 1;last=msg&31
                    count,level=0,32;commands += [0xAE,0x28];handled=True
                elif mode==1 and (action==1 or msg==n.LONG_BW or flags&1 and msg==n.SHORT_1):
                    reg7=(reg7&0xFC)|((reg7-1)&3);writes+=1;handled=True
                elif mode==1 and (action==2 or msg==n.LONG_MONO or flags&1 and msg==n.SHORT_2):
                    reg7=(reg7&0xF3)|(0 if reg7&4 else 4)
                    writes+=1;handled=True
                elif mode==1 and (action==3 or msg==n.LONG_BASS or flags&1 and msg==n.SHORT_5):
                    reg7^=16
                    writes+=1;handled=True
                elif mode==1 and (action==4 or msg==n.LONG_DE or flags&1 and msg==n.SHORT_6):
                    reg7^=32
                    writes+=1;handled=True
                elif (action or flags&1) and 0x80<=msg<0xC0:
                    flags&=0xC2
                elif flags&1 and msg in (0xF8,0xFE):dirty=1
                if before!=reg7:
                    pref=reg7&63;nv_dirty=1;save_ticks=0
        if handled:output=0;dirty=1
        if flags&2 and not safe:
            flags&=0xF0;commands += [0x2F,0xAF];dirty=1
            count,level=saved_count,saved_level
        elif flags&2:count,level=0,32
        elif not safe:flags&=0xC2
        else:
            if mode==1 and reg7&63!=pref:
                reg7=reg7&0xC0|pref;writes+=1;dirty=1
            if case.get('draw_hook') and case.get('stock_redraw',1):dirty=1
            if dirty:frames+=1;dirty=0
    return dict(flags=flags,message=output,reg7=reg7,writes=writes,
                frame=bool(frames and safe and not flags&2),frames=frames,popups=popups,
                commands=commands,block=block,last=last,bl_count=count,bl_level=level,
                pref=pref,timeout=timeout,idle=idle,nv_dirty=nv_dirty,saves=saves,record=record,scan_cfg=scan_cfg,scanner=scanner,
                tunes=scanner['tunes'] if scanner else 0)


def frame_lines(case, state):
    mode, flags = state[n.MODE], state[n.FLAGS]
    if flags&n.SCAN_UI:
        from verify_radio_scan import screen_lines
        return screen_lines(state)
    if flags&n.MENU:
        from verify_radio_ui import settings_lines
        return settings_lines(state)
    freq = state[n.FREQ] | state[n.FREQ + 1] << 8
    rf = state[n.RF] if state[n.RF] < 128 else state[n.RF] - 256
    offset = state[n.METRICS + 6]
    offset = offset if offset < 128 else offset - 256
    voltage = state[n.VOLTAGE] | state[n.VOLTAGE + 1] << 8
    if mode == 1:
        header = f'FM {freq // 10}.{freq % 10}MHZ'
        bw = [150, 200, 50, 100][state[n.REG7] & 3]
        band = f'BW:{bw}KHZ ' + ('MONO' if state[n.REG7] & 4 else 'AUTO')
        offset_text = str(offset)
        stereo = str(int(bool(state[n.METRICS + 3] & 128)))
        footer = '3 EXIT 7 GRAPH 4 OFF'
    else:
        header = ('MW ' if mode == 2 else 'AM ') + f'{freq}KHZ'
        band = 'AM SIGNAL MONITOR'
        offset_text = ('-' if offset < 0 else '') + f'{abs(offset) // 10}.{abs(offset) % 10}'
        stereo, footer = '-', '3 EXIT 7 GRAPH 4 OFF'
    return [header,
            f'C/N:{state[n.CN]}DB ' + ('LOCK' if state[n.METRICS] & 32 else '----'),
            f'RF~{rf}DBUV R:{state[n.METRICS + 7] & 127}',
            f'OFF:{offset_text}KHZ ST:{stereo}',
            f'IC:{voltage // 100}.{voltage % 100:02d}V G:{state[n.RFGAIN]}/{state[n.IFGAIN]}',
            band, ('BASS:' + ('ON' if state[n.REG7]&16 else 'OFF') + ' DE:' + ('50US' if state[n.REG7]&32 else '75US')) if mode==1 else 'FW '+n.VERSION+' 4:OFF', footer]


def reference_frame(lines):
    font = n.font_bytes()
    data = bytearray(1024)
    for page, line in enumerate(lines):
        if len(line) > 21:
            raise ValueError(f'expected screen line too long: {line}')
        for column, ch in enumerate(line):
            data[page * 128 + column * 6:page * 128 + column * 6 + 5] = font[(ord(ch)-32)*5:(ord(ch)-31)*5]
    return bytes(data)


def run_batch(cases, candidate=None):
    glue, entries = n.common_glue()
    ctl, _ = n.control(CTL_BASE)
    disp, _ = n.renderer(DISPLAY_BASE)
    prefs,_=n.settings(SETTINGS_BASE)
    menu,_=n.menu_renderer(MENU_BASE)
    extra=[(GRAPH_BASE,history_control(GRAPH_BASE)[0]),(GRAPH_DISPLAY_BASE,graph_renderer(GRAPH_DISPLAY_BASE)[0]),
           (HOME_BASE,home_renderer(HOME_BASE)[0]),(SCAN_BASE,scan_control(SCAN_BASE)[0]),
           (SCAN_DISPLAY_BASE,scan_renderer(SCAN_DISPLAY_BASE)[0])]
    bw, _ = bandwidth_handler()
    service, services = shim()
    # Execute the emitted call-site bytes as well as the generated bank code.
    # The short hook copies contain absolute LCALLs and NOPs, so their wrapper
    # addresses do not affect the instructions or targets under test.
    hooks=[]
    if candidate is not None:
        for start,code in [(0x1800+n.COMMON_PC,glue),
                           (n.CONTROL_FLASH,n.control()[0]),
                           (n.DISPLAY_FLASH,n.renderer()[0]),
                           (0x3000+n.SETTINGS_BANK*0x800,n.settings()[0]),
                           (0x3000+n.MENU_BANK*0x800,n.menu_renderer()[0]),
                           (0x11EB0,bw),
                           (0x3000+n.GRAPH_BANK*0x800,history_control()[0]),
                           (0x3000+n.GRAPH_DISPLAY_BANK*0x800,graph_renderer()[0]),
                           (0x3000+n.HOME_BANK*0x800,home_renderer()[0]),
                           (0x3000+n.SCAN_BANK*0x800,scan_control()[0]),
                           (0x3000+n.SCAN_DISPLAY_BANK*0x800,scan_renderer()[0])]:
            if candidate[start:start+len(code)]!=code:
                raise ValueError(f'candidate machine code differs at {start:#x}')
        for name,pc,length,wrapper in [('init',0x4A12,6,0x7100),
                                       ('draw',0x4A5F,3,0x7130),('wait',0x4A62,3,0x7110),
                                       ('message',0x4A65,3,0x7120)]:
            offset=0x1D800+pc-0x4000
            hooks.append((wrapper,bytes(candidate[offset:offset+length])+b'\x22'))
            entries[name]=wrapper
    a = Asm(0x8000)
    for index, case in enumerate(cases):
        a.movdi(0x81, 0x8F)
        a.movdi(0xD0, 0)
        n.write(a,0x3BE2,0x40);n.write(a,0x3BE3,0x13)
        n.write(a,0x3B3A,case.get('reg7',0xA1))
        stored=normalized_record(case.get('stored',[0]*8));loaded=valid_record(stored)
        for k,value in enumerate(stored):n.write(a,0x7E00+k,value)
        a.emit(0x12, entries['init'] >> 8, entries['init'] & 255)
        for suffix, addr, count, value in [('logs', 0x7000, 32, 0), ('frame', FRAME, 0, 0x5A)]:
            a.movdptr(addr)
            a.movri(0, count)
            a.movri(1, 4 if suffix == 'frame' else 1)
            a.movai(value)
            a.L(f'{suffix}{index}')
            a.movxda()
            a.emit(0xA3)
            a.djnz(0, f'{suffix}{index}')
            a.djnz(1, f'{suffix}{index}')
        regs = list(case.get('regs', [0]*28))
        regs[7] = case.get('reg7', 0xA1)
        for reg in [7,*range(20,28)]:
            n.write(a, 0x7F00 + reg, regs[reg])
        for addr, value in [(n.STATE+n.FLAGS, case.get('flags', 0)),
                            (n.STATE+n.DIRTY, case.get('dirty', 0)),
                            (n.STATE+n.PREF_REG,case.get('pref_reg',stored[3] if loaded else regs[7]&63)),
                            (n.STATE+n.TIMEOUT,case.get('timeout',stored[4] if loaded else 0)),
                            (n.STATE+n.IDLE,case.get('idle',0)),
                            (n.STATE+n.NV_DIRTY,case.get('nv_dirty',0)),
                            (n.STATE+n.SAVE_TICKS,case.get('save_ticks',0)),
                            (n.STATE+n.WAKE_BLOCK,case.get('wake_block',0)),
                            (n.STATE+n.LAST_KEY,case.get('last_key',0)),
                            (n.STATE+n.BL_COUNT,case.get('saved_bl_count',45)),
                            (n.STATE+n.BL_LEVEL,case.get('saved_bl_level',0)),
                            (0x3B71,case.get('bl_count',45)),(0x3B72,1),
                            (0x3AB8, case.get('task', 3)), (0x3CD0, case.get('scan', 0)),
                            (0x3B3A, regs[7]), (0x3CDC, case.get('freq', 953) & 255),
                            (0x3CDD, case.get('freq', 953) >> 8),
                            (0x27FF, 0xA5), (0x2900, 0xB6),
                            (n.STATE-1, 0xC7), (n.STATE+n.STATE_SIZE, 0xD8)]:
            n.write(a, addr, value)
        a.movri(0, 0x18)
        a.emit(0x76, case.get('mode', 1))
        a.movri(0,0x8B);a.emit(0x76,case.get('mw10',0))
        n.write(a,0x3AAC,0)
        a.movri(0, 0x8C)
        a.emit(0x76, case.get('lock', 0))
        a.movdi(0x9C, case.get('pcon1', 0))
        a.movdi(0x90,0xDF|case.get('bl_level',0))
        for reg in range(8):
            a.movri(reg, case.get('msg', 0) if reg == 1 else 0x20 + reg)
        a.movdi(0xF0, 0x77)
        a.movdi(0xD0, 0x80 if index & 1 else 0)
        a.movai(0x44)
        for msg in case.get('messages',[case.get('msg',0)]):
            a.movri(1,msg)
            a.emit(0x12,entries['message']>>8,entries['message']&255)
            a.movdptr(0x7040)
            for source in [2,3,4,5,6,7,0xF0,0x81]:
                a.movad(source);a.movxda();a.emit(0xA3)
            if case.get('draw_hook'):
                n.write(a,0x3CD1,case.get('stock_redraw',1))
                a.emit(0x12,entries['draw']>>8,entries['draw']&255)
            a.emit(0x12,entries['wait']>>8,entries['wait']&255)
        for source,dest in [(0x3B71,0x7009),(0x3B72,0x700A),(0x7F07,0x700C),(0x3B3A,0x700D)]:
            n.read(a,source);a.movdptr(dest);a.movxda()
        a.movad(0x90);a.anlai(32);a.movdptr(0x700B);a.movxda()
        out = 0xA000 + index * RECORD
        for source, length in [(n.STATE, n.STATE_SIZE), (0x7000, 32), (0x7040, 8),
                               (0x27FF, 1), (0x2900, 1), (n.STATE-1, 1), (n.STATE+n.STATE_SIZE, 1),
                               (FRAME, 1024)]:
            copy_xram(a,source,out,length,f'copy{index}_{source}_{out}')
            out+=length
        # Pad the fixed record tail to RECORD bytes.
        for address in range(out, 0xA000 + (index+1)*RECORD):
            n.write(a, address, 0)
    stop = a.base + len(a.code)
    a.emit(0x80, 0xFE)
    if len(a.code) + a.base > SCAN_BASE:
        raise ValueError('test wrapper too big; reduce batch')
    chunks = [(n.COMMON_PC, glue), (CTL_BASE, ctl), (DISPLAY_BASE, disp), (0x1EB0, bw),
              (0x100, bytes.fromhex('d0 e0 22')), (0x133, bytes([2, SHIM >> 8, SHIM & 255])),
              (0x20B1, bytes([2, services['get_msg'] >> 8, services['get_msg'] & 255])),
              (0x42DB, bytes([2, services['stock_key'] >> 8, services['stock_key'] & 255])),
              (0x2054, bytes([2, services['delay'] >> 8, services['delay'] & 255])),
              (0x4184,bytes([2,services['stock_draw']>>8,services['stock_draw']&255])),
              (0x208A,bytes([2,services['pref_read']>>8,services['pref_read']&255])),
              (SETTINGS_BASE,prefs),(MENU_BASE,menu),
              (SHIM, service), (a.base, a.assemble()), *extra, *hooks]
    output=emulate(chunks,a.base,stop,0xA000,len(cases)*RECORD,timeout=60)
    return [output[i*RECORD:(i+1)*RECORD] for i in range(len(cases))]


def check(case, result):
    want = expected(case)
    size=n.STATE_SIZE
    state,logs=result[:size],result[size:size+32]
    if (state[n.PREF_REG],state[n.TIMEOUT],state[n.IDLE],state[n.NV_DIRTY],logs[14]) != (want['pref'],want['timeout'],want['idle'],want['nv_dirty'],want['saves']):
        raise ValueError(f'preferences mismatch: {case}: {state.hex()}, {logs.hex()}, {want}')
    if list(state[n.PREF_RECORD:n.PREF_RECORD+n.PREF_SIZE])!=want['record']:
        raise ValueError(f'stored preferences mismatch: {case}: {state.hex()}, {want}')
    if logs[0]:
        raise ValueError(f'native ABI error {logs[0]}: {case}')
    if state[n.FLAGS] != want['flags'] or logs[6] != want['message']:
        raise ValueError(f'key/flag mismatch: {case}: {state[:8].hex()}, {logs.hex()}, {want}')
    if logs[2] != want['writes'] or logs[8] != case.get('pcon1',0):
        raise ValueError(f'write count or clocks during stock handler wrong: {case}, {logs.hex()}')
    pcon1=case.get('pcon1',0)
    if logs[7]!=pcon1 or state[n.ACTIVE]:
        raise ValueError(f'unexpected clock change: {case}')
    if list(logs[16:16+logs[15]])!=want['commands']:
        raise ValueError(f'LCD sleep/wake commands mismatch: {case}: {logs.hex()}, {want}')
    if logs[12]!=want['reg7'] or logs[13]!=want['reg7']:
        raise ValueError(f'tuner/shadow register mismatch: {case}: {logs.hex()}, {want}')
    if state[n.WAKE_BLOCK]!=want['block'] or state[n.LAST_KEY]!=want['last']:
        raise ValueError(f'wake gesture mismatch: {case}: {state.hex()}, {want}')
    if logs[9]!=want['bl_count'] or logs[11]!=want['bl_level']:
        raise ValueError(f'backlight state mismatch: {case}: {logs.hex()}, {want}')
    if logs[4]!=want['tunes']:raise ValueError(f'scanner tune count mismatch: {case}, {logs.hex()}, {want}')
    if logs[5]!=(len(case.get('messages',[0])) if case.get('draw_hook') and not (case.get('task',3)==3 and case.get('scan',0)==0 and case.get('mode',1) in (1,2,3)) else 0):
        raise ValueError(f'native draw fallback mismatch: {case}, {logs.hex()}')
    if logs[1]!=want['frames']*2 or logs[3]!=want['frames']*4+want['popups']:
        raise ValueError(f'display/tuner traffic while asleep or render count mismatch: {case}: {logs.hex()}, {want}')
    if result[size+32:size+40] != bytes([0x22,0x23,0x24,0x25,0x26,0x27,0x77,0x8F]):
        raise ValueError(f'CPU context mismatch: {case}: {result[size+32:size+40].hex()}')
    if result[size+40:size+44] != bytes([0xA5,0xB6,0xC7,0xD8]):
        raise ValueError(f'buffer/state bounds violated: {case}: {result[size+40:size+44].hex()}')
    if want['frame']:
        regs = case.get('regs', [0]*28)
        gain_rf, gain_if = (regs[24] >> 5) & 7, (regs[24] >> 2) & 7
        level = (123 if case.get('mode',1)==2 else 103) - (regs[27]&127) - 6*(gain_rf+gain_if)
        cn = regs[23 if case.get('mode',1)==1 else 22] & 127
        voltage = 180 + 5*(regs[25]&63)
        if state[n.CN] != cn or state[n.RF] != level & 255:
            raise ValueError(f'signal arithmetic mismatch: {case}: {state.hex()}')
        if (state[n.VOLTAGE] | state[n.VOLTAGE+1]<<8) != voltage:
            raise ValueError('voltage arithmetic mismatch')
        frame = result[size+44:size+44+1024]
        if state[n.FLAGS]&n.SCAN_UI or state[n.FLAGS]&n.MENU or state[n.FLAGS]&1 and not state[n.FLAGS]&n.GRAPH:
            lines=frame_lines(case,state);pixels=reference_frame(lines)
        elif state[n.FLAGS]&n.GRAPH and state[n.FLAGS]&1:
            from verify_signal_graph import graph_frame
            lines=['C/N history'];pixels=graph_frame(state)
        else:
            from verify_radio_ui import home_frame
            lines=['Large radio home'];pixels=home_frame(state,case.get('mw10',0))
        if frame!=pixels:
            first=next(i for i,(x,y) in enumerate(zip(frame,pixels)) if x!=y)
            raise ValueError(f'framebuffer mismatch at {first}: {lines}; state={state.hex()}')
        return frame, lines
    return None


def verify_lcd_guard(candidate):
    """Run the patched stock prologue/return branch for every page byte."""
    glue,labels=n.common_glue()
    guard=bytes([0x12,labels['lcd_guard']>>8,labels['lcd_guard']&255,0,0,0])
    if candidate[0x402B:0x4031]!=guard:
        raise ValueError('final image is missing the LCD transfer guard')
    cases=[(flags,owned,painting,page) for flags in (0,2) for owned in (0,1) for painting in (0,1) for page in range(256)]
    for first in range(0,len(cases),128):
        batch=cases[first:first+128]
        a=Asm(0x8000)
        for index,(flags,owned,painting,page) in enumerate(batch):
            a.movdi(0x81,0x60);a.movdi(0xD0,0)
            n.write(a,n.STATE+n.FLAGS,flags);n.write(a,n.STATE+n.UI_ACTIVE,owned);n.write(a,n.STATE+n.UI_PAINT,painting);n.write(a,0x7000,0)
            a.movdi(0xD8,0xA5);a.movdi(0xF0,0x55)
            a.movri(1,31);a.movri(2,page);a.movdptr(0x5A5A)
            a.emit(0x12,0x18,0x2B)
            a.emit(0xAE,0x82,0xAF,0x83)
            n.read(a,0x7000);a.movdptr(0xA000+index*8);a.movxda();a.emit(0xA3)
            for source in (1,2,0xF0,0xD8,0x81,6,7):
                a.movad(source);a.movxda();a.emit(0xA3)
        stop=a.base+len(a.code);a.emit(0x80,0xFE)
        # Mark the first instruction after the guarded branch, then return.
        # No hardware service is needed; asleep/out-of-range calls reach the
        # unmodified stock LJMP at 187b instead.
        marker=bytes.fromhex('90 70 00 74 01 f0 22')
        chunks=[(n.COMMON_PC,glue),(0x1800,bytes(candidate[0x4000:0x4800])),
                (0x1833,marker),(0x100,b'\x22'),(a.base,a.assemble())]
        with tempfile.TemporaryDirectory(prefix='v115-lcd-guard-') as directory:
            path=Path(directory)/'guard.hex';path.write_text(intel_hex(chunks))
            commands=f'file "{path}"\nrun {a.base:#x} {stop:#x}\ndump /h xram 0xa000 {0xa000+len(batch)*8-1:#x} 32\nquit\n'
            r=subprocess.run([executable('s51'),'-b','-q','-t','C52','-c','-'],
                             input=commands,text=True,capture_output=True,timeout=30,check=True)
        found={}
        for line in r.stdout.splitlines():
            m=re.match(r'^0x([0-9a-fA-F]+)\s+((?:[0-9a-fA-F]{2}(?:\s+|$))+)',line)
            if m:
                for offset,value in enumerate(bytes.fromhex(m[2])[:32]):found[int(m[1],16)+offset]=value
        for index,(flags,owned,painting,page) in enumerate(batch):
            result=bytes(found[0xA000+index*8+k] for k in range(8))
            active=not flags&2 and (not owned or painting) and page<8
            want=bytes([int(active),31,page,0x55,0xA5,0x60,
                        0 if active else 0x5A,0x70 if active else 0x5A])
            if result!=want:raise ValueError(f'LCD guard failed for {flags=}, {page=}: {result.hex()}')
    return len(cases)


def all_cases(smoke=False):
    regs = [0]*28
    regs[20:28] = [0xEA,0x34,0x80|9,0x80|14,0x32,30,0xF2,14]
    cases = [dict(flags=1,dirty=1,regs=regs),
             dict(mode=2,flags=1,dirty=1,freq=999,regs=regs),
             dict(mode=3,flags=1,dirty=1,freq=30000,regs=regs)]
    if not smoke:
        cases += [dict(msg=msg,flags=flags,mode=mode) for msg in range(256)
                  for flags in (0,1,2,3) for mode in (1,2,3)]
        cases += [dict(msg=msg,mode=mode,lock=lock,scan=scan,task=task,flags=1)
                  for msg in (n.LONG_DIAG,n.LONG_BW,n.LONG_MONO,n.LONG_SCREEN,n.LONG_BASS,n.LONG_DE)
                  for mode,lock,scan,task in [(0,0,0,3),(1,1,0,3),(2,0,1,3),(3,0,0,2),(255,0,0,3)]]
        cases += [dict(mode=mode,pcon1=value) for mode in (1,2,3) for value in range(256)]
        cases += [dict(mode=1,msg=msg,reg7=value) for value in range(256)
                  for msg in (n.LONG_BW,n.LONG_MONO,n.LONG_BASS,n.LONG_DE)]
        for mode in (1,2,3):
            for offset in (0,1,127,128,255):
                for gain in (0,0xFC):
                    r=regs.copy();r[24]=gain;r[25]=63;r[26]=offset;r[27]=127
                    cases.append(dict(mode=mode,flags=1,dirty=1,regs=r,freq=2300 if mode==1 else 150))
        sequences=[
            [n.LONG_SCREEN,0xFA,0xDA,0xF8,0xFE,0],
            [n.LONG_SCREEN,0xDA,0x57,0x97],
            [n.LONG_SCREEN,0xDA,0x57,n.LONG_BW,0xF7,0xD7],
            [n.LONG_SCREEN,0xDA,n.LONG_SCREEN,0xFA,0xDA],
            [n.SHORT_4,0xFE,0xF8,0],
            [n.SHORT_4,n.SHORT_1],
            [n.SHORT_4,0xE1],
            [n.LONG_BASS,n.LONG_BASS,n.LONG_DE,n.LONG_DE,n.LONG_MONO,n.LONG_MONO]]
        cases += [dict(mode=mode,flags=flags,messages=messages)
                  for mode in (1,2,3) for flags in (0,1) for messages in sequences]
        cases += [dict(flags=2,messages=[0x40|key,0xA0|key,0xE0|key,0xC0|key])
                  for key in range(1,32)]
        cases += [dict(mode=mode,task=task,scan=scan,flags=flags)
                  for flags in (2,3) for mode,task,scan in
                  [(1,2,0),(2,3,1),(3,0,0),(0,3,0),(255,3,0)]]
        cases += [dict(msg=msg,flags=flags,mode=mode,dirty=1)
                  for msg in range(256) for flags in (n.MENU,n.MENU|n.HELP) for mode in (1,2,3)]
        cases += [dict(stored=[0x52,0x33,1,pref,timeout,pref^255,timeout^255,0xA5])
                  for pref in range(64) for timeout in range(4)]
        valid=[0x52,0x33,1,0x21,2,0xDE,0xFD,0xA5]
        for index in range(8):
            for value in (0,1,3,4,64,255):
                record=valid.copy();record[index]=value
                cases.append(dict(stored=record))
        cases += [dict(mode=mode,lock=lock,timeout=timeout,idle=seconds-1,messages=[0xF8,0xFE,0xFE,0x57,0x97])
                  for mode in (1,2,3) for lock in (0,1) for timeout,seconds in [(1,10),(2,30),(3,60)]]
        edits=[
            [n.LONG_MENU,0xFE,0xDE,0x97,0x98,0x99,0x9A,0x9B,0x9B,0x9B,0x9B,0x96],
            [n.LONG_MENU,0xDE,0x9F,0xFE,0xF8,0x9F,0x96],
            [n.LONG_MENU,0xFE,0xDE,n.LONG_MENU,0xFE,0xDE],
            [n.LONG_MENU,0xDE,0x98,0x98,0x96],
            [n.LONG_MENU,0xDE,0x9B,0x9C,0x57,0x97],
            [n.LONG_MENU,0xDE,0x9B,0x9D,0xFE,0xFE,0xFE],
            [n.LONG_BASS,0xDD,0xFE,0xFE,0xFE,0xFE],
            [n.LONG_DE,0xFE,n.LONG_SCREEN,0xDA],
        ]
        cases += [dict(mode=mode,messages=messages,stored=stored)
                  for mode in (1,2,3) for messages in edits for stored in ([0]*8,valid)]
        cases += [dict(msg=msg,flags=flags,mode=mode) for msg in range(256)
                  for flags in (1|n.GRAPH,3|n.GRAPH) for mode in (1,2,3)]
        cases += [dict(mode=mode,flags=0,dirty=1,regs=regs,freq=freq,mw10=mw10)
                  for mode,freq,mw10 in [(1,953,0),(2,999,0),(2,1000,1),(3,30000,0)]]
        cases += [dict(draw_hook=True,stock_redraw=redraw,mode=mode,task=task,scan=scan,flags=flags)
                  for mode,task,scan in [(1,3,0),(2,3,0),(3,3,0),(1,2,0),(2,3,1),(0,3,0)]
                  for flags in (0,1,1|n.GRAPH,n.MENU) for redraw in (0,1)]
        from itertools import product
        def v2(config):return [0x52,0x33,2,0x21,1,0xDE,0xFE,0xA5,*config,*[value^255 for value in config]]
        cases += [dict(stored=v2(config),flags=n.MENU,msg=0x9E) for config in product(range(4),range(3),range(2),range(4))]
        scan_sequences=[
            [n.LONG_MENU,0xDE,0x9E,0x98,0x99,0x9A,0x9E,0x96],
            [n.LONG_MENU,0xDE,0x9E,0x9B,0x9B,0x9B,0x9C,0x96],
            [n.LONG_MENU,0xDE,0x9E,n.LONG_SCREEN,0xDA,0x57,0x97,0x96],
            [n.LONG_MENU,0xDE,0x9E,0x9D,0x89],
            [n.LONG_MENU,0xDE,0x9E,0x9D,n.LONG_MENU,n.LONG_MENU,0xDE],
        ]
        cases += [dict(mode=mode,freq=freq,messages=messages,stored=stored)
                  for mode,freq in [(1,953),(2,999),(3,10000)] for messages in scan_sequences
                  for stored in ([0]*8,v2([1,1,1,0]))]
    return cases


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report', type=Path, default=ROOT/'build/native-features-emulation.json')
    p.add_argument('--smoke', action='store_true')
    p.add_argument('--candidate',type=Path,help='also verify and execute hooks from this final image')
    args = p.parse_args()
    candidate_raw=args.candidate.read_bytes() if args.candidate else None
    candidate=decode(candidate_raw) if candidate_raw is not None else None
    source_hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                   ('native_features.py','bandwidth_patch.py','verify_native_features.py','lcd_draw.py','native_ui.py','radio_ui.py','signal_graph.py','radio_scan.py','emulation.py','assets/font-5x8.bin')}
    components={name:hashlib.sha256(fn()[0]).hexdigest() for name,fn in
                [('common',n.common_glue),('control',n.control),('display',n.renderer),('settings',n.settings),('menu',n.menu_renderer),
                 ('history',history_control),('graph',graph_renderer),('home',home_renderer),('scanner',scan_control),('scanner_display',scan_renderer),('bandwidth',bandwidth_handler)]}
    cases=all_cases(args.smoke)
    frames = []
    for start in range(0,len(cases),4):
        batch=cases[start:start+4]
        outputs=run_batch(batch,candidate)
        for case,output in zip(batch,outputs):
            frame=check(case,output)
            if frame and len(frames)<3:frames.append(frame)
        if start and start%200==0:print(f'{start}/{len(cases)} native cases passed',flush=True)
    if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha for name,sha in source_hashes.items()):
        raise ValueError('implementation source changed during verification')
    report = dict(machine_code_cases_passed=len(cases),source_sha256=source_hashes,
                  assembled_sha256=components,full_suite=not args.smoke,
                  checks=['key dispatch and guards','all 256 bandwidth/mono/bass/de-emphasis register values',
                          'all 256 clock values unchanged; quiet gating removed',
                          'native bank-call arguments, register preservation and stack balance',
                          'signed signal/offset arithmetic and framebuffer pixels',
                          'LCD buffer and allocated state boundary sentinels',
                          'LCD sleep/wake commands, refresh suppression and backlight restoration',
                          'full wake gestures consumed, hold repeats and system/timer events',
                          'settings/help framebuffer pixels and all menu key events in FM/MW/SW',
                          'persistent record validation, all 64 FM preferences and four timeout choices',
                          'native settings ABI, legacy migration and bounded sixteen-byte save/readback',
                          'deferred shortcut saving, unchanged preferences skip writes, automatic sleep boundaries',
                          'large home screen, graph switching, native draw ownership and fallback',
                          'scanner menu transitions, pause/resume, sleep and saved scan options'],
                  scope='Actual new instructions run at relocated bank addresses; native I2C/LCD/message services are instrumented. Full stock runtime and RF hardware are not emulated.')
    if candidate_raw is not None:
        report['candidate_sha256']=hashlib.sha256(candidate_raw).hexdigest()
        report['checks'].append('final-image bank bytes and emitted init/wait/message hooks')
        report['stock_lcd_guard_cases_passed']=verify_lcd_guard(candidate)
    if args.smoke:
        report['checks']=['three representative FM/MW/SW screens and their signal calculations']
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,indent=2)+'\n')
    (ROOT/'build/previews').mkdir(parents=True,exist_ok=True)
    for index,(frame,lines) in enumerate(frames):
        image=Image.new('1',(128,64))
        for y in range(64):
            for x in range(128):image.putpixel((x,y),(frame[y//8*128+x]>>(y%8))&1)
        ImageOps.invert(image.convert('L')).resize((768,384),Image.Resampling.NEAREST).save(ROOT/f'build/previews/native-diagnostics-{index}.png')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
