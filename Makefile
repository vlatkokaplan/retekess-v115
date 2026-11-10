PYTHON ?= python3
SOURCE ?= stock.bin
IMAGE ?= build/v115-full-features-rc3.bin

.PHONY: references build verify smoke clean

references:
	$(PYTHON) fetch_references.py --firmware

build: $(IMAGE)

$(IMAGE):
	$(PYTHON) build_full_feature_image.py --source "$(SOURCE)" --output "$(IMAGE)"

smoke: $(IMAGE)
	$(PYTHON) verify_native_features.py --smoke --candidate "$(IMAGE)" --report build/smoke.json

verify: $(IMAGE)
	$(PYTHON) verify_band_patch.py "$(IMAGE)"
	$(PYTHON) verify_native_features.py --candidate "$(IMAGE)" --report "$(basename $(IMAGE)).native-emulation.json"
	$(PYTHON) verify_preferences_boot.py "$(IMAGE)"
	$(PYTHON) verify_full_feature_image.py "$(IMAGE)" --source "$(SOURCE)"
	$(PYTHON) verify_flash_update.py --source "$(SOURCE)"

clean:
	$(PYTHON) -c 'import shutil; shutil.rmtree("build", ignore_errors=True)'
