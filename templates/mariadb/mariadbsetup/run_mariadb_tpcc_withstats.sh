#!/bin/sh
export PATH=$PATH:.:

LD_LIBRARY_PATH=/lib64/mariadb:$LD_LIBRARY_PATH
export LD_LIBRARY_PATH

## Set variables for the run
storagetype="Storage not specified"
Usercount="10 20 40 80 100"
whc="500"
benchmark_results_dir=`pwd`"/results"
DBRESTARTUP="n"
Testname="HDB_tpcc_mariadb"

usage()
{
  echo "Usage:
        run_mssql_tpcc.sh [-h] [-u users] [-t runlength] [-w warehouse count] [-s storage type]

        Usage:
        -h help
        -u # of users (Default - "10 20 40 80 100")
        -t runlength in minutes (Default - 15 minuteS)
        -w warehouse count (Default - 500 )
        -s storage type ( Default - "Storage not speficied" )
        -n name for the run (Default - "HDB_tpcc_mariadb" )

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
        -n) Testname=$2
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


## Collecting system information
benchmark_name="Hammerdb-tpcc"
echo ${benchmark_name} > ${benchmark_run_dir}/user-benchmark-name.txt
hostnm=`hostname -f`
hostip=`hostname -i`
numcpu=`nproc`
totmem=`cat /proc/meminfo |grep "MemTotal:" | awk '{print $2}'`
krel=`uname -r`      ## Kernel

## Stop and start the mariadb server and drop and create the database
#systemctl restart mariadb.service
#echo "Restarted DB"
#sleep 60


# Set Host IP and Warehouse Count in the tcl file
sed -i "s/^diset connection mysql_host.*/diset connection mysql_host ${hostip}/" runtest_mariadb.tcl
sed -i "s/^diset tpcc mysql_count_ware.*/diset tpcc mysql_count_ware ${whc}/" runtest_mariadb.tcl

echo "StartTime,EndTime,Hostname,Kernel,Database,DBVer,Cpus,Memory,StorageType,Users,Tpm" > user-benchmark-result.csv
mariaver=`mysql -V |awk '{print $5}' | sed -e 's/,//'`


for uc in ${Usercount}
do
    echo "Restarting Database before each run"
    systemctl restart mariadb.service
    starttime=`date +%Y.%m.%d.%T`
    echo "Run with  ${uc} users"
    vmstat -n 3  > vmstat_mariadb_${Testname}_${uc}_${starttime}.out &
    iostat -dmxz 3 > iostat_mariadb_${Testname}_${uc}_${starttime}.out &
    sed -i "s/^vuset.*/vuset vu ${uc}/" runtest_mariadb.tcl
    ./hammerdbcli auto runtest_mariadb.tcl > test_mariadb_${Testname}_${uc}.out 2>&1
    endtime=`date +%Y.%m.%d.%T`
    grep RESULT test_mariadb_${Testname}_${uc}.out
    tpm=`grep TPM test_mariadb_${Testname}_${uc}.out | awk '{print $7}'`
    echo "\"${starttime}\",\"${endtime}\",\"${hostnm}\",\"${krel}\",\"Mariadb\",\"${mariaver}\",\"${numcpu}\",\"${totmem}\",\"${storagetype}\",\"${uc}\",\"${tpm}\"" >> user-benchmark-result.csv
    killall vmstat iostat
    cp user-benchmark-result.csv $benchmark_results_dir
    cp /etc/my.cnf $benchmark_results_dir
    cp test_mariadb_${Testname}_${uc}.out $benchmark_results_dir
    cp vmstat_mariadb_${Testname}_${uc}.out $benchmark_results_dir
    cp iostat_mariadb_${Testname}_${uc}.out $benchmark_results_dir
done
systemctl stop mariadb.service 
