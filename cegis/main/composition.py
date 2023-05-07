import logging
import os, sys, signal
import queue
import pprint
from copy import deepcopy
import cegis
from dfg import gen_dfg

LOG = logging.getLogger("composition")
pp = pprint.PrettyPrinter(indent=4)

def compose_sql(dfg, block):
    args_sql = []
    for arg in block['args']:
        if arg[2] < 0:
            args_sql.append(f"@arg{-arg[2]-1}")
        else:
            args_sql.append(compose_sql(dfg, dfg['blocks'][arg[2]]))
    sql = block['sql']
    for i in range(len(args_sql)):
        sql = sql.replace(f'@param{i}', args_sql[i])
    return sql

def comp_synth(dfg):
    for bid in dfg['blocks']:
        block = dfg['blocks'][bid]
        if 'sql' in block:
            if block['sql'] is None:
                return bid
            continue
        
        LOG.info(f"----------------------Synthesizing block {bid}----------------------")
        LOG.debug(f"\n{pprint.pformat(block)}")
        sql = cegis.c_cegis(block)
        if sql is None:
            block['sql'] = None
            return bid
        else:
            block['sql'] = str(sql)

    # import pdb; pdb.set_trace()
    sqls = list(compose_sql(dfg, dfg['blocks'][i]) for i in dfg['returns'])
    if len(dfg["returns"]) == 1:
        return sqls[0]
    else:
        return f"row({', '.join(sqls)})"

def enumerate_node(dfg):
    for bid in dfg['blocks']:
        yield bid


pids = {} # pid -> (r, block_from)
def comp_synth_mp(dfg, debug=False):
    np = os.cpu_count()
    node_iter = enumerate_node(dfg)

    from2bid = {str(block.get('from', {bid})): bid for bid, block in dfg['blocks'].items()}
    killed_pids = set()
    running_block = set()
    for p in pids:
        if pids[p][1] not in from2bid:
            LOG.info(f"Killing pid[{p}] because block {pids[p][1]} is removed")
            os.kill(p, signal.SIGKILL)
            pids[p][0].close()
            killed_pids.add(p)
        else:
            running_block.add(from2bid[pids[p][1]])

    for p in killed_pids:
        os.waitpid(p, 0)
        del pids[p]

    while True:
        # check error
        for bid, block in dfg['blocks'].items():
            if 'sql' in block:
                if block['sql'] is None:
                    return bid
            else:
                break

        if len(pids) < np:
            bid = next(node_iter, None)
            if bid is not None:
                block = dfg['blocks'][bid]
                if 'sql' in block:
                    continue
                if bid in running_block:
                    continue

                pid, r = cegis_mp(dfg, bid, debug)
                pids[pid] = (r, str(block.get('from', {bid})))
                continue

        if len(pids) == 0:
            sqls = list(compose_sql(dfg, dfg['blocks'][i]) for i in dfg['returns'])
            if len(dfg["returns"]) == 1:
                return sqls[0]
            else:
                return f"row({', '.join(sqls)})"

        LOG.info(f"waiting child processes { {k: v[1] for k,v in pids.items()} }")
        pid, status = os.wait()
        assert status == 0, f"pid [{pid}] exit with {status}"
        r, block_from = pids[pid]
        del pids[pid]
        bid = from2bid[block_from]
        block = dfg['blocks'][bid]
        sql = r.read()
        block['sql'] = sql if sql != '' else None

def merge_dfg_node(dfg, pid, cid, i):
    pblock = dfg["blocks"][pid]
    cblock = dfg["blocks"][cid]
    del cblock["args"][i]
    if "sql" in cblock:
        del cblock["sql"]
    cblock["args"] = list(set(cblock["args"]).union(pblock["args"]))
    if "url" in cblock:
        del cblock["url"]
    cblock["body"] = f"{pblock['body']}\n{cblock['body']}"
    cblock["ints"] = list(set(cblock.get("ints", [])).union(set(pblock.get("ints", []))))
    cblock["doubles"] = list(set(cblock.get("doubles", [])).union(set(pblock.get("doubles", []))))
    cblock["strings"] = list(set(cblock.get("strings", [])).union(set(pblock.get("strings", []))))
    cblock["depth"] += pblock["depth"]
    cblock["from"] = pblock.get("from", {pid}).union(cblock.get("from", {cid}))

def merge_dfg(dfg, bid):
    merged = []

    # collect neighbors (parents and children)
    parents = [(a[2], i) for i, a in enumerate(dfg["blocks"][bid]['args']) if a[2] >= 0]
    children = []
    for cid, block in dfg["blocks"].items():
        for j, a in enumerate(block['args']):
            if a[2] == bid:
                children.insert(0, (cid, j))

    # try merging with all children
    if len(children) > 0:
        g = deepcopy(dfg)
        for cid, i in children:
            merge_dfg_node(g, bid, cid, i)
        del g["blocks"][bid]
        merged.insert(0, g)

    # try merging with each parent
    for pid, i in parents:
        g = deepcopy(dfg)
        merge_dfg_node(g, pid, bid, i)
        merged.append(g)

    # import pdb; pdb.set_trace()
    return merged

def merge_dfg_mp(dfg, bid):
    merged = []

    # collect neighbors (parents and children)
    parents = [(a[2], i) for i, a in enumerate(dfg["blocks"][bid]['args']) if a[2] >= 0]
    children = []
    for cid, block in dfg["blocks"].items():
        for j, a in enumerate(block['args']):
            if a[2] == bid:
                children.insert(0, (cid, j))

    # try merging with all children
    if len(children) > 0:
        g = deepcopy(dfg)
        for cid, i in children:
            merge_dfg_node(g, bid, cid, i)
        del g["blocks"][bid]
        merged.insert(0, g)

    # try merging with each parent
    for pid, i in parents:
        g = deepcopy(dfg)
        merge_dfg_node(g, pid, bid, i)
        merged.append(g)

    # import pdb; pdb.set_trace()
    return merged

def synth(udf, refinement=True, debug=False, mp=False):
    cegis.init(debug)

    LOG.info("Constructing DFG")
    dfg = gen_dfg(udf)

    dfg_queue = [dfg]
    while len(dfg_queue) > 0:
        dfg = dfg_queue[0]
        del dfg_queue[0]
        LOG.info(f"=====================DFG size {len(dfg['blocks'])}======================")
        LOG.debug(f"\n{pprint.pformat(dfg)}")

        if mp:
            sql = comp_synth_mp(dfg, debug=debug)
        else:
            sql = comp_synth(dfg)

        if type(sql) == int: # FIXME: hacky
            LOG.info("Synthesized failed")
            if refinement:
                LOG.info("Trying to merge node")
                merged_dfgs = merge_dfg(dfg, bid=sql)
                dfg_queue = merged_dfgs + dfg_queue
            else:
                LOG.info("DFG refinement is disabled, exit with failure")
                return
        else:
            LOG.info(f"Composed SQL: {sql}")
            return sql
    LOG.info("Failed to syntheis with refinement")

def enumerate_dfg_nodes(dfg_queue):
    while len(dfg_queue) > 0:
        dfg = dfg_queue[0]
        del dfg_queue[0]
        LOG.info(f"=====================DFG size {len(dfg['blocks'])}======================")
        LOG.debug(f"\n{pprint.pformat(dfg)}")

        for bid in dfg['blocks']:
            yield dfg, bid

def cegis_mp(dfg, bid, debug=False):
    block = dfg['blocks'][bid]
    blockname = "_".join([str(i) for i in block.get("from", {bid})])
    r, w = os.pipe()
    r, w = os.fdopen(r, 'r'), os.fdopen(w, 'w')
    pid = os.fork()
    if pid == 0:
        r.close()
        cegis.init(debug=debug, work_dir=f'cegis.{blockname}')
        log = open('log', 'w')
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        sql = cegis.c_cegis(block)
        if sql:
            w.write(str(sql))
        # w.close()
        sys.exit(0)
    else:
        w.close()
        LOG.info(f"----------------------Synthesizing block {blockname} in pid[{pid}]----------------------")
        LOG.debug(f"\n{pprint.pformat(block)}")
        return pid, r

def dfg_done(dfg, partial_results):
    for bid in dfg['blocks']:
        block_from = str(dfg['blocks'][bid].get('from', {bid}))
        if block_from not in partial_results:
            return False
        if partial_results[block_from] == '':
            return False
    return True


def get_composed_sql(dfg, partial_results):
    for bid in dfg['blocks']:
        block = dfg['blocks'][bid]
        block_from = str(block.get('from', {bid}))
        block['sql'] = partial_results[block_from]
        assert block['sql'] != ''
    sqls = list(compose_sql(dfg, dfg['blocks'][i]) for i in dfg['returns'])
    if len(dfg["returns"]) == 1:
        sql = sqls[0]
    else:
        sql = f"row({', '.join(sqls)})"

    print(partial_results)
    LOG.info(f"Composed SQL: {sql}")
    return sql

def kill_all_subprocess(pids):
    for pid in pids:
        os.kill(pid, signal.SIGKILL)
    for pid in pids:
        os.wait()

def synth_mp(udf, refinement=True, debug=False):
    return synth(udf, refinement, debug, mp=True)
    assert refinement
    np = os.cpu_count()

    FORMAT = '%(asctime)-15s %(levelname)s %(message)s'
    logging.basicConfig(format=FORMAT, level=logging.DEBUG if debug else logging.INFO)
    LOG.info("Constructing DFG")
    dfg = gen_dfg(udf)

    pending_dfg = [dfg]
    nodes_iter = enumerate_dfg_nodes(pending_dfg)
    running_dfg = {} # id(dfg) -> (dfg, {bid})
    failed_dfg = set()
    running_block = {} # block_from -> {(id(dfg), bid)}
    partial_results = {} # block_from -> sql
    pids = {} # pid -> (r, block_from)

    while True:
        if len(pids) < np:
            dfg, bid = next(nodes_iter, (None, None))
            if dfg is None and len(pending_dfg) > 0:
                nodes_iter = enumerate_dfg_nodes(pending_dfg)
                dfg, bid = next(nodes_iter, (None, None))
            
            if dfg is not None:
                if id(dfg) in failed_dfg:
                    continue

                if id(dfg) not in running_dfg:
                    running_dfg[id(dfg)] = (dfg, set())

                block_from = str(dfg['blocks'][bid].get('from', {bid}))
                if block_from in partial_results:
                    if partial_results[block_from]:
                        if len(running_dfg[id(dfg)][1]) == 0 and dfg_done(dfg, partial_results):
                            kill_all_subprocess(pids)
                            return get_composed_sql(dfg, partial_results)
                    else:
                        LOG.info(f"block {block_from} failed before, merging dfg")
                        merged_dfgs = merge_dfg_mp(dfg, bid=bid)
                        pending_dfg = pending_dfg + merged_dfgs
                        del running_dfg[id(dfg)]
                        failed_dfg.add(id(dfg))
                    continue

                running_dfg[id(dfg)][1].add(bid)
                if block_from in running_block:
                    running_block[block_from].add((id(dfg), bid))
                    continue

                pid, r = cegis_mp(dfg, bid, debug)
                pids[pid] = (r, block_from)
                running_block[block_from] = {(id(dfg), bid)}
                continue

        if len(pids) == 0:
            LOG.info("No process running, failed to syntheis with refinement")
            return

        LOG.info(f"waiting child processes {str({k: v[1] for k,v in pids.items()})}")
        pid, status = os.wait()
        assert status == 0
        r, block_from = pids[pid]
        del pids[pid]

        sql = r.read()
        partial_results[block_from] = sql
        if sql:
            for dfgid, bid in running_block[block_from]:
                if dfgid in running_dfg:
                    dfg = running_dfg[dfgid][0]
                    running_dfg[dfgid][1].remove(bid)
                    if len(running_dfg[dfgid][1]) == 0 and dfg_done(dfg, partial_results):
                        kill_all_subprocess(pids)
                        return get_composed_sql(dfg, partial_results)
            del running_block[block_from]
        else:
            for dfgid, bid in running_block[block_from]:
                dfg = running_dfg[dfgid][0]
                if dfgid in running_dfg:
                    LOG.info(f"block {block_from} failed, merging dfg")
                    merged_dfgs = merge_dfg_mp(dfg, bid=bid)
                    pending_dfg = pending_dfg + merged_dfgs
                    del running_dfg[dfgid]
                failed_dfg.add(dfgid)
            del running_block[block_from]


if __name__ == '__main__':
    FORMAT = '%(asctime)-15s %(levelname)s %(message)s'
    logging.basicConfig(format=FORMAT, level=logging.DEBUG)
    # def udf(x: (Int, Int, Int)) = {
    #    val t1 = x._1 + x._2
    #    return (t1+x._3, t1+x._3)
    #}
    dfg = {
        "args": ["Int", "Int", "Int"], # type list of udf args
        "blocks": {
            1: {
                "args": [("a", "Int", -1), ("b", "Int", -2)], # (<name>, <type>, <from>), negative number for udf args
                "ret": ("t1", "Int"),
                "body" : "val t1 = a + b",
                # "sql": "add(@param0, @param1)"
            },
            2: {
                "args": [("t1", "Int", 1), ("c", "Int", -3)],
                "ret": ("t2", "Int"),
                "body" : "val t2 = t1 + c",
                # "sql": "add(@param0, @param1)"
            },
            3: {
                "args": [("t1", "Int", 1), ("c", "Int", -3)],
                "ret": ("t3", "Int"),
                "body" : "val t3 = t1 - c",
                # "sql": "sub(@param0, @param1)"
            }
        },
        "returns": [2, 3],
    }

    print(comp_synth(dfg))
