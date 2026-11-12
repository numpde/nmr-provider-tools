override SHELL := bash
override .SHELLFLAGS := -eu -o pipefail -c
override .DEFAULT_GOAL := help

# Make documents the CLI and forwards explicit arguments; invoked scripts
# own input validation, avoiding two independent sets of admission rules.
# Non-isolated host Python helpers must never write bytecode into source trees.
# Isolated Python (-I) still requires an explicit -B in its own launch argv.
override export PYTHONDONTWRITEBYTECODE := 1

unexport DOCKER_CONTEXT
unexport DOCKER_CONTEXT_INPUT
unexport DOCKER_CONTEXT_INPUT_ORIGIN
unexport NMR_SELECTED_DOCKER_ENDPOINT
override export DOCKER_CONTEXT_INPUT_ORIGIN := $(origin DOCKER_CONTEXT)
ifeq ($(origin DOCKER_CONTEXT),command line)
override DOCKER_CONTEXT_INPUT := $(value DOCKER_CONTEXT)
export DOCKER_CONTEXT_INPUT
endif

ifneq ($(filter tests/acquisition/network tests/acquisition/shared-network,$(MAKECMDGOALS)),)
ifneq ($(origin DOCKER_CONTEXT),command line)
$(error Acquisition network tests require DOCKER_CONTEXT=<local-name> on the command line)
endif
endif

# Select once for every Docker goal in this invocation. Help and publish/release
# use no Docker; child processes inherit the captured endpoint.
ifneq ($(strip $(filter-out help publish/release checks/python-cache/clean packages/acquisition-bootstrap/check packages/acquisition-bootstrap/refresh,$(MAKECMDGOALS))),)
ifeq ($(origin DOCKER_CONTEXT),command line)
docker_context_quote = '$(subst ','"'"',$(1))'
override export NMR_SELECTED_DOCKER_ENDPOINT := $(shell env -u NMR_SELECTED_DOCKER_ENDPOINT -u DOCKER_CONTEXT_INPUT -u DOCKER_CONTEXT_INPUT_ORIGIN DOCKER_CONTEXT_INPUT_ORIGIN='command line' DOCKER_CONTEXT_INPUT=$(call docker_context_quote,$(value DOCKER_CONTEXT)) bash host/tools/docker_context.sh --endpoint)
else
override export NMR_SELECTED_DOCKER_ENDPOINT := $(shell env -u NMR_SELECTED_DOCKER_ENDPOINT -u DOCKER_CONTEXT_INPUT -u DOCKER_CONTEXT_INPUT_ORIGIN bash host/tools/docker_context.sh --endpoint)
endif
ifeq ($(strip $(NMR_SELECTED_DOCKER_ENDPOINT)),)
$(error Select a local Docker context for this operation)
endif
endif

unexport ENGINE_CONFIG ENGINE_CONFIG_INPUT ENGINE_CONFIG_INPUT_ORIGIN
override export ENGINE_CONFIG_INPUT_ORIGIN := $(origin ENGINE_CONFIG)
ifeq ($(origin ENGINE_CONFIG),command line)
override ENGINE_CONFIG_INPUT := $(value ENGINE_CONFIG)
export ENGINE_CONFIG_INPUT
endif

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

unexport FUZZ_PROFILE
unexport FUZZ_PROFILE_INPUT
unexport FUZZ_PROFILE_INPUT_ORIGIN
override export FUZZ_PROFILE_INPUT_ORIGIN := $(origin FUZZ_PROFILE)
ifeq ($(origin FUZZ_PROFILE),command line)
override FUZZ_PROFILE_INPUT := $(value FUZZ_PROFILE)
export FUZZ_PROFILE_INPUT
endif

unexport OUTPUT
unexport OUTPUT_INPUT
unexport OUTPUT_INPUT_ORIGIN
override export OUTPUT_INPUT_ORIGIN := $(origin OUTPUT)
ifeq ($(origin OUTPUT),command line)
override OUTPUT_INPUT := $(value OUTPUT)
export OUTPUT_INPUT
endif

unexport LOCK
unexport LOCK_INPUT
unexport LOCK_INPUT_ORIGIN
override export LOCK_INPUT_ORIGIN := $(origin LOCK)
ifeq ($(origin LOCK),command line)
override LOCK_INPUT := $(value LOCK)
export LOCK_INPUT
endif

unexport INDEX_URL SOURCE_URLS REQUIRE_SOURCE
override export LOCATE_INDEX_INPUT := $(if $(filter command line,$(origin INDEX_URL)),$(value INDEX_URL))
override export LOCATE_SOURCES_INPUT := $(if $(filter command line,$(origin SOURCE_URLS)),$(value SOURCE_URLS))
override export LOCATE_REQUIRED_INPUT := $(if $(filter command line,$(origin REQUIRE_SOURCE)),$(value REQUIRE_SOURCE))
unexport SOURCE_LOCK
override export SOURCE_LOCK_INPUT := $(if $(filter command line,$(origin SOURCE_LOCK)),$(value SOURCE_LOCK))

unexport ARTIFACTS
unexport ARTIFACTS_INPUT
unexport ARTIFACTS_INPUT_ORIGIN
override export ARTIFACTS_INPUT_ORIGIN := $(origin ARTIFACTS)
ifeq ($(origin ARTIFACTS),command line)
override ARTIFACTS_INPUT := $(value ARTIFACTS)
export ARTIFACTS_INPUT
endif

unexport DEPLOYMENT
unexport DEPLOYMENT_INPUT
unexport DEPLOYMENT_INPUT_ORIGIN
override export DEPLOYMENT_INPUT_ORIGIN := $(origin DEPLOYMENT)
ifeq ($(origin DEPLOYMENT),command line)
override DEPLOYMENT_INPUT := $(value DEPLOYMENT)
export DEPLOYMENT_INPUT
endif

unexport SHARED_SETTINGS
unexport SHARED_SETTINGS_INPUT
unexport SHARED_SETTINGS_INPUT_ORIGIN
override export SHARED_SETTINGS_INPUT_ORIGIN := $(origin SHARED_SETTINGS)
ifeq ($(origin SHARED_SETTINGS),command line)
override SHARED_SETTINGS_INPUT := $(value SHARED_SETTINGS)
export SHARED_SETTINGS_INPUT
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

.PHONY: engine/cache/gc engine/cache/gc/install
.PHONY: packages/images/retire
.PHONY: tests/acquisition/identity tests/acquisition/network help tests tests/repository tests/repository/quality tests/repository/runtime tests/repository/command tests/repository/provisioning tests/repository/release tests/repository/network tests/repository/network/identity
.PHONY: checks/base-image/download checks/dependencies/download checks/dependencies/check checks/image/build checks/prepare
.PHONY: delivery/deployment/up delivery/deployment/update delivery/deployment/proxy/restart delivery/deployment/down delivery/deployment/status delivery/deployment/logs delivery/deployment/held delivery/deployment/discard delivery/deployment/retire-old
.PHONY: delivery/prepare publish/prepare publish/release tests/deployment tests/deployment/route tests/deployment/route/literal delivery/image/build tests/package
.PHONY: packages/provider-core/download packages/provider-core/check package/build tests/unit/provider-core tests/contract/provider-core tests/integration/provider-core tests/integration/provider-core/delivery tests/fuzz/provider-core
.PHONY: wheelhouse/image/build wheelhouse/lock/locate wheelhouse/lock/acquire tests/wheelhouse tests/delivery/publisher
.PHONY: checks/python-cache/clean

help:
	@printf '%s\n' \
	  'NMR provider tools — make <target> [NAME=value ...] from the repository root' \
	  'Make scripts validate inputs; no release destination, Docker engine or deployment is guessed.' \
	  '' \
	  'Inputs (pass on the Make command line; DOCKER_CONTEXT and WHEELHOUSE can prompt at a terminal):' \
	  '  TAG=vX.Y.Z                 Must match the committed provider-core version and release note.' \
	  '  DOCKER_CONTEXT=<context>   Local Unix-socket Docker engine; no default.' \
	  '  WHEELHOUSE=<cache-dir>     Shared dependency cache, separate from release OUTPUT.' \
	  '  OUTPUT=<absolute-dir>      An existing, empty, canonical directory owned by you, mode 0700.' \
	  '  ARTIFACTS=<dir>            For publish/release: reviewed OUTPUT root (not OUTPUT/artifacts/).' \
	  '  NETWORK_INTERFACE=<system|name>   Route for acquisition; system means host default routing.' \
	  '  DEPLOYMENT=<name>          Configured local deployment; PROJECT=<name> is an installed Compose project.' \
	  '  SHARED_RUNTIME_IMAGE=sha256:<digest>   Explicit immutable previously built image.' \
	  '  LOCK=<file>                Input lock for wheelhouse operations.' \
	  '  FUZZ_PROFILE=<default|extended>   Select bounded or extended SDK fuzz campaign.' \
	  '  ENGINE_CONFIG=<file>       Absolute daemon configuration for cache maintenance.' \
	  '' \
	  'Example release preparation (replace v0.6.8 and cache path for another version):' \
	  '  mkdir -m 700 -- release-artifacts/v0.6.8' \
	  '  make publish/prepare TAG=v0.6.8 DOCKER_CONTEXT=<context> WHEELHOUSE=/real/cache OUTPUT="$$PWD/release-artifacts/v0.6.8"' \
	  '  Inspect and review OUTPUT/artifacts/, OUTPUT/release-build.log, OUTPUT/release-test.log, and OUTPUT/release-notes.md.' \
	  '  make publish/release TAG=v0.6.8 ARTIFACTS="$$PWD/release-artifacts/v0.6.8"' \
	  '  publish/prepare creates artifacts and tests them; publish/release checks exact bytes before publishing.' \
	  '  On failure, preparation prints causes and writes OUTPUT/release-failures.txt beside the full log.' \
	  '  Never reuse a nonempty OUTPUT directory; existing release evidence is preserved.' \
	  ''
	@printf '%s\n' \
	  '== Release and versioned package artifacts ==' \
	  'make package/build DOCKER_CONTEXT=<context> WHEELHOUSE=<cache-dir> OUTPUT=<empty-private-dir>  Build committed source into a fresh destination.' \
	  'make tests/package DOCKER_CONTEXT=<context> WHEELHOUSE=<cache-dir> ARTIFACTS=<artifact-dir>  Qualify the eight files under OUTPUT/artifacts.' \
	  'make publish/prepare TAG=<version> DOCKER_CONTEXT=<context> WHEELHOUSE=<cache-dir> OUTPUT=<empty-private-dir>  Build and qualify; never publish.' \
	  'make publish/release TAG=<version> ARTIFACTS=<prepared-output-root>  Publish the reviewed bundle; NOT its artifacts/ child.' \
	  ''
	@printf '%s\n' \
	  '== Provider-core package and installed tests ==' \
	  'make packages/provider-core/download WHEELHOUSE=<cache-dir> NETWORK_INTERFACE=<system|interface> Acquire checker/bootstrap tools, then locked SDK wheels.' \
	  'make packages/provider-core/check WHEELHOUSE=<cache-dir> Verify locked wheels offline.' \
	  'make tests/unit/provider-core WHEELHOUSE=<cache-dir> Build and test the isolated installed core.' \
	  'make tests/fuzz/provider-core WHEELHOUSE=<cache-dir> [FUZZ_PROFILE=extended] Run shrinking lifecycle sequences.' \
	  'make tests/contract/provider-core WHEELHOUSE=<cache-dir> Qualify installed package contracts.' \
	  'make tests/integration/provider-core WHEELHOUSE=<cache-dir> Run contained package integrations.' \
	  'make tests/integration/provider-core/delivery WHEELHOUSE=<cache-dir> Qualify real delivery IPC and recovery.' \
	  ''
	@printf '%s\n' \
	  '== Cache acquisition and reproducible checker ==' \
	  'make tests/acquisition/network DOCKER_CONTEXT=<name> Qualify the isolated relay on the selected engine.' \
	  'make tests/acquisition/shared-network ARTIFACTS=<artifact-dir> DOCKER_CONTEXT=<name> Qualify the released shared owner.' \
	  'make packages/acquisition-bootstrap/check Verify the checked-in qualified acquisition owner.' \
	  'make packages/acquisition-bootstrap/refresh ARTIFACTS=<artifact-dir> Refresh owner source from reviewed release artifacts.' \
	  'make tests/acquisition/identity WHEELHOUSE=<cache-dir> Test cold identity mapping and retirement receipts offline.' \
	  'make tests/acquisition/owner Test common session cancellation, streams and retirement offline.' \
	  'make wheelhouse/lock/locate LOCK=<file> NETWORK_INTERFACE=<system|interface> [WHEELHOUSE=<cache-dir>] [INDEX_URL=<url>] [SOURCE_URLS=<urls>] [REQUIRE_SOURCE=<source>] Project a pinned lock.' \
	  'make wheelhouse/lock/acquire LOCK=<file> [SOURCE_LOCK=<file>] WHEELHOUSE=<cache-dir> NETWORK_INTERFACE=<system|interface> Acquire locked artifacts.' \
	  'make checks/base-image/download Bootstrap the pinned base using Docker daemon routing.' \
	  'make checks/bootstrap/build Build the checker bootstrap offline from the pinned base.' \
	  'make checks/dependencies/download WHEELHOUSE=<cache-dir> NETWORK_INTERFACE=<system|interface> Acquire declared artifacts.' \
	  'make checks/dependencies/check WHEELHOUSE=<cache-dir> Verify cached artifacts offline.' \
	  'make checks/image/build WHEELHOUSE=<cache-dir> Build the checker offline.' \
	  'make checks/prepare WHEELHOUSE=<cache-dir> NETWORK_INTERFACE=<system|interface> Acquire artifacts and build.' \
	  'make wheelhouse/image/build WHEELHOUSE=<cache-dir> Build offline after packages/provider-core/download.' \
	  'make tests/wheelhouse WHEELHOUSE=<cache-dir> Run shared wheelhouse tests offline.' \
	  ''
	@printf '%s\n' \
	  '== Shared delivery and deployed service ==' \
	  'make tests/delivery/publisher WHEELHOUSE=<cache-dir> Check the shared delivery publisher offline.' \
	  'make delivery/prepare WHEELHOUSE=<cache-dir> NETWORK_INTERFACE=system Acquire the pinned release and build shared delivery.' \
	  'make delivery/image/build WHEELHOUSE=<cache-dir> Build the pinned delivery image from a complete wheelhouse.' \
	  'make tests/deployment/route SHARED_RUNTIME_IMAGE=sha256:<digest> Qualify private API routes with disposable local services.' \
	  'make tests/deployment/route/literal SHARED_RUNTIME_IMAGE=sha256:<digest> Qualify mapped IP authority through private API routes.' \
	  'make tests/deployment SHARED_RUNTIME_IMAGE=sha256:<digest> Qualify installed deployment rendering and proxy boundaries.' \
	  'make delivery/deployment/up DEPLOYMENT=<name> Start or replace the outbox; retain its queue and the running proxy.' \
	  'make delivery/deployment/update DEPLOYMENT=<name> WHEELHOUSE=<cache-dir> Prepare and apply the outbox release; retain its queue and running proxy.' \
	  'make delivery/deployment/proxy/restart DEPLOYMENT=<name> Replace the proxy; active connections can be interrupted and its socket volume is retained.' \
	  'make delivery/deployment/down PROJECT=<compose-project> Stop shared delivery and preserve all volumes.' \
	  'make delivery/deployment/status PROJECT=<compose-project> Inspect installed service state.' \
	  'make delivery/deployment/logs PROJECT=<compose-project> Show bounded recent service logs.' \
	  'make delivery/deployment/held DEPLOYMENT=<name> API_ORIGIN=<url> PROVIDER_REF=<ref> List pending and held metadata while stopped.' \
	  'make delivery/deployment/discard DEPLOYMENT=<name> API_ORIGIN=<url> PROVIDER_REF=<ref> EXECUTION_ATTEMPT_REF=<ref> BODY_SHA256=<digest> Discard exactly one stopped held item.' \
	  'make delivery/deployment/retire-old PROJECT=<compose-project> SHARED_RUNTIME_IMAGE=sha256:<digest> Retire unused old per-provider socket volumes.' \
	  ''
	@printf '%s\n' \
	  '== Repository checks and host maintenance ==' \
	  'make help                        Show maintained commands.' \
	  'make tests                       Run all current validation lanes.' \
	  'make packages/images/retire DOCKER_CONTEXT=<context> Retire exact obsolete project images, preserving current selections and borrowers.' \
	  'make engine/cache/gc/install DOCKER_CONTEXT=<context> ENGINE_CONFIG=/absolute/daemon.json Install per-engine cache GC without restarting Docker.' \
	  'make engine/cache/gc DOCKER_CONTEXT=<context> ENGINE_CONFIG=/absolute/daemon.json Apply the native cache thresholds now.' \
	  'make tests/repository            Run repository conventions and detector fixtures offline.' \
	  'make tests/repository/quality    Run code and operator convention checks offline.' \
  'make tests/repository/installer  Verify the pinned installer and checker bootstrap offline.' \
	  'make tests/repository/bytecode   Check maintained Python launches suppress source bytecode.' \
	  'make checks/python-cache/clean    Explicitly remove generated first-party .pyc/.pyo files and empty __pycache__ directories.' \
	  'make tests/repository/provisioning Run checker preparation and cleanup fixtures offline.' \
	  'make tests/repository/release    Run release and wheelhouse bundle fixtures offline.' \
	  'make tests/repository/network    Run acquisition network-owner fixtures offline.' \
	  'make tests/repository/network/identity Run the focused acquisition identity fixture.' \
	  'make tests/repository/runtime    Inspect the checker container grants.' \
	  'make tests/repository/context DOCKER_CONTEXT=<context> WHEELHOUSE=<cache-dir> Qualify disposable client contexts and a real read-only cache worker.' \
	  'make tests/repository/provisioning/live DOCKER_CONTEXT=<context> NETWORK_INTERFACE=<system|interface> Qualify cold checker acquisition and offline cache admission.' \
	  'make tests/repository/command    Print the repository checker command without starting it.' \
	  ''

packages/provider-core/download: checks/prepare
	bash host/tools/provider_core.sh download

wheelhouse/image/build:
	bash host/tools/wheelhouse_image.sh build

tests/wheelhouse:
	bash host/tools/wheelhouse_image.sh test

tests/delivery/publisher:
	bash host/tools/delivery_publisher_check.sh

wheelhouse/lock/locate:
	@bash host/tools/wheelhouse_locate.sh

wheelhouse/lock/acquire:
	@bash host/tools/wheelhouse_acquire.sh

packages/provider-core/check:
	bash host/tools/provider_core.sh check

tests/deployment/route:
	bash host/tools/delivery_route_tests.sh

tests/deployment/route/literal:
	bash host/tools/delivery_route_tests.sh --literal-ip

tests/deployment:
	bash host/tools/delivery_tests.sh

delivery/prepare: checks/prepare
	bash host/tools/delivery_prepare.sh

delivery/image/build:
	bash host/tools/delivery_image.sh

package/build:
	bash host/tools/provider_core.sh build

tests/package:
	bash host/tools/provider_core.sh package

tests/unit/provider-core:
	bash host/tools/provider_core.sh unit

tests/fuzz/provider-core:
	bash host/tools/provider_core.sh fuzz

tests/contract/provider-core:
	bash host/tools/provider_core.sh contract

tests/integration/provider-core:
	bash host/tools/provider_core.sh integration

tests/integration/provider-core/delivery:
	bash host/tools/provider_core.sh integration/delivery

checks/base-image/download:
	bash host/tools/checker.sh base-download

.PHONY: checks/bootstrap/build
checks/bootstrap/build:
	bash host/tools/checker.sh bootstrap-build

checks/dependencies/download: checks/bootstrap/build
	bash host/tools/checker.sh download

checks/dependencies/check:
	bash host/tools/checker.sh check

checks/image/build: checks/bootstrap/build
	bash host/tools/checker.sh build

checks/prepare:
	bash host/tools/checker.sh prepare

checks/python-cache/clean:
	bash host/tools/python_cache_clean.sh

tests/acquisition/identity:
	@bash host/tools/acquisition_identity_test.sh

.PHONY: tests/acquisition/owner
tests/acquisition/owner:
	@bash host/tools/repository.sh tests/acquisition/owner

tests: tests/repository tests/unit/provider-core tests/contract/provider-core tests/integration/provider-core tests/deployment

.PHONY: tests/acquisition/shared-network
tests/acquisition/shared-network:
	bash host/tools/acquisition_owner_network_test.sh

tests/acquisition/network:
	bash host/tools/acquisition_network_test.sh

tests/repository:
	bash host/tools/repository.sh tests/repository

tests/repository/quality:
	bash host/tools/repository.sh tests/repository/quality

.PHONY: tests/repository/bytecode
tests/repository/bytecode:
	@bash host/tools/repository.sh tests/repository/bytecode

.PHONY: packages/acquisition-bootstrap/check packages/acquisition-bootstrap/refresh
packages/acquisition-bootstrap/check:
	@bash host/tools/acquisition_bootstrap.sh check

packages/acquisition-bootstrap/refresh:
	@bash host/tools/acquisition_bootstrap.sh refresh

tests/repository/provisioning:
	bash host/tools/repository.sh tests/repository/provisioning

tests/repository/release:
	bash host/tools/repository.sh tests/repository/release

.PHONY: tests/repository/installer
tests/repository/installer:
	bash host/tools/repository.sh tests/repository/installer

tests/repository/network:
	bash host/tools/repository.sh tests/repository/network

tests/repository/network/identity:
	bash host/tools/repository.sh tests/repository/network/identity

tests/repository/runtime:
	bash host/tools/repository.sh tests/repository/runtime

.PHONY: tests/repository/context
tests/repository/context:
	bash host/tools/context_test.sh

.PHONY: tests/repository/provisioning/live
tests/repository/provisioning/live:
	bash host/tools/checker_provisioning_test.sh

tests/repository/command:
	bash host/tools/repository.sh --print tests/repository

delivery/deployment/up:
	bash host/tools/delivery.sh up

delivery/deployment/update:
	bash host/tools/delivery_prepare.sh
	bash host/tools/delivery.sh up

delivery/deployment/proxy/restart:
	bash host/tools/delivery.sh proxy-restart

delivery/deployment/down:
	bash host/tools/delivery.sh down

delivery/deployment/status:
	bash host/tools/delivery.sh status

delivery/deployment/logs:
	bash host/tools/delivery.sh logs

delivery/deployment/held:
	bash host/tools/delivery.sh held

delivery/deployment/discard:
	bash host/tools/delivery.sh discard

delivery/deployment/retire-old:
	[[ $${PROJECT_INPUT_ORIGIN} == 'command line' && $${SHARED_RUNTIME_IMAGE_INPUT_ORIGIN} == 'command line' ]] || { printf '%s\n' 'Select PROJECT and SHARED_RUNTIME_IMAGE on the Make command line.' >&2; exit 2; }
	bash host/tools/delivery_retire_old.sh "$$PROJECT_INPUT" "$$SHARED_RUNTIME_IMAGE_INPUT"

publish/prepare:
	bash host/tools/publish.sh prepare

publish/release:
	bash host/tools/publish.sh release

engine/cache/gc/install:
	bash host/tools/engine_cache_gc.sh install

engine/cache/gc:
	[[ $$DOCKER_CONTEXT_INPUT_ORIGIN == 'command line' && $$ENGINE_CONFIG_INPUT_ORIGIN == 'command line' && -v ENGINE_CONFIG_INPUT && -n $$ENGINE_CONFIG_INPUT ]] || { printf '%s\n' 'Supply DOCKER_CONTEXT=<context> and ENGINE_CONFIG=/absolute/daemon.json on the Make command line.' >&2; exit 2; }
	bash host/tools/engine_cache_gc.sh run "$$DOCKER_CONTEXT_INPUT" "$$NMR_SELECTED_DOCKER_ENDPOINT" "$$ENGINE_CONFIG_INPUT"

packages/images/retire:
	bash host/tools/image_retirement.sh

.PHONY: tests/acquisition/compose
tests/acquisition/compose:
	@bash host/tools/repository.sh tests/acquisition/compose
