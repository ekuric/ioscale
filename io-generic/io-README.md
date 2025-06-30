# Generic I/O Tests 

In order to run FIO i/o generic tests  we have to 


- create test virtual machine for this we can use virtual machine template `/templates/geniotest.yml`
- log into machine using `virtctl ssh root@TESTVM -i /root/.ssh/id_rsa`  and execute `fio-tests.sh`  
```
./fio-tests.sh <device>
``` 

Usually, newly added device ( PVC ), if template `geniotest.yml` is used is presented `/dev/vdc` and above command would be 
```
./fio-tests.sh vdc
```

After starting test it will run and generate fio json output files on test virtual machine. 



