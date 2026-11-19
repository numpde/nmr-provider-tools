#!/usr/bin/env bash
# Identity of the committed recipe, independent of any caller's wheelhouse path.
source "$root/operations/docker/image_fingerprint.sh"
delivery_recipe_inputs=(operations/docker/base_image.sh tools/container_guard.py tools/artifacts.py tools/wheelhouse.py
    tools/adapters/__init__.py tools/adapters/github_release.py tools/adapters/pip_install.py tools/adapters/pypi_project_json.py
    tools/adapters/pylock.py
    tools/validation/__init__.py tools/validation/upstream/__init__.py tools/validation/upstream/pypi_project_json.py
    tools/validation/upstream/pip_installation_report.py tools/validation/upstream/pip_requirements.py
    packages/provider-core/sdk_build/core_environment.py tools/installer.py requirements/installer-release.json
    packages/provider-core/src/provider_core/tools.py packages/provider-core/src/provider_core/startup_file.py
    packages/provider-core/src/provider_core/validation/upstream/docker_ids.py operations/delivery/image/delivery_support/__init__.py
    packages/type-requirements/src/type_requirements.py
    operations/delivery/build_inputs.py operations/delivery/image/delivery_deployment.py packages/provider-core/sdk_build/provider_core_acquire.py host/tools/build_stream_inputs.py
    requirements/delivery-runtime.lock requirements/delivery-release.json requirements/wheelhouse-tool-release.json
    operations/delivery/image/Dockerfile operations/delivery/image/check_runtime.py)
delivery_recipe_identity() {
    image_fingerprint checker "${delivery_recipe_inputs[@]}"
}
