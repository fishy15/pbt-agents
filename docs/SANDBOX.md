# Code sandbox (`pbt_agents.sandbox`)

Subagents write and run LLM-generated Python (implementations, property sets, cross-checks).
This runs that code in throwaway Docker containers instead of directly on the host.

## Setup

Start Docker, then build the image once:

```
docker build -t pbt-sandbox docker/sandbox            # python 3.13 + hypothesis + numpy
```

## Use

CLI (what the workflow's agents call):

```
uv run python -m pbt_agents.sandbox.runner script.py [--timeout 10]     # `-` reads the script from stdin
```

It prints the script's stdout/stderr and exits with the script's exit code: **124** on timeout, **125** if the sandbox itself is unavailable (Docker not running, image missing), so "sandbox down" is never mistaken for "code failed".

From Python:

```python
from pbt_agents.sandbox.runner import run_code
res = run_code("print(1 + 1)", timeout=10)
# {"exit_code": 0, "stdout": "2\n", "stderr": "", "timed_out": False, "duration": 0.4}
```

`run_code` raises `SandboxUnavailable` when Docker can't run the container. Env vars: `PBT_SANDBOX_IMAGE` (default `pbt-sandbox`), `PBT_SANDBOX_MAX_CONCURRENT` (default 4).

## Isolation

Each run is a fresh container with: no network, read-only root filesystem, 128MB tmpfs at `/tmp`, all capabilities dropped, `no-new-privileges`, non-root user, 512MB memory (no swap), 1 CPU, 128 pids. The code is sent on stdin; nothing from the host is mounted. Timeouts also `docker rm -f` the container.

Tested against the real image: network unreachable, writes outside `/tmp` rejected, host paths invisible, uid 10001, infinite loop → 124, 2GB allocation → killed (137), fork bomb stopped by the pid limit, no containers left behind.

At most `PBT_SANDBOX_MAX_CONCURRENT` containers run at once across all processes (flock slot files in the system temp dir), so many agents calling the CLI together queue instead of piling up. Time spent queued doesn't count against `--timeout`.

## Limits

- Containers share the host kernel: much stronger than running code directly, but not a VM-grade boundary.
- Nothing forces subagents to use the sandbox: they have Bash on the host, and the workflow only instructs them to. Docker access isn't a privilege boundary here either, since agents run as the same user.
- Each run costs container start-up time (~0.5s), so put a whole cross-check in one script rather than many small runs.
