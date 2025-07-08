# VMScale testing 

In Virtual Machine Scale test ( VMScale ) we want to start maximum number of virutal machines with desired storage backend. Average time for machines to be in `Ready` state is collected. 

In order to start vmscale test do below from machine where `oc` command work.

1. get `kube-burner`  binary [kube-burner github](https://github.com/kube-burner/kube-burner/releases) and put it to `/usr/local/bin` of your bastion machine.
2. clone repository 
```
$ git clone https://github.com/ekuric/fusion-access.git
$ cd fusion-access  
```

start test 

``` 
$ OBJ_REPLICAS=10 kube-burner init -c templates/vmdensity-template.yml
```

`OBJ_REPLICAS` defines how many virtual machines we want to create.

Once test is done, it will create `kube-burner-<uuid>.log` log file where will be collected all important parameters related to this test. For us interesting part is line as
```
time="2025-06-24 15:47:34" level=info msg="vm-den-delete: VMReady 99th: 170535 max: 176375 avg: 150527" file="base_measurement.go:108"
```
which says `VMReady` before virtual machines are in `READY` state. 

# Important 

In `vm-dv.yml` template we can add `nodeSelector` specificiation 

```
spec:
      nodeSelector:
        scale.spectrum.ibm.com/role: test-storage
```
in order to start virtual only on Fusion Access storage nodes if that is important. If `nodeSelector` is used then it is necessary to label nodes with `scale.spectrum.ibm.com/role: test-storage`

File `templates/vmdensity-template.yml` is main configuration file, where we can change storage class and image used by virtual machines. 


