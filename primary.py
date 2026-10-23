import fcntl
import os
import socket
import sys
import threading
import time

PORT = int(os.environ.get("PP_PORT", "5100"))          # clients connect here
BACKUP_PORT = int(os.environ.get("PP_BACKUP_PORT", "5101"))   # backup listens here for heartbeats and checkpoints
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10   # checkpoint every N requests
HB_INTERVAL = 0.02   # seconds between heartbeats
HB_TIMEOUT = float(os.environ.get("PP_HB_TIMEOUT", "0.1"))   # the backup takes over after this much silence
MARGIN = HB_TIMEOUT * 0.3   # our lease runs out this long before the backup may take over
LEASE = HB_TIMEOUT - MARGIN   # how long one acked heartbeat lets us keep serving
MODE = os.environ.get("PP_MODE", "none")   # none | replay | durable (see README)
LOG_PATH = "results/durable.log"

last_seq = 0
total = 0
ckpt_seq = 0   # last seq the backup has been told about

# The lease. Every heartbeat is stamped with the time we sent it. When the backup acks one,
# we may serve until that send time plus LEASE. The backup can only ack a heartbeat after it
# got it, and it waits a full HB_TIMEOUT of silence after that before it takes over. So our
# lease always runs out at least MARGIN before the backup can take over, even if we were
# frozen and the ack sat in a buffer the whole time.
epoch = 1   # our term. The backup bumps it when it takes over, and anything older is stale.
lease_until = 0.0
send_lock = threading.Lock()   # two threads write to the backup socket

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

server = conn = None
leaving = threading.Lock()

def step_down(why):
    # Someone else may be serving now, or soon will be. Let go of the client port so the
    # backup can bind it, and leave without answering anything. A clean exit, not a crash.
    leaving.acquire()   # only the first caller gets past here, the process ends before the next
    print(f"stepping down: {why}", flush=True)
    for s in (server, conn, backup):
        try:
            if s:
                s.close()
        except OSError:
            pass
    os._exit(0)

def to_backup(msg):
    try:
        with send_lock:
            backup.sendall(msg.encode())
    except OSError:
        step_down("can't reach the backup")

# heartbeat thread: tells the backup we are alive, every HB_INTERVAL
def heartbeat():
    while True:
        to_backup(f"HB {epoch} {time.monotonic()!r}\n")
        time.sleep(HB_INTERVAL)

# reads acks from the backup. An ack renews the lease. A higher epoch or a closed
# connection means the backup has taken over, so we stop right away.
def listen_to_backup():
    global lease_until
    buf = b""
    while True:
        try:
            data = backup.recv(4096)
        except OSError:
            data = b""
        if not data:
            step_down("the backup closed the connection")
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            parts = line.split()
            if int(parts[1]) > epoch:
                step_down(f"the backup is on epoch {int(parts[1])} and we are on {epoch}")
            if parts[0] == b"ACK":
                lease_until = max(lease_until, float(parts[2]) + LEASE)

def lease_ok():
    # Usual case: the lease is still good. If not, we were slow or frozen. Ask for a fresh
    # ack and wait a little. If the backup took over, listen_to_backup ends us first.
    if time.monotonic() < lease_until:
        return True
    to_backup(f"HB {epoch} {time.monotonic()!r}\n")
    give_up = time.monotonic() + HB_TIMEOUT
    while time.monotonic() < give_up:
        if time.monotonic() < lease_until:
            return True
        time.sleep(0.001)
    return False

threading.Thread(target=heartbeat, daemon=True).start()
threading.Thread(target=listen_to_backup, daemon=True).start()

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
            to_backup(f"CKPT {epoch} {last_seq} {total}\n")
            ckpt_seq = last_seq
        # no lease, no answer. If the backup might have taken over, our state is stale.
        if not lease_ok():
            step_down("the lease ran out and the backup didn't renew it")
        # reply carries ckpt_seq so a replaying client knows what it may forget
        conn.sendall(f"{seq} {total} {ckpt_seq}\n".encode())

conn.close()
server.close()
