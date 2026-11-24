/* Source-built radio application, independent of stock menus and task state. */
#include "radio.h"
#include "font.h"
#include <stddef.h>
#ifdef RADIO_MENU
#define FONT_LAST 'Z'
#else
#define FONT_LAST 95
#endif

typedef char state_must_fit[sizeof(struct Radio)<=128?1:-1];
const __code u8 app_layout[]={sizeof(struct Radio),offsetof(struct Radio,mode),offsetof(struct Radio,volume),
    offsetof(struct Radio,frequency),offsetof(struct Radio,entry),offsetof(struct Radio,digits),
    offsetof(struct Radio,editing),offsetof(struct Radio,sleeping),offsetof(struct Radio,muted),
    offsetof(struct Radio,error),offsetof(struct Radio,cn),offsetof(struct Radio,status),
    offsetof(struct Radio,adc1),offsetof(struct Radio,adc2),offsetof(struct Radio,last_key),
    offsetof(struct Radio,injected_key),offsetof(struct Radio,command),offsetof(struct Radio,frames),
    offsetof(struct Radio,events),offsetof(struct Radio,tunes),offsetof(struct Radio,page_crc),
    offsetof(struct Radio,pending),offsetof(struct Radio,entry_bad),offsetof(struct Radio,read_channel),
    offsetof(struct Radio,standby),offsetof(struct Radio,reserved)
#ifdef RADIO_MENU
    ,offsetof(struct Radio,menu),offsetof(struct Radio,item),offsetof(struct Radio,handoff),
    offsetof(struct Radio,message),offsetof(struct Radio,reg7),offsetof(struct Radio,band_frequency),
    offsetof(struct Radio,eq),offsetof(struct Radio,stereo)
#endif
    };

__xdata __at(RADIO_STATE) struct Radio radio;
__xdata __at(RADIO_PAGE) u8 page[128];

__xdata u8 cursor;
static u8 rem;
#ifdef RADIO_MENU
/* Tail-call a bank entry through the stock trampoline; the entry returns via 0x0100. */
void bank_call(u16 entry) __naked {
    entry;
    __asm
        mov a,dph
        mov dph,#0x18
        ljmp 0x0133
    __endasm;
}
#endif
static u16 divide(u16 value,u8 by) {
    u16 quotient=0; u8 bit=16; rem=0;
    do {
        rem<<=1; if(value&0x8000) rem|=1; value<<=1; quotient<<=1;
        if(rem>=by) { rem-=by; quotient|=1; }
    } while(--bit);
    return quotient;
}
void clear(void) { u8 i; for(i=0;i<128;++i) page[i]=0; cursor=0; }
void character(u8 c) {
    u8 column; u16 offset;
    if(c<32 || c>FONT_LAST || cursor>122) return;
    offset=(u16)(c-32); offset=(offset<<2)+offset;
    for(column=0;column<5;++column) page[cursor++]=font[offset+column];
    ++cursor;
}
void text(const __code char *s) { while(*s) character(*s++); }
#ifdef RADIO_MENU
/* number() can collect digits for the large seven-segment frequency instead of drawing them. */
#define GLYPH_CURSOR 10
#define GLYPH_POINT 11
static __xdata u8 glyph[7];
static __xdata u8 glyphs;
static __xdata u8 to_glyphs;
static void add_glyph(u8 g);
#endif
void number(u16 value,u8 places) {
    static const __code u16 divisors[]={10000,1000,100,10,1};
    u8 pos,digit,started=0;
    for(pos=0;pos<5;++pos) {
        digit=0; while(value>=divisors[pos]) { value-=divisors[pos]; ++digit; }
        if(digit || started || pos==4 || pos>=5-places) {
#ifdef RADIO_MENU
            if(to_glyphs) add_glyph(digit); else
#endif
            character('0'+digit);
            started=1;
        }
    }
}
#ifdef RADIO_MENU
static u16 low(void);
static u16 high(void);
/* Screen pages: 0 status bar, 1-4 large frequency, 5 tuning dial, 6 signal or message,
   7 key hints. Each part is its own function so SDCC keeps its temporaries in
   registers and XDATA: internal RAM above 0x09 belongs to the banks and stock code. */
static const __code char bands[]="FM\0MW\0SW";
/* Seven-segment digits are 12x32 pixels on a 15-pixel pitch. Row masks (LSB = top)
   per frequency page: top rows 1-3, middle 14-16, bottom 27-29, upper verticals
   2-14, lower verticals 16-28. */
static const __code u8 seg_rows[20]={0x0E,0,0,0, 0,0xC0,0x01,0, 0,0,0,0x38, 0xFC,0x7F,0,0, 0,0,0xFF,0x1F};
/* Segments a-g in bits 0-6 for digits 0-9, then the entry cursor (bottom bar). */
static const __code u8 seg_glyph[11]={0x3F,0x06,0x5B,0x4F,0x66,0x6D,0x7D,0x07,0x7F,0x6F,0x08};
/* Bars 3-7 pixels tall over a 1-pixel baseline dot (LSB = top row). */
static const __code u8 bar_mask[5]={0xE0,0xF0,0xF8,0xFC,0xFE};
static const __code u8 bar_level[5]={6,12,18,24,30};
static const __code u8 speaker[4]={0x1C,0x1C,0x3E,0x7F};
static __xdata u8 gp;
/* Inverts columns 0..width-1 (width 1-255) of the page buffer. */
static void invert(u8 width) __naked {
    width;
    __asm
        mov r7,dpl
        mov dptr,#_page
    00001$:
        movx a,@dptr
        cpl a
        movx @dptr,a
        inc dptr
        djnz r7,00001$
        ret
    __endasm;
}
#if (RADIO_PAGE & 0xFF) != 0
#error the glyph renderer needs a 256-byte aligned page buffer
#endif
/* Page gp (0-3) of the glyph string, right-aligned to x=107 and drawn from the last
   glyph: digits are 12 pixels on a 15-pixel pitch, the decimal point 3 pixels on a
   5-pixel pitch. Columns 0-2 carry the left verticals, 9-11 the right ones, 1-10 the
   horizontals. R0=glyphs left (index), R1=gp, R2=x, R5/R6/R7=horizontal/left/right
   masks, B=segments. */
static void glyph_page(void) __naked {
    __asm
        mov dptr,#_glyphs
        movx a,@dptr
        jnz 00000$
        ret
    00000$:
        mov r0,a
        mov dptr,#_gp
        movx a,@dptr
        mov r1,a
        mov r2,#107
    00010$:
        dec r0
        mov a,#_glyph
        add a,r0
        mov dpl,a
        clr a
        addc a,#(_glyph >> 8)
        mov dph,a
        movx a,@dptr
        cjne a,#GLYPH_POINT,00020$
        mov a,r2
        add a,#(256-5)
        mov r2,a
        cjne r1,#3,00040$
        mov dph,#(RADIO_PAGE >> 8)
        mov dpl,r2
        mov a,#0x38
        movx @dptr,a
        inc dptr
        movx @dptr,a
        inc dptr
        movx @dptr,a
        sjmp 00040$
    00020$:
        mov dptr,#_seg_glyph
        movc a,@a+dptr
        mov b,a
        mov dptr,#_seg_rows
        clr a
        mov r5,a
        mov r6,a
        mov r7,a
        jnb b.0,00021$
        mov a,r1
        movc a,@a+dptr
        orl ar5,a
    00021$:
        jnb b.6,00022$
        mov a,r1
        add a,#4
        movc a,@a+dptr
        orl ar5,a
    00022$:
        jnb b.3,00023$
        mov a,r1
        add a,#8
        movc a,@a+dptr
        orl ar5,a
    00023$:
        jnb b.5,00024$
        mov a,r1
        add a,#12
        movc a,@a+dptr
        orl ar6,a
    00024$:
        jnb b.4,00025$
        mov a,r1
        add a,#16
        movc a,@a+dptr
        orl ar6,a
    00025$:
        jnb b.1,00026$
        mov a,r1
        add a,#12
        movc a,@a+dptr
        orl ar7,a
    00026$:
        jnb b.2,00027$
        mov a,r1
        add a,#16
        movc a,@a+dptr
        orl ar7,a
    00027$:
        mov a,r2
        add a,#(256-15)
        mov r2,a
        mov dph,#(RADIO_PAGE >> 8)
        mov dpl,a
        mov a,r6
        movx @dptr,a
        inc dptr
        orl a,r5
        movx @dptr,a
        inc dptr
        movx @dptr,a
        inc dptr
        mov a,r5
        mov r4,#6
    00030$:
        movx @dptr,a
        inc dptr
        djnz r4,00030$
        mov a,r7
        orl a,r5
        movx @dptr,a
        inc dptr
        movx @dptr,a
        inc dptr
        mov a,r7
        movx @dptr,a
    00040$:
        mov a,r0
        jz 00041$
        ljmp 00010$
    00041$:
        ret
    __endasm;
}
static void add_glyph(u8 g) __naked {
    g;
    __asm
        mov r7,dpl
        mov dptr,#_glyphs
        movx a,@dptr
        mov r6,a
        inc a
        movx @dptr,a
        mov a,#_glyph
        add a,r6
        mov dpl,a
        clr a
        addc a,#(_glyph >> 8)
        mov dph,a
        mov a,r7
        movx @dptr,a
        ret
    __endasm;
}
/* Signal bars at x=20+3i (two columns each, lit when the C/N reaches bar_level[i],
   else a baseline dot; unknown or tuning counts as 0), then the speaker icon at x=108. */
#define OFS_CN 17
#define OFS_PENDING 40
typedef char check_cn_offset[offsetof(struct Radio,cn)==OFS_CN?1:-1];
typedef char check_pending_offset[offsetof(struct Radio,pending)==OFS_PENDING?1:-1];
static void status_icons(void) __naked {
    __asm
        mov dptr,#(_radio + OFS_PENDING)
        movx a,@dptr
        mov r7,#0
        jnz 00000$
        mov dptr,#(_radio + OFS_CN)
        movx a,@dptr
        mov r7,a
        cjne a,#255,00000$
        mov r7,#0
    00000$:
        mov r6,#0
        mov r0,#20
    00001$:
        mov dptr,#_bar_level
        mov a,r6
        movc a,@a+dptr
        mov r5,a
        clr c
        mov a,r7
        subb a,r5
        mov a,#0x80
        jc 00002$
        mov dptr,#_bar_mask
        mov a,r6
        movc a,@a+dptr
    00002$:
        mov dph,#(RADIO_PAGE >> 8)
        mov dpl,r0
        movx @dptr,a
        inc dptr
        movx @dptr,a
        inc r0
        inc r0
        inc r0
        inc r6
        cjne r6,#5,00001$
        mov r6,#0
    00003$:
        mov dptr,#_speaker
        mov a,r6
        movc a,@a+dptr
        mov dph,#(RADIO_PAGE >> 8)
        mov dpl,r6
        mov r5,a
        mov a,#108
        add a,r6
        mov dpl,a
        mov a,r5
        movx @dptr,a
        inc r6
        cjne r6,#4,00003$
        ret
    __endasm;
}
/* Dial page: baseline at row 4 over x=4..124, ticks (rows 3-5) every 10 pixels, and
   a 3-pixel needle (rows 1-6) starting at x. */
static void dial_page(u8 x) __naked {
    x;
    __asm
        mov r7,dpl
        mov dptr,#(_page + 4)
        mov r6,#121
        mov a,#0x10
    00001$:
        movx @dptr,a
        inc dptr
        djnz r6,00001$
        mov r6,#4
    00002$:
        mov dpl,r6
        mov a,#0x38
        movx @dptr,a
        mov a,r6
        add a,#10
        mov r6,a
        cjne a,#134,00002$
        mov dpl,r7
        mov a,#0x7E
        movx @dptr,a
        inc dptr
        movx @dptr,a
        inc dptr
        movx @dptr,a
        ret
    __endasm;
}
/* Band tag, signal bars (one per 6 dB of C/N), stereo or forced mono, volume. */
static void draw_status(void) {
    clear(); cursor=2; text(bands+(u8)((radio.mode<<1)+radio.mode)); invert(15);
    status_icons();
    if(radio.mode==FM && !radio.pending) {
        cursor=40;
        if(radio.reg7&4) text("MONO"); else if(radio.stereo) text("STEREO");
    }
    cursor=114;
    if(radio.muted || radio.volume==24) character('X'); else number(radio.volume-24,0);
    lcd_page(0);
}
/* Frequency right-aligned to x=107; an entry shows the typed digits and a cursor. */
static void draw_frequency(void) {
    glyphs=0; to_glyphs=1;
    if(radio.editing) { if(radio.digits) number(radio.entry,0); add_glyph(GLYPH_CURSOR); }
    else if(radio.mode==FM) {
        number(divide(radio.frequency,40),0); add_glyph(GLYPH_POINT);
        number(((u16)rem<<4)+((u16)rem<<3)+rem,3);
    } else number(radio.frequency,0);
    to_glyphs=0;
    for(gp=0;gp<4;++gp) {
        clear(); glyph_page();
        if(gp==3 && !radio.editing) { cursor=110; text(radio.mode==FM?"MHZ":"KHZ"); }
        lcd_page(gp+1);
    }
}
/* Tuning dial over x=4..124: FM spans the 87.5-108 MHz broadcast band, MW and SW
   their tuning range. needle = 3 + min(120, ((frequency-low) >> shift) / divisor),
   and 3 below the low end; divide() needs divisors up to 128. */
#define OFS_MODE 5
#define OFS_FREQUENCY 8
typedef char check_mode_offset[offsetof(struct Radio,mode)==OFS_MODE?1:-1];
typedef char check_frequency_offset[offsetof(struct Radio,frequency)==OFS_FREQUENCY?1:-1];
static const __code u16 dial_low[3]={3500,522,150};
static const __code u8 dial_shift[3]={0,0,2};
static const __code u8 dial_divisor[3]={7,10,63};
static void draw_dial(void) __naked {
    __asm
        lcall _clear
        mov dptr,#(_radio + OFS_MODE)
        movx a,@dptr
        mov r7,a
        add a,acc
        mov r6,a
        mov dptr,#_dial_low
        movc a,@a+dptr
        mov r4,a
        mov a,r6
        inc a
        movc a,@a+dptr
        mov r5,a
        mov dptr,#(_radio + OFS_FREQUENCY)
        movx a,@dptr
        clr c
        subb a,r4
        mov r2,a
        inc dptr
        movx a,@dptr
        subb a,r5
        mov r3,a
        mov a,#3
        jc 00009$
        mov a,r7
        mov dptr,#_dial_shift
        movc a,@a+dptr
        jz 00002$
        mov r6,a
    00001$:
        clr c
        mov a,r3
        rrc a
        mov r3,a
        mov a,r2
        rrc a
        mov r2,a
        djnz r6,00001$
    00002$:
        mov a,r7
        mov dptr,#_dial_divisor
        movc a,@a+dptr
        mov dptr,#_divide_PARM_2
        movx @dptr,a
        mov dpl,r2
        mov dph,r3
        lcall _divide
        mov a,dph
        jnz 00003$
        mov a,dpl
        add a,#(256-121)
        jc 00003$
        mov a,dpl
        sjmp 00004$
    00003$:
        mov a,#120
    00004$:
        add a,#3
    00009$:
        mov dpl,a
        lcall _dial_page
        mov dpl,#5
        ljmp _lcd_page
    __endasm;
}
/* Signal quality, or the message that matters most. */
static void draw_info(void) {
    clear();
    if(radio.message) text("NO SD CARD OR USB");
    else if(radio.error&ERR_I2C) text("TUNER READ ERROR");
    else if(radio.error&ERR_TUNE) text("CHECK FREQUENCY");
    else if(radio.error) text("HARDWARE ERROR");
    else if(radio.editing) text("PLAY OK  DEL BACK");
    else if(radio.pending) text("TUNING");
    else {
        text("SNR "); if(radio.cn==255) text("--"); else number(radio.cn,0); text(" DB");
        if(radio.status&0x20) { cursor=66; text("LOCK"); }
        if(radio.reg7&0x10) { cursor=98; text("BASS"); }
    }
    lcd_page(6);
}
static void draw(void) {
    if(radio.menu) bank_call(BANK_MENU_DRAW);
    else {
        draw_status(); draw_frequency(); draw_dial(); draw_info();
        clear(); cursor=4; text(RADIO_POWER_HINT); invert(128); lcd_page(7);
    }
    ++radio.frames; radio.dirty=0;
}
#else
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
        case 4: text("VOL "); number(radio.volume-24,0); text((radio.muted||radio.volume==24)?" MUTED":" SOUND"); break;
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
#endif
#ifdef RADIO_MENU
static const __code u16 band_low[3]={1200,522,150};
static const __code u16 band_high[3]={9200,1710,30000};
static const __code u8 band_step[3]={1,9,5};
static u16 low(void) { return band_low[radio.mode]; }
static u16 high(void) { return band_high[radio.mode]; }
static u8 step(void) { return band_step[radio.mode]; }
#else
static u16 low(void) { return radio.mode==FM?1200:radio.mode==MW?522:150; }
static u16 high(void) { return radio.mode==FM?9200:radio.mode==MW?1710:30000; }
static u8 step(void) { return radio.mode==FM?1:radio.mode==MW?9:5; }
#endif
static void metrics(void) {
    u8 cn;
    radio.status=tuner_read(20);
    radio.read_channel=((u16)(radio.status&31)<<8)|tuner_read(21);
    cn=tuner_read(radio.mode==FM?23:22);
    radio.cn=cn==255?255:cn&127;
#ifdef RADIO_MENU
    /* Register 23 bit 7: FM stereo demodulation active. */
    radio.stereo=0;
    if(cn!=255 && (cn&0x80) && radio.mode==FM) radio.stereo=1;
#endif
    if(radio.status==255) { radio.status=0; radio.error|=ERR_I2C; }
}
void audio(void) {
    u8 mute=4;
    hardware_volume(radio.volume-24);
    if(radio.volume>24 && !radio.muted && !radio.pending && radio.settle && !(radio.error&(ERR_I2C|ERR_AUDIO))) mute=0;
    tuner_write(0,(radio.mode==FM?0xE8:0xA8)|mute);
    if(mute || (radio.error&ERR_I2C)) hardware_amplifier(0);
    else hardware_amplifier(1);
}
void tune(void) {
    u16 channel;
    u8 config, reg2;
    if(radio.frequency<low() || radio.frequency>high()) { radio.error|=ERR_TUNE; return; }
    hardware_amplifier(0);
    radio.error&=0xEF;
    radio.band_frequency[radio.mode]=radio.frequency;
    config=radio.mode==FM?0xC4:0x84;
    tuner_write(0,config);
    tuner_write(1,radio.mode==SW?0x20:0x10);
    channel=radio.mode==FM?radio.frequency-1200:divide(radio.frequency,radio.mode==MW?3:5);
    radio.target_channel=channel;
    reg2=0x40|(u8)(channel>>8); if(radio.mode==MW) reg2|=0x20;
    tuner_write(2,reg2); tuner_write(3,(u8)channel);
    tuner_write(6,0x8C); tuner_write(7,radio.reg7);
    tuner_write(9,0x0F); tuner_write(13,0x8C);
    tuner_write(0,config|0x20);
    radio.pending=1; radio.settle=150;
    radio.status=0; radio.cn=255; ++radio.tunes; radio.dirty=1;
}
static void key(u8 k) {
    u16 next; u8 before;
    ++radio.events; radio.last_key=k; radio.dirty=1;
#ifdef RADIO_AUTONOMOUS
    if(radio.standby) {
        if(k!=KEY_POWER) return;
        radio.standby=0; radio.sleeping=0; radio.repeat=255;
        lcd_power(1); tune(); return;
    }
    if(k==KEY_POWER) {
        radio.standby=1; radio.sleeping=1; radio.pending=0; radio.editing=0;
#ifdef RADIO_MENU
        radio.menu=0;
#endif
        hardware_amplifier(0); tuner_write(0,(radio.mode==FM?0x44:4)); lcd_power(0); return;
    }
#endif
    if(radio.sleeping) { radio.sleeping=0; radio.repeat=255; lcd_power(1); radio.dirty=1; return; }
#ifdef RADIO_MENU
    if(radio.menu) { bank_call(BANK_MENU_KEY); return; }
    radio.message=0;
    if(k==KEY_MODE) { radio.editing=0; radio.handoff=HANDOFF_MUSIC; return; }
#endif
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
        } else { radio.muted^=1; audio(); }
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
        before=radio.volume;
        if(k==9 && radio.volume<56) ++radio.volume;
        if(k==8 && radio.volume>24) --radio.volume;
        hardware_volume(radio.volume-24);
        /* Only crossing silence changes the tuner mute and speaker enable. */
        if(before==24 || radio.volume==24) audio();
    } else if(k==KEY_MENU) {
        if(radio.editing) { radio.editing=0; radio.error&=0xEF; }
#ifdef RADIO_MENU
        else radio.menu=1;
#else
        else { radio.sleeping=1; lcd_power(0); }
#endif
    }
    else if(k==KEY_POWER) { radio.command=1; }
    radio.dirty=1;
}
void app_init(void) {
    /* 0x0B=0xC0 selects the 25 kHz grid; live tests round manual tunes with 0xE0. */
    static const __code u8 initial[]={0xCC,0x10,0x4A,0xC8,0x19,0x32,0x8C,0xA1,0x58,7,0x7F,0xC0,0,0x0C,0x40,0x86,0xCC,0xFD,0x80,0x80};
    u8 i;
#ifdef RADIO_MENU
    /* Overlay statics are not zeroed by the loader (a stock task used this RAM). */
    to_glyphs=0;
    /* Saves the stock context, then resumes the previous radio state or sets defaults. */
    bank_call(BANK_ENTER);
#else
    for(i=0;i<sizeof(radio);++i) ((__xdata u8 *)&radio)[i]=0;
    radio.magic[0]='V'; radio.magic[1]='1'; radio.magic[2]='1'; radio.magic[3]='5';
    radio.version=1; radio.volume=25; radio.reg7=0xA1;
    radio.frequency=3812; radio.band_frequency[0]=3812; radio.band_frequency[1]=999; radio.band_frequency[2]=6000;
#endif
    hardware_init();
#ifdef RADIO_NATIVE_AUDIO
    /* The established boot path already selected this board's speaker route. */
    hardware_amplifier(0);
#else
    hardware_audio();
#endif
    hardware_volume(radio.volume-24);
    for(i=0;i<20;++i) tuner_write(i,initial[i]);
    tune(); draw();
}
void app_tick(void) {
    u8 k;
#ifdef RADIO_MENU
    if(radio.command || radio.handoff) return;
#else
    if(radio.command) return;
#endif
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
            radio.pending=0; audio(); radio.dirty=1;
        } else if(!--radio.settle) { radio.pending=0; radio.error|=ERR_TUNE; radio.dirty=1; }
    }
    if(++radio.ticks>=25) { radio.ticks=0; metrics(); radio.dirty=1; }
    if(radio.dirty && !radio.sleeping) draw();
}
#ifndef RADIO_AUTONOMOUS
void app_stop(void) { hardware_amplifier(0); tuner_write(0,(radio.mode==FM?0xEC:0xAC)); lcd_power(1); }
#endif
