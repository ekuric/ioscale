#!/bin/bash
# ssh-keygen -b 2048 -t rsa -f /root/.ssh/vmkey -q -N ""
# generate key, or use already present key on bastion machine. If 
# key is present below command is enough. 

kubectl create secret generic vmkeyroot  --from-file=/root/.ssh/id_rsa.pub
