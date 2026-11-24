#ifndef V115_RADIO_H
#define V115_RADIO_H

typedef unsigned char u8;
typedef unsigned int u16;

#define FM 0
#define MW 1
#define SW 2
#define ERR_I2C 1
#define ERR_ADC 2
#define ERR_LCD 4
#define ERR_AUDIO 8
#define ERR_TUNE 16
#define TICK_MS 20
#define KEY_POWER 0x14
#define KEY_MENU 0x0C
#define KEY_DELETE 0x11
#define KEY_MODE 0x0A

/* State layout version; a matching record resumes after a stock task. */
#define RADIO_VERSION 2
/* Stock task ids at XDATA 0x3AB8 (bank 11 switch at 0x1BE4): 1 = music, 3 = radio, 0x13 = power off. */
#define TASK_ID 0x3AB8
#define HANDOFF_MUSIC 1
#define HANDOFF_POWEROFF 0x13
#define MSG_NO_MEDIA 1
#define MENU_ITEMS 11
/* Stock music-player EQ preset (XDATA 0x3B6A, saved setting 0x0D): 0 NORMAL, 1 POP,
   2 ROCK, 3 JAZZ, 4 CLASSIC, 5 COUNTRY, 6 BASS. The DAC's hardware EQ filters decoded
   playback only; the radio's analogue AUX path does not pass through it. */
#define STOCK_EQ 0x3B6A
#define EQ_PRESETS 7

/* Two empty stock 2 KiB application banks, entered through the stock banked-call
   trampoline at 0x0133 and left through 0x0100. Six-byte entries at PC 0x1800.
   bank_call takes (bank << 8) | entry offset. */
#define MENU_BANK 0x2C
#define SYSTEM_BANK 0x2D
#define BANK_MENU_KEY ((MENU_BANK<<8)|0)
#define BANK_MENU_DRAW ((MENU_BANK<<8)|6)
#define BANK_ENTER ((SYSTEM_BANK<<8)|0)
#define BANK_LEAVE ((SYSTEM_BANK<<8)|6)
#define BANK_HANDOFF ((SYSTEM_BANK<<8)|12)

#ifndef RADIO_STATE
#define RADIO_STATE 0x2380
#endif
#ifndef RADIO_PAGE
#define RADIO_PAGE 0x2300
#endif
#ifdef RADIO_MENU
#define RADIO_LABEL "RETEKESS OPEN 0.3.0"
#define RADIO_POWER_HINT "MENU SETUP  MODE MP3"
#elif defined(RADIO_AUTONOMOUS)
#define RADIO_LABEL "RETEKESS OPEN 0.2.6"
#define RADIO_POWER_HINT "MENU LCD  POWER STBY"
#else
#define RADIO_LABEL "RETEKESS OPEN 0.1"
#define RADIO_POWER_HINT "MENU LCD  POWER EXIT"
#endif

/* FM frequency is in 25 kHz units; AM frequency is in kHz. */
struct Radio {
    u8 magic[4];
    u8 version, mode, volume, reg7;
    u16 frequency, entry;
    u8 digits, editing, sleeping, muted;
    u8 error, cn, status, ticks;
    u8 adc1, adc2, candidate, debounce, held, repeat;
    u8 dirty, last_key, injected_key, command;
    u16 frames, events, tunes, read_channel, target_channel;
    u8 pending, settle, entry_bad;
    u8 page_crc[8];
    u16 band_frequency[3];
    /* reserved[0..1] count timer ticks; the menu fields use former reserved bytes,
       so OPEN-0.2 offsets (and the timer-preview build) are unchanged. */
    u8 standby, reserved[9], stereo, eq, menu, item, handoff, message;
};
extern __xdata __at(RADIO_STATE) struct Radio radio;
extern __xdata __at(RADIO_PAGE) u8 page[128];
extern __xdata u8 cursor;

void clear(void);
void character(u8 c);
void text(const __code char *s);
void number(u16 value,u8 places);
void tune(void);
void audio(void);
void bank_call(u16 entry);
void lcd_page(u8 row);
void lcd_power(u8 on);
void tuner_write(u8 reg, u8 value);
void hardware_volume(u8 level);
void hardware_amplifier(u8 enabled);

u8 tuner_read(u8 reg);
void tuner_write(u8 reg, u8 value);
void hardware_init(void);
void hardware_audio(void);
void hardware_volume(u8 level);
void hardware_amplifier(u8 enabled);
u8 hardware_key(void);
void lcd_page(u8 row);
void lcd_power(u8 on);
void app_init(void);
void app_tick(void);
void app_stop(void);

#endif
