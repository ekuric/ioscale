FROM quay.io/ekuric/benchmark-base:latest

ARG IOSCALE_REPO=https://github.com/ekuric/ioscale.git
ARG IOSCALE_REF=main

RUN dnf install -y git \
 && git clone --depth 1 --branch "${IOSCALE_REF}" "${IOSCALE_REPO}" /work/hammerdb-bundled \
 && test -f /work/hammerdb-bundled/templates/mariadb/Hammerdb-mariadb-install-script \
 && dnf remove -y git \
 && dnf clean all

COPY mariadb.py .
COPY mariadb-config.yaml examples/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
