/* Banked OPEN settings menu. Linked at PC 0x1800 for MENU_BANK; entered only
   through the stock trampoline (LCALL 0x0133) and left through LJMP 0x0100.
   The key arrives in radio.last_key, because the bank loader clobbers DPTR. */
#include "radio.h"

void bank_entries(void) __naked {
    __asm
        lcall _menu_key
        ljmp 0x0100
        lcall _menu_draw
        ljmp 0x0100
    __endasm;
}

static const __code char * const __code labels[MENU_ITEMS]={
    "MP3 PLAYER","MP3 EQ","BAND","VOLUME","FM FILTER","STEREO","BASS BOOST","DE-EMPHASIS",
    "SCREEN OFF","STANDBY","POWER OFF"};
/* Stock preset order (SDK STR_EQ_NORMAL..STR_EQ_BASS; music_eq loads BASS separately). */
static const __code char * const __code eq_name[EQ_PRESETS]={
    "NORMAL","POP","ROCK","JAZZ","CLASSIC","COUNTRY","BASS"};
/* Register 7 bits 1:0 in menu order 200, 150, 100, 50 kHz, and names by code. */
static const __code u8 filter_code[]={1,0,3,2};
static const __code char * const __code filter_name[]={"150K","200K","50K","100K"};
static const __code char * const __code band_name[]={"FM","MW","SW"};

void menu_draw(void) {
    u8 row,item,x;
    item=radio.item>5?radio.item-5:0;
    /* Title underlined on the page's bottom row; footer inverted like the radio screen. */
    clear(); cursor=2; text("SETTINGS"); cursor=96; number(radio.item+1,0); character('/'); number(MENU_ITEMS,0);
    for(x=0;x<128;++x) page[x]|=0x80;
    lcd_page(0);
    for(row=1;row<7;++row,++item) {
        clear(); character(' '); text(labels[item]); cursor=78;
        switch(item) {
        case 1: text(eq_name[radio.eq]); break;
        case 2: text(band_name[radio.mode]); break;
        case 3: number(radio.volume-24,0); break;
        case 4: text(filter_name[radio.reg7&3]); break;
        case 5: text(radio.reg7&4?"MONO":"AUTO"); break;
        case 6: text(radio.reg7&0x10?"ON":"OFF"); break;
        case 7: text(radio.reg7&0x20?"75US":"50US"); break;
        }
        if(item==radio.item) for(x=0;x<128;++x) page[x]^=0xFF;
        lcd_page(row);
    }
    clear(); cursor=4; text("PLAY OK   MENU EXIT");
    for(x=0;x<128;++x) page[x]^=0xFF;
    lcd_page(7);
}

void menu_key(void) {
    u8 k=radio.last_key,item=radio.item,i,before;
    if(k==KEY_MENU) { radio.menu=0; return; }
    if(k==1) { radio.item=item?item-1:MENU_ITEMS-1; return; }
    if(k==3) { radio.item=item+1<MENU_ITEMS?item+1:0; return; }
    /* Digits 1-9 jump to the numbered item; 0 jumps to the tenth (POWER OFF is last, by scrolling). */
    if(k>=0x16 && k<=0x1F) { radio.item=k==0x16?9:k-0x17; return; }
    if(k!=2 && k!=8 && k!=9) return;
    switch(item) {
    case 0: if(k==2) { radio.menu=0; radio.handoff=HANDOFF_MUSIC; } return;
    case 1:
        /* Applied by the stock player when MP3 playback starts. */
        radio.eq=k==8?(radio.eq?radio.eq-1:EQ_PRESETS-1):(radio.eq+1<EQ_PRESETS?radio.eq+1:0);
        return;
    case 2:
        radio.mode=k==8?(radio.mode?radio.mode-1:SW):(radio.mode<SW?radio.mode+1:FM);
        radio.frequency=radio.band_frequency[radio.mode]; tune(); return;
    case 3:
        before=radio.volume;
        if(k==9 && radio.volume<56) ++radio.volume;
        if(k==8 && radio.volume>24) --radio.volume;
        hardware_volume(radio.volume-24);
        if(before==24 || radio.volume==24) audio();
        return;
    case 4:
        for(i=0;i<3 && filter_code[i]!=(radio.reg7&3);++i);
        i=k==8?(i+3)&3:(i+1)&3;
        radio.reg7=(radio.reg7&0xFC)|filter_code[i]; break;
    /* Register 7: bit 2 forces mono, bit 4 bass boost, bit 5 selects 75 us de-emphasis. */
    case 5: radio.reg7^=4; break;
    case 6: radio.reg7^=0x10; break;
    case 7: radio.reg7^=0x20; break;
    case 8: if(k==2) { radio.menu=0; radio.sleeping=1; lcd_power(0); } return;
    case 9:
        if(k==2) {
            radio.menu=0; radio.standby=1; radio.sleeping=1; radio.pending=0; radio.editing=0;
            hardware_amplifier(0); tuner_write(0,radio.mode==FM?0x44:4); lcd_power(0);
        }
        return;
    default: if(k==2) { radio.menu=0; radio.handoff=HANDOFF_POWEROFF; } return;
    }
    tuner_write(7,radio.reg7);
}
