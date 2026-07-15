FROM quay.io/ekuric/benchmark-base:latest

ARG IOSCALE_REPO=https://github.com/ekuric/ioscale.git
ARG IOSCALE_REF=main

# Bake HammerDB MSSQL wrapper scripts into the image (Option C).
RUN dnf install -y git \
 && git clone --depth 1 --branch "${IOSCALE_REF}" "${IOSCALE_REPO}" /work/hammerdb-bundled \
 && if [ ! -d /work/hammerdb-bundled/templates/mssql ] && [ -d /work/hammerdb-bundled/db/templates/mssql ]; then \
      mkdir -p /work/hammerdb-bundled/templates \
      && ln -sf ../db/templates/mssql /work/hammerdb-bundled/templates/mssql; \
    fi \
 && test -f /work/hammerdb-bundled/templates/mssql/Hammerdb-mssql-install-script \
 && dnf remove -y git \
 && dnf clean all

COPY mssqldb.py .
COPY mssql-config.yaml examples/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
