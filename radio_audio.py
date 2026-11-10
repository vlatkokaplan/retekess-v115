"""Bounded source-built DAC startup for the loader-RAM radio prototype.

Each short 8051 program returns to the USB loader. Register addresses and the
calibration order were established from the V115 initialization instructions.
No vendor function calls or flash commands are used.
"""
import hashlib
import time

from lcd_draw import Asm

BASE = 0x2200
RESULT = 0x23C0
XREGS = {0x7809, *range(0x7890, 0x7896)}
SREGS = {0x97, 0x9C, 0xB0, 0xBD, 0xDC}


def program(operations):
    a = Asm(BASE)
    a.push(0xE0)
    a.push(0xA8)
    a.clr(0xAF)
    a.push(0xD0)
    a.anldi(0xD0, 0xE7)
    a.push(3)
    a.push(0x86)
    a.movdi(0x86, 0)
    for reg in (0x82, 0x83, 0x84, 0x85):
        a.push(reg)
    outputs = 0

    def save():
        nonlocal outputs
        a.movdptr(RESULT + outputs)
        a.movxda()
        outputs += 1

    for index, operation in enumerate(operations):
        kind, reg, *args = operation
        if any(not isinstance(value, int) or not 0 <= value <= 255 for value in args):
            raise ValueError('audio register values must be bytes')
        if kind in ('xread', 'xwrite', 'xmodify'):
            if reg not in XREGS:
                raise ValueError('unreviewed audio XDATA register')
            a.movdptr(reg)
            if kind == 'xwrite':
                a.movai(args[0])
            else:
                a.emit(0xE0)
            if kind == 'xmodify':
                a.anlai(args[0])
                a.orlai(args[1])
            if kind != 'xread':
                a.movxda()
        elif kind == 'smodify':
            if reg not in SREGS:
                raise ValueError('unreviewed audio SFR')
            a.anldi(reg, args[0])
            a.orldi(reg, args[1])
            a.movad(reg)
        elif kind in ('atread', 'atwrite'):
            if not 0 <= reg <= 11:
                raise ValueError('unreviewed audio terminal register')
            if kind == 'atwrite':
                a.movdi(0x8C, args[0])
            a.movdi(0xA4, reg | (0xC0 if kind == 'atwrite' else 0x80))
            a.movri(3, 255)
            a.L(f'wait{index}')
            a.movad(0xA4)
            a.movcb(0xE7)
            a.jnc(f'done{index}')
            a.djnz(3, f'wait{index}')
            a.L(f'done{index}')
            save()  # Bit 7 still set means the bounded transfer timed out.
            a.movad(0x8C)
        else:
            raise ValueError('unknown audio operation')
        save()

    for reg in (0x85, 0x84, 0x83, 0x82, 0x86, 3, 0xD0, 0xA8, 0xE0):
        a.pop(reg)
    a.ret()
    code = a.assemble()
    if len(code) > RESULT - BASE or outputs > 64:
        raise ValueError('audio startup program exceeds its RAM allocation')
    return code, outputs


def initialize(transport):
    programs = set()

    def access(*operations):
        code, size = program(operations)
        programs.add(hashlib.sha256(code).hexdigest())
        block = code.ljust(512, b'\0')
        transport.encoded(bytes([0xFB, 3]) + BASE.to_bytes(4, 'big') + bytes(10), outgoing=block)
        if transport.read_ram(BASE, 512) != block:
            raise RuntimeError('audio startup RAM readback mismatch')
        transport.encoded(bytes([0xFB, 2]) + BASE.to_bytes(4, 'big') + bytes(10))
        raw = transport.read_ram(RESULT, 512)[:size]
        values = []
        cursor = 0
        for operation in operations:
            if operation[0] in ('atread', 'atwrite'):
                if raw[cursor] & 0x80:
                    raise RuntimeError('audio terminal transfer timed out')
                cursor += 1
            values.append(raw[cursor])
            cursor += 1
        return values

    def at(*pairs):
        return access(*(('atwrite', reg, value) for reg, value in pairs))

    def wait(reg, mask):
        deadline = time.monotonic() + 2
        while True:
            value = access(('atread', reg))[0]
            if value & mask:
                return value
            if time.monotonic() >= deadline:
                raise RuntimeError(f'DAC calibration timed out at register {reg}')
            time.sleep(0.01)

    # Keep the amplifier muted while the DAC establishes its bias and trims.
    access(('xmodify', 0x7809, 0xF7, 0), ('smodify', 0xBD, 0xDF, 0),
           ('smodify', 0xB0, 255, 0x20), ('xmodify', 0x7891, 255, 0x80),
           ('xwrite', 0x7892, 0xC0), ('xwrite', 0x7890, 0xF8),
           ('smodify', 0xDC, 0x3F, 0x80), ('smodify', 0x9C, 0x7F, 0))
    at((0, 1), (8, 255), (9, 127), (10, 255), (11, 127), (6, 15), (7, 1))
    stages = [wait(7, 2)]
    access(('xwrite', 0x7893, 0xBF))
    time.sleep(0.015)
    access(('xmodify', 0x7890, 255, 2), ('xmodify', 0x7891, 0x7F, 0))
    at((8, 127), (9, 2), (10, 127), (11, 2), (6, 11), (7, 1))
    stages.append(wait(7, 2))
    access(('xmodify', 0x7890, 255, 4))
    time.sleep(0.025)
    at((6, 0x45), (7, 1))
    stages.append(wait(7, 2))
    time.sleep(0.005)
    trims = access(*(('atread', reg) for reg in range(8, 12)))
    access(('xmodify', 0x7892, 0xFD, 0))
    at((5, 6), (4, 0), (3, 0))
    volume = wait(3, 0x80)
    # Leave the calibrated DAC off until app_init selects the radio route.
    access(('xmodify', 0x7890, 0xF7, 0))
    at((0, 0))
    access(('smodify', 0x9C, 255, 0x80))
    return dict(stages=stages, trims=trims, volume_settled=volume,
                program_sha256=sorted(programs))
