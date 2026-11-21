ARG BASE_IMAGE
FROM scratch AS wheel_inputs
# selected-wheel-inputs
FROM ${BASE_IMAGE}
ENV PROVIDER_CORE_IN_CONTAINER=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/build
COPY tools/container_guard.py tools/artifacts.py tools/artifact_metadata.py tools/wheelhouse.py tools/installer.py ./tools/
COPY tools/adapters/__init__.py tools/adapters/pip_install.py ./tools/adapters/
COPY tools/validation/__init__.py ./tools/validation/
COPY tools/validation/upstream/__init__.py tools/validation/upstream/pip_installation_report.py \
     tools/validation/upstream/pip_requirements.py ./tools/validation/upstream/
COPY packages/type-requirements/src/type_requirements.py ./
COPY packages/provider-core/sdk_build/core_environment.py ./sdk_build/
COPY requirements/installer-release.json /opt/provider-tools/installer/release.json
COPY --from=wheel_inputs /installer/ /opt/provider-tools/installer/wheels/
COPY provider-core-install.lock provider-core-wheel-inputs.json ./
RUN --network=none --mount=type=bind,from=wheel_inputs,target=/wheels,readonly \
    NMR_PROVIDER_TOOLS_IN_CONTAINER=1 python3 -B -m tools.installer --record /opt/provider-tools/installer/release.json --pool /wheels/installer
RUN --mount=type=bind,from=wheel_inputs,target=/wheels,readonly \
    NMR_PROVIDER_TOOLS_IN_CONTAINER=1 python3 -B -m sdk_build.core_environment install --root /opt/provider-core --lock /opt/build/provider-core-install.lock --pool /wheels --expected-inputs /opt/build/provider-core-wheel-inputs.json
COPY operations/delivery/image/delivery_deployment.py /opt/provider-tools/delivery_deployment.py
COPY operations/delivery/image/delivery_support/__init__.py \
     packages/type-requirements/src/type_requirements.py \
     packages/provider-core/src/provider_core/tools.py packages/provider-core/src/provider_core/startup_file.py \
     packages/provider-core/src/provider_core/validation/upstream/docker_ids.py /opt/provider-tools/delivery_support/
COPY operations/delivery/image/check_runtime.py /opt/build/check_runtime.py
RUN /opt/provider-core/environment/bin/python -I -B /opt/build/check_runtime.py \
    && /opt/provider-core/environment/bin/python -I -B -m provider_core.delivery_main --help \
    && /opt/provider-core/environment/bin/python -I -B -m provider_core.provider_proxy --help \
    && /opt/provider-core/environment/bin/python -I -B /opt/provider-tools/delivery_deployment.py --help \
    && rm -rf /opt/build
WORKDIR /opt/provider-core
USER 65532:65532
ENTRYPOINT ["/opt/provider-core/environment/bin/python", "-I", "-B", "-m", "provider_core.delivery_main"]
