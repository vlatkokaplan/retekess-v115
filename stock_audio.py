"""Stock V115 volume reference: decoded SPI wrapper and observed native DAC writes.

Bank 9:1AF6 (SPI 7AF6) maps the 0..32 UI level through SPI 7B66,
adds 3B6E, clamps to 0..65, and calls native dac_set_volume at 20C3.
That API queues a target at 21A2. Stock common code at 04F7 invokes 20C6
on timer ticks; 20C9 restores the requested level after an analogue fade-in.

The native fade routine was exercised muted at every 0..65 target. Gain is
written to XDATA 7893; 7890 stays FE, or FF for mute. The latter's upper
control bits are not a volume field. These observations are register facts,
not a reconstruction of the unreadable internal ROM instructions.
"""
FM_VOLUME = (0,10,13,16,18,20,22,24,26,27,29,30,32,34,36,38,40,
             42,44,46,47,48,49,50,51,52,53,54,55,56,58,59,60)
NATIVE_GAIN = (0,32,64,96,128,160,1,33,65,97,129,161,3,35,67,99,131,163,
               5,37,69,101,133,165,7,39,71,103,135,167,9,41,73,105,137,169,
               11,43,75,107,139,171,13,45,77,109,141,173,15,47,79,111,143,
               175,113,145,177,115,147,179,181,183,185,187,189,191)
VOLUME_REGISTERS = tuple(NATIVE_GAIN[value] for value in FM_VOLUME)


def verify_stock_curve(source):
    from band_patch import decode, is_supported
    if not is_supported(source):raise ValueError('verified stock v1.4 required')
    plain=decode(source)
    assert tuple(plain[0x7B66:0x7B87])==FM_VOLUME
    assert plain[0x7B3E:0x7B45]==bytes.fromhex('12 20 c3 d3 02 01 00')
    assert plain[0x1CF7:0x1CFA]==bytes.fromhex('12 20 c6')
    return plain
