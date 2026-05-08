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

Write-Host "Force-dropping tpcc database if it exists (handles missing files after disk reformat)..."
$sqlArgs = if ($mssqlPass) { @("-U", "sa", "-P", $mssqlPass, "-l", "30", "-t", "60") } else { @("-E", "-l", "30", "-t", "60") }

$checkSql = "IF DB_ID('tpcc') IS NOT NULL PRINT 'EXISTS' ELSE PRINT 'NOTEXISTS'"
$checkResult = sqlcmd @sqlArgs -Q $checkSql -h -1 2>&1
if ($checkResult -match 'NOTEXISTS') {
    Write-Host "Database tpcc does not exist - nothing to drop"
} else {
    Write-Host "Database tpcc found in catalog, force-removing..."
    sqlcmd @sqlArgs -Q "BEGIN TRY ALTER DATABASE tpcc SET EMERGENCY END TRY BEGIN CATCH PRINT ERROR_MESSAGE() END CATCH" 2>&1 | ForEach-Object { Write-Host "  emergency: $_" }
    sqlcmd @sqlArgs -Q "BEGIN TRY DROP DATABASE tpcc PRINT 'Dropped tpcc' END TRY BEGIN CATCH PRINT 'DROP failed: ' + ERROR_MESSAGE() END CATCH" 2>&1 | ForEach-Object { Write-Host "  drop: $_" }

    $checkResult2 = sqlcmd @sqlArgs -Q $checkSql -h -1 2>&1
    if ($checkResult2 -match 'EXISTS') {
        Write-Host "  DROP did not work, trying sp_detach_db..."
        sqlcmd @sqlArgs -Q "EXEC sp_detach_db 'tpcc', 'true'" 2>&1 | ForEach-Object { Write-Host "  detach: $_" }

        $checkResult3 = sqlcmd @sqlArgs -Q $checkSql -h -1 2>&1
        if ($checkResult3 -match 'EXISTS') {
            Write-Host "  detach did not work, trying OFFLINE + DROP..."
            sqlcmd @sqlArgs -Q "ALTER DATABASE tpcc SET OFFLINE WITH ROLLBACK IMMEDIATE" 2>&1 | Out-Null
            Start-Sleep -Seconds 2
            sqlcmd @sqlArgs -Q "DROP DATABASE tpcc" 2>&1 | ForEach-Object { Write-Host "  offline-drop: $_" }
        }
    }

    $finalCheck = sqlcmd @sqlArgs -Q $checkSql -h -1 2>&1
    if ($finalCheck -match 'NOTEXISTS') {
        Write-Host "Database tpcc removed successfully"
    } else {
        Write-Host "WARNING: Could not fully remove tpcc - CREATE DATABASE may fail"
    }
}
Write-Host "Force drop/detach step complete"

Write-Host "Running delete schema TCL (cleanup any remaining objects)..."
.\hammerdbcli auto $deleteSchema

Start-Process powershell.exe -ArgumentList "-NoExit -Command & {$hammerdbPath\hammerdbws}"
Write-Host "Running create_db.sql..."
if ($mssqlPass) {
    sqlcmd -U sa -P $mssqlPass -i $createDbSql -b -l 30 -t $sqlcmdTimeout
} else {
    sqlcmd -U sa -i $createDbSql -b -l 30 -t $sqlcmdTimeout
}
if ($LASTEXITCODE -ne 0) {
    Write-Error "create_db.sql failed with exit code $LASTEXITCODE"
    exit 1
}
Write-Host "create_db.sql completed successfully"
Write-Host "Running build schema TCL..."
.\hammerdbcli auto $buildSchema
