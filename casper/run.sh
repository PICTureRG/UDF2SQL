#!/bin/bash

src=$1
id=`basename -s .sk $src`
params=`head -n1 $src`
params=${params:2}

mkdir -p log/$id
rm -fr log/$id/*

for ((depth=1;depth<100;depth++))
do
    for ((grammar=1;grammar<=2;grammar++))
    do
        echo "int depth=$depth;" > config.sk
        echo "int grammar=$grammar;" >> config.sk
        echo "(depth=$depth, grammar=$grammar)"
        echo "sketch $params $src &> log/$id/$depth.$grammar"
        sketch $params $src &> log/$id/$depth.$grammar
        if [ $? -eq 0 ]
        then
            cat log/$id/$depth.$grammar
            exit 0
        fi
    done
done
