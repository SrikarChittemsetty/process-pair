# The post (draft for sign-off)

## Recording

    cd ~/projects/process-pair && ./demo.sh 10

Record the terminal for the ~8 seconds around the kill. The count ticks by 2000s, then
">>> kill -9 primary", then "taking over with last_seq=...", the client reconnects, prints
"lost 6 requests" once, and the count keeps going. Crop to that window. asciinema or a plain
screen recording both work; keep the font big.

## Tweet (one post, clip + chart attached)

Rebuilt Jim Gray's 1985 process pair and killed it 200 times.

Primary checkpoints to a backup every N requests. Backup takes over when heartbeats stop. Client resends by sequence number.

Gray said N is a tradeoff and never measured it. Chart attached.

Checkpointing every request costs 21% throughput. Knee is N≈10. Lost work is exactly N/2.

The 1974 checkpoint formula (Young/Daly) says N*=192. It's 20x high because it prices lost work as rework. A process pair never recomputes what it lost.

Takeover time had nothing to do with N. Half the time the client reconnected into the dead primary's listen queue and sat on a connection nobody was behind.

Paper: jimgray.azurewebsites.net/papers/TandemTR85.7_WhyDoComputersStop.pdf
Code + data: github.com/SrikarChittemsetty/process-pair

## If it has to be shorter (one tweet, no thread)

Rebuilt Jim Gray's 1985 process pair and killed it 200 times to measure the one tradeoff he asserted and never tested.

Checkpoint every request: -21% throughput. Knee at N≈10. Lost work = N/2 exactly. The 1974 formula says N=192, 20x too high, because it assumes lost work gets recomputed.

Code, data, chart: github.com/SrikarChittemsetty/process-pair
