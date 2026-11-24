/* Autonomous radio loop. The existing boot path initializes the PLL and DAC. */
#include "radio.h"
#define X8(a) (*(volatile __xdata u8 *)(a))
__sfr __at(0xDB) SYSTEM_CLOCK;
__sfr __at(0xDC) CLOCK_DIVIDER;
__sfr __at(0x9C) CLOCK_GATES;
#define TIMER_MODE X8(0x785A)
#define TIMER_CONTROL X8(0x785B)
__sfr __at(0xF7) WATCHDOG;

void radio_timer_start(void) {
    static const __code u16 periods[]={60000,30000,20000,15000};
    u16 count=periods[CLOCK_DIVIDER&3];
    /* Preserve the startup clock/divider used by the board's audio path. */
    count>>=(CLOCK_DIVIDER>>2)&3; --count;
    SYSTEM_CLOCK=(SYSTEM_CLOCK&0xEC)|2;
    CLOCK_GATES&=0xEF;
    TIMER_MODE=0; TIMER_CONTROL=4;
    X8(0x7850)=0; X8(0x7851)=0;
    X8(0x7852)=(u8)count; X8(0x7853)=(u8)(count>>8);
    TIMER_MODE=0x14;
}
static void timer_tick(void) {
    /* A stalled timer is recovered by the 512 ms watchdog in normal operation. */
    while(!(TIMER_CONTROL&0x80));
    TIMER_CONTROL=4; WATCHDOG=0x3C;
    if(!++radio.reserved[0]) ++radio.reserved[1];
}
void radio_main(void) {
    WATCHDOG=0;
    app_init();
    /* Consume the key that powered the radio on, including a held POWER. */
    radio.held=hardware_key(); radio.candidate=radio.held;
    radio.debounce=2; radio.repeat=255;
    radio_timer_start(); WATCHDOG=0x3C;
#ifdef RADIO_MENU
    while(!radio.handoff) { timer_tick(); app_tick(); }
    /* Return the board to the stock radio task's state; it then runs its exit path. */
    bank_call(BANK_LEAVE);
#else
    for(;;) { timer_tick(); app_tick(); }
#endif
}
#ifdef RADIO_MENU
/* Called by the stock radio task in place of its overlay main (bank 17, 0x1A0A).
   Stock IAR code expects R6/R7 and its virtual registers preserved; save 0x00-0x0F. */
void radio_forever(void) __naked {
    __asm
        push 0x00
        push 0x01
        push 0x02
        push 0x03
        push 0x04
        push 0x05
        push 0x06
        push 0x07
        push 0x08
        push 0x09
        push 0x0a
        push 0x0b
        push 0x0c
        push 0x0d
        push 0x0e
        push 0x0f
        push 0xd0
        push 0xa8
        push 0xa9
        push 0x86
        push 0xf7
        mov 0xa8,#0
        mov 0xa9,#0
        anl 0xd0,#0xe7
        mov 0x86,#0
        lcall _radio_main
        pop 0xf7
        orl 0xf7,#0x20
        pop 0x86
        pop 0xa9
        pop 0xa8
        ; Interrupts are live again, still on register bank 0: replay the stock
        ; MODE-to-music sequence if requested, then restore the stock PSW.
        mov dptr,#BANK_HANDOFF
        lcall _bank_call
        pop 0xd0
        pop 0x0f
        pop 0x0e
        pop 0x0d
        pop 0x0c
        pop 0x0b
        pop 0x0a
        pop 0x09
        pop 0x08
        pop 0x07
        pop 0x06
        pop 0x05
        pop 0x04
        pop 0x03
        pop 0x02
        pop 0x01
        pop 0x00
        ret
    __endasm;
}
#else
void radio_forever(void) __naked {
    __asm
        mov 0xa8,#0
        mov 0xa9,#0
        anl 0xd0,#0xe7
        mov 0x86,#0
        ljmp _radio_main
    __endasm;
}
#endif
