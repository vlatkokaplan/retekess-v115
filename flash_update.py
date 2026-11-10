#!/usr/bin/env python3
"""Install a reproducible image while preserving the bootloader and device settings.

Default is offline preparation. --write performs the authorized hardware update.
The first 8 KiB and settings are protected. Only the welcome bitmap may change
outside the executable and its CRC table.
"""
import argparse
import hashlib
import json
import os
import signal
from pathlib import Path

from firmware_integrity import check, TABLE, COUNT
from spi_flash import Flash, RawTransport, ROOT, CAPACITY, PROTECTED, SECTOR, PAGE

from boot_version import OFFSET, SIZE
from build_full_feature_image import build
from native_features import VERSION
from validation_requirements import *

RESOURCE_SECTORS=tuple(range(OFFSET//SECTOR*SECTOR,(OFFSET+SIZE-1)//SECTOR*SECTOR+SECTOR,SECTOR))
TRIAL=0x1D000
CODE_END=TABLE+COUNT*2
KNOWN_PRIOR_IMAGES={
    '90d1da53c06b84c259c78c2da05a2dab333fa66b11a72cff37f9ffc18f5f1ed7':
    dict(version='1.5-RC3',welcome_sha256='69b8fd06bb363cf0e6ada3f468dcb64366b8f6d0ba6974f5fab7703c199df1bf'),
    'db1e3a4d2d437fc3192b43db457d244f6bd2f3349f13ceac180e689e4f20f0c0':
    dict(version='1.5-RC4',welcome_sha256='425cdafa622adb977d803db8abbaae83a15664d7398bdc7aa4a2353fda03bdc3')
}
DATASHEET='https://www.zbitsemi.com/upload/file/20200901/20200901155301_74863.pdf'


def sha(data):return hashlib.sha256(data).hexdigest()


def prepare(baseline, release, source):
    rebuilt, _ = build(source)
    if release != rebuilt:
        raise ValueError('candidate differs from a reproducible build of the verified stock source')
    check(baseline)
    # Accept the supported stock executable or the exact current release.
    # Older custom versions need their separately reviewed migration path.
    prefix = baseline[:CODE_END]
    if prefix == source[:CODE_END]:
        expected_welcome = sha(source[OFFSET:OFFSET+SIZE])
    elif prefix == release[:CODE_END]:
        expected_welcome = sha(release[OFFSET:OFFSET+SIZE])
    elif sha(prefix) in KNOWN_PRIOR_IMAGES:
        expected_welcome=KNOWN_PRIOR_IMAGES[sha(prefix)]['welcome_sha256']
    else:
        raise ValueError('device executable is neither stock, this release, nor a reviewed prior release')
    if sha(baseline[OFFSET:OFFSET+SIZE]) != expected_welcome:
        raise ValueError('device welcome bitmap differs from its known executable version')
    target = bytearray(baseline)
    target[:CODE_END] = release[:CODE_END]
    target[OFFSET:OFFSET+SIZE] = release[OFFSET:OFFSET+SIZE]
    target = bytes(target)
    check(target)
    if target[:PROTECTED] != baseline[:PROTECTED]:
        raise ValueError('target changes protected boot/header prefix')
    if (target[CODE_END:OFFSET] != baseline[CODE_END:OFFSET] or
            target[OFFSET+SIZE:] != baseline[OFFSET+SIZE:]):
        raise ValueError('target changes unapproved resources or settings')
    sectors = [start for start in range(0, CAPACITY, SECTOR)
               if target[start:start+SECTOR] != baseline[start:start+SECTOR]]
    if any(start < PROTECTED or start >= CODE_END and start not in RESOURCE_SECTORS for start in sectors):
        raise ValueError('target changes an unapproved sector')
    return target, sectors


class Session:
    def __init__(self,path):
        self.path=path
        path.mkdir(parents=True,exist_ok=False)

    def save(self,name,data):
        with (self.path/name).open('xb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())

    def event(self,stage,**fields):
        record=dict(stage=stage,**fields)
        with (self.path/'journal.jsonl').open('a') as f:
            f.write(json.dumps(record)+'\n');f.flush();os.fsync(f.fileno())
        print(stage,fields,flush=True)

    def report(self,name,data):
        self.save(name,(json.dumps(data,indent=2)+'\n').encode())


def program_sector(flash,start,data,session):
    if start<PROTECTED or start%SECTOR or len(data)!=SECTOR:
        raise ValueError('invalid or protected sector rewrite')
    session.event('erase_pending',address=start)
    flash.erase(start)
    if flash.read(start,SECTOR)!=b'\xff'*SECTOR:
        raise RuntimeError(f'erase verification failed at {start:#x}')
    for offset in range(0,SECTOR,PAGE):
        page=data[offset:offset+PAGE]
        if page!=b'\xff'*PAGE:
            flash.program(start+offset,page)
    if flash.read(start,SECTOR)!=data:
        raise RuntimeError(f'sector verification failed at {start:#x}')
    session.event('sector_verified',address=start,sha256=sha(data))


def rollback(flash,baseline,session,geometry_verified,permitted_resource_sectors=()):
    flash.wait_ready()
    current=flash.read(0,CAPACITY)
    if current[:PROTECTED]!=baseline[:PROTECTED]:
        raise RuntimeError('protected prefix mismatch; external recovery required')
    for start in range(PROTECTED,CAPACITY,SECTOR):
        have=current[start:start+SECTOR];want=baseline[start:start+SECTOR]
        if have==want:continue
        if start>=CODE_END and start not in permitted_resource_sectors:
            raise RuntimeError('unexpected resource/settings change; external recovery required')
        session.event('rollback_sector',address=start)
        if all(a&b==b for a,b in zip(have,want)):
            # Also recovers an unexpectedly oversized initial erase without
            # repeating an erase whose geometry has not yet been established.
            for offset in range(0,SECTOR,PAGE):
                page=want[offset:offset+PAGE]
                if page!=have[offset:offset+PAGE]:flash.program(start+offset,page)
            if flash.read(start,SECTOR)!=want:
                raise RuntimeError('program-only rollback failed')
        elif geometry_verified:
            program_sector(flash,start,want,session)
        else:
            raise RuntimeError('rollback needs erase before geometry was verified')
    flash.write_disable()
    restored=flash.read(0,CAPACITY)
    if restored!=baseline:raise RuntimeError('full rollback verification failed')
    session.save('rollback-readback.bin',restored)
    session.event('original_restored',sha256=sha(restored))


def install(flash,baseline,target,sectors,session,*,trial=True,
            permitted_resource_sectors=(),release_sha256=None,version=VERSION):
    probe=flash.probe()
    if probe['jedec_id']!='5e4015' or probe['status1']!=0:
        raise ValueError('unsupported flash identity or protection/status state')
    session.report('chip.json',dict(probe,datasheet=DATASHEET,
                                  sector_bytes=SECTOR,page_bytes=PAGE))
    for pass_number in (1,2):
        current=flash.read(0,CAPACITY)
        if current!=baseline:raise ValueError('device changed since the verified backup')
        session.event('preflight_read_verified',pass_number=pass_number,sha256=sha(current))
    if flash.command(0x03,0,incoming=256)!=baseline[:256]:
        raise ValueError('direct SPI and native helper reads disagree')
    session.event('preflight_complete',sectors=sectors,protected_prefix_bytes=PROTECTED)

    changed=False
    geometry_verified=not trial
    # Avoid interrupting a libusb transfer halfway through its BOT transaction.
    # The journal and sector backups remain available after process/host failure.
    previous=signal.signal(signal.SIGINT,signal.SIG_IGN)
    try:
        flash.writable=True
        flash.write_enable();flash.write_disable()
        session.event('write_enable_disable_verified')

        if trial:
            # Establish actual erase extent away from the boot block, then prove
            # full-page writes and restoration before installing modified code.
            session.event('erase_geometry_trial_pending',address=TRIAL)
            changed=True
            flash.erase(TRIAL)
            erased=flash.read(0,CAPACITY)
            expected=baseline[:TRIAL]+b'\xff'*SECTOR+baseline[TRIAL+SECTOR:]
            if erased!=expected:raise RuntimeError('erase extent differs from exactly 4 KiB')
            geometry_verified=True
            session.event('erase_geometry_verified',address=TRIAL,bytes=SECTOR,
                          all_other_flash_bytes_preserved=True)
            for address,pattern in [(TRIAL,bytes(range(256))),
                                    (TRIAL+SECTOR-PAGE,bytes(reversed(range(256))))]:
                flash.program(address,pattern)
                if flash.read(address,512 if address%512==0 else PAGE*2)[:PAGE]!=pattern:
                    raise RuntimeError('full-page pattern verification failed')
            session.event('page_program_patterns_verified',bytes=PAGE)
            program_sector(flash,TRIAL,baseline[TRIAL:TRIAL+SECTOR],session)
            restored=flash.read(0,CAPACITY)
            if restored!=baseline:raise RuntimeError('trial restore differs from original flash')
            session.save('trial-restored-readback.bin',restored)
            session.event('restore_trial_passed',sha256=sha(restored))

        else:
            session.event('using_previous_verified_erase_program_restore_profile')

        for start in sectors:
            changed=True
            program_sector(flash,start,target[start:start+SECTOR],session)
        flash.write_disable()
        for pass_number in (1,2):
            readback=flash.read(0,CAPACITY)
            if readback!=target:raise RuntimeError('complete programmed image differs from target')
            check(readback)
            session.save(f'installed-readback-{pass_number}.bin',readback)
            session.event('installed_image_verified',pass_number=pass_number,sha256=sha(readback))
        session.report('result.json',dict(completed=True,flash_written=True,
                       installed_sha256=sha(target),original_sha256=sha(baseline),
                       release_sha256=release_sha256,version=version,settings_preserved=True,
                       first_8k_identical=True,geometry_empirically_verified=True,
                       sector_page_program_and_restore_verified=True,
                       restore_trial_performed=trial,
                       permitted_resource_sectors=list(permitted_resource_sectors),
                       sectors_written=sectors,loader_after=flash.t.identify(),
                       normal_boot_tested=False))
    except BaseException as error:
        session.event('update_failed',error=str(error),flash_may_have_changed=changed)
        if changed:
            try:rollback(flash,baseline,session,geometry_verified,permitted_resource_sectors)
            except BaseException as recovery_error:
                session.event('rollback_failed',error=str(recovery_error))
                raise RuntimeError('update and rollback failed; use session original.bin for recovery') from error
        raise
    finally:
        flash.writable=False
        signal.signal(signal.SIGINT,previous)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,required=True,help='fresh full backup with matching .verify.bin')
    p.add_argument('--source',type=Path,default=ROOT/'stock.bin')
    p.add_argument('--image',type=Path,default=ROOT/'build/v115-full-features-rc5.bin')
    p.add_argument('--session',type=Path,required=True,help='new local directory for backup, plan and journal')
    p.add_argument('--write',action='store_true',help='program flash; otherwise prepare offline only')
    args=p.parse_args()
    baseline=args.baseline.read_bytes()
    if args.baseline.with_suffix('.verify.bin').read_bytes()!=baseline:
        p.error('independent baseline backups do not match')
    release=args.image.read_bytes()
    validation=json.loads(args.image.with_suffix('.validation.json').read_text())
    if (validation.get('candidate_sha256')!=sha(release) or validation.get('version')!=VERSION or
            validation.get('native_machine_code_cases')!=NATIVE_CASES or
            validation.get('cold_preferences_cases')!=PREFERENCE_CASES or validation.get('stock_lcd_guard_cases')!=LCD_GUARD_CASES):
        p.error('complete image-bound validation report is required')
    if (validation.get('graph_pixel_cases')!=GRAPH_PIXEL_CASES or validation.get('radio_ui_cases')!=RADIO_UI_CASES or
            validation.get('scanner_state_cases')!=SCAN_STATE_CASES or validation.get('scanner_pixel_cases')!=SCAN_PIXEL_CASES):
        p.error('complete graph, interface and scanner evidence is required')
    for name,want in validation['build_source_sha256'].items():
        if sha((ROOT/name).read_bytes())!=want:p.error('validated source changed: '+name)
    target,sectors=prepare(baseline,release,args.source.read_bytes())
    if sha(baseline[:CODE_END]) in KNOWN_PRIOR_IMAGES:
        model=json.loads((ROOT/'build/flash-writer-model-validation.json').read_text())
        if (model.get('candidate_sha256')!=sha(release) or model.get('prior_code_sha256')!=sha(baseline[:CODE_END])):
            p.error('run verify_flash_update.py --prior BASELINE for this migration before writing')
        for name,want in model['source_sha256'].items():
            if sha((ROOT/name).read_bytes())!=want:p.error('reviewed migration source changed: '+name)
    session=Session(args.session)
    session.save('original.bin',baseline);session.save('target.bin',target)
    session.report('plan.json',dict(version=VERSION,original_sha256=sha(baseline),target_sha256=sha(target),
                   release_sha256=sha(release),sectors=sectors,protected_prefix_bytes=PROTECTED,
                   resource_exception=dict(offset=OFFSET,bytes=SIZE),settings_preserved=True,
                   write_requested=args.write))
    session.report('writer-source-hashes.json',{name:sha((ROOT/name).read_bytes()) for name in
                   ('flash_update.py','spi_flash.py','usb_dump.py','loader_packets.py')})
    if not args.write or not sectors:
        session.event('offline_preparation_complete',target_sha256=sha(target),sectors=sectors);return
    t=RawTransport()
    try:
        flash=Flash(t);flash.initialize()
        install(flash,baseline,target,sectors,session,trial=True,
                permitted_resource_sectors=RESOURCE_SECTORS,release_sha256=sha(release),version=VERSION)
    finally:t.close()


if __name__=='__main__':main()
