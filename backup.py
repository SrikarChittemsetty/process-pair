import socket
import time

PORT = 5000          # we take this over when the primary dies
BACKUP_PORT = 5001   # we listen here for the primary's heartbeats and checkpoints
HB_TIMEOUT = 0.1     # seconds of silence before we declare the primary dead

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
        conn.sendall(f"{seq} {total}\n".encode())

conn.close()
server.close()
