#!/usr/bin/env python3
"""Verify the compiled application's volume writes on a muted radio in loader RAM."""
import argparse
import hashlib
import json
from pathlib import Path

import radio_audio
from preview_radio_volume import VolumePreview
from run_radio import ROOT, upload, execute, select, inject, state
from usb_dump import RawTransport
from stock_audio import VOLUME_REGISTERS,verify_stock_curve


def sha(value):
    return hashlib.sha256(value).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build',type=Path,default=ROOT/'build/radio-timer-preview')
    parser.add_argument('--report',type=Path,default=ROOT/'build/radio-volume-hardware.json')
    args=parser.parse_args()
    manifest=json.loads((args.build/'radio.json').read_text())
    code=(args.build/'radio.bin').read_bytes()
    if manifest.get('simulation') or not manifest.get('timer_batch') or sha(code)!=manifest['sha256']:
        parser.error('matching real timer-batch build required')
    for name,digest in manifest['source_sha256'].items():
        if sha((ROOT/name).read_bytes())!=digest:parser.error('build source changed: '+name)
    verify_stock_curve((ROOT/'stock.bin').read_bytes())
    report=dict(stock_volume_curve_verified=True,completed=False,flash_written=False,audio_enabled=False,
                application_sha256=manifest['sha256'],source_sha256=manifest['source_sha256'],
                verification_source_sha256=sha(Path(__file__).read_bytes()),
                helper_source_sha256=sha((ROOT/'preview_radio_volume.py').read_bytes()),samples=[],sweep_samples=[])
    transport=RawTransport()
    board=VolumePreview(transport,manifest,(args.build/'radio.map').read_text())
    initialized=False

    def press(key=None,ticks=1):
        select(transport,manifest,'app_tick')
        if key is not None:inject(transport,manifest,key)
        else:
            for unused in range(ticks):execute(transport)
        return state(transport,manifest)

    def settle():
        # Loader-RAM ticks run back to back over USB, far faster than the radio's 20 ms
        # timer, so a tune can still be in progress after the 6 ticks the firmware
        # waits before polling. Tick until the firmware clears pending itself, within
        # its own 145-tick settle window; any tune error still fails the run.
        for unused in range(145):
            current=press(ticks=1)
            if not current['pending']:break
        assert not current['pending'] and not current['error'],current
        return current
    def sample(level):
        current=state(transport,manifest)
        raw=board.access([('xread',0x7890),('xread',0x7893),('smodify',0xB0,255,0)])
        got=dict(level=level,gain_register=raw[1],control_register=raw[0],speaker=bool(raw[2]&32),
                 muted=current['muted'],volume=current['volume'],pending=current['pending'],
                 error=current['error'],pre_gain=board.tuner(13),tuner_volume=board.tuner(6))
        assert got['volume']==24+level and got['gain_register']==VOLUME_REGISTERS[level] and (got['control_register']&0xFE)==0xFE and not got['speaker'],got
        assert not got['pending'] and not got['error'] and got['pre_gain']==0x8C and got['tuner_volume']==0x8C,got
        return got

    try:
        report['loader_before']=transport.identify();transport.unlock()
        board.mute();radio_audio.initialize(transport)
        for offset in range(0,len(code),512):upload(transport,0x2400+offset,code[offset:offset+512].ljust(512,b'\0'))
        upload(transport,0x2200,bytes(512));select(transport,manifest,'app_init');execute(transport);initialized=True
        assert press(2)['muted']==1
        settle()
        press(8)
        for level in range(33):
            if level:press(9)
            got=sample(level);assert got['muted']==1,got
            report['samples'].append(got);report['sweep_samples'].append(dict(direction='up',**got))
        for direction,levels,key in [('down',range(31,-1,-1),8),('up_again',range(1,33),9)]:
            for level in levels:
                press(key);got=sample(level);assert got['muted']==1,got
                report['sweep_samples'].append(dict(direction=direction,**got))
            print(direction+': all gains and tuner input level verified',flush=True)
        report['up_down_up_verified']=True
        report['constant_tuner_volume']=True
        for unused in range(32):press(8)
        assert press(2)['muted']==0
        sample(0);assert board.tuner(0)&4
        report['zero_volume_mutes_tuner']=True
        press(2);press(9);press(3);settle();sample(1)
        report['retune_gain_preserved']=True
        assert press(0x14)['standby']==1
        assert press(0x14)['standby']==0
        settle();sample(1)
        report['standby_resume_volume_verified']=True
        report['completed']=True
    except BaseException as error:
        report['error']=f'{type(error).__name__}: {error}'
        raise
    finally:
        try:
            board.mute();report['speaker_muted_on_exit']=True
            if initialized:
                board.tuner(0,0x44)
                assert board.tuner(0)&0x84==4
                board.call('lcd_power',0)
            board.access([('atwrite',0,0)])
            report['loader_after']=transport.identify()
        finally:
            transport.close();args.report.parent.mkdir(parents=True,exist_ok=True)
            args.report.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
