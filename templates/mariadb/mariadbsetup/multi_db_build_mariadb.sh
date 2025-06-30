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
   mysql -pdbpassword -h${hostnm} -e 'drop database tpcc;'
   cp build_mariadb.tcl build${ctr}_mariadb.tcl
   sed -i "s/^diset connection mysql_host.*/diset connection mysql_host ${hostnm}/" build${ctr}_mariadb.tcl
   nohup ./hammerdbcli auto build${ctr}_mariadb.tcl > build_mariadb_pod${ctr}_${uc}.out 2>&1 &
   ctr=$((ctr + 1))
done
wait
echo "Build done"

