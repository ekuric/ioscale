FROM quay.io/ekuric/benchmark-base:latest

COPY fio-tests.py .
COPY fio-config.yaml examples/
COPY example-*.yaml examples/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
