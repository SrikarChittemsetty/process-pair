"""Run the process pair at several checkpoint intervals N, kill the primary at random,
and record per trial: throughput while healthy, requests lost at failover, takeover time.

usage: python3 sweep.py [trials_per_N] [N values...]
"""
import random
import re
import signal
import subprocess
import sys
import time

TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
NS = [int(x) for x in sys.argv[2:]] or [1, 10, 100, 1000]
CLIENT_COUNT = 400000            # enough to still be running when we kill
KILL_AFTER = (1.5, 3.0)          # seconds after start, uniform random

def run(cmd):
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

def trial(n):
    b = run(["python3", "-u", "backup.py"]); time.sleep(0.3)
    p = run(["python3", "-u", "primary.py", str(n)]); time.sleep(0.3)
    c = run(["python3", "-u", "client.py", str(CLIENT_COUNT)])
    time.sleep(random.uniform(*KILL_AFTER))
    p.send_signal(signal.SIGKILL)
    try:
        c_out, _ = c.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        c.kill(); c_out, _ = c.communicate()
    b.kill(); b_out, _ = b.communicate()

    lost = re.search(r"lost (\d+) requests", c_out)
    take = re.search(r"takeover took ([\d.]+)s", b_out)
    rps = re.search(r"= (\d+) req/s", c_out)
    reconnects = len(re.findall(r"\[client\] reconnected", c_out))
    return (int(lost.group(1)) if lost else 0,
            float(take.group(1)) if take else None,
            int(rps.group(1)) if rps else None,
            reconnects)

def healthy_throughput(n):
    """Throughput with no kill at all, so it is not polluted by the failover."""
    b = run(["python3", "-u", "backup.py"]); time.sleep(0.3)
    p = run(["python3", "-u", "primary.py", str(n)]); time.sleep(0.3)
    c = run(["python3", "-u", "client.py", "200000"])
    c_out, _ = c.communicate(timeout=120)
    p.kill(); b.kill(); b.communicate(); p.communicate()
    m = re.search(r"= (\d+) req/s", c_out)
    return int(m.group(1)) if m else None

import os
os.makedirs("results", exist_ok=True)
csv = open("results/trials.csv", "a")
print("N,trial,lost,takeover_s,req_s_during_run,reconnects")
results = {}
for n in NS:
    tp = healthy_throughput(n)
    csv.write(f"healthy,{n},{tp}\n"); csv.flush()
    rows = []
    for t in range(TRIALS):
        lost, take, rps, rc = trial(n)
        rows.append((lost, take))
        line = f"{n},{t},{lost},{take},{rps},{rc}"
        print(line, flush=True); csv.write("trial,"+line+"\n"); csv.flush()
    losts = [r[0] for r in rows]
    takes = [r[1] for r in rows if r[1] is not None]
    results[n] = (tp, sum(losts)/len(losts), max(losts), sum(takes)/len(takes) if takes else None)

print("\nN,healthy_req_s,mean_lost,max_lost,mean_takeover_s")
for n, (tp, ml, mx, mt) in results.items():
    print(f"{n},{tp},{ml:.2f},{mx},{mt if mt is None else round(mt,4)}")
