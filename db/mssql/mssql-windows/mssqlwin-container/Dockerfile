FROM quay.io/ekuric/benchmark-base:latest

COPY mssqlwin.py .
COPY templates/ templates/
COPY mssql-configwin.yaml examples/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
