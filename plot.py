"""Read results/trials.csv and draw the tradeoff chart.

Three panels against N (log scale):
  1. throughput while healthy
  2. requests lost per failover (mean and max), with N/2 and N-1 reference lines
  3. takeover time as seen by the backup, every trial plotted, so both modes show

Also computes the Young (1974) optimal interval from measured C and M and draws it.
"""
import csv
import math
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import os

def load(path):
    healthy, trials = {}, defaultdict(list)
    if not os.path.exists(path):
        return healthy, trials
    with open(path) as f:
        for row in csv.reader(f):
            if row[0] == "healthy":
                healthy[int(row[1])] = int(row[2])
            elif row[0] == "trial":
                n, t, lost, take, rps, rc = row[1:]
                trials[int(n)].append((int(lost), None if take == "None" else float(take), int(rc)))
    return healthy, trials

healthy, trials = load("results/trials.csv")                  # version 2: checkpoint only
healthy_r, trials_r = load("results/trials_replay.csv")       # version 3a: client replays
healthy_d, trials_d = load("results/trials_durable.csv")      # version 3b: durable log

Ns = sorted(healthy)
tp = [healthy[n] for n in Ns]
mean_lost = [sum(x[0] for x in trials[n]) / len(trials[n]) for n in Ns]
max_lost = [max(x[0] for x in trials[n]) for n in Ns]

# ---- Young 1974 / Daly 2006: T* = sqrt(2 C M), converted to requests ----
# L = per-request latency with checkpointing amortised away (largest N)
# C = extra latency per request at N=1 minus L  (the cost of one checkpoint)
# M = mean time between kills in the sweep (kill at uniform(1.0, 2.5) s after start)
L = 1.0 / healthy[max(Ns)]
C = 1.0 / healthy[min(Ns)] - L
M = 1.75
T_star = math.sqrt(2 * C * M)
N_star = T_star / L

fig, axes = plt.subplots(3, 1, figsize=(8, 11), sharex=True)
fig.suptitle("Process pair (Gray 1985): what the checkpoint interval N costs", fontsize=13)

ax = axes[0]
ax.plot(Ns, tp, "o-", color="#1f77b4", label="checkpoint only (v2)")
if healthy_r:
    Nr = sorted(healthy_r)
    ax.plot(Nr, [healthy_r[n] for n in Nr], "^-", color="#ff7f0e", alpha=0.8, label="+ client replay (v3a)")
if healthy_d:
    Nd = sorted(healthy_d)
    ax.plot(Nd, [healthy_d[n] for n in Nd], "v-", color="#9467bd", alpha=0.8, label="+ durable log, F_FULLFSYNC (v3b)")
    ax.text(Nd[0], healthy_d[Nd[0]] + max(tp) * 0.03, f"{healthy_d[Nd[0]]} req/s", fontsize=8, color="#9467bd")
ax.legend(fontsize=8, loc="center right")
ax.set_ylabel("requests / second (no failures)")
ax.set_ylim(0, max(tp) * 1.1)
ax.axvline(N_star, color="gray", ls="--", lw=1)
ax.text(N_star * 1.1, max(tp) * 0.15, f"Young/Daly N* ≈ {N_star:.0f}\n(sqrt(2CM)/L)", fontsize=8, color="gray")
ax.grid(alpha=0.3)

ax = axes[1]
ax.plot(Ns, mean_lost, "o-", color="#d62728", label="mean lost per failover")
ax.plot(Ns, max_lost, "s--", color="#d62728", alpha=0.5, label="max lost")
ax.plot(Ns, [n / 2 for n in Ns], ":", color="gray", label="N/2 (expected if kills are uniform)")
ax.plot(Ns, [n - 1 for n in Ns], ":", color="black", alpha=0.4, label="N-1 (worst case)")
for lab, tr, mk, col in (("+ client replay (v3a)", trials_r, "^", "#ff7f0e"), ("+ durable log (v3b)", trials_d, "v", "#9467bd")):
    if tr:
        Nx = sorted(tr)
        ax.plot(Nx, [max(x[0] for x in tr[n]) for n in Nx], mk + "-", color=col, alpha=0.8, label=f"{lab}, max lost")
ax.set_ylabel("requests lost at failover")
ax.set_yscale("symlog")
ax.axvline(N_star, color="gray", ls="--", lw=1)
ax.legend(fontsize=8)
ax.grid(alpha=0.3)

ax = axes[2]
for n in Ns:
    takes = [x[1] for x in trials[n] if x[1] is not None]
    ax.scatter([n] * len(takes), takes, s=12, alpha=0.5, color="#2ca02c")
ax.set_ylabel("takeover time, seconds")
ax.set_xlabel("N (checkpoint every N requests)")
ax.set_xscale("log")
ax.axhline(0.1, color="gray", ls=":", lw=1)
ax.text(Ns[0], 0.105, "heartbeat timeout (0.1 s): kernel close did not arrive first", fontsize=7, color="gray")
ax.set_ylim(-0.005, 0.13)
ax.grid(alpha=0.3)

plt.tight_layout(rect=(0, 0, 1, 0.97))
plt.savefig("results/tradeoff.png", dpi=160)
print(f"L={L*1e6:.1f}us  C={C*1e6:.1f}us  M={M}s  T*={T_star*1e3:.2f}ms  N*={N_star:.0f}")
print("wrote results/tradeoff.png")
