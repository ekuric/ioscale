$hammerdbPath = $env:HAMMERDB_PATH
if (-not $hammerdbPath) { $hammerdbPath = "c:\tools\hammerdb-4.12" }
$createDbSql = $env:CREATE_DB_SQL
if (-not $createDbSql) { $createDbSql = "create_db.sql" }
$mssqlPass = $env:MSSQL_PASS
$sqlcmdTimeout = $env:SQLCMD_TIMEOUT
if (-not $sqlcmdTimeout) { $sqlcmdTimeout = 600 }
$waitSeconds = $env:SQL_STARTUP_WAIT
if (-not $waitSeconds) { $waitSeconds = 120 }
$retryInterval = 5
$deleteSchema = $env:DELETE_SCHEMA_TCL
if (-not $deleteSchema) { $deleteSchema = Join-Path $hammerdbPath "scripts\\tcl\\mssqls\\tprocc\\mssqls_tprocc_deleteschema.tcl" }
$buildSchema = $env:BUILD_SCHEMA_TCL
if (-not $buildSchema) { $buildSchema = Join-Path $hammerdbPath "scripts\\tcl\\mssqls\\tprocc\\mssqls_tprocc_buildschema.tcl" }

Write-Host "Using delete schema TCL: $deleteSchema"
Write-Host "Using build schema TCL: $buildSchema"
Write-Host "Using create_db.sql: $createDbSql"
if (-not (Test-Path $deleteSchema)) { Write-Error "Delete schema TCL not found: $deleteSchema"; exit 1 }
if (-not (Test-Path $buildSchema)) { Write-Error "Build schema TCL not found: $buildSchema"; exit 1 }
if (-not (Test-Path $createDbSql)) { Write-Error "create_db.sql not found: $createDbSql"; exit 1 }

Write-Host "Waiting for SQL Server to accept connections (timeout ${waitSeconds}s)..."
$elapsed = 0
while ($elapsed -lt $waitSeconds) {
    $svc = Get-Service -Name MSSQLSERVER -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") {
        sqlcmd -S localhost -E -Q "SELECT 1" -b -l 5 -t 5 *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "SQL Server is ready."
            break
        }
    }
    Start-Sleep -Seconds $retryInterval
    $elapsed += $retryInterval
}
if ($elapsed -ge $waitSeconds) {
    Write-Error "SQL Server did not become ready within ${waitSeconds}s."
    exit 1
}

cd $hammerdbPath

Write-Host "Running delete schema TCL..."
.\hammerdbcli auto $deleteSchema
Start-Process powershell.exe -ArgumentList "-NoExit -Command & {$hammerdbPath\hammerdbws}"
Write-Host "Running create_db.sql..."
if ($mssqlPass) {
    sqlcmd -U sa -P $mssqlPass -i $createDbSql -b -l 30 -t $sqlcmdTimeout
} else {
    sqlcmd -U sa -i $createDbSql -b -l 30 -t $sqlcmdTimeout
}
Write-Host "Running build schema TCL..."
.\hammerdbcli auto $buildSchema
