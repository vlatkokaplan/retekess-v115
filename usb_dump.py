#!/usr/bin/env python3
"""Read V115 flash through its SPI080-Loader 1.00, using Linux SG_IO.

Only the exact identified loader is accepted. Device data-out is restricted to
an audited helper and its configuration in volatile RAM. Helper calls use GPIO/SPI
initialization (0), flash reads (1), and volatile read-opcode configuration (6).
No erase/program/status-write is exposed.
"""
import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time

from loader_packets import encode_command, flash_read_plain

ROOT = Path(__file__).resolve().parent
HELPER_SHA256 = "7cfd4f3768b6244d8181eeed37f9b686ec755bbbdeabc1e121042d4da036cfdb"


class SgHeader(C.Structure):
    _fields_ = [
        ("interface_id", C.c_int), ("dxfer_direction", C.c_int),
        ("cmd_len", C.c_ubyte), ("mx_sb_len", C.c_ubyte),
        ("iovec_count", C.c_ushort), ("dxfer_len", C.c_uint),
        ("dxferp", C.c_void_p), ("cmdp", C.c_void_p), ("sbp", C.c_void_p),
        ("timeout", C.c_uint), ("flags", C.c_uint), ("pack_id", C.c_int),
        ("usr_ptr", C.c_void_p), ("status", C.c_ubyte),
        ("masked_status", C.c_ubyte), ("msg_status", C.c_ubyte),
        ("sb_len_wr", C.c_ubyte), ("host_status", C.c_ushort),
        ("driver_status", C.c_ushort), ("resid", C.c_int),
        ("duration", C.c_uint), ("info", C.c_uint),
    ]


class Transport:
    def __init__(self, device):
        self.fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)
        self.libc = C.CDLL(None, use_errno=True)
        self.key = None
        self.count = 0

    def command(self, cdb, incoming=0, outgoing=None):
        cmd = C.create_string_buffer(cdb, len(cdb))
        sense = C.create_string_buffer(64)
        length = len(outgoing) if outgoing is not None else incoming
        data = C.create_string_buffer(max(length, 1))
        if outgoing is not None:
            C.memmove(data, outgoing, length)
        hdr = SgHeader()
        hdr.interface_id = ord("S")
        hdr.dxfer_direction = -2 if outgoing is not None else -3
        hdr.cmd_len = len(cdb)
        hdr.mx_sb_len = len(sense)
        hdr.dxfer_len = length
        hdr.dxferp = C.addressof(data) if length else None
        hdr.cmdp = C.addressof(cmd)
        hdr.sbp = C.addressof(sense)
        hdr.timeout = 10000
        if self.libc.ioctl(self.fd, 0x2285, C.byref(hdr)) < 0:
            err = C.get_errno()
            raise OSError(err, os.strerror(err))
        if hdr.status or hdr.host_status or hdr.driver_status or hdr.info & 1:
            raise RuntimeError(
                f"SCSI {cdb.hex()} status={hdr.status:02x} host={hdr.host_status:04x} "
                f"driver={hdr.driver_status:04x} sense={sense.raw[:hdr.sb_len_wr].hex()}"
            )
        if incoming and hdr.resid:
            raise RuntimeError(f"Short SCSI response: expected {length}, residual {hdr.resid}")
        return data.raw[:incoming]

    def identify(self):
        data = self.command(bytes.fromhex("12 00 00 00 24 00"), incoming=36)
        info = {"vendor": data[8:16].decode("ascii", "replace").strip(),
                "product": data[16:32].decode("ascii", "replace").strip(),
                "revision": data[32:36].decode("ascii", "replace").strip()}
        print("INQUIRY", json.dumps(info), flush=True)
        if info != {"vendor": "Buildwin", "product": "SPI080-Loader", "revision": "1.00"}:
            raise RuntimeError("Device is not the exact supported radio loader")
        return info

    def challenge(self, encrypted=False):
        packet = bytes([0xFB if encrypted else 0xFA, 9]) + bytes(14)
        if encrypted:
            packet = encode_command(packet, self.key)
        result = self.command(packet, incoming=4)
        self.key = int.from_bytes(result[:2], "big")
        return result

    def unlock(self):
        # The key-entry path already activates the loader. It rejects a
        # redundant FA00/FB00 unlock, which MPTool's upload path ignores.
        self.challenge()
        self.count = 0
        print("Active loader session established", flush=True)

    def encoded(self, plain, incoming=0, outgoing=None):
        # A fresh plaintext challenge works at any point in this active
        # loader, avoiding dependence on MPTool's three-command refresh.
        self.challenge()
        return self.command(encode_command(plain, self.key), incoming, outgoing)

    def read_ram(self, address, size):
        plain = bytes([0xFB, 4]) + address.to_bytes(4, "big") + bytes(10)
        return self.encoded(plain, incoming=size)

    def upload_helper(self, helper):
        for offset in range(0, len(helper), 512):
            plain = bytes([0xFB, 3]) + (0x2200 + offset).to_bytes(4, "big") + bytes(10)
            self.encoded(plain, outgoing=helper[offset:offset + 512])
        for offset in range(0, len(helper), 512):
            if self.read_ram(0x2200 + offset, 512) != helper[offset:offset + 512]:
                raise RuntimeError(f"RAM helper verification failed at {offset:#x}")
        print("Volatile RAM helper uploaded and read back exactly", flush=True)
        # The initialization body only configures SPI/GPIO and RAM state.
        self.encoded(bytes([0xFB, 2]) + (0x2200).to_bytes(4, "big") + bytes(10))
        print("SPI read interface initialized", flush=True)
        # MPTool 0x40be4e configures an eight-byte SPI opcode table in RAM
        # using helper command 6 and a 512-byte OUT transfer. Only install
        # ordinary READ (03); leave all write/erase/status opcodes zero.
        config = bytes([3]) + bytes(7)
        plain = bytes([0xFB, 2]) + (0x2200).to_bytes(4, "big") + bytes([6]) + bytes(9)
        self.encoded(plain, outgoing=config + bytes(504))
        if self.read_ram(0x1EF7, 512)[:8] != config:
            raise RuntimeError("Volatile SPI read-opcode configuration did not match")
        print("Volatile SPI READ opcode configured; write/erase opcodes remain zero", flush=True)

    def read_flash(self, address, size):
        if not 512 <= size <= 65536 or size % 512:
            raise ValueError("Use whole 512-byte sectors")
        return self.encoded(flash_read_plain(address, size), incoming=size)


class RawTransport(Transport):
    """USB Bulk-Only Transport, avoiding Linux's legacy SCSI LUN-bit rewrite."""
    def __init__(self, device=None):
        self.usb = C.CDLL("libusb-1.0.so.0")
        self.usb.libusb_init.argtypes = [C.POINTER(C.c_void_p)]
        self.usb.libusb_open_device_with_vid_pid.argtypes = [C.c_void_p, C.c_ushort, C.c_ushort]
        self.usb.libusb_open_device_with_vid_pid.restype = C.c_void_p
        self.usb.libusb_set_auto_detach_kernel_driver.argtypes = [C.c_void_p, C.c_int]
        self.usb.libusb_claim_interface.argtypes = [C.c_void_p, C.c_int]
        self.usb.libusb_release_interface.argtypes = [C.c_void_p, C.c_int]
        self.usb.libusb_close.argtypes = [C.c_void_p]
        self.usb.libusb_exit.argtypes = [C.c_void_p]
        self.usb.libusb_clear_halt.argtypes = [C.c_void_p, C.c_ubyte]
        self.usb.libusb_bulk_transfer.argtypes = [C.c_void_p, C.c_ubyte, C.c_void_p,
                                                C.c_int, C.POINTER(C.c_int), C.c_uint]
        self.context = C.c_void_p()
        self.handle = None
        self.claimed = False
        self.key = None
        self.count = 0
        self.tag = 0
        if self.usb.libusb_init(C.byref(self.context)):
            raise RuntimeError("libusb initialization failed")
        self.handle = self.usb.libusb_open_device_with_vid_pid(self.context, 0x1908, 0x4870)
        if not self.handle:
            raise PermissionError("Cannot open 1908:4870 through libusb; check USB device-node access")
        self.usb.libusb_set_auto_detach_kernel_driver(self.handle, 1)
        result = self.usb.libusb_claim_interface(self.handle, 0)
        if result:
            self.close()
            raise RuntimeError(f"Cannot claim loader interface: libusb {result}")
        self.claimed = True

    def close(self):
        if self.handle:
            if self.claimed:
                self.usb.libusb_release_interface(self.handle, 0)
            self.usb.libusb_close(self.handle)
            self.handle = None
        if self.context:
            self.usb.libusb_exit(self.context)
            self.context = None

    def bulk(self, endpoint, outgoing=None, size=0, allow_stall=False):
        length = len(outgoing) if outgoing is not None else size
        buf = C.create_string_buffer(max(length, 1))
        if outgoing is not None:
            C.memmove(buf, outgoing, length)
        transferred = C.c_int()
        result = self.usb.libusb_bulk_transfer(
            self.handle, endpoint, buf, length, C.byref(transferred), 10000)
        if result == -9 and allow_stall:
            self.usb.libusb_clear_halt(self.handle, endpoint)
            return buf.raw[:transferred.value]
        if result:
            raise RuntimeError(f"USB endpoint {endpoint:#x}: libusb {result}, transferred {transferred.value}")
        if outgoing is not None and transferred.value != length:
            raise RuntimeError("Short USB write")
        return buf.raw[:transferred.value]

    def command(self, cdb, incoming=0, outgoing=None):
        # Normalize before sending a CBW: ctypes rejects bytearray after the
        # loader has already entered its data stage, leaving BOT incomplete.
        if outgoing is not None:outgoing=bytes(outgoing)
        if not 1 <= len(cdb) <= 16:
            raise ValueError("Invalid CDB length")
        self.tag += 1
        length = len(outgoing) if outgoing is not None else incoming
        cbw = struct.pack("<IIIBBB16s", 0x43425355, self.tag, length,
                          0 if outgoing is not None else 0x80, 0, len(cdb), cdb.ljust(16, b"\0"))
        self.bulk(1, outgoing=cbw)
        data = b""
        if outgoing is not None:
            self.bulk(1, outgoing=outgoing, allow_stall=True)
        elif incoming:
            data = self.bulk(0x81, size=incoming, allow_stall=True)
        status = self.bulk(0x81, size=13, allow_stall=True)
        if len(status) != 13:
            raise RuntimeError(f"Invalid USB CSW: {status.hex()}")
        signature, tag, residual, error = struct.unpack("<IIIB", status)
        if signature != 0x53425355 or tag != self.tag:
            raise RuntimeError(f"USB CSW identity mismatch: {status.hex()}")
        if error or residual or (incoming and len(data) != incoming):
            raise RuntimeError(f"USB CDB {cdb.hex()} failed: status={error}, residual={residual}, bytes={len(data)}")
        return data


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default="/dev/sg2")
    ap.add_argument("--raw-usb", action="store_true", help="bypass SCSI LUN-bit modification")
    ap.add_argument("--output", type=Path)
    ap.add_argument("--size", type=lambda s: int(s, 0), default=0x200000)
    args = ap.parse_args()
    if args.size <= 0 or args.size % 4096 or args.size > 0x1000000:
        ap.error("size must be a positive multiple of 4096, at most 16 MiB")
    if args.output and (args.output.exists() or args.output.with_suffix(".verify.bin").exists()):
        ap.error("output already exists")
    helper = (ROOT / "reference/spi080-driver-decoded.bin").read_bytes()
    if hashlib.sha256(helper).hexdigest() != HELPER_SHA256:
        raise RuntimeError("Helper hash mismatch")
    transport = RawTransport() if args.raw_usb else Transport(args.device)
    import atexit
    if args.raw_usb:
        atexit.register(transport.close)
    identity = transport.identify()
    transport.unlock()
    if not args.output:
        print("Probe complete; no helper uploaded and no flash read", flush=True)
        return
    transport.upload_helper(helper)
    # Keep a short read even if a later step fails, for diagnosis.
    head = transport.read_flash(0, 4096)
    print("First 16 flash bytes:", head[:16].hex(" "), flush=True)
    if len(set(head)) < 16:
        raise RuntimeError("Flash sample has implausibly little variation")
    paths = [args.output, args.output.with_suffix(".verify.bin")]
    hashes = []
    for number, path in enumerate(paths, 1):
        digest = hashlib.sha256()
        started = time.monotonic()
        with path.open("xb") as f:
            for address in range(0, args.size, 4096):
                data = transport.read_flash(address, 4096)
                f.write(data)
                digest.update(data)
                if (address + 4096) % 0x40000 == 0:
                    print(f"Pass {number}: {address + 4096:#x}/{args.size:#x}", flush=True)
        hashes.append(digest.hexdigest())
        print(f"Pass {number} SHA256 {hashes[-1]} ({time.monotonic()-started:.1f}s)", flush=True)
    if hashes[0] != hashes[1]:
        raise RuntimeError("Independent full flash reads differ; preserved both files")
    result = {"identity": identity, "bytes": args.size, "sha256": hashes[0],
              "matching_independent_reads": True, "files": [str(p.resolve()) for p in paths]}
    args.output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
    print("VERIFIED USB FLASH BACKUP", json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print("Stopped:", error, file=sys.stderr, flush=True)
        sys.exit(1)
