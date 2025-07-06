mtpoint=none
disklist=none
dbhosts="127.0.0.1"
whc=50
Usercount="1"
# 10 20 30 40 50 60 70 80 90 100"


usage()
{
  echo " HOST NAME or IP REQUIRED!! - see usage below
        Usage:
        ./postgres.sh [-h] [-H Host names] [-d device] [-m mount point]

        Usage:
        -h help
        -H <Host names sepearted by space> - default 127.0.0.1
        -d <device > - default none
        -m <mount points> - default none
        -u <user count> - default "10 20 30 40 50"
        -w <warehouse count> - default "500"

       Examples:
        ./postgres.sh -H "vm1" -d /dev/vdb
        ./postgres.sh -H "vm1 vm2" -d /dev/vdb 
        ./postgres.sh -H "vm1" -m /perf1  
        ./postgres.sh -h "vm1 vm2" -m /perf1    
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
         *) usage;
            exit;
            ;;
esac
done

# copy scripts to the VMS
for hostnm in ${dbhosts}
do
   export hostnm
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "dnf -y install curl vim wget git"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "rm -rf /root/hammerdb-tpcc-wrapper-scripts"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "mkdir -p /root/hammerdb-tpcc-wrapper-scripts"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "export GIT_SSL_NO_VERIFY=true; git clone https://github.com/ekuric/fusion-access.git /root/hammerdb-tpcc-wrapper-scripts"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "chmod +x /root/hammerdb-tpcc-wrapper-scripts/templates/postgresql/Hammerdb-postgres-install-script"
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
      virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /root/hammerdb-tpcc-wrapper-scripts/templates/postgresql; ./Hammerdb-postgres-install-script -d ${disklist}" &
   else
      virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /root/hammerdb-tpcc-wrapper-scripts/templates/postgresql; ./Hammerdb-postgres-install-script -m ${mtpoint}" & 
   fi
   ctr=$((ctr + 1))
done

wait
echo "Posgres installed and started "

ctr=1
export ctr
for hostnm in ${dbhosts}
do
   export hostnm
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c  "systemctl restart postgresql"
   sleep 15
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "echo 'DROP DATABASE tpcc;' > input"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "echo 'DROP ROLE tpcc;' >> input"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "/usr/bin/psql -U postgres -d postgres -h 127.0.0.1 -f input"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; cp /root/hammerdb-tpcc-wrapper-scripts/templates/postgresql/postgresqlsetup/build_pg.tcl build${ctr}_pg.tcl"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; sed -i 's/^diset connection pg_host.*/diset connection pg_host 127.0.0.1/g' build${ctr}_pg.tcl"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; sed -i 's/^diset tpcc pg_count_ware.*/diset tpcc pg_count_ware ${whc}/g' build${ctr}_pg.tcl"
   virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; nohup ./hammerdbcli auto build${ctr}_pg.tcl > build_pg${ctr}.out 2>&1 " &
   ctr=$((ctr + 1))
done
wait
echo "Build done"

ctr=1
for hostnm in ${dbhosts}
do
   numhosts=${ctr}
   ctr=$((ctr + 1))
done
rundt=`date +%Y.%m.%d`
export rundt

export Usercount

for uc in ${Usercount}
do
   export ctr=1
   export uc
   for hostnm in ${dbhosts}
   do
     export hostnm
      virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; cp /root/hammerdb-tpcc-wrapper-scripts/templates/postgresql/postgresqlsetup/runtest_pg.tcl runtest${ctr}_pg.tcl"
      virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; sed -i 's/^diset tpcc pg_count_ware.*/diset tpcc pg_count_ware ${whc}/g' runtest${ctr}_pg.tcl"
      virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; sed -i 's/^vuset.*/vuset vu ${uc}/g' runtest${ctr}_pg.tcl"
      virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; sed -i 's/^diset tpcc pg_duration.*/diset tpcc pg_duration 15/g' runtest${ctr}_pg.tcl"
      virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; nohup ./hammerdbcli auto runtest${ctr}_pg.tcl > test_ESX_pg_${rundt}_${numhosts}pod_pod${ctr}_${uc}.out 2>&1 " &
      export outputfname=test_ESX_pg_${rundt}_${numhosts}pod_pod${ctr}_${uc}.out
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
     virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "cd /usr/local/HammerDB; grep TPM test_ESX_pg_${rundt}_${numhosts}pod_pod${ctr1}_${uc}.out | awk '{print $7}'"
     ctr1=$((ctr1 + 1))
   done
done

echo "All runs done. Stopping Postgres database instances"

for hostnm in ${dbhosts}
do
     export hostnm
     virtctl -n default ssh -t "-o StrictHostKeyChecking=no"  --local-ssh=true root@${hostnm} -c "systemctl stop postgresql;"
done

