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

## Property tests

The crash tests are also written as property tests, with Antithesis's Hegel for Go, plus a
Hypothesis version. Both are the same test. `tests/hegel/process_pair_test.go` uses Hegel's
state machine support (`hegel.RunStateful`) and `tests/test_properties.py` uses Hypothesis
(`RuleBasedStateMachine`). Hegel has no Python library yet, but the test drives the real
`primary.py` and `backup.py` as separate processes anyway, so the harness language doesn't matter.

The library picks the schedule: the checkpoint interval N (1 to 12), how many requests to send,
when to `kill -9` the primary, and whether the kill lands between requests or while one is still
on the wire. The client side is a small harness that speaks the same protocol as `client.py` and
handles failures the same way, because `client.py` is one fixed loop the test can't step through.

Request k carries the value 2^(k-1), so the server's total says exactly which requests it has
applied. After every reply the test checks three things:

- (a) nothing acked is lost: every request the client saw answered is in the total
- (b) nothing is applied twice: the total is never more than the sum of everything sent
- (c) the total the client sees never goes backwards

It also fails if nobody answers on the client port for a few seconds, because a pair that's down
is losing everything.

Run the Hegel version (needs Go, macOS arm64 or Linux. `go test` pulls Hegel and its engine is
bundled, nothing else to install):

    cd tests/hegel
    go test ./... -v                                  # checkpoint only
    PP_MODE=replay  go test ./... -v
    PP_MODE=durable go test ./... -v
    PP_STALL=1 PP_MODE=replay go test ./... -v        # also pause the primary

Run the Hypothesis version:

    pip install hypothesis pytest
    python3 -m pytest tests/test_properties.py                      # checkpoint only
    PP_MODE=replay  python3 -m pytest tests/test_properties.py
    PP_MODE=durable python3 -m pytest tests/test_properties.py
    PP_STALL=1 PP_MODE=replay python3 -m pytest tests/test_properties.py   # also pause the primary

`PP_EXAMPLES` sets the number of test cases in both (default 50). `HEGEL_STATISTICS=1` prints how
often each kind of case came up. Ports start at 5200 and skip anything already listening.
`primary.py` and `backup.py` now read `PP_PORT`, `PP_BACKUP_PORT` and `PP_HB_TIMEOUT` so the tests
can use their own ports. The defaults are unchanged.

What they found:

**Checkpoint only, it breaks right away.** Hegel fails every run and shrinks to the same two
steps every time. Four fresh runs, 72 to 128 seconds each, most of it shrinking:

    n := hegel.Draw(tc, hegel.Integers(1, 12)) = 2
    Rule: RuleKillPrimaryMidRequest
    delay := hegel.Draw(tc, hegel.SampledFrom([]float64{0, 0.0002, 0.001, 0.005})) = 0.0002
    Rule: RuleSend
    k := hegel.Draw(tc, hegel.Integers(1, 25)) = 1
    failure in worker 0: seq 2: acked requests [1] are missing from the state (N=2, mode=none, reconnects=1)

N=2, send request 1 and kill the primary 0.2 ms later, just after it answered, then send one more.
Request 1 was answered but never checkpointed, so the backup never heard of it. A delay of 0
doesn't work because the primary dies before it answers, and then the client just resends.
Hypothesis fails within the first few dozen examples and usually shrinks to a three step version
of the same thing:

    AssertionError: seq 2: acked requests [1] are missing from the state (N=2, mode=none, reconnects=1)
    state = ProcessPair()
    state.start_pair(n=2)
    state.send(k=1)
    state.kill_primary()
    state.send(k=1)
    state.teardown()

On one run it found Hegel's two step version instead. Either way it's the N/2 lost work from
RESULTS.md section 2, boiled down to the smallest case.

**Replay holds.** Hegel: 1,000 test cases, 0 failures, in 43 seconds. 85% of them include a
failover. Hypothesis: 1,000 examples, 0 failures, in 75 seconds.

**Durable holds.** Hegel: 1,000 test cases, 0 failures, in 106 seconds. Hypothesis: 1,000
examples, 0 failures, in 152 seconds.

**A pause takes down the whole pair.** With `PP_STALL=1` the test can `SIGSTOP` the primary for a
moment and then let it go, like a GC pause or a laptop lid. Both libraries find this every run.
Hegel shrinks to:

    n := hegel.Draw(tc, hegel.Integers(1, 12)) = 1
    Rule: RuleStallPrimary
    pause := hegel.Draw(tc, hegel.SampledFrom([]float64{0.05, 0.3})) = 0.3
    Rule: RuleKillPrimaryMidRequest
    delay := hegel.Draw(tc, hegel.SampledFrom([]float64{0, 0.0002, 0.001, 0.005})) = 0.001
    failure in worker 0: seq 1: nobody listening on 5244 for 1s, the pair is down

Every failing case here takes over a second, so Hegel stopped shrinking at its 300 second limit and
reported the smallest case so far. Re-running picked it up from its example database and failed
again in 4 seconds. Hypothesis shrinks to almost the same thing:

    AssertionError: seq 1: nobody listening on 5276 for 1.0s, the pair is down
    state = ProcessPair()
    state.start_pair(n=1)
    state.stall_primary(pause=0.3)
    state.kill_primary_mid_request(delay=0)
    state.teardown()

The kill isn't needed. An earlier Hegel run shrank to a 0.3 s pause and then plain requests, and
by hand a pause and one request is enough. Here's what happens. The backup hears no heartbeat for
0.1 s and decides the primary is dead. It tries to take port 5100, but the primary is still holding
it, so `bind` fails with "Address already in use" and the backup crashes. The primary wakes up,
sends its next checkpoint to a backup that's gone, gets a broken pipe and crashes too. Nothing was
killed and both processes are dead. I swept the pause by hand, five tries each: 0.05, 0.08 and
0.1 s were fine every time, 0.12, 0.15 and 0.2 s took the pair down every time. With a bigger N the
primary keeps going until its next checkpoint, so for a while it looks healthy with no backup at
all. This is the false failure detection problem. A slow process looks exactly like a dead one,
and the takeover path assumes dead means gone.

Two more things the tests made obvious:

- The pair survives one failover, not more. After the backup takes over it never gets a backup of
  its own, and `primary.py` always starts from zero, so there's no way to rejoin. A second crash
  is a total outage. So "after any number of failovers" really means after one.
- The first version of the Hypothesis test cycled through ports 5200 to 5600 and hit 5432, where
  OrbStack forwards Postgres. After a kill the client reconnected to OrbStack, which accepted and
  never answered, and the test hung until it was killed by hand. Same thing as AirPlay on port 5000
  (RESULTS.md section 3). Both tests now skip ports that are already taken and fail instead of
  hanging.

## Run it

    ./demo.sh                          # one failover, watch the count survive a kill -9
    PP_MODE=replay ./demo.sh           # same, with client replay: nothing lost
    python3 sweep.py                   # the full experiment (~15 min)
    PP_MODE=replay python3 sweep.py    # again with client replay
    PP_MODE=durable python3 sweep.py 5 1 10 100 1000   # durable log, slow, fewer trials
    python3 plot.py                    # results/tradeoff.png

Don't run it on port 5000 on a Mac. See RESULTS.md section 3.

Paper: Gray, "Why Do Computers Stop and What Can Be Done About It?", Tandem TR 85.7, 1985.
