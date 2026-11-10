"""Bounded SPI1 transactions in loader RAM, alongside the verified read helper.

Pins/registers follow spi080_driver 1.00: CS=P1.4, SPI1CON=A2,
SPI1BUF=A3. The stub, payload, results, helper, and loader stack do not overlap.
"""
import hashlib
import time

from lcd_draw import Asm
from usb_dump import ROOT, HELPER_SHA256, RawTransport

CODE, DATA, RESULT = 0x3300, 0x3000, 0x3600
CAPACITY, PROTECTED, SECTOR, PAGE = 0x200000, 0x2000, 0x1000, 0x100
READ_OPS = {0x9F, 0x05, 0x35, 0x15, 0x5A, 0x03}


def transaction_stub(opcode, address=None, outgoing=b'', incoming=0, writable=False):
    allowed=READ_OPS | ({0x06,0x04,0x20,0x02} if writable else set())
    if opcode not in allowed or not 0<=incoming<=256 or len(outgoing)>PAGE:
        raise ValueError('unsupported SPI transaction')
    addressed=opcode in (0x5A,0x03,0x20,0x02)
    if addressed != (address is not None):
        raise ValueError('wrong address format for opcode')
    if address is not None and not 0<=address<CAPACITY:
        raise ValueError('address outside flash')
    if opcode==0x20 and (address<PROTECTED or address%SECTOR or outgoing or incoming):
        raise ValueError('erase intersects protected prefix or is unaligned')
    if opcode==0x02 and (address<PROTECTED or not outgoing or incoming or
                        address+len(outgoing)>CAPACITY or
                        address//PAGE!=(address+len(outgoing)-1)//PAGE):
        raise ValueError('program intersects protected prefix or crosses page')
    if opcode!=0x02 and outgoing:
        raise ValueError('payload is only supported for page program')
    if opcode in READ_OPS and not incoming:
        raise ValueError('read must receive data')
    if opcode in (0x06,0x04) and incoming:
        raise ValueError('write-enable/disable must have no data phase')

    a=Asm(CODE)
    for reg in (0xE0,0xD0,0x82,0x83,0xF0,0xA0):a.push(reg)
    a.anldi(0xD0,0xE7)
    for reg in range(8):a.push(reg)
    a.push(0xA8);a.clr(0xAF)
    a.push(0xA2)
    a.movdptr(0x782E);a.emit(0xE0);a.push(0xE0)
    a.anlai(0xFC);a.movxda()  # raw SPI; output scrambling and CRC disabled
    a.movdptr(RESULT);a.movai(0)
    a.movxda();a.emit(0xA3);a.movxda()
    a.movdi(0xA2,0x11)  # native helper's enabled two-wire, no decryption
    a.clr(0x94)

    # Use explicit labels: branches stay local even with many address bytes.
    def byte(value, index):
        a.movai(value);a.lcall('send');a.jnc('byte_ok'+str(index))
        a.emit(0x02);a._abs('failed');a.L('byte_ok'+str(index))

    command=bytes([opcode])
    if addressed:command+=address.to_bytes(3,'big')
    if opcode==0x5A:command+=b'\0'
    for index,value in enumerate(command):byte(value,index)
    if outgoing:
        a.movdptr(DATA);a.movri(4,len(outgoing)&255)
        a.L('payload');a.emit(0xE0,0xA3);a.lcall('send')
        a.jnc('payload_ok');a.emit(0x02);a._abs('failed')
        a.L('payload_ok');a.djnz(4,'payload')
    if incoming:
        a.movdptr(RESULT+8);a.movri(4,incoming&255)
        a.L('response');a.lcall('receive')
        a.jnc('response_ok');a.emit(0x02);a._abs('failed')
        a.L('response_ok');a.movxda();a.emit(0xA3);a.djnz(4,'response')
    a.sjmp('finish')
    a.L('failed');a.movdptr(RESULT+1);a.movai(1);a.movxda()
    a.L('finish');a.setb(0x94)
    a.movdptr(RESULT);a.movai(0xA5);a.movxda()
    a.movdptr(0x782E);a.pop(0xE0);a.movxda()
    a.pop(0xA2);a.pop(0xA8)
    for reg in (*range(7,-1,-1),0xA0,0xF0,0x83,0x82,0xD0,0xE0):a.pop(reg)
    a.ret()

    a.L('send');a.anldi(0xA2,0xDF);a.movda(0xA3)
    a.sjmp('wait')
    a.L('receive');a.orldi(0xA2,0x20);a.movdi(0xA3,0xFF)
    a.L('wait');a.movri(6,255);a.movri(7,255)
    a.L('poll');a.movad(0xA2);a.movcb(0xE7);a.jnc('busy')
    a.movad(0xA3);a.clr(0xD7);a.ret()
    a.L('busy');a.djnz(7,'poll');a.djnz(6,'poll')
    a.setb(0xD7);a.ret()
    code=a.assemble()
    if len(code)>512:raise ValueError('SPI stub exceeds its RAM allocation')
    return code


class Flash:
    def __init__(self,transport):
        self.t=transport
        self.writable=False

    def initialize(self):
        identity=self.t.identify();self.t.unlock()
        helper=(ROOT/'reference/spi080-driver-decoded.bin').read_bytes()
        if hashlib.sha256(helper).hexdigest()!=HELPER_SHA256:
            raise ValueError('read-helper hash mismatch')
        if 0x2200+len(helper)>DATA:
            raise ValueError('helper overlaps SPI payload buffer')
        self.t.upload_helper(helper)
        return identity

    def upload(self,address,data):
        block=data.ljust(512,b'\0')
        if len(block)!=512:raise ValueError('RAM uploads must fit one block')
        self.t.encoded(bytes([0xFB,3])+address.to_bytes(4,'big')+bytes(10),outgoing=block)
        if self.t.read_ram(address,512)!=block:
            raise RuntimeError(f'RAM verification failed at {address:#x}')

    def command(self,opcode,address=None,outgoing=b'',incoming=0):
        code=transaction_stub(opcode,address,outgoing,incoming,self.writable)
        if outgoing:self.upload(DATA,outgoing)
        self.upload(CODE,code)
        self.t.encoded(bytes([0xFB,2])+CODE.to_bytes(4,'big')+bytes(10))
        result=self.t.read_ram(RESULT,512)
        if result[:2]!=b'\xa5\0':
            raise RuntimeError(f'SPI transaction timed out: {result[:8].hex()}')
        return result[8:8+incoming]

    def status(self):
        return self.command(0x05,incoming=1)[0]

    def wait_ready(self,timeout=5):
        deadline=time.monotonic()+timeout
        while True:
            status=self.status()
            if not status&1:return status
            if time.monotonic()>=deadline:raise TimeoutError('flash remains busy')
            time.sleep(0.01)

    def write_enable(self):
        if not self.writable:raise ValueError('flash writes not enabled')
        if self.wait_ready()&0xFC:raise ValueError('unexpected flash protection/status bits')
        self.command(0x06)
        if self.status()!=2:raise RuntimeError('write-enable latch did not set')

    def write_disable(self):
        self.wait_ready()
        self.command(0x04)
        if self.status()!=0:raise RuntimeError('flash did not return to write-disabled state')

    def erase(self,address):
        # Validate before WREN; never arm a rejected erase operation.
        transaction_stub(0x20,address,writable=self.writable)
        self.write_enable();self.command(0x20,address)
        if self.wait_ready()!=0:raise RuntimeError('erase left unexpected flash status')

    def program(self,address,data):
        transaction_stub(0x02,address,outgoing=data,writable=self.writable)
        self.write_enable();self.command(0x02,address,outgoing=data)
        if self.wait_ready()!=0:raise RuntimeError('program left unexpected flash status')

    def read(self,address,size):
        if address<0 or size<=0 or address+size>CAPACITY or size%512:
            raise ValueError('read must contain whole 512-byte blocks within flash')
        return b''.join(self.t.read_flash(offset,min(4096,address+size-offset))
                        for offset in range(address,address+size,4096))

    def probe(self):
        jedec=self.command(0x9F,incoming=3)
        if jedec in (b'\xff'*3,b'\0'*3):raise RuntimeError('invalid JEDEC response')
        if self.command(0x9F,incoming=3)!=jedec:raise RuntimeError('JEDEC reads disagree')
        sfdp=self.command(0x5A,0,incoming=256)
        return dict(jedec_id=jedec.hex(),status1=self.status(),
                    status2=self.command(0x35,incoming=1)[0],
                    status3=self.command(0x15,incoming=1)[0],sfdp_hex=sfdp.hex(),
                    sfdp_present=sfdp[:4]==b'SFDP')
