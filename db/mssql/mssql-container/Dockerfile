FROM quay.io/ekuric/benchmark-base:latest

COPY mssqldb.py .
COPY mssql-config.yaml examples/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["/work/entrypoint.sh"]
