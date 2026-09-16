"""Read results/trials.csv and draw the tradeoff chart.

Three panels against N (log scale):
  1. throughput while healthy
  2. requests lost per failover (mean and max), with N/2 and N-1 reference lines
  3. takeover time as seen by the backup, every trial plotted, so the two modes show

Also computes the Young (1974) optimal interval from measured C and M and draws it.
"""
import csv
import math
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

healthy = {}
trials = defaultdict(list)
with open("results/trials.csv") as f:
    for row in csv.reader(f):
        if row[0] == "healthy":
            healthy[int(row[1])] = int(row[2])
        elif row[0] == "trial":
            n, t, lost, take, rps, rc = row[1:]
            trials[int(n)].append((int(lost), None if take == "None" else float(take), int(rc)))

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
ax.plot(Ns, tp, "o-", color="#1f77b4")
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
ax.axhline(0.5, color="gray", ls=":", lw=1)
ax.text(Ns[0], 0.52, "client read timeout (0.5 s): reconnect hit the dead primary's listen queue first", fontsize=7, color="gray")
ax.axhline(0.1, color="gray", ls=":", lw=1)
ax.text(Ns[0], 0.11, "heartbeat timeout (0.1 s)", fontsize=7, color="gray")
ax.grid(alpha=0.3)

plt.tight_layout(rect=(0, 0, 1, 0.97))
plt.savefig("results/tradeoff.png", dpi=160)
print(f"L={L*1e6:.1f}us  C={C*1e6:.1f}us  M={M}s  T*={T_star*1e3:.2f}ms  N*={N_star:.0f}")
print("wrote results/tradeoff.png")
