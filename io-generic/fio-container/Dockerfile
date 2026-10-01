FROM quay.io/ekuric/benchmark-base:latest

COPY fio_tests/ /work/fio_tests/
COPY fio-tests.py /work/fio-tests.py
COPY fio-config.yaml examples/
COPY example-*.yaml examples/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
