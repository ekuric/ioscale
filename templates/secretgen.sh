#!/bin/bash
ssh-keygen -b 2048 -t rsa -f /root/.ssh/vmkey -q -N ""
kubectl create secret generic vmkeyroot  --from-file=/root/.ssh/vmkey.pub
