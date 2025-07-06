mtpoint=none
disklist=none
dbhosts="127.0.0.1"
whc=50
Usercount="1"
# 10 20 30 40 50 60 70 80 90 100"
runname="HDB_MDB"
runtime="15"
storagetype="null"
export Usercount


usage()
{
  echo " HOST NAME or IP REQUIRED!! - see usage below

        Usage:
        ./mariadb.sh [-h] [-H Host names] [-d device] [-m mount point]
        ./mariadb.sh -H "vm1" -d /dev/vdc
        ./mariadb.sh -H "vm1 vm2" -d /dev/vdc 
        ./mariadb.sh -H "vm1" -m /perf1
        ./mariadb.sh -H "vm1 vm2" -d /dev/vdc
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
        -d) disklist=$2
            shift 2
            ;;
        -m) mtpoint=$2
            shift 2
            ;;
        -u) Usercount=$2
            shift 2
            ;;
        -w) whc=$2
            shift 2
            ;;
        -c) runname=${runname}_$2
            shift 2
            ;;
        -s) storagetype=$2
            shift 2
            ;;
        -t) runtime=$2
            shift 2
            ;;
         *) usage;
            exit;
            ;;
esac
done

for hostnm in ${dbhosts}
do
   export hostnm
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "dnf -y install git curl vim wget"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "rm -rf /root/hammerdb-tpcc-wrapper-scripts"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "mkdir -p /root/hammerdb-tpcc-wrapper-scripts"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "export GIT_SSL_NO_VERIFY=true; git clone https://github.com/ekuric/fusion-access.git /root/hammerdb-tpcc-wrapper-scripts" 
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "chmod +x /root/hammerdb-tpcc-wrapper-scripts/templates/mariadb/Hammerdb-mariadb-install-script"

done


if [[ $mtpoint == *"none"* ]]; then
    if [[ $disklist == *"none"* ]]; then
         echo "Please specify a disk device or Mount Point"
         exit;
      else
         echo "Using Disks"
      fi
fi

ctr=1
export ctr
for hostnm in ${dbhosts}
do
   export hostnm
   export disklist
   export mtpoint
   if [[ $mtpoint == *"none"* ]]; then
      virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /root/hammerdb-tpcc-wrapper-scripts/templates/mariadb; ./Hammerdb-mariadb-install-script -d ${disklist}" &
   else
      virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /root/hammerdb-tpcc-wrapper-scripts/templates/mariadb; ./Hammerdb-mariadb-install-script -m ${mtpoint}" & 
   fi
   ctr=$((ctr + 1))
done

wait
echo "Mariadb installed and started. Building database and loading data"
ctr=1
export ctr
for hostnm in ${dbhosts}
do
   export hostnm
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "mysql -pdbpassword -e 'drop database tpcc;'"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; cp build_mariadb.tcl build${ctr}_mariadb.tcl"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; sed -i 's/^diset tpcc mysql_count_ware.*/diset tpcc mysql_count_ware ${whc}/' build${ctr}_mariadb.tcl"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; nohup ./hammerdbcli auto build${ctr}_mariadb.tcl > build_mariadb_pod${ctr}.out 2>&1 " &
   ctr=$((ctr + 1))
done
wait
echo "Build and load done"


ctr=1
for hostnm in ${dbhosts}
do
   numhosts=${ctr}
   ctr=$((ctr + 1))
done
rundt=`date +%Y.%m.%d`
export rundt
iteration_name=" "
for uc in ${Usercount}
do
   export ctr=1
   export uc
   export runargs="-w ${whc} -u ${uc} -t ${runtime} -s ${storagetype}"
   export iteration_name=${runname}_${whc}WH_${uc}
   echo "${iteration_name} ${runargs}" > /usr/local/HammerDB/iteration.lis
   for hostnm in ${dbhosts}
   do
     export hostnm
         virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; ./run_mariadb_tpcc.sh ${runargs}" &
      ctr=$((ctr + 1))
      export ctr
   done
   wait
   echo "${uc} User run done"

done
echo "All runs done. Shutting down Mariadb databases"
for hostnm in ${dbhosts}
do
     export hostnm
     virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "systemctl stop mariadb.service"
     ctr1=$((ctr1 + 1))
done

