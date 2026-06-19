## Getting started

In this repository we summarize the tools and processes used to run performance and scale tests. It can be used as a 
starting point for further investigation and testing. Scripts in this repository can be used to run the tests below on any storage, not only ioscale.


This repository is divided by test type.

- `db` for database-related test setup
- `io-generic` generic I/O tests using FIO
- `scalevm` tests related to virtual machine scaling

Please check these directories for specific information.

# Prerequisites

- OpenShift Container Platform setup (tested with v4.19/v4.20) up and running
- OpenShift Virtualization installed and running on top of OCP
- A functional storage backend able to allocate PVCs and create test virtual machines
- An `ssh` key and secret created to enable root SSH access to the virtual machine. An example script for secret generation is in [secretgen.sh](https://github.com/ekuric/ioscale/blob/main/templates/secretgen.sh)


# Database Testing Setup

For database testing, the [HammerDB](https://www.hammerdb.com) tool is used. Small adaptations were necessary for the virtual machine test case.
It is assumed we already have a test virtual machine up and running with SSH `root` access.
Virtual machines for testing can be created using various tools. An example script is [here](https://github.com/ekuric/ioscale/blob/main/templates/multivm.sh)

Once the virtual machine is up and running, it is possible to test MariaDB /  PostgreSQL / MSSQL with HammerDB workloads. More details in [db](https://github.com/ekuric/ioscale/tree/main/db) section and regarding specific test [db-README](https://github.com/ekuric/ioscale/blob/main/db/db-README.md) is first thing to read. We recommend to read it before starting testing. 

Recommended reading for Database testing

MariaDB/Postgresql/MSSQL general [readme](https://github.com/ekuric/ioscale/blob/main/db/db-README.md)

- MariaDB [containerized](https://github.com/ekuric/ioscale/blob/main/db/mariadb/mariadb-container/README-container.md)
- Postgresql [containerized](https://github.com/ekuric/ioscale/blob/main/db/postgresql/postgresql-container/README-container.md)
- MSSQL linux [containerized](https://github.com/ekuric/ioscale/blob/main/db/mssql/mssql-container/README-container.md)

Readme files contain detailed guide how to run database workloads at scale on Linux test machines. 


# I/O Generic Testing Setup

For I/O generic testing, the virtual machine can be identical to the one used for database tests. However, for the generic I/O FIO workload it is not necessary to have a virtual machine with large CPU/memory allocation. An example script is [here](https://github.com/ekuric/ioscale/blob/main/templates/multivm.sh) and can be used to create test virtual machines. 

After the test virtual machine is up and running, we can start the test as described in the [io-generic](https://github.com/ekuric/ioscale/blob/main/io-generic/fio-test-README.md)
For containerized version of test as start point [containerized](https://github.com/ekuric/ioscale/blob/main/io-generic/fio-container/README-container.md) version of test can be used. 

Image used for virtual machines is Centos 9. It is possible to use different image. However tools in this repository at this time only support `dnf/yum` install managers so be aware of that. Fedora 43 will give you access to all packages without need for subscription. In case you use image which does not have in base channel db packages, then you have to ensure that you have proper subscriptions assigned to test machines.




