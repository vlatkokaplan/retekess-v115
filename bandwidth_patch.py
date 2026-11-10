"""Native FM bandwidth handler used by the full firmware builder."""
from lcd_draw import Asm

HANDLER_PC = 0x1EB0
HANDLER_FLASH = 0x11800 + HANDLER_PC - 0x1800


def farcall(a, bank, pc):
    a.movdptr(pc); a.movai(bank); a.emit(0x12, 0x01, 0x33)


def handler():
    a = Asm(HANDLER_PC)
    a.movri(0, 0x18); a.emit(0xE6)
    a.cjne_ai(1, 'reject')  # AM/SW retain their original behavior
    a.movdptr(0x3CD0); a.emit(0xE0)
    a.emit(0x70); a._rel('reject')  # do not change the filter during seeking
    a.sjmp('body')
    a.L('reject'); a.emit(0x02, 0x01, 0x00)  # normal IAR bank return
    a.L('body')
    for reg in (0xF0, 2, 3, 4, 5, 6, 7): a.push(reg)

    # Shadow is the state: no new unallocated RAM or persistent-flash setting.
    a.movdptr(0x3B3A); a.emit(0xE0, 0xFA)  # original reg7 in R2
    a.anlai(3); a.emit(0x14); a.anlai(3); a.emit(0xFE)
    a.movar(2); a.anlai(0xFC); a.emit(0x4E); a.movxda(); a.emit(0xFA)
    # Match the native IAR caller layout for tuner_write_register(7, value, 1).
    a.movai(1); a.push(0xE0)
    a.movri(1, 7); a.movri(3, 0); a.movri(4, 0); a.movri(5, 0)
    farcall(a, 0x1D, 0x1D17)
    a.pop(0xE0)

    # The custom home/meter screen shows the new value on the next draw.
    a.movdptr(0x3CD1);a.movai(1);a.movxda()
    for reg in (7,6,5,4,3,2,0xF0): a.pop(reg)
    a.movri(1,0)  # consume long-1; remaining stock dispatch ignores zero
    a.emit(0x02,0x01,0x00)
    return a.assemble(), {'handler_pc':HANDLER_PC,'popup':False}
