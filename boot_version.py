"""Add a build label to the stock welcome bitmap, within its existing payload."""
import hashlib
from PIL import Image

OFFSET, SIZE = 0x11EC54, 1024
ORIGINAL_SHA='595cb3bc9053680fdd7d7c75f3a0600b5d6a95952a1b2db475ed9d36c7819c49'


def patch(plain,version,font):
    data=bytearray(plain[OFFSET:OFFSET+SIZE])
    if hashlib.sha256(data).hexdigest()!=ORIGINAL_SHA:
        raise ValueError('welcome image differs from the verified original')
    for page,text in [(0,'RETEKESS V115'),(6,'CUSTOM FW '+version),(7,'2026-11-10')]:
        if len(text)*6>128:raise ValueError('boot label is too wide')
        data[page*128:(page+1)*128]=bytes(128)
        x=(128-len(text)*6)//2
        for index,ch in enumerate(text):
            glyph=font[(ord(ch)-32)*5:(ord(ch)-31)*5]
            if len(glyph)!=5:raise ValueError('unsupported boot-label character')
            start=page*128+x+index*6
            data[start:start+5]=glyph
    result=bytearray(plain);result[OFFSET:OFFSET+SIZE]=data
    return result,dict(offset=OFFSET,bytes=SIZE,image_index=225,version=version,
                       original_sha256=ORIGINAL_SHA,new_sha256=hashlib.sha256(data).hexdigest())


def preview(raw,path):
    data=raw[OFFSET:OFFSET+SIZE]
    image=Image.new('1',(128,64),1)
    for y in range(64):
        for x in range(128):image.putpixel((x,y),0 if data[y//8*128+x]>>(y%8)&1 else 1)
    rebuilt=bytes(sum((not image.getpixel((x,page*8+bit)))<<bit for bit in range(8))
                  for page in range(8) for x in range(128))
    if rebuilt!=data:raise ValueError('welcome PNG round-trip failed')
    image.resize((768,384),Image.Resampling.NEAREST).save(path)
