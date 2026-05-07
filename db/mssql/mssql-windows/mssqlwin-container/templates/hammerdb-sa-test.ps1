$results = $env:HAMMERDB_RESULT_DIR
if (-not $results) { $results = "results" }
New-Item -Path $results -ItemType Directory -Force | Out-Null

.\hammerdbcli auto .\mssqls_tprocc_run_$user_count.tcl > "$results\mssqls_tprocc_010vu_run1.out"
.\hammerdbcli auto .\scripts\tcl\mssqls\tprocc\mssqls_tprocc_result.tcl > "$results\mssqls_tprocc_010vu_run1.json"
