# Results

200 failovers. One laptop (M-series MacBook Air), Python 3.9, all three processes on localhost.
For each N in {1, 2, 5, 10, 20, 50, 100, 200, 500, 1000}: 20 trials, primary killed with `kill -9`
at a uniform random time 1.0 to 2.5 s into a 150,000-request run. Throughput measured separately with
no kill. Raw data in `results/trials.csv`, chart in `results/tradeoff.png`, produced by `plot.py`.

![tradeoff](results/tradeoff.png)

## 1. What checkpointing costs

| N | req/s (no failures) | vs N=1000 |
|---|---|---|
| 1 | 31,402 | -21% |
| 2 | 35,548 | -11% |
| 5 | 36,625 | -8% |
| 10 | 37,150 | -7% |
| 50 | 37,912 | -5% |
| 100 | 38,153 | -4% |
| 1000 | 39,755 | 0 |

Checkpointing on every request costs about a fifth of throughput. By N=10 you are within 7% of the
ceiling and the curve is flat from there. The knee is between N=5 and N=10.

From these numbers: per-request latency with checkpointing amortised away is L = 25.2 µs, and one
checkpoint (a ~20-byte line to a socket on the same machine) costs C = 6.7 µs.

## 2. What checkpointing rarely costs

Lost work tracks the model exactly. Mean lost per failover is N/2, max is N-1. Kills are uniform in
time, so the primary is on average halfway to its next checkpoint when it dies.

| N | mean lost | max lost |
|---|---|---|
| 1 | 0 | 0 |
| 10 | 6.3 | 9 |
| 100 | 49.6 | 99 |
| 1000 | 434.7 | 925 |

Nothing recovers these. After a failover the backup's `total` runs exactly that many behind the
client's count for the rest of the run. In this design lost work is lost, not delayed.

## 3. Takeover time does not depend on N

It depends on what the client's reconnect hits, and it comes in three modes across the 200 trials:

- **96 trials, under 1 ms.** The backup had already bound port 5000 when the client reconnected.
- **20 trials, about 0.1 s.** The backup detected death by heartbeat timeout rather than by the
  kernel closing the connection, so takeover waited out the 100 ms window.
- **80 trials, about 0.5 s.** The client reconnected before the backup had bound the port, and the
  connection landed in the dead primary's listen queue, which the kernel had not torn down yet.
  Nobody was behind it. The client waited out its 0.5 s read timeout and reconnected again.

The heartbeat interval was 20 ms and the timeout 100 ms, so detection was fast in every case. What
the client experienced was set by its own timeout policy. Gray's paper spends its pages on detection
and takeover; the thing that actually cost us time was a zombie listen queue and a client that trusted
a connection because `connect()` succeeded. That is the gray-failure problem (Huang 2017) in
miniature: the component looks alive to the thing talking to it.

Before the read timeout was added, this bug hung the client forever on the first run.

## 4. Does the 1974 formula predict the knee?

Young (1974) and Daly (2006) give the optimal checkpoint interval for a long computation as
T* = sqrt(2 · C · M), where C is the cost of one checkpoint and M is mean time between failures.
Plugging in the measured C = 6.7 µs and the sweep's M = 1.75 s:

    T* = sqrt(2 · 6.7e-6 · 1.75) = 4.84 ms  →  N* = T* / L = 192 requests

Measured knee: N ≈ 10. The formula says 192.

The formula is not wrong; it is minimising the wrong thing for this system. Young's model assumes
lost work is *recomputed* after a failure, so a lost request costs its own compute time (25 µs). Under
that assumption, checkpointing more often than every 192 requests wastes more time on checkpoints than
it saves on rework, and the data agrees: between N=10 and N=192 you gain about 2% throughput.

But in a process pair the lost requests are never recomputed. They are gone. If a lost request is a
lost order rather than 25 µs of arithmetic, the cost of N=192 is 92 lost requests per failover against
6 at N=10, for 2% more throughput. Nobody takes that trade. The right N for a system whose state
matters is where the throughput curve goes flat, and that is an order of magnitude smaller than the
formula's answer.

So: shape holds, number is off by ~20x, and the reason is the difference between rework and loss.

## 5. The same knob elsewhere

- PostgreSQL `synchronous_commit`: on (wait for the replica) vs off. Same trade, same shape.
- Kafka producer `acks=all` vs `acks=1`.
- Flink checkpoint interval.
- Raft commits after a majority has the entry; the leader's throughput is bounded by that round trip.

Every one of them ships a default someone picked. This is the curve behind the default.

## What's next (version 3)

Close the hole. Two ways, both from Gray: the client keeps every request since the last checkpoint
and replays them on takeover (his "automatic checkpointing", and how Raft's log works), or the primary
writes to something durable before replying (his transactions). Then measure what each costs against
this baseline.

Paper: Gray, "Why Do Computers Stop and What Can Be Done About It?", Tandem TR 85.7, 1985.
Formula: Young, "A first order approximation to the optimum checkpoint interval", CACM 1974;
Daly, "A higher order estimate of the optimum checkpoint interval", FGCS 2006.
