FROM quay.io/ekuric/benchmark-base:latest

COPY postgresql.py .
COPY postgresql-config.yaml examples/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
