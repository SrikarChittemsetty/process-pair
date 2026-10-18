# process-pair

A process pair, from Jim Gray's 1985 paper, with a checkpoint-interval knob. I vary the knob
and measure tradeoffs. 

## What it is

Two copies of the same little server on my laptop. The primary sits on port 5100 and answers
requests from a client. Every request has a sequence number. The backup sits on port 5101 and
gets two things from the primary: a heartbeat every few ms, and every N requests, a copy of the
primary's state (a checkpoint). If the heartbeats stop, the backup decides the primary is dead,
grabs port 5100, and starts answering from whatever checkpoint it last got. The client notices
its connection died, reconnects, and resends the request it was waiting on. The backup checks
the sequence number so it doesn't do that request twice.

Gray had this running across two processor boards in a Tandem machine. I have one laptop, so
`kill -9` is the equivalent of a board failure.

## The experiment

N is the knob. Checkpoint on every request and the backup is never behind, but every request
has to wait on a message to the backup first. Checkpoint every 1000 and requests are fast, but
if the primary dies at request 999 the backup is 999 requests behind.

I'll run N = 1, 10, 100, 1000. For each: stream requests, kill the primary at random a few
hundred times, let the backup take over, spin up a new backup, repeat. Three numbers per N:

- requests/sec while nothing is broken
- how many requests the backup didn't know about after each kill
- time from kill to the backup's first reply

Then plot them against N.

## What I think will happen

Throughput goes up with N and flattens out. Lost work goes up with N, probably close to linear.
Takeover time shouldn't care about N at all, it's just the heartbeat timeout. Somewhere there's
a point of diminishing marginal return where checkpointing more often stops being worth it.

There's a formula for this from supercomputing (Young 1974, Daly 2006): the best checkpoint
interval is roughly sqrt(2CM), C being what a checkpoint costs and M being how long the machine
lasts between failures. That's for saving to disk, not a live backup, so I don't know if it
holds here. My hypothesis is the shape holds but the number is off, because their formula assumes lost work gets redone and mine just loses it. I expect it to predict a smaller N than I measure.

## Results

Done. See [RESULTS.md](RESULTS.md). Short version: checkpointing every request costs 25% throughput,
the knee is between N=5 and 10, lost work is about N/2, takeover time has nothing to do with N, and the
1974 formula lands 20x too high because it prices lost work as rework. Then I built Gray's two fixes:
client replay closes the hole for free, a durable log closes it too but costs 99% of throughput, because a real disk flush is 2.9 ms. Also, the first
version of the takeover finding was wrong, because macOS AirPlay listens on port 5000. Section 3.

## Run it

    ./demo.sh                          # one failover, watch the count survive a kill -9
    PP_MODE=replay ./demo.sh           # same, with client replay: nothing lost
    python3 sweep.py                   # the full experiment (~15 min)
    PP_MODE=replay python3 sweep.py    # again with client replay
    PP_MODE=durable python3 sweep.py 5 1 10 100 1000   # durable log, slow, fewer trials
    python3 plot.py                    # results/tradeoff.png

Don't run it on port 5000 on a Mac. See RESULTS.md section 3.

Paper: Gray, "Why Do Computers Stop and What Can Be Done About It?", Tandem TR 85.7, 1985.
