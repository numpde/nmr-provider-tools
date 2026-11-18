ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS bootstrap
LABEL org.nmr-provider-tools.role=checker-bootstrap
COPY operations/checker/image/dependencies.py /opt/provider-tools/operations/checker/image/
COPY tools/container_guard.py tools/artifacts.py tools/installer.py /opt/provider-tools/tools/
COPY requirements/checker-dependencies.json /opt/provider-tools/requirements/
COPY requirements/installer-release.json /opt/provider-tools/installer/release.json
WORKDIR /opt/provider-tools
USER 65532:65532

FROM bootstrap AS checker
LABEL org.nmr-provider-tools.role=checker
USER root
COPY dependencies /build/dependencies
COPY dependencies/pip-*.whl /opt/provider-tools/installer/wheels/
RUN --network=none NMR_PROVIDER_TOOLS_IN_CONTAINER=1 python3 -B -m operations.checker.image.dependencies install --declaration requirements/checker-dependencies.json --directory /build/dependencies --installer /opt/provider-tools/installer/release.json && rm -rf /build
USER 65532:65532
WORKDIR /workspace
