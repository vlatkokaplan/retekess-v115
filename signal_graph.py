"""Native one-second C/N history and its 32-column LCD graph."""
from lcd_draw import Asm
import native_features as n
from native_ui import Screen


def history_control(base=0x1800,state=n.STATE):
    a=Asm(base)
    for label in ('observe','clear'):n.jump(a,label)
    def get(field):n.read(a,state+field)
    def put(field,value):n.write(a,state+field,value)
    a.L('clear');n.save_context(a);a.lcall('reset');n.restore_context(a);a.emit(0x02,0x01,0x00)
    a.L('reset')
    for field in (n.GRAPH_HEAD,n.GRAPH_COUNT,n.GRAPH_PEAK):put(field,0)
    a.movdptr(state+n.HISTORY);a.movri(0,n.HISTORY_SIZE);a.movai(0)
    a.L('zero');a.movxda();a.emit(0xA3);a.djnz(0,'zero')
    put(n.GRAPH_PENDING,1);a.ret()
    a.L('observe');n.save_context(a)
    # Do not mix samples from different tuning frequencies or modes.
    for field,old in [(n.FREQ,n.GRAPH_FREQ),(n.FREQ+1,n.GRAPH_FREQ+1),(n.MODE,n.GRAPH_MODE)]:
        get(field);a.emit(0xFA);get(old);a.emit(0x6A);n.jnz(a,'changed')
    a.sjmp('check_sample')
    a.L('changed');a.lcall('reset')
    for field,old in [(n.FREQ,n.GRAPH_FREQ),(n.FREQ+1,n.GRAPH_FREQ+1),(n.MODE,n.GRAPH_MODE)]:
        get(field);a.movdptr(state+old);a.movxda()
    a.L('check_sample');get(n.GRAPH_PENDING);a.jz('done')
    get(n.FLAGS);a.anlai(n.MENU|n.SCAN_UI|n.DISPLAY_SLEEP);n.jnz(a,'done')
    put(n.GRAPH_PENDING,0)
    get(n.CN);a.emit(0xFA);get(n.GRAPH_HEAD)
    a.emit(0x24,(state+n.HISTORY)&255,0xF5,0x82);a.movdi(0x83,(state+n.HISTORY)>>8)
    a.movar(2);a.movxda()
    get(n.GRAPH_HEAD);a.emit(0x04);a.anlai(n.HISTORY_SIZE-1);a.movxda()
    get(n.GRAPH_COUNT);a.cjne_ai(n.HISTORY_SIZE,'count_inc');a.sjmp('peak')
    a.L('count_inc');a.emit(0x04);a.movxda()
    a.L('peak');get(n.GRAPH_PEAK);a.emit(0xC3,0x9A);a.jnc('done')
    a.movar(2);a.movdptr(state+n.GRAPH_PEAK);a.movxda()
    a.L('done');n.restore_context(a);a.emit(0x02,0x01,0x00)
    return a.assemble(),a.labels


def graph_renderer(base=0x1800,state=n.STATE):
    ui=Screen(base,state);a=ui.a
    ui.clear();ui.mode();ui.text(' ');ui.frequency();ui.row()
    ui.text('NOW:');ui.number(n.CN);ui.text(' PEAK:');ui.number(n.GRAPH_PEAK);ui.text('DB');ui.flush(0)
    masks=[]
    for height in range(31):
        mask=((1<<(height+1))-1)<<(31-height)
        masks.extend(mask.to_bytes(4,'little'))
    ui.tables['heights']=bytes(masks)
    for pair,page in [(0,2),(2,4)]:
        ui.clear()
        # Grid lines at C/N 0, 15, 30, 45 and 60 dB, every other column.
        for local in range(2):
            grid=0
            for level in (0,15,30,45,60):
                y=31-level//2
                if y//8==pair+local:grid|=1<<(y&7)
            if grid:
                a.movri(6,0);a.L('grid'+str(page+local));a.movar(6);a.emit(0x24,local*128,0xF5,0x82)
                a.movdi(0x83,0x28);a.movai(grid);a.movxda();a.incr(6);a.incr(6);a.movar(6)
                a.cjne_ai(128,'grid'+str(page+local))
        # Right-align a short history; a full ring starts at the oldest sample.
        a.movri(6,0)
        a.L('column'+str(page))
        ui.get(n.GRAPH_COUNT);a.emit(0xFA);a.movai(32);a.emit(0xC3,0x9A,0xFB)
        a.movar(6);a.emit(0xC3,0x9B);a.jnc('has_sample'+str(page));n.jump(a,'next'+str(page))
        a.L('has_sample'+str(page));a.emit(0xFA)
        ui.get(n.GRAPH_COUNT);a.cjne_ai(32,'index_ready'+str(page))
        ui.get(n.GRAPH_HEAD);a.emit(0x2A);a.anlai(31);a.emit(0xFA)
        a.L('index_ready'+str(page));a.movar(2);a.emit(0x24,(state+n.HISTORY)&255,0xF5,0x82)
        a.movdi(0x83,(state+n.HISTORY)>>8);a.emit(0xE0,0xC3,0x13)
        a.emit(0xC3,0x94,31);a.jnc('clip'+str(page));a.emit(0x24,31);a.sjmp('height'+str(page))
        a.L('clip'+str(page));a.movai(30)
        a.L('height'+str(page));a.emit(0x23,0x23,0x24,pair,0xFF)
        for local in range(2):
            ui.pointer('heights');a.movar(7);a.emit(0x24,local,0x93,0xFC)
            for column in range(3):
                a.movar(6);a.emit(0x23,0x23,0x24,column+local*128,0xF5,0x82)
                a.movdi(0x83,0x28);a.emit(0xE0,0x4C);a.movxda()
        a.L('next'+str(page));a.incr(6);a.movar(6);a.cjne_ai(32,'continue'+str(page));a.sjmp('finished'+str(page))
        a.L('continue'+str(page));n.jump(a,'column'+str(page));a.L('finished'+str(page))
        ui.flush(page)
    ui.clear();ui.text('C/N 0-60DB  1S/SAMPLE');ui.row();ui.text('0 CLR 7 METER 3 EXIT');ui.flush(6)
    return ui.finish()
