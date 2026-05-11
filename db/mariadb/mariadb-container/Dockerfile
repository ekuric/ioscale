FROM quay.io/ekuric/benchmark-base:latest

COPY mariadb.py .
COPY mariadb-config.yaml examples/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
