ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY tools /build/tools
COPY requirements /build/requirements
COPY dependencies /build/dependencies
RUN cd /build && NMR_PROVIDER_TOOLS_IN_CONTAINER=1 python3 -B -m tools.checker_dependencies install --declaration requirements/checker-dependencies.json --directory dependencies && rm -rf /build
USER 65532:65532
WORKDIR /workspace
