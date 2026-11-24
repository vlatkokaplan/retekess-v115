/* Banked board context save/restore, state resume, and stock music-task entry.
   Linked at PC 0x1800 for SYSTEM_BANK. Entered only through the stock trampoline
   (LCALL 0x0133 with A=bank, DPTR=entry) and left through LJMP 0x0100.
   Arguments and results travel in the radio state, because the trampoline's
   bank loader clobbers DPTR. */
#include "radio.h"

#define X8(a) (*(volatile __xdata u8 *)(a))

void bank_entries(void) __naked {
    __asm
        lcall _radio_enter
        ljmp 0x0100
        lcall _radio_leave
        ljmp 0x0100
        lcall _stock_handoff
        ljmp 0x0100
    __endasm;
}

/* ---- Board context ------------------------------------------------------ */

/* Peripheral registers the radio reconfigures. The timer mode register is last,
   so the stock timer restarts only after its count and control are restored. */
#define BOARD_XDATA 14
static const __code u16 board_xdata[BOARD_XDATA]={0x7809,0x7823,0x7825,0x7841,0x784E,0x7880,0x7890,0x7893,
    0x7850,0x7851,0x7852,0x7853,0x785B,0x785A};
/* SFR order: P0DIR P1DIR P2DIR P3DIR P1 P2 P3 UARTCON PCON2 ADCMODE SPI0CON CLKGATE SYSCLK. */
#define BOARD_SFRS 13
__xdata u8 saved[BOARD_XDATA+BOARD_SFRS];

static void save_sfrs(void) __naked {
    __asm
        mov dptr,#(_saved+BOARD_XDATA)
        mov a,0xba
        movx @dptr,a
        inc dptr
        mov a,0xbb
        movx @dptr,a
        inc dptr
        mov a,0xbc
        movx @dptr,a
        inc dptr
        mov a,0xbd
        movx @dptr,a
        inc dptr
        mov a,0x90
        movx @dptr,a
        inc dptr
        mov a,0xa0
        movx @dptr,a
        inc dptr
        mov a,0xb0
        movx @dptr,a
        inc dptr
        mov a,0xfd
        movx @dptr,a
        inc dptr
        mov a,0xd3
        movx @dptr,a
        inc dptr
        mov a,0xda
        movx @dptr,a
        inc dptr
        mov a,0xd9
        movx @dptr,a
        inc dptr
        mov a,0x9c
        movx @dptr,a
        inc dptr
        mov a,0xdb
        movx @dptr,a
        ret
    __endasm;
}
static void restore_sfrs(void) __naked {
    __asm
        mov dptr,#(_saved+BOARD_XDATA)
        movx a,@dptr
        mov 0xba,a
        inc dptr
        movx a,@dptr
        mov 0xbb,a
        inc dptr
        movx a,@dptr
        mov 0xbc,a
        inc dptr
        movx a,@dptr
        mov 0xbd,a
        inc dptr
        movx a,@dptr
        mov 0x90,a
        inc dptr
        movx a,@dptr
        mov 0xa0,a
        inc dptr
        movx a,@dptr
        mov 0xb0,a
        inc dptr
        movx a,@dptr
        mov 0xfd,a
        inc dptr
        movx a,@dptr
        mov 0xd3,a
        inc dptr
        movx a,@dptr
        mov 0xda,a
        inc dptr
        movx a,@dptr
        mov 0xd9,a
        inc dptr
        movx a,@dptr
        mov 0x9c,a
        inc dptr
        movx a,@dptr
        mov 0xdb,a
        ret
    __endasm;
}

/* The first ten state bytes of a fresh record: magic, version, FM, volume 1, reg7, 95.300 MHz. */
static const __code u8 defaults[]={'V','1','1','5',RADIO_VERSION,FM,25,0xA1,(u8)3812,3812>>8};
static const __code u16 band_defaults[]={3812,999,6000};

static void sync_eq(void) {
    /* The stock player's own menu may have changed the preset meanwhile. */
    radio.eq=X8(STOCK_EQ)<EQ_PRESETS?X8(STOCK_EQ):0;
}

void radio_enter(void) {
    u8 i;
    __xdata u8 *state=(__xdata u8 *)&radio;
    for(i=0;i<BOARD_XDATA;++i) saved[i]=X8(board_xdata[i]);
    save_sfrs();
    for(i=0;i<5 && state[i]==defaults[i];++i);
    if(i==5 && radio.mode<=SW && radio.volume>=24 && radio.volume<=56) {
        /* Returning from a stock task: keep band, frequencies, volume, mute, tuner
           options and any handoff message; drop every transient UI state. */
        radio.editing=0; radio.entry_bad=0; radio.digits=0; radio.sleeping=0; radio.standby=0;
        radio.menu=0; radio.handoff=0; radio.pending=0; radio.error=0; radio.command=0;
        radio.injected_key=0; radio.frequency=radio.band_frequency[radio.mode];
        sync_eq();
        return;
    }
    for(i=0;i<sizeof(radio);++i) state[i]=0;
    for(i=0;i<sizeof(defaults);++i) state[i]=defaults[i];
    for(i=0;i<3;++i) radio.band_frequency[i]=band_defaults[i];
    sync_eq();
}

void radio_leave(void) {
    u8 i;
    hardware_amplifier(0);
    tuner_write(0,radio.mode==FM?0x44:4);
    lcd_power(1);
    for(i=0;i<BOARD_XDATA;++i) X8(board_xdata[i])=saved[i];
    restore_sfrs();
}

/* ---- Stock music task entry ----------------------------------------------
   Replays the stock radio overlay's MODE handler (overlay PC 0x45E5-0x462E):
   device test 0x2111 (R1: 0=SD, 1=USB), select 0C:1FD1 (R1=1 prefers USB),
   media check 0x119C, then 00:1DC9(2), 04:1E81, 00:1DC9(0). Runs with the stock
   interrupts and registers restored, before returning to the stock radio task.
   The EQ preset goes through the player's own eq_set (09:1A54: store 0x3B6A, save
   setting 0x0D, no-op if unchanged); the player applies it at each playback start
   (15:1887 -> music_eq 15:1CFF). */

static u8 device_present(u8 device) __naked {
    device;
    __asm
        mov r1,dpl
        lcall 0x2111
        clr a
        rlc a
        mov dpl,a
        ret
    __endasm;
}
static u8 media_ready(void) __naked {
    __asm
        lcall 0x119C
        clr a
        rlc a
        mov dpl,a
        ret
    __endasm;
}
static void select_device(u8 usb) __naked {
    usb;
    __asm
        mov r1,dpl
        mov dptr,#0x1FD1
        mov a,#0x0C
        ljmp 0x0133
    __endasm;
}
static void audio_source(u8 source) __naked {
    source;
    __asm
        mov r1,dpl
        mov dptr,#0x1DC9
        clr a
        ljmp 0x0133
    __endasm;
}
static void eq_set(u8 preset) __naked {
    preset;
    __asm
        mov r1,dpl
        mov dptr,#0x1A54
        mov a,#0x09
        ljmp 0x0133
    __endasm;
}
static void media_start(void) __naked {
    __asm
        mov dptr,#0x1E81
        mov a,#0x04
        ljmp 0x0133
    __endasm;
}

void stock_handoff(void) {
    u8 usb;
    if(radio.handoff==HANDOFF_POWEROFF) { X8(TASK_ID)=HANDOFF_POWEROFF; return; }
    if(radio.handoff!=HANDOFF_MUSIC) return;
    usb=device_present(1);
    if(!usb && !device_present(0)) { radio.message=MSG_NO_MEDIA; return; }
    eq_set(radio.eq);
    X8(TASK_ID)=HANDOFF_MUSIC;
    select_device(usb);
    if(media_ready()) { audio_source(2); media_start(); audio_source(0); }
}
