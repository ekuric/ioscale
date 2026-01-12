# Create database files
/opt/mssql-tools/bin/sqlcmd -U sa -P mssqlpasswd <<! 
drop database tpcc
go
CREATE DATABASE tpcc
ON PRIMARY
(   NAME        = MSSQL_data_1,
    FILENAME    = '/perf1/mssql_data/data/MSSQL_tpcc_Data_1.mdf',
    SIZE        = 64GB,
    FILEGROWTH  = 32GB)
LOG ON
(   NAME        = MSSQL_tpcc_Log,
    FILENAME    = '/perf1/mssql_data/data/tpcc_Log.ldf',
    SIZE        = 20480MB,
    FILEGROWTH  = 500MB,
    MAXSIZE     = 270000MB)
go

ALTER DATABASE tpcc ADD FILE
(    NAME = MSSQL_data_2, FILENAME = '/perf1/mssql_data/data/MSSQL_tpcc_Data_2.mdf', SIZE = 32768)
GO

exit
!

