udfs=$@

for i in $udfs; 
do
	echo "[`date`] $i"; 
	pushd `dirname $i`; 
	timeout 3h python `basename $i` comp 2>&1 | tee `basename $i`.log;
	popd;
done
