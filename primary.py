import fcntl
import os
import socket
import sys
import threading
import time

PORT = 5100          # clients connect here
BACKUP_PORT = 5101   # backup listens here for heartbeats and checkpoints
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10   # checkpoint every N requests
HB_INTERVAL = 0.02   # seconds between heartbeats
MODE = os.environ.get("PP_MODE", "none")   # none | replay | durable (see README)
LOG_PATH = "results/durable.log"

last_seq = 0
total = 0
ckpt_seq = 0   # last seq the backup has been told about

# durable mode: every request is written and forced to disk before we reply.
# F_FULLFSYNC because on macOS a plain fsync() does not wait for the disk.
if MODE == "durable":
    os.makedirs("results", exist_ok=True)
    log = open(LOG_PATH, "w")

# connect to the backup first; keep trying until it is up
backup = socket.socket()
while True:
    try:
        backup.connect(("127.0.0.1", BACKUP_PORT))
        break
    except ConnectionRefusedError:
        time.sleep(0.05)
print(f"primary connected to backup on {BACKUP_PORT}, N={N}")

# heartbeat thread: tells the backup we are alive, every HB_INTERVAL
def heartbeat():
    while True:
        try:
            backup.sendall(b"HB\n")
        except OSError:
            return
        time.sleep(HB_INTERVAL)

threading.Thread(target=heartbeat, daemon=True).start()

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", PORT))
server.listen(1)
print(f"primary listening on {PORT}")

conn, addr = server.accept()
print(f"client connected from {addr}")

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
        total += value
        last_seq = seq
        if MODE == "durable":
            log.write(f"{seq} {value}\n")
            log.flush()
            fcntl.fcntl(log.fileno(), fcntl.F_FULLFSYNC)
        # the knob: every N requests, copy our state to the backup BEFORE replying
        if seq % N == 0:
            backup.sendall(f"CKPT {last_seq} {total}\n".encode())
            ckpt_seq = last_seq
        # reply carries ckpt_seq so a replaying client knows what it may forget
        conn.sendall(f"{seq} {total} {ckpt_seq}\n".encode())

conn.close()
server.close()
