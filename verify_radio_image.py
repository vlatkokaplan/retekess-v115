#!/usr/bin/env python3
"""Audit the autonomous image, boot entry, timer setup, and NOR recovery."""
import argparse
import hashlib
import json
from pathlib import Path

from band_patch import ROOT,decode
from build_radio import build as compile_radio
from build_radio_image import build,VERSION,OVERLAY,OVERLAY_SIZE,HANDOFF,LOAD_COUNT,DISPATCH,SHIM_PC,SHIM_FLASH,startup
from firmware_integrity import check,TABLE,COUNT,finalize
from build_full_feature_image import build as build_rc6
from boot_version import OFFSET,SIZE
from lcd_draw import Asm
from emulation import run,copy_xram
from run_radio import wrapper
from flash_radio import prepare
from flash_update import install,RESOURCE_SECTORS
from verify_flash_update import NorModel,MemorySession


def sha(data):return hashlib.sha256(data).hexdigest()


def entry_checks(manifest,code):
    a=Asm(0xA000)
    a.movdi(0x81,0x8F);a.movdi(0xA8,0x80);a.movdi(0xA9,255);a.movdi(0x86,0x80);a.movdi(0xD0,0x18)
    entry=manifest['entries']['radio_forever'];a.emit(0x12,entry>>8,entry&255)
    capture=Asm(0x9000);capture.movdptr(0xB000)
    for reg in [0xA8,0xA9,0x86,0xD0,0xF7]:capture.movad(reg);capture.movxda();capture.emit(0xA3)
    stop=capture.base+len(capture.code);capture.emit(0x80,0xFE)
    init=manifest['entries']['app_init']
    got=run([(0x4000,code),(a.base,a.assemble()),(init,bytes.fromhex('02 90 00')),
             (capture.base,capture.assemble())],a.base,stop,0xB000,5)
    assert got[:3]==bytes(3) and got[3]&0x18==0 and got[4]==0,('boot context',got.hex())

    a=Asm(0xA000);a.movdi(0x81,0x8F)
    a.emit(0x12,SHIM_PC>>8,SHIM_PC&255)
    capture=Asm(0x9000)
    for dest,source in [(0x70,0xE0),(0x71,0x82),(0x72,0x83)]:capture.emit(0x85,source,dest)
    capture.movdptr(0xB000)
    for reg in [0x70,0x71,0x72]:capture.movad(reg);capture.movxda();capture.emit(0xA3)
    copy_xram(capture,0x3AB8,0xB003,1,'mode')
    stop=capture.base+len(capture.code);capture.emit(0x80,0xFE)
    got=run([(SHIM_PC,startup()),(0x133,bytes.fromhex('02 90 00')),
             (a.base,a.assemble()),(capture.base,capture.assemble())],a.base,stop,0xB000,4)
    assert got==bytes([0x17,0xD3,0x19,3]),('forced radio dispatch',got.hex())


def timer_check(manifest,code):
    for divider,base in enumerate([60000,30000,20000,15000]):
        for shift in range(4):
            clock=0xC0|(shift<<2)|divider;period=(base>>shift)-1
            a=Asm(0xA000);a.movdi(0x81,0x8F);a.movdi(0x86,0)
            a.movdi(0xDB,255);a.movdi(0xDC,clock);a.movdi(0x9C,255)
            a.emit(0x12,0x60,0)
            for index,address in enumerate([0x7850,0x7851,0x7852,0x7853,0x785A,0x785B]):
                a.movdptr(address);a.emit(0xE0);a.movdptr(0xB000+index);a.movxda()
            a.movdptr(0xB006)
            for reg in (0xDB,0xDC,0x9C):a.movad(reg);a.movxda();a.emit(0xA3)
            stop=a.base+len(a.code);a.emit(0x80,0xFE)
            got=run([(0x4000,code),(a.base,a.assemble()),
                     (0x6000,wrapper(manifest['entries']['radio_timer_start'],manifest['internal_data_bytes'],0x6000))],
                    a.base,stop,0xB000,9)
            expected=bytes([0,0,period&255,period>>8,0x14,4,0xEE,clock,0xEF])
            assert got==expected,('preserved audio clock and 20 ms timer',clock,got.hex(),expected.hex())


def boot_key_checks():
    m=compile_radio(ROOT/'build/radio-boot-key-sim',simulation=True,autonomous=True)
    code=(ROOT/'build/radio-boot-key-sim/radio.bin').read_bytes()
    for key in (0,0x14):
        a=Asm(0xA000);a.movdi(0x81,0x8F);a.movdi(0x86,0)
        a.movdptr(0x7042);a.movai(key);a.movxda()
        entry=m['entries']['radio_forever'];a.emit(0x12,entry>>8,entry&255)
        capture=Asm(0x9000)
        copy_xram(capture,m['state_address']+m['fields']['adc2']+1,0xB000,4,'key_state')
        stop=capture.base+len(capture.code);capture.emit(0x80,0xFE)
        got=run([(0x4000,code),(a.base,a.assemble()),
                 (m['entries']['radio_timer_start'],bytes.fromhex('02 90 00')),
                 (capture.base,capture.assemble())],a.base,stop,0xB000,4)
        assert got==bytes([key,2,key,255]),('startup key must be consumed',key,got.hex())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=ROOT/'stock.bin')
    p.add_argument('--candidate',type=Path,default=ROOT/'build/v115-open-0.2.3.bin')
    p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--application-report',type=Path,default=ROOT/'build/radio-autonomous-sim/validation.json')
    p.add_argument('--hardware-report',type=Path)
    a=p.parse_args();source=a.source.read_bytes();candidate=a.candidate.read_bytes();baseline=a.baseline.read_bytes()
    rebuilt,meta=build(source,ROOT/'build/radio-audit-rebuild');assert candidate==rebuilt,'candidate differs from source build'
    compiled=meta['compiled'];code=(ROOT/'build/radio-audit-rebuild/radio.bin').read_bytes()
    application=json.loads(a.application_report.read_text())
    assert application['autonomous'] and application['cases_passed']==55 and application['hardware_sha256']==compiled['sha256']
    assert application['amplifier_gpio_cases_passed']==4
    for name,digest in application['source_sha256'].items():assert sha((ROOT/name).read_bytes())==digest,('application source changed',name)
    check(candidate);plain=decode(candidate);original=decode(source)
    assert plain[OVERLAY:OVERLAY+len(code)]==code
    assert plain[LOAD_COUNT]==3 and len(code)<=plain[LOAD_COUNT]*0x800,'complete program must be loaded'
    assert not any(plain[OVERLAY+len(code):OVERLAY+OVERLAY_SIZE-2]),'nonempty overlay padding'
    entry=compiled['entries']['radio_forever'];assert plain[HANDOFF:HANDOFF+3]==bytes([0x12,entry>>8,entry&255])
    assert plain[DISPATCH:DISPATCH+8]==bytes([2,SHIM_PC>>8,SHIM_PC&255])+bytes(5)
    spans=[(OVERLAY,OVERLAY_SIZE),(HANDOFF,3),(LOAD_COUNT,1),(DISPATCH,8),(SHIM_FLASH,len(startup())),(TABLE,COUNT*2),(OFFSET,SIZE)]
    allowed=bytearray(len(plain))
    for start,size in spans:allowed[start:start+size]=bytes([1])*size
    assert all(allowed[i] or x==y for i,(x,y) in enumerate(zip(plain,original))), 'unreviewed image changes'
    assert candidate[:0x2000]==source[:0x2000]
    entry_checks(compiled,code);timer_check(compiled,code);boot_key_checks()
    target,sectors=prepare(baseline,candidate,source)
    same,empty=prepare(target,candidate,source);assert same==target and not empty
    def reject(action):
        try:action()
        except ValueError:return
        raise AssertionError('unreviewed image accepted')
    altered=bytearray(candidate);altered[0x134020]^=1
    reject(lambda:prepare(baseline,bytes(altered),source))
    altered=bytearray(baseline);altered[OFFSET]^=1
    reject(lambda:prepare(bytes(altered),candidate,source))
    altered=decode(source);altered[0x14800]^=1;unknown,_=finalize(altered)
    reject(lambda:prepare(unknown,candidate,source))
    altered=bytearray(baseline);altered[0x134020]^=1
    preserved,_=prepare(bytes(altered),candidate,source);assert preserved[0x134020]==altered[0x134020]
    completed=[]
    for failure in [None,1,24,60,90]:
        model=NorModel(baseline,fail_program=failure);session=MemorySession()
        try:install(model,baseline,target,sectors,session,permitted_resource_sectors=RESOURCE_SECTORS,version=VERSION)
        except RuntimeError:assert failure is not None and bytes(model.data)==baseline
        else:assert failure is None and bytes(model.data)==target
        assert not model.writable;completed.append(failure)
    model=NorModel(baseline,oversized_erase=True);session=MemorySession()
    try:install(model,baseline,target,sectors,session,permitted_resource_sectors=RESOURCE_SECTORS,version=VERSION)
    except RuntimeError:assert bytes(model.data)==baseline
    else:raise AssertionError('oversized erase accepted')
    rc6,_=build_rc6(source);restored,restore_sectors=prepare(target,rc6,source,restore_rc6=True)
    assert restored[:TABLE+COUNT*2]==rc6[:TABLE+COUNT*2]
    assert restored[OFFSET:OFFSET+SIZE]==rc6[OFFSET:OFFSET+SIZE]
    assert restored[TABLE+COUNT*2:OFFSET]==baseline[TABLE+COUNT*2:OFFSET] and restored[OFFSET+SIZE:]==baseline[OFFSET+SIZE:]
    model=NorModel(target);session=MemorySession()
    install(model,target,restored,restore_sectors,session,permitted_resource_sectors=RESOURCE_SECTORS,version='1.5-RC6')
    assert bytes(model.data)==restored
    native=False
    if a.hardware_report:
        hardware=json.loads(a.hardware_report.read_text())
        timer_build=compile_radio(ROOT/'build/radio-timer-audit',autonomous=True,timer_batch=True)
        assert hardware['completed'] and not hardware['host_paces_ticks'] and hardware['application_sha256']==timer_build['sha256']
        assert hardware['audio_source_sha256']==sha((ROOT/'radio_audio.py').read_bytes())
        assert hardware['speaker_muted_on_exit']
        assert len(hardware['batches'])>=2
        assert all(sample['clock_ticks']==(i+1)*256 and sample['error']==0 and 4.8<=sample['seconds']<=8
                   for i,sample in enumerate(hardware['batches']))
        native=True
    paths=[*compiled['source_sha256'], 'build_radio_image.py','verify_radio.py','verify_radio_image.py',
           'flash_radio.py','flash_update.py','spi_flash.py','usb_dump.py','loader_packets.py','firmware_integrity.py','boot_version.py',
           'radio_audio.py','radio_timer.py','preview_radio_timer.py']
    report=dict(version=VERSION,candidate_sha256=sha(candidate),compiled_sha256=compiled['sha256'],
                baseline_sha256=sha(baseline),application_cases=55,amplifier_gpio_cases=4,
                boot_entry_checked=True,timer_configuration_checked=True,timer_configuration_cases=16,
                overlay_load_length_checked=True,boot_key_cases=2,
                normal_update_and_recovery_model_passed=True,partial_program_rollback_pages=completed[1:],
                unsafe_images_rejected=True,rc6_restore_model_passed=True,
                oversized_erase_detected_and_restored=True,native_timer_hardware_verified=native,
                first_8k_identical=True,current_settings_preserved=True,
                source_sha256={name:sha((ROOT/name).read_bytes()) for name in sorted(set(paths))})
    a.candidate.with_suffix('.validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
