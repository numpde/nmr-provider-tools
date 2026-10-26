override SHELL := bash
override .SHELLFLAGS := -eu -o pipefail -c
override .DEFAULT_GOAL := help

.PHONY: help tests tests/repository tests/repository/quality tests/repository/runtime tests/repository/command

help:
	@printf '%s\n' \
	  'make help                        Show maintained commands.' \
	  'make tests                       Run all current validation lanes.' \
	  'make tests/repository            Run repository conventions and detector fixtures offline.' \
	  'make tests/repository/quality    Run code and operator convention checks offline.' \
	  'make tests/repository/runtime    Inspect the checker container grants.' \
	  'make tests/repository/command    Print the repository checker command without starting it.'

tests: tests/repository

tests/repository:
	bash tools/repository.sh tests/repository

tests/repository/quality:
	bash tools/repository.sh tests/repository/quality

tests/repository/runtime:
	bash tools/repository.sh tests/repository/runtime

tests/repository/command:
	bash tools/repository.sh --print tests/repository
