"""Native Timer1 startup and bounded radio batches within verified loader RAM."""
from lcd_draw import Asm
from run_radio import wrapper

SNAPSHOT=0x23E0


def configuration(saved=None):
    a=Asm(0)
    if saved is None:
        for i,reg in enumerate([0xDC,0xDB,0x9C,0xF7]):
            a.movad(reg);a.movdptr(SNAPSHOT+i);a.movxda()
        for i,address in enumerate([0x785A,0x785B,0x7850,0x7851]):
            a.movdptr(address);a.emit(0xE0);a.movdptr(SNAPSHOT+4+i);a.movxda()
        a.movdptr(0x785A);a.emit(0xE0);a.anlai(0x30);a.jz('configure');a.sjmp('done')
        a.L('configure')
        a.anldi(0xDC,0xF0);a.anldi(0xDB,0xEC);a.orldi(0xDB,2);a.anldi(0x9C,0xEF)
        pairs=[(0x785A,0),(0x785B,4),(0x7850,0),(0x7851,0),(0x7852,0x5F),(0x7853,0xEA),(0x785A,0x14)]
    else:
        if len(saved)!=8 or saved[4]&0x30:raise ValueError('Timer1 must have been inactive before the preview')
        pairs=[(0x785A,0),(0x785B,saved[5]),(0x7850,saved[6]),(0x7851,saved[7]),(0x785A,saved[4])]
    for address,value in pairs:a.movdptr(address);a.movai(value);a.movxda()
    if saved is not None:
        for reg,mask,value in [(0xDC,0xF0,saved[0]&15),(0xDB,0xEC,saved[1]&0x13),(0x9C,0xEF,saved[2]&16)]:
            a.anldi(reg,mask);a.orldi(reg,value)
    a.L('done')
    dummy=0xFFF0;call=bytes([0x12,255,0xF0])
    code=wrapper(dummy,0).replace(call,a.assemble())
    if len(code)>SNAPSHOT-0x2200:raise ValueError('timer configuration exceeds reserved RAM')
    return code


def batch(manifest):
    entry=manifest['entries']['app_tick']
    counter=manifest['state_address']+manifest['fields']['clock_ticks']
    a=Asm(0)
    a.push(0xF7);a.movdi(0xF7,0x3C)
    a.L('wait');a.movdptr(0x785B);a.emit(0xE0,0x30,0xE7);a._rel('wait')
    a.movai(4);a.movxda();a.orldi(0xF7,0x20)
    a.emit(0x12,entry>>8,entry&255)
    a.movdptr(counter);a.emit(0xE0,0x04,0xF0,0x70);a._rel('wait')
    a.emit(0xA3,0xE0,0x04,0xF0)
    a.pop(0xF7)
    code=wrapper(entry,manifest['internal_data_bytes'])
    code=code.replace(bytes([0x12,entry>>8,entry&255]),a.assemble())
    if len(code)>128:raise ValueError('timer batch exceeds the loader wrapper reservation')
    return code
