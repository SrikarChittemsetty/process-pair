import os
import socket
import time

PORT = 5100          # we take this over when the primary dies
BACKUP_PORT = 5101   # we listen here for the primary's heartbeats and checkpoints
HB_TIMEOUT = 0.1     # seconds of silence before we declare the primary dead
MODE = os.environ.get("PP_MODE", "none")
LOG_PATH = "results/durable.log"

last_seq = 0
total = 0

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", BACKUP_PORT))
listener.listen(1)
print(f"backup listening for primary on {BACKUP_PORT}")

primary, _ = listener.accept()
primary.settimeout(HB_TIMEOUT)   # recv() raises if nothing arrives for HB_TIMEOUT
print("primary connected")

# phase 1: follow the primary. Every line is either HB or CKPT last_seq total.
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
        print(f"no heartbeat for {HB_TIMEOUT}s, primary is dead")
        break
    buf += data
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        parts = line.split()
        if parts[0] == b"CKPT":
            last_seq, total = int(parts[1]), int(parts[2])

# phase 2: take over. Bind the client port and serve from the last checkpoint.
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
print(f"taking over with last_seq={last_seq} total={total}")
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", PORT))
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
