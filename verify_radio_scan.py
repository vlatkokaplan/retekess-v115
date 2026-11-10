#!/usr/bin/env python3
"""Execute bounded scanning against a deterministic tuner-service model."""
import argparse,hashlib,json
from pathlib import Path
from lcd_draw import Asm
from band_patch import ROOT
from emulation import copy_xram,run,verify_image_banks
from radio_scan import scan_control,scan_renderer,THRESHOLDS,DWELL_TICKS
import native_features as n
from verify_signal_graph import render,font_frame


def limits(mode,mw10,kind,anchor):
    step=1 if mode==1 else (10 if mw10 else 9) if mode==2 else 5
    absolute=(300,2300) if mode==1 else ((520 if mw10 else 522),1710) if mode==2 else (150,30000)
    if kind==0:low,high=(870,1080) if mode==1 else absolute if mode==2 else (4750,21850)
    elif kind==3:low,high=absolute
    else:
        span=(10 if kind==1 else 50) if mode==1 else (100 if kind==1 else 500)
        low=max(absolute[0],min(absolute[1],anchor-span));high=max(absolute[0],min(absolute[1],anchor+span))
    return ((low+step-1)//step*step,high//step*step,step)


def execute(case):
    a=Asm(0x8000);a.movdi(0x81,0x8F);a.movdi(0xD0,0)
    mode=case.get('mode',1);freq=case.get('freq',953);mw10=case.get('mw10',0)
    for k in range(n.STATE_SIZE):n.write(a,n.STATE+k,0)
    for addr,value in [(n.STATE+n.FLAGS,n.MENU|n.SCAN_UI),(n.STATE+n.SCAN_DIRECTION,case.get('direction',1)),
                       (n.STATE+n.SCAN_RANGE,case.get('range',0)),(n.STATE+n.SCAN_THRESHOLD,case.get('threshold',1)),
                       (n.STATE+n.SCAN_DWELL,case.get('dwell',1)),(0x3CDC,freq&255),(0x3CDD,freq>>8),
                       (0x3AB8,3),(0x3CD0,0),(0x7000,0),(0x7001,0),(0x7002,0),(0x3AAC,0)]:n.write(a,addr,value)
    a.movdi(0x18,mode)
    for addr,value in [(0x8B,mw10),(0x8C,0)]:a.movri(0,addr);a.emit(0x76,value)
    a.emit(0x12,0x60,0x00)
    ops=[{'open':True}]+case.get('ops',[])
    for index,op in enumerate(ops):
        if not op.get('open'):
            if 'mode' in op:a.movdi(0x18,op['mode'])
            if 'task' in op:n.write(a,0x3AB8,op['task'])
            if 'submode' in op:n.write(a,0x3CD0,op['submode'])
            if 'lock' in op:a.movri(0,0x8C);a.emit(0x76,op['lock'])
            if 'key' in op:
                n.write(a,n.STATE+n.ACTION,0);a.movri(1,op['key']);a.emit(0x12,0x60,0x06)
            else:
                n.write(a,0x3AAC,op.get('tick',0))
                for addr,value in [(0x7F14,op.get('raw_status',(0x40 if op.get('complete',True) else 0)|(0x20 if op.get('tuned',True) else 0))),
                                   (0x7F15,0),(0x7F16,op.get('cn',14)),(0x7F17,op.get('cn',14))]:n.write(a,addr,value)
                a.emit(0x12,0x60,0x03)
        copy_xram(a,n.STATE,0x9000+index*132,n.STATE_SIZE,'state'+str(index))
        copy_xram(a,0x3CDC,0x9000+index*132+128,2,'freq'+str(index))
        copy_xram(a,0x7001,0x9000+index*132+130,2,'counts'+str(index))
    copy_xram(a,0x7000,0x9000+len(ops)*132,1,'error')
    stop=a.base+len(a.code);a.emit(0x80,0xFE)
    if stop>=0x10000:raise ValueError('scanner test exceeds code space')
    shim=Asm(0x5000)
    shim.cjne_ai(0x1D,'bad');shim.movad(0x82);shim.cjne_ai(0x8B,'read')
    shim.movar(6);n.jnz(shim,'bad');n.read(shim,0x7001);shim.emit(0x04);shim.movxda();shim.ret()
    shim.L('read');shim.cjne_ai(0x3E,'bad');shim.movar(2);shim.cjne_ai(1,'bad')
    n.read(shim,0x7002);shim.emit(0x04);shim.movxda();shim.movdptr(0x7F00)
    shim.movar(1);shim.movda(0x82);shim.emit(0xE0,0xFA)
    for reg in (3,4,5):shim.movri(reg,255)
    shim.ret();shim.L('bad');n.write(shim,0x7000,1);shim.ret()
    output=run([(a.base,a.assemble()),(0x6000,scan_control(0x6000)[0]),(0x5000,shim.assemble()),
                (0x133,bytes.fromhex('02 50 00')),(0x100,b'\x22')],a.base,stop,0x9000,len(ops)*132+1)
    assert output[-1]==0,'scanner called an unexpected native service'
    return [output[k*132:(k+1)*132] for k in range(len(ops))]


def model(case):
    mode=case.get('mode',1);freq=case.get('freq',953);mw10=case.get('mw10',0)
    direction=case.get('direction',1);kind=case.get('range',0);threshold=case.get('threshold',1);dwell=case.get('dwell',1)
    low,high,step=limits(mode,mw10,kind,freq);anchor=freq;status=0;tick=wait=settle=remaining=cn=0
    clock=0;tunes=reads=dirty_nv=0;flags=n.MENU|n.SCAN_UI;task=3;submode=lock=0;actual_mode=mode;result=[]
    def tune(value):
        nonlocal freq,status,wait,settle,tunes,tick
        freq=max(low,min(high,value));status=1;wait=2;settle=20;tick=clock;tunes+=1
    def restart():tune(low if direction else high)
    def start():
        if low<=freq<=high:tune(max(low,freq//step*step))
        else:restart()
    def advance():
        nonlocal status
        value=freq+(step if direction else -step)
        if not low<=value<=high:status=4
        else:tune(value)
    for op in [{'open':True}]+case.get('ops',[]):
        if not op.get('open') and 'key' not in op:clock=op.get('tick',0)
        actual_mode=op.get('mode',actual_mode);task=op.get('task',task);submode=op.get('submode',submode);lock=op.get('lock',lock)
        if not op.get('open'):
            if task!=3 or submode or actual_mode!=mode:status=0;flags&=0xC2
            elif 'key' in op:
                key=op['key']
                if not lock and key not in (0,0xF8,0xFE):
                    if key==n.LONG_SCREEN:status=2
                    elif key in (n.LONG_MENU,0x9E):status=0;flags=(flags&0xC2)|n.MENU
                    elif key==0x96:status=0;flags&=0xC2
                    elif key in (0x97,0x98,0x99,0x9A):
                        status=0;dirty_nv=1
                        if key==0x97:kind=(kind+1)%4;anchor=freq;low,high,step=limits(mode,mw10,kind,anchor)
                        elif key==0x98:threshold=(threshold+1)%4
                        elif key==0x99:dwell=(dwell+1)%3
                        else:direction^=1
                    elif key==0x9B:
                        if status in (1,3):status=2
                        elif status==2:advance()
                        elif status==4:restart()
                        else:start()
                    elif key==0x9C:
                        if status==0:start()
                        elif status==4:restart()
                        else:advance()
                    elif key==0x9D:restart()
                    elif key<0xF8 and key&31<0x16:status=0;flags&=0xC2
            elif status in (1,3):
                now=op.get('tick',0);elapsed=(now-tick)%10
                if elapsed:
                    tick=now
                    if status==3:
                        remaining=max(0,remaining-elapsed)
                        if not remaining:advance()
                    else:
                        settle=max(0,settle-elapsed);wait=max(0,wait-elapsed)
                        if not wait:
                            reads+=1
                            if op.get('raw_status')==255:advance()
                            elif not op.get('complete',True):
                                if not settle:advance()
                                else:wait=1
                            elif not op.get('tuned',True):advance()
                            else:
                                reads+=1
                                raw=op.get('cn',14)
                                if raw==255:advance()
                                else:
                                    cn=raw&127
                                    if cn<THRESHOLDS[threshold]:advance()
                                    else:status=3;remaining=DWELL_TICKS[dwell]
        result.append(dict(freq=freq,status=status,low=low,high=high,step=step,tick=tick,wait=wait,settle=settle,
                           remaining=remaining,cn=cn,tunes=tunes,reads=reads,threshold=threshold,dwell=dwell,
                           direction=direction,range=kind,nv=dirty_nv,flags=flags))
    return result


def check(case):
    actual=execute(case);expected=model(case)
    for index,(record,want) in enumerate(zip(actual,expected)):
        state=record[:128]
        got={name:state[field] for name,field in [('status',n.SCAN_STATUS),('step',n.SCAN_STEP),('tick',n.SCAN_TICK),
             ('wait',n.SCAN_WAIT),('settle',n.SCAN_SETTLE),('remaining',n.SCAN_REMAIN),('cn',n.SCAN_CN),
             ('threshold',n.SCAN_THRESHOLD),('dwell',n.SCAN_DWELL),('direction',n.SCAN_DIRECTION),('range',n.SCAN_RANGE),
             ('nv',n.NV_DIRTY),('flags',n.FLAGS)]}
        got.update(low=state[n.SCAN_LOW]|state[n.SCAN_LOW+1]<<8,high=state[n.SCAN_HIGH]|state[n.SCAN_HIGH+1]<<8,
                   freq=record[128]|record[129]<<8,tunes=record[130],reads=record[131])
        if got!=want:raise ValueError(f'scanner case {case}, step {index}: {got} != {want}')
        assert not got['tunes'] or got['low']<=got['freq']<=got['high']
    return actual[-1][:128]


def screen_lines(state):
    mode=state[n.MODE];freq=state[n.FREQ]|state[n.FREQ+1]<<8
    def frequency(value):return f'{value//10}.{value%10}' if mode==1 else str(value)
    low=state[n.SCAN_LOW]|state[n.SCAN_LOW+1]<<8;high=state[n.SCAN_HIGH]|state[n.SCAN_HIGH+1]<<8
    return ['SCAN '+['','FM','MW','SW'][mode]+' '+['READY','RUN','PAUSED','STATION','END'][state[n.SCAN_STATUS]],
            frequency(freq)+('MHZ' if mode==1 else 'KHZ')+f' C/N:{state[n.CN]}',
            '1 RANGE:'+['BROADCAST','NEAR','WIDE','FULL'][state[n.SCAN_RANGE]],frequency(low)+'-'+frequency(high),
            f'2 MIN C/N:{THRESHOLDS[state[n.SCAN_THRESHOLD]]}DB',
            f'3 PAUSE:{[2,5,10][state[n.SCAN_DWELL]]}S 4:'+('UP' if state[n.SCAN_DIRECTION] else 'DOWN'),
            '5 RUN/PAUSE 6 NEXT','0 EXIT 7 RESET 8 MENU']


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,default=ROOT/'build/radio-scan-tests.json');p.add_argument('--candidate',type=Path);args=p.parse_args()
    candidate_sha=verify_image_banks(args.candidate,[(n.SCAN_BANK,scan_control),(n.SCAN_DISPLAY_BANK,scan_renderer)])
    cases=[]
    configs=[(1,0,[0,300,953,2300,65535]),(2,0,[0,522,999,1710,65535]),(2,1,[0,520,1000,1710,65535]),(3,0,[0,150,10000,30000,65535])]
    for mode,mw10,frequencies in configs:
        for frequency in frequencies:
            for kind in range(4):
                for direction in (0,1):
                    cases.append(dict(mode=mode,mw10=mw10,freq=frequency,range=kind,direction=direction,ops=[{'key':0x9B},{'tick':2,'cn':0},{'tick':4,'cn':20}]))
    for threshold in range(4):
        for cn in (0,3,4,7,8,11,12,15,16,127):cases.append(dict(threshold=threshold,ops=[{'key':0x9B},{'tick':2,'cn':cn}]))
    for dwell in range(3):cases.append(dict(dwell=dwell,ops=[{'key':0x9B},{'tick':2}]+[{'tick':k%10} for k in range(3,104)]))
    for mode in (1,2,3):
        for invalid in ({'raw_status':255},{'cn':255}):
            cases.append(dict(mode=mode,ops=[{'key':0x9B},dict(tick=2,**invalid)]))
    for guard in ({'task':2},{'mode':3},{'submode':1}):
        cases.append(dict(ops=[{'key':0x9B},dict(tick=2,**guard),{'key':0x9C}]))
    cases += [dict(ops=[{'key':0x9B}]+[{'tick':k%10,'complete':False} for k in range(1,24)]),
              dict(ops=[{'key':0x9B},{'tick':2,'tuned':False},{'key':0x9B},{'tick':9},{'key':0x9B},{'key':0x9C},{'key':0x96}]),
              dict(ops=[{'key':0x97},{'key':0x98},{'key':0x99},{'key':0x9A},{'key':0x9D},{'key':n.LONG_SCREEN},{'key':0x9E}])]
    for key in range(256):cases.append(dict(ops=[{'key':key}]))
    for case in cases:check(case)
    pixel_cases=0
    for mode,mw10,freq in [(1,0,953),(2,0,999),(2,1,1000),(3,0,30000)]:
        for kind in range(4):
            for status in range(5):
                state=bytearray(n.STATE_SIZE);state[n.MODE]=mode;state[n.FREQ]=freq&255;state[n.FREQ+1]=freq>>8
                state[n.CN]=127;state[n.SCAN_STATUS]=status;state[n.SCAN_RANGE]=kind
                state[n.SCAN_THRESHOLD]=3;state[n.SCAN_DWELL]=2;state[n.SCAN_DIRECTION]=1
                low,high,_=limits(mode,mw10,kind,freq)
                for field,value in [(n.SCAN_LOW,low),(n.SCAN_HIGH,high)]:state[field]=value&255;state[field+1]=value>>8
                assert render(state,scan_renderer)==bytes(font_frame(screen_lines(state)))
                pixel_cases+=1
    report=dict(state_machine_cases_passed=len(cases),scanner_pixel_cases_passed=pixel_cases,
                station_list_writes=0,bounds_and_grid_verified=True,stc_lock_and_cn_required=True,
                dwell_pause_resume_and_timeout_verified=True,single_register_reads_verified=True,
                all_ones_bus_reads_rejected=True,
                source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                               ('radio_scan.py','verify_radio_scan.py','emulation.py')})
    if args.candidate:args.report=args.candidate.with_suffix('.scan-emulation.json')
    report['candidate_sha256']=candidate_sha
    args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
