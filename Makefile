# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

PROJECT ?= examples/mlem/project.toml
ARTISTIC ?= ./bin/artistic

.PHONY: all inspect logo-prepare logo-merge logo render-generate render-compose render \
        map-generate map-build map

all:
	$(MAKE) logo
	$(MAKE) render
	$(MAKE) map
inspect:
	$(ARTISTIC) inspect $(PROJECT)
logo-prepare:
	$(ARTISTIC) logo prepare $(PROJECT)
logo-merge:
	$(ARTISTIC) logo merge $(PROJECT)
logo:
	$(MAKE) logo-prepare
	$(MAKE) logo-merge
render-generate:
	$(ARTISTIC) render generate $(PROJECT)
render-compose:
	$(ARTISTIC) render compose $(PROJECT)
render:
	$(MAKE) render-generate
	$(MAKE) render-compose
map-generate:
	$(ARTISTIC) map generate $(PROJECT)
map-build:
	$(ARTISTIC) map build $(PROJECT)
map:
	$(MAKE) map-generate
	$(MAKE) map-build
