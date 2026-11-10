#!/usr/bin/env python3
"""Run the source-built radio in volatile loader RAM, with bounded native ticks.

The host uploads, paces calls and records state. Key handling, tuning, drawing
and volume control execute on the 8051. No SPI flash command is exposed here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import sys
import time

from lcd_draw import Asm
from usb_dump import RawTransport
import radio_audio

ROOT=Path(__file__).resolve().parent


def wrapper(entry,internal_bytes,base=0x2200):
    a=Asm(base)
    a.push(0xE0);a.push(0xA8);a.clr(0xAF);a.push(0xD0);a.anldi(0xD0,0xE7)
    for direct in range(internal_bytes):a.push(direct)
    for direct in (0xF0,0x86):a.push(direct)
    a.movdi(0x86,0)
    for direct in (0x82,0x83,0x84,0x85,0xA0):a.push(direct)
    a.emit(0x12,entry>>8,entry&255)
    for direct in (0xA0,0x85,0x84,0x83,0x82,0x86,0xF0):a.pop(direct)
    for direct in range(internal_bytes-1,-1,-1):a.pop(direct)
    for direct in (0xD0,0xA8,0xE0):a.pop(direct)
    a.ret();code=a.assemble()
    if len(code)>128:raise ValueError('context wrapper exceeds its RAM reservation')
    return code


def upload(t,address,data):
    if address<0x2200 or address+len(data)>0x3A00 or len(data)!=512:
        raise ValueError('RAM upload outside the reviewed loader allocation')
    t.encoded(bytes([0xFB,3])+address.to_bytes(4,'big')+bytes(10),outgoing=data)
    if t.read_ram(address,512)!=data:raise RuntimeError('RAM upload readback mismatch')


def execute(t):t.encoded(bytes([0xFB,2])+(0x2200).to_bytes(4,'big')+bytes(10))


def select(t,manifest,name):
    data=bytearray(t.read_ram(0x2200,512))
    code=wrapper(manifest['entries'][name],manifest['internal_data_bytes'])
    data[:128]=code.ljust(128,b'\0')
    upload(t,0x2200,bytes(data))


def state(t,manifest):
    raw=t.read_ram(manifest['state_address'],512)[:manifest['state_bytes']]
    if raw[:4]!=b'V115':raise RuntimeError('application state signature missing')
    words={'frequency','entry','frames','events','tunes','read_channel','clock_ticks'}
    result={name:int.from_bytes(raw[offset:offset+(2 if name in words else 1)],'little')
            for name,offset in manifest['fields'].items() if name not in ('size','page_crc')}
    result['page_crc']=list(raw[manifest['fields']['page_crc']:manifest['fields']['page_crc']+8])
    result['frequency_khz']=result['frequency']*25 if result['mode']==0 else result['frequency']
    return result


def inject(t,manifest,key):
    if not 1<=key<=31:raise ValueError('invalid synthetic key')
    data=bytearray(t.read_ram(0x2200,512))
    data[0x180+manifest['fields']['injected_key']]=key
    upload(t,0x2200,bytes(data));execute(t)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build',type=Path,default=ROOT/'build/radio-ram')
    p.add_argument('--seconds',type=float,default=120)
    p.add_argument('--self-test',action='store_true',help='exercise synthetic input, separate from physical-key acceptance')
    p.add_argument('--report',type=Path,default=ROOT/'build/radio-ram/hardware.json')
    p.add_argument('--leave-audio',action='store_true',help='leave the tuner playing after the host loop exits')
    p.add_argument('--key-trace',action='store_true',help='record raw keypad samples in the local report')
    a=p.parse_args()
    if not 0<a.seconds<=3600:p.error('seconds must be in 0..3600')
    manifest=json.loads((a.build/'radio.json').read_text());code=(a.build/'radio.bin').read_bytes()
    if manifest.get('simulation') or manifest.get('autonomous') or hashlib.sha256(code).hexdigest()!=manifest['sha256']:
        p.error('real, unmodified application build required')
    for name,digest in manifest['source_sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:p.error('build source changed: '+name)
    stop_requested=False
    def stop(signum,frame):
        nonlocal stop_requested
        stop_requested=True
    prior=signal.signal(signal.SIGINT,stop)
    report=dict(version=manifest['version'],flash_written=False,completed=False,
                application_sha256=manifest['sha256'],
                audio_source_sha256=hashlib.sha256(Path(radio_audio.__file__).read_bytes()).hexdigest(),
                samples=[],synthetic_tests=[])
    if a.key_trace:report['key_samples']=[]
    t=RawTransport();initialized=False
    try:
        report['loader_before']=t.identify();t.unlock()
        report['audio_initialization']=radio_audio.initialize(t)
        for offset in range(0,len(code),512):upload(t,0x2400+offset,code[offset:offset+512].ljust(512,b'\0'))
        upload(t,0x2200,bytes(512));select(t,manifest,'app_init');execute(t);initialized=True
        select(t,manifest,'app_tick')
        if a.self_test:
            for keys,want in [([0x1F,0x1B,0x19,2],95300),([3],95325),([1],95300),
                              ([4],999),([0x12],6000),([4,4],95300)]:
                for k in keys:inject(t,manifest,k)
                for attempt in range(30):
                    execute(t);s=state(t,manifest)
                    if not s['pending']:break
                    time.sleep(0.04)
                s=state(t,manifest)
                expected_channel=s['frequency']-1200 if s['mode']==0 else s['frequency']//(3 if s['mode']==1 else 5)
                if (s['frequency_khz']!=want or s['pending'] or s['error'] or s['read_channel']!=expected_channel):
                    raise RuntimeError('synthetic tuning/readback failed: '+str(s))
                report['synthetic_tests'].append(s)
        print('Independent radio active; keypad handling runs on the device.',flush=True)
        print('Digits + PLAY: frequency; FM/AM or SW: band; arrows: tune; volume keys: level; MENU: LCD; POWER: exit.',flush=True)
        start=time.monotonic();last=None;cycles=0;call_total=0;call_max=0
        while not stop_requested and time.monotonic()-start<a.seconds:
            tick_start=time.monotonic()
            execute(t);s=state(t,manifest)
            duration=time.monotonic()-tick_start
            cycles+=1;call_total+=duration;call_max=max(call_max,duration)
            if a.key_trace:
                report['key_samples'].append([round(time.monotonic()-start,3),s['adc1'],s['adc2'],s['events'],s['last_key']])
            signature=tuple(s[k] for k in ('mode','frequency','entry','editing','volume','muted','sleeping','pending','read_channel','error','last_key'))
            if signature!=last:
                print(json.dumps(s),flush=True);report['samples'].append(dict(seconds=round(time.monotonic()-start,2),**s));last=signature
            if s['command']:break
            time.sleep(max(0,manifest['tick_milliseconds']/1000-(time.monotonic()-tick_start)))
        report['final']=state(t,manifest);report['completed']=True
        report['timing']=dict(ticks=cycles,seconds=time.monotonic()-start,
                              mean_call_ms=1000*call_total/max(1,cycles),max_call_ms=1000*call_max)
    except Exception as exc:
        report['error']=str(exc)
        if initialized:
            try:report['failure_state']=state(t,manifest)
            except Exception as state_error:report['state_error']=str(state_error)
        raise
    finally:
        active_exception=sys.exc_info()[0] is not None
        cleanup_error=None
        try:
            if initialized and not a.leave_audio:
                select(t,manifest,'app_stop');execute(t)
            report['loader_after']=t.identify()
        except Exception as exc:
            cleanup_error=exc;report['cleanup_error']=str(exc)
        finally:
            t.close();signal.signal(signal.SIGINT,prior)
            a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps(report,indent=2)+'\n')
        if cleanup_error and not active_exception:raise cleanup_error


if __name__=='__main__':main()
