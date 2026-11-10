"""Small page-buffer UI assembler shared by the custom radio screens."""
from lcd_draw import Asm
from bandwidth_patch import farcall
import native_features as n


class Screen:
    def __init__(self, base=0x1800, state=n.STATE, big=False):
        self.a=Asm(base);self.state=state;self.big=big
        self.strings={};self.tables={}
        n.save_context(self.a)

    def get(self, field):n.read(self.a,self.state+field)
    def put(self, field, value):n.write(self.a,self.state+field,value)
    def pointer(self, label):self.a.emit(0x90);self.a._abs(label)

    def text(self, value):
        if value not in self.strings:self.strings[value]='str'+str(len(self.strings))
        self.pointer(self.strings[value]);self.a.lcall('text')

    def number(self, field, places=0, word=False):
        a=self.a;self.get(field);a.emit(0xFA)
        if word:a.emit(0xA3,0xE0,0xFB)
        else:a.movri(3,0)
        self.number_regs(places)

    def number_regs(self, places=0):
        self.a.movri(4,places);self.a.lcall('number')

    def clear(self):
        a=self.a;a.movdptr(0x2800);a.movri(0,0);a.movai(0)
        label='clear'+str(len(a.code));a.L(label)
        a.movxda();a.emit(0xA3);a.djnz(0,label)
        self.put(n.CURSOR,0)
        if self.big:self.put(n.FONT_SCALE,0)

    def row(self, offset=128):self.put(n.CURSOR,offset)
    def flush(self, page):
        self.a.movri(1,page);farcall(self.a,2,0x1996)

    def mode(self):
        a=self.a;tag=str(len(a.code));self.get(n.MODE)
        a.cjne_ai(1,'mode_am'+tag);self.text('FM');a.sjmp('mode_done'+tag)
        a.L('mode_am'+tag);a.cjne_ai(2,'mode_sw'+tag);self.text('MW');a.sjmp('mode_done'+tag)
        a.L('mode_sw'+tag);self.text('SW');a.L('mode_done'+tag)

    def frequency(self, field=n.FREQ, units=True):
        a=self.a;tag=str(len(a.code));self.get(n.MODE)
        a.cjne_ai(1,'freq_am'+tag);self.number(field,1,True)
        if units:self.text('MHZ')
        a.sjmp('freq_done'+tag);a.L('freq_am'+tag);self.number(field,0,True)
        if units:self.text('KHZ')
        a.L('freq_done'+tag)

    def finish(self):
        a=self.a;n.restore_context(a);a.emit(0x02,0x01,0x00)
        a.L('number');a.movri(7,0)
        for exponent in range(4,-1,-1):
            divisor=10**exponent;name='digit'+str(exponent);a.movri(6,0);a.L(name)
            a.movar(2);a.emit(0xC3,0x94,divisor&255,0xFD);a.movar(3);a.emit(0x94,divisor>>8)
            a.jnc(name+'_sub');a.sjmp(name+'_ready');a.L(name+'_sub')
            a.emit(0xFB);a.movar(5);a.emit(0xFA);a.incr(6);a.sjmp(name)
            a.L(name+'_ready');a.movar(7);n.jnz(a,name+'_emit');a.movar(6);n.jnz(a,name+'_emit')
            a.movar(4);a.emit(0xC3,0x94,exponent);a.jnc(name+'_emit');a.sjmp(name+'_next')
            a.L(name+'_emit');a.movri(7,1);a.movar(6);a.emit(0x24,ord('0'));a.lcall('char')
            if exponent:
                a.movar(4);a.cjne_ai(exponent,name+'_next');a.movai(ord('.'));a.lcall('char')
            a.L(name+'_next')
        a.ret()
        a.L('text');a.movai(0);a.emit(0x93);a.jz('text_done')
        a.lcall('char');a.emit(0xA3);a.sjmp('text');a.L('text_done');a.ret()
        a.L('char')
        for reg in (0x82,0x83,0xF0,*range(8)):a.push(reg)
        a.emit(0x24,224);a.movdi(0xF0,5);a.emit(0xA4)
        self.pointer('font');a.emit(0x25,0x82,0xF5,0x82);a.movad(0xF0);a.emit(0x35,0x83,0xF5,0x83)
        a.movri(5,0)
        if self.big:
            a.push(0x82);a.push(0x83);self.get(n.FONT_SCALE);a.pop(0x83);a.pop(0x82);n.jnz(a,'big_column')
        a.L('char_column');a.movai(0);a.emit(0x93,0xFC,0xA3)
        a.push(0x82);a.push(0x83);self.get(n.CURSOR);a.movda(0x82);a.movdi(0x83,0x28)
        a.movar(4);a.movxda();self.get(n.CURSOR);a.emit(0x04);a.movxda()
        a.pop(0x83);a.pop(0x82);a.incr(5);a.movar(5);a.cjne_ai(5,'char_column')
        self.get(n.CURSOR);a.emit(0x04);a.movxda()
        if self.big:
            n.jump(a,'char_done');a.L('big_column')
            a.movai(0);a.emit(0x93,0xFC,0xA3);a.push(0x82);a.push(0x83)
            self.pointer('stretch');a.movar(4);a.anlai(15);a.emit(0x93,0xFE)
            a.movar(4);a.emit(0xC4);a.anlai(15);a.emit(0x93,0xFF)
            for offset,reg in [(0,6),(1,6),(128,7),(129,7)]:
                self.get(n.CURSOR);a.emit(0x24,offset,0xF5,0x82);a.movdi(0x83,0x28);a.movar(reg);a.movxda()
            self.get(n.CURSOR);a.emit(0x24,2);a.movxda();a.pop(0x83);a.pop(0x82)
            a.incr(5);a.movar(5);a.cjne_ai(5,'big_column')
            self.get(n.CURSOR);a.emit(0x24,2);a.movxda()
            a.L('char_done')
        for reg in (*range(7,-1,-1),0xF0,0x83,0x82):a.pop(reg)
        a.ret()
        a.L('font');a.code.extend(n.font_bytes())
        if self.big:
            a.L('stretch');a.code.extend(bytes(sum(((value>>bit)&1)*3<<(bit*2) for bit in range(4)) for value in range(16)))
        for name,data in self.tables.items():a.L(name);a.code.extend(data)
        for value,label in self.strings.items():a.L(label);a.code.extend(value.encode('ascii')+b'\0')
        code=a.assemble()
        if len(code)>0x800:raise ValueError(f'UI bank exceeds 2 KiB: {len(code)}')
        return code,a.labels
