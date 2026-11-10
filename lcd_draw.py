#!/usr/bin/env python3
"""Draw on the V115 LCD via the HARDWARE SPI0 engine, from a loader RAM stub.

Ground truth from firmware SPI0 send routine (flash ~0x4040) + linker map:
  SPI0BUF = SFR 0xD8, SPI0CON = SFR 0xD9 (set to 0x15 = enable+master).
  send byte:  MOV SPI0BUF,A ; then poll: MOV A,SPI0CON ; MOV C,ACC.7(PND) ; JNC $
  A0 = P3.4 (CLR=command, SETB=data), CS = P3.1 (active low), RES = P3.0 (reset pulse).
  LCD data/clk ride on the SPI0 peripheral pins (P2.5/P2.6, output-enabled via P2DIR).
Also replicate firmware: XDATA 0x784E bit 4 routes SPI0 DOUT onto P2.5;
XDATA 0x7823 = 1 selects the firmware's SPI0 transfer configuration.

The program saves/restores all CPU context so the loader survives, then:
port-init -> reset pulse -> SPI0 init -> ST7565 init -> fill 8 pages x 128 cols.
"""
import argparse, sys
from pathlib import Path
from usb_dump import RawTransport

class Asm:
    def __init__(self, base): self.base=base; self.code=bytearray(); self.labels={}; self.fix=[]
    def L(self,n): self.labels[n]=self.base+len(self.code)
    def emit(self,*bs): self.code+=bytes(bs)
    def _rel(self,n): self.fix.append((len(self.code),n,self.base+len(self.code)+1)); self.code.append(0)
    def _abs(self,n): self.fix.append(('abs',len(self.code),n)); self.code+=b'\x00\x00'
    def setb(self,b): self.emit(0xD2,b)
    def clr(self,b):  self.emit(0xC2,b)
    def movai(self,i):self.emit(0x74,i)            # MOV A,#imm
    def movri(self,r,i):self.emit(0x78+r,i)        # MOV Rn,#imm
    def movar(self,r):self.emit(0xE8+r)            # MOV A,Rn
    def orlai(self,i):self.emit(0x44,i)            # ORL A,#imm
    def anlai(self,i):self.emit(0x54,i)            # ANL A,#imm
    def anldi(self,d,i):self.emit(0x53,d,i)        # ANL dir,#imm
    def orldi(self,d,i):self.emit(0x43,d,i)        # ORL dir,#imm
    def movdi(self,d,i):self.emit(0x75,d,i)        # MOV dir,#imm
    def movda(self,d):self.emit(0xF5,d)            # MOV dir,A
    def movad(self,d):self.emit(0xE5,d)            # MOV A,dir
    def movcb(self,b):self.emit(0xA2,b)            # MOV C,bit
    def movdptr(self,a16):self.emit(0x90,(a16>>8)&0xff,a16&0xff)  # MOV DPTR,#imm16
    def movxda(self): self.emit(0xF0)              # MOVX @DPTR,A
    def incr(self,r): self.emit(0x08+r)            # INC Rn
    def ret(self):    self.emit(0x22)
    def push(self,d): self.emit(0xC0,d)
    def pop(self,d):  self.emit(0xD0,d)
    def jz(self,n):   self.emit(0x60); self._rel(n)
    def jnc(self,n):  self.emit(0x50); self._rel(n)
    def sjmp(self,n): self.emit(0x80); self._rel(n)
    def djnz(self,r,n):self.emit(0xD8+r); self._rel(n)
    def cjne_ai(self,i,n):self.emit(0xB4,i); self._rel(n)
    def lcall(self,n):self.emit(0x12); self._abs(n)
    def assemble(self):
        for item in self.fix:
            if item[0]=='abs':
                _,pos,name=item; tgt=self.labels[name]
                self.code[pos]=(tgt>>8)&0xff; self.code[pos+1]=tgt&0xff
            else:
                pos,name,nxt=item
                delta=self.labels[name]-nxt
                if not -128 <= delta <= 127:
                    raise ValueError(f"Relative branch to {name} is out of range: {delta}")
                self.code[pos]=delta&0xff
        return bytes(self.code)

# bit addresses / SFRs
P3_0=0xB0; A0=0xB4; CS=0xB1            # confirmed by lcd_send_cmd and lcd_update @ flash 0x407e/0x402b
P2DIR=0xBC; P3DIR=0xBD
SPI0BUF=0xD8; SPI0CON=0xD9; PND=0xE7   # PND = ACC.7 bit addr after MOV A,SPI0CON

FRAMEBUFFER_ADDR=0x2800

def build(pattern, contrast=0):
    if not 0 <= contrast <= 63:
        raise ValueError("contrast must be 0..63")
    a=Asm(0x2200)
    # Save the active PSW, then use bank 0 and preserve that bank's registers.
    for d in (0xE0,0xD0,0x82,0x83,0xF0,0xA0): a.push(d)
    a.anldi(0xD0,0xE7)
    for d in range(8): a.push(d)
    # P1.5 backlight, active low, matching the working backlight stub.
    a.anldi(0xBB,0xDF); a.clr(0x95)
    # port init (from firmware 0xd101)
    a.movdptr(0x784E); a.emit(0xE0); a.orlai(0x10); a.movxda()  # PMUXCON0
    a.setb(0xA6)                # P2.6 clock high, matching firmware
    a.orldi(0xB0,0x13)           # P3: RES,A0,CS high (bits 0,1,4)
    a.anldi(P2DIR,0x9F)          # P2.5,P2.6 -> output (SPI0 data/clk)
    a.anldi(P3DIR,0xEC)          # P3.0,P3.1,P3.4 -> output
    # SPI0 enable (firmware: SPI0CON=0x15, XDATA 0x7823=1)
    a.movai(0x01); a.movdptr(0x7823); a.movxda()
    a.movdi(SPI0CON,0x15)
    # reset pulse on P3.0
    a.setb(P3_0); a.lcall('DLY')
    a.clr(P3_0);  a.lcall('DLY')
    a.setb(P3_0); a.lcall('DLY')
    # ST7565 init (A0 low = command)
    a.clr(A0)
    for cmd in (0xAE,0xA2,0xA0,0xC8,0x26,0xA8,0xA6):  # actual V115 init @ flash d139
        a.movai(cmd); a.lcall('SEND')
    a.movai(0x81); a.lcall('SEND'); a.movai(contrast); a.lcall('SEND')
    a.movai(0x2F); a.lcall('SEND'); a.lcall('DLY')  # regulator, follower, booster
    a.movai(0xA4); a.lcall('SEND')               # resume display RAM after A5
    a.movai(0x40); a.lcall('SEND')               # start line 0
    a.movai(0xAF); a.lcall('SEND')               # display ON
    if pattern=='allpoints':
        a.movai(0xA5); a.lcall('SEND'); a.sjmp('DONE')
    # fill 8 pages x 128 cols
    if pattern=='framebuffer':
        a.movdptr(FRAMEBUFFER_ADDR)
    a.movri(4,0)                 # R4 = page
    a.L('PAGE')
    a.clr(A0)
    a.movar(4); a.orlai(0xB0); a.lcall('SEND')   # set page
    a.movai(0x10); a.lcall('SEND')               # col hi nibble 0
    a.movai(0x00); a.lcall('SEND')               # col lo nibble 0
    a.setb(A0)                   # data
    a.movri(5,128); a.movri(6,0) # R5=cols, R6=col idx
    a.L('COL')
    if pattern=='framebuffer':
        a.emit(0xE0,0xA3)        # MOVX A,@DPTR ; INC DPTR
    elif pattern=='vbars':
        a.movar(6); a.anlai(0x08); a.jz('ZERO'); a.movai(0xFF); a.sjmp('WR'); a.L('ZERO'); a.movai(0x00); a.L('WR')
    elif pattern=='hstripe':
        a.movai(0xF0)
    elif pattern=='checker':
        a.movar(6); a.anlai(0x08); a.jz('CA'); a.movai(0x0F); a.sjmp('WR'); a.L('CA'); a.movai(0xF0); a.L('WR')
    elif pattern=='half':     # left 64 cols ON (0xFF), right 64 OFF
        a.movar(6); a.anlai(0x40); a.jz('HON'); a.movai(0x00); a.sjmp('WR'); a.L('HON'); a.movai(0xFF); a.L('WR')
    elif pattern=='clear':
        a.movai(0x00)
    else:
        a.movai(0xFF)
    a.lcall('SEND')
    a.incr(6); a.djnz(5,'COL')
    a.incr(4); a.movar(4); a.cjne_ai(8,'PAGE')
    a.L('DONE')
    a.setb(CS)                   # deselect
    for d in (7,6,5,4,3,2,1,0,0xA0,0xF0,0x83,0x82,0xD0,0xE0): a.pop(d)
    a.ret()
    # SEND: byte in ACC -> SPI0, wait for PND
    a.L('SEND')
    a.clr(CS)
    a.movda(SPI0BUF)             # SPI0BUF = A  (kick transfer)
    a.L('SP'); a.movad(SPI0CON); a.movcb(PND); a.jnc('SP')   # wait PND=1
    a.setb(CS)
    a.ret()
    # DLY
    a.L('DLY')
    a.push(2); a.push(3)
    a.movri(2,0x40)
    a.L('D1'); a.movri(3,0xFF)
    a.L('D2'); a.djnz(3,'D2')
    a.djnz(2,'D1')
    a.pop(3); a.pop(2); a.ret()
    return a.assemble()

def main():
    ap=argparse.ArgumentParser()
    source=ap.add_mutually_exclusive_group()
    source.add_argument("--pattern",choices=["vbars","hstripe","checker","half","clear","on","allpoints"],default="checker")
    source.add_argument("--framebuffer",type=Path,help="1024 raw LCD page bytes (128x64, bit 0 at top)")
    source.add_argument("--image-index",type=int,help="draw an image from the local firmware backup")
    ap.add_argument("--firmware",type=Path,default=Path(__file__).resolve().parent/'stock.bin')
    ap.add_argument("--dry",action="store_true")
    ap.add_argument("--contrast",type=int,choices=range(64),default=0,metavar="0..63")
    args=ap.parse_args()
    framebuffer=None
    if args.framebuffer:
        framebuffer=args.framebuffer.read_bytes()
        if len(framebuffer)!=1024: ap.error("framebuffer must contain exactly 1024 bytes")
    elif args.image_index is not None:
        from extract_images import load_resources
        assets,_=load_resources(args.firmware)
        icons=[a for a in assets if a['kind']=='image']
        if not 0<=args.image_index<len(icons): ap.error("image index is outside the firmware table")
        icon=icons[args.image_index]['lines'][0]
        w,h,raw=icon['width'],icon['height'],icon['data']
        framebuffer=bytearray(1024)
        x=(128-w)//2; page=(8-h//8)//2
        for row in range(h//8):
            framebuffer[(page+row)*128+x:(page+row)*128+x+w]=raw[row*w:(row+1)*w]
        framebuffer=bytes(framebuffer)
    pattern='framebuffer' if framebuffer is not None else args.pattern
    code=build(pattern, args.contrast)
    print(f"program: {len(code)} bytes (pattern={pattern}, contrast={args.contrast})")
    if args.dry: print(code.hex(' '))
    if args.dry: return
    if len(code)>512: sys.exit("program >512B")
    t=RawTransport(); import atexit; atexit.register(t.close)
    print("INQUIRY:",t.identify()); t.unlock()
    if framebuffer is not None:
        for offset in range(0,1024,512):
            t.encoded(bytes([0xFB,3])+(FRAMEBUFFER_ADDR+offset).to_bytes(4,"big")+bytes(10),outgoing=framebuffer[offset:offset+512])
            if t.read_ram(FRAMEBUFFER_ADDR+offset,512)!=framebuffer[offset:offset+512]:
                raise RuntimeError("Framebuffer RAM verification failed")
    blk=code+b"\x00"*(512-len(code))
    t.encoded(bytes([0xFB,3])+(0x2200).to_bytes(4,"big")+bytes(10),outgoing=blk)
    if t.read_ram(0x2200,512)!=blk:
        raise RuntimeError("Stub RAM verification failed")
    t.encoded(bytes([0xFB,2])+(0x2200).to_bytes(4,"big")+bytes(10))
    print("ran. loader alive:",t.identify())

if __name__=="__main__":
    try: main()
    except (OSError,RuntimeError,ValueError,PermissionError) as e:
        print("Stopped:",e,file=sys.stderr); sys.exit(1)
