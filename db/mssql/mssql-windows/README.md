# Testing MSSQL on Windows Server using HammerDB 

In this document we will summarize steps how to run HammerDB workload against MSSQL server running on windows server. We will present steps how to run test on baremetal and/or kvm virtual machines, and how to efecifelly use it to test MSSQL server running in OpenShift virtualization virtual machines.


## Prerequestis 

This tool expects that: 

- MSSQL server is installed and running on test machine(s). We do not do Windows MSSQL setup steps. We only need `mssql_pass` to know in advance in order to be able to create test database. 
Get necessary MSSQL bits from official Microsoft channels

- HammerDB tool installed on test machines, easy to get from [this link](https://www.hammerdb.com/download.html)

The Tool will build database ( assuming MSSQL server process is up and running ) and start test.

## Example of Usage 

```
# python mssqlwin.py -h
usage: mssqlwin.py [-h] [-c CONFIG] [-v] [--dry-run] [--copy-results] [--ssh-only] [--virtctl-only] [--test-script TEST_SCRIPT] [--rebuild-script REBUILD_SCRIPT] [--create-db CREATE_DB]
                   [--hammerdb-test-script HAMMERDB_TEST_SCRIPT] [--build-schema-file BUILD_SCHEMA_FILE] [--generate-only] [--rebuild-always] [--prepare-machine]

MSSQL HammerDB Windows Testing Script (YAML Configuration Version)

optional arguments:
  -h, --help            show this help message and exit
  -c CONFIG, --config CONFIG
                        Path to YAML configuration file (default: mssql-config.yaml)
  -v, --verbose         Verbose output
  --dry-run             Validate configuration and show what would be done without executing
  --copy-results        Only copy results from hosts (skip rebuild and tests)
  --ssh-only            Force SSH for all hosts (baremetal/KVM, no virtctl)
  --virtctl-only        Force virtctl for all hosts (OpenShift VMs)
  --test-script TEST_SCRIPT
                        Local test script to copy to Windows hosts and run
  --rebuild-script REBUILD_SCRIPT
                        Local rebuild script to copy to Windows hosts and run
  --create-db CREATE_DB
                        Local create_db.sql to copy to Windows hosts
  --hammerdb-test-script HAMMERDB_TEST_SCRIPT
                        Local HammerDB test script to copy to Windows hosts
  --build-schema-file BUILD_SCHEMA_FILE
                        Local build schema TCL to customize and copy to Windows hosts
  --generate-only       Only generate per-user files locally and exit
  --rebuild-always      Rebuild database before each user-count test run
  --prepare-machine     Prepare Windows machines by formatting the data disk and exit

EXAMPLES:
    python3 mssqlwin.py                          # Use default mssql-config.yaml
    python3 mssqlwin.py -c mssql-config.yaml     # Use custom configuration file
    python3 mssqlwin.py -c mssql-config.yaml -v  # Verbose output
    python3 mssqlwin.py --copy-results           # Only copy results
        
``` 

Content of configuration file is presented below 

`mssql-configwin.yaml` 

```
# Minimal MSSQL HammerDB Windows Configuration (mssqlwin.py)

description: "1-100-users-hammerdb-2win-parallel"

database:
  # Choose ONE of: hosts, host_pattern, host_labels, host_file
  hosts: "192.168.122.201 192.168.122.200"   # Space-separated list of hosts
  # host_pattern: "winvm-{1..3}"              # Expands to winvm-1 winvm-2 winvm-3
  # host_labels: "app=mssql,role=primary"     # OpenShift VM labels (virtctl)
  # host_file: "mssql_hosts.txt"              # File with one host per line
  namespace: "default"                       # Only used for virtctl mode
  test_duration: 1                           # Test duration in minutes
  # mssql_total_iterations: 10000000         # can be set - we use default from HammerDB
  # warehouse_count: 500                     # if used - must be consistent accross runs
  user_count: "1 100"                        # list of HammerDB test user "1 10 20 30 60 100" 
  # mssql_pass: "mypass"                     # msssql database password 

windows:
  hammerdb_path: "C:\\tools\\Hammerdb-4.12"
  test_script: ""
  hammerdb_test_script: "" 
  result_dir: "C:\\tools\\Hammerdb-4.12\\results"
  rebuild_script: "" 
  create_db_sql: ""
  ssh_user: "Administrator"
  rebuilddb: false                            # rebuild database prior testing
  rebuild_always: false                       # rebuild database before each user count
  rebuild_only: false                         # only rebuild db 
  test_only: true                             # only test - no rebuild, assuming rebuild was done before

```

As shown in above `mssql-configwin.yaml` example we can specify test host on four different ways

1. space separted hosts eg. `hosts: "host1 host2` 
2. hostname pattern, eg. `host_pattern: "winvm-{1..3}"` this is useful if there are many test hosts
3. host labels , eg. `host_labels: "app=mssql,role=primary"` useful in OCP environments
4. host_file: "mssql_hosts.txt" in this case we put hostnames for example 
```
host1
host2 
host3 
```

The workflow we follow here is 

1. Build database 

`python mssqlwin.py -c mssql-configwin.yaml --rebuild-script rebuild-db.ps1 --create-db create_db.sql` 

2. Generate configuration files 

Assuming MSSQL server is up and running, then we can build database for testing. It will be achieved with above command. Rebuild script `rebuild-db.ps1` and `create-db.sql` are part of this repository.
This tool accepts any rebuld / create database scripts. It only passes them to powershell on windows side and execute them. 

2. Generate test files

In this step we generate test files based on sample input specified with `--test-script` , `--hammerdb-test-script` 

We can run test directly without generating files, but this is best practice as it allows to check test configuration and if eventually change it locally. 

` # python  mssqlwin.py -c mssql-configwin.yaml  --test-script hammerdb-sa-test.ps1 --hammerdb-test-script mssqls_tprocc_run.tcl --ssh-only --generate-only` 

`--generate-only` hammerdb / powershell test scripts will be generated from `hammerdb-sa-test.ps1` and `mssqls_tprocc_run.tcl` and saved locally. 
If `windows.build_schema_file` is set, the build schema TCL is updated with `database.warehouse_count`
(`diset tpcc mssqls_count_ware <count>`) and saved locally as well. In generate-only mode, if
`windows.create_db_sql` is provided, a sized `create_db.sql` is generated with:
- data `SIZE` = `warehouse_count * 150MB`
- log `SIZE` = `warehouse_count * 75MB`
- log `MAXSIZE` = data size
The generated `create_db.sql` is written to `.mssqltestfiles-generated/` as `create_db-wh<COUNT>.sql`.

```
 ls -l .mssqltestfiles-generated/
total 16
-rw-r--r--. 1 root root  346 Mar 25 05:11 hammerdb-sa-test_100.ps1
-rw-r--r--. 1 root root  344 Mar 25 05:11 hammerdb-sa-test_1.ps1
-rw-r--r--. 1 root root 1100 Mar 25 05:11 mssqls_tprocc_run100.tcl
-rw-r--r--. 1 root root 1098 Mar 25 05:11 mssqls_tprocc_run1.tcl

```
Test can be started with 

`python  mssqlwin.py -c mssql-configwin.yaml` 

it will pick up configuration files from `.mssqltestfiles-generated/` scp-copy them to test hosts and execute them. 

Required Windows setup

- Passwordless SSH access works for the Windows user (default: `Administrator`). Ensure that proper keys are copied / integrated to test machines. For KVM they can be injected on virtual machine creation, for OCP VM ssh keys can be passed to VMs using kubernetes secrets. 

- HammerDB tools are preinstalled on the Windows image (default: `C:\tools\Hammerdb-4.12`).
- Database has to be created and rebuild, one can use as example `rebuild-db.ps1`, `create_db.sql`, but any rebuild/created scripts will work, they are transfered to host(s) and executed. 

Configuration (mssql-configwin.yaml)
- `windows.hammerdb_path`: path to HammerDB tools on Windows.
- `windows.test_script`: PowerShell script to execute for the test run.
- `windows.result_dir`: directory on Windows where test outputs are written.
- `windows.ssh_user`: SSH user for Windows hosts (default `Administrator`).
- `windows.rebuilddb`: `true` to run `rebuild-db.ps1` before tests, `false` to skip.
- `windows.rebuild_always`: `true` to rebuild before each user-count test run.
- `windows.rebuild_timeout`: optional timeout in seconds for rebuild step (omit to disable).
- `windows.rebuild_script`: optional path to a custom rebuild script on the host.
- `windows.create_db_sql`: optional path to `create_db.sql` on the host (if provided locally, it is copied to the host).
- `windows.hammerdb_test_script`: optional HammerDB test TCL path on the host.
- `windows.build_schema_file`: optional build schema TCL path used for rebuild (local file is patched with `warehouse_count`).
- `windows.mssql_pass`: Required MSSQL password override used to patch generated TCL files. This is password used to connect to MSSQL database. 
- `database.warehouse_count`: optional warehouse count override for generated TCL files.
- `database.mssql_total_iterations`: optional iteration override for generated TCL files.
- CLI override: `mssqlwin.py --test-script <local.ps1>` copies the script to the host and runs it.
- CLI override: `mssqlwin.py --rebuild-script <local.ps1>` copies the script to the host and runs it.
- CLI override: `mssqlwin.py --hammerdb-test-script <local.tcl>` copies the test file to the host.
- `windows.rebuild_only`: `true` to run rebuild only and exit.
- CLI override: `mssqlwin.py --create-db <local.sql>` copies `create_db.sql` to the host.
- CLI override: `mssqlwin.py --rebuild-always` rebuilds before each user count.
- `--prepare-machine` formats the data disk (per `windows.disk_id`) and exits.
- For script paths: if the value points to a local file (e.g., `./rebuild-db.ps1`), it is copied to the Windows host; if it points to a Windows path (e.g., `C:\tools\...`), it is assumed to already exist on the host.

What the script does in Windows mode

- Skips Linux package install, git clone, and MSSQL install.
- Optionally runs `rebuild-db.ps1` (controlled by `windows.rebuilddb`) and streams output.
- Generates per-user PowerShell/TCL scripts locally and copies them to each host.
- Runs per-user tests in parallel across hosts.
- Collects results back to the bastion from `windows.result_dir` and extracts them locally.
- Cleans up result files in `windows.result_dir` on each host after a successful copy.
- Copies `.mssqltestfiles-generated` into results as `mssqltestfiles-generated`.
- When using virtctl, collects VM domain details under `vm-dump/<host>/` (dumpxml, dominfo, domstats, etc.).

Environment variables passed to Windows scripts

- `HAMMERDB_PATH`: base HammerDB directory on the host (rebuild step).
- `CREATE_DB_SQL`: path to `create_db.sql` on the host (rebuild step).
- `MSSQL_PASS`: MSSQL password used by rebuild script (if set).
- `HAMMERDB_TEST_SCRIPT`: TCL test file path on the host (test step).
- `BUILD_SCHEMA_TCL`: build schema TCL path on the host (rebuild step).
- `HAMMERDB_WAREHOUSE_COUNT`: warehouse count (from config).
- `HAMMERDB_TEST_DURATION`: test duration in minutes (from config).
- `HAMMERDB_USER_COUNT`: user count for the current run.
- `HAMMERDB_RESULT_DIR`: results directory on the host (test step).
- `RESULT_DIR`: alias for `HAMMERDB_RESULT_DIR` (test step).

Notes on test scripts

- For `--test-script`, use `$env:HAMMERDB_RESULT_DIR` (or `$RESULT_DIR`) in the PowerShell template so output goes to `windows.result_dir`.
- The generator replaces `mssqls_tprocc_run_$user_count.tcl` and `mssqls_tprocc_010vu_run1` with per-user values when creating per-user scripts.
- You can set `database.mssql_pass` in the config to patch `diset connection mssqls_pass` in generated TCL files (Windows-specific override: `windows.mssql_pass`).
- If `database.warehouse_count` is set, the generator updates the `diset tpcc mssqls_count_ware` line in the TCL template. If it is not set, the template value is left unchanged.
- If `database.mssql_total_iterations` is set, the generator updates the `diset tpcc mssqls_total_iterations` line in the TCL template; otherwise it keeps the template default.
- If a cached `create_db-wh<COUNT>.sql` exists in `.mssqltestfiles-generated`, `mssqlwin.py` uses it automatically and logs the selected path.

Generated files cache
- Generated per-user PowerShell/TCL files are written to `.mssqltestfiles-generated`.
- If that directory already contains all required per-user files, you can run `mssqlwin.py` without `--test-script` or `--hammerdb-test-script`.
- To refresh cached files, re-run with `--test-script` and `--hammerdb-test-script` (optionally `--generate-only`).
- Empty strings in config (for example `windows.test_script: ""`) are treated as unset so CLI overrides still apply.
- If `windows.test_script` or `windows.hammerdb_test_script` are unset, the script will use cached generated files when available.

# Test results

After test is done, results will be collected from test machines and copied to localhost, example is below where we see that results are collected per machine and per number of test users. 


```
# cd mssql-results-20260325-062555-1_20_40_50_60_80_100_users_running_parallel_on_2windows_kvm
[root@perf-intel-3 mssql-results-20260325-062555-1_20_40_50_60_80_100_users_running_parallel_on_2windows_kvm]# tree .
.
├── 192.168.122.200
│   ├── mssqls_tprocc_001vu_run1.json
│   ├── mssqls_tprocc_001vu_run1.out
│   ├── mssqls_tprocc_020vu_run1.json
│   ├── mssqls_tprocc_020vu_run1.out
│   ├── mssqls_tprocc_040vu_run1.json
│   ├── mssqls_tprocc_040vu_run1.out
│   ├── mssqls_tprocc_050vu_run1.json
│   ├── mssqls_tprocc_050vu_run1.out
│   ├── mssqls_tprocc_060vu_run1.json
│   ├── mssqls_tprocc_060vu_run1.out
│   ├── mssqls_tprocc_080vu_run1.json
│   ├── mssqls_tprocc_080vu_run1.out
│   ├── mssqls_tprocc_100vu_run1.json
│   └── mssqls_tprocc_100vu_run1.out
├── 192.168.122.201
│   ├── mssqls_tprocc_001vu_run1.json
│   ├── mssqls_tprocc_001vu_run1.out
│   ├── mssqls_tprocc_020vu_run1.json
│   ├── mssqls_tprocc_020vu_run1.out
│   ├── mssqls_tprocc_040vu_run1.json
│   ├── mssqls_tprocc_040vu_run1.out
│   ├── mssqls_tprocc_050vu_run1.json
│   ├── mssqls_tprocc_050vu_run1.out
│   ├── mssqls_tprocc_060vu_run1.json
│   ├── mssqls_tprocc_060vu_run1.out
│   ├── mssqls_tprocc_080vu_run1.json
│   ├── mssqls_tprocc_080vu_run1.out
│   ├── mssqls_tprocc_100vu_run1.json
│   └── mssqls_tprocc_100vu_run1.out
└── mssqlwin-20260325-1_20_40_50_60_80_100_users_running_parallel_on_2windows_kvm.txt
```

In collected `.json` files we have TPM values for particular test 
```
HAMMERDB RESULT
[
  "69C3E265AB0603E203438303",
  "2026-03-25 06:25:57",
  "1 Active Virtual Users configured",
  "TEST RESULT : System achieved 29051 NOPM from 67250 SQL Server TPM"
]

```
We work to create script to draw these results automatically. 



