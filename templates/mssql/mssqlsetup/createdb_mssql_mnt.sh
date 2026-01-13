mountpoint=none
logmountpoint=none

while [ $# -gt 0 ]
do
case $1 in
        -m) mountpoint=$2
            shift 2
            ;;
        -lm) logmountpoint=$2
            shift 2
            ;;
esac
done

if [[ $logmountpoint == *"none"* ]]; then
   echo "using data mount point as log mount point"
   logmountpoint=${mountpoint}
fi

export mountpoint logmountpoint
mkdir -p ${mountpoint}/mssql_data/data
mkdir -p ${logmountpoint}/mssql_log
chown -R mssql:mssql ${mountpoint}
chown -R mssql:mssql ${logmountpoint}


# Create database files
/opt/mssql-tools/bin/sqlcmd -U sa -P mssqlpasswd1! <<! 
drop database tpcc
go
CREATE DATABASE tpcc
ON PRIMARY
(   NAME        = MSSQL_data_1,
    FILENAME    = '${mountpoint}/mssql_data/data/MSSQL_tpcc_Data_1.mdf',
    SIZE        = 32768MB,
    FILEGROWTH  = 20)
LOG ON
(   NAME        = MSSQL_tpcc_Log,
    FILENAME    = '${logmountpoint}/mssql_log/tpcc_Log.ldf',
    SIZE        = 20480MB,
    FILEGROWTH  = 500MB,
    MAXSIZE     = 270000MB)
go

ALTER DATABASE tpcc ADD FILE
(    NAME = MSSQL_data_2, FILENAME = '${mountpoint}/mssql_data/data/MSSQL_tpcc_Data_2.mdf', SIZE = 32768)
GO

exit
!

