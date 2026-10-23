"""Property tests for the process pair.

Runs the real primary.py and backup.py as separate processes, kills the primary with
SIGKILL at points Hypothesis picks, and checks three properties on every reply the client
gets back:

  (a) nothing acked is lost: the state includes every request the client saw acknowledged
  (b) nothing is applied twice: sequence-number dedupe holds
  (c) the state the client observes never goes backwards

The client side is a harness that speaks the same protocol as client.py and follows the
same rules: resend the same seq after a reconnect, and in replay mode resend everything
newer than the last checkpoint the server reported. client.py itself is one fixed loop,
so the test can't step it one request at a time.

Request seq k carries value 2**(k-1), so the server's total is a bitmask of exactly which
requests it has applied. A missing bit is a lost request. A total bigger than the sum of
everything sent means something was added twice.

usage:
    pip install hypothesis pytest
    python3 -m pytest tests/test_properties.py              # checkpoint only (PP_MODE unset)
    PP_MODE=replay  python3 -m pytest tests/test_properties.py
    PP_MODE=durable python3 -m pytest tests/test_properties.py
    PP_STALL=1 PP_MODE=replay python3 -m pytest tests/test_properties.py   # also pause the primary

Knobs: PP_EXAMPLES (default 50), PP_TEST_PORT (default 5200, uses a pair of ports per example).
"""
import itertools
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from hypothesis import HealthCheck, event, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, precondition, rule

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODE = os.environ.get("PP_MODE", "none")
STALL = os.environ.get("PP_STALL") == "1"
HB_TIMEOUT = 0.1                      # backup.py's default
BASE_PORT = int(os.environ.get("PP_TEST_PORT", "5200"))
_ports = itertools.cycle(range(BASE_PORT, BASE_PORT + 200, 2))
STARTUP_LIMIT = 5.0                   # seconds to wait for a process to come up
RECONNECT_LIMIT = 1.0                 # seconds with nobody on the port = the pair is down
REQUEST_LIMIT = 3.0                   # seconds without an answer to one request = the pair is down


def taken(port):
    """Something already listening here? Then don't use it. The test's first version cycled
    through 5432, where OrbStack forwards Postgres on *:5432, and the client hung forever
    talking to it after a failover. Same story as AirPlay on 5000 in RESULTS.md section 3."""
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def free_pair():
    while True:
        port = next(_ports)
        if not taken(port) and not taken(port + 1):
            return port


def value(seq):
    return 1 << (seq - 1)


def applied(total):
    """Which seqs a total says were applied, assuming none were applied twice."""
    return [i + 1 for i in range(total.bit_length()) if total >> i & 1]


def wait_for(path, text, proc):
    end = time.time() + STARTUP_LIMIT
    while time.time() < end:
        if os.path.exists(path) and text in open(path).read():
            return
        if proc.poll() is not None:
            raise RuntimeError(f"{path} exited early:\n{open(path).read()}")
        time.sleep(0.005)
    raise RuntimeError(f"timed out waiting for {text!r} in {path}")


class Client:
    """Same protocol and same failure handling as client.py."""

    def __init__(self, port, mode):
        self.port, self.mode = port, mode
        self.sock = self.f = None
        self.pending = []      # replay mode: acked seqs the backup may not have yet
        self.reconnects = 0

    def connect(self):
        end = time.time() + RECONNECT_LIMIT
        while time.time() < end:
            s = socket.socket()
            try:
                s.connect(("127.0.0.1", self.port))
            except ConnectionRefusedError:
                s.close()
                time.sleep(0.005)
                continue
            s.settimeout(0.5)   # same read timeout as client.py
            self.sock, self.f = s, s.makefile("rw")
            return True
        return False

    def ask(self, seq, before_read=None):
        self.f.write(f"{seq} {value(seq)}\n")
        self.f.flush()
        if before_read:
            before_read()
        reply = self.f.readline()
        if not reply:
            raise ConnectionError("server closed")
        r_seq, r_total, r_ckpt = map(int, reply.split())
        return r_seq, r_total, r_ckpt

    def request(self, seq, before_read=None):
        """Send seq until some server answers it. Returns (total, ckpt) from the answer."""
        errors = (ConnectionError, BrokenPipeError, OSError, socket.timeout)
        give_up = time.time() + REQUEST_LIMIT
        while True:
            assert time.time() < give_up, (
                f"seq {seq}: no answer in {REQUEST_LIMIT}s, the pair is down")
            try:
                r_seq, r_total, r_ckpt = self.ask(seq, before_read)
                ok = True
            except errors:
                ok = False
            if not ok:
                before_read = None
                try:
                    self.sock.close()
                except OSError:
                    pass
                # liveness: some server has to come back on the client port
                assert self.connect(), (
                    f"seq {seq}: nobody listening on {self.port} for {RECONNECT_LIMIT}s, "
                    "the pair is down")
                self.reconnects += 1
                if self.mode == "replay":
                    event("replayed on reconnect: " + ("0" if not self.pending else "1+"))
                    try:
                        for old in self.pending:
                            self.ask(old)
                    except errors:
                        pass
                continue
            assert r_seq == seq, f"asked for seq {seq}, reply was for seq {r_seq}"
            if self.mode == "replay":
                self.pending.append(r_seq)
                while self.pending and self.pending[0] <= r_ckpt:
                    self.pending.pop(0)
            return r_total, r_ckpt


class ProcessPair(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.dir = tempfile.mkdtemp(prefix="pp-")   # durable.log lands here, not in results/
        self.procs = {}
        self.seq = 0          # last seq the client got an answer for
        self.last_total = 0
        self.failed_over = False

    def spawn(self, name, args, ready):
        out = os.path.join(self.dir, f"{name}.out")
        p = subprocess.Popen(
            [sys.executable, "-u", os.path.join(ROOT, f"{name}.py"), *args],
            cwd=self.dir, env=self.env, stdout=open(out, "w"), stderr=subprocess.STDOUT)
        self.procs[name] = p
        wait_for(out, ready, p)
        return p

    @initialize(n=st.integers(1, 12))
    def start_pair(self, n):
        self.n = n
        port = free_pair()
        self.env = dict(os.environ, PP_MODE=MODE, PP_PORT=str(port),
                        PP_BACKUP_PORT=str(port + 1), PP_HB_TIMEOUT=str(HB_TIMEOUT))
        self.spawn("backup", [], "backup listening")
        self.spawn("primary", [str(n)], "primary listening")
        self.client = Client(port, MODE)
        assert self.client.connect()

    def send_one(self, before_read=None):
        seq = self.seq + 1
        total, _ = self.client.request(seq, before_read)
        self.seq = seq
        self.check(total)

    def check(self, total):
        expected = (1 << self.seq) - 1         # every seq 1..self.seq applied exactly once
        acked = set(range(1, self.seq + 1))
        # (b) no double apply. Checked first because a double apply corrupts the bitmask.
        assert total <= expected, (
            f"seq {self.seq}: total {total} > {expected}, some request was applied twice")
        # (a) nothing acked is lost
        missing = sorted(acked - set(applied(total)))
        assert not missing, (
            f"seq {self.seq}: acked requests {missing} are missing from the state "
            f"(N={self.n}, mode={MODE}, reconnects={self.client.reconnects})")
        # (c) monotonic
        assert total >= self.last_total, (
            f"seq {self.seq}: total went backwards, {self.last_total} -> {total}")
        self.last_total = total

    @rule(k=st.integers(1, 25))
    def send(self, k):
        for _ in range(k):
            self.send_one()

    def kill(self):
        p = self.procs["primary"]
        p.send_signal(signal.SIGKILL)
        p.wait()
        self.failed_over = True

    # backup.py never takes on a backup of its own after it takes over, and primary.py
    # always starts from zero, so the real code survives exactly one failover.
    @precondition(lambda self: not self.failed_over)
    @rule()
    def kill_primary(self):
        self.kill()

    @precondition(lambda self: not self.failed_over)
    @rule(delay=st.sampled_from([0, 0.0002, 0.001, 0.005]))
    def kill_primary_mid_request(self, delay):
        """Request is on the wire when the primary dies: maybe applied, maybe checkpointed,
        maybe logged, maybe answered."""
        def boom():
            if delay:
                time.sleep(delay)
            self.kill()
        self.send_one(before_read=boom)

    if STALL:
        @precondition(lambda self: not self.failed_over)
        @rule(pause=st.sampled_from([0.05, 0.3]))
        def stall_primary(self, pause):
            """SIGSTOP the primary, like a GC pause or a laptop lid, then let it go."""
            p = self.procs["primary"]
            p.send_signal(signal.SIGSTOP)
            time.sleep(pause)
            p.send_signal(signal.SIGCONT)
            event(f"stalled {pause}s")

    def teardown(self):
        event(f"failed over: {self.failed_over}")
        for p in self.procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGCONT)
                p.kill()
            p.wait()
        if getattr(self, "client", None) and self.client.sock:
            self.client.sock.close()
        shutil.rmtree(self.dir, ignore_errors=True)


ProcessPair.TestCase.settings = settings(
    max_examples=int(os.environ.get("PP_EXAMPLES", "50")),
    stateful_step_count=8,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
TestProcessPair = ProcessPair.TestCase
