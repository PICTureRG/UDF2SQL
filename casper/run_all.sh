#!/bin/bash

mkdir -p log
rm -fr log/*

for src in $@
do
    id=`basename -s .sk $src`
    echo "================$src=================="
    { time timeout 1h bash run.sh $src; } 2>&1 | tee log/$src.log
    echo
done
