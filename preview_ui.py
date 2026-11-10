#!/usr/bin/env python3
"""Preview RC4 screens on the physical LCD from volatile loader RAM.

Uses deterministic demonstration values, not live RF measurements. Each page
pair is CRC-checked in RAM before transfer. No flash write command is sent.
"""
import argparse,binascii,json
from pathlib import Path
from lcd_draw import Asm,build as lcd_init
from usb_dump import RawTransport
from radio_ui import home_renderer,settings_renderer
from signal_graph import graph_renderer
from radio_scan import scan_renderer
from verify_radio_ui import home_frame,settings_lines
from verify_radio_scan import screen_lines
from verify_signal_graph import graph_frame,font_frame
import native_features as n

ROOT=Path(__file__).resolve().parent
STATE,RENDER,API,LINE=0x3000,0x3200,0x2400,0x2900


def upload(t,address,data):
    block=bytes(data).ljust(512,b'\0')
    if len(block)!=512:raise ValueError('upload requires at most 512 bytes')
    t.encoded(bytes([0xFB,3])+address.to_bytes(4,'big')+bytes(10),outgoing=block)
    if t.read_ram(address,512)!=block:raise RuntimeError('RAM upload differs')


def execute(t,code):
    upload(t,0x2200,code)
    t.encoded(bytes([0xFB,2])+(0x2200).to_bytes(4,'big')+bytes(10))


def line_sender():
    a=Asm(LINE);n.save_context(a);a.movar(1);a.emit(0xFC);a.movri(6,2);a.movdptr(0x2800)
    a.L('page');a.clr(0xB4);a.movar(4);a.orlai(0xB0);a.lcall('send')
    for cmd in (0x10,0):a.movai(cmd);a.lcall('send')
    a.setb(0xB4);a.movri(5,128)
    a.L('pixels');a.emit(0xE0,0xA3);a.lcall('send');a.djnz(5,'pixels')
    a.incr(4);a.djnz(6,'page');a.setb(0xB1);n.restore_context(a);a.ret()
    a.L('send');a.clr(0xB1);a.movda(0xD8);a.movri(7,255)
    a.L('wait');a.movad(0xD9);a.movcb(0xE7);a.jnc('busy');a.sjmp('sent')
    a.L('busy');a.djnz(7,'wait');a.push(0x82);a.push(0x83);n.write(a,0x31E1,1);a.pop(0x83);a.pop(0x82)
    a.L('sent');a.setb(0xB1);a.ret();return a.assemble()


def adapter():
    a=Asm(API);n.save_context(a)
    a.cjne_ai(2,'bad');a.movad(0x83);a.cjne_ai(0x19,'bad');a.movad(0x82);a.cjne_ai(0x96,'bad')
    a.lcall('crc');a.movar(1);a.emit(0x24,0xD0,0xF5,0x82);a.movdi(0x83,0x31)
    a.movar(6);a.movxda();a.emit(0xA3);a.movar(7);a.movxda()
    a.emit(0x12,LINE>>8,LINE&255);a.sjmp('done')
    a.L('bad');n.write(a,0x31E2,1)
    a.L('done');n.restore_context(a);a.ret()
    a.L('crc');a.movri(6,255);a.movri(7,255);a.movri(0,0);a.movdptr(0x2800)
    a.L('byte');a.emit(0xE0,0x6F,0xFF);a.movri(5,8)
    a.L('bit');a.clr(0xD7);a.movar(6);a.emit(0x33,0xFE);a.movar(7);a.emit(0x33,0xFF)
    a.jnc('next_bit');a.emit(0x63,6,0x21,0x63,7,0x10)
    a.L('next_bit');a.djnz(5,'bit');a.emit(0xA3);a.djnz(0,'byte');a.ret()
    return a.assemble()


def fixture(kind):
    state=bytearray(n.STATE_SIZE);state[n.MODE]=1;state[n.FREQ]=953&255;state[n.FREQ+1]=953>>8
    state[n.CN]=14;state[n.REG7]=0xA1;state[n.PREF_REG]=0x21;state[n.TIMEOUT]=1
    state[n.GRAPH_COUNT]=32;state[n.GRAPH_PEAK]=30
    state[n.HISTORY:n.HISTORY+32]=bytes([12,13,14,12,10,11,13,16,18,20,22,25,30,28,25,22,18,16,14,12,9,8,10,14,18,22,25,24,21,18,15,14])
    if kind=='home':return state,home_renderer,home_frame(state)
    if kind=='graph':state[n.FLAGS]=1|n.GRAPH;return state,graph_renderer,graph_frame(state)
    if kind=='settings':state[n.FLAGS]=n.MENU;return state,settings_renderer,bytes(font_frame(settings_lines(state)))
    state[n.FLAGS]=n.MENU|n.SCAN_UI;state[n.SCAN_STATUS]=3
    state[n.SCAN_THRESHOLD]=1;state[n.SCAN_DWELL]=1;state[n.SCAN_DIRECTION]=1
    for field,value in [(n.SCAN_LOW,870),(n.SCAN_HIGH,1080)]:state[field]=value&255;state[field+1]=value>>8
    return state,scan_renderer,bytes(font_frame(screen_lines(state)))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--screen',choices=['home','graph','settings','scan'],default='home')
    p.add_argument('--all',action='store_true',help='check all four screens, leaving the home screen visible')
    p.add_argument('--dry',action='store_true');p.add_argument('--report',type=Path,default=ROOT/'build/ram-ui-preview.json');args=p.parse_args()
    kinds=['graph','settings','scan','home'] if args.all else [args.screen]
    components={API:adapter(),LINE:line_sender()}
    assert API+len(components[API])<=0x2600 and LINE+len(components[LINE])<=0x2B00
    if args.dry:
        print({kind:len(fixture(kind)[1](RENDER,STATE)[0]) for kind in kinds});return
    report=dict(completed=False,flash_written=False,demo_values=True,screens=[])
    t=RawTransport()
    try:
        report['identity']=t.identify();t.unlock();execute(t,lcd_init('clear'))
        for origin,code in components.items():upload(t,origin,code)
        for kind in kinds:
            state,renderer,frame=fixture(kind);code,labels=renderer(RENDER,STATE)
            if RENDER+len(code)>0x3A00:raise ValueError('renderer exceeds spare loader RAM')
            end=labels['font']-RENDER;prefix=code[:end]
            if prefix.count(bytes.fromhex('02 01 00'))!=1:raise ValueError('unexpected bank-return sequence')
            code=prefix.replace(bytes.fromhex('02 01 00'),bytes.fromhex('22 00 00')).replace(bytes.fromhex('12 01 33'),bytes([0x12,API>>8,API&255]))+code[end:]
            for offset in range(0,len(code),512):upload(t,RENDER+offset,code[offset:offset+512])
            upload(t,STATE,state)
            a=Asm(0x2200)
            for reg in (0xE0,0xD0,0x82,0x83,0xF0,0xA0):a.push(reg)
            a.anldi(0xD0,0xE7)
            for reg in range(8):a.push(reg)
            a.push(0xA8);a.clr(0xAF);a.emit(0x12,RENDER>>8,RENDER&255);a.pop(0xA8)
            for reg in (*range(7,-1,-1),0xA0,0xF0,0x83,0x82,0xD0,0xE0):a.pop(reg)
            a.ret();execute(t,a.assemble())
            data=t.read_ram(STATE,512)
            if any(data[0x1E1:0x1E3]):raise RuntimeError('LCD adapter reported a failure')
            wanted=b''.join(binascii.crc_hqx(frame[k:k+256],0xFFFF).to_bytes(2,'little') for k in range(0,1024,256))
            if data[0x1D0:0x1D8]!=wanted:raise RuntimeError('native rendered page CRCs differ from expected pixels: '+kind)
            if t.read_ram(0x2800,512)[:256]!=frame[768:]:raise RuntimeError('final LCD pages differ: '+kind)
            report['screens'].append(dict(screen=kind,all_page_pair_crcs_verified=True,final_pages_verified=True))
            print(kind+': all page CRCs and final pixels verified',flush=True)
        report['loader_after']=t.identify();report['completed']=True
    finally:
        t.close();args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
