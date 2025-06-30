#!/bin/tclsh
#

puts "SETTING CONFIGURATION"

global complete
proc wait_to_complete {} {
global complete
set  complete [vucomplete]
if {!$complete} {after 5000  wait_to_complete} else { exit } 
}

dbset db mysql
dbset bm TPC-C
diset connection mysql_host 127.0.0.1
diset connection mysql_port 3306
diset tpcc mysql_pass mysql
diset tpcc mysql_count_ware 500
diset tpcc mysql_driver timed
diset tpcc mysql_rampup 2
diset tpcc mysql_duration 15
loadscript
vuset vu 10
vucreate
vurun

wait_to_complete
vwait forever
