#!/usr/bin/env python3
"""Band-limit patches and image codecs for the verified V115 v1.4 executable.

Never opens USB. Keeps MW 9/10 kHz mode and FM 100 kHz stepping unchanged;
extends the FM range and makes SW a 5 kHz AM range from 150 to 30000 kHz.
"""
import binascii
import hashlib
from pathlib import Path
import struct

from analyze import firmware_xor

ROOT = Path(__file__).resolve().parent
# The executable + CRC-table prefix is identical across supported stock v1.4
# images. Settings after this prefix may differ between individual radios.
SOURCE_CODE_END = 0x2007A
SUPPORTED_CODE_SHA256 = '7832598fd754490ca1ef0228d734b5f5e851d3a9f2f6f02b49f0d2a304b55672'


def is_supported(raw):
    return (len(raw) == 0x200000 and
            hashlib.sha256(raw[:SOURCE_CODE_END]).hexdigest() == SUPPORTED_CODE_SHA256)


# Offsets refer to decoded flash, not banked MCU addresses.
# Each replacement is the same length as the original instruction/data span.
PATCHES = [
    # MW branches also enter at 0xc082 to reuse MOV R3,#2. Preserve that byte!
    (0x0C07E, '60 06', '80 06', 'UI FM lower: both flags use the patched 30 MHz branch; preserve shared MW high byte'),
    (0x0C086, '7a 66 7b 03', '7a 2c 7b 01', 'UI FM lower: ordinary flag, 87 -> 30 MHz'),
    (0x0C0AC, '7a 38 7b 04', '7a fc 7b 08', 'UI FM upper: 108 -> 230 MHz'),
    (0x0C09E, '7a 8e 7b 12', '7a 96 7b 00', 'UI SW/AM lower: 4750 -> 150 kHz'),
    (0x0C0BD, '7a 5a 7b 55', '7a 30 7b 75', 'UI SW/AM upper: 21850 -> 30000 kHz'),
    (0x1180E, '7c bc 7d 02', '7c 2c 7d 01', 'Clamp FM lower: extended flag, 70 -> 30 MHz'),
    (0x11814, '7c 66 7d 03', '7c 2c 7d 01', 'Clamp FM lower: ordinary flag, 87 -> 30 MHz'),
    (0x11818, '7a 38 7b 04', '7a fc 7b 08', 'Clamp FM upper: 108 -> 230 MHz'),
    (0x11836, '7c 8e 7d 12', '7c 96 7d 00', 'Clamp SW/AM lower: 4750 -> 150 kHz'),
    (0x1183A, '7a 5a 7b 55', '7a 30 7b 75', 'Clamp SW/AM upper: 21850 -> 30000 kHz'),
    (0x0E5B3, '94 5b a3 e0 94 55', '94 31 a3 e0 94 75',
     'Loaded-frequency guard: reject >=21851 -> reject >=30001'),
    (0x02DC0, '8e 12', '96 00',
     'First SW shortcut/classification boundary: 4750 -> 150 kHz'),
]


def decode(raw):
    if len(raw) != 0x200000:
        raise ValueError('Expected a complete 2 MiB V115 flash backup')
    plain = bytearray(raw)
    plain[:0x800] = firmware_xor(raw[:0x800], 0x50AF)
    plain[:0x20] = firmware_xor(raw[:0x20], 0xB6B3)
    for offset in range(0x800, 0x20000, 0x800):
        plain[offset:offset + 0x800] = firmware_xor(raw[offset:offset + 0x800], 0x8A0D ^ (offset >> 8))
    return plain


def encode(plain):
    raw = bytearray(plain)
    raw[:0x800] = firmware_xor(plain[:0x800], 0x50AF)
    raw[:0x20] = firmware_xor(plain[:0x20], 0xB6B3)
    for offset in range(0x800, 0x20000, 0x800):
        raw[offset:offset + 0x800] = firmware_xor(plain[offset:offset + 0x800], 0x8A0D ^ (offset >> 8))
    return bytes(raw)


def verify_crcs(plain):
    expected_header = struct.unpack_from('<H', plain, 0x1C)[0]
    actual_header = binascii.crc_hqx(plain[:0x1C], 0xFFFF)
    if actual_header != expected_header or plain[0x1E:0x20] != bytes.fromhex('55 aa'):
        raise ValueError('Primary boot header CRC or magic mismatch')
    checks = []
    for entry in (0x20, 0x30):
        _, offset, size, expected = struct.unpack_from('<4I', plain, entry)
        actual = binascii.crc_hqx(plain[offset:offset + size], 0xFFFF)
        if actual != expected:
            raise ValueError(f'CRC mismatch at flash {offset:#x}: {actual:#x} != {expected:#x}')
        checks.append({'offset': offset, 'bytes': size, 'crc16_ccitt': actual})
    return checks


def patch_decoded(plain):
    modified = bytearray(plain)
    changes = []
    for offset, before, after, reason in PATCHES:
        before, after = bytes.fromhex(before), bytes.fromhex(after)
        if modified[offset:offset + len(before)] != before:
            raise ValueError(f'Unexpected original bytes at {offset:#x}; refusing patch')
        if len(before) != len(after):
            raise ValueError('Patch changes instruction length')
        modified[offset:offset + len(after)] = after
        changes.append({'offset': offset, 'offset_hex': hex(offset), 'before': before.hex(' '),
                        'after': after.hex(' '), 'reason': reason})
    # The boot section stays byte-identical. Recompute only the application CRC.
    _, offset, size, _ = struct.unpack_from('<4I', modified, 0x30)
    crc = binascii.crc_hqx(modified[offset:offset + size], 0xFFFF)
    struct.pack_into('<I', modified, 0x3C, crc)
    return modified, changes


def report(plain):
    return {
        'bank_mapping': 'flash = 0x3000 + bank*0x800 + (PC-0x1800), for banked PC 0x1800..0x1fff',
        'functions': [
            {'bank': 0x12, 'pc': 0x1874, 'flash': 0xC074, 'name': 'band_lower_limit'},
            {'bank': 0x12, 'pc': 0x18A5, 'flash': 0xC0A5, 'name': 'band_upper_limit'},
            {'bank': 0x1D, 'pc': 0x1800, 'flash': 0x11800, 'name': 'band_clamp_or_wrap'},
            {'bank': 0x1D, 'pc': 0x198B, 'flash': 0x1198B, 'name': 'frequency_to_tuner_channel'},
            {'bank': 0x1D, 'pc': 0x1BB9, 'flash': 0x11BB9, 'name': 'tuner_initialize'},
            {'bank': 0x1D, 'pc': 0x1D17, 'flash': 0x11D17, 'name': 'tuner_write_register'},
            {'bank': 0x1D, 'pc': 0x1D3E, 'flash': 0x11D3E, 'name': 'tuner_read_register'},
            {'bank': 0x1D, 'pc': 0x1DD7, 'flash': 0x11DD7, 'name': 'tuner_set_channel_registers_2_3'},
        ],
        'state': {'mode_idata': 0x18, 'mode_values': {'1': 'FM', '2': 'MW', '3': 'SW'},
                  'fm_extended_flag_idata': 0x8A, 'mw_10khz_flag_idata': 0x8B,
                  'current_frequency_xdata_le16': 0x3CDC, 'tuner_shadow_xdata': 0x3B33},
        'original_ranges': {'FM_MHz': [[87, 108], [70, 108]], 'MW_kHz': [[522, 1710], [520, 1710]],
                            'SW_kHz': [4750, 21850]},
        'sw_shortcuts_kHz': list(struct.unpack_from('<9H', plain, 0x2DC0)),
        'tuner_bus': {'address_7bit': 0x10, 'scl': 'P1.6', 'sda': 'P1.7'},
        'channel_formulas': {'FM': 'channel = (frequency_in_100kHz_units - 300) * 4',
                             'MW_9kHz': 'channel = frequency_kHz / 3',
                             'SW_and_MW_10kHz': 'channel = frequency_kHz / 5'},
        'bandwidth_patch_byte': {'flash': 0x11BEB, 'original': 0xA1,
                                 'description': 'AKC6955 reg7 init value; low 2 bits 01 = 200 kHz'},
    }
