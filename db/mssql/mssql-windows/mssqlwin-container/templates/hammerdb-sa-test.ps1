$results = $env:HAMMERDB_RESULT_DIR
if (-not $results) { $results = "results" }
New-Item -Path $results -ItemType Directory -Force | Out-Null

# Sweep iteration from mssqlwin ($env:HAMMERDB_RUN); default 1.
$run = $env:HAMMERDB_RUN
if (-not $run) { $run = "1" }

.\hammerdbcli auto .\mssqls_tprocc_run_$user_count.tcl > "$results\mssqls_tprocc_010vu_run$run.out"
.\hammerdbcli auto .\scripts\tcl\mssqls\tprocc\mssqls_tprocc_result.tcl > "$results\mssqls_tprocc_010vu_run$run.json"
