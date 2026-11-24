#!/usr/bin/env python3
"""Install the source-built radio with the verified sector writer and rollback."""
import argparse
import hashlib
import json
from pathlib import Path

from band_patch import ROOT
from build_radio_image import build,VERSION
from build_full_feature_image import build as build_rc6
from firmware_integrity import check,TABLE,COUNT
from flash_update import Session,install,RESOURCE_SECTORS
from spi_flash import Flash,RawTransport,PROTECTED,CAPACITY,SECTOR
from boot_version import OFFSET,SIZE
from validation_requirements import RADIO_APPLICATION_CASES,RADIO_VOLUME_GPIO_CASES

CODE_END=TABLE+COUNT*2
KNOWN_OPEN={
    # OPEN-0.2.6 (installed 2026-11-12): the upgrade source for the stock-task-loop release.
    '31e1727697870a270deebe971f4afe003eed76fab446491f843567aa35186233':
        '2d5739d2a0f3c12bff4a12deedeca82c25bbdea991ea3c105c0980f0b6722b12',
    # OPEN-0.2.5 remains an accepted upgrade/restore source.
    '29671cf26c567174d7ddd98bf34d4bdeb4eed0f05db8f8aee23e09e5471f31be':
        'b89840b229ec548225140b19e4badfbf6fb01fe7dfccb2cf0ecd27e538c4c3ec',
    # OPEN-0.2.4 is retained for migration; its ATCON0 gain experiment is superseded.
    'fd6196c3695645b5424ed7ea995bd267a6ddeb16218c3efbe0a015dd8f9b7988':
        '1fc5d42172589e89eef7604465f15cc7673c245659e340e3b71dd092ce80ef73',
    # Qualified OPEN-0.2.3: preserve its reviewed upgrade/restore path.
    'a28a84f5a43e1ace5df5a04cfa4e18af12f1182477e810bf85c2e66550a5032d':
        'f826c2b6ff9dcdac75462e9d2111b0f5152d36322034933d0b966f5e16bd1a49',
    'aeedaafcee51c41ed19d6a7f37d36dd661560a75167a5d00cff2d8289c2bcd1f':
        '61a1d1ac05265f35e2860f52895b22da895cd7d0680b0da49d91b82b8506abba',
    # Reviewed OPEN-0.2 boot candidate; its two-block load is fixed in 0.2.1.
    '04da1aab91a79531e53747f17ed04091ca0032b4cee294b6a6fa22a823e792fa':
        '8123cf7839a8824c412a110a0b97972a31ee9de963ad63064a072179bbfd5524',
    'db34d0bc7b3c58498c2b3ca13c849b36b4f9370e280b04ccc12e8a96fd49eb6c':
        '403f6cccae4e25c609d7df0d7a6d1174e017b6ca3987b629a1d22c25fa04d00f',
}
def sha(data):return hashlib.sha256(data).hexdigest()


def prepare(baseline,release,source,*,restore_rc6=False):
    opened,_=build(source,ROOT/'build/radio-install-rebuild')
    rc6,_=build_rc6(source)
    expected=rc6 if restore_rc6 else opened
    if release!=expected:raise ValueError('candidate differs from its reproducible build')
    check(baseline)
    known=[source,rc6,opened]
    matches=[image for image in known if image[:CODE_END]==baseline[:CODE_END]]
    matched=any(image[OFFSET:OFFSET+SIZE]==baseline[OFFSET:OFFSET+SIZE] for image in matches)
    matched=matched or KNOWN_OPEN.get(sha(baseline[:CODE_END]))==sha(baseline[OFFSET:OFFSET+SIZE])
    if not matched:
        raise ValueError('baseline is not supported stock, RC6, or this exact OPEN build')
    target=bytearray(baseline)
    target[:CODE_END]=release[:CODE_END];target[OFFSET:OFFSET+SIZE]=release[OFFSET:OFFSET+SIZE]
    target=bytes(target);check(target)
    if target[:PROTECTED]!=baseline[:PROTECTED]:raise ValueError('protected boot/header prefix changed')
    if target[CODE_END:OFFSET]!=baseline[CODE_END:OFFSET] or target[OFFSET+SIZE:]!=baseline[OFFSET+SIZE:]:
        raise ValueError('settings or unapproved resources changed')
    sectors=[start for start in range(PROTECTED,CAPACITY,SECTOR) if baseline[start:start+SECTOR]!=target[start:start+SECTOR]]
    if any(start>=CODE_END and start not in RESOURCE_SECTORS for start in sectors):
        raise ValueError('unapproved target sector')
    return target,sectors


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--source',type=Path,default=ROOT/'stock.bin')
    p.add_argument('--image',type=Path,default=ROOT/f'build/v115-{VERSION.lower()}.bin')
    p.add_argument('--session',type=Path,required=True)
    p.add_argument('--restore-rc6',action='store_true',help='restore reproducible RC6 code while retaining current settings')
    p.add_argument('--write',action='store_true')
    a=p.parse_args();source=a.source.read_bytes();baseline=a.baseline.read_bytes()
    if a.baseline.with_suffix('.verify.bin').read_bytes()!=baseline:p.error('independent baseline reads differ')
    release=a.image.read_bytes()
    if not a.restore_rc6:
        validation=json.loads(a.image.with_suffix('.validation.json').read_text())
        if (validation.get('candidate_sha256')!=sha(release) or validation.get('application_cases')!=RADIO_APPLICATION_CASES or
                validation.get('amplifier_gpio_cases')!=4 or
                validation.get('volume_gpio_cases')!=RADIO_VOLUME_GPIO_CASES or
                not validation.get('zero_volume_and_mute_disable_speaker') or
                not validation.get('stock_tuner_pre_gain_preserved') or
                not validation.get('volume_roundtrip_no_audio_interrupt') or not validation.get('constant_tuner_volume') or
                not validation.get('boot_entry_checked') or not validation.get('timer_configuration_checked') or
                not validation.get('overlay_load_length_checked') or validation.get('boot_key_cases')!=2 or
                not validation.get('stock_boot_selection_checked') or not validation.get('radio_return_path_checked') or
                not validation.get('bank_context_restore_checked') or validation.get('image_handoff_cases')!=6 or
                not validation.get('usb_overlay_relocation_checked') or
                not validation.get('normal_update_and_recovery_model_passed')):
            p.error('complete image-bound autonomous validation is required')
        for name,digest in validation['source_sha256'].items():
            if sha((ROOT/name).read_bytes())!=digest:p.error('validated source changed: '+name)
        if a.write and not (validation.get('native_timer_hardware_verified') and validation.get('volume_hardware_verified')):
            p.error('matching native timer and volume RAM hardware reports are required before writing')
    target,sectors=prepare(baseline,release,source,restore_rc6=a.restore_rc6)
    session=Session(a.session);session.save('original.bin',baseline);session.save('target.bin',target)
    session.report('plan.json',dict(version='1.5-RC6' if a.restore_rc6 else VERSION,
                   baseline_sha256=sha(baseline),target_sha256=sha(target),release_sha256=sha(release),
                   sectors=sectors,first_8k_identical=True,settings_preserved=True,write_requested=a.write))
    print('Prepared',len(sectors),'sectors; protected prefix and current settings are identical.',flush=True)
    if a.write:
        t=RawTransport()
        try:
            flash=Flash(t);flash.initialize()
            install(flash,baseline,target,sectors,session,permitted_resource_sectors=RESOURCE_SECTORS,
                    release_sha256=sha(release),version='1.5-RC6' if a.restore_rc6 else VERSION)
        finally:t.close()


if __name__=='__main__':main()
