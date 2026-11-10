"""Shared uCsim runner and bounded XRAM-copy emitter for native-code tests."""
from pathlib import Path
import re
import subprocess
import tempfile
from analyze import intel_hex
from env_tools import executable


def copy_xram(a, source, dest, length, label):
    if not 0<length<=0xFFFF:raise ValueError('invalid copy length')
    a.movri(6,source&255);a.movri(7,source>>8)
    a.movri(4,dest&255);a.movri(5,dest>>8)
    a.movri(2,length&255);a.movri(3,(length+255)//256)
    a.L(label)
    a.emit(0x8E,0x82,0x8F,0x83,0xE0,0xF8,0xA3,0xAE,0x82,0xAF,0x83)
    a.emit(0x8C,0x82,0x8D,0x83,0xE8,0xF0,0xA3,0xAC,0x82,0xAD,0x83)
    a.djnz(2,label);a.djnz(3,label)


def run(chunks,start,stop,address,length,timeout=30):
    with tempfile.TemporaryDirectory(prefix='v115-emulation-') as directory:
        path=Path(directory)/'run.hex';path.write_text(intel_hex(chunks))
        result=subprocess.run([executable('s51'),'-b','-q','-t','C52','-c','-'],
            input=f'file "{path}"\nrun {start:#x} {stop:#x}\ndump /h xram {address:#x} {address+length-1:#x} 32\nquit\n',
            text=True,capture_output=True,timeout=timeout,check=True)
    data={}
    for line in result.stdout.splitlines():
        match=re.match(r'^0x([0-9a-fA-F]+)\s+((?:[0-9a-fA-F]{2}(?:\s+|$))+)',line)
        if match:
            for offset,value in enumerate(bytes.fromhex(match[2])):data[int(match[1],16)+offset]=value
    if any(p not in data for p in range(address,address+length)):
        raise ValueError('incomplete emulator output: '+result.stdout[-1000:])
    return bytes(data[p] for p in range(address,address+length))


def verify_image_banks(candidate,banks):
    """Bind standalone instruction tests to their exact emitted image banks."""
    if candidate is None:return None
    import hashlib
    from band_patch import decode
    raw=Path(candidate).read_bytes();plain=decode(raw)
    for bank,assembler in banks:
        code=assembler()[0];start=0x3000+bank*0x800
        if plain[start:start+len(code)]!=code:raise ValueError(f'image bank {bank:#x} differs from the tested implementation')
    return hashlib.sha256(raw).hexdigest()
