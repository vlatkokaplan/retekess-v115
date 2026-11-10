/* Source-built radio application, independent of stock menus and task state. */
#include "radio.h"
#include "font.h"
#include <stddef.h>

typedef char state_must_fit[sizeof(struct Radio)<=128?1:-1];
const __code u8 app_layout[]={sizeof(struct Radio),offsetof(struct Radio,mode),offsetof(struct Radio,volume),
    offsetof(struct Radio,frequency),offsetof(struct Radio,entry),offsetof(struct Radio,digits),
    offsetof(struct Radio,editing),offsetof(struct Radio,sleeping),offsetof(struct Radio,muted),
    offsetof(struct Radio,error),offsetof(struct Radio,cn),offsetof(struct Radio,status),
    offsetof(struct Radio,adc1),offsetof(struct Radio,adc2),offsetof(struct Radio,last_key),
    offsetof(struct Radio,injected_key),offsetof(struct Radio,command),offsetof(struct Radio,frames),
    offsetof(struct Radio,events),offsetof(struct Radio,tunes),offsetof(struct Radio,page_crc),
    offsetof(struct Radio,pending),offsetof(struct Radio,entry_bad),offsetof(struct Radio,read_channel),
    offsetof(struct Radio,standby),offsetof(struct Radio,reserved)};

__xdata __at(RADIO_STATE) struct Radio radio;
__xdata __at(RADIO_PAGE) u8 page[128];

static u8 cursor;
static u8 rem;
static u16 divide(u16 value,u8 by) {
    u16 quotient=0; u8 bit=16; rem=0;
    do {
        rem<<=1; if(value&0x8000) rem|=1; value<<=1; quotient<<=1;
        if(rem>=by) { rem-=by; quotient|=1; }
    } while(--bit);
    return quotient;
}
static void clear(void) { u8 i; for(i=0;i<128;++i) page[i]=0; cursor=0; }
static void character(u8 c) {
    u8 column; u16 offset;
    if(c<32 || c>95 || cursor>122) return;
    offset=(u16)(c-32); offset=(offset<<2)+offset;
    for(column=0;column<5;++column) page[cursor++]=font[offset+column];
    ++cursor;
}
static void text(const __code char *s) { while(*s) character(*s++); }
static void number(u16 value,u8 places) {
    static const __code u16 divisors[]={10000,1000,100,10,1};
    u8 pos,digit,started=0;
    for(pos=0;pos<5;++pos) {
        digit=0; while(value>=divisors[pos]) { value-=divisors[pos]; ++digit; }
        if(digit || started || pos==4 || pos>=5-places) { character('0'+digit); started=1; }
    }
}
static void draw(void) {
    u8 row; u16 value;
    for(row=0;row<8;++row) {
        clear();
        switch(row) {
        case 0: text(RADIO_LABEL); break;
        case 1: text(radio.mode==FM?"FM":radio.mode==MW?"MW":"SW");
            text(radio.sleeping?" SLEEP":" RADIO"); break;
        case 2:
            if(radio.editing) {
                if(radio.entry_bad) text("ENTRY TOO LARGE");
                else { text("ENTER "); number(radio.entry,0); character('_'); }
            }
            else if(radio.mode==FM) {
                value=divide(radio.frequency,40); number(value,0); character('.');
                value=((u16)rem<<4)+((u16)rem<<3)+rem; number(value,3); text(" MHZ");
            } else { number(radio.frequency,0); text(" KHZ"); }
            break;
        case 3:
            text("C/N "); if(radio.cn==255) text("--"); else number(radio.cn,0);
            text(" DB "); text(radio.pending?"TUNING":radio.status&0x20?"LOCK":"----"); break;
        case 4: text("VOL "); number(radio.volume-24,0); text(radio.muted?" MUTED":" SOUND"); break;
        case 5:
            if(radio.error&ERR_I2C) text("TUNER READ ERROR");
            else if(radio.error&ERR_TUNE) text("CHECK FREQUENCY");
            else if(radio.error) text("HARDWARE ERROR");
            else text(radio.editing?"PLAY OK  DEL BACK":"FM/AM  SW  +/- TUNE"); break;
        case 6: text("0-9 FREQ  PLAY MUTE"); break;
        case 7: text(RADIO_POWER_HINT); break;
        }
        lcd_page(row);
    }
    ++radio.frames; radio.dirty=0;
}
static u16 low(void) { return radio.mode==FM?1200:radio.mode==MW?522:150; }
static u16 high(void) { return radio.mode==FM?9200:radio.mode==MW?1710:30000; }
static u8 step(void) { return radio.mode==FM?1:radio.mode==MW?9:5; }
static void metrics(void) {
    u8 cn;
    radio.status=tuner_read(20);
    radio.read_channel=((u16)(radio.status&31)<<8)|tuner_read(21);
    cn=tuner_read(radio.mode==FM?23:22);
    radio.cn=cn==255?255:cn&127;
    if(radio.status==255) { radio.status=0; radio.error|=ERR_I2C; }
}
static void tune(void) {
    u16 channel;
    u8 config, reg2;
    if(radio.frequency<low() || radio.frequency>high()) { radio.error|=ERR_TUNE; return; }
    radio.error&=0xEF;
    radio.band_frequency[radio.mode]=radio.frequency;
    config=radio.mode==FM?0xC4:0x84;
    tuner_write(0,config);
    tuner_write(1,radio.mode==SW?0x20:0x10);
    channel=radio.mode==FM?radio.frequency-1200:divide(radio.frequency,radio.mode==MW?3:5);
    radio.target_channel=channel;
    reg2=0x40|(u8)(channel>>8); if(radio.mode==MW) reg2|=0x20;
    tuner_write(2,reg2); tuner_write(3,(u8)channel);
    tuner_write(6,radio.volume<<2); tuner_write(7,radio.reg7);
    tuner_write(9,0x0F); tuner_write(13,0x8C);
    tuner_write(0,config|0x20);
    radio.pending=1; radio.settle=150;
    radio.status=0; radio.cn=255; ++radio.tunes; radio.dirty=1;
}
static void key(u8 k) {
    u16 next;
    ++radio.events; radio.last_key=k; radio.dirty=1;
#ifdef RADIO_AUTONOMOUS
    if(radio.standby) {
        if(k!=KEY_POWER) return;
        radio.standby=0; radio.sleeping=0; radio.repeat=255;
        lcd_power(1); tune(); hardware_amplifier(1); return;
    }
    if(k==KEY_POWER) {
        radio.standby=1; radio.sleeping=1; radio.pending=0; radio.editing=0;
        hardware_amplifier(0); tuner_write(0,(radio.mode==FM?0x44:4)); lcd_power(0); return;
    }
#endif
    if(radio.sleeping) { radio.sleeping=0; radio.repeat=255; lcd_power(1); radio.dirty=1; return; }
    if(k>=0x16 && k<=0x1F) {
        if(!radio.editing) { radio.entry=0; radio.digits=0; radio.entry_bad=0; radio.editing=1; radio.error&=0xEF; }
        if(radio.digits<5 && !radio.entry_bad) {
            next=radio.entry>3000?30001:(radio.entry<<3)+(radio.entry<<1)+(k-0x16);
            ++radio.digits;
            if(next>=radio.entry && next<=30000) radio.entry=next;
            else { radio.entry_bad=1; radio.error|=ERR_TUNE; }
        } else { if(!radio.entry_bad) ++radio.digits; radio.entry_bad=1; radio.error|=ERR_TUNE; }
    } else if(k==2) {
        if(radio.editing) {
            if(radio.entry_bad) return;
            if(radio.mode==FM && (radio.entry<300 || radio.entry>2300)) { radio.error|=ERR_TUNE; return; }
            next=radio.mode==FM?radio.entry<<2:radio.entry;
            if(radio.mode!=FM) { divide(next,step()); if(rem) { radio.error|=ERR_TUNE; return; } }
            if(next>=low() && next<=high()) {
                radio.frequency=next; radio.editing=0; tune();
            } else radio.error|=ERR_TUNE;
        } else { radio.muted^=1; tuner_write(0,(radio.mode==FM?0xE8:0xA8)|((radio.muted||radio.pending)?4:0)); }
    } else if(k==KEY_DELETE) {
        if(radio.entry_bad) { radio.entry_bad=0; --radio.digits; radio.error&=0xEF; }
        else if(radio.editing && radio.digits) { radio.entry=divide(radio.entry,10); --radio.digits; }
        else radio.editing=0;
    } else if(k==4 || k==0x12) {
        radio.editing=0; radio.mode=k==0x12?SW:(radio.mode==MW?FM:MW);
        radio.frequency=radio.band_frequency[radio.mode]; tune();
    } else if(k==1 || k==3) {
        radio.editing=0;
        if(k==3 && radio.frequency<=high()-step()) radio.frequency+=step();
        if(k==1 && radio.frequency>=low()+step()) radio.frequency-=step();
        tune();
    } else if(k==8 || k==9) {
        if(k==9 && radio.volume<43) ++radio.volume;
        if(k==8 && radio.volume>24) --radio.volume;
        tuner_write(6,radio.volume<<2);
    } else if(k==KEY_MENU) { if(radio.editing) { radio.editing=0; radio.error&=0xEF; } else { radio.sleeping=1; lcd_power(0); } }
    else if(k==KEY_POWER) { radio.command=1; }
    radio.dirty=1;
}
void app_init(void) {
    /* 0x0B=0xC0 selects the 25 kHz grid; live tests round manual tunes with 0xE0. */
    static const __code u8 initial[]={0xCC,0x10,0x4A,0xC8,0x19,0x32,0x8C,0xA1,0x58,7,0x7F,0xC0,0,0x0C,0x40,0x86,0xCC,0xFD,0x80,0x80};
    u8 i;
    for(i=0;i<sizeof(radio);++i) ((__xdata u8 *)&radio)[i]=0;
    radio.magic[0]='V'; radio.magic[1]='1'; radio.magic[2]='1'; radio.magic[3]='5';
    radio.version=1; radio.volume=35; radio.reg7=0xA1;
    radio.frequency=3812; radio.band_frequency[0]=3812; radio.band_frequency[1]=999; radio.band_frequency[2]=6000;
    hardware_init();
#ifdef RADIO_NATIVE_AUDIO
    /* The established boot path already selected this board's speaker route. */
    hardware_amplifier(0);
#else
    hardware_audio();
#endif
    for(i=0;i<20;++i) tuner_write(i,initial[i]);
    tune(); draw();
}
void app_tick(void) {
    u8 k;
    if(radio.command) return;
    if(radio.injected_key) { k=radio.injected_key; radio.injected_key=0; key(k); }
    else {
        k=hardware_key();
        if(k!=radio.candidate) { radio.candidate=k; radio.debounce=1; }
        else if(radio.debounce<2) {
            if(++radio.debounce==2 && k!=radio.held) {
                radio.held=k; radio.repeat=0; if(k) key(k);
            }
        } else if(k && radio.repeat!=255 && (k==1 || k==3 || k==8 || k==9)) {
            if(++radio.repeat>=25) { radio.repeat=17; key(k); }
        }
    }
    if(radio.standby) return;
    if(radio.pending) {
        if(radio.settle<=145) metrics();
        if(radio.settle<=145 && (radio.status&0x40) && radio.read_channel==radio.target_channel) {
            radio.pending=0; tuner_write(0,(radio.mode==FM?0xE8:0xA8)|(radio.muted?4:0)); radio.dirty=1;
#ifdef RADIO_NATIVE_AUDIO
            hardware_amplifier(1);
#endif
        } else if(!--radio.settle) { radio.pending=0; radio.error|=ERR_TUNE; radio.dirty=1; }
    }
    if(++radio.ticks>=25) { radio.ticks=0; metrics(); radio.dirty=1; }
    if(radio.dirty && !radio.sleeping) draw();
}
#ifndef RADIO_AUTONOMOUS
void app_stop(void) { tuner_write(0,(radio.mode==FM?0xEC:0xAC)); lcd_power(1); }
#endif
