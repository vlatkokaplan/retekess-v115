#!/usr/bin/env python3
"""Audit the OPEN image: stock-loop boot, radio entry/return, banks, timer setup, and NOR recovery."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from band_patch import ROOT,decode
from build_radio import build as compile_radio
from build_radio_image import (build,VERSION,OVERLAY,OVERLAY_SIZE,HANDOFF,LOAD_COUNT,DISPATCH,BOOT_MODE,BOOT_MODE_STOCK,
                               BOOT_MODE_RADIO,USB_OVERLAY,USB_OVERLAY_SIZE,USB_RELOCATED,USB_LOADER,bank_flash,overlay_page)
from firmware_integrity import check,TABLE,COUNT,finalize
from build_full_feature_image import build as build_rc6
from boot_version import OFFSET,SIZE
from lcd_draw import Asm
from emulation import run,copy_xram
from run_radio import wrapper
from flash_radio import prepare
from flash_update import install,RESOURCE_SECTORS
from verify_flash_update import NorModel,MemorySession
from validation_requirements import RADIO_APPLICATION_CASES,RADIO_VOLUME_GPIO_CASES
from stock_audio import VOLUME_REGISTERS
from verify_radio import stock_stubs,bank_chunks,MEDIA,CALL_LOG,TASK_ID,BOARD_XDATA,EQ_SET
MENU_BANK,SYSTEM_BANK=0x2C,0x2D
# Without the context wrapper, P2 is checked as well.
BOARD_SFRS=[0xBA,0xBB,0xBC,0xBD,0x90,0xA0,0xB0,0xFD,0xD3,0xDA,0xD9,0x9C,0xDB]


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


def boot_selection_checks(plain,original):
    """Bank 11 0x1ACE: the saved task (A) becomes R6. Stock clips it to 1..3; OPEN always picks radio."""
    def select(window,saved):
        a=Asm(0xA000);a.movdi(0x81,0x8F);a.movai(saved);a.movri(6,0x55);a.emit(0x02,0x1A,0xCE)
        capture=Asm(0x1AD6);capture.movar(6);capture.movdptr(0xB000);capture.movxda()
        stop=capture.base+len(capture.code);capture.emit(0x80,0xFE)
        return run([(0x1ACE,window),(a.base,a.assemble()),(capture.base,capture.assemble())],a.base,stop,0xB000,1)[0]
    assert original[BOOT_MODE:BOOT_MODE+8]==BOOT_MODE_STOCK and plain[BOOT_MODE:BOOT_MODE+8]==BOOT_MODE_RADIO
    for saved in (0,1,2,3,4,0x13,255):
        assert select(BOOT_MODE_STOCK,saved)==(saved if saved<4 else 1),('stock boot model',saved)
        assert select(plain[BOOT_MODE:BOOT_MODE+8],saved)==3,('boot always selects the radio task',saved)
    return 7


def return_path_check(manifest,code,mapping):
    """radio_forever returns to the stock radio task with its context, after the handoff bank call."""
    entry=manifest['entries']['radio_forever']
    main=int(re.search(r'C:\s+([0-9A-F]+)\s+_radio_main\s',mapping)[1],16)
    body=bytearray(code);call=bytes([0x12,main>>8,main&255]);at=body.find(call,entry-0x4000)
    assert 0<=at<entry-0x4000+64,'radio_forever main call not found'
    body[at+1:at+3]=bytes([0x91,0x00])
    stub=Asm(0x9100)  # radio_main stand-in: leave every saved register clobbered
    for direct in range(16):stub.movdi(direct,0xC0+direct)
    for reg,value in [(0xA8,0x11),(0xA9,0x22),(0x86,0x33),(0xF7,0x00)]:stub.movdi(reg,value)
    stub.ret()
    # Stock trampoline stand-in: record A, DPH, DPL, PSW, IE and IE1 at the handoff call.
    trampoline=Asm(0x0133);trampoline.emit(0x85,0x83,0x74,0x85,0x82,0x75,0x85,0xD0,0x76)
    trampoline.movdptr(0xB020);trampoline.movxda()
    for reg in [0x74,0x75,0x76,0xA8,0xA9]:trampoline.movad(reg);trampoline.emit(0xA3);trampoline.movxda()
    trampoline.ret()
    a=Asm(0xA000);a.movdi(0x81,0x8F)
    for direct in range(16):a.movdi(direct,0x30+direct)
    for reg,value in [(0xA8,0x85),(0xA9,0x5A),(0x86,0x40),(0xF7,0x1D)]:a.movdi(reg,value)
    a.movdi(0xD0,0x18|0x40);a.emit(0x12,entry>>8,entry&255)
    capture=Asm(0x9000);capture.emit(0x85,0xD0,0x7F);capture.movdptr(0xB000)
    for source in [*range(16),0xA8,0xA9,0x86,0xF7,0x7F]:capture.movad(source);capture.movxda();capture.emit(0xA3)
    stop=capture.base+len(capture.code);capture.emit(0x80,0xFE)
    a.emit(0x02,0x90,0x00)
    got=run([(0x4000,bytes(body)),(a.base,a.assemble()),(stub.base,stub.assemble()),(trampoline.base,trampoline.assemble()),
             (capture.base,capture.assemble())],a.base,stop,0xB000,0x26)
    assert got[:16]==bytes(range(0x30,0x40)),('stock registers 0x00-0x0F restored',got[:16].hex())
    assert got[16:20]==bytes([0x85,0x5A,0x40,0x3D]),('IE/IE1/DPS restored and watchdog kicked',got[16:20].hex())
    assert got[20]&0xFE==0x58,('stock PSW restored after the handoff',hex(got[20]))
    # Handoff call: system bank entry 0x180C, with stock interrupts live and register bank 0.
    assert got[0x20]==SYSTEM_BANK and got[0x21:0x23]==bytes([0x18,0x0C]),('handoff bank call',got[0x20:0x26].hex())
    assert got[0x23]&0x18==0 and got[0x24:0x26]==bytes([0x85,0x5A]),('handoff context',got[0x20:0x26].hex())


def bank_checks(manifest,code,plain,original):
    """Run the image's own system bank against the real overlay drivers."""
    system=plain[bank_flash(SYSTEM_BANK):bank_flash(SYSTEM_BANK)+0x800]
    for bank in manifest['banks']:
        data=Path(bank['path']).read_bytes();start=bank_flash(bank['bank'])
        assert plain[start:start+len(data)]==data and not any(plain[start+len(data):start+0x800]),('bank image',bank['bank'])
        assert not any(original[start:start+0x800]),('bank was not empty in stock',bank['bank'])
    assert plain[USB_RELOCATED:USB_RELOCATED+USB_OVERLAY_SIZE]==original[USB_OVERLAY:USB_OVERLAY+USB_OVERLAY_SIZE]
    page=overlay_page(USB_RELOCATED)
    assert plain[USB_LOADER:USB_LOADER+6]==bytes([0x79,3,0x7A,page&255,0x7B,page>>8]),'USB overlay loader'
    stubs=stock_stubs(0x1800);fields=manifest['fields'];state=manifest['state_address']
    # Board context: radio_enter saves, the radio reconfigures, radio_leave (with its real
    # amplifier, I2C and LCD drivers) must still return every register to its stock value.
    a=Asm(0xA000);a.movdi(0x81,0x8F);a.movdi(0x86,0)
    def put(address,value):a.movdptr(address);a.movai(value);a.movxda()
    for i,address in enumerate(BOARD_XDATA):put(address,0x40+i)
    for i,reg in enumerate(BOARD_SFRS):a.movdi(reg,0x60+i)
    a.emit(0x12,0x18,0x00)
    for address in BOARD_XDATA:put(address,0xEE)
    for reg in BOARD_SFRS:a.movdi(reg,0xEE)
    a.emit(0x12,0x18,0x06)
    for i,address in enumerate(BOARD_XDATA):a.movdptr(address);a.emit(0xE0);a.movdptr(0xB000+i);a.movxda()
    for i,reg in enumerate(BOARD_SFRS):a.movad(reg);a.movdptr(0xB010+i);a.movxda()
    stop=a.base+len(a.code);a.emit(0x80,0xFE)
    got=run([(0x4000,code),(0x1800,system),*stubs,(a.base,a.assemble())],a.base,stop,0xB000,0x10+len(BOARD_SFRS),timeout=60)
    assert got[:len(BOARD_XDATA)]==bytes(0x40+i for i in range(len(BOARD_XDATA))),('real-bank XDATA restore',got.hex())
    assert got[0x10:]==bytes(0x60+i for i in range(len(BOARD_SFRS))),('real-bank SFR restore',got.hex())
    sd,usb=(0x0C,0x1F,0xD1,0),(0x0C,0x1F,0xD1,1)
    start=[(0,0x1D,0xC9,2),(4,0x1E,0x81,None),(0,0x1D,0xC9,0)]
    def eq(preset):return (*EQ_SET,preset)
    cases=[(1,(1,0,1),4,1,[eq(4),sd,*start],0),(1,(1,1,1),6,1,[eq(6),usb,*start],0),(1,(0,1,0),0,1,[eq(0),usb],0),
           (1,(0,0,1),4,3,[],1),(0x13,(1,1,1),4,0x13,[],0),(0,(1,1,1),4,3,[],0)]
    for handoff,media,preset,task,calls,message in cases:
        a=Asm(0xA000);a.movdi(0x81,0x8F);a.movdi(0x86,0)
        put(TASK_ID,3);put(CALL_LOG,0);put(state+fields['handoff'],handoff);put(state+fields['message'],0)
        put(state+fields['eq'],preset)
        for i,value in enumerate(media):put(MEDIA+i,value)
        a.emit(0x12,0x18,0x0C)
        copy_xram(a,CALL_LOG,0xB000,65,'calls');copy_xram(a,TASK_ID,0xB041,1,'task')
        copy_xram(a,state+fields['message'],0xB042,1,'message')
        stop=a.base+len(a.code);a.emit(0x80,0xFE)
        got=run([(0x4000,code),(0x1800,system),*stubs,(a.base,a.assemble())],a.base,stop,0xB000,0x43)
        log=got[1:1+got[0]];seen=[tuple(log[i:i+4]) for i in range(0,len(log),4)]
        assert len(seen)==len(calls) and all(c[:3]==v[:3] and (v[3] is None or c[3]==v[3]) for c,v in zip(seen,calls)),\
            ('image handoff calls',handoff,media,seen)
        assert got[0x41]==task and got[0x42]==message,('image handoff task/message',handoff,media,got[0x41:0x43].hex())
    return len(cases)


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
        # app_init resumes or initializes the state through the (simulation-linked) system bank.
        got=run([(0x4000,code),(a.base,a.assemble()),*bank_chunks(m)[0],
                 (m['entries']['radio_timer_start'],bytes.fromhex('02 90 00')),
                 (capture.base,capture.assemble())],a.base,stop,0xB000,4)
        assert got==bytes([key,2,key,255]),('startup key must be consumed',key,got.hex())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=ROOT/'stock.bin')
    p.add_argument('--candidate',type=Path,default=ROOT/f'build/v115-{VERSION.lower()}.bin')
    p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--application-report',type=Path,default=ROOT/'build/radio-autonomous-sim/validation.json')
    p.add_argument('--hardware-report',type=Path)
    p.add_argument('--volume-hardware-report',type=Path)
    a=p.parse_args();source=a.source.read_bytes();candidate=a.candidate.read_bytes();baseline=a.baseline.read_bytes()
    rebuilt,meta=build(source,ROOT/'build/radio-audit-rebuild');assert candidate==rebuilt,'candidate differs from source build'
    compiled=meta['compiled'];code=(ROOT/'build/radio-audit-rebuild/radio.bin').read_bytes()
    application=json.loads(a.application_report.read_text())
    assert application['autonomous'] and application['cases_passed']==RADIO_APPLICATION_CASES and application['hardware_sha256']==compiled['sha256']
    assert application['amplifier_gpio_cases_passed']==4
    assert application['menu'] and application['menu_frames_verified'] and application['stock_music_handoff_verified']
    assert application['state_resume_verified'] and application['board_context_restore_verified']
    assert application['volume_gpio_cases_passed']==RADIO_VOLUME_GPIO_CASES
    assert application['stock_volume_curve_verified']
    assert application['volume_roundtrip_no_audio_interrupt'] and application['constant_tuner_volume']
    assert application['zero_volume_and_mute_disable_speaker'] and application['stock_tuner_pre_gain_preserved']
    for name,digest in application['source_sha256'].items():assert sha((ROOT/name).read_bytes())==digest,('application source changed',name)
    check(candidate);plain=decode(candidate);original=decode(source)
    assert plain[OVERLAY:OVERLAY+len(code)]==code
    assert plain[LOAD_COUNT]==3 and len(code)<=plain[LOAD_COUNT]*0x800,'complete program must be loaded'
    assert not any(plain[OVERLAY+len(code):OVERLAY+OVERLAY_SIZE-2]),'nonempty overlay padding'
    entry=compiled['entries']['radio_forever'];assert plain[HANDOFF:HANDOFF+3]==bytes([0x12,entry>>8,entry&255])
    assert plain[DISPATCH:DISPATCH+8]==original[DISPATCH:DISPATCH+8],'stock task dispatcher must be unchanged'
    spans=[(OVERLAY,OVERLAY_SIZE),(HANDOFF,3),(LOAD_COUNT,1),(BOOT_MODE,8),(USB_RELOCATED,USB_OVERLAY_SIZE),
           (USB_LOADER+3,1),(USB_LOADER+5,1),*[(bank_flash(b['bank']),0x800) for b in compiled['banks']],
           (TABLE,COUNT*2),(OFFSET,SIZE)]
    allowed=bytearray(len(plain))
    for start,size in spans:allowed[start:start+size]=bytes([1])*size
    assert all(allowed[i] or x==y for i,(x,y) in enumerate(zip(plain,original))), 'unreviewed image changes'
    assert candidate[:0x2000]==source[:0x2000]
    entry_checks(compiled,code);timer_check(compiled,code);boot_key_checks()
    boot_cases=boot_selection_checks(plain,original)
    return_path_check(compiled,code,(ROOT/'build/radio-audit-rebuild/radio.map').read_text())
    handoff_cases=bank_checks(compiled,code,plain,original)
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
    volume_verified=False
    if a.volume_hardware_report:
        volume=json.loads(a.volume_hardware_report.read_text())
        timer_build=compile_radio(ROOT/'build/radio-volume-audit',autonomous=True,timer_batch=True)
        assert volume['completed'] and not volume['flash_written'] and not volume['audio_enabled']
        assert volume['application_sha256']==timer_build['sha256'] and volume['speaker_muted_on_exit']
        assert volume['verification_source_sha256']==sha((ROOT/'verify_radio_volume_hardware.py').read_bytes())
        assert volume['helper_source_sha256']==sha((ROOT/'preview_radio_volume.py').read_bytes())
        assert len(volume['samples'])==33
        assert all(row['level']==index and row['gain_register']==VOLUME_REGISTERS[index] and row['control_register']&0xFE==0xFE and row['volume']==24+index
                   and not row['speaker'] and row['muted'] and not row['pending'] and not row['error'] and row['pre_gain']==0x8C
                   for index,row in enumerate(volume['samples']))
        assert volume['up_down_up_verified'] and volume['constant_tuner_volume']
        assert len(volume['sweep_samples'])==97
        expected=[('up',level) for level in range(33)]+[('down',level) for level in range(31,-1,-1)]+[('up_again',level) for level in range(1,33)]
        for row,(direction,level) in zip(volume['sweep_samples'],expected):
            assert row['direction']==direction and row['level']==level and row['gain_register']==VOLUME_REGISTERS[level]
            assert row['control_register']==(255 if level==0 else 254) and row['tuner_volume']==0x8C and not row['speaker'] and row['muted'] and not row['error']
        assert volume['zero_volume_mutes_tuner'] and volume['retune_gain_preserved'] and volume['standby_resume_volume_verified']
        for name,digest in volume['source_sha256'].items():assert sha((ROOT/name).read_bytes())==digest
        volume_verified=True
    paths=[*compiled['source_sha256'], 'build_radio_image.py','verify_radio.py','verify_radio_image.py',
           'flash_radio.py','flash_update.py','spi_flash.py','usb_dump.py','loader_packets.py','firmware_integrity.py','boot_version.py',
           'radio_audio.py','radio_timer.py','preview_radio_timer.py','validation_requirements.py',
           'verify_radio_volume_hardware.py','preview_radio_volume.py','stock_audio.py','verify_stock_audio.py']
    report=dict(version=VERSION,candidate_sha256=sha(candidate),compiled_sha256=compiled['sha256'],
                baseline_sha256=sha(baseline),application_cases=RADIO_APPLICATION_CASES,amplifier_gpio_cases=4,
                volume_gpio_cases=RADIO_VOLUME_GPIO_CASES,zero_volume_and_mute_disable_speaker=True,stock_tuner_pre_gain_preserved=True,
                volume_hardware_verified=volume_verified,volume_roundtrip_no_audio_interrupt=True,constant_tuner_volume=True,
                boot_entry_checked=True,timer_configuration_checked=True,timer_configuration_cases=16,
                overlay_load_length_checked=True,boot_key_cases=2,
                stock_boot_selection_checked=True,stock_boot_selection_cases=boot_cases,radio_return_path_checked=True,
                bank_context_restore_checked=True,image_handoff_cases=handoff_cases,usb_overlay_relocation_checked=True,
                banks={f"{b['bank']:02x}":dict(flash=bank_flash(b['bank']),bytes=b['bytes'],sha256=b['sha256']) for b in compiled['banks']},
                normal_update_and_recovery_model_passed=True,partial_program_rollback_pages=completed[1:],
                unsafe_images_rejected=True,rc6_restore_model_passed=True,
                oversized_erase_detected_and_restored=True,native_timer_hardware_verified=native,
                first_8k_identical=True,current_settings_preserved=True,
                source_sha256={name:sha((ROOT/name).read_bytes()) for name in sorted(set(paths))})
    a.candidate.with_suffix('.validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
