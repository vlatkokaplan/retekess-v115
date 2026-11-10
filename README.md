# Retekess V115 Custom Firmware

Expanded tuning, signal history, configurable scanning, saved preferences, and
screen sleep for the Retekess V115. This project patches a verified stock v1.4 image and
assembles the added 8051 code directly in Python.

**Current version: 1.5-RC5 · Linux development tools · 2 MiB SPI flash**

![Radio screen, C/N history, and scanning controls](assets/ui-preview.png)

## Features

- **Extended tuning:** FM 30–230 MHz and SW-mode AM 150–30,000 kHz. Original
  MW 9/10 kHz bands are retained.
- **FM controls:** 200/150/100/50 kHz bandwidth, automatic stereo or forced
  mono, bass boost, and 50/75 µs de-emphasis.
- **Clearer interface:** a large frequency readout, a relative C/N bar, and
  separate FM/AM settings pages.
- **Signal graph:** 32 one-second C/N samples, peak hold, and a fixed 0–60 dB
  scale. History resets on frequency/band changes and pauses in menus, scanning,
  and screen sleep.
- **Preview scanning:** bounded ranges, C/N thresholds, station dwell, direction,
  pause/resume, and skip. It does not replace the saved station list.
- **Signal diagnostics:** C/N, estimated RF level, frequency offset, stereo
  status, tuner gain, and tuner supply voltage.
- **Display sleep:** switches off the LCD and backlight while audio continues.
  The first complete key gesture wakes the screen without changing a setting.
- **Settings and help:** an on-radio menu, saved FM preferences, and automatic
  screen sleep after 10, 30, or 60 seconds, or Off.
- **Versioned startup:** firmware version and build date on the welcome screen.

RC5 fixes the constant **127 dB** C/N reading in RC4. The stock tuner helper
NACKs after its first byte, so multi-register requests returned `0xFF` for later
registers. Signal measurements now use separate single-register requests. A live
RAM test reproduced the failure and measured 4–5 dB at 95.0 MHz versus about
10 dB at 95.3 MHz with the corrected protocol. The scanner uses the same fix and
rejects all-ones status/C/N reads. RC5 passed the full release suite and was flashed
with two matching full-image readbacks, preserving the boot area, settings, and
station lists. Normal-boot acceptance of RC5 is pending; the RC4 screens were
exercised on the physical LCD from RAM.

## On the radio

| RC3 startup | Settings menu |
| :---: | :---: |
| <img src="assets/IMG_20261110_093107.jpg" alt="Retekess V115 showing the custom firmware 1.5-RC3 welcome screen" width="330"> | <img src="assets/IMG_20261110_093115.jpg" alt="Retekess V115 showing the RC3 settings menu" width="330"> |

## Controls

Use these shortcuts during idle radio playback with the keypad unlocked.

| Hold | Action |
| --- | --- |
| **1** | Cycle FM bandwidth: 200 → 150 → 100 → 50 kHz |
| **2** | Open or close signal diagnostics |
| **3** | Toggle FM automatic stereo / forced mono |
| **4** | Switch off the screen and backlight |
| **6** | Toggle FM 50/75 µs de-emphasis |
| **7** | Toggle FM bass boost |
| **8** | Open settings |

Stock **hold 5** retains its numeric-entry behavior. Normal short-digit entry
is retained outside the custom screens.

Inside **settings**, use:

| Key | Action |
| --- | --- |
| **1 / 2 / 3 / 4** | FM bandwidth / mono / bass / de-emphasis |
| **5** | Screen timeout: Off → 10 → 30 → 60 seconds |
| **6** | Save, exit, and switch off the screen |
| **7** | Save, exit, and open diagnostics |
| **8** | Open preview-scan options |
| **9** | Show or hide shortcut help |
| **0**, or **hold 8** | Save and exit |

FM settings apply only in FM. Menu changes save on exit; shortcut changes save
after about three seconds of idle playback. Wait for that save before removing
power or changing tasks. A `*` in the menu means unsaved changes; `!`
means a save/readback failed and will be retried. RC3 preferences load without
losing FM or screen-timeout choices; scan settings are added on the next save.
Automatic screen sleep defaults to **Off**, pauses
in settings and during scanning, and resets its countdown on key activity.

Inside **FM diagnostics**, short keys are **1** bandwidth, **2** mono, **3**
exit, **4** screen off, **5** bass, and **6** de-emphasis. In **AM diagnostics**,
use **3** to exit and **4** for screen off. In either meter, **7** switches
between values and the signal graph. In the graph, **0** clears history.

## Preview scanning

Open **hold 8 → 8**. Tune to the desired area before opening the scanner.

| Key | Action |
| --- | --- |
| **1** | Range: Broadcast → Near → Wide → Full |
| **2** | Minimum C/N: 4 → 8 → 12 → 16 dB |
| **3** | Station pause: 2 → 5 → 10 seconds |
| **4** | Direction: up / down |
| **5** | Run, pause, or resume |
| **6** | Skip to the next channel |
| **7** | Restart from the selected range boundary |
| **8** | Stop and return to settings |
| **0** | Stop and return to the radio screen |

Broadcast uses FM 87–108 MHz, the original MW band, or SW 4.75–21.85 MHz.
Near spans ±1 MHz in FM or ±100 kHz in AM; Wide spans ±5 MHz or ±500 kHz.
Full uses the expanded band limits. All ranges are clamped to the band and
aligned to its existing tuning step; the actual boundaries appear on screen.

Scanning starts at the current channel when it is inside the range and stops
at the far boundary. A station must complete tuning, report lock, and meet the
C/N threshold before the dwell pause. A tuning timeout advances the scan.
**Hold 4** pauses scanning and sleeps the LCD; after waking, use **5** to resume.
Other ordinary radio controls leave preview scanning before normal handling.
Scan options save when leaving the scanner/menu. The original scan controls
retain their stock behavior.

## Build

Requirements: Python 3.10+, Pillow, and `make`. Full instruction verification
also needs **uCsim `s51`**; install the `sdcc-ucsim` package on Debian/Ubuntu or
set `S51` to the executable path. USB tools use the system `libusb-1.0` library.

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 fetch_references.py --firmware
make build
make verify
```

`fetch_references.py` downloads the public stock v1.4 image and matching vendor
loader helper, verifies their SHA-256 hashes, and decodes the helper locally.
These downloaded files are ignored by Git. To use an existing stock backup,
run the fetcher without `--firmware` and pass `SOURCE=/path/to/stock.bin` to
`make build` and `make verify`. The executable and CRC-table prefix must match
the researched stock v1.4 layout; individual saved settings may differ.

The output is `build/v115-full-features-rc5.bin`, a complete scrambled SPI
image, with a manifest, validation reports, decoded analysis image, and boot
preview alongside it. **Program the `.bin` image, not `.decoded.bin`.**
Builds are deterministic. Existing image outputs are not overwritten; use
`make clean` before rebuilding, or select a new output with `IMAGE=...`.

`make smoke` runs a quick display check. `make verify` runs the full suite:

- 8,651 native instruction cases and 2,048 stock LCD guard/ownership cases.
- 1,488 cold-start preference cases, including RC3 migration and invalid records.
- 10 signal-history sequences, 28 graph pixel cases, and 320 home/menu pixel cases.
- 471 scanner state-machine cases and 80 scanner pixel cases.
- 10,226 tuning-grid frequencies, 90 boundaries, and 10 UI getters.
- Rebuild, scrambling, header/section CRCs, all 61 block CRCs, and resource
  preservation checks.
- A NOR-flash model covering update guards, interrupted writes, oversized
  erase detection, and restoration of the original image.

## Backup and flash

The USB tools target **SPI080-Loader 1.00** (`1908:4870`). The writer supports
the tested **Zbit `5e4015`** flash profile: 2 MiB capacity, 4 KiB sectors, and
256-byte pages. Other loaders and flash chips are rejected.

1. Unplug USB and remove the battery. Hold **1** while reconnecting a USB data
   cable. If USB alone does not power the radio, reinsert the battery while
   still holding **1**. Release it once the loader appears.
2. Give your user access to the matching USB device. Find its current bus and
   device numbers with `lsusb -d 1908:4870`.
3. Make a fresh double-read backup:

   ```sh
   mkdir -p local
   python3 usb_dump.py --raw-usb --output local/radio.bin
   ```

4. Prepare and inspect an offline update plan:

   ```sh
   python3 flash_update.py --baseline local/radio.bin --source stock.bin \
     --image build/v115-full-features-rc5.bin --session local/plan
   ```

5. To program, use a new session directory and add `--write`:

   ```sh
   python3 flash_update.py --baseline local/radio.bin --source stock.bin \
     --image build/v115-full-features-rc5.bin --session local/flash --write
   ```

The writer accepts supported stock firmware, this exact release, and the reviewed
RC3/RC4 executables. Before upgrading either release, run the migration model
against your fresh local backup (or the exact prior-release image):

```sh
python3 verify_flash_update.py --source stock.bin --prior local/radio.bin
```

This models complete programming and recovery from four partial writes. The
writer requires this image-bound migration evidence for reviewed prior releases;
other custom releases need a separately reviewed migration. It requires matching backup
reads and image-bound validation, preserves the first **8 KiB**, retains current
settings and station lists, and changes only application code, its CRC table,
and the existing welcome bitmap. It performs an erase/program/restore trial,
verifies each changed sector, then checks two complete readbacks. Session
folders retain the original image, target, plan, and recovery journal.

Keep your verified original backup outside the checkout and have SPI recovery
available. After verification finishes, disconnect USB and restart normally
without holding **1**. For an RC5 acceptance check, open settings, select **10S**,
save with **0**, test sleep/wake, then restart and check that the preference remains.
Also test **hold 2 → 7** for the graph and **hold 8 → 8** for a short scan.
`python3 preview_ui.py --all` checks all four new screens from loader RAM with
demonstration values and does not program flash.

## Implementation

| Area | Implementation |
| --- | --- |
| Image format | Per-block scrambling, section CRCs, and a 61-entry block CRC table |
| Native code | Common glue plus checked 2 KiB banks 0x2C–0x34 for views, history, preferences, and scanning |
| Preferences | Sixteen versioned bytes at group-0 offsets 0xE0–0xEF; stock record writer and CRC; V1 migration |
| Display | ST7565-compatible 128×64 LCD; SPI/DMA updates are suppressed while asleep and native draws yield to custom views |
| Protection | CRC balancing in unused application padding keeps boot/header sectors unchanged |

The current additions use 14,353 bytes of code, font, and strings. Nine
wholly empty 2 KiB application banks remain (18 KiB), plus padding in occupied banks.
History and scanner state use 128 bytes in the checked common-RAM tail; the
stock audio buffer is not borrowed.

The extended ranges are software tuning limits; reception, antenna paths, and
RF performance across them are not guaranteed. FM still steps by 100 kHz and
SW-mode AM by 5 kHz. There is no narrowband-FM demodulator; 50 kHz is the
smallest available FM filter. RF level is an estimate, not a calibrated meter.
Screen sleep leaves the CPU and radio running. The earlier quiet-clock experiment
was removed because it did not improve noise in normal operation.

All project documentation is in this README. Firmware dumps, downloaded SDKs,
flash sessions, captures, old builds, and device-specific records are excluded
from version control. `extract_images.py` can inspect image resources in a
local firmware file without adding them to the repository.

## References

- [IliyaZak/Retekess-v115-firmware](https://github.com/IliyaZak/Retekess-v115-firmware)
  — public stock firmware reference.
- [Edragon/buildwin](https://github.com/Edragon/buildwin)
  — AX2227 SDK and original MPTool loader helper.
- [AKC695X](https://github.com/pu2clr/AKC695X)
  — related tuner documentation and register research.

Stock firmware and vendor tools are external inputs and are not bundled here.
