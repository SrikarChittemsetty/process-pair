import os
import socket
import time

PORT = int(os.environ.get("PP_PORT", "5100"))          # we take this over when the primary dies
BACKUP_PORT = int(os.environ.get("PP_BACKUP_PORT", "5101"))   # we listen here for the primary's heartbeats and checkpoints
HB_TIMEOUT = float(os.environ.get("PP_HB_TIMEOUT", "0.1"))   # seconds of silence before we declare the primary dead
MODE = os.environ.get("PP_MODE", "none")
LOG_PATH = "results/durable.log"

last_seq = 0
total = 0
epoch = 0   # the primary's term, learned from its messages. We bump it when we take over.

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", BACKUP_PORT))
listener.listen(1)
print(f"backup listening for primary on {BACKUP_PORT}")

primary, _ = listener.accept()
primary.settimeout(HB_TIMEOUT)   # recv() raises if nothing arrives for HB_TIMEOUT
print("primary connected")

# phase 1: follow the primary. Every line is HB epoch time or CKPT epoch last_seq total.
# We ack every heartbeat, and the ack is the primary's lease. The primary's lease runs out
# a margin before our HB_TIMEOUT does, so when we time out it has already stopped serving.
buf = b""
dead_at = None
while True:
    try:
        data = primary.recv(4096)
        if not data:
            dead_at = time.time()
            print("primary closed the connection")
            break
    except socket.timeout:
        dead_at = time.time()
        print(f"no heartbeat for {HB_TIMEOUT}s, primary is dead or its lease ran out")
        break
    except OSError:
        # a reset instead of a clean close, e.g. kill -9 with our acks still unread
        dead_at = time.time()
        print("primary connection reset")
        break
    buf += data
    acks = b""
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        parts = line.split()
        if int(parts[1]) < epoch:
            continue   # from an older term, ignore it
        epoch = int(parts[1])
        if parts[0] == b"HB":
            acks += b"ACK %d %s\n" % (epoch, parts[2])
        elif parts[0] == b"CKPT":
            last_seq, total = int(parts[2]), int(parts[3])
    try:
        if acks:
            primary.sendall(acks)
    except OSError:
        dead_at = time.time()
        print("primary connection broke")
        break

# phase 2: take over. New epoch, so anything the old primary still sends is stale. Tell it,
# in case it's only frozen and wakes up later, then hang up on it.
epoch += 1
try:
    primary.sendall(b"FENCE %d\n" % epoch)
except OSError:
    pass
primary.close()

# durable mode: the primary's log has every request it replied to, so roll forward
# from the checkpoint before serving. This is the recovery half of a transaction log.
if MODE == "durable":
    replayed = 0
    try:
        with open(LOG_PATH) as log:
            for line in log:
                parts = line.split()
                if len(parts) != 2:
                    continue   # torn last line from the crash
                seq, value = int(parts[0]), int(parts[1])
                if seq > last_seq:
                    total += value
                    last_seq = seq
                    replayed += 1
    except FileNotFoundError:
        pass
    print(f"rolled forward {replayed} requests from the log")
print(f"taking over on epoch {epoch} with last_seq={last_seq} total={total}")

# Bind the client port and serve from the last checkpoint. If the old primary is only frozen
# it still holds the port, and it can't let go until it wakes up and sees it's been fenced.
# So keep trying with backoff instead of crashing.
wait = 0.005
while True:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", PORT))
        break
    except OSError:
        server.close()
        print(f"port {PORT} still held by the old primary, trying again in {wait}s")
        time.sleep(wait)
        wait = min(wait * 2, 0.05)
server.listen(1)

conn, addr = server.accept()
print(f"client reconnected from {addr}, takeover took {time.time() - dead_at:.4f}s")

buf = b""
while True:
    data = conn.recv(4096)
    if not data:
        print("client left")
        break
    buf += data
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        seq, value = line.split()
        seq, value = int(seq), int(value)
        if seq > last_seq:
            # new request: apply it
            total += value
            last_seq = seq
        # else: already applied before the crash, do NOT add again. Just answer.
        # We have no backup of our own, so everything we hold counts as checkpointed.
        conn.sendall(f"{seq} {total} {last_seq}\n".encode())

conn.close()
server.close()
