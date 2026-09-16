import os
import socket
import sys
import time

PORT = 5000
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
VERBOSE = int(os.environ.get("PP_VERBOSE", "0"))   # print every Nth reply (for the demo)

def connect():
    # keep trying until someone is listening on 5000 (primary, or backup after takeover)
    while True:
        try:
            s = socket.socket()
            s.connect(("127.0.0.1", PORT))
            s.settimeout(0.5)   # a connection that never answers is as dead as no connection
            return s, s.makefile("rw")
        except ConnectionRefusedError:
            time.sleep(0.005)

sock, f = connect()
seq = 1
gap = 0   # how far total is behind seq; only print when it changes
start = time.time()
while seq <= COUNT:
    try:
        f.write(f"{seq} 1\n")
        f.flush()
        reply = f.readline()
        if not reply:
            raise ConnectionError("server closed")
    except (ConnectionError, BrokenPipeError, OSError, socket.timeout):
        # server died mid-request. Reconnect and resend the SAME seq.
        t0 = time.time()
        sock, f = connect()
        print(f"[client] reconnected after {time.time() - t0:.4f}s, resending seq {seq}")
        continue
    r_seq, r_total = map(int, reply.split())
    if VERBOSE and r_seq % VERBOSE == 0:
        print(f"{time.strftime('%H:%M:%S')}  seq {r_seq:>7}  total {r_total:>7}", flush=True)
    if r_seq - r_total != gap:
        # with value=1 every time, total should equal seq. The gap is lost work.
        gap = r_seq - r_total
        print(f"[client] seq {r_seq}: total={r_total}, lost {gap} requests")
    seq += 1

elapsed = time.time() - start
print(f"[client] done: {COUNT} requests in {elapsed:.3f}s = {COUNT/elapsed:.0f} req/s")
sock.close()
