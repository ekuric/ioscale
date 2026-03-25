$hammerdbPath = $env:HAMMERDB_PATH
if (-not $hammerdbPath) { $hammerdbPath = "c:\tools\hammerdb-4.12" }
$createDbSql = $env:CREATE_DB_SQL
if (-not $createDbSql) { $createDbSql = "create_db.sql" }
$mssqlPass = $env:MSSQL_PASS

cd $hammerdbPath

.\hammerdbcli auto .\scripts\tcl\mssqls\tprocc\mssqls_tprocc_deleteschema.tcl
Start-Process powershell.exe -ArgumentList "-NoExit -Command & {$hammerdbPath\hammerdbws}"
if ($mssqlPass) {
    sqlcmd -U sa -P $mssqlPass -i $createDbSql
} else {
    sqlcmd -U sa -i $createDbSql
}
.\hammerdbcli auto .\scripts\tcl\mssqls\tprocc\mssqls_tprocc_buildschema.tcl
