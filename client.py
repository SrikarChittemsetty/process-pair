import os
import socket
import sys
import time

PORT = 5100
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
VERBOSE = int(os.environ.get("PP_VERBOSE", "0"))   # print every Nth reply (for the demo)
MODE = os.environ.get("PP_MODE", "none")

def connect():
    # keep trying until someone is listening on 5100 (primary, or backup after takeover)
    while True:
        try:
            s = socket.socket()
            s.connect(("127.0.0.1", PORT))
            s.settimeout(0.5)   # a connection that never answers is as dead as no connection
            return s, s.makefile("rw")
        except ConnectionRefusedError:
            time.sleep(0.005)

def ask(f, seq):
    """Send one request and return the parsed reply. Raises on any socket trouble."""
    f.write(f"{seq} 1\n")
    f.flush()
    reply = f.readline()
    if not reply:
        raise ConnectionError("server closed")
    r_seq, r_total, r_ckpt = map(int, reply.split())
    return r_seq, r_total, r_ckpt

sock, f = connect()
seq = 1
gap = 0   # how far total is behind seq; only print when it changes
pending = []   # replay mode: every seq the server has answered but not yet checkpointed
replayed = 0
start = time.time()
while seq <= COUNT:
    try:
        r_seq, r_total, r_ckpt = ask(f, seq)
    except (ConnectionError, BrokenPipeError, OSError, socket.timeout):
        # server died mid-request. Reconnect and resend the SAME seq.
        t0 = time.time()
        sock, f = connect()
        print(f"[client] reconnected after {time.time() - t0:.4f}s, resending seq {seq}")
        if MODE == "replay":
            # Gray's "requester resends": replay everything the backup may not have.
            # The backup dedups by seq, so replaying too much is safe; too little is loss.
            try:
                for old in pending:
                    ask(f, old)
                replayed += len(pending)
                print(f"[client] replayed {len(pending)} requests")
            except (ConnectionError, BrokenPipeError, OSError, socket.timeout):
                pass   # died again mid-replay; the outer loop will reconnect and retry
        continue
    if MODE == "replay":
        # keep only what the primary has not yet handed to the backup.
        # pending is ascending and ckpt only moves forward, so trim from the front.
        pending.append(r_seq)
        while pending and pending[0] <= r_ckpt:
            pending.pop(0)
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
