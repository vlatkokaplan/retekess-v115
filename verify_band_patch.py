#!/usr/bin/env python3
"""Run the actual V115 band getter/clamp instructions in the uCsim 8051 emulator.

Only the normal firmware's bank-return trampoline at 0x0100 is replaced by RET.
The LCD/audio side-effect branch is disabled through its existing state flag.
No radio or USB access. Requires uCsim s51 (sdcc-ucsim) on PATH, or set S51.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
from env_tools import executable
import tempfile

from analyze import intel_hex
from band_patch import ROOT, decode, verify_crcs
from lcd_draw import Asm


CONFIGS = [
    ('FM, normal flag', 1, 0, 0, 300, 2300, 1),
    ('FM, extended flag', 1, 1, 0, 300, 2300, 1),
    ('MW 9 kHz', 2, 0, 0, 522, 1710, 9),
    ('MW 10 kHz', 2, 0, 1, 520, 1710, 10),
    ('SW/AM 5 kHz', 3, 0, 0, 150, 30000, 5),
]


def initialize(a, mode, fmflag, mwflag):
    a.movdi(0x81, 0x60)  # stack away from register bank and mode byte
    a.movdi(0xD0, 0)
    a.movdi(0x18, mode)
    for address, value in [(0x8A, fmflag), (0x8B, mwflag)]:
        a.movri(0, address); a.emit(0x76, value)
    a.movdptr(0x3B7C); a.movai(0); a.movxda()


def frequency(a, value):
    a.movdptr(0x3CDC)
    a.movai(value & 255); a.movxda(); a.emit(0xA3)
    a.movai(value >> 8); a.movxda()


def record_frequency(a, output):
    a.movdptr(0x3CDC)
    a.emit(0xE0, 0xFE, 0xA3, 0xE0, 0xFF)  # read current frequency into R6:R7
    a.movdptr(output)
    a.movar(6); a.movxda(); a.emit(0xA3)
    a.movar(7); a.movxda()


def emulate(bank, a, count):
    stop = a.base + len(a.code)
    a.sjmp('halt'); a.L('halt'); a.sjmp('halt')
    code = a.assemble()
    if a.base + len(code) > 65536:
        raise ValueError('Emulator wrapper exceeded code space')
    with tempfile.TemporaryDirectory(prefix='v115-band-verify-') as directory:
        path = Path(directory) / 'run.hex'
        path.write_text(intel_hex([(0x0100, b'\x22'), (0x1800, bank), (a.base, code)]))
        commands = (f'file "{path}"\nrun {a.base:#x} {stop:#x}\n'
                    f'dump /h xram 0x8000 {0x8000 + count * 2 - 1:#x} 32\nquit\n')
        run = subprocess.run([executable('s51'), '-b', '-q', '-t', 'C52', '-c', '-'],
                             input=commands, text=True, capture_output=True, timeout=30, check=True)
    if f'PC=0x{stop:06x}' not in run.stdout and f'0x{stop:04x}' not in run.stdout:
        raise ValueError('Emulator did not reach the expected stop address: ' + run.stdout[-1500:])
    data = bytearray(count * 2)
    seen = set()
    for line in run.stdout.splitlines():
        match = re.match(r'^0x([0-9a-fA-F]+)\s+((?:[0-9a-fA-F]{2}(?:\s+|$))+)', line)
        if not match:
            continue
        offset = int(match[1], 16) - 0x8000
        if not 0 <= offset < len(data):
            continue
        row = bytes.fromhex(match[2])[:min(32, len(data) - offset)]
        data[offset:offset + len(row)] = row
        seen.update(range(offset, offset + len(row)))
    if len(seen) != len(data):
        raise ValueError(f'Incomplete emulator output: {len(seen)} != {len(data)}; ' + run.stdout[-1500:])
    return list(struct.unpack('<' + 'H' * count, data))


def verify(plain):
    clamp_bank = plain[0x11800:0x12000]
    ui_bank = plain[0xC000:0xC800]
    boundary_count, grid_count, getter_count = 0, 0, 0
    for name, mode, fmflag, mwflag, lower, upper, step in CONFIGS:
        a = Asm(0x8000)
        initialize(a, mode, fmflag, mwflag)
        a.emit(0x12, 0x18, 0x74)  # actual band_lower_limit
        a.movdptr(0x8000); a.movar(2); a.movxda(); a.emit(0xA3); a.movar(3); a.movxda()
        a.emit(0x12, 0x18, 0xA5)  # actual band_upper_limit
        a.movdptr(0x8002); a.movar(2); a.movxda(); a.emit(0xA3); a.movar(3); a.movxda()
        found = emulate(ui_bank, a, 2)
        if found != [lower, upper]:
            raise ValueError(f'{name}: UI bounds {found} != {[lower, upper]}')
        getter_count += 2

        cases, expected = [], []
        a = Asm(0x8000)
        initialize(a, mode, fmflag, mwflag)
        values = sorted({0, lower - 1, lower, lower + step, (lower + upper) // 2,
                         upper - step, upper, upper + 1, 65535})
        for clamp in (False, True):
            for value in values:
                frequency(a, value)
                a.movdi(0xF0, int(clamp))
                a.emit(0x12, 0x18, 0x00)
                record_frequency(a, 0x8000 + len(cases) * 2)
                cases.append((clamp, value))
                expected.append((lower if clamp else upper) if value < lower else
                                (upper if clamp else lower) if value > upper else value)
        found = emulate(clamp_bank, a, len(cases))
        for case, want, actual in zip(cases, expected, found):
            if actual != want:
                raise ValueError(f'{name}: clamp/value {case}, got {actual}, expected {want}')
        boundary_count += len(cases)

        # Execute the actual clamp routine at every frequency on the requested grid.
        a = Asm(0x8000)
        initialize(a, mode, fmflag, mwflag)
        a.movdi(0xF0, 1)
        for address, value in [(0x20, lower & 255), (0x21, lower >> 8), (0x22, 0), (0x23, 0x80)]:
            a.movdi(address, value)
        a.L('loop')
        a.movdptr(0x3CDC); a.movad(0x20); a.movxda(); a.emit(0xA3)
        a.movad(0x21); a.movxda()
        a.emit(0x12, 0x18, 0x00)
        a.movdptr(0x3CDC); a.emit(0xE0, 0xFE, 0xA3, 0xE0, 0xFF)
        a.emit(0x85, 0x22, 0x82, 0x85, 0x23, 0x83)
        a.movar(6); a.movxda(); a.emit(0xA3); a.movar(7); a.movxda(); a.emit(0xA3)
        a.emit(0x85, 0x82, 0x22, 0x85, 0x83, 0x23)
        a.movad(0x20); a.emit(0x24, step); a.movda(0x20)
        a.movad(0x21); a.emit(0x34, 0); a.movda(0x21)
        a.movad(0x20); a.cjne_ai((upper + step) & 255, 'loop')
        a.movad(0x21); a.cjne_ai((upper + step) >> 8, 'loop')
        expected = list(range(lower, upper + 1, step))
        found = emulate(clamp_bank, a, len(expected))
        if found != expected:
            raise ValueError(f'{name}: a requested grid frequency was clamped unexpectedly')
        grid_count += len(expected)
        print(f'{name}: bounds {lower}..{upper}; {len(cases)} boundary checks; '
              f'{len(expected)} grid frequencies passed', flush=True)
    return {'emulator': 'uCsim s51 0.6.4, C52 instruction set',
            'ui_getter_results_verified': getter_count,
            'clamp_wrap_boundary_cases_passed': boundary_count,
            'grid_frequencies_passed': grid_count,
            'scope': 'Actual firmware getter/clamp instructions. RF reception, I2C, '
                     'LCD/UI integration and analog antenna paths are not emulated.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidate', type=Path)
    args = parser.parse_args()
    raw = args.candidate.read_bytes()
    plain = decode(raw)
    checks = verify_crcs(plain)
    result = verify(plain)
    result.update({'candidate': str(args.candidate.resolve()),
                   'candidate_sha256': hashlib.sha256(raw).hexdigest(), 'code_crcs': checks})
    path = args.candidate.with_suffix('.emulation.json')
    path.write_text(json.dumps(result, indent=2) + '\n')
    print('Emulation evidence:', path)


if __name__ == '__main__':
    main()
