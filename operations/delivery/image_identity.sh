#!/usr/bin/env bash
# Identity of the committed recipe, independent of any caller's wheelhouse path.
delivery_recipe_identity() {
    source "$root/host/tools/checker_inputs.sh"
    local -a checker_input_paths=(operations/docker/base_image.sh tools/container_guard.py tools/artifacts.py tools/wheelhouse.py
        packages/provider-core/sdk_build/core_environment.py tools/installer.py requirements/installer-release.json
        operations/delivery/build_inputs.py operations/delivery/image/delivery_deployment.py packages/provider-core/sdk_build/provider_core_acquire.py host/tools/build_stream_inputs.py
        requirements/provider-core.lock requirements/delivery-release.json requirements/wheelhouse-tool-release.json
        operations/delivery/image/Dockerfile operations/delivery/image/check_runtime.py)
    checker_fingerprint checker
}
