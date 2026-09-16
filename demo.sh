#!/bin/bash
# One-command demo for the recording: start the pair, stream replies, kill the primary
# mid-stream, watch the backup take over and the count continue.
cd "$(dirname "$0")"
pkill -f "primary.py|backup.py|client.py" 2>/dev/null; sleep 0.2
N=${1:-10}
python3 -u backup.py &  sleep 0.3
python3 -u primary.py $N &  sleep 0.3
PP_VERBOSE=2000 python3 -u client.py 200000 &
CLIENT=$!
sleep 2.5
echo; echo ">>> kill -9 primary"; echo
kill -9 $(pgrep -f "primary.py $N")
wait $CLIENT
pkill -f "backup.py" 2>/dev/null
