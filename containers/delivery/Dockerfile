ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ENV PROVIDER_CORE_IN_CONTAINER=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/build
COPY tools/container_guard.py tools/artifacts.py tools/wheelhouse.py tools/core_environment.py ./tools/
COPY provider-core-install.lock provider-core-wheel-inputs.json ./
RUN --mount=type=bind,from=wheelhouse,target=/wheels,readonly \
    NMR_PROVIDER_TOOLS_IN_CONTAINER=1 python3 -B -m tools.core_environment install --root /opt/provider-core --lock /opt/build/provider-core-install.lock --pool /wheels --expected-inputs /opt/build/provider-core-wheel-inputs.json
COPY tools/delivery_deployment.py /opt/provider-tools/delivery_deployment.py
COPY containers/delivery/check_runtime.py /opt/build/check_runtime.py
RUN /opt/provider-core/environment/bin/python -I -B /opt/build/check_runtime.py \
    && /opt/provider-core/environment/bin/python -I -B -m provider_core.delivery_main --help \
    && /opt/provider-core/environment/bin/python -I -B -m provider_core.provider_proxy --help \
    && /opt/provider-core/environment/bin/python -I -B /opt/provider-tools/delivery_deployment.py --help \
    && rm -rf /opt/build
WORKDIR /opt/provider-core
USER 65532:65532
ENTRYPOINT ["/opt/provider-core/environment/bin/python", "-I", "-B", "-m", "provider_core.delivery_main"]
