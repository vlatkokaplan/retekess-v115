/* Instrumented board services for instruction tests of the same app.c. */
#include "radio.h"
#define REG ((volatile __xdata u8 *)0x7000)
#define FRAME ((__xdata u8 *)0x8000)
#define SHOWN ((__xdata u8 *)0x8400)
#define KEY (*(volatile __xdata u8 *)0x7040)
#define FAULT (*(volatile __xdata u8 *)0x7041)
#define BOOT_KEY (*(volatile __xdata u8 *)0x7042)

u8 tuner_read(u8 reg) { return FAULT==1?255:REG[reg]; }
void tuner_write(u8 reg,u8 value) {
    u8 previous=REG[reg];
    if(reg>=20) { radio.error|=ERR_I2C; return; }
    REG[reg]=value;
    /* Model a rising tune edge, including the chip's observed FM grid rounding. */
    if(reg==0 && (value&0x20) && !(previous&0x20) && FAULT!=2) {
        u8 channel=REG[3],space=(REG[11]>>4)&3;
        if(value&0x40) channel&=255<<space;
        REG[20]=0x60|(REG[2]&31); REG[21]=channel;
    }
}
void hardware_init(void) { KEY=BOOT_KEY; FAULT=0; REG[20]=0; REG[21]=0; REG[22]=7; REG[23]=0x80|18; }
void hardware_audio(void) {}
void hardware_amplifier(u8 enabled) { REG[31]=enabled; }
u8 hardware_key(void) { radio.adc1=255; radio.adc2=255; return KEY; }
void lcd_power(u8 on) { REG[30]=on; }
void lcd_page(u8 row) {
    u8 x,crc=0;
    for(x=0;x<128;++x) { FRAME[(u16)row*128+x]=page[x]; crc=(crc<<1)|(crc>>7); crc^=page[x]; }
    radio.page_crc[row]=crc;
    if(row==7) for(x=0;x<sizeof(radio);++x) SHOWN[x]=((__xdata u8 *)&radio)[x];
}
