#!/usr/bin/env python3
"""Pixel and buffer-boundary checks for the large home screen and mode-aware menu."""
import argparse,hashlib,json
from pathlib import Path
from PIL import Image
from band_patch import ROOT
import native_features as n
from radio_ui import home_renderer,settings_renderer
from emulation import verify_image_banks
from verify_signal_graph import render,font_frame


def put_text(data,text,x,y,scale=1):
    font=n.font_bytes()
    assert x+len(text)*6*scale<=128
    for index,ch in enumerate(text):
        for column,value in enumerate(font[(ord(ch)-32)*5:(ord(ch)-31)*5]):
            for bit in range(8):
                if value>>bit&1:
                    for dx in range(scale):
                        for dy in range(scale):
                            xx=x+(index*6+column)*scale+dx;yy=y+bit*scale+dy
                            data[yy//8*128+xx]|=1<<(yy&7)


def home_frame(state,mw10=0):
    mode=state[n.MODE];freq=state[n.FREQ]|state[n.FREQ+1]<<8;reg=state[n.REG7]
    frequency=f'{freq//10}.{freq%10}' if mode==1 else str(freq)
    data=bytearray(1024);put_text(data,frequency,6,0,2)
    put_text(data,['','FM','MW','SW'][mode],94,0);put_text(data,'MHZ' if mode==1 else 'KHZ',94,8)
    put_text(data,f'C/N:{state[n.CN]}DB PK:{state[n.GRAPH_PEAK]}',0,16)
    for x in range(128):data[3*128+x]=0x7E if x in (0,127) or x<min(60,state[n.CN])*2 else 0x42
    if mode==1:
        line=f'BW:{[150,200,50,100][reg&3]}KHZ '+('MONO' if reg&4 else 'AUTO')
        detail='BASS:'+('ON' if reg&16 else 'OFF')+' DE:'+('50US' if reg&32 else '75US')
    else:line=('MW STEP:10KHZ' if mw10 else 'MW STEP:9KHZ') if mode==2 else 'SW STEP:5KHZ';detail='AM SIGNAL MONITOR'
    for row,text in [(4,line),(5,detail),(6,'HOLD2 METERS 8 MENU'),(7,'HOLD4 SCREEN OFF')]:put_text(data,text,0,row*8)
    return bytes(data)


def settings_lines(state):
    if state[n.FLAGS]&n.HELP:
        return ['KEYS '+n.VERSION,'HOLD1 BW 2 METERS','HOLD3 MONO 4 LCD OFF','HOLD6 DE 7 BASS',
                'HOLD8 SETTINGS','METERS:7 GRAPH/VALUES','* UNSAVED ! SAVE FAIL','0 SAVE/EXIT 9 BACK']
    mode=state[n.MODE];pref=state[n.PREF_REG]
    header=['','FM','MW','SW'][mode]+' SETTINGS'+(' !' if state[36] else ' *' if state[n.NV_DIRTY] else '')
    timeout='5 LCD SLEEP:'+['OFF','10S','30S','60S'][state[n.TIMEOUT]]
    if mode==1:
        return [header,f'1 BANDWIDTH:{[150,200,50,100][pref&3]}KHZ','2 AUDIO:'+('MONO' if pref&4 else 'AUTO'),
                '3 BASS:'+('ON' if pref&16 else 'OFF'),'4 DE-EMPHASIS:'+('50US' if pref&32 else '75US'),
                timeout,'6 OFF 7 METER','0 SAVE 8 SCAN 9 HELP']
    return [header,timeout,'6 SCREEN OFF','7 SIGNAL METER','8 SCAN OPTIONS','9 SHORTCUT HELP','0 SAVE AND EXIT','CUSTOM '+n.VERSION]


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,default=ROOT/'build/radio-ui-tests.json');p.add_argument('--candidate',type=Path);args=p.parse_args()
    candidate_sha=verify_image_banks(args.candidate,[(n.HOME_BANK,home_renderer),(n.MENU_BANK,settings_renderer)])
    cases=0;previews={}
    for mode,freq in [(1,300),(1,875),(1,953),(1,1080),(1,1999),(1,2300),(2,522),(2,999),(3,150),(3,30000)]:
        for cn,reg in [(0,0xA1),(14,0xA1),(31,0xB4),(60,0x87),(127,0xBF)]:
            state=bytearray(n.STATE_SIZE);state[n.MODE]=mode;state[n.FREQ]=freq&255;state[n.FREQ+1]=freq>>8
            state[n.CN]=cn;state[n.REG7]=reg;state[n.GRAPH_PEAK]=127
            frame=render(state,home_renderer)
            if frame!=home_frame(state):
                first=next(k for k,(x,y) in enumerate(zip(frame,home_frame(state))) if x!=y)
                raise ValueError(f'home pixels differ at {first}: mode={mode}, freq={freq}, C/N={cn}')
            cases+=1
            if mode==1 and freq==953 and cn==14:previews['home']=frame
    for mode in (1,2,3):
        for pref in range(0,64,3):
            for timeout in range(4):
                state=bytearray(n.STATE_SIZE);state[n.MODE]=mode;state[n.FLAGS]=n.MENU
                state[n.PREF_REG]=pref;state[n.TIMEOUT]=timeout;state[n.NV_DIRTY]=pref&1;state[36]=int(pref==63)
                frame=render(state,settings_renderer)
                assert frame==bytes(font_frame(settings_lines(state))),(mode,pref,timeout)
                cases+=1
                if mode==2 and pref==0 and timeout==1:previews['am-settings']=frame
                if mode==1 and pref==0 and timeout==1:previews['fm-settings']=frame
    for cn in (0,14,31,60,127):
        state=bytearray(n.STATE_SIZE);state[n.MODE]=2;state[n.FREQ]=1000&255;state[n.FREQ+1]=1000>>8;state[n.CN]=cn
        assert render(state,home_renderer,mw10=1)==home_frame(state,mw10=1);cases+=1
    state=bytearray(n.STATE_SIZE);state[n.FLAGS]=n.MENU|n.HELP
    assert render(state,settings_renderer)==bytes(font_frame(settings_lines(state)));cases+=1
    out=ROOT/'build/previews';out.mkdir(parents=True,exist_ok=True)
    for name,frame in previews.items():
        im=Image.new('RGB',(128,64),'#c4d1ab')
        for y in range(64):
            for x in range(128):
                if frame[y//8*128+x]>>(y&7)&1:im.putpixel((x,y),(33,48,38))
        im.resize((768,384),Image.Resampling.NEAREST).save(out/(name+'.png'))
    report=dict(pixel_cases_passed=cases,buffer_bounds_verified=True,large_frequency_verified=True,am_menu_hides_fm_controls=True,
                source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                               ('radio_ui.py','native_ui.py','verify_radio_ui.py')})
    if args.candidate:args.report=args.candidate.with_suffix('.ui-emulation.json')
    report['candidate_sha256']=candidate_sha
    args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
