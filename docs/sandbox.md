# Sandbox

Generated integration code never runs in the backend process. It runs only inside a Docker
container started by `app.codegen.sandbox.SandboxRunner`, after the static gate has passed.

## What a run looks like

```
docker run --read-only --tmpfs /scratch (32 MB, noexec) --tmpfs /tmp (16 MB, noexec)
  --network <per-run internal network | none> --cap-drop ALL --security-opt no-new-privileges
  --pids-limit 64 --memory 256m (no swap) --cpus 0.5 --user 10001:10001
  --ulimit nofile=128 --ulimit fsize=10MB --log-driver none
  --mount <bundle>:/app/bundle (read-only)  morph-sandbox:dev  python -E -s -B -m integration
```

Defaults: 256 MB, 0.5 CPU, 64 processes, 60 s wall clock. Only
`MORPH_*` environment variables can be passed in. Nothing else is mounted: no repository, no
`.env`, no bench, no oracle, no docker socket.

## Output cap

`SandboxLimits.output_cap_bytes = 1_000_000`: at most **1,000,000 bytes (about 1 MB) of stdout
and, separately, 1,000,000 bytes of stderr** are kept. The first byte past the cap makes the
runner kill the container and label the run `OUTPUT_LIMIT`; anything already beyond the cap is
discarded (the pipe keeps being drained so nothing blocks), and `--log-driver none` stops Docker
from storing the flood. The containment test lowers the cap to 200,000 to run quickly and shows
that the kept output never exceeds the cap (it kept 196,608 bytes: the whole 64 KiB chunks that
fit under it).

## Network

Each run creates a throwaway `--internal` network (no gateway, no internet) with fresh mock CRM
and Support containers on it, aliased `crm` and `support`, using random per-run credentials. The
mocks also publish loopback ports so trusted host code can seed state and read the request log
through `/__admin`; the admin tokens are never given to the sandbox.

## Resource hygiene

Everything created carries `morph.owner=morph` and `morph.run=<id>`. Only resources this code
created, and that still carry both labels, are removed, including on failure. There is no
`prune` and no unscoped `rm` anywhere. Stale resources of a crashed run can be removed with
`remove_labelled_leftovers()`, which selects by the owner label only.

## Limits are verified before anything runs

`SandboxRunner.run` first starts a probe container and checks, from both `docker inspect` and
inside the container (cgroup memory, pids and CPU limits, capabilities, `no-new-privileges`,
read-only root, mount options, uid, interfaces, environment), that every limit is in force. If
any is not, it raises `LimitsNotEnforced` and nothing runs.

## Static gate

1. **AST stage** (`app.codegen.gate`, host side, parse only): import allowlist, banned
   builtins, no dunder access, no star imports, no decorators except `dataclass`, no async, no
   URL or secret-looking string literals, no lint/type suppression comments, file and size
   limits.
2. **ruff and mypy --strict** (`app.codegen.gate_tools`, inside the image, no network). The
   configuration is in the image (`/opt/gate`), so a bundle cannot weaken it; `noqa` is ignored.

A bundle that fails any stage is recorded as `GATE_FAILED` and never executed. The gate is
defence in depth; the container is the boundary.

## Threat model and evidence

| Threat | Stopped by | Test |
|---|---|---|
| fork or thread bomb | `--pids-limit` | `fork bomb`, `thread bomb` |
| memory bomb | `--memory`, no swap | `memory hog` (OOM, exit 137) |
| CPU spin, hang | `--cpus`, host timeout + `docker kill` | `cpu spin`, `cpu share` |
| output flood | capped capture, kill, `--log-driver none` | `output flood` |
| descriptor exhaustion | `--ulimit nofile` | `fd exhaustion` |
| writing outside scratch, tampering with the bundle | read-only root and bundle | `write outside scratch`, `bundle tamper` |
| filling the disk, persisting state | size-limited, ephemeral tmpfs | `disk fill`, `scratch persistence` |
| privilege escalation | non-root, no capabilities, no-new-privileges | `privilege escalation` |
| reading host secrets | nothing mounted, explicit env allowlist | `secret hunt` |
| internet or host access | internal network or none | `network egress` (with positive control) |
| using the mocks' admin API | admin tokens not in the sandbox | `admin api` |

The tests (`pytest -m docker backend/tests/codegen`) derive each verdict from raw measurements
and independent host-side evidence (a host listener that must see no connection, a hashed
bundle directory, a canary secret), never from a hostile module's own claim.

Residual risk: a Linux kernel or container-runtime escape is not addressed beyond the defaults
above. This is a Phase 1 development sandbox, not a production isolation claim.

## Building the images

```powershell
docker build -t morph-sandbox:dev --label morph.owner=morph sandbox
docker build -t morph-sbx-mock:dev --label morph.owner=morph mock_systems
```
