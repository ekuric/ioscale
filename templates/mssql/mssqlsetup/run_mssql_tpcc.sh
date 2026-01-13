#!/usr/bin/bash

## Defining variables
PATH=/opt/mssql-tools/bin:$PATH
storagetype="Storage not specified"
Usercount="10 20 30 40 50"
whc="500"
benchmark_results_dir=`pwd`"/results"
DBRESTARTUP="n"

usage()
{
  echo "Usage:
        run_mssql_tpcc.sh [-h] [-u users] [-t runlength] [-w warehouse count] [-s storage type] 

        Usage:
        -h help
	-u # of users  (Default "10 20 30 40 50" )
	-t runlength in minutes ( Default "15" ) 
	-w warehouse count  ( Default "500" )
	-s storage type  ( Default "Storage not specified" )
        -n Test Name  (Default "HDB_tpcc_mssql" ) 
        
       Examples: 
        nrunoastoltp150.sh -u "10 20 30"
            Do runs with 10, 20 and 30 users
        nrunoastoltp150.sh -s "nvme"
            Do a 10 user, 500 warehouse, 15 minute run with storage type as "nvme"
        nrunoastoltp150.sh -w 1000 -u "10 20 40 80 100" -s "iscsi"
	    Do a 1000 warehouse run with 10 20 40 80 and 100 users and storage type specified as iscsi
  "
}

while [ $# -gt 0 ]
do
case $1 in
        -h) usage;
            exit;
            ;;
        -w) whc=$2
            shift 2
            ;;
        -u) Usercount=$2
            shift 2
            ;;
        -s) storagetype=$2
            shift 2
            ;;
        -t) RUNLENGTH=$2
            shift 2
            ;;
	 *) usage;
	    exit;
	    ;;
esac
done


mkdir -p results

if [ -z "${benchmark_results_dir}" ]
then
benchmark_results_dir=`pwd`"/results"
fi

if [ -z "${benchmark_run_dir}" ]
then
benchmark_run_dir=`pwd`"/results"
fi

echo ${storagetype}
echo ${Usercount}
echo ${benchmark_results_dir}
echo ${whc}

## Set variables for the run

benchmark_name="Hammerdb-tpcc"
echo ${benchmark_name} > ${benchmark_run_dir}/user-benchmark-name.txt
hostnm=`hostname -f`
hostip=`hostname -i`
numcpu=`nproc`
totmem=`cat /proc/meminfo |grep "MemTotal:" | awk '{print $2}'`
krel=`uname -r`      ## Kernel
## Building the mssql.conf file based on recommendations

./mssql_settings.sh


echo "Starting MS SQL Server"
systemctl restart mssql-server

echo "Waiting 60 seconds for DB to start"
sleep 60

Testname="Hammerdb_tpcc_mssql"

## Run the database workload with different user count

## Set warehouse count and Host IP in the runtest.tcl file 
sed -i "s/^diset tpcc mssqls_count_ware.*/diset tpcc mssqls_count_ware ${whc}/" runtest_mssql.tcl
sed -i "s/^diset connection mssqls_linux_server.*/diset connection mssqls_linux_server ${hostip}/" runtest_mssql.tcl

echo "StartTime,EndTime,Hostname,Kernel,Database,DBVer,Cpus,Memory,StorageType,Users,Tpm" > user-benchmark-result.csv
mssqlver=`sqlcmd -U sa -P 'mssqlpasswd1!' -h-1 -Y 15 -Q "set nocount on; SELECT SERVERPROPERTY('productversion')"`

for uc in ${Usercount}
do
    starttime=`date +%Y.%m.%d.%T`
    echo "Run with ${uc} users"
    sed -i "s/^vuset.*/vuset vu ${uc}/" runtest_mssql.tcl
    ./hammerdbcli auto runtest_mssql.tcl > test_mssql_${Testname}_${uc}.out 2>&1
    endtime=`date +%Y.%m.%d.%T`
    grep RESULT test_mssql_${Testname}_${uc}.out
    tpm=`grep TPM test_mssql_${Testname}_${uc}.out | awk '{print $7}'`
    echo "\"${starttime}\",\"${endtime}\",\"${hostnm}\",\"${krel}\",\"MSSQL\",\"${mssqlver}\",\"${numcpu}\",\"${totmem}\",\"${storagetype}\",\"${uc}\",\"${tpm}\"" >> user-benchmark-result.csv
    cp user-benchmark-result.csv $benchmark_results_dir
    cp /var/opt/mssql/mssql.conf $benchmark_results_dir
    cp test_mssql_${Testname}_${uc}.out $benchmark_results_dir
done

systemctl stop mssql-server
