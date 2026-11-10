#!/usr/bin/env python3
"""Execute the history ring and graph renderer, checking exact native pixels."""
import argparse,hashlib,json
from pathlib import Path
from PIL import Image
from lcd_draw import Asm
from band_patch import ROOT
from emulation import copy_xram,run,verify_image_banks
import native_features as n
from signal_graph import history_control,graph_renderer


def font_frame(lines):
    data=bytearray(1024);font=n.font_bytes()
    for page,line in enumerate(lines):
        if len(line)>21:raise ValueError('line too wide: '+line)
        for x,ch in enumerate(line):data[page*128+x*6:page*128+x*6+5]=font[(ord(ch)-32)*5:(ord(ch)-31)*5]
    return data


def graph_frame(state):
    mode=state[n.MODE];freq=state[n.FREQ]|state[n.FREQ+1]<<8
    value=f'{freq//10}.{freq%10}MHZ' if mode==1 else f'{freq}KHZ'
    data=font_frame([['','FM','MW','SW'][mode]+' '+value,
                     f'NOW:{state[n.CN]} PEAK:{state[n.GRAPH_PEAK]}DB','','','','',
                     'C/N 0-60DB  1S/SAMPLE','0 CLR 7 METER 3 EXIT'])
    count=state[n.GRAPH_COUNT];head=state[n.GRAPH_HEAD]
    for level in (0,15,30,45,60):
        y=16+31-level//2
        for x in range(0,128,2):data[y//8*128+x]|=1<<(y&7)
    for column in range(32-count,32):
        index=column-(32-count)
        if count==32:index=(head+index)&31
        height=min(30,state[n.HISTORY+index]//2)+1
        for x in range(column*4,column*4+3):
            for y in range(48-height,48):data[y//8*128+x]|=1<<(y&7)
    return bytes(data)


def render(state,fn=graph_renderer,mw10=0):
    a=Asm(0x8000);a.movdi(0x81,0x8F);a.movdi(0xD0,0)
    a.movri(0,0x8B);a.emit(0x76,mw10)
    for k,value in enumerate(state):n.write(a,n.STATE+k,value)
    n.write(a,0x27FF,0xA5);n.write(a,0x2900,0xB6)
    a.emit(0x12,0x60,0x00)
    copy_xram(a,0x9000,0xA000,1024,'copy_frame')
    copy_xram(a,0x27FF,0xA400,1,'copy_before');copy_xram(a,0x2900,0xA401,1,'copy_after')
    stop=a.base+len(a.code);a.emit(0x80,0xFE)
    shim=Asm(0x5000)
    shim.cjne_ai(2,'bad');shim.movri(6,0)
    shim.L('copy');shim.movdptr(0x2800);shim.movar(6);shim.movda(0x82);shim.emit(0xE0,0xFC)
    shim.movar(1);shim.movdi(0xF0,128);shim.emit(0xA4,0x2E,0xF5,0x82)
    shim.movad(0xF0);shim.emit(0x34,0x90,0xF5,0x83);shim.movar(4);shim.movxda()
    shim.incr(6);shim.movar(6);n.jnz(shim,'copy');shim.ret()
    shim.L('bad');n.write(shim,0xA400,0);shim.ret()
    result=run([(a.base,a.assemble()),(0x6000,fn(0x6000)[0]),(0x5000,shim.assemble()),
                (0x133,bytes.fromhex('02 50 00')),(0x100,b'\x22')],a.base,stop,0xA000,1026)
    if result[1024:]!=b'\xa5\xb6':raise ValueError('renderer crossed page-buffer bounds')
    return result[:1024]


def check_history(samples):
    a=Asm(0x8000);a.movdi(0x81,0x8F);a.movdi(0xD0,0)
    for k in range(n.STATE_SIZE):n.write(a,n.STATE+k,0)
    history=[0]*32;head=count=peak=0;last=None
    for freq,mode,cn,pending,flags in samples:
        for field,value in [(n.FREQ,freq&255),(n.FREQ+1,freq>>8),(n.MODE,mode),(n.CN,cn),
                            (n.GRAPH_PENDING,pending),(n.FLAGS,flags)]:n.write(a,n.STATE+field,value)
        a.emit(0x12,0x60,0x00)
        if last!=(freq,mode):history=[0]*32;head=count=peak=0;pending=1;last=(freq,mode)
        if pending and not flags&(n.MENU|n.SCAN_UI|n.DISPLAY_SLEEP):
            history[head]=cn;head=(head+1)&31;count=min(32,count+1);peak=max(peak,cn)
    copy_xram(a,n.STATE,0xA000,n.STATE_SIZE,'copy_state');stop=a.base+len(a.code);a.emit(0x80,0xFE)
    result=run([(a.base,a.assemble()),(0x6000,history_control(0x6000)[0]),(0x100,b'\x22')],a.base,stop,0xA000,n.STATE_SIZE)
    assert (result[n.GRAPH_HEAD],result[n.GRAPH_COUNT],result[n.GRAPH_PEAK])==(head,count,peak)
    assert list(result[n.HISTORY:n.HISTORY+32])==history


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--report',type=Path,default=ROOT/'build/signal-graph-tests.json');parser.add_argument('--candidate',type=Path);args=parser.parse_args()
    candidate_sha=verify_image_banks(args.candidate,[(n.GRAPH_BANK,history_control),(n.GRAPH_DISPLAY_BANK,graph_renderer)])
    histories=[[(953,1,k%128,1,0) for k in range(length)] for length in (1,2,31,32,33,63,64,100)]
    histories += [[(953,1,20,1,0),(953,1,60,0,0),(954,1,5,0,0),(954,2,7,0,0)],
                  [(953,1,10,1,0),(953,1,100,1,n.MENU),(953,1,99,1,n.DISPLAY_SLEEP),(953,1,20,1,0)]]
    for sequence in histories:check_history(sequence)
    cases=0;preview=None
    for mode,freq in [(1,953),(1,2300),(2,999),(3,30000)]:
        for count,head in [(0,0),(1,1),(15,15),(31,31),(32,0),(32,17),(32,31)]:
            state=bytearray(n.STATE_SIZE);state[n.MODE]=mode;state[n.FREQ]=freq&255;state[n.FREQ+1]=freq>>8
            state[n.CN]=14;state[n.GRAPH_PEAK]=127;state[n.GRAPH_COUNT]=count;state[n.GRAPH_HEAD]=head
            state[n.HISTORY:n.HISTORY+32]=bytes((k*7)%128 for k in range(32))
            frame=render(state);expected=graph_frame(state)
            if frame!=expected:
                first=next(k for k,(a,b) in enumerate(zip(frame,expected)) if a!=b)
                raise ValueError(f'graph pixels differ at {first}: mode={mode}, count={count}, head={head}')
            cases+=1
            if mode==1 and count==32 and head==0:preview=frame
    if preview:
        im=Image.new('RGB',(128,64),'#c4d1ab')
        for y in range(64):
            for x in range(128):
                if preview[y//8*128+x]>>(y&7)&1:im.putpixel((x,y),(33,48,38))
        out=ROOT/'build/previews';out.mkdir(parents=True,exist_ok=True);im.resize((768,384),Image.Resampling.NEAREST).save(out/'signal-graph.png')
    report=dict(history_sequences_passed=len(histories),graph_pixel_cases_passed=cases,
                history_bytes=32,sampling_period_seconds=1,scope='Actual history and renderer instructions, simulated LCD transfer',
                source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                               ('signal_graph.py','native_ui.py','native_features.py','emulation.py','verify_signal_graph.py')})
    if args.candidate:args.report=args.candidate.with_suffix('.graph-emulation.json')
    report['candidate_sha256']=candidate_sha
    args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
