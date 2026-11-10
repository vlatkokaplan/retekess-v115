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

#ifndef RADIO_STATE
#define RADIO_STATE 0x2380
#endif
#ifndef RADIO_PAGE
#define RADIO_PAGE 0x2300
#endif
#ifdef RADIO_AUTONOMOUS
#define RADIO_LABEL "RETEKESS OPEN 0.2.3"
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
    u8 standby, reserved[15];
};
extern __xdata __at(RADIO_STATE) struct Radio radio;
extern __xdata __at(RADIO_PAGE) u8 page[128];

u8 tuner_read(u8 reg);
void tuner_write(u8 reg, u8 value);
void hardware_init(void);
void hardware_audio(void);
void hardware_amplifier(u8 enabled);
u8 hardware_key(void);
void lcd_page(u8 row);
void lcd_power(u8 on);
void app_init(void);
void app_tick(void);
void app_stop(void);

#endif
