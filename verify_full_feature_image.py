#!/usr/bin/env python3
"""Audit a complete native-feature release against its source and test evidence.

Offline only. No USB transport is imported and no flash operation is available.
"""
import argparse
import hashlib
import json
from pathlib import Path

from band_patch import ROOT, decode
from build_full_feature_image import build, sector_plan
from firmware_integrity import check, preserve_original, TABLE, COUNT
import native_features as n
import boot_version


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def rejected(action):
    try:
        action()
    except ValueError:
        return True
    raise ValueError('invalid image unexpectedly accepted')


def audit(original, raw, manifest, band_evidence, native_evidence, preferences_evidence):
    exceptions=[(boot_version.OFFSET,boot_version.SIZE)]
    integrity=preserve_original(original,raw,exceptions)
    rebuilt,metadata=build(original)
    require(raw==rebuilt,'candidate differs from current reproducible build')
    require(manifest['integrity']==integrity,'manifest integrity metadata is stale')
    for field in ('source_sha256','native_layout','band_changes','bandwidth_layout','crc_balance','version','boot_image'):
        require(manifest[field]==metadata[field],f'manifest {field} is stale')
    require(manifest.get('deterministic_rebuild') is True,'deterministic build not recorded')
    plan=sector_plan(original,raw)
    require(manifest['flash_plan']==plan,'manifest sector plan is stale')
    require(all(s['start']>=0x2000 for s in plan['sectors']),
            'a proposed sector rewrite intersects the protected first 8 KiB')

    sha=integrity['sha256']
    for name,evidence in [('band',band_evidence),('native',native_evidence)]:
        require(evidence.get('candidate_sha256')==sha,f'{name} evidence belongs to another image')
    require(band_evidence.get('grid_frequencies_passed')==10226,'incomplete band grid evidence')
    require(band_evidence.get('clamp_wrap_boundary_cases_passed')==90,'incomplete band boundary evidence')
    require(band_evidence.get('ui_getter_results_verified')==10,'incomplete band getter evidence')
    require(native_evidence.get('full_suite') is True,'native evidence is only a smoke test')
    require(native_evidence.get('machine_code_cases_passed')==6922,'incomplete native feature evidence')
    require(native_evidence.get('stock_lcd_guard_cases_passed')==512,'incomplete LCD sleep guard evidence')
    require('final-image bank bytes and emitted init/wait/message hooks' in native_evidence.get('checks',[]),
            'native tests did not exercise final-image hooks')
    for name,digest in native_evidence['source_sha256'].items():
        require(sha256((ROOT/name).read_bytes())==digest,f'native test source changed: {name}')
    for name,fn in [('common',n.common_glue),('control',n.control),('display',n.renderer),('settings',n.settings),('menu',n.menu_renderer)]:
        require(native_evidence['assembled_sha256'][name]==sha256(fn()[0]),
                f'{name} machine code changed since emulation')

    require(preferences_evidence.get('candidate_sha256')==sha,'preferences evidence belongs to another image')
    require(preferences_evidence.get('cases_passed')==912,'incomplete cold preferences evidence')
    require(preferences_evidence.get('cold_owned_state_not_preseeded') is True,'cold initialization was not tested')
    for name,digest in preferences_evidence['source_sha256'].items():
        require(sha256((ROOT/name).read_bytes())==digest,f'preferences test source changed: {name}')

    plain=decode(raw)
    # Verify the displaced init store and each hook at the actual overlay offset.
    _,labels=n.common_glue()
    for name,pc,tail in [('init',0x4A12,b'\0\0\xf0'),
                         ('wait',0x4A62,b''),('message',0x4A65,b'')]:
        offset=0x1D800+pc-0x4000
        hook=bytes([0x12])+labels[name].to_bytes(2,'big')+tail
        require(plain[offset:offset+len(hook)]==hook,f'wrong {name} hook in final image')

    corrupted=bytearray(raw)
    corrupted[n.CONTROL_FLASH]^=1
    rejected(lambda:check(corrupted))
    corrupted=bytearray(raw)
    corrupted[TABLE]^=1
    rejected(lambda:check(corrupted))
    corrupted=bytearray(raw)
    corrupted[0x134000]^=1  # Valid code CRCs cannot excuse changed saved settings.
    check(corrupted)
    rejected(lambda:preserve_original(original,corrupted))
    corrupted=bytearray(raw)
    corrupted[boot_version.OFFSET-1]^=1
    rejected(lambda:preserve_original(original,corrupted,exceptions))
    rejected(lambda:sector_plan(original,raw[:-1]))

    sources=('build_full_feature_image.py','firmware_integrity.py','native_features.py',
             'band_patch.py','bandwidth_patch.py','verify_native_features.py',
             'verify_band_patch.py','verify_full_feature_image.py',
             'boot_version.py','lcd_draw.py','analyze.py','env_tools.py','verify_preferences_boot.py','assets/font-5x8.bin')
    return dict(candidate_sha256=sha,source_sha256=sha256(original),
                status='offline validated; installed application and flash/restore remain untested',
                deterministic_rebuild_verified=True,first_8k_byte_identical=True,
                all_61_block_crcs_match=True,header_and_section_crcs_match=True,
                resources_preserved_except_welcome_bitmap=True,settings_preserved=True,
                version=metadata['version'],boot_image=metadata['boot_image'],
                changed_bytes=plan['changed_bytes'],
                changed_sector_starts=[s['start'] for s in plan['sectors']],
                crc_balance=metadata['crc_balance'],
                band_grid_cases=10226,band_boundary_cases=90,band_getter_checks=10,
                native_machine_code_cases=6922,cold_preferences_cases=912,stock_lcd_guard_cases=512,final_image_hooks_exercised=True,
                invalid_image_guards_passed=['changed code','changed block CRC',
                                             'changed settings with valid CRCs','unapproved image bytes','truncated sector plan'],
                build_source_sha256={name:sha256((ROOT/name).read_bytes()) for name in sources},
                flash_written=False,flash_geometry_verified=False,
                flash_writer_and_restore_verified=False,
                evidence_scope='New instructions and hook bytes tested with simulated board services. '
                               'Preserved boot bytes reduce update exposure; recovery after power loss '
                               'or an invalid application has not been demonstrated.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('candidate',type=Path)
    p.add_argument('--source',type=Path,default=ROOT/'stock.bin')
    args=p.parse_args()
    def evidence(suffix):
        return json.loads(args.candidate.with_suffix(suffix).read_text())
    result=audit(args.source.read_bytes(),args.candidate.read_bytes(),
                 evidence('.manifest.json'),evidence('.emulation.json'),
                 evidence('.native-emulation.json'),evidence('.preferences-boot-emulation.json'))
    path=args.candidate.with_suffix('.validation.json')
    path.write_text(json.dumps(result,indent=2)+'\n')
    print('Release audit passed:',path)
    print('SHA-256:',result['candidate_sha256'])
    print('Protected first 8 KiB unchanged; all CRCs and image-bound test evidence pass.')


if __name__=='__main__':
    main()
