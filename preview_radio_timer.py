#!/usr/bin/env python3
"""Run 256 native 20 ms radio ticks per USB call, without per-tick host pacing."""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import time

from build_radio import ROOT
from run_radio import upload,execute,select,state
from usb_dump import RawTransport
import radio_audio
import radio_timer


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build',type=Path,default=ROOT/'build/radio-timer-preview')
    p.add_argument('--batches',type=int,default=6)
    p.add_argument('--report',type=Path,default=ROOT/'build/radio-timer-preview/hardware.json')
    a=p.parse_args()
    if not 1<=a.batches<=100:p.error('batches must be 1..100')
    m=json.loads((a.build/'radio.json').read_text());code=(a.build/'radio.bin').read_bytes()
    if m.get('simulation') or not m.get('timer_batch') or hashlib.sha256(code).hexdigest()!=m['sha256']:
        p.error('an unmodified native timer preview build is required')
    for name,digest in m['source_sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:p.error('build source changed: '+name)
    stop=False
    def interrupted(signum,frame):
        nonlocal stop
        stop=True
    old=signal.signal(signal.SIGINT,interrupted)
    t=RawTransport();saved=None
    report=dict(flash_written=False,host_paces_ticks=False,application_sha256=m['sha256'],batches=[])
    try:
        report['loader_before']=t.identify();t.unlock()
        report['audio_initialization']=radio_audio.initialize(t)
        upload(t,0x2200,radio_timer.configuration().ljust(512,b'\0'));execute(t)
        saved=list(t.read_ram(radio_timer.SNAPSHOT,512)[:8])
        if saved[4]&0x30:saved=None;raise RuntimeError('Timer1 is already in use by the loader')
        for offset in range(0,len(code),512):upload(t,0x2400+offset,code[offset:offset+512].ljust(512,b'\0'))
        upload(t,0x2200,bytes(512));select(t,m,'app_init');execute(t)
        data=bytearray(t.read_ram(0x2200,512));data[:128]=radio_timer.batch(m).ljust(128,b'\0');upload(t,0x2200,bytes(data))
        print(m['version']+' active; each USB call runs 256 hardware-timed native ticks.',flush=True)
        print('POWER: standby/wake. Keep USB connected during this RAM test.',flush=True)
        previous=state(t,m)['clock_ticks']
        for index in range(a.batches):
            if stop:break
            start=time.monotonic();execute(t);elapsed=time.monotonic()-start;s=state(t,m)
            if (s['clock_ticks']-previous)&65535!=256:raise RuntimeError('native tick count mismatch')
            if not 4.8<=elapsed<=8:raise RuntimeError(f'native timer pace outside expected range: {elapsed:.3f}s')
            previous=s['clock_ticks'];sample=dict(batch=index+1,seconds=round(elapsed,3),**s)
            report['batches'].append(sample);print(json.dumps(sample),flush=True)
        report['completed']=True
    except Exception as exc:
        report['error']=str(exc);raise
    finally:
        try:
            if saved is not None:
                upload(t,0x2200,radio_timer.configuration(saved).ljust(512,b'\0'));execute(t)
            report['loader_after']=t.identify()
        finally:
            t.close();signal.signal(signal.SIGINT,old)
            a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
