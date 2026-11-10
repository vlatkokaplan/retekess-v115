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
    for(;;) { timer_tick(); app_tick(); }
}
void radio_forever(void) __naked {
    __asm
        mov 0xa8,#0
        mov 0xa9,#0
        anl 0xd0,#0xe7
        mov 0x86,#0
        ljmp _radio_main
    __endasm;
}
