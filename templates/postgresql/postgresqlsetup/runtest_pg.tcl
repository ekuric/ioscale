#!/bin/tclsh
#

puts "SETTING CONFIGURATION"

global complete
proc wait_to_complete {} {
global complete
set  complete [vucomplete]
if {!$complete} {after 5000  wait_to_complete} else { exit } 
}

dbset db pg
dbset bm TPC-C
diset connection pg_host 127.0.0.1
diset connection pg_port 5432
diset tpcc pg_driver timed
diset tpcc pg_count_ware 500
diset tpcc pg_num_vu 10
diset tpcc pg_rampup 2
diset tpcc pg_duration 15

loadscript
vuset vu 10
vurun

wait_to_complete
vwait forever
