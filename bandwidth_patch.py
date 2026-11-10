"""Native FM bandwidth handler used by the full firmware builder."""
from lcd_draw import Asm

HANDLER_PC = 0x1EB0
HANDLER_FLASH = 0x11800 + HANDLER_PC - 0x1800
BW_LABEL = bytes.fromhex('7f49494936 00 3f4038403f')
DIGITS = bytes([155,159,154, 156,154,154, 0,159,154, 155,154,154])


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

    # The stock radio draws using the shared 256-byte line buffer at 0x2800.
    a.movdptr(0x2800); a.movri(2, 0); a.movai(0)
    a.L('clear'); a.movxda(); a.emit(0xA3); a.djnz(2, 'clear')
    a.movri(7, 0)
    a.L('label')
    a.movdptr(0); label_patch = len(a.code) - 2
    a.movar(7); a.emit(0x93, 0xFA)  # MOVC A,@A+DPTR; MOV R2,A
    a.movdptr(0x2814); a.movar(7); a.emit(0x24, 0x14); a.movda(0x82)
    a.movar(2); a.movxda()
    a.incr(7); a.movar(7); a.cjne_ai(len(BW_LABEL), 'label')

    a.movdptr(0x3B3A); a.emit(0xE0); a.anlai(3)
    a.movdi(0xF0, 3); a.emit(0xA4, 0xFE)  # digit-table row = bits*3
    a.movri(7, 0)
    a.L('digit')
    a.movdptr(0); digits_patch = len(a.code) - 2
    a.movar(6); a.emit(0x2F, 0x93); a.jz('next_digit')
    a.emit(0xFA); a.movri(3, 0); a.movri(4, 0)
    a.movar(7); a.emit(0x23,0x23,0x23,0x24,38,0xF9)
    farcall(a, 2, 0x1A04)  # disp_draw(image index R2:R3, x R1, line R4)
    a.L('next_digit')
    a.incr(7); a.movar(7); a.cjne_ai(3, 'digit')
    a.movri(1,64); a.movri(2,21); a.movri(3,0); a.movri(4,0)
    farcall(a,2,0x1A04)  # existing kHz icon
    a.movri(1,3); farcall(a,2,0x1996)  # line_update(3): buffer -> LCD pages 3,4
    a.movri(1,200); a.emit(0x12,0x20,0x54)  # native delay_5ms(200)
    a.movdptr(0x3CD1); a.movai(1); a.movxda()  # native FM screen redraw on return
    for reg in (7,6,5,4,3,2,0xF0): a.pop(reg)
    a.movri(1,0)  # consume long-1; remaining stock dispatch ignores zero
    a.emit(0x02,0x01,0x00)
    label_pc = a.base + len(a.code); a.code.extend(BW_LABEL)
    digits_pc = a.base + len(a.code); a.code.extend(DIGITS)
    a.code[label_patch:label_patch+2] = label_pc.to_bytes(2,'big')
    a.code[digits_patch:digits_patch+2] = digits_pc.to_bytes(2,'big')
    return a.assemble(), {'label_pc':label_pc, 'digits_pc':digits_pc}
