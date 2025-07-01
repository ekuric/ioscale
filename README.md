## Getting started

In this repository we will summarize tools and process we used to run Perf and Scale tests on Fusion Access storage. It can be used as
starting point for furher investigation and testing. Scripts in this repository can be used to run below tests on any storage, and not only Fusion Access.


This repository is divided on test types 

- `db` for database related test setup
- `io-generic` generic io tests, using FIO
- `scalevm` tests related to virtual machine scaling 

Please check these directories for specific information.

# Prerequesties 

- OpenShift Container Platform setup ( tested with v4.18/v4.19 )  up and runnning 
- OpenShift Virtualization installed and running on on top of OCP. 
- Storage backend functional and possible to allocate PVC from it and be able to create test virtual machines. 
- `ssh` key and secret created to enable ssh root access to virtual machine. Example script for secret generation is in `/templates/secretgen.sh` 


# Database Testing Setup

For database testing [HammerDB](https://www.hammerdb.com) tool is used. Small adaptations were necessary for virtual machine test case.
It is assumed we have already up and running test virutal machine with ssh `root` access. An example of virtual machine template `vmdbtest.yml` for database testing can be found found in [template](https://github.com/ekuric/fusion-access/tree/main/templates) directory. Once virtual machine is up and runnng it is possible to test mariadb and postgresql workloads. More details in [db](https://github.com/ekuric/fusion-access/tree/main/db)


# I/O Genereic Testing Setup 

For I/O generic testing virtal machine could be identical as for database tests. However for generic I/O fio workload it is not necessary to have Virtual machine with massive cpu/memory allocatation. For this purpose [geniotest.yml](https://github.com/ekuric/fusion-access/blob/main/templates/geniotest.yml) virtual template can be used. 

After test virtual machine is up and running we can start test as decribed in [io-generic](https://github.com/ekuric/fusion-access/blob/main/io-generic/io-README.md) readme.

# VM scale test setup

For virtual machines scale testing we used [kube-burner](https://github.com/kube-burner/kube-burner) which is versatile tool for running machine density tests on OpenShift Container Platform. 
First we need to get `kube-burner` binary which can be found at [kube-burner github](https://github.com/kube-burner/kube-burner/releases). For more details check [scalevm/scale-README.md](https://github.com/ekuric/fusion-access/blob/main/scalevm/scale-README.md) 




