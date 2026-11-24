#!/usr/bin/env python3
"""Try analogue radio volume in loader RAM, starting muted; never writes flash."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import signal
import time

import radio_audio
from run_radio import ROOT, execute, inject, select, state, upload, wrapper
from lcd_draw import Asm
from usb_dump import RawTransport
from stock_audio import VOLUME_REGISTERS


class VolumePreview:
    def __init__(self, transport, manifest, mapping):
        self.transport = transport
        self.manifest = manifest
        self.symbols = {name: int(address, 16) for address, name in re.findall(
            r'^\s*(?:C:|D:)?\s*([0-9A-F]+)\s+_(\w+)\s', mapping, re.M)}

    def access(self, operations):
        code, size = radio_audio.program(operations)
        self.run(code)
        return list(self.transport.read_ram(radio_audio.RESULT, 512)[:size])

    def run(self, code):
        # Preserve the compiled application's scratch space and state in this block.
        if len(code) > 128:
            raise ValueError('diagnostic exceeds the loader wrapper allocation')
        data = bytearray(self.transport.read_ram(0x2200, 512))
        data[:128] = code.ljust(128, b'\0')
        upload(self.transport, 0x2200, data)
        execute(self.transport)

    def call(self, name, argument, value=None):
        if name not in ('tuner_read', 'tuner_write', 'lcd_power'):
            raise ValueError('unsupported diagnostic board call')
        entry = self.symbols[name]
        a = Asm(0)
        if value is not None:
            a.movdptr(self.symbols[name + '_PARM_2'])
            a.movai(value)
            a.movxda()
        a.movdi(0x82, argument)
        call = bytes([0x12, entry >> 8, entry & 255])
        a.emit(*call)
        a.movad(0x82)
        a.movdptr(0x23E0)
        a.movxda()
        self.run(wrapper(entry, self.manifest['internal_data_bytes']).replace(call, a.assemble()))
        return self.transport.read_ram(0x23E0, 512)[0]

    def tuner(self, register, value=None):
        return self.call('tuner_read' if value is None else 'tuner_write', register, value)

    def mute(self):
        values = self.access([('smodify', 0xBD, 0xDF, 0),
                              ('smodify', 0xB0, 0xDF, 0),
                              ('xmodify', 0x7890, 0xFF, 1)])
        if values[1] & 0x20:
            raise RuntimeError('speaker mute readback differs')

    def apply(self, sample):
        level = sample['volume'] - 24
        if not 0 <= level <= 32:
            raise ValueError('invalid application volume')
        enabled = bool(level and not any(sample[key] for key in
                       ('muted', 'standby', 'pending', 'error')))
        # The compiled application owns gain. Observe the stock gain encoding;
        # never turn the codec's power/bias controls into a gain field.
        values = self.access([('xread', 0x7890), ('xread', 0x7893),
                              ('smodify', 0xB0, 255, 0)])
        if values[1] != VOLUME_REGISTERS[level] or (values[0] & 0xFE) != 0xFE:
            self.mute()
            raise RuntimeError('stock DAC gain/control readback differs')
        if not enabled:
            self.access([('smodify', 0xB0, 0xDF, 0)])
        return dict(level=level, enabled=enabled, control_register=values[0],
                    gain_register=values[1], p3=values[2],
                    error=sample['error'], key=sample['last_key'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, default=ROOT / 'build/radio-timer-preview')
    parser.add_argument('--seconds', type=float, default=900)
    parser.add_argument('--idle-seconds', type=float, default=60)
    parser.add_argument('--report', type=Path, default=ROOT / 'build/radio-volume-preview.json')
    args = parser.parse_args()
    if not 0 < args.seconds <= 900 or not 0 < args.idle_seconds <= 60:
        parser.error('duration must be in 0..900 seconds and idle timeout in 0..60')
    manifest = json.loads((args.build / 'radio.json').read_text())
    code = (args.build / 'radio.bin').read_bytes()
    if (manifest.get('simulation') or not manifest.get('timer_batch') or
            manifest['code_address'] != 0x2400 or
            hashlib.sha256(code).hexdigest() != manifest['sha256']):
        parser.error('a matching real timer-batch build is required')
    for name, digest in manifest['source_sha256'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            parser.error('build source changed: ' + name)
    report = dict(flash_written=False, completed=False, starts_muted=True,
                  application_sha256=manifest['sha256'], stock_dac_volume=True,
                  preview_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  audio_source_sha256=hashlib.sha256(Path(radio_audio.__file__).read_bytes()).hexdigest(),
                  samples=[])
    stopped = False

    def stop(*unused):
        nonlocal stopped
        stopped = True

    previous = {number: signal.signal(number, stop) for number in (signal.SIGINT, signal.SIGTERM)}
    transport = RawTransport()
    preview = VolumePreview(transport, manifest, (args.build / 'radio.map').read_text())
    initialized = False

    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')

    try:
        report['loader_before'] = transport.identify()
        transport.unlock()
        preview.mute()
        report['calibration'] = radio_audio.initialize(transport)
        for offset in range(0, len(code), 512):
            upload(transport, 0x2400 + offset, code[offset:offset + 512].ljust(512, b'\0'))
        upload(transport, 0x2200, bytes(512))
        select(transport, manifest, 'app_init')
        execute(transport)
        initialized = True
        select(transport, manifest, 'app_tick')
        initial = state(transport, manifest)
        for unused in range(abs(initial['volume'] - 25)):
            inject(transport, manifest, 8 if initial['volume'] > 25 else 9)
        if not initial['muted']:
            inject(transport, manifest, 2)
        initial = state(transport, manifest)
        if initial['volume'] != 25 or initial['muted'] != 1:
            raise RuntimeError('muted VOL 1 startup check failed')
        preview.apply(initial)
        print('Ready: MUTED, VOL 1, stock DAC volume. PLAY enables audio; POWER stops.', flush=True)
        print(f'Idle mute after {args.idle_seconds:g}s; test ends after {args.seconds:g}s.', flush=True)
        start = last_activity = time.monotonic()
        last_events = initial['events']
        signature = None
        while not stopped and time.monotonic() - start < args.seconds:
            select(transport, manifest, 'app_tick')
            execute(transport)
            sample = state(transport, manifest)
            if sample['events'] != last_events:
                last_events = sample['events']
                last_activity = time.monotonic()
            if not sample['muted'] and time.monotonic() - last_activity >= args.idle_seconds:
                inject(transport, manifest, 2)
                sample = state(transport, manifest)
                print('Idle timeout: MUTED. PLAY resumes.', flush=True)
            current = tuple(sample[key] for key in
                            ('volume', 'muted', 'standby', 'pending', 'error', 'tunes', 'mode'))
            if current != signature:
                output = preview.apply(sample)
                output['seconds'] = round(time.monotonic() - start, 2)
                report['samples'].append(output)
                save()
                print(json.dumps(output), flush=True)
                signature = current
            if sample['standby']:
                break
            time.sleep(0.02)
        report['completed'] = True
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        # Attempt each shutdown operation even if another peripheral has failed.
        cleanup_errors = []

        def cleanup(name, action):
            try:
                return action()
            except Exception as error:
                cleanup_errors.append(f'{name}: {type(error).__name__}: {error}')
                return None

        def speaker_off():
            preview.mute()
            report['speaker_muted_on_exit'] = True

        def tuner_off():
            preview.tuner(0, 0x44)
            value = preview.tuner(0)
            report['tuner_shutdown_readback'] = value
            if (value & 0x84) != 4:
                raise RuntimeError('tuner shutdown readback differs')
            report['tuner_off_on_exit'] = True

        def display_off():
            preview.call('lcd_power', 0)
            report['display_off_on_exit'] = True

        try:
            cleanup('speaker mute', speaker_off)
            if initialized:
                cleanup('tuner shutdown', tuner_off)
                cleanup('display shutdown', display_off)
            cleanup('DAC shutdown', lambda: preview.access([('atwrite', 0, 0)]))
            report['loader_after'] = cleanup('loader inquiry', transport.identify)
        finally:
            transport.close()
            for number, handler in previous.items():
                signal.signal(number, handler)
            if cleanup_errors:
                report['cleanup_errors'] = cleanup_errors
            save()
        if cleanup_errors and 'error' not in report:
            raise RuntimeError('; '.join(cleanup_errors))



if __name__ == '__main__':
    main()
