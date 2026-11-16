#!/usr/bin/env python3
"""Runs the simulated-camera end-to-end tests (tests/test_e2e_simulated_camera.py). Localhost only, no real keys or cameras.
Needs ffmpeg >= 7.1 (whip muxer), ffprobe and mediamtx on PATH."""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-m", "e2e", "-p", "no:cacheprovider", str(root / "tests" / "test_e2e_simulated_camera.py")], cwd=root))
