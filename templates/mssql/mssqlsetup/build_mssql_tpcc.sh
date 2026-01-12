## Collecting system information
## Setting up variables for the run and Collecting system information ####
Usercount="50"
whc="500"
benchmark_name="Hammerdb-tpcc"
echo ${benchmark_name} > ${benchmark_run_dir}/user-benchmark-name.txt
hostnm=`hostname -f`
hostip=`hostname -i`
numcpu=`nproc`
totmem=`cat /proc/meminfo |grep "MemTotal:" | awk '{print $2}'`
krel=`uname -r`      ## Kernel

usage()
{
  echo "Usage:
        ./build_mssql_tpcc.sh [-h] [-u users] [-w warehouse count] 

        Usage:
        -h help
        -u # of users (Default - 50)
        -w warehouse count (Default - 500)

       Examples:
        ./build_mssql_tpcc.sh -u "10" -w 50
            Build 50 warehouse tpcc database with 10 users
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
         *) usage;
            exit;
            ;;
esac
done


## Stop and start the mssql-server and drop and create the database
echo "Stopping MS SQL Server"
systemctl stop mssql-server


## Building the mssql.conf file based on recommendations

./mssql_settings.sh


echo "Starting MS SQL Server"
systemctl start mssql-server

echo "Waiting 60 seconds for DB to start"
sleep 60

# Setting host ip in build_mssql.tcl file
sed -i "s/^diset connection mssqls_linux_server.*/diset connection mssqls_linux_server ${hostip}/" build_mssql.tcl

echo "Building ${whc} warehouses with ${Usercount} users"
sed -i "s/^diset tpcc mssqls_count_ware.*/diset tpcc mssqls_count_ware ${whc}/" build_mssql.tcl
sed -i "s/^diset tpcc mssqls_num_vu.*/diset tpcc mssqls_num_vu ${Usercount}/" build_mssql.tcl

echo "Creating database"
./createdb_mssql.sh > crdb_${whc}WH_${Usercount}.out 2>&1

echo "Adding files to tempdb"
./update_temp.sh > update_temp_${whc}WH_${Usercount}.out 2>&1

echo "building the tpcc schema"

./hammerdbcli auto build_mssql.tcl > build_${whc}WH_${Usercount}.out 2>&1

systemctl stop mssql-server
