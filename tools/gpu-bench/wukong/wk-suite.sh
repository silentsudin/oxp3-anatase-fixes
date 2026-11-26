#!/bin/bash
# Each variant: relaunch with env, warm run, then two measured runs (MangoHud stats).
S=$(cd "$(dirname "$0")" && pwd)
W=$HOME/.cache/oxp3-winre; O=$HOME/.cache/oxp3-winre/wk/suite; mkdir -p $O
run() { # name env...
    local n=$1; shift
    $S/wk-variant.sh "$n-a" "$@" > $O/$n.log 2>&1
    $S/wk-bench.sh $HOME/.cache/oxp3-winre/wk/$n-b.png >> $O/$n.log 2>&1
    grep -h "frames=" $O/$n.log | sed "s/^/$n /" | tee -a $O/summary.txt
}
run lsc intel_sampler_route_to_lsc=true
run wt intel_storage_cache_policy_wt=true
run atom anv_enable_opt_divergent_atomics=3
run cm1 intel_code_motion=1
run cm2 intel_code_motion=2
run base
echo "suite done" >> $O/summary.txt
