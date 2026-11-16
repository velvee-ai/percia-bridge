"""Stand-in for ffmpeg in supervisor tests. Usage: fake_ffmpeg.py SCENARIO. Obeys 'q' on stdin like ffmpeg."""
import select
import sys
import time

scenario = sys.argv[1]


def got_q(t=0.0):
    r, _, _ = select.select([sys.stdin], [], [], t)
    return bool(r and sys.stdin.readline().strip() == "q")


def progress(n):
    print(f"frame={n}\nfps=15.0\nspeed=1.0x\nprogress=continue", flush=True)


if scenario == "die":
    print("Connection refused", file=sys.stderr, flush=True)
    sys.exit(1)
if scenario == "rtsp401":
    print("[in#0 @ 0x1] Error opening input: Server returned 401 Unauthorized (authorization failed)", file=sys.stderr, flush=True)
    sys.exit(1)
if scenario == "whip401":
    print("[out#0/whip @ 0x1] Could not write header (incorrect codec parameters ?): Server returned 401 Unauthorized (authorization failed)", file=sys.stderr, flush=True)
    while not got_q(0.2):
        pass
    sys.exit(0)
if scenario == "nothing":  # alive but never produces a frame
    while not got_q(0.2):
        pass
    sys.exit(0)
n = 0
limit = {"stream": None, "stream_then_die": 0.6, "stall": 0.4}[scenario]
t0 = time.time()
while True:
    if got_q(0.05):
        sys.exit(0)
    if limit is None or time.time() - t0 < limit:
        n += 1
        progress(n)
    elif scenario == "stream_then_die":
        print("Connection reset by peer", file=sys.stderr, flush=True)
        sys.exit(1)
    # stall: keep running with no new frames
