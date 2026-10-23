// Property tests for the process pair, written with Hegel (hegel.dev) for Go.
//
// This is a port of tests/test_properties.py. It runs the real primary.py and backup.py
// as separate processes, kills the primary with SIGKILL at points Hegel picks, and checks
// three properties on every reply the client gets back:
//
//	(a) nothing acked is lost: the state includes every request the client saw acknowledged
//	(b) nothing is applied twice: sequence-number dedupe holds
//	(c) the state the client observes never goes backwards
//
// It also fails if nobody answers on the client port for a while, because a pair that's
// down is losing everything.
//
// The client side speaks the same protocol as client.py and follows the same rules: resend
// the same seq after a reconnect, and in replay mode resend everything newer than the last
// checkpoint the server reported.
//
// Request seq k carries value 2^(k-1), so the server's total is a bitmask of exactly which
// requests it has applied. A missing bit is a lost request. A total bigger than the sum of
// everything sent means something was added twice.
//
// usage (from this directory):
//
//	go test ./...                                   # checkpoint only (PP_MODE unset)
//	PP_MODE=replay  go test ./...
//	PP_MODE=durable go test ./...
//	PP_STALL=1 PP_MODE=replay go test ./...         # also pause the primary
//
// Knobs: PP_EXAMPLES (default 50), PP_TEST_PORT (default 5200, uses a pair of ports per
// test case). The pair gets PP_PORT, PP_BACKUP_PORT, PP_HB_TIMEOUT and PP_MODE.
package hegeltest

import (
	"bufio"
	"fmt"
	"math/big"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"hegel.dev/go/hegel"
)

const (
	hbTimeout      = "0.1"           // backup.py's default
	startupLimit   = 5 * time.Second // wait for a process to come up
	reconnectLimit = 1 * time.Second // nobody on the port this long = the pair is down
	requestLimit   = 3 * time.Second // no answer to one request this long = the pair is down
	readTimeout    = 500 * time.Millisecond
)

var (
	root     = mustRoot()
	mode     = envOr("PP_MODE", "none")
	stall    = os.Getenv("PP_STALL") == "1"
	basePort = atoi(envOr("PP_TEST_PORT", "5200"))
	nextPort = basePort
)

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func atoi(s string) int {
	n, err := strconv.Atoi(s)
	if err != nil {
		panic(err)
	}
	return n
}

func mustRoot() string {
	wd, err := os.Getwd() // go test runs in tests/hegel
	if err != nil {
		panic(err)
	}
	return filepath.Dir(filepath.Dir(wd))
}

// taken says whether something is already listening on port. The Python test's first
// version cycled through 5432, where OrbStack forwards Postgres, and the client hung
// talking to it after a failover. Same story as AirPlay on 5000 in RESULTS.md section 3.
func taken(port int) bool {
	c, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), 200*time.Millisecond)
	if err != nil {
		return false
	}
	c.Close()
	return true
}

// freePair cycles through base..base+200 in steps of 2 and skips anything in use.
func freePair() int {
	for {
		port := nextPort
		nextPort += 2
		if nextPort >= basePort+200 {
			nextPort = basePort
		}
		if !taken(port) && !taken(port+1) {
			return port
		}
	}
}

// value is 2^(seq-1). With up to 8 steps of 25 requests that's 200 bits, so big.Int.
func seconds(s float64) time.Duration { return time.Duration(s * float64(time.Second)) }

func value(seq int) *big.Int { return new(big.Int).Lsh(big.NewInt(1), uint(seq-1)) }

// applied lists which seqs a total says were applied, assuming none were applied twice.
func applied(total *big.Int) map[int]bool {
	out := map[int]bool{}
	for i := 0; i < total.BitLen(); i++ {
		if total.Bit(i) == 1 {
			out[i+1] = true
		}
	}
	return out
}

// client speaks the same protocol as client.py and handles failures the same way.
type client struct {
	port       int
	mode       string
	conn       net.Conn
	r          *bufio.Reader
	pending    []int // replay mode: acked seqs the backup may not have yet
	reconnects int
	tc         hegel.TestCase // the current rule's test case, for failures and events
}

// fail reports a broken property to Hegel and stops the test case.
func (c *client) fail(format string, args ...any) { c.tc.Errorf(format, args...) }

func (c *client) connect() bool {
	end := time.Now().Add(reconnectLimit)
	for time.Now().Before(end) {
		conn, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%d", c.port))
		if err != nil {
			time.Sleep(5 * time.Millisecond)
			continue
		}
		c.conn, c.r = conn, bufio.NewReader(conn)
		return true
	}
	return false
}

func (c *client) ask(seq int, beforeRead func()) (int, *big.Int, int, error) {
	if _, err := fmt.Fprintf(c.conn, "%d %d\n", seq, value(seq)); err != nil {
		return 0, nil, 0, err
	}
	if beforeRead != nil {
		beforeRead()
	}
	c.conn.SetReadDeadline(time.Now().Add(readTimeout)) // same read timeout as client.py
	line, err := c.r.ReadString('\n')
	if err != nil {
		return 0, nil, 0, err
	}
	f := strings.Fields(line)
	if len(f) != 3 {
		return 0, nil, 0, fmt.Errorf("bad reply %q", line)
	}
	rSeq, _ := strconv.Atoi(f[0])
	rTotal, ok := new(big.Int).SetString(f[1], 10)
	if !ok {
		return 0, nil, 0, fmt.Errorf("bad total in %q", line)
	}
	rCkpt, _ := strconv.Atoi(f[2])
	return rSeq, rTotal, rCkpt, nil
}

// request sends seq until some server answers it. Returns the total from the answer.
func (c *client) request(seq int, beforeRead func()) *big.Int {
	giveUp := time.Now().Add(requestLimit)
	for {
		if !time.Now().Before(giveUp) {
			c.fail("seq %d: no answer in %v, the pair is down", seq, requestLimit)
		}
		rSeq, rTotal, rCkpt, err := c.ask(seq, beforeRead)
		if err != nil {
			beforeRead = nil
			c.conn.Close()
			// liveness: some server has to come back on the client port
			if !c.connect() {
				c.fail("seq %d: nobody listening on %d for %v, the pair is down", seq, c.port, reconnectLimit)
			}
			c.reconnects++
			if c.mode == "replay" {
				if len(c.pending) == 0 {
					c.tc.Event("replayed on reconnect: 0")
				} else {
					c.tc.Event("replayed on reconnect: 1+")
				}
				for _, old := range c.pending {
					if _, _, _, err := c.ask(old, nil); err != nil {
						break
					}
				}
			}
			continue
		}
		if rSeq != seq {
			c.fail("asked for seq %d, reply was for seq %d", seq, rSeq)
		}
		if c.mode == "replay" {
			c.pending = append(c.pending, rSeq)
			for len(c.pending) > 0 && c.pending[0] <= rCkpt {
				c.pending = c.pending[1:]
			}
		}
		return rTotal
	}
}

// pair is the state machine. Rules are the methods named Rule*.
type pair struct {
	dir        string
	env        []string
	procs      map[string]*proc
	client     *client
	n          int
	seq        int // last seq the client got an answer for
	lastTotal  *big.Int
	failedOver bool
}

type proc struct {
	cmd    *exec.Cmd
	exited chan struct{} // closed once the process has exited and been reaped
}

func (p *pair) spawn(name string, args []string, ready string) {
	out := filepath.Join(p.dir, name+".out")
	f, err := os.Create(out)
	if err != nil {
		panic(err)
	}
	cmd := exec.Command("python3", append([]string{"-u", filepath.Join(root, name+".py")}, args...)...)
	cmd.Dir, cmd.Env, cmd.Stdout, cmd.Stderr = p.dir, p.env, f, f
	if err := cmd.Start(); err != nil {
		panic(err)
	}
	exited := make(chan struct{})
	go func() { cmd.Wait(); f.Close(); close(exited) }()
	p.procs[name] = &proc{cmd, exited}
	end := time.Now().Add(startupLimit)
	for time.Now().Before(end) {
		b, _ := os.ReadFile(out)
		if strings.Contains(string(b), ready) {
			return
		}
		select {
		case <-exited:
			panic(fmt.Sprintf("%s exited early:\n%s", out, b))
		default:
		}
		time.Sleep(5 * time.Millisecond)
	}
	panic(fmt.Sprintf("timed out waiting for %q in %s", ready, out))
}

func (p *pair) start(n int) {
	p.n = n
	port := freePair()
	p.env = append(os.Environ(), "PP_MODE="+mode, fmt.Sprintf("PP_PORT=%d", port),
		fmt.Sprintf("PP_BACKUP_PORT=%d", port+1), "PP_HB_TIMEOUT="+hbTimeout)
	p.spawn("backup", nil, "backup listening")
	p.spawn("primary", []string{strconv.Itoa(n)}, "primary listening")
	p.client.port = port
	if !p.client.connect() {
		panic("could not connect to the primary")
	}
}

func (p *pair) sendOne(beforeRead func()) {
	seq := p.seq + 1
	total := p.client.request(seq, beforeRead)
	p.seq = seq
	p.check(total)
}

func (p *pair) check(total *big.Int) {
	// every seq 1..p.seq applied exactly once
	expected := new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), uint(p.seq)), big.NewInt(1))
	// (b) no double apply. Checked first because a double apply corrupts the bitmask.
	if total.Cmp(expected) > 0 {
		p.client.fail("seq %d: total %d > %d, some request was applied twice", p.seq, total, expected)
	}
	// (a) nothing acked is lost
	got := applied(total)
	var missing []int
	for s := 1; s <= p.seq; s++ {
		if !got[s] {
			missing = append(missing, s)
		}
	}
	if len(missing) > 0 {
		p.client.fail("seq %d: acked requests %v are missing from the state (N=%d, mode=%s, reconnects=%d)",
			p.seq, missing, p.n, mode, p.client.reconnects)
	}
	// (c) monotonic
	if total.Cmp(p.lastTotal) < 0 {
		p.client.fail("seq %d: total went backwards, %d -> %d", p.seq, p.lastTotal, total)
	}
	p.lastTotal = total
}

func (p *pair) kill() {
	pr := p.procs["primary"]
	syscall.Kill(pr.cmd.Process.Pid, syscall.SIGKILL)
	<-pr.exited // like p.wait() in the Python version
	p.failedOver = true
}

func (p *pair) teardown() {
	for _, pr := range p.procs {
		select {
		case <-pr.exited:
		default:
			syscall.Kill(pr.cmd.Process.Pid, syscall.SIGCONT)
			syscall.Kill(pr.cmd.Process.Pid, syscall.SIGKILL)
			<-pr.exited
		}
	}
	if p.client.conn != nil {
		p.client.conn.Close()
	}
	os.RemoveAll(p.dir)
}

// RuleSend sends k requests in a row.
func (p *pair) RuleSend(tc hegel.TestCase) {
	p.client.tc = tc
	k := hegel.Draw(tc, hegel.Integers(1, 25))
	for range k {
		p.sendOne(nil)
	}
}

// backup.py never takes on a backup of its own after it takes over, and primary.py
// always starts from zero, so the real code survives exactly one failover.

// RuleKillPrimary kills the primary between requests.
func (p *pair) RuleKillPrimary(tc hegel.TestCase) {
	p.client.tc = tc
	tc.Assume(!p.failedOver)
	p.kill()
}

// RuleKillPrimaryMidRequest kills the primary while a request is on the wire: maybe
// applied, maybe checkpointed, maybe logged, maybe answered.
func (p *pair) RuleKillPrimaryMidRequest(tc hegel.TestCase) {
	p.client.tc = tc
	tc.Assume(!p.failedOver)
	delay := hegel.Draw(tc, hegel.SampledFrom([]float64{0, 0.0002, 0.001, 0.005})) // seconds
	p.sendOne(func() {
		if delay > 0 {
			time.Sleep(seconds(delay))
		}
		p.kill()
	})
}

// stallPair adds the pause rule. It only exists when PP_STALL=1, same as the Python version.
type stallPair struct{ *pair }

// RuleStallPrimary SIGSTOPs the primary, like a GC pause or a laptop lid, then lets it go.
func (p *stallPair) RuleStallPrimary(tc hegel.TestCase) {
	p.client.tc = tc
	tc.Assume(!p.failedOver)
	pause := hegel.Draw(tc, hegel.SampledFrom([]float64{0.05, 0.3})) // seconds
	pid := p.procs["primary"].cmd.Process.Pid
	syscall.Kill(pid, syscall.SIGSTOP)
	time.Sleep(seconds(pause))
	syscall.Kill(pid, syscall.SIGCONT)
	tc.Event(fmt.Sprintf("stalled %vs", pause))
}

func TestProcessPair(t *testing.T) {
	examples := atoi(envOr("PP_EXAMPLES", "50"))
	cases := 0
	start := time.Now()
	hegel.Test(t, func(ht *hegel.T) {
		cases++
		var tc hegel.TestCase = ht
		dir, err := os.MkdirTemp("", "pp-") // durable.log lands here, not in results/
		if err != nil {
			panic(err)
		}
		p := &pair{dir: dir, procs: map[string]*proc{}, lastTotal: new(big.Int), client: &client{mode: mode, tc: tc}}
		defer p.teardown()
		n := hegel.Draw(tc, hegel.Integers(1, 12))
		p.start(n)
		if stall {
			hegel.RunStateful(tc, &stallPair{p}, hegel.WithStatefulStepCount(8))
		} else {
			hegel.RunStateful(tc, p, hegel.WithStatefulStepCount(8))
		}
		tc.Event(fmt.Sprintf("failed over: %v", p.failedOver))
	}, hegel.WithTestCases(examples), hegel.SuppressHealthCheck(hegel.TooSlow))
	t.Logf("%d test cases (including shrinking) in %.1fs, mode=%s stall=%v", cases, time.Since(start).Seconds(), mode, stall)
}
