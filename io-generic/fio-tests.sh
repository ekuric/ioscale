#!/bin/bash

# create test directories 
device="$1"

printf "create test directories\n"

mkdir direct
mkdir -p /root/tests/data
dnf install -y fio 

# write test dataset 

# format device 
printf "format test device\n" 

umount /root/tests/data
mkfs.xfs -f /dev/$device

mount /dev/$device /root/tests/data 

printf "write dataset\n" 

fio --name=write --directory=/root/tests/data --size=5GB  --rw=randwrite --bs=4k --runtime=300 --direct=1 --numjobs=16 --time_based=1 --iodepth=16  --output-format=json+ --output=direct/writedata.json


for bs in 4096k 128k 8k 4k; do 
	for fiotest in randwrite randread write read; do
		fio --name=write --directory=/root/tests/data --size=5GB --rw=$fiotest --bs=$bs --runtime=300 --direct=1 --numjobs=16 --time_based=1 --iodepth=16  --output-format=json+ --output=direct/fio-test-$fiotest-bs-$bs.json
	done
done 
