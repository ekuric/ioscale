#!/bin/sh

export PATH=$PATH:.:
## Stop and start the mariadb server and drop and create the database
LD_LIBRARY_PATH=/lib64/mysql:$LD_LIBRARY_PATH
export LD_LIBRARY_PATH

## Setting up variables for the run and Collecting system information ####
Usercount="50"
whc="500"
Testname="HDB_tpcc_mariadb"
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
        ./build_mariadb_tpcc.sh [-h] [-u users] [-w warehouse count] 

        Usage:
        -h help
        -u # of users (Default - 50)
        -w warehouse count (Default - 500)

       Examples:
        ./build_mariadb_tpcc.sh -u "10" -w 50
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



## Updating the value of buffer_pool_size based on available memory
## Setting bufferpool to half of physical memory if memory is less than 128G else set to 64G
totmem_BP=`expr $totmem / 1024`
totmem_BP=`expr $totmem_BP / 2`

if [ $totmem_BP -lt 64000 ]
then
   sed -i "s/^innodb_buffer_pool_size=.*/innodb_buffer_pool_size=${totmem_BP}M/" my.cnf
else
   sed -i "s/^innodb_buffer_pool_size=.*/innodb_buffer_pool_size=64000M/" my.cnf
fi

/usr/bin/cp -f my.cnf /etc/my.cnf

### Shutting down and starting Mariadb instance ####
systemctl restart mariadb.service
echo "Restarted DB"
sleep 60

# Define number of warehouses and user count for the build


echo "Dropping database tpcc"
mysql -pdbpassword -e 'drop database tpcc;'

echo "Building ${whc} warehouses with ${Usercount} users"
sed -i "s/^diset connection mysql_host.*/diset connection mysql_host ${hostip}/" build_mariadb.tcl
sed -i "s/^diset tpcc mysql_count_ware.*/diset tpcc mysql_count_ware ${whc}/" build_mariadb.tcl
sed -i "s/^diset tpcc mysql_num_vu.*/diset tpcc mysql_num_vu ${Usercount}/" build_mariadb.tcl

./hammerdbcli auto build_mariadb.tcl > build_mariadb_${Testname}_${whc}WH_${Usercount}.out

echo "Mariadb TPCC database with ${whc} build done"
