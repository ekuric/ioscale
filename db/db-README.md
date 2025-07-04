# Database testing setup

For database testing we used [HammerDB](https://www.hammerdb.com) tool with small adaptations necessary for virtual machine test case.

- create ssh key and secret - `secretgen.sh` can be used to generate key/secret for passwordless login to virtual machine 

- create virtual machine for database testing, it can be created using `vmdbtest.yml` template. It is necessary to check that template and adapt cpu / memory limits
- image ( currently it uses centos 10 stream image. You can use any image, but it is necessary to ensure it has proper repository to install mariadb/postgresql bits. RHEL images 
does not provide these in default channels. Ensure you have right golden image. Centos image in `vmdbtest.yml` is used for test purposes for easy installation. 
- ensure that separate device is allocated for database tests. In `vmdbtests.yml` that is specified by 
- test scripts expect test virtual machine to be in `default` namespace. 

```
 name: datavolumedb
      spec:
        pvc:
          accessModes:
            - ReadWriteOnce
          resources:
            requests:
              storage: 1000Gi
          storageClassName: ibm-spectrum-scale-sample
          volumeMode: Filesystem
``` 

1000Gi is probably too much for PVC, depending on how many warehouses/clients is tested, it might need less / more space. 
- Ensure proper `storageClass` is used for virtual machine to allocated storage. In `vmdbtest.yml` example it is `ibm-spectrum-scale-sample`, edit this template and use storage class you want to test. Very likely any storage class will work.

Once there is desired test virtual machine up and running. Starting tests is easy as 

For MariaDB 

```
./mariadb.sh -H "vm name from as presented `oc get vm`" -d /dev/vdc 
``` 

For Postgresql 
```
./postgresql.sh -H "vm name from as presented `oc get vm`" -d /dev/vdc
``` 

It is important to mention that above scripts will work for multiple virtual machines 
For example
```
#./mariadb.sh -H "vm1 vm2 vm3 vm4" -d /dev/vdc
#./postgresql.sh -H "vm1 vm2 vm3 vm4" -d /dev/vdc
``` 
Virtual machine names are ones we get with `oc get vm` 

Be aware to adapt test parameters for multiple virual machine test case. It is to expected to run lower number of clients / warehouses than for case when single virtual machine is tested.


Note: in virtual environment, first disk ( from PVC ) is `usually` presented as `/dev/vdc`. It is recommended to check is that the case for your VM. 

# Database Test Results 
Test results for database testing will be saved to `/usr/local/HammerDB/` on the test machine. For MariaDB look for files with name `test_mariadb_HDB_tpcc_mariadb*` and for postgresql `test_ESX_pg_*`.

Results will be also visible in terminal where test script is executed. 

