#!/bin/tclsh
#

puts "SETTING CONFIGURATION"

global complete
proc wait_to_complete {} {
global complete
set  complete [vucomplete]
if {!$complete} {after 5000  wait_to_complete} else { exit } 
}


dbset db mssqls
dbset bm TPC-C
diset connection mssqls_linux_server 127.0.0.1
set mssql_pass mssqlpasswd1!
if {[info exists env(MSSQL_PASS)] && $env(MSSQL_PASS) ne ""} {
    set mssql_pass $env(MSSQL_PASS)
}
diset connection mssqls_pass $mssql_pass
diset tpcc mssqls_driver timed
diset tpcc mssqls_count_ware 500
diset tpcc mssqls_num_vu 100
diset tpcc mssqls_rampup 1
diset tpcc mssqls_duration 15

loadscript
vuset vu 20
vurun
wait_to_complete
vwait forever
