"""Large-frequency radio view and mode-aware settings/help screens."""
import native_features as n
from native_ui import Screen


def home_renderer(base=0x1800,state=n.STATE):
    ui=Screen(base,state,big=True);a=ui.a
    ui.clear();ui.row(6);ui.put(n.FONT_SCALE,1);ui.frequency(units=False)
    ui.put(n.FONT_SCALE,0);ui.row(94);ui.mode();ui.row(222)
    ui.get(n.MODE);a.cjne_ai(1,'units_am');ui.text('MHZ');a.sjmp('units_done')
    a.L('units_am');ui.text('KHZ');a.L('units_done');ui.flush(0)
    ui.clear();ui.text('C/N:');ui.number(n.CN);ui.text('DB PK:');ui.number(n.GRAPH_PEAK)
    ui.get(n.CN);a.emit(0xC3,0x94,61);a.jnc('bar_clip');a.emit(0x24,61);a.sjmp('bar_level')
    a.L('bar_clip');a.movai(60)
    a.L('bar_level');a.emit(0x23,0xFC);a.movri(6,0)
    a.L('bar_column');a.movar(6);a.jz('bar_border');a.cjne_ai(127,'bar_inside')
    a.L('bar_border');a.movai(0x7E);a.sjmp('bar_write')
    a.L('bar_inside');a.movar(6);a.emit(0xC3,0x9C);a.jnc('bar_empty');a.movai(0x7E);a.sjmp('bar_write')
    a.L('bar_empty');a.movai(0x42)
    a.L('bar_write');a.emit(0xFD);a.movar(6);a.emit(0x24,128,0xF5,0x82);a.movdi(0x83,0x28)
    a.movar(5);a.movxda();a.incr(6);a.movar(6);a.cjne_ai(128,'bar_column');ui.flush(2)
    ui.clear();ui.get(n.MODE);a.cjne_ai(1,'home_am')
    ui.text('BW:');ui.get(n.REG7);a.anlai(3);ui.pointer('widths');a.emit(0x93,0xFA);a.movri(3,0);ui.number_regs();ui.text('KHZ ')
    ui.get(n.REG7);a.anlai(4);a.jz('home_auto');ui.text('MONO');a.sjmp('home_audio')
    a.L('home_auto');ui.text('AUTO');a.L('home_audio');ui.row();ui.text('BASS:')
    ui.get(n.REG7);a.anlai(16);a.jz('home_bass_off');ui.text('ON');a.sjmp('home_bass_done')
    a.L('home_bass_off');ui.text('OFF');a.L('home_bass_done');ui.text(' DE:')
    ui.get(n.REG7);a.anlai(32);a.jz('home_de75');ui.text('50US');a.sjmp('home_details_done')
    a.L('home_de75');ui.text('75US');a.sjmp('home_details_done')
    a.L('home_am');a.cjne_ai(2,'home_sw');ui.text('MW STEP:')
    a.movri(0,0x8B);a.emit(0xE6);a.jz('home_mw9');ui.text('10KHZ');a.sjmp('home_am_line2')
    a.L('home_mw9');ui.text('9KHZ');a.sjmp('home_am_line2')
    a.L('home_sw');ui.text('SW STEP:5KHZ')
    a.L('home_am_line2');ui.row();ui.text('AM SIGNAL MONITOR')
    a.L('home_details_done');ui.flush(4)
    ui.clear();ui.text('HOLD2 METERS 8 MENU');ui.row();ui.text('HOLD4 SCREEN OFF');ui.flush(6)
    ui.tables['widths']=bytes([150,200,50,100])
    return ui.finish()


def settings_renderer(base=0x1800,state=n.STATE):
    ui=Screen(base,state);a=ui.a
    ui.get(n.FLAGS);a.anlai(n.HELP);n.jnz(a,'help');n.jump(a,'settings');a.L('help')
    help_lines=['KEYS '+n.VERSION,'HOLD1 BW 2 METERS','HOLD3 MONO 4 LCD OFF','HOLD6 DE 7 BASS',
                'HOLD8 SETTINGS','METERS:7 GRAPH/VALUES','* UNSAVED ! SAVE FAIL','0 SAVE/EXIT 9 BACK']
    for page in range(0,8,2):ui.clear();ui.text(help_lines[page]);ui.row();ui.text(help_lines[page+1]);ui.flush(page)
    n.jump(a,'screen_done')
    a.L('settings');ui.clear();ui.mode();ui.text(' SETTINGS')
    ui.get(36);a.jz('settings_not_failed');ui.text(' !');a.sjmp('settings_status_done')
    a.L('settings_not_failed');ui.get(n.NV_DIRTY);a.jz('settings_status_done');ui.text(' *')
    a.L('settings_status_done');ui.row();ui.get(n.MODE);a.cjne_ai(1,'settings_am_jump');a.sjmp('settings_fm')
    a.L('settings_am_jump');n.jump(a,'settings_am');a.L('settings_fm')
    ui.text('1 BANDWIDTH:');ui.get(n.PREF_REG);a.anlai(3);ui.pointer('widths');a.emit(0x93,0xFA)
    a.movri(3,0);ui.number_regs();ui.text('KHZ');ui.flush(0)
    ui.clear();ui.text('2 AUDIO:');ui.get(n.PREF_REG);a.anlai(4);a.jz('menu_auto')
    ui.text('MONO');a.sjmp('menu_audio_done');a.L('menu_auto');ui.text('AUTO');a.L('menu_audio_done')
    ui.row();ui.text('3 BASS:');ui.get(n.PREF_REG);a.anlai(16);a.jz('menu_bass_off')
    ui.text('ON');a.sjmp('menu_bass_done');a.L('menu_bass_off');ui.text('OFF');a.L('menu_bass_done');ui.flush(2)
    ui.clear();ui.text('4 DE-EMPHASIS:');ui.get(n.PREF_REG);a.anlai(32);a.jz('menu_de75')
    ui.text('50US');a.sjmp('menu_de_done');a.L('menu_de75');ui.text('75US');a.L('menu_de_done')
    ui.row();a.lcall('timeout');ui.flush(4)
    ui.clear();ui.text('6 OFF 7 METER');ui.row();ui.text('0 SAVE 8 SCAN 9 HELP');ui.flush(6)
    n.jump(a,'screen_done')
    a.L('settings_am');a.lcall('timeout');ui.flush(0)
    for page,lines in [(2,['6 SCREEN OFF','7 SIGNAL METER']),(4,['8 SCAN OPTIONS','9 SHORTCUT HELP']),(6,['0 SAVE AND EXIT','CUSTOM '+n.VERSION])]:
        ui.clear();ui.text(lines[0]);ui.row();ui.text(lines[1]);ui.flush(page)
    n.jump(a,'screen_done')
    a.L('timeout');ui.text('5 LCD SLEEP:');ui.get(n.TIMEOUT);a.jz('timeout_off')
    ui.pointer('timeouts');a.emit(0x93,0xFA);a.movri(3,0);ui.number_regs();ui.text('S');a.ret()
    a.L('timeout_off');ui.text('OFF');a.ret()
    a.L('screen_done');ui.tables['widths']=bytes([150,200,50,100]);ui.tables['timeouts']=bytes([0,10,30,60])
    return ui.finish()
