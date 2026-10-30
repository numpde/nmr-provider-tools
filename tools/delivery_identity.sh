#!/usr/bin/env bash
# Identity of the committed recipe, independent of any caller's wheelhouse path.
delivery_recipe_identity() {
    source "$root/tools/checker_inputs.sh"
    local -a checker_input_paths=(tools/base_image.sh tools/container_guard.py tools/artifacts.py tools/wheelhouse.py
        tools/core_environment.py tools/delivery_image.py tools/delivery_deployment.py
        requirements/provider-core.lock requirements/delivery-release.json
        containers/delivery/Dockerfile containers/delivery/check_runtime.py)
    checker_fingerprint
}
