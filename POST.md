# The post (draft for sign-off)

## Recording

    cd ~/projects/process-pair && ./demo.sh 10

Record the terminal for the ~8 seconds around the kill. The count ticks by 2000s, then
">>> kill -9 primary", then "taking over with last_seq=...", the client reconnects, prints
"lost 6 requests" once, and the count keeps going. Crop to that window. asciinema or a plain
screen recording both work; keep the font big.

## Tweet (one post, clip + chart attached)

Rebuilt Jim Gray's 1985 process pair and killed it 420 times.

Primary checkpoints to a backup every N requests. Backup takes over when heartbeats stop. Client resends by sequence number.

Gray said N is a tradeoff and never measured it. Chart attached.

Checkpointing every request costs 25% throughput. Knee is N≈5-10. Lost work is N/2.

The 1974 checkpoint formula (Young/Daly) says N*=215. 20x high, because it prices lost work as rework. A process pair never recomputes what it lost.

Then I built Gray's two fixes. Client keeps and replays unacked requests: 0 lost, 0 cost. Server writes durably before replying: 0 lost, 99% of throughput gone, because a real disk flush on a Mac is 3ms and fsync() lies.

Also: my first takeover finding was wrong. I blamed a zombie listen queue. It was macOS AirPlay on port 5000 accepting my client's connection and saying nothing. Same lesson, connect() succeeding tells you nothing, worse cause.

Paper: jimgray.azurewebsites.net/papers/TandemTR85.7_WhyDoComputersStop.pdf
Code + data: github.com/SrikarChittemsetty/process-pair

## If it has to be shorter

Rebuilt Jim Gray's 1985 process pair and killed it 420 times to measure the tradeoff he asserted and never tested.

Checkpoint every request: -25% throughput. Knee at N≈5-10. Lost work = N/2. The 1974 formula says N=215, 20x too high, because it assumes lost work gets recomputed.

Gray's fixes: client replay closes the hole for free. A durable log closes it for 99% of throughput, because fsync() on macOS doesn't wait for the disk.

Code, data, chart: github.com/SrikarChittemsetty/process-pair
