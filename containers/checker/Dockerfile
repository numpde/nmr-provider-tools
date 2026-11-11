ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY tools /build/tools
COPY requirements /build/requirements
COPY dependencies /build/dependencies
COPY tools/installer.py /opt/provider-installer/driver.py
COPY requirements/installer-release.json /opt/provider-installer/release.json
RUN cd /build && NMR_PROVIDER_TOOLS_IN_CONTAINER=1 python3 -B -m tools.checker_dependencies install --declaration requirements/checker-dependencies.json --directory dependencies --installer /opt/provider-installer && rm -rf /build
USER 65532:65532
WORKDIR /workspace
