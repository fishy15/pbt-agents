"""Run Python source in a throwaway Docker container.

    from pbt_agents.sandbox.runner import run_code
    res = run_code("print(1+1)", timeout=10)   # {"exit_code", "stdout", "stderr", "timed_out", "duration"}

    uv run python -m pbt_agents.sandbox.runner script.py [--timeout 10]    # `-` reads stdin

The CLI prints the script's stdout/stderr and exits with its exit code: 124 on timeout,
125 if the sandbox itself is unavailable (Docker not running, image missing), so callers
never mistake "sandbox down" for "code failed".

Each call starts a fresh container: no network, read-only root filesystem, tmpfs scratch
space, dropped capabilities, and memory/CPU/pid limits. The code goes in on stdin, so
nothing from the host is mounted.

A cross-process limit on simultaneous containers is enforced with flock slot files, so
many agents calling the CLI at once cannot start an unbounded number of containers.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import subprocess
import tempfile
import time
import uuid

IMAGE = os.environ.get("PBT_SANDBOX_IMAGE", "pbt-sandbox")
MAX_CONCURRENT = int(os.environ.get("PBT_SANDBOX_MAX_CONCURRENT", "4"))
MAX_TIMEOUT_S = 120
MAX_OUTPUT = 64_000
LOCK_DIR = os.path.join(tempfile.gettempdir(), "pbt-sandbox-locks")


class SandboxUnavailable(RuntimeError):
    """Docker is missing or its daemon is not reachable (not a failure of the user's code)."""


def docker_cmd(name: str, image: str = IMAGE, memory: str = "512m", cpus: str = "1", pids: int = 128) -> list[str]:
    return [
        "docker", "run", "--rm", "-i", "--name", name,
        "--network", "none",
        "--memory", memory, "--memory-swap", memory,
        "--cpus", cpus, "--pids-limit", str(pids),
        "--read-only", "--tmpfs", "/tmp:rw,size=128m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "-e", "HOME=/tmp", "-e", "PYTHONUNBUFFERED=1",
        image, "python", "-I", "-",
    ]


class _Slot:
    """Hold one of MAX_CONCURRENT flock slot files for the duration of a run."""

    def __enter__(self):
        os.makedirs(LOCK_DIR, exist_ok=True)
        while True:
            for i in range(MAX_CONCURRENT):
                fd = os.open(os.path.join(LOCK_DIR, f"slot{i}"), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.fd = fd
                    return self
                except OSError:
                    os.close(fd)
            time.sleep(0.05)

    def __exit__(self, *exc):
        os.close(self.fd)  # closing releases the lock (also if the process dies)


def _text(x) -> str:
    return x.decode(errors="replace") if isinstance(x, bytes) else (x or "")


def run_code(code: str, timeout: float = 10, **docker_opts) -> dict:
    timeout = min(float(timeout), MAX_TIMEOUT_S)
    name = f"pbt-sandbox-{uuid.uuid4().hex[:12]}"
    with _Slot():
        start = time.monotonic()  # time spent waiting for a slot does not count
        timed_out = False
        try:
            r = subprocess.run(docker_cmd(name, **docker_opts), input=code,
                               capture_output=True, text=True, timeout=timeout)
            exit_code, out, err = r.returncode, r.stdout, r.stderr
        except FileNotFoundError as e:
            raise SandboxUnavailable(f"docker CLI not found: {e}") from e
        except subprocess.TimeoutExpired as e:
            timed_out = True
            # killing the docker CLI does not stop the container; remove it explicitly
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            exit_code, out, err = 124, _text(e.stdout), _text(e.stderr)
        # docker itself uses 125 for "could not run the container" (daemon down, no image, ...)
        if exit_code == 125 and not timed_out:
            raise SandboxUnavailable(err.strip() or "docker run failed")
    return {
        "exit_code": exit_code,
        "stdout": out[:MAX_OUTPUT],
        "stderr": err[:MAX_OUTPUT],
        "timed_out": timed_out,
        "duration": round(time.monotonic() - start, 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a Python file in the Docker sandbox.")
    ap.add_argument("file")
    ap.add_argument("--timeout", type=float, default=10)
    a = ap.parse_args()
    code = sys.stdin.read() if a.file == "-" else open(a.file).read()
    try:
        res = run_code(code, a.timeout)
    except SandboxUnavailable as e:
        print(f"sandbox unavailable: {e}", file=sys.stderr)
        sys.exit(125)
    sys.stdout.write(res["stdout"])
    sys.stderr.write(res["stderr"])
    if res["timed_out"]:
        print(f"[sandbox] timed out after {a.timeout}s", file=sys.stderr)
    sys.exit(res["exit_code"])


if __name__ == "__main__":
    main()
