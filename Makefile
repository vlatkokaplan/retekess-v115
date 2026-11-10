PYTHON ?= python3
SOURCE ?= stock.bin
IMAGE ?= build/v115-full-features-rc6.bin
OPEN_IMAGE ?= build/v115-open-0.2.2.bin
BASELINE ?= local/radio.bin

.PHONY: references build verify smoke clean radio-build radio-verify radio-run radio-image radio-autonomous-verify radio-timer-build radio-timer-run radio-image-verify

references:
	$(PYTHON) fetch_references.py --firmware

build: $(IMAGE)

$(IMAGE):
	$(PYTHON) build_full_feature_image.py --source "$(SOURCE)" --output "$(IMAGE)"

smoke: $(IMAGE)
	$(PYTHON) verify_native_features.py --smoke --candidate "$(IMAGE)" --report build/smoke.json

radio-build:
	$(PYTHON) build_radio.py

radio-verify:
	$(PYTHON) verify_radio.py

radio-run: radio-build
	$(PYTHON) run_radio.py --self-test

radio-image:
	$(PYTHON) build_radio_image.py --source "$(SOURCE)" --output "$(OPEN_IMAGE)"

radio-autonomous-verify:
	$(PYTHON) verify_radio.py --autonomous --output build/radio-autonomous-sim

radio-timer-build:
	$(PYTHON) build_radio.py --autonomous --timer-batch --output build/radio-timer-preview

radio-timer-run: radio-timer-build
	$(PYTHON) preview_radio_timer.py --batches 12

radio-image-verify: radio-autonomous-verify
	$(PYTHON) verify_radio_image.py --source "$(SOURCE)" --candidate "$(OPEN_IMAGE)" --baseline "$(BASELINE)" --hardware-report build/radio-timer-preview/hardware.json

verify: $(IMAGE)
	$(PYTHON) verify_band_patch.py "$(IMAGE)"
	$(PYTHON) verify_native_features.py --candidate "$(IMAGE)" --report "$(basename $(IMAGE)).native-emulation.json"
	$(PYTHON) verify_preferences_boot.py "$(IMAGE)"
	$(PYTHON) verify_signal_graph.py --candidate "$(IMAGE)"
	$(PYTHON) verify_radio_ui.py --candidate "$(IMAGE)"
	$(PYTHON) verify_radio_scan.py --candidate "$(IMAGE)"
	$(PYTHON) verify_full_feature_image.py "$(IMAGE)" --source "$(SOURCE)"
	$(PYTHON) verify_flash_update.py --source "$(SOURCE)" $(if $(PRIOR),--prior "$(PRIOR)",)

clean:
	$(PYTHON) -c 'import shutil; shutil.rmtree("build", ignore_errors=True)'
