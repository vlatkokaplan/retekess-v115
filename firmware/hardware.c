/* Direct AX2220 GPIO/SPI/ADC/audio and AKC6955 drivers. No stock ABI calls. */
#include "radio.h"

#define X8(a) (*(volatile __xdata u8 *)(a))
__sfr __at(0x90) P1;
__sfr __at(0xA0) P2;
__sbit __at(0xA2) POWER_SWITCH;
__sfr __at(0xB0) P3;
__sfr __at(0xBA) P0DIR;
__sfr __at(0xBB) P1DIR;
__sfr __at(0xBC) P2DIR;
__sfr __at(0xBD) P3DIR;
__sfr __at(0xFD) UARTCON;
__sfr __at(0xD2) ADCCON;
__sfr __at(0xDA) ADCMODE;
__sfr __at(0xD3) PCON2;
__sfr __at(0xD5) ADCDATA;
__sfr __at(0xD8) SPIBUF;
__sfr __at(0xD9) SPICON;
__sfr __at(0xDC) CLKCON1;
__sfr __at(0x9C) PCON1;
__sfr __at(0x97) PCON3;
__sfr __at(0x8C) ATDAT;
__sfr __at(0xA4) ATADR;

static void delay(u16 count) { volatile u16 n=count; while(n) --n; }
static void edge_delay(void) { volatile u8 n=15; while(n) --n; }
static void clock_high(void) { edge_delay(); P1|=0x40; edge_delay(); }
static void clock_pulse(void) { clock_high(); P1&=0xBF; }
static void start(void) {
    P1DIR&=0x7F; P1|=0x80; clock_high(); P1&=0x7F;
    edge_delay(); P1&=0xBF;
}
static void stop(void) {
    P1DIR&=0x7F; P1&=0x7F; clock_high(); P1|=0x80; edge_delay();
}
static u8 send(u8 value) {
    u8 bits=8, ack;
    P1DIR&=0x7F;
    do {
        if(value&0x80) P1|=0x80; else P1&=0x7F;
        clock_pulse(); value<<=1;
    } while(--bits);
    P1|=0x80; P1DIR|=0x80; clock_high();
    ack=P1&0x80; P1&=0xBF;
    if(ack) radio.error|=ERR_I2C;
    return ack;
}
static u8 receive(void) {
    u8 value=0,bits=8;
    P1|=0x80; P1DIR|=0x80;
    do { clock_high(); value<<=1; if(P1&0x80) value|=1; P1&=0xBF; } while(--bits);
    /* NACK this single byte, then stop. */
    P1DIR&=0x7F; P1|=0x80; clock_pulse();
    return value;
}
static u8 old_uart, old_dir, old_mux;
static void bus_begin(void) {
    old_uart=UARTCON; old_dir=P1DIR; old_mux=X8(0x7841);
    UARTCON&=0xEF; X8(0x7841)|=0xC0; P1DIR&=0x3F;
    P1|=0xC0; start();
}
static void bus_end(void) { stop(); X8(0x7841)=old_mux; P1DIR=old_dir; UARTCON=old_uart; }
u8 tuner_read(u8 reg) {
    u8 value=0xFF;
    bus_begin();
    if(!send(0x20) && !send(reg)) {
        start(); if(!send(0x21)) value=receive();
    }
    bus_end(); return value;
}
void tuner_write(u8 reg,u8 value) {
    if(reg>=20) { radio.error|=ERR_I2C; return; }
    bus_begin(); if(!send(0x20) && !send(reg)) send(value); bus_end();
    /* Allow the tuner to latch each write before the next register change. */
    delay(3000);
}

static void lcd_byte(u8 value) {
    u16 timeout=4000;
    P3&=0xFD; SPIBUF=value;
    while(!(SPICON&0x80) && --timeout);
    if(!timeout) radio.error|=ERR_LCD;
    P3|=2;
}
void lcd_power(u8 on) {
    P3&=0xEF; lcd_byte(on?0x2F:0x28); lcd_byte(on?0xAF:0xAE);
    P1DIR&=0xDF;
    if(on) P1&=0xDF; else P1|=0x20;
}
void lcd_page(u8 row) {
    u8 x, crc=0;
    P3&=0xEF; lcd_byte(0xB0|row); lcd_byte(0x10); lcd_byte(0);
    P3|=0x10;
    for(x=0;x<128;++x) { lcd_byte(page[x]); crc=(crc<<1)|(crc>>7); crc^=page[x]; }
    radio.page_crc[row]=crc;
}
void hardware_init(void) {
    static const __code u8 commands[]={0xAE,0xA2,0xA0,0xC8,0x26,0xA8,0xA6,0x81,0,0x2F,0xA4,0x40,0xAF};
    u8 i;
    P1DIR&=0xDF; P1&=0xDF;
    X8(0x784E)|=0x10; P2|=0x40; P3|=0x13; P2DIR&=0x9F; P3DIR&=0xEC;
    X8(0x7823)=1; SPICON=0x15;
    delay(1500); P3&=0xFE; delay(1500); P3|=1; delay(1500);
    P3&=0xEF;
    for(i=0;i<sizeof(commands);++i) lcd_byte(commands[i]);
    /* Key ladders are ADC channel 1/P0.6 and channel 5/P1.3. */
    P0DIR|=0x40; P1DIR|=8; P3DIR|=8; P2DIR|=4;
    /* Loader strap sensing leaves P0.6's 10k pull-up enabled. It shifts digits. */
    X8(0x7825)&=0xBF; X8(0x7841)&=0xF7;
    PCON2&=0xFE; ADCMODE=0; X8(0x7880)=23;
}
static u8 adc(u8 channel) {
    u16 timeout=2000;
    ADCCON=0x88|channel;
    while((ADCCON&0x80) && --timeout);
    if(!timeout) { radio.error|=ERR_ADC; return 255; }
    return ADCDATA;
}
u8 hardware_key(void) {
    static const __code u8 bounds[]={0x0A,0x24,0x36,0x4A,0x60,0x76,0x8A,0xA6,0xC0,0xE2,0xF8};
    static const __code u8 first[]={0x17,0x18,0x19,0x1A,0x1B,0x1C,0x1D,0x1E,0x1F,8,0};
    static const __code u8 second[]={1,3,0x11,5,4,0x0A,0x0C,0x16,2,9,0x12};
    u8 i;
    if(POWER_SWITCH) return KEY_POWER;
    radio.adc1=adc(1); radio.adc2=adc(5);
    if(radio.adc1<0xE2) { for(i=0;i<10;++i) if(radio.adc1<bounds[i]) return first[i]; }
    if(radio.adc2<0xF8) { for(i=0;i<11;++i) if(radio.adc2<bounds[i]) return second[i]; }
    return 0;
}

static void audio_write(u8 reg,u8 value) {
    u16 timeout=2000;
    ATDAT=value; ATADR=reg|0xC0;
    while((ATADR&0x80) && --timeout);
    if(!timeout) radio.error|=ERR_AUDIO;
}
void hardware_audio(void) {
    /* Observed headphone analogue route. Cold-start playback needs listening validation. */
    X8(0x7809)&=0xF7; P3DIR&=0xDF; P3|=0x20;
    CLKCON1=(CLKCON1&0x3F)|0x80; PCON1&=0x7D; PCON3&=0xDB;
    X8(0x7890)=0xFE; X8(0x7891)=0x11; X8(0x7892)=0xE8;
    X8(0x7893)=0xFF; X8(0x7894)=0x0D; X8(0x7895)=0x10;
    audio_write(2,0); audio_write(0,1);
    audio_write(5,6); audio_write(4,0); audio_write(3,0);
    P3&=0xDF;
}
void hardware_amplifier(u8 enabled) {
    X8(0x7809)&=0xF7; P3DIR&=0xDF;
    if(enabled) P3&=0xDF; else P3|=0x20;
}
