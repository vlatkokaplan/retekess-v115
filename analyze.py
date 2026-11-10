"""Firmware scrambling, loader-helper decoding and Intel HEX utilities."""
import subprocess
from pathlib import Path
from env_tools import executable

ROOT = Path(__file__).resolve().parent


def firmware_xor(data, state):
    """makecode.exe 0x401020; state transition at 0x401000."""
    out = bytearray()
    for value in data:
        key = 0
        for _ in range(8):
            bit = state >> 15
            key = (key << 1) | bit
            state = (((state ^ (0x6881 if bit else 0)) << 1) | bit) & 0xFFFF
        out.append(value ^ key)
    return bytes(out)


def decode_spi_driver(raw):
    """MPTool 0x404e40 removes inserted bytes; 0x40c490 decodes payload."""
    if len(raw) != 0xE03:
        raise ValueError("Expected the archived 3587-byte spi0802 driver")
    data = bytes(v for i, v in enumerate(raw) if i not in (0x31, 0x95, 0xF9))
    state = 0xEF31
    out = bytearray()
    for value in data:
        key = 0
        for _ in range(8):
            key = (key << 1) | (state >> 15)
            bit = state & 1
            state = ((state ^ (0x7E3A if bit else 0)) >> 1) | (bit << 15)
        out.append(value ^ key)
    entry = int.from_bytes(out[1:3], "big")
    if out[0] != 2 or not 0x2200 <= entry < 0x3000 or any(out[-256:]):
        raise ValueError("Unexpected decoded driver entry or padding")
    return bytes(out)


def intel_hex(chunks):
    lines = []
    for origin, data in chunks:
        for offset in range(0, len(data), 16):
            part = data[offset:offset + 16]
            addr = origin + offset
            record = bytes([len(part), addr >> 8, addr & 255, 0]) + part
            lines.append(":" + record.hex().upper() + f"{(-sum(record)) & 255:02X}")
    return "\n".join(lines + [":00000001FF"]) + "\n"


def sparse_switch(image, address):
    if image[address] != 0:
        raise ValueError("Expected the IAR sparse switch table format")
    count = image[address + 1]
    cursor = address + 2
    entries = {}
    for _ in range(count):
        entries[f"0x{image[cursor]:02x}"] = f"0x{int.from_bytes(image[cursor + 1:cursor + 3], 'little'):04x}"
        cursor += 3
    return {"entries": entries, "default": f"0x{int.from_bytes(image[cursor:cursor + 2], 'little'):04x}"}


def disassemble(hex_path, output_path, entries):
    result = subprocess.run(
        [executable('dis51'), '-l', *map(hex, entries)],
        input=hex_path.read_bytes(), capture_output=True, check=True,
    )
    output_path.write_bytes(result.stdout)
