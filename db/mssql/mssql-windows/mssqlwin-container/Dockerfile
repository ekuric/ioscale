FROM quay.io/ekuric/benchmark-base:latest

COPY mssqlwin.py .
COPY mssqlwin_tests/ mssqlwin_tests/
COPY templates/ templates/
COPY mssql-configwin.yaml examples/
COPY perfmon-counters-default.txt .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
