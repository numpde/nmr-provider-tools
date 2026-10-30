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

unexport OUTPUT
unexport OUTPUT_INPUT
unexport OUTPUT_INPUT_ORIGIN
override export OUTPUT_INPUT_ORIGIN := $(origin OUTPUT)
ifeq ($(origin OUTPUT),command line)
override OUTPUT_INPUT := $(value OUTPUT)
export OUTPUT_INPUT
endif

unexport ARTIFACTS
unexport ARTIFACTS_INPUT
unexport ARTIFACTS_INPUT_ORIGIN
override export ARTIFACTS_INPUT_ORIGIN := $(origin ARTIFACTS)
ifeq ($(origin ARTIFACTS),command line)
override ARTIFACTS_INPUT := $(value ARTIFACTS)
export ARTIFACTS_INPUT
endif

unexport DELIVERY_BINDINGS
unexport DELIVERY_BINDINGS_INPUT
unexport DELIVERY_BINDINGS_INPUT_ORIGIN
override export DELIVERY_BINDINGS_INPUT_ORIGIN := $(origin DELIVERY_BINDINGS)
ifeq ($(origin DELIVERY_BINDINGS),command line)
override DELIVERY_BINDINGS_INPUT := $(value DELIVERY_BINDINGS)
export DELIVERY_BINDINGS_INPUT
endif

unexport SHARED_RUNTIME_IMAGE
unexport SHARED_RUNTIME_IMAGE_INPUT
unexport SHARED_RUNTIME_IMAGE_INPUT_ORIGIN
override export SHARED_RUNTIME_IMAGE_INPUT_ORIGIN := $(origin SHARED_RUNTIME_IMAGE)
ifeq ($(origin SHARED_RUNTIME_IMAGE),command line)
override SHARED_RUNTIME_IMAGE_INPUT := $(value SHARED_RUNTIME_IMAGE)
export SHARED_RUNTIME_IMAGE_INPUT
endif

unexport API_ORIGIN
unexport API_ORIGIN_INPUT
unexport API_ORIGIN_INPUT_ORIGIN
override export API_ORIGIN_INPUT_ORIGIN := $(origin API_ORIGIN)
ifeq ($(origin API_ORIGIN),command line)
override API_ORIGIN_INPUT := $(value API_ORIGIN)
export API_ORIGIN_INPUT
endif

unexport PROVIDER_REF
unexport PROVIDER_REF_INPUT
unexport PROVIDER_REF_INPUT_ORIGIN
override export PROVIDER_REF_INPUT_ORIGIN := $(origin PROVIDER_REF)
ifeq ($(origin PROVIDER_REF),command line)
override PROVIDER_REF_INPUT := $(value PROVIDER_REF)
export PROVIDER_REF_INPUT
endif

unexport EXECUTION_ATTEMPT_REF
unexport EXECUTION_ATTEMPT_REF_INPUT
unexport EXECUTION_ATTEMPT_REF_INPUT_ORIGIN
override export EXECUTION_ATTEMPT_REF_INPUT_ORIGIN := $(origin EXECUTION_ATTEMPT_REF)
ifeq ($(origin EXECUTION_ATTEMPT_REF),command line)
override EXECUTION_ATTEMPT_REF_INPUT := $(value EXECUTION_ATTEMPT_REF)
export EXECUTION_ATTEMPT_REF_INPUT
endif

unexport BODY_SHA256
unexport BODY_SHA256_INPUT
unexport BODY_SHA256_INPUT_ORIGIN
override export BODY_SHA256_INPUT_ORIGIN := $(origin BODY_SHA256)
ifeq ($(origin BODY_SHA256),command line)
override BODY_SHA256_INPUT := $(value BODY_SHA256)
export BODY_SHA256_INPUT
endif

unexport TAG
unexport TAG_INPUT
unexport TAG_INPUT_ORIGIN
override export TAG_INPUT_ORIGIN := $(origin TAG)
ifeq ($(origin TAG),command line)
override TAG_INPUT := $(value TAG)
export TAG_INPUT
endif

unexport PROJECT
unexport PROJECT_INPUT
unexport PROJECT_INPUT_ORIGIN
override export PROJECT_INPUT_ORIGIN := $(origin PROJECT)
ifeq ($(origin PROJECT),command line)
override PROJECT_INPUT := $(value PROJECT)
export PROJECT_INPUT
endif

.PHONY: help tests tests/repository tests/repository/quality tests/repository/runtime tests/repository/command tests/repository/provisioning
.PHONY: checks/base-image/download checks/dependencies/download checks/dependencies/check checks/image/build checks/prepare
.PHONY: delivery/deployment/up delivery/deployment/proxy/restart delivery/deployment/down delivery/deployment/status delivery/deployment/logs delivery/deployment/held delivery/deployment/discard delivery/deployment/retire-old
.PHONY: publish/prepare publish/release tests/deployment tests/deployment/route tests/deployment/route/literal delivery/image/build tests/package
.PHONY: packages/provider-core/download packages/provider-core/check package/build tests/unit/provider-core tests/contract/provider-core tests/integration/provider-core tests/integration/provider-core/delivery

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
	@printf '%s\n' \
	  'make packages/provider-core/download WHEELHOUSE=... NETWORK_INTERFACE=... Acquire locked wheels.' \
	  'make packages/provider-core/check WHEELHOUSE=... Verify locked wheels offline.' \
	  'make tests/unit/provider-core WHEELHOUSE=... Build and test the isolated installed core.' \
	  'make tests/contract/provider-core WHEELHOUSE=... Qualify installed package contracts.' \
	  'make tests/integration/provider-core WHEELHOUSE=... Run contained package integrations.' \
	  'make tests/integration/provider-core/delivery WHEELHOUSE=... Qualify real delivery IPC and recovery.' \
	  'make package/build WHEELHOUSE=... OUTPUT=... Build clean committed source into an empty owned directory.' \
	  'make tests/package WHEELHOUSE=... ARTIFACTS=... Qualify exact release files and the source rebuild.' \
	  'make delivery/image/build WHEELHOUSE=... ARTIFACTS=... Build shared delivery from qualified wheel inputs.' \
	  'make tests/deployment/route SHARED_RUNTIME_IMAGE=sha256:... Qualify private API routes with disposable local services.' \
	  'make tests/deployment/route/literal SHARED_RUNTIME_IMAGE=sha256:... Qualify mapped IP authority through private API routes.' \
	  'make tests/deployment SHARED_RUNTIME_IMAGE=sha256:... Qualify installed deployment rendering and proxy boundaries.' \
	  'make delivery/deployment/up DELIVERY_BINDINGS=... SHARED_RUNTIME_IMAGE=sha256:... Start from the shared operator settings file.' \
	  'make delivery/deployment/proxy/restart DELIVERY_BINDINGS=... SHARED_RUNTIME_IMAGE=sha256:... Replace only the independent proxy and keep its socket volume.' \
	  'make delivery/deployment/down PROJECT=... Stop shared delivery and preserve all volumes.' \
	  'make delivery/deployment/status PROJECT=... Inspect installed service state.' \
	  'make delivery/deployment/logs PROJECT=... Show bounded recent service logs.' \
	  'make delivery/deployment/held DELIVERY_BINDINGS=... SHARED_RUNTIME_IMAGE=sha256:... API_ORIGIN=... PROVIDER_REF=... List pending and held metadata while stopped.' \
	  'make delivery/deployment/discard DELIVERY_BINDINGS=... SHARED_RUNTIME_IMAGE=sha256:... API_ORIGIN=... PROVIDER_REF=... EXECUTION_ATTEMPT_REF=... BODY_SHA256=... Discard exactly one stopped held item.' \
	  'make delivery/deployment/retire-old PROJECT=... SHARED_RUNTIME_IMAGE=sha256:... Retire unused old per-provider socket volumes.' \
	  'make publish/prepare TAG=vX.Y.Z WHEELHOUSE=... OUTPUT=... Prepare exact artifacts, logs and authored release notes.' \
	  'make publish/release TAG=vX.Y.Z ARTIFACTS=... Publish reviewed inputs through a verified GitHub draft.'

packages/provider-core/download:
	bash tools/provider_core.sh download

packages/provider-core/check:
	bash tools/provider_core.sh check

tests/deployment/route:
	bash tools/delivery_route_tests.sh

tests/deployment/route/literal:
	bash tools/delivery_route_tests.sh --literal-ip

tests/deployment:
	bash tools/delivery_tests.sh

delivery/image/build:
	bash tools/delivery_image.sh

package/build:
	bash tools/provider_core.sh build

tests/package:
	bash tools/provider_core.sh package

tests/unit/provider-core:
	bash tools/provider_core.sh unit

tests/contract/provider-core:
	bash tools/provider_core.sh contract

tests/integration/provider-core:
	bash tools/provider_core.sh integration

tests/integration/provider-core/delivery:
	bash tools/provider_core.sh integration/delivery

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

tests: tests/repository tests/unit/provider-core tests/contract/provider-core tests/integration/provider-core tests/deployment

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

delivery/deployment/up:
	bash tools/delivery.sh up

delivery/deployment/proxy/restart:
	bash tools/delivery.sh proxy-restart

delivery/deployment/down:
	bash tools/delivery.sh down

delivery/deployment/status:
	bash tools/delivery.sh status

delivery/deployment/logs:
	bash tools/delivery.sh logs

delivery/deployment/held:
	bash tools/delivery.sh held

delivery/deployment/discard:
	bash tools/delivery.sh discard

delivery/deployment/retire-old:
	[[ $${PROJECT_INPUT_ORIGIN} == 'command line' && $${SHARED_RUNTIME_IMAGE_INPUT_ORIGIN} == 'command line' ]] || { printf '%s\n' 'Select PROJECT and SHARED_RUNTIME_IMAGE on the Make command line.' >&2; exit 2; }
	bash tools/delivery_retire_old.sh "$$PROJECT_INPUT" "$$SHARED_RUNTIME_IMAGE_INPUT"

publish/prepare:
	bash tools/publish.sh prepare

publish/release:
	bash tools/publish.sh release
