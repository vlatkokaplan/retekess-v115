#!/usr/bin/env python3
"""Fetch hash-pinned public inputs; downloaded vendor binaries remain untracked."""
import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen

from analyze import decode_spi_driver
from usb_dump import HELPER_SHA256

ROOT=Path(__file__).resolve().parent
DRIVER_URL='https://raw.githubusercontent.com/Edragon/buildwin/master/Tools/MPTool/spi080_driver.dll'
DRIVER_SHA='25f03a117e520a5c0d3efb8223789220a97ed15713e50dc2fbabd96e3af988a8'
FIRMWARE_URL='https://raw.githubusercontent.com/IliyaZak/Retekess-v115-firmware/main/Retekess%20V115.BIN'
FIRMWARE_SHA='a6fd05812310f783b372e616fcb16481b1c2836fcde6a934e9daddf4347b25fb'


def checked(data,digest):
    if hashlib.sha256(data).hexdigest()!=digest:
        raise ValueError('downloaded or existing file has an unexpected SHA-256')
    return data


def fetch(path,url,digest,size):
    if path.exists():return checked(path.read_bytes(),digest)
    with urlopen(url,timeout=30) as response:data=response.read(size+1)
    if len(data)!=size:raise ValueError('download has an unexpected size')
    checked(data,digest);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as output:output.write(data)
    return data


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--firmware',action='store_true',help='also fetch the public stock v1.4 image as stock.bin')
    args=parser.parse_args()
    encoded=fetch(ROOT/'reference/spi080_driver.dll',DRIVER_URL,DRIVER_SHA,3587)
    helper=checked(decode_spi_driver(encoded),HELPER_SHA256)
    output=ROOT/'reference/spi080-driver-decoded.bin'
    if output.exists():checked(output.read_bytes(),HELPER_SHA256)
    else:
        with output.open('xb') as stream:stream.write(helper)
    print('Verified SPI080-Loader 1.00 read helper')
    if args.firmware:
        fetch(ROOT/'stock.bin',FIRMWARE_URL,FIRMWARE_SHA,0x200000)
        print('Verified public V115 v1.4 source')


if __name__=='__main__':main()
