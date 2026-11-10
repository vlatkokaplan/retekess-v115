#!/usr/bin/env python3
"""Exercise update guards and recovery with an independent NOR memory model."""
import argparse
import hashlib
import json
from pathlib import Path

from spi_flash import transaction_stub, CAPACITY, PROTECTED, SECTOR, PAGE, ROOT
from flash_update import prepare, install, RESOURCE_SECTORS, CODE_END, KNOWN_PRIOR_IMAGES
from build_full_feature_image import build
from native_features import VERSION
from firmware_integrity import finalize
from band_patch import decode
from boot_version import OFFSET


class MemorySession:
    def __init__(self):self.files={};self.events=[]
    def save(self,name,data):self.files[name]=data
    def event(self,stage,**data):self.events.append(dict(stage=stage,**data))
    def report(self,name,data):self.files[name]=data


class NorModel:
    def __init__(self,baseline,fail_program=None,oversized_erase=False):
        self.data=bytearray(baseline);self.writable=False
        self.program_count=0;self.fail_program=fail_program
        self.oversized_erase=oversized_erase;self.erase_count=0
        self.t=self
    def identify(self):return dict(model=True)
    def probe(self):return dict(jedec_id='5e4015',status1=0)
    def wait_ready(self):return 0
    def write_enable(self):assert self.writable
    def write_disable(self):pass
    def command(self,opcode,address=None,incoming=0):
        assert opcode==3
        return bytes(self.data[address:address+incoming])
    def read(self,address,size):return bytes(self.data[address:address+size])
    def erase(self,address):
        assert self.writable and address>=PROTECTED and address%SECTOR==0
        self.erase_count+=1
        start,size=address,SECTOR
        if self.oversized_erase and self.erase_count==1:start,size=address//65536*65536,65536
        self.data[start:start+size]=b'\xff'*size
    def program(self,address,data):
        assert self.writable and address>=PROTECTED
        assert data and address//PAGE==(address+len(data)-1)//PAGE
        self.program_count+=1
        if self.program_count==self.fail_program:
            # Simulate partial programming before an operation reports failure.
            for i,value in enumerate(data[:len(data)//2]):self.data[address+i]&=value
            self.fail_program=None
            raise RuntimeError('injected program failure')
        for i,value in enumerate(data):self.data[address+i]&=value


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=ROOT/'stock.bin')
    p.add_argument('--prior',type=Path,help='also model a reviewed prior-release migration from a local image')
    args=p.parse_args()
    root=ROOT/'build';root.mkdir(exist_ok=True)
    source=args.source.read_bytes();release,_=build(source)
    baseline=bytearray(source);baseline[0x134020]^=1;baseline=bytes(baseline)
    target,sectors=prepare(baseline,release,source)
    def run(model,session):
        install(model,baseline,target,sectors,session,permitted_resource_sectors=RESOURCE_SECTORS,release_sha256=hashlib.sha256(release).hexdigest())
    same,empty=prepare(target,release,source);assert same==target and not empty
    def reject(action):
        try:action()
        except ValueError:return
        raise AssertionError('unsafe image plan accepted')
    changed=bytearray(release);changed[0x134020]^=1
    reject(lambda:prepare(baseline,bytes(changed),source))
    changed=decode(source);changed[0x1C800]^=1;unknown,_=finalize(changed)
    reject(lambda:prepare(unknown,release,source))
    changed=bytearray(baseline);changed[OFFSET]^=1
    reject(lambda:prepare(bytes(changed),release,source))
    rejected=0
    invalid=[dict(opcode=op,address=addr,writable=True,
                  outgoing=b'\x55'*size if op==2 else b'')
             for op,addr,size in [(0x20,0,0),(0x20,0x1000,0),(0x20,0x2001,0),
                                  (0x20,CAPACITY,0),(2,0x1FFF,1),(2,0x20FF,2),
                                  (2,0x2000,257),(2,CAPACITY,1),(2,0x2000,0)]]
    invalid += [dict(opcode=op,writable=True) for op in (0x01,0x31,0xC7,0x60,0xD8,0x52)]
    invalid += [dict(opcode=0x06),dict(opcode=0x20,address=0x2000)]
    for request in invalid:
        try:transaction_stub(**request)
        except ValueError:rejected+=1
        else:raise AssertionError(f'unsafe SPI request accepted: {request}')
    for address in sectors:
        transaction_stub(0x20,address,writable=True)
        for offset in range(0,SECTOR,PAGE):
            assert len(transaction_stub(2,address+offset,outgoing=bytes(range(256)),writable=True))<=512

    model=NorModel(baseline);session=MemorySession()
    run(model,session)
    assert bytes(model.data)==target and model.data[:PROTECTED]==baseline[:PROTECTED]
    assert session.files['result.json']['completed'] and not model.writable
    recovered=[]
    # Fail during the trial, during the first target sector, and mid-update.
    for failed_page in (1,24,60,90):
        model=NorModel(baseline,fail_program=failed_page);session=MemorySession()
        try:run(model,session)
        except RuntimeError:pass
        else:raise AssertionError('injected failure did not interrupt update')
        assert bytes(model.data)==baseline and not model.writable
        assert any(e['stage']=='original_restored' for e in session.events)
        recovered.append(failed_page)
    model=NorModel(baseline,oversized_erase=True);session=MemorySession()
    try:run(model,session)
    except RuntimeError:pass
    else:raise AssertionError('oversized erase was not rejected')
    assert bytes(model.data)==baseline and not model.writable
    assert target[0x134000:]==baseline[0x134000:]
    assert session.events
    prior_sha=None
    if args.prior:
        prior=args.prior.read_bytes();prior_sha=hashlib.sha256(prior[:CODE_END]).hexdigest()
        assert prior_sha in KNOWN_PRIOR_IMAGES
        migrated,changed=prepare(prior,release,source)
        for failure in (None,1,24,60,90):
            model=NorModel(prior,fail_program=failure);session=MemorySession()
            try:install(model,prior,migrated,changed,session,permitted_resource_sectors=RESOURCE_SECTORS)
            except RuntimeError:
                assert failure is not None and bytes(model.data)==prior
            else:
                assert failure is None and bytes(model.data)==migrated
            assert not model.writable
    report=dict(candidate_sha256=hashlib.sha256(release).hexdigest(),prior_code_sha256=prior_sha,version=VERSION,unsafe_spi_requests_rejected=rejected,legal_page_stubs_verified=len(sectors)*16,
                complete_update_model_passed=True,partial_program_rollback_cases=recovered,
                oversized_erase_detected_and_restored=True,
                protected_prefix_and_current_settings_preserved=True,
                source_sha256={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                               for name in ('spi_flash.py','flash_update.py','verify_flash_update.py')},
                scope='Independent host-side NOR model; actual flash behavior requires hardware trial.')
    (root/'flash-writer-model-validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
