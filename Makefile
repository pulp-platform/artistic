# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

PYTHON        ?= python3
SCRIPTS       ?= scripts
KLAYOUT       ?= klayout
CFG_FILE      ?= /dev/null


CHIPNAME  := $(shell $(PYTHON) $(SCRIPTS)/fetch_key.py $(CFG_FILE) general chip)
WORKDIR   := $(shell $(PYTHON) $(SCRIPTS)/fetch_key.py $(CFG_FILE) work dir)
SCALE_FAC := $(shell $(PYTHON) $(SCRIPTS)/list_files.py $(CFG_FILE) SCALE "")
ROOT_DIR  := $(shell pwd)


.PHONY: analyze
analyze:
	mkdir -p $(WORKDIR)
	$(PYTHON) $(SCRIPTS)/analyze.py $(CFG_FILE)

.SECONDEXPANSION:
.SECONDARY:
.DELETE_ON_ERROR:
.PRECIOUS:

# Phony targets
.PHONY: all
all : gen_raw gen_pdfs

# color rule
$(WORKDIR)/COL__%.png: $(WORKDIR)/RAW__%.png $(CFG_FILE)
	convert \
	    $< \
	    -limit thread 1 \
	    -negate \
	    -background $(shell $(PYTHON) $(SCRIPTS)/fetch_color.py $(CFG_FILE) $< color) \
	    -alpha shape \
	    -alpha set \
	    -background none \
	    -channel A \
	    -evaluate multiply $(shell $(PYTHON) $(SCRIPTS)/fetch_color.py $(CFG_FILE) $< alpha) \
	    +channel \
	    $@

# merge tiles
$(WORKDIR)/MRG__%.png: $$(shell $(PYTHON) $(SCRIPTS)/list_files.py $(CFG_FILE) COL $$@)
	convert $(shell $(PYTHON) $(SCRIPTS)/list_files.py $(CFG_FILE) CMP $<)  \
		-limit thread 1 \
	    -background black \
	    -alpha remove \
	    -alpha off \
	    $@

# resize tiles
$(WORKDIR)/RSZ__%.png: $(WORKDIR)/MRG__%.png $(CFG_FILE)
	convert \
		$< \
		-scale $(SCALE_FAC)% \
		$@

# merge tiles
$(WORKDIR)/SEG__%.png: $$(shell $(PYTHON) $(SCRIPTS)/list_files.py $(CFG_FILE) SEGSRC $$@)
	$(shell $(PYTHON) $(SCRIPTS)/list_files.py $(CFG_FILE) SEGGEN $@)

# change dpi
$(WORKDIR)/DPI__%.png: $(WORKDIR)/SEG__%.png $(CFG_FILE)
	convert \
	    $< \
	    -units PixelsPerInch \
	    -density $(shell $(PYTHON) $(SCRIPTS)/list_files.py $(CFG_FILE) DPI_SCALE $@) \
	    -page $(shell $(PYTHON) $(SCRIPTS)/list_files.py $(CFG_FILE) PAGE_PX $@) \
	    -gravity center \
	    -interlace none \
	    -background white \
	    -alpha remove \
	    -alpha off \
	    -flatten \
	    -format png \
	    $@

# generate PDF
$(WORKDIR)/PDF__%.pdf: $(WORKDIR)/DPI__%.png $(CFG_FILE)
	img2pdf \
	    --verbose \
	    --pillow-limit-break \
	    $< \
	    -o \
	    $@

# generate raw layer files from KLayout
.PHONY: gen_raw
gen_raw: $(CFG_FILE) $(SCRIPTS)/gen_layer_props.py $(SCRIPTS)/png_export.lym
	mkdir -p $(WORKDIR)
	cp $(CFG_FILE) $(WORKDIR)/chip.json
	$(PYTHON) $(SCRIPTS)/gen_layer_props.py $(CFG_FILE) > $(WORKDIR)/$(CHIPNAME).lyp
	cd $(WORKDIR); $(KLAYOUT) -zz -rm $(ROOT_DIR)/$(SCRIPTS)/png_export.lym

.PHONY: gen_tiles
gen_tiles: $$(shell $(PYTHON) $(SCRIPTS)/list_files.py $(CFG_FILE) MRG "")

.PHONY: gen_resized_tiles
gen_resized_tiles: $$(shell $(PYTHON) $(SCRIPTS)/list_files.py $(CFG_FILE) RSZ "")

.PHONY: gen_segs
gen_segs: $$(shell $(PYTHON) $(SCRIPTS)/list_files.py $(CFG_FILE) SEG "")

.PHONY: gen_pdfs
gen_pdfs: $$(shell $(PYTHON) $(SCRIPTS)/list_files.py $(CFG_FILE) PDF "")
