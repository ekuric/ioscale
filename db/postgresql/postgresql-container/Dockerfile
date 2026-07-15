FROM quay.io/ekuric/benchmark-base:latest

ARG IOSCALE_REPO=https://github.com/ekuric/ioscale.git
ARG IOSCALE_REF=main

RUN dnf install -y git \
 && git clone --depth 1 --branch "${IOSCALE_REF}" "${IOSCALE_REPO}" /work/hammerdb-bundled \
 && test -f /work/hammerdb-bundled/templates/postgresql/Hammerdb-postgres-install-script \
 && dnf remove -y git \
 && dnf clean all

COPY postgresql.py .
COPY postgresql-config.yaml examples/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
