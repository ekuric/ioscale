# fusion-access



## Getting started

In this repository we will summarize tools and process we used to run Perf and Scale tests on Fusion Access storage. It can be used as
starting point for furher investigation and testing. 

This repository is divided on test types 

- `db` for database related test setup
- `io-generic` generic io tests, using FIO
- `scalevm` tests related to virtual machine scaling 

Please check these directories for specific information.

# Prerequesties 

- OpenShift Container Platform setup ( tested with v4.18/v4.19 )  up and runnning 
- OpenShift Virtualization installed and running on on top of OCP
- Storage backend functional and possible to allocate PVC from it and be able to create virtual machine. 
- `ssh` key and secret created to enable ssh root access to virtual machine. Example script for secret generation is in `/templates/secretgen.sh` 


# Database Testing Setup

For database testing we used [HammerDB](https://www.hammerdb.com) tool with small adaptations necessary for virtual machine test case.
It is assuemed we have already up and running test virutal machine with ssh `root` access. An example of template `vmdbtest.yml` for database testing found at template directory. Once virtual machine is up and runnng it is possible to test mariadb and postgresql workloads. More details in `db/mariadb` and `db/postgresql`


# I/O Genereic Testing Setup 
For I/O generic testing virtal machine could be identical as for database tests. However for generic I/O fio workload it is not necessary to have Virtual machine with massive cpu/memory allocatation. For this purpose `geniotest.yml` virtual template can be used. 

After test virtual machine is up and running we can start test as decribed in `io-generic` readme.

# VM scale test setup

For virtual machines scale testing we used `kube-burner` which is versatile tool for running machine density tests on OpenShift Container Platform. 
First we need to get `kube-burner` binary which can be found at [kube-burner github](https://github.com/kube-burner/kube-burner/releases). For more details check `scalevm/scale-README.md`. 




