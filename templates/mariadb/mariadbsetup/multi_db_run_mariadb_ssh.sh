usage()
{
  echo " HOST NAME or IP REQUIRED!! - see usage below
        Usage:
        ./multi_vm_driver.sh [-h] [-H Host names] 

        Usage:
        -h help
        -H <Host names sepearted by space>

       Examples:
        ./multi_vm_driver.sh -H "dhcp31-32 dhcp31-33" 
        ./multi_vm_driver.sh -H "10.16.31.32 10.16.31.33" 
  "
}
if [ $# -eq 0 ]
then
    usage;
    exit;
fi

while [ $# -gt 0 ]
do
case $1 in
        -h) usage;
            exit;
            ;;
        -H) dbhosts=$2
            shift 2
            ;;
         *) usage;
            exit;
            ;;
esac
done
ctr=1
for hostnm in ${dbhosts}
do
   numhosts=${ctr}
   ctr=$((ctr + 1))
done
rundt=`date +%Y.%m.%d`
export rundt

#Usercount="10"
Usercount="10 20 40 80 100"
export Usercount

for uc in ${Usercount}
do
   export ctr=1
   export uc
   for hostnm in ${dbhosts}
   do
     export hostnm
      ssh root@${hostnm} "cd /usr/local/HammerDB-3.3; cp runtest_mariadb.tcl runtest${ctr}_mariadb.tcl"
      ssh root@${hostnm} "cd /usr/local/HammerDB-3.3; sed -i 's/^diset connection mysql_host.*/diset connection mysql_host ${hostnm}/' runtest${ctr}_mariadb.tcl"
      ssh root@${hostnm} "cd /usr/local/HammerDB-3.3; sed -i 's/^vuset.*/vuset vu ${uc}/' runtest${ctr}_mariadb.tcl"
      ssh root@${hostnm} "cd /usr/local/HammerDB-3.3; nohup ./hammerdbcli auto runtest${ctr}_mariadb.tcl > test_CNV_mariadb_${rundt}_${numhosts}pod_pod${ctr}_${uc}.out 2>&1 " &
      export outputfname=test_CNV_mariadb_${rundt}_${numhosts}pod_pod${ctr}_${uc}.out
      ctr=$((ctr + 1))
      export ctr
   done
     wait
     echo "${uc} User run done"

ctr1=1
export ctr1
   for hostnm in ${dbhosts}
   do
     export hostnm
     echo "host - ${hostnm}"
     ssh root@${hostnm} "cd /usr/local/HammerDB-3.3; grep TPM test_CNV_mariadb_${rundt}_${numhosts}pod_pod${ctr1}_${uc}.out | awk '{print $7}'" 
     ctr1=$((ctr1 + 1))
   done
done
echo "All runs done"

