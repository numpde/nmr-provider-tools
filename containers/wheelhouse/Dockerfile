ARG BASE_IMAGE
FROM scratch AS dependencies
COPY ["wheelhouse/packaging-26.3-py3-none-any.whl", "/packaging-26.3-py3-none-any.whl"]
COPY ["wheelhouse/tomli_w-1.2.0-py3-none-any.whl", "/tomli_w-1.2.0-py3-none-any.whl"]

FROM ${BASE_IMAGE}
USER root
RUN --network=none --mount=type=bind,from=dependencies,target=/dependencies,readonly \
    echo 'd7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c  /dependencies/packaging-26.3-py3-none-any.whl' | sha256sum --check \
    && echo '188306098d013b691fcadc011abd66727d3c414c571bb01b1a174ba8c983cf90  /dependencies/tomli_w-1.2.0-py3-none-any.whl' | sha256sum --check \
    && python -m pip install --no-index --no-deps /dependencies/packaging-26.3-py3-none-any.whl /dependencies/tomli_w-1.2.0-py3-none-any.whl
WORKDIR /opt
COPY wheelhouse_tool /opt/wheelhouse_tool
ENV PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
ENTRYPOINT ["python", "-m", "wheelhouse_tool"]
