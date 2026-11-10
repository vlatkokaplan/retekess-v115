"""Offline packet construction recovered from the archived MPTool binary.

No USB I/O. This is a protocol model, not a tested V115 dumper.
The SPI helper must match the device's ROM loader, identified after entering it.
"""
import binascii

PERMUTATION = (7, 5, 8, 3, 10, 4, 9, 11, 2, 0, 6, 1, 12)


def encode_command(plain, session_key):
    """MPTool 0x40c540 / 0x40c600 / 0x40c3d0."""
    if len(plain) != 16 or plain[0] != 0xFB:
        raise ValueError("Expected a 16-byte FB command")
    packet = bytearray(plain)
    state = session_key
    for index in PERMUTATION:
        position = 1 + index
        value = packet[position]
        packet[position] ^= state & 255
        state = binascii.crc_hqx(bytes([value]), state)
    packet[14:16] = binascii.crc_hqx(packet[1:14], 0xFFFF).to_bytes(2, "big")
    return bytes(packet)


def flash_read_plain(address, size):
    """MPTool 0x40b840: call RAM 0x2200, helper command 1, ordinary read mode.

    Host data-in length is max(size, 512). A 65536-byte request encodes size=0.
    This command is only meaningful after the correct SPI driver is in RAM.
    """
    if not 0 <= address <= 0xFFFFFFFF or not 1 <= size <= 65536:
        raise ValueError("Invalid address or size")
    params = bytes([1]) + (size & 65535).to_bytes(2, "big")
    params += address.to_bytes(4, "big") + bytes([0x0C, 0, 0])
    return bytes([0xFB, 2]) + (0x2200).to_bytes(4, "big") + params
