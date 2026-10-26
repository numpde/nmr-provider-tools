override SHELL := bash
override .SHELLFLAGS := -eu -o pipefail -c
override .DEFAULT_GOAL := help

unexport NETWORK_INTERFACE
unexport NETWORK_INTERFACE_INPUT
unexport NETWORK_INTERFACE_INPUT_ORIGIN
override export NETWORK_INTERFACE_INPUT_ORIGIN := $(origin NETWORK_INTERFACE)
ifeq ($(origin NETWORK_INTERFACE),command line)
override NETWORK_INTERFACE_INPUT := $(value NETWORK_INTERFACE)
export NETWORK_INTERFACE_INPUT
endif
unexport WHEELHOUSE
unexport WHEELHOUSE_INPUT
unexport WHEELHOUSE_INPUT_ORIGIN
override export WHEELHOUSE_INPUT_ORIGIN := $(origin WHEELHOUSE)
ifeq ($(origin WHEELHOUSE),command line)
override WHEELHOUSE_INPUT := $(value WHEELHOUSE)
export WHEELHOUSE_INPUT
endif

.PHONY: help tests tests/repository tests/repository/quality tests/repository/runtime tests/repository/command tests/repository/provisioning
.PHONY: checks/base-image/download checks/dependencies/download checks/dependencies/check checks/image/build checks/prepare

help:
	@printf '%s\n' \
	  'make help                        Show maintained commands.' \
	  'make tests                       Run all current validation lanes.' \
	  'make tests/repository            Run repository conventions and detector fixtures offline.' \
	  'make tests/repository/quality    Run code and operator convention checks offline.' \
	  'make tests/repository/provisioning Run checker preparation and cleanup fixtures offline.' \
	  'make tests/repository/runtime    Inspect the checker container grants.' \
	  'make tests/repository/command    Print the repository checker command without starting it.' \
	  'make checks/base-image/download Bootstrap the pinned base using Docker daemon routing.' \
	  'make checks/dependencies/download WHEELHOUSE=... NETWORK_INTERFACE=... Acquire declared artifacts.' \
	  'make checks/dependencies/check WHEELHOUSE=... Verify cached artifacts offline.' \
	  'make checks/image/build WHEELHOUSE=... Build the checker offline.' \
	  'make checks/prepare WHEELHOUSE=... NETWORK_INTERFACE=... Acquire artifacts and build.'

checks/base-image/download:
	bash tools/checker.sh base-download

checks/dependencies/download:
	bash tools/checker.sh download

checks/dependencies/check:
	bash tools/checker.sh check

checks/image/build:
	bash tools/checker.sh build

checks/prepare:
	bash tools/checker.sh prepare

tests: tests/repository

tests/repository:
	bash tools/repository.sh tests/repository

tests/repository/quality:
	bash tools/repository.sh tests/repository/quality

tests/repository/provisioning:
	bash tools/repository.sh tests/repository/provisioning

tests/repository/runtime:
	bash tools/repository.sh tests/repository/runtime

tests/repository/command:
	bash tools/repository.sh --print tests/repository
