ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS bootstrap
COPY --from=ghcr.io/astral-sh/uv:0.10.0@sha256:78a7ff97cd27b7124a5f3c2aefe146170793c56a1e03321dd31a289f6d82a04f /uv /usr/local/bin/uv
LABEL org.nmr-provider-tools.role=checker-bootstrap
COPY operations/checker/image/dependencies.py /opt/provider-tools/operations/checker/image/
COPY tools/container_guard.py tools/artifacts.py tools/installer.py /opt/provider-tools/tools/
COPY tools/adapters/__init__.py tools/adapters/debian.py tools/adapters/http_download.py /opt/provider-tools/tools/adapters/
COPY tools/adapters/pip_install.py /opt/provider-tools/tools/adapters/
COPY tools/validation/__init__.py /opt/provider-tools/tools/validation/
COPY tools/validation/upstream/__init__.py tools/validation/upstream/debian.py /opt/provider-tools/tools/validation/upstream/
COPY packages/type-requirements/src/type_requirements.py /opt/provider-tools/
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
