"""Phase 9: process lifetime and machine-wide AI request slots (real processes)."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ai import slots
from ai.base import TemporaryError

HERE = Path(__file__).resolve().parent.parent


def alive(pid):
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                             text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
def test_children_die_when_studio_is_killed(tmp_path):
    pid_file = tmp_path / "child.pid"
    parent_code = (
        "import subprocess, sys, time; sys.path.insert(0, sys.argv[1]); import winjob;"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
        "assert winjob.attach(c); open(sys.argv[2], 'w').write(str(c.pid)); time.sleep(120)")
    parent = subprocess.Popen([sys.executable, "-c", parent_code, str(HERE), str(pid_file)])
    deadline = time.time() + 20
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.1)
    child = int(pid_file.read_text())
    assert alive(child)
    subprocess.run(["taskkill", "/PID", str(parent.pid), "/F"], capture_output=True)  # no /T
    parent.wait(10)
    deadline = time.time() + 10
    while alive(child) and time.time() < deadline:
        time.sleep(0.2)
    assert not alive(child), "the worker ended with Studio"


def run_holders(tmp_path, count, seconds, limit):
    env = {**os.environ, "CHOPIFY_CONFIG": str(tmp_path / "cfg")}
    procs = [subprocess.Popen([sys.executable, "-m", "ai.slots", "groq", str(seconds),
                               str(tmp_path / f"h{i}.txt"), str(limit)], cwd=HERE, env=env)
             for i in range(count)]
    for proc in procs:
        assert proc.wait(60) == 0
    spans = []
    for i in range(count):
        lines = (tmp_path / f"h{i}.txt").read_text(encoding="utf-8").split()
        spans.append((float(lines[0]), float(lines[2])))
    return spans


def test_at_most_n_requests_per_provider_across_processes(tmp_path):
    spans = run_holders(tmp_path, count=4, seconds=1.5, limit=2)
    events = sorted([(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans])
    running = peak = 0
    for _, step in events:
        running += step
        peak = max(peak, running)
    assert peak == 2, "two at a time, never more"


def test_a_killed_holder_releases_its_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("CHOPIFY_CONFIG", str(tmp_path / "cfg"))
    env = {**os.environ, "CHOPIFY_CONFIG": str(tmp_path / "cfg")}
    holder = subprocess.Popen([sys.executable, "-m", "ai.slots", "groq", "60",
                               str(tmp_path / "h.txt"), "1"], cwd=HERE, env=env)
    deadline = time.time() + 20
    while not (tmp_path / "h.txt").exists() and time.time() < deadline:
        time.sleep(0.1)
    with pytest.raises(TemporaryError):
        with slots.slot("groq", limit=1, wait=1):
            pass
    holder.kill()
    holder.wait(10)
    started = time.time()
    with slots.slot("groq", limit=1, wait=10):
        assert time.time() - started < 5, "the OS released the dead process's lock"
    assert slots.busy("groq", 1) == 0


def test_threads_in_one_process_share_the_limit(tmp_path, monkeypatch):
    import threading
    monkeypatch.setenv("CHOPIFY_CONFIG", str(tmp_path / "cfg"))
    inside, peak, lock = [0], [0], threading.Lock()

    def work():
        with slots.slot("nvidia", limit=2, wait=20):
            with lock:
                inside[0] += 1
                peak[0] = max(peak[0], inside[0])
            time.sleep(0.3)
            with lock:
                inside[0] -= 1
    threads = [threading.Thread(target=work) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert peak[0] == 2


def test_simulated_provider_only_exists_when_enabled_and_is_deterministic(tmp_path):
    code = r'''
import json, subprocess, sys
from ai import registry
print(json.dumps({"registered": "simulated" in registry.REGISTRY}))
if "simulated" in registry.REGISTRY:
    from ai.gateway import Gateway
    from ai import policy
    from ai.usage import Ledger
    s = policy.load()
    s["providers"]["simulated"] = {"enabled": True, "models": []}
    s["routes"]["transcribe"] = {"provider": "simulated", "model": "whisper"}
    s["routes"]["generate"] = {"provider": "simulated", "model": "judge"}
    g = Gateway(ledger=Ledger(__import__("pathlib").Path(sys.argv[1]) / "u.db"), settings=s)
    wav = sys.argv[1] + "/a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=12", wav], check=True)
    t = g.transcribe(wav, offset=100)
    schema = {"type": "object", "properties": {"hook": {"type": "integer"},
              "verdict": {"type": "string", "enum": ["keep", "drop"]}}}
    a = g.generate("s", "clip one", schema=schema).data
    b = g.generate("s", "clip one", schema=schema).data
    print(json.dumps({"words": sum(len(x.words) for x in t.segments),
                      "first": t.segments[0].words[0].start, "same": a == b,
                      "verdict": a["verdict"], "rows": g.ledger.summary()}))
'''
    base = {**os.environ, "CHOPIFY_CONFIG": str(tmp_path / "cfg"), "CHOPIFY_AI_SIMULATE_LATENCY": "0"}
    off = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=HERE, env={
        k: v for k, v in base.items() if k != "CHOPIFY_AI_SIMULATE"}, capture_output=True,
        text=True)
    assert '"registered": false' in off.stdout, off.stderr
    on = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=HERE,
                        env={**base, "CHOPIFY_AI_SIMULATE": "1"}, capture_output=True, text=True)
    lines = on.stdout.strip().splitlines()
    assert '"registered": true' in lines[0], on.stderr
    import json
    result = json.loads(lines[1])
    assert result["words"] > 15 and result["first"] >= 100 and result["same"]
    assert result["verdict"] in ("keep", "drop")
    assert all(r["free"] and r["cost_usd"] == 0 for r in result["rows"])
