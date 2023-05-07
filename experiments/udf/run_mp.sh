udfs=$@

for i in $udfs; 
do
	echo "[`date`] $i"; 
	pushd `dirname $i`; 
	timeout 1h python `basename $i` mp 2>&1 | tee `basename $i`.log;
	popd;
done
