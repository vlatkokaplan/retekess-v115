"""Strict integrity checks for the researched V115 v1.4 2 MiB image layout."""
import binascii
import hashlib
import struct

from band_patch import decode, encode, is_supported

TABLE, APP, BLOCK, COUNT = 0x20000, 0x1800, 0x800, 61
REGIONS = [(0x20, 8, 0x800, 0x1000), (0x30, 0x101, 0x1800, 0x1E800)]


def crc16(data):
    # Separate bit implementation, cross-checked with the library implementation.
    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    if crc != binascii.crc_hqx(data, 0xFFFF):
        raise ValueError('independent CRC implementations disagree')
    return crc


def check(raw):
    if len(raw) != 0x200000:
        raise ValueError('image must be exactly 2 MiB')
    plain = decode(raw)
    if encode(plain) != raw:
        raise ValueError('scramble/decode round-trip failed')
    if plain[0x1E:0x20] != b'\x55\xaa':
        raise ValueError('boot header magic mismatch')
    header_crc = crc16(plain[:0x1C])
    if struct.unpack_from('<H', plain, 0x1C)[0] != header_crc:
        raise ValueError('primary boot header CRC mismatch')
    checks = []
    for entry, flags, offset, size in REGIONS:
        got_flags, got_offset, got_size, stored = struct.unpack_from('<4I', plain, entry)
        if (got_flags, got_offset, got_size) != (flags, offset, size):
            raise ValueError(f'unexpected section layout at {entry:#x}')
        actual = crc16(plain[offset:offset+size])
        if stored != actual:
            raise ValueError(f'section CRC mismatch at {offset:#x}')
        checks.append(dict(offset=offset, bytes=size, crc16=actual))
    blocks = []
    for index in range(COUNT):
        actual = crc16(plain[APP+index*BLOCK:APP+(index+1)*BLOCK])
        stored = struct.unpack_from('<H', raw, TABLE+index*2)[0]
        if actual != stored:
            raise ValueError(f'block {index} CRC mismatch: {stored:04x} != {actual:04x}')
        blocks.append(actual)
    return dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                primary_header_crc16=header_crc, section_crcs=checks,
                block_crcs=blocks, all_61_block_crcs_match=True)


def finalize(plain):
    plain = bytearray(plain)
    if len(plain) != 0x200000:
        raise ValueError('wrong image size')
    for entry, flags, offset, size in REGIONS:
        if struct.unpack_from('<3I', plain, entry) != (flags, offset, size):
            raise ValueError('unexpected image section layout')
        struct.pack_into('<I', plain, entry+12, crc16(plain[offset:offset+size]))
    for index in range(COUNT):
        struct.pack_into('<H', plain, TABLE+index*2,
                         crc16(plain[APP+index*BLOCK:APP+(index+1)*BLOCK]))
    raw = encode(plain)
    return raw, check(raw)


def balance_application_crc(plain, target, offset):
    """Solve 16 CRC difference equations using two explicitly unused bytes.

    This keeps the existing header CRC value valid without modifying its
    sector. Every checksum still verifies against the actual final bytes.
    """
    plain = bytearray(plain)
    if not APP <= offset < 0x20000-1 or any(plain[offset:offset+2]):
        raise ValueError('CRC balancing location must be two unused zero bytes in the app')
    start, end = APP, 0x20000
    initial = binascii.crc_hqx(plain[start:end], 0xFFFF)
    basis = {}
    for variable in range(16):
        plain[offset + variable//8] ^= 1 << (variable%8)
        value = binascii.crc_hqx(plain[start:end], 0xFFFF) ^ initial
        plain[offset + variable//8] ^= 1 << (variable%8)
        combination = 1 << variable
        while value:
            pivot = value.bit_length()-1
            if pivot not in basis:
                basis[pivot] = value, combination
                break
            row, columns = basis[pivot]
            value ^= row
            combination ^= columns
    value, solution = target ^ initial, 0
    while value:
        pivot = value.bit_length()-1
        if pivot not in basis:
            raise ValueError('CRC balancing system is singular')
        row, columns = basis[pivot]
        value ^= row
        solution ^= columns
    plain[offset:offset+2] = solution.to_bytes(2, 'little')
    if crc16(plain[start:end]) != target:
        raise ValueError('application CRC balancing did not verify')
    return plain, dict(offset=offset,bytes=plain[offset:offset+2].hex(),
                       target_crc16=target,crc16_before=initial,
                       method='16-variable GF(2) CRC difference solve; padding only')


def preserve_original(original, candidate, allowed_resource_spans=()):
    if not is_supported(original):
        raise ValueError('unrecognized source backup')
    check(original)
    checks = check(candidate)
    if candidate[:0x2000] != original[:0x2000]:
        raise ValueError('first 8 KiB including both boot-containing sectors changed')
    cursor=TABLE+COUNT*2
    for start,size in sorted(allowed_resource_spans):
        if size<=0 or start<cursor or start+size>len(original):
            raise ValueError('invalid explicit resource exception')
        if candidate[cursor:start]!=original[cursor:start]:
            raise ValueError('unapproved resources or settings changed')
        cursor=start+size
    if candidate[cursor:] != original[cursor:]:
        raise ValueError('resources, calibration or settings changed')
    return checks
