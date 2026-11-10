#!/usr/bin/env python3
"""Assemble native V115 diagnostics, display sleep and FM audio controls.

Offline only. Common glue/state use the checked common-code tail; nine wholly
zero application banks hold control, diagnostics, settings, history and scanning.
No USB access or flash writer.
"""
from pathlib import Path
import hashlib

from lcd_draw import Asm
from bandwidth_patch import farcall

COMMON_PC = 0x16EC
STATE = 0x1780
STATE_SIZE = 128
CONTROL_BANK = 0x33
DISPLAY_BANK = 0x34
CONTROL_FLASH = 0x3000 + CONTROL_BANK * 0x800
DISPLAY_FLASH = 0x3000 + DISPLAY_BANK * 0x800
FLAGS, BACKUP, ACTIVE, DIRTY, MODE = range(5)
METRICS = 8  # registers 20..27
REG7, FREQ = 16, 17
CN, RF, RFGAIN, IFGAIN, VOLTAGE = 19, 20, 21, 22, 23
CURSOR = 25
VERSION = '1.5-RC6'
SETTINGS_BANK, MENU_BANK = 0x32, 0x31
PREF_REG, TIMEOUT, IDLE, NV_DIRTY = 32, 33, 34, 35
PREF_RECORD, SAVE_TICKS, AUTO_DUE, ACTION = 37, 120, 121, 122
MENU, HELP = 4, 8
LONG_MENU = 0xBE
LONG_BW, LONG_DIAG, LONG_MONO, LONG_SCREEN = range(0xB7,0xBB)
LONG_DE, LONG_BASS = 0xBC, 0xBD  # keep stock hold-5 numeric-entry behavior
SHORT_1, SHORT_2, SHORT_3 = 0x97, 0x98, 0x99
SHORT_4, SHORT_5, SHORT_6 = 0x9A, 0x9B, 0x9C
BL_COUNT, BL_LEVEL, WAKE_BLOCK = 27, 28, 29
LAST_KEY = 30
DISPLAY_SLEEP = 2
GRAPH = 0x10
SCAN_UI = 0x20
UI_ACTIVE, UI_PAINT = 5, 6
GRAPH_HEAD, GRAPH_COUNT, GRAPH_PEAK = 53, 54, 55
GRAPH_FREQ, GRAPH_MODE, GRAPH_PENDING = 56, 58, 59
FONT_SCALE = 60
HISTORY = 64
HISTORY_SIZE = 32
GRAPH_BANK, GRAPH_DISPLAY_BANK, HOME_BANK = 0x30, 0x2F, 0x2E
SCAN_BANK, SCAN_DISPLAY_BANK = 0x2D, 0x2C
SCAN_STATUS, SCAN_DIRECTION, SCAN_THRESHOLD, SCAN_DWELL, SCAN_RANGE = 96, 97, 98, 99, 100
SCAN_LOW, SCAN_HIGH, SCAN_START = 101, 103, 105
SCAN_STEP, SCAN_MODE, SCAN_TICK, SCAN_WAIT, SCAN_REMAIN, SCAN_CN = 107, 108, 109, 110, 111, 112
SCAN_ACTION, SCAN_SETTLE, SCAN_FAILED = 113, 114, 115
PREF_SIZE = 16


def jump(a, label):
    a.emit(2)
    a._abs(label)


def jnz(a, label):
    a.emit(0x70)
    a._rel(label)


def read(a, addr):
    a.movdptr(addr)
    a.emit(0xE0)


def write(a, addr, value):
    a.movdptr(addr)
    a.movai(value)
    a.movxda()


def read_tuner_register(a, register):
    # Stock 1d:1d3e sends a NACK after every byte (1d78 -> 1aab).
    # Only the first returned byte is valid: request one register per call.
    a.movri(1,register);a.movri(2,1)
    farcall(a,0x1D,0x1D3E)


def save_context(a):
    for reg in (0xE0, 0xD0, 0x82, 0x83, 0xF0, *range(8)):
        a.push(reg)


def restore_context(a):
    for reg in (*range(7, -1, -1), 0xF0, 0x83, 0x82, 0xD0, 0xE0):
        a.pop(reg)


def common_glue():
    a = Asm(COMMON_PC)
    labels = {}

    def call_control(entry):
        farcall(a, CONTROL_BANK, 0x1800 + entry * 3)

    def protect_call(entry):
        a.push(0xE0)
        a.movai(entry*3)
        a.lcall('protected_control')
        a.pop(0xE0)

    labels['init'] = a.base + len(a.code)
    call_control(0)
    a.movai(1)
    a.movdptr(0x3CD1)
    a.ret()  # reproduces displaced MOV A,#1; MOV DPTR,#3cd1

    labels['wait'] = a.base + len(a.code)
    protect_call(1)
    a.emit(0x12, 0x20, 0xB1)
    a.ret()

    labels['message'] = a.base + len(a.code)
    protect_call(2)
    a.emit(0x02, 0x42, 0xDB)  # original key handler, with selected R1

    labels['draw'] = a.base + len(a.code)
    protect_call(3)
    a.ret()

    a.L('protected_control')
    for reg in (0xD0,0x82,0x83):a.push(reg)
    a.movda(0x82);a.movdi(0x83,0x18)
    a.movai(CONTROL_BANK);a.emit(0x12,0x01,0x33)
    for reg in (0x83,0x82,0xD0):a.pop(reg)
    a.ret()

    # Patched into bank 2 lcd_update's six-byte prologue. Carry clear takes
    # its existing out-of-range return branch, without starting SPI or DMA.
    labels['lcd_guard'] = a.base + len(a.code)
    for reg in (0xE0, 0xD0, 0x82, 0x83):
        a.push(reg)
    read(a, STATE + FLAGS)
    a.anlai(DISPLAY_SLEEP)
    jnz(a,'lcd_blocked')
    read(a,STATE+UI_ACTIVE);a.jz('lcd_awake')
    a.emit(0xA3,0xE0);jnz(a,'lcd_awake')
    a.L('lcd_blocked')
    for reg in (0x83, 0x82, 0xD0, 0xE0):
        a.pop(reg)
    a.setb(0xB1);a.clr(0xD7);a.ret()
    a.L('lcd_awake')
    for reg in (0x83, 0x82, 0xD0, 0xE0):a.pop(reg)
    a.setb(0xB1);a.movar(2);a.emit(0xC3,0x94,8)
    a.ret()
    code = a.assemble()
    if a.base + len(code) > STATE:
        raise ValueError('common glue overlaps its allocated state')
    return code, labels


def control(base=0x1800, state=STATE):
    a = Asm(base)
    for name in ('init', 'before_wait', 'message', 'draw'):
        jump(a, name)

    def get(field):
        read(a, state + field)

    def put(field, value):
        write(a, state + field, value)

    def bank_return():
        a.emit(0x02, 0x01, 0x00)

    def dirty():
        put(DIRTY, 1)

    a.L('init')
    save_context(a)
    a.movdptr(state)
    a.movri(0, STATE_SIZE)
    a.movai(0)
    a.L('init_zero')
    a.movxda()
    a.emit(0xA3)
    a.djnz(0, 'init_zero')
    farcall(a,SETTINGS_BANK,0x1800)
    put(GRAPH_PENDING,1);dirty()
    restore_context(a)
    bank_return()

    a.L('draw')
    save_context(a)
    a.lcall('safe');a.jnc('draw_stock')
    read(a,0x3CD1);a.jz('draw_idle');dirty()
    a.L('draw_idle');a.lcall('paint');a.sjmp('draw_done')
    a.L('draw_stock');a.lcall('release_ui');a.emit(0x12,0x41,0x84)
    a.L('draw_done');restore_context(a);bank_return()

    a.L('release_ui')
    put(SCAN_STATUS,0)
    a.L('yield_native')
    get(UI_ACTIVE);a.jz('release_ui_done')
    put(UI_ACTIVE,0);write(a,0x3CD1,1)
    a.L('release_ui_done');a.ret()

    a.L('safe')
    # Stock radio task=3, no scanning/submode, real FM/MW/SW playback mode.
    read(a, 0x3AB8)
    a.cjne_ai(3, 'unsafe')
    read(a, 0x3CD0)
    jnz(a, 'unsafe')
    read(a,0x3CF7)  # native numeric/preset prompt countdown
    jnz(a,'unsafe')
    a.movri(0, 0x18)
    a.emit(0xE6)
    a.jz('unsafe')
    a.emit(0xC3, 0x94, 4)
    a.jnc('unsafe')
    a.setb(0xD7)
    a.ret()
    a.L('unsafe')
    a.clr(0xD7)
    a.ret()

    a.L('before_wait')
    save_context(a)
    get(FLAGS);a.anlai(DISPLAY_SLEEP);a.jz('wait_awake')
    a.lcall('safe');a.jnc('wait_dark_unsafe')
    # Keep the stock backlight timer/latch off while the display sleeps.
    write(a,0x3B71,0);write(a,0x3B72,0)
    a.anldi(0xBB,0xDF);a.setb(0x95)
    jump(a,'wait_done')
    a.L('wait_dark_unsafe')
    a.lcall('wake_screen')
    get(FLAGS);a.anlai(0xC2);a.movxda()
    jump(a,'wait_done')
    a.L('wait_awake')
    a.lcall('safe');a.jnc('wait_unsafe')
    farcall(a,SETTINGS_BANK,0x1803)
    get(FLAGS);a.anlai(SCAN_UI);a.jz('wait_no_scan')
    farcall(a,SCAN_BANK,0x1803)
    a.L('wait_no_scan');a.lcall('paint')
    a.sjmp('wait_done')
    a.L('wait_unsafe')
    a.lcall('release_ui')
    get(FLAGS);a.anlai(0xC2);a.movxda()
    a.L('wait_done')
    restore_context(a);bank_return()

    a.L('paint')
    get(FLAGS);a.anlai(DISPLAY_SLEEP);a.jz('paint_awake');a.ret()
    a.L('paint_awake');put(UI_ACTIVE,1)
    get(DIRTY);a.jz('paint_done')
    put(DIRTY,0);write(a,0x3CD1,0);a.lcall('sample')
    farcall(a,GRAPH_BANK,0x1800)
    put(UI_PAINT,1)
    get(FLAGS);a.anlai(SCAN_UI);a.jz('paint_not_scan')
    farcall(a,SCAN_DISPLAY_BANK,0x1800);jump(a,'paint_finished')
    a.L('paint_not_scan');get(FLAGS);a.anlai(MENU);a.jz('paint_radio')
    farcall(a,MENU_BANK,0x1800);a.sjmp('paint_finished')
    a.L('paint_radio');get(FLAGS);a.anlai(1);a.jz('paint_home')
    get(FLAGS);a.anlai(GRAPH);a.jz('paint_meter')
    farcall(a,GRAPH_DISPLAY_BANK,0x1800);a.sjmp('paint_finished')
    a.L('paint_meter');farcall(a,DISPLAY_BANK,0x1800);a.sjmp('paint_finished')
    a.L('paint_home');farcall(a,HOME_BANK,0x1800)
    a.L('paint_finished');put(UI_PAINT,0)
    a.L('paint_done');a.ret()

    a.L('message')
    save_context(a)
    a.movdptr(state+26);a.movar(1);a.movxda()
    # Consume an entire key gesture used to enter sleep or wake the display.
    # Timer messages keep flowing, and system messages wake transparently.
    get(WAKE_BLOCK);a.jz('not_blocked')
    get(LAST_KEY);a.emit(0xFA);a.movar(1);a.anlai(0x1F);a.emit(0x6A)
    jnz(a,'blocked_other')
    a.lcall('release_block');jump(a,'consume_dirty')
    a.L('blocked_other')
    a.lcall('physical');a.jnc('not_blocked')
    a.lcall('release_block')
    jump(a,'consume_dirty')
    a.L('not_blocked')
    get(FLAGS);a.anlai(DISPLAY_SLEEP);a.jz('awake_dispatch')
    a.movar(1);a.jz('sleep_idle')
    a.cjne_ai(0xF8,'sleep_fe');jump(a,'message_done')
    a.L('sleep_fe');a.cjne_ai(0xFE,'sleep_wake');jump(a,'message_done')
    a.L('sleep_idle');jump(a,'message_done')
    a.L('sleep_wake')
    a.lcall('wake_screen')
    a.lcall('physical');a.jnc('awake_dispatch')
    put(WAKE_BLOCK,1)
    a.movar(1);a.anlai(0x1F);a.movdptr(state+LAST_KEY);a.movxda()
    a.lcall('release_block')
    jump(a,'consume_dirty')
    a.L('awake_dispatch')
    a.movar(1);a.cjne_ai(0xFE,'not_graph_tick');put(GRAPH_PENDING,1)
    a.L('not_graph_tick')
    a.movar(1);a.cjne_ai(0xFE,'not_home_tick');dirty()
    a.L('not_home_tick')
    a.lcall('safe');a.emit(0x40);a._rel('dispatch_safe');jump(a,'message_done')
    a.L('dispatch_safe')
    farcall(a,SETTINGS_BANK,0x1806)
    get(ACTION);a.jz('settings_pass')
    for action,label in [(1,'bandwidth'),(2,'mono'),(3,'bass'),(4,'deemphasis'),(5,'sleep_screen'),(6,'toggle_view'),(7,'consume_dirty')]:
        nxt='action_'+str(action)
        a.cjne_ai(action,nxt);jump(a,label);a.L(nxt)
    a.L('settings_pass')
    a.movri(0,0x8C);a.emit(0xE6);jnz(a,'dispatch_reject')
    a.movar(1)
    for key,label in [(LONG_DIAG,'toggle_view'),(LONG_BW,'bandwidth'),
                      (LONG_MONO,'mono'),(LONG_SCREEN,'sleep_screen'),
                      (LONG_BASS,'bass'),(LONG_DE,'deemphasis')]:
        next_label='key_'+str(key)
        a.cjne_ai(key,next_label);jump(a,label);a.L(next_label)
    get(FLAGS);a.anlai(1);a.jz('dispatch_reject')
    a.movar(1);a.cjne_ai(0x9D,'graph_clear_key');jump(a,'toggle_graph')
    a.L('graph_clear_key');a.cjne_ai(0x96,'diagnostic_keys')
    get(FLAGS);a.anlai(GRAPH);a.jz('diagnostic_keys');jump(a,'clear_graph')
    a.L('diagnostic_keys');a.movar(1)
    for key,label in [(SHORT_1,'bandwidth'),(SHORT_2,'mono'),
                      (SHORT_3,'toggle_view'),(SHORT_4,'sleep_screen'),
                      (SHORT_5,'bass'),(SHORT_6,'deemphasis')]:
        next_label='key_'+str(key)
        a.cjne_ai(key,next_label);jump(a,label);a.L(next_label)
    a.cjne_ai(0xF8,'periodic_fe');jump(a,'mark_dirty')
    a.L('periodic_fe');a.cjne_ai(0xFE,'other_key');jump(a,'mark_dirty')
    a.L('dispatch_reject');jump(a,'message_done')
    a.L('other_key')
    a.movar(1);a.emit(0xC3,0x94,0x80);a.jnc('other_key_up')
    jump(a,'message_done')
    a.L('other_key_up')
    a.movar(1);a.emit(0xC3,0x94,0xC0);a.jnc('dispatch_reject')
    get(FLAGS);a.anlai(0xC2);a.movxda()
    write(a,0x3CD1,1);jump(a,'message_done')

    a.L('physical')
    a.movar(1);a.jz('not_physical')
    a.emit(0xC3,0x94,0xE0);a.jnc('not_physical')
    a.setb(0xD7);a.ret()
    a.L('not_physical');a.clr(0xD7);a.ret()
    a.L('release_block')
    a.movar(1);a.anlai(0xE0)
    a.cjne_ai(0x80,'release_long');put(WAKE_BLOCK,0);a.ret()
    a.L('release_long');a.cjne_ai(0xC0,'release_done');put(WAKE_BLOCK,0)
    a.L('release_done');a.ret()

    a.L('sleep_screen')
    read(a,0x3B71);a.movdptr(state+BL_COUNT);a.movxda()
    a.movad(0x90);a.anlai(0x20);a.movdptr(state+BL_LEVEL);a.movxda()
    get(FLAGS);a.orlai(DISPLAY_SLEEP);a.movxda()
    get(AUTO_DUE);a.jz('sleep_manual')
    put(AUTO_DUE,0);a.sjmp('sleep_commands')
    a.L('sleep_manual');put(WAKE_BLOCK,1)
    a.movar(1);a.anlai(0x1F);a.movdptr(state+LAST_KEY);a.movxda()
    a.lcall('release_block')
    a.L('sleep_commands')
    write(a,0x3B71,0);write(a,0x3B72,0)
    a.anldi(0xBB,0xDF);a.setb(0x95)
    for command in (0xAE,0x28):
        a.movri(1,command);farcall(a,2,0x187E)
    jump(a,'consume_dirty')
    a.L('wake_screen')
    a.push(1)
    get(FLAGS);a.anlai(0xFD);a.movxda()
    for command in (0x2F,0xAF):
        a.movri(1,command);farcall(a,2,0x187E)
    get(BL_COUNT);a.movdptr(0x3B71);a.movxda()
    get(BL_LEVEL);a.jz('wake_light_on')
    a.setb(0x95);write(a,0x3B72,0);a.sjmp('wake_light_done')
    a.L('wake_light_on');a.clr(0x95);write(a,0x3B72,1)
    a.L('wake_light_done')
    write(a,0x3CD1,1);dirty();a.pop(1);a.ret()

    a.L('toggle_graph')
    get(FLAGS);a.emit(0x64,GRAPH);a.movxda();jump(a,'consume_dirty')
    a.L('clear_graph')
    farcall(a,GRAPH_BANK,0x1803);jump(a,'consume_dirty')
    a.L('toggle_view')
    get(FLAGS);a.emit(0x64,1);a.movxda()
    write(a,0x3CD1,1);jump(a,'consume_dirty')

    a.L('bandwidth')
    a.movri(0,0x18);a.emit(0xE6);a.cjne_ai(1,'fm_reject')
    farcall(a,0x1D,0x1EB0)
    farcall(a,SETTINGS_BANK,0x1809);jump(a,'consume_dirty')
    a.L('fm_reject')
    # Unhandled digit keys leave diagnostics before stock numeric entry.
    jump(a,'other_key')

    a.L('mono')
    a.movri(0,0x18);a.emit(0xE6);a.cjne_ai(1,'fm_reject')
    read(a,0x3B3A);a.emit(0xFA);a.anlai(4);a.jz('force_mono')
    a.movar(2);a.anlai(0xF3);a.sjmp('write_reg7')
    a.L('force_mono');a.movar(2);a.anlai(0xF3);a.orlai(4);a.sjmp('write_reg7')
    a.L('bass')
    a.movri(0,0x18);a.emit(0xE6);a.cjne_ai(1,'fm_reject')
    read(a,0x3B3A);a.emit(0x64,0x10);a.sjmp('write_reg7')
    a.L('deemphasis')
    a.movri(0,0x18);a.emit(0xE6);a.cjne_ai(1,'fm_reject')
    read(a,0x3B3A);a.emit(0x64,0x20)
    a.L('write_reg7')
    a.movxda();a.emit(0xFA)
    a.movai(1);a.push(0xE0)
    a.movri(1,7)
    for reg in (3,4,5):a.movri(reg,0)
    farcall(a,0x1D,0x1D17);a.pop(0xE0)
    farcall(a,SETTINGS_BANK,0x1809)
    jump(a,'consume_dirty')

    a.L('consume_dirty');put(26,0)
    a.L('mark_dirty');dirty()
    a.L('message_done')
    # Native frequency/volume dialogs run inside the stock key handler and
    # may block there while drawing. Yield before dispatch, not on its return.
    get(26);a.jz('message_restore')
    a.cjne_ai(0xF8,'yield_not_half_tick');a.sjmp('message_restore')
    a.L('yield_not_half_tick');a.cjne_ai(0xFE,'yield_message');a.sjmp('message_restore')
    a.L('yield_message')
    a.lcall('yield_native');dirty()
    a.L('message_restore')
    restore_context(a)
    a.push(0xE0);a.push(0x82);a.push(0x83)
    read(a,state+26);a.emit(0xF9)
    a.pop(0x83);a.pop(0x82);a.pop(0xE0);bank_return()

    a.L('sample')
    for reg in range(20,28):
        read_tuner_register(a,reg)
        a.movdptr(state+METRICS+reg-20);a.movar(2);a.movxda()
    a.movri(0, 0x18)
    a.emit(0xE6)
    a.movdptr(state + MODE)
    a.movxda()
    read(a, 0x3B3A)
    a.movdptr(state + REG7)
    a.movxda()
    read(a, 0x3CDC)
    a.emit(0xFA, 0xA3, 0xE0, 0xFB)
    a.movdptr(state + FREQ)
    a.movar(2)
    a.movxda()
    a.emit(0xA3)
    a.movar(3)
    a.movxda()
    get(MODE)
    a.cjne_ai(1, 'cn_am')
    get(METRICS + 3)
    a.sjmp('cn_store')
    a.L('cn_am')
    get(METRICS + 2)
    a.L('cn_store')
    a.anlai(127)
    a.movdptr(state + CN)
    a.movxda()
    get(METRICS + 4)
    a.emit(0xC4, 0x03)
    a.anlai(7)
    a.emit(0xFA)
    a.movdptr(state + RFGAIN)
    a.movxda()
    get(METRICS + 4)
    a.emit(0x03, 0x03)
    a.anlai(7)
    a.emit(0xFB)
    a.movdptr(state + IFGAIN)
    a.movxda()
    a.movar(2)
    a.emit(0x2B)
    a.movdi(0xF0, 6)
    a.emit(0xA4, 0xFA)
    get(METRICS + 7)
    a.anlai(127)
    a.emit(0x2A, 0xFA)
    get(MODE)
    a.cjne_ai(2, 'rf_fm_sw')
    a.movai(123)
    a.sjmp('rf_sub')
    a.L('rf_fm_sw')
    a.movai(103)
    a.L('rf_sub')
    a.emit(0xC3, 0x9A)
    a.movdptr(state + RF)
    a.movxda()
    get(METRICS + 5)
    a.anlai(63)
    a.movdi(0xF0, 5)
    a.emit(0xA4, 0x24, 180, 0xFA)
    a.movad(0xF0)
    a.emit(0x34, 0, 0xFB)
    a.movdptr(state + VOLTAGE)
    a.movar(2)
    a.movxda()
    a.emit(0xA3)
    a.movar(3)
    a.movxda()
    a.ret()
    code = a.assemble()
    if len(code) > 0x800:
        raise ValueError(f'control bank exceeds 2 KiB: {len(code)}')
    return code, a.labels


def font_bytes():
    data = (Path(__file__).resolve().parent/'assets/font-5x8.bin').read_bytes()
    if len(data)!=475 or hashlib.sha256(data).hexdigest()!='055245e1abd806b06bfde14ff20ccaedb3525f3c9e83d1dacd812e427adb3363':
        raise ValueError('native diagnostic font asset is missing or changed')
    return data


def renderer(base=0x1800, state=STATE, line_update=None, menu_screen=False):
    a = Asm(base)
    strings = {}

    def text(value):
        name = f'string{len(strings)}'
        strings[name] = value
        a.emit(0x90)
        a._abs(name)
        a.lcall('text')

    def get(field):
        read(a, state + field)

    def number(field, places=0, word=False, signed=False):
        get(field)
        if signed:
            a.lcall('signed8')
        else:
            a.emit(0xFA)
            if word:
                a.emit(0xA3, 0xE0, 0xFB)
            else:
                a.movri(3, 0)
        a.movri(4, places)
        a.lcall('number')

    def pair_start():
        a.movdptr(0x2800)
        a.movri(0, 0)
        a.movai(0)
        name = f'clear{len(a.code)}'
        a.L(name)
        a.movxda()
        a.emit(0xA3)
        a.djnz(0, name)
        write(a, state + CURSOR, 0)

    def next_row():
        write(a, state + CURSOR, 128)

    def flush(page):
        a.movri(1, page)
        if line_update is None:
            farcall(a, 2, 0x1996)
        else:
            a.emit(0x12, line_update >> 8, line_update & 255)

    save_context(a)
    if menu_screen:
        get(FLAGS);a.anlai(HELP);jnz(a,'help_screen');jump(a,'settings_screen')
        a.L('help_screen')
        help_lines=['KEYS / '+VERSION,'HOLD 1:FM BANDWIDTH','HOLD 2:SIGNAL DISPLAY',
                    'HOLD 3:FM AUTO/MONO','HOLD 4:SCREEN OFF','HOLD 6:DE 7:BASS',
                    'HOLD 8:SETTINGS','9:BACK 0:SAVE/EXIT']
        for page in range(0,8,2):
            pair_start();text(help_lines[page]);next_row();text(help_lines[page+1]);flush(page)
        jump(a,'render_done')
        a.L('settings_screen')
        pair_start();text('SETTINGS '+VERSION)
        next_row();text('1 FM BW:')
        get(PREF_REG);a.anlai(3);a.movdptr(0);width_patch=len(a.code)-2
        a.emit(0x93,0xFA);a.movri(3,0);a.movri(4,0);a.lcall('number');text('KHZ');flush(0)
        pair_start();text('2 ');get(PREF_REG);a.anlai(4);a.jz('menu_auto')
        text('MONO');a.sjmp('menu_mono_done');a.L('menu_auto');text('AUTO')
        a.L('menu_mono_done');text(' 3 BASS:');get(PREF_REG);a.anlai(16);a.jz('menu_bass_off')
        text('ON');a.sjmp('menu_bass_done');a.L('menu_bass_off');text('OFF');a.L('menu_bass_done')
        next_row();text('4 DE-EMPHASIS:');get(PREF_REG);a.anlai(32);a.jz('menu_de75')
        text('50US');a.sjmp('menu_de_done');a.L('menu_de75');text('75US');a.L('menu_de_done');flush(2)
        pair_start();text('5 SCREEN SLEEP:');get(TIMEOUT);a.jz('menu_timeout_off')
        a.movdptr(0);timeout_patch=len(a.code)-2;a.emit(0x93,0xFA)
        a.movri(3,0);a.movri(4,0);a.lcall('number');text('S');a.sjmp('menu_timeout_done')
        a.L('menu_timeout_off');text('OFF');a.L('menu_timeout_done')
        next_row();text('6 SCREEN OFF 7 SIGNAL');flush(4)
        pair_start();text('0 SAVE/EXIT 9 HELP');next_row()
        get(NV_DIRTY);a.jz('menu_saved');text('CHANGES NOT SAVED');a.sjmp('menu_save_done')
        a.L('menu_saved');text('SAVED / NO CHANGES');a.L('menu_save_done');flush(6)
        table=a.base+len(a.code)+3
        jump(a,'menu_tables_done')
        a.code.extend(bytes([0,10,30,60]));a.code[timeout_patch:timeout_patch+2]=table.to_bytes(2,'big')
        a.L('menu_tables_done')
    else:
        pair_start()
        get(MODE)
        a.cjne_ai(1, 'header_am')
        text('FM ')
        number(FREQ, 1, word=True)
        text('MHZ')
        a.sjmp('header_done')
        a.L('header_am')
        a.cjne_ai(2, 'header_sw')
        text('MW ')
        a.sjmp('header_am_number')
        a.L('header_sw')
        text('AM ')
        a.L('header_am_number')
        number(FREQ, word=True)
        text('KHZ')
        a.L('header_done')
        next_row()
        text('C/N:')
        number(CN)
        text('DB ')
        get(METRICS)
        a.anlai(0x20)
        a.jz('no_lock')
        text('LOCK')
        a.sjmp('lock_done')
        a.L('no_lock')
        text('----')
        a.L('lock_done')
        flush(0)

        pair_start()
        text('RF~')
        number(RF, signed=True)
        text('DBUV R:')
        get(METRICS + 7)
        a.anlai(127)
        a.emit(0xFA)
        a.movri(3, 0)
        a.movri(4, 0)
        a.lcall('number')
        next_row()
        text('OFF:')
        get(METRICS + 6)
        a.lcall('signed8')
        a.movri(4, 0)
        get(MODE)
        a.cjne_ai(1, 'offset_am')
        a.sjmp('offset_number')
        a.L('offset_am')
        a.movri(4, 1)
        a.L('offset_number')
        a.lcall('number')
        text('KHZ ST:')
        get(MODE)
        a.cjne_ai(1, 'stereo_am')
        get(METRICS + 3)
        a.movcb(0xE7)
        a.movai(ord('0'))
        a.emit(0x34, 0)
        a.lcall('char')
        a.sjmp('stereo_done')
        a.L('stereo_am')
        text('-')
        a.L('stereo_done')
        flush(2)

        pair_start()
        text('IC:')
        number(VOLTAGE, 2, word=True)
        text('V G:')
        number(RFGAIN)
        text('/')
        number(IFGAIN)
        next_row()
        get(MODE)
        a.cjne_ai(1, 'band_am')
        text('BW:')
        get(REG7)
        a.anlai(3)
        a.movdptr(0)
        width_patch = len(a.code) - 2
        a.emit(0x93, 0xFA)
        a.movri(3, 0)
        a.movri(4, 0)
        a.lcall('number')
        text('KHZ ')
        get(REG7)
        a.anlai(4)
        a.jz('auto')
        text('MONO')
        a.sjmp('band_done')
        a.L('auto')
        text('AUTO')
        a.sjmp('band_done')
        a.L('band_am')
        text('AM SIGNAL MONITOR')
        a.L('band_done')
        flush(4)

        pair_start()
        get(MODE);a.cjne_ai(1,'settings_am')
        text('BASS:')
        get(REG7);a.anlai(0x10);a.jz('bass_off')
        text('ON');a.sjmp('bass_done')
        a.L('bass_off');text('OFF')
        a.L('bass_done');text(' DE:')
        get(REG7);a.anlai(0x20);a.jz('de_50')
        text('50US');a.sjmp('settings_done')
        a.L('de_50');text('75US');a.sjmp('settings_done')
        a.L('settings_am');text('FW '+VERSION+' 4:OFF')
        a.L('settings_done')
        next_row()
        get(MODE)
        a.cjne_ai(1, 'footer_am')
        text('3 EXIT 7 GRAPH 4 OFF')
        a.sjmp('footer_done')
        a.L('footer_am')
        text('3 EXIT 7 GRAPH 4 OFF')
        a.L('footer_done')
        flush(6)
    a.L('render_done')
    restore_context(a)
    a.L('bank_return')
    a.emit(0x02, 0x01, 0x00)

    a.L('signed8')
    a.movcb(0xE7)
    a.jnc('positive8')
    a.emit(0xF4, 0x04, 0xFA)
    a.movai(ord('-'))
    a.lcall('char')
    a.movar(2)
    a.L('positive8')
    a.emit(0xFA)
    a.movri(3, 0)
    a.ret()

    a.L('number')
    a.movri(7, 0)  # leading digit has been emitted
    for exponent in range(4, -1, -1):
        divisor = 10 ** exponent
        name = f'digit{exponent}'
        a.movri(6, 0)
        a.L(name)
        a.movar(2)
        a.emit(0xC3, 0x94, divisor & 255, 0xFD)  # low remainder candidate R5
        a.movar(3)
        a.emit(0x94, divisor >> 8)
        a.jnc(name + '_subtract')
        a.sjmp(name + '_ready')
        a.L(name + '_subtract')
        a.emit(0xFB)  # commit high remainder candidate
        a.movar(5)
        a.emit(0xFA)
        a.incr(6)
        a.sjmp(name)
        a.L(name + '_ready')
        a.movar(7)
        jnz(a, name + '_emit')
        a.movar(6)
        jnz(a, name + '_emit')
        # Emit at least one digit before the decimal point / integer end.
        a.movar(4)
        a.emit(0xC3, 0x94, exponent)
        a.jnc(name + '_emit')
        a.sjmp(name + '_next')
        a.L(name + '_emit')
        a.movri(7, 1)
        a.movar(6)
        a.emit(0x24, ord('0'))
        a.lcall('char')
        if exponent:
            a.movar(4)
            a.cjne_ai(exponent, name + '_next')
            a.movai(ord('.'))
            a.lcall('char')
        a.L(name + '_next')
    a.ret()

    a.L('text')
    a.movai(0)
    a.emit(0x93)
    a.jz('text_done')
    a.lcall('char')
    a.emit(0xA3)
    a.sjmp('text')
    a.L('text_done')
    a.ret()

    a.L('char')
    # Preserve all registers used by the string and numeric formatters.
    for reg in (0x82, 0x83, 0xF0, *range(8)):
        a.push(reg)
    a.emit(0x24, 256 - 32)
    a.movdi(0xF0, 5)
    a.emit(0xA4)
    a.movdptr(0)
    font_patch = len(a.code) - 2
    a.emit(0x25, 0x82, 0xF5, 0x82)
    a.movad(0xF0)
    a.emit(0x35, 0x83, 0xF5, 0x83)
    a.movri(5, 0)
    a.L('char_column')
    a.movai(0)
    a.emit(0x93, 0xFC, 0xA3)
    a.push(0x82)
    a.push(0x83)
    get(CURSOR)
    a.emit(0xF5, 0x82)
    a.movdi(0x83, 0x28)
    a.movar(4)
    a.movxda()
    get(CURSOR)
    a.emit(0x04)
    a.movxda()
    a.pop(0x83)
    a.pop(0x82)
    a.incr(5)
    a.movar(5)
    a.cjne_ai(5, 'char_column')
    get(CURSOR)
    a.emit(0x04)
    a.movxda()  # blank sixth column was already cleared
    for reg in (*range(7, -1, -1), 0xF0, 0x83, 0x82):
        a.pop(reg)
    a.ret()

    widths_pc = a.base + len(a.code)
    a.code.extend(bytes([150, 200, 50, 100]))
    a.code[width_patch:width_patch + 2] = widths_pc.to_bytes(2, 'big')
    font_pc = a.base + len(a.code)
    a.code.extend(font_bytes())
    a.code[font_patch:font_patch + 2] = font_pc.to_bytes(2, 'big')
    for name, value in strings.items():
        a.L(name)
        a.code.extend(value.encode('ascii') + b'\0')
    code = a.assemble()
    if len(code) > 0x800:
        raise ValueError(f'display bank exceeds 2 KiB: {len(code)}')
    return code, dict(a.labels, font=font_pc, widths=widths_pc)


def menu_renderer(base=0x1800,state=STATE,line_update=None):
    from radio_ui import settings_renderer
    return settings_renderer(base,state)


def settings(base=0x1800, state=STATE):
    """Use stock group-0 record bytes E0..EF and its journaled writer.

    Native callers use 00..CA in group 0. FM's 60 u16 slots end at CA;
    AM lists occupy other groups. Record CRC/magic remain stock-owned.
    FE is the 1-second event: 5 ms hook /20 -> /10 -> common 0177.
    """
    a=Asm(base)
    for name in ('prefs_init','prefs_poll','prefs_message','prefs_changed'):
        jump(a,name)
    def get(field):read(a,state+field)
    def put(field,value):write(a,state+field,value)
    def finish():restore_context(a);a.emit(0x02,0x01,0x00)
    def action(value):put(ACTION,value);jump(a,'prefs_message_done')

    a.L('prefs_init');save_context(a)
    read(a,0x3B3A);a.anlai(0x3F);a.movdptr(state+PREF_REG);a.movxda()
    put(SCAN_THRESHOLD,1);put(SCAN_DWELL,1);put(SCAN_DIRECTION,1);put(SCAN_RANGE,0)
    a.lcall('read_record');a.lcall('valid_record');a.jnc('prefs_init_done')
    get(PREF_RECORD+3);a.movdptr(state+PREF_REG);a.movxda()
    get(PREF_RECORD+4);a.movdptr(state+TIMEOUT);a.movxda()
    get(PREF_RECORD+2);a.cjne_ai(2,'prefs_init_done')
    for field,source in [(SCAN_THRESHOLD,8),(SCAN_DWELL,9),(SCAN_DIRECTION,10),(SCAN_RANGE,11)]:
        get(PREF_RECORD+source);a.movdptr(state+field);a.movxda()
    a.L('prefs_init_done');finish()

    a.L('prefs_poll');save_context(a)
    # Radio initialization rewrites register 7; reapply only while in FM.
    a.movri(0,0x18);a.emit(0xE6);a.cjne_ai(1,'prefs_poll_done')
    read(a,0x3B3A);a.anlai(0x3F);a.emit(0xFA)
    get(PREF_REG);a.emit(0x6A);a.jz('prefs_poll_done')
    read(a,0x3B3A);a.anlai(0xC0);a.emit(0xFA)
    get(PREF_REG);a.emit(0x4A);a.movdptr(0x3B3A);a.movxda();a.emit(0xFA)
    a.movai(1);a.push(0xE0);a.movri(1,7)
    for reg in (3,4,5):a.movri(reg,0)
    farcall(a,0x1D,0x1D17);a.pop(0xE0)
    put(DIRTY,1)
    a.L('prefs_poll_done');finish()

    a.L('prefs_changed');save_context(a)
    read(a,0x3B3A);a.anlai(0x3F);a.emit(0xFA)
    get(PREF_REG);a.emit(0x6A);a.jz('prefs_changed_done')
    a.movar(2);a.movdptr(state+PREF_REG);a.movxda()
    put(NV_DIRTY,1);put(SAVE_TICKS,0)
    a.L('prefs_changed_done');finish()

    a.L('prefs_message');save_context(a);put(ACTION,0)
    a.movar(1);a.cjne_ai(0xFE,'prefs_key')
    get(FLAGS);a.anlai(MENU);a.jz('timer_idle');jump(a,'prefs_message_done')
    a.L('timer_idle')
    get(NV_DIRTY);a.jz('timer_sleep')
    get(SAVE_TICKS);a.emit(0x04);a.movxda();a.cjne_ai(3,'timer_sleep')
    a.lcall('commit')
    a.L('timer_sleep')
    get(TIMEOUT);a.jz('timer_done');a.emit(0xFA)
    a.movdptr(0);timer_patch=len(a.code)-2;a.emit(0x93,0xFB)
    get(IDLE);a.emit(0x04);a.movxda();a.emit(0xC3,0x9B);a.jnc('auto_sleep')
    a.L('timer_done');jump(a,'prefs_message_done')
    a.L('auto_sleep')
    a.lcall('commit');put(AUTO_DUE,1);action(5)
    a.L('prefs_key')
    a.movar(1);jnz(a,'prefs_nonzero');jump(a,'prefs_message_done')
    a.L('prefs_nonzero')
    a.cjne_ai(0xF8,'prefs_activity');jump(a,'prefs_message_done')
    a.L('prefs_activity');put(IDLE,0)
    a.L('prefs_no_activity')
    a.movri(0,0x8C);a.emit(0xE6);a.jz('prefs_unlocked');jump(a,'prefs_message_done')
    a.L('prefs_unlocked')
    get(FLAGS);a.anlai(SCAN_UI);a.jz('prefs_not_scan')
    farcall(a,SCAN_BANK,0x1806)
    get(FLAGS);a.anlai(SCAN_UI);a.jz('scan_exit_save')
    get(ACTION);a.cjne_ai(5,'scan_dispatch_done')
    a.L('scan_exit_save');a.lcall('commit')
    a.L('scan_dispatch_done');jump(a,'prefs_message_done')
    a.L('prefs_not_scan');a.movar(1);a.cjne_ai(LONG_MENU,'menu_keys')
    get(FLAGS);a.anlai(MENU);a.jz('menu_open')
    a.lcall('close_menu');a.sjmp('menu_hold_block')
    a.L('menu_open')
    get(FLAGS);a.anlai(0xC2);a.orlai(MENU);a.movxda()
    a.L('menu_hold_block');put(WAKE_BLOCK,1);put(LAST_KEY,LONG_MENU&31);action(7)
    a.L('menu_keys')
    get(FLAGS);a.anlai(MENU);jnz(a,'menu_dispatch')
    # Save pending edits before manual screen sleep.
    a.movar(1);a.cjne_ai(LONG_SCREEN,'prefs_key_done');a.lcall('commit')
    a.L('prefs_key_done');jump(a,'prefs_message_done')
    a.L('menu_dispatch')
    a.movar(1)
    for key,name in [(0x96,'menu_exit'),(0x9F,'menu_help'),(0x9B,'menu_timeout'),
                     (0x9C,'menu_sleep'),(0x9D,'menu_signal'),(0x9E,'menu_scan')]:
        nxt='menu_key_'+str(key);a.cjne_ai(key,nxt);jump(a,name);a.L(nxt)
    get(FLAGS);a.anlai(HELP);a.jz('menu_audio');action(7)
    a.L('menu_audio')
    a.movar(1);a.emit(0xC3,0x94,0x97);a.jnc('menu_audio_range');a.sjmp('menu_audio_fm')
    a.L('menu_audio_range');a.emit(0xC3,0x94,4);a.jnc('menu_audio_fm')
    a.movri(0,0x18);a.emit(0xE6);a.cjne_ai(1,'menu_audio_am');a.sjmp('menu_audio_fm')
    a.L('menu_audio_am');action(7)
    a.L('menu_audio_fm');a.movar(1)
    for key,value in [(0x97,1),(0x98,2),(0x99,3),(0x9A,4)]:
        nxt='menu_audio_'+str(key);a.cjne_ai(key,nxt);action(value);a.L(nxt)
    # Consume digit press/hold/repeat/release within the menu. Other key-up
    # gestures close it before stock volume/tuning/mode handling.
    a.movar(1);a.anlai(31);a.emit(0xC3,0x94,0x16);a.jnc('menu_digit')
    a.movar(1);a.emit(0xC3,0x94,0x80);a.jnc('menu_other_up');jump(a,'prefs_message_done')
    a.L('menu_other_up');a.emit(0xC3,0x94,0x40);a.jnc('menu_other_system')
    a.lcall('close_menu')
    a.L('menu_other_system');jump(a,'prefs_message_done')
    a.L('menu_digit');action(7)
    a.L('menu_exit');a.lcall('close_menu');action(7)
    a.L('menu_help');get(FLAGS);a.emit(0x64,HELP);a.movxda();action(7)
    a.L('menu_timeout')
    get(FLAGS);a.anlai(HELP);a.jz('menu_timeout_edit');action(7)
    a.L('menu_timeout_edit')
    get(TIMEOUT);a.emit(0x04);a.anlai(3);a.movxda()
    put(NV_DIRTY,1);put(SAVE_TICKS,0);action(7)
    a.L('menu_scan');get(FLAGS);a.anlai(0xC2);a.orlai(MENU|SCAN_UI);a.movxda()
    farcall(a,SCAN_BANK,0x1800);action(7)
    a.L('menu_sleep');a.lcall('close_menu');action(5)
    a.L('menu_signal');a.lcall('close_menu');action(6)
    a.L('prefs_message_done');finish()

    a.L('close_menu');a.lcall('commit')
    get(FLAGS);a.anlai(0xC2);a.movxda();write(a,0x3CD1,1);put(DIRTY,1);a.ret()

    a.L('valid_record')
    for field,value in [(0,0x52),(1,0x33),(7,0xA5)]:
        get(PREF_RECORD+field);a.cjne_ai(value,'record_invalid')
    get(PREF_RECORD+3);a.emit(0xFA);a.anlai(0xC0);jnz(a,'record_invalid')
    get(PREF_RECORD+5);a.emit(0x6A);a.cjne_ai(0xFF,'record_invalid')
    get(PREF_RECORD+4);a.emit(0xFA,0xC3,0x94,4);a.jnc('record_invalid')
    get(PREF_RECORD+6);a.emit(0x6A);a.cjne_ai(0xFF,'record_invalid')
    get(PREF_RECORD+2);a.cjne_ai(1,'record_v2');a.setb(0xD7);a.ret()
    a.L('record_invalid');a.clr(0xD7);a.ret()
    a.L('record_v2');a.cjne_ai(2,'record_invalid')
    # V2 adds four independently complemented scan options. V1 migrates in RAM.
    for field,limit in [(8,4),(9,3),(10,2),(11,4)]:
        get(PREF_RECORD+field);a.emit(0xFA,0xC3,0x94,limit);a.jnc('record_invalid')
        get(PREF_RECORD+field+4);a.emit(0x6A);a.cjne_ai(0xFF,'record_invalid')
    a.setb(0xD7);a.ret()

    a.L('read_record')
    a.push(1);a.movri(7,0)
    a.L('read_record_byte')
    a.push(7)
    read(a,0x3BE2);a.emit(0xFB,0xA3,0xE0,0xFC);a.movri(5,0)
    a.movar(7);a.emit(0x24,0xE0,0xFA)
    a.emit(0x12,0x20,0x8A)  # spi_read8: address R2..R5, value R1
    a.pop(7);a.movar(7);a.emit(0x24,(state+PREF_RECORD)&255,0xF5,0x82)
    a.movdi(0x83,(state+PREF_RECORD)>>8);a.movar(1);a.movxda()
    a.incr(7);a.movar(7);a.cjne_ai(PREF_SIZE,'read_record_byte')
    a.pop(1);a.ret()

    a.L('commit');a.push(1)
    get(NV_DIRTY);jnz(a,'commit_check');jump(a,'commit_done')
    a.L('commit_check')
    # A round trip back to the stored choices requires no flash operation.
    a.lcall('valid_record');a.jnc('commit_write')
    get(PREF_RECORD+2);a.cjne_ai(2,'commit_write')
    get(PREF_RECORD+3);a.emit(0xFA);get(PREF_REG);a.emit(0x6A);jnz(a,'commit_write')
    get(PREF_RECORD+4);a.emit(0xFA);get(TIMEOUT);a.emit(0x6A);jnz(a,'commit_write')
    for field,source in [(SCAN_THRESHOLD,8),(SCAN_DWELL,9),(SCAN_DIRECTION,10),(SCAN_RANGE,11)]:
        get(PREF_RECORD+source);a.emit(0xFA);get(field);a.emit(0x6A);jnz(a,'commit_write')
    jump(a,'commit_clean')
    a.L('commit_write')
    for field,value in [(0,0x52),(1,0x33),(2,2),(7,0xA5)]:put(PREF_RECORD+field,value)
    for source,dest in [(PREF_REG,3),(TIMEOUT,4)]:
        get(source);a.movdptr(state+PREF_RECORD+dest);a.movxda()
        a.emit(0xF4);a.movdptr(state+PREF_RECORD+dest+2);a.movxda()
    for source,dest in [(SCAN_THRESHOLD,8),(SCAN_DWELL,9),(SCAN_DIRECTION,10),(SCAN_RANGE,11)]:
        get(source);a.movdptr(state+PREF_RECORD+dest);a.movxda();a.emit(0xF4)
        a.movdptr(state+PREF_RECORD+dest+4);a.movxda()
    a.movai(0);a.push(0xE0)  # stock fifth argument: group 0
    a.movri(1,0xE0);a.movri(2,(state+PREF_RECORD)&255);a.movri(3,(state+PREF_RECORD)>>8)
    a.movri(4,PREF_SIZE);a.movri(5,0);farcall(a,0x0E,0x1C84);a.pop(0xE0)
    a.lcall('read_record');a.lcall('valid_record');a.jnc('commit_retry')
    get(PREF_RECORD+3);a.emit(0xFA);get(PREF_REG);a.emit(0x6A);jnz(a,'commit_retry')
    get(PREF_RECORD+4);a.emit(0xFA);get(TIMEOUT);a.emit(0x6A);jnz(a,'commit_retry')
    for field,source in [(SCAN_THRESHOLD,8),(SCAN_DWELL,9),(SCAN_DIRECTION,10),(SCAN_RANGE,11)]:
        get(PREF_RECORD+source);a.emit(0xFA);get(field);a.emit(0x6A);jnz(a,'commit_retry')
    a.L('commit_clean');put(36,0);put(NV_DIRTY,0);a.sjmp('commit_done')
    a.L('commit_retry');put(36,1);put(SAVE_TICKS,0)
    a.L('commit_done');a.pop(1);a.ret()
    table=a.base+len(a.code);a.code.extend(bytes([0,10,30,60]))
    a.code[timer_patch:timer_patch+2]=table.to_bytes(2,'big')
    code=a.assemble()
    if len(code)>0x800:raise ValueError(f'settings bank exceeds 2 KiB: {len(code)}')
    return code,a.labels


def state_layout():
    spans={'flags':(0,1),'legacy_clock':(1,2),'dirty_mode':(3,2),'display_ownership':(5,2),
           'metrics':(8,8),'reg7':(16,1),'frequency':(17,2),'signal':(19,6),'cursor_message_backlight':(25,6),
           'preferences':(32,5),'record':(PREF_RECORD,PREF_SIZE),'graph':(GRAPH_HEAD,7),
           'font':(FONT_SCALE,1),'history':(HISTORY,HISTORY_SIZE),'scan':(SCAN_STATUS,24),
           'save_ticks':(SAVE_TICKS,1),'auto_sleep':(AUTO_DUE,1),'action':(ACTION,1)}
    occupied={}
    for name,(offset,size) in spans.items():
        if offset<0 or offset+size>STATE_SIZE:raise ValueError('state span outside common RAM: '+name)
        for byte in range(offset,offset+size):
            if byte in occupied:raise ValueError('overlapping state spans: '+name+' / '+occupied[byte])
            occupied[byte]=name
    return {name:dict(offset=offset,bytes=size) for name,(offset,size) in spans.items()}


def apply(plain):
    allocation=state_layout()
    result = bytearray(plain)
    glue, labels = common_glue()
    ctl, ctl_labels = control()
    disp, disp_labels = renderer()
    from signal_graph import history_control, graph_renderer
    hist,hist_labels=history_control()
    graph,graph_labels=graph_renderer()
    from radio_scan import scan_control,scan_renderer
    scanner,scan_labels=scan_control()
    scan_display,scan_display_labels=scan_renderer()
    from radio_ui import home_renderer
    home,home_labels=home_renderer()
    prefs,prefs_labels=settings()
    menu,menu_labels=menu_renderer()
    patches = [
        (0x3000+SCAN_BANK*0x800,bytes(0x800),scanner.ljust(0x800,b'\0'),'bounded scanner control bank'),
        (0x3000+SCAN_DISPLAY_BANK*0x800,bytes(0x800),scan_display.ljust(0x800,b'\0'),'scanner options renderer bank'),
        (0x3000+HOME_BANK*0x800,bytes(0x800),home.ljust(0x800,b'\0'),'large-frequency radio screen bank'),
        (0x3000+GRAPH_BANK*0x800,bytes(0x800),hist.ljust(0x800,b'\0'),'signal history control bank'),
        (0x3000+GRAPH_DISPLAY_BANK*0x800,bytes(0x800),graph.ljust(0x800,b'\0'),'signal graph renderer bank'),
        (0x3000+SETTINGS_BANK*0x800,bytes(0x800),prefs.ljust(0x800,b'\0'),'persistent settings bank'),
        (0x3000+MENU_BANK*0x800,bytes(0x800),menu.ljust(0x800,b'\0'),'settings menu and help bank'),
        (0x1800 + COMMON_PC, bytes(0x1800 - COMMON_PC),
         glue.ljust(STATE - COMMON_PC, b'\0') + bytes(STATE_SIZE), 'common feature glue and owned state'),
        (CONTROL_FLASH, bytes(0x800), ctl.ljust(0x800, b'\0'), 'native diagnostic control bank'),
        (DISPLAY_FLASH, bytes(0x800), disp.ljust(0x800, b'\0'), 'native diagnostic renderer/font bank'),
    ]
    for flash, original, after, reason in patches:
        if result[flash:flash + len(original)] != original:
            raise ValueError(f'Expected unused zero padding for {reason} at {flash:#x}')
        if len(after) != len(original):
            raise ValueError('native patch span changed size')
        result[flash:flash + len(after)] = after
    for pc, original, target, nops in (
        (0x4A12, '74 01 90 3c d1', labels['init'], 2),
        (0x4A5F, '12 41 84', labels['draw'], 0),
        (0x4A62, '12 20 b1', labels['wait'], 0),
        (0x4A65, '12 42 db', labels['message'], 0),
    ):
        flash = 0x1D800 + pc - 0x4000
        original = bytes.fromhex(original)
        after = bytes([0x12, target >> 8, target & 255]) + bytes(nops)
        if result[flash:flash + len(original)] != original:
            raise ValueError(f'Unexpected native hook site {pc:#x}')
        result[flash:flash + len(after)] = after
    lcd_offset=0x4000+0x2B
    if result[lcd_offset:lcd_offset+6]!=bytes.fromhex('d2 b1 ea c3 94 08'):
        raise ValueError('unexpected native lcd_update prologue')
    result[lcd_offset:lcd_offset+6]=bytes([0x12,labels['lcd_guard']>>8,
                                         labels['lcd_guard']&255,0,0,0])
    return result, dict(common=labels, control=ctl_labels, display=disp_labels,
                        common_bytes=len(glue), control_bytes=len(ctl), display_bytes=len(disp),
                        settings=prefs_labels,menu=menu_labels,settings_bytes=len(prefs),menu_bytes=len(menu),
                        scanner=scan_labels,scanner_display=scan_display_labels,scanner_bytes=len(scanner),scanner_display_bytes=len(scan_display),
                        home=home_labels,home_bytes=len(home),
                        history=hist_labels,graph=graph_labels,history_bytes=len(hist),graph_bytes=len(graph),
                        state=STATE, state_bytes=STATE_SIZE, state_layout=allocation, control_bank=CONTROL_BANK,
                        display_bank=DISPLAY_BANK)
