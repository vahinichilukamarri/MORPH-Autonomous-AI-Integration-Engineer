"""Containment corpus: hostile modules run in the REAL sandbox and must be contained.

Every module is a ``python -m integration`` entry point written to a temp bundle (the AST gate is
deliberately bypassed: this tests the container, not the gate). Verdicts are derived by trusted
test code from raw measurements and from independent host-side evidence (a host listener, a
hashed bundle directory, a canary secret), never from a module's claim that it was contained.

The first test verifies that the configured limits are in force; if they are not, the whole
session stops before any hostile module runs.

Run with: ``pytest -m docker tests/codegen/test_containment.py``
"""

import hashlib
import json
import os
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.codegen.sandbox import (
    SANDBOX_UID,
    LimitsNotEnforced,
    MockEnvironment,
    Outcome,
    SandboxError,
    SandboxLimits,
    SandboxResult,
    SandboxRunner,
    docker,
)

pytestmark = pytest.mark.docker

CANARY = "MORPH-CANARY-" + hashlib.sha256(os.urandom(8)).hexdigest()[:16]
REPORT = Path(__file__).resolve().parents[2] / ".cache" / "containment-report.json"
_rows: list[dict[str, str]] = []


def runner_with(**overrides: Any) -> SandboxRunner:
    runner = SandboxRunner(limits=SandboxLimits(**overrides))
    try:
        runner.verify_limits()
    except LimitsNotEnforced as error:
        info = docker(
            "info", "--format", "cgroup={{.CgroupVersion}} swaplimit={{.SwapLimit}} {{.SecurityOptions}}",
            check=False,
        ).stdout.strip()  # fmt: skip
        detail = " ".join(f"{error} | docker: {info}".split())[:1500]
        print(f"::error title=sandbox limits are not enforced::{detail}")  # shown by CI annotations
        pytest.exit(f"sandbox limits are NOT enforced, stopping: {error}", returncode=3)
    return runner


def bundle(tmp_path: Path, source: str) -> Path:
    package = tmp_path / "integration"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(source)
    return tmp_path


def run(runner: SandboxRunner, tmp_path: Path, source: str, **kwargs: Any) -> SandboxResult:
    return runner.run(
        bundle(tmp_path, source), ["python", "-E", "-s", "-B", "-m", "integration"], **kwargs
    )


def tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(file.relative_to(path).as_posix().encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def record(module: str, expected: str, observed: str, evidence: str) -> None:
    _rows.append(
        {"module": module, "expected": expected, "observed": observed, "evidence": evidence}
    )
    assert observed == expected, f"{module}: expected {expected}, observed {observed}"


@pytest.fixture(scope="module", autouse=True)
def write_report() -> Iterator[None]:
    yield
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(_rows, indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def canary_in_host_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MORPH_HOST_CANARY", CANARY)
    monkeypatch.setenv("HOST_SECRET_CANARY", CANARY)


def test_00_limits_are_in_force() -> None:
    """Runs first. A failure stops the session: no hostile module may run without limits."""
    info = runner_with().verify_limits()
    probe = info["probe"]
    assert probe["uid"] == SANDBOX_UID and probe["interfaces"] == ["lo"]
    record("limits probe", "ENFORCED", "ENFORCED", "cgroup memory/pids/cpu, caps, mounts, uid")


# ---- resource exhaustion -----------------------------------------------------------------------

FORK_BOMB = """
import json, os, time
children, error = 0, None
for _ in range(500):
    try:
        pid = os.fork()
    except OSError as e:
        error = e.errno
        break
    if pid == 0:
        time.sleep(60)
        os._exit(0)
    children += 1
print(json.dumps({"children": children, "errno": error}), flush=True)
os._exit(0)
"""

THREAD_BOMB = """
import json, threading, time
started, error = 0, None
for _ in range(500):
    try:
        threading.Thread(target=time.sleep, args=(60,), daemon=True).start()
    except RuntimeError as e:
        error = str(e)
        break
    started += 1
print(json.dumps({"threads": started, "error": error}), flush=True)
"""


def test_fork_bomb_stops_at_the_pids_limit(tmp_path: Path) -> None:
    result = run(runner_with(), tmp_path, FORK_BOMB)
    data = result.json_stdout()
    ok = data["children"] < 64 and data["errno"] == 11  # EAGAIN
    record(
        "fork bomb",
        "PIDS_LIMIT",
        "PIDS_LIMIT" if ok else "ESCAPED",
        f"{data['children']} forks, errno {data['errno']}",
    )
    assert result.outcome is Outcome.OK


def test_thread_bomb_stops_at_the_pids_limit(tmp_path: Path) -> None:
    result = run(runner_with(), tmp_path, THREAD_BOMB)
    data = result.json_stdout()
    ok = data["threads"] < 64 and data["error"] is not None
    record(
        "thread bomb", "PIDS_LIMIT", "PIDS_LIMIT" if ok else "ESCAPED", f"{data['threads']} threads"
    )


MEMORY_HOG = """
import sys
chunks = []
while True:
    chunks.append(bytearray(16 * 1024 * 1024))
    for c in chunks[-1:]:
        c[::4096] = b"x" * len(c[::4096])
"""


def test_memory_hog_is_oom_killed(tmp_path: Path) -> None:
    result = run(runner_with(timeout_s=40), tmp_path, MEMORY_HOG)
    record(
        "memory hog",
        "OOM",
        result.outcome.value,
        f"exit {result.exit_code}, OOMKilled in docker state",
    )
    assert result.exit_code == 137


def test_infinite_loop_is_killed_on_timeout(tmp_path: Path) -> None:
    result = run(runner_with(timeout_s=4), tmp_path, "while True:\n    pass\n")
    record("cpu spin", "TIMEOUT", result.outcome.value, f"{result.duration_s:.1f}s wall clock")
    assert result.duration_s < 30
    assert (
        docker(
            "ps", "-a", "--filter", f"name={result.container}", "--format", "{{.Names}}"
        ).stdout.strip()
        == ""
    )


def test_cpu_quota_slows_a_spinner(tmp_path: Path) -> None:
    source = """
import json, time
start = time.process_time(); wall = time.monotonic()
while time.monotonic() - wall < 3:
    pass
print(json.dumps({"cpu": time.process_time() - start, "wall": time.monotonic() - wall}))
"""
    data = run(runner_with(), tmp_path, source).json_stdout()
    share = data["cpu"] / data["wall"]
    record(
        "cpu share",
        "CPU_QUOTA",
        "CPU_QUOTA" if share <= 0.65 else "UNLIMITED",
        f"{share:.2f} of one core (limit 0.5)",
    )


def test_output_flood_is_cut_off(tmp_path: Path) -> None:
    source = "import sys\nwhile True:\n    sys.stdout.write('x' * 4096)\n"
    result = run(runner_with(timeout_s=40, output_cap_bytes=200_000), tmp_path, source)
    record(
        "output flood",
        "OUTPUT_LIMIT",
        result.outcome.value,
        f"kept {len(result.stdout)} bytes of an unbounded stream",
    )
    assert len(result.stdout) <= 200_000


def test_descriptor_exhaustion_stops_at_nofile(tmp_path: Path) -> None:
    source = """
import json, os
fds, error = [], None
for _ in range(2000):
    try:
        fds.append(os.open("/proc/self/status", os.O_RDONLY))
    except OSError as e:
        error = e.errno
        break
print(json.dumps({"opened": len(fds), "errno": error}))
"""
    data = run(runner_with(), tmp_path, source).json_stdout()
    ok = data["opened"] <= 128 and data["errno"] == 24  # EMFILE
    record(
        "fd exhaustion",
        "NOFILE_LIMIT",
        "NOFILE_LIMIT" if ok else "ESCAPED",
        f"{data['opened']} descriptors",
    )


# ---- filesystem --------------------------------------------------------------------------------

WRITE_ATTEMPTS = """
import json, os
def attempt(path):
    try:
        with open(path, "w") as f:
            f.write("pwned")
        return "WRITTEN"
    except OSError as e:
        return e.errno
print(json.dumps({p: attempt(p) for p in [
    "/pwned", "/etc/pwned", "/usr/pwned", "/app/pwned", "/app/bundle/pwned",
    "/app/bundle/integration/__main__.py", "/proc/pwned", "/root/pwned", "/home/pwned",
]}))
"""


def test_writes_outside_scratch_are_refused_and_the_bundle_is_untouched(tmp_path: Path) -> None:
    source_dir = bundle(tmp_path, WRITE_ATTEMPTS)
    before = tree_hash(source_dir)
    result = SandboxRunner(limits=SandboxLimits()).run(
        source_dir, ["python", "-E", "-s", "-B", "-m", "integration"]
    )
    data = result.json_stdout()
    ok = "WRITTEN" not in data.values()
    record(
        "write outside scratch",
        "READ_ONLY_FS",
        "READ_ONLY_FS" if ok else "ESCAPED",
        f"errnos {sorted(set(map(str, data.values())))}",
    )
    record(
        "bundle tamper",
        "BUNDLE_UNCHANGED",
        "BUNDLE_UNCHANGED" if tree_hash(source_dir) == before else "MODIFIED",
        "host directory hash before/after",
    )


def test_scratch_is_capped_and_does_not_persist(tmp_path: Path) -> None:
    source = """
import json, os
written, error = 0, None
try:
    for n in range(20):
        with open(f"/scratch/fill{n}", "wb") as f:
            for _ in range(8):
                f.write(b"x" * 1024 * 1024)
                f.flush()
                written += 1
except OSError as e:
    error = e.errno
print(json.dumps({"mb": written, "errno": error, "previous": os.path.exists("/scratch/marker")}))
open("/scratch/marker", "w").write("1")
"""
    runner = runner_with()
    first = run(runner, tmp_path, source)
    data = first.json_stdout()
    ok = data["mb"] <= 32 and data["errno"] == 28  # ENOSPC
    record(
        "disk fill",
        "SCRATCH_CAP",
        "SCRATCH_CAP" if ok else "ESCAPED",
        f"{data['mb']} MB written, errno {data['errno']}",
    )
    second = run(runner, tmp_path, source).json_stdout()
    record(
        "scratch persistence",
        "EPHEMERAL",
        "EPHEMERAL" if not second["previous"] else "PERSISTED",
        "marker from run 1 absent in run 2",
    )


# ---- privilege and secrets ---------------------------------------------------------------------

PRIVILEGE = """
import ctypes, json, os
out = {"uid": os.getuid(), "gid": os.getgid()}
for name, call in {
    "setuid0": lambda: os.setuid(0),
    "chown": lambda: os.chown("/tmp", 0, 0),
    "chroot": lambda: os.chroot("/"),
}.items():
    try:
        call()
        out[name] = "ALLOWED"
    except OSError as e:
        out[name] = e.errno
libc = ctypes.CDLL(None, use_errno=True)
out["mount"] = libc.mount(b"none", b"/mnt", b"tmpfs", 0, None), ctypes.get_errno()
out["socket_raw"] = "ALLOWED"
try:
    import socket
    socket.socket(socket.AF_INET, socket.SOCK_RAW)
except OSError as e:
    out["socket_raw"] = e.errno
out["sock"] = os.path.exists("/var/run/docker.sock") or os.path.exists("/run/docker.sock")
print(json.dumps(out))
"""


def test_no_privilege_escalation(tmp_path: Path) -> None:
    data = run(runner_with(), tmp_path, PRIVILEGE).json_stdout()
    denied = all(data[k] != "ALLOWED" for k in ("setuid0", "chown", "chroot", "socket_raw"))
    mount_failed = data["mount"][0] == -1
    ok = data["uid"] == SANDBOX_UID and denied and mount_failed and not data["sock"]
    record(
        "privilege escalation",
        "NO_PRIVILEGE",
        "NO_PRIVILEGE" if ok else "ESCALATED",
        f"uid {data['uid']}, mount errno {data['mount'][1]}",
    )


SECRET_HUNT = """
import json, os
needle = os.environ.get("MORPH_NEEDLE", "")
hits, scanned = [], 0
for root in ["/app", "/etc", "/home", "/root", "/mnt", "/host", "/media", "/opt", "/srv", "/var"]:
    for base, dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            try:
                if os.path.getsize(path) > 1_000_000:
                    continue
                data = open(path, "rb").read()
            except OSError:
                continue
            scanned += 1
            if needle.encode() in data:
                hits.append(path)
env_dump = json.dumps(dict(os.environ))
print(json.dumps({"hits": hits, "scanned": scanned, "env_has_needle": needle in env_dump,
                  "env_keys": sorted(os.environ)}))
"""


def test_host_secrets_are_not_visible(tmp_path: Path) -> None:
    secret_file = tmp_path.parent / f"{tmp_path.name}-host-secret.txt"
    secret_file.write_text(CANARY)
    # the canary is in the host process environment (autouse fixture) and in a host file next to the bundle
    result = run(runner_with(), tmp_path, SECRET_HUNT, env={"MORPH_NEEDLE": CANARY})
    data = result.json_stdout()
    leaked = [p for p in data["hits"]]
    # the needle itself is passed in as MORPH_NEEDLE, so /proc/self/environ legitimately contains it
    leaked = [p for p in leaked if not p.startswith("/proc/")]
    keys = set(data["env_keys"])
    unexpected = {
        k for k in keys if k.startswith(("HOST_", "CRM_", "SUPPORT_", "GROQ", "ADMIN", "DATABASE"))
    }
    ok = not leaked and not unexpected and CANARY not in result.stdout
    record(
        "secret hunt",
        "NO_SECRETS",
        "NO_SECRETS" if ok else "LEAKED",
        f"{data['scanned']} files scanned, env keys {sorted(keys - {'PATH', 'HOME', 'HOSTNAME', 'LANG', 'GPG_KEY', 'PYTHON_VERSION', 'PYTHON_SHA256', 'PYTHONDONTWRITEBYTECODE', 'PYTHONUNBUFFERED', 'PIP_NO_CACHE_DIR', 'PIP_DISABLE_PIP_VERSION_CHECK'})}",
    )


def test_environment_allowlist_is_enforced_by_the_runner(tmp_path: Path) -> None:
    with pytest.raises(SandboxError):
        run(runner_with(), tmp_path, "print(1)", env={"AWS_SECRET_ACCESS_KEY": "x"})


# ---- network -----------------------------------------------------------------------------------

EGRESS = """
import json, os, socket
def tcp(host, port):
    try:
        s = socket.create_connection((host, port), timeout=3)
        s.close()
        return "CONNECTED"
    except OSError as e:
        return e.errno or str(e)
def dns(name):
    try:
        return socket.gethostbyname(name)
    except OSError as e:
        return e.errno or str(e)
port = int(os.environ["MORPH_LISTENER_PORT"])
targets = {"public_http": ("1.1.1.1", 80), "public_dns": ("8.8.8.8", 53)}
for candidate in os.environ["MORPH_HOST_CANDIDATES"].split(","):
    targets["host:" + candidate] = (candidate, port)
out = {name: tcp(*t) for name, t in targets.items()}
out["dns"] = {n: dns(n) for n in ["example.com", "postgres", "host.docker.internal", "gateway.docker.internal"]}
out["control"] = tcp("crm", 8101) if os.environ.get("MORPH_CONTROL") else "n/a"
print(json.dumps(out))
"""


@dataclass
class Listener:
    port: int
    connections: list[str]


def start_listener() -> tuple[Listener, socket.socket]:
    server = socket.socket()
    server.bind(("0.0.0.0", 0))  # noqa: S104  (reachable from containers on purpose)
    server.listen(8)
    listener = Listener(server.getsockname()[1], [])

    def accept() -> None:
        while True:
            try:
                conn, addr = server.accept()
            except OSError:
                return
            listener.connections.append(addr[0])
            conn.close()

    threading.Thread(target=accept, daemon=True).start()
    return listener, server


def host_candidates() -> str:
    addresses = {"172.17.0.1", "192.168.65.254", "192.168.65.1", "host.docker.internal"}
    try:
        addresses.update(
            str(i[4][0]) for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        )
    except OSError:
        pass
    return ",".join(sorted(addresses))


@pytest.mark.parametrize("mode", ["no network", "internal network with mocks"])
def test_no_egress_to_the_internet_or_the_host(tmp_path: Path, mode: str) -> None:
    listener, server = start_listener()
    try:
        env = {
            "MORPH_LISTENER_PORT": str(listener.port),
            "MORPH_HOST_CANDIDATES": host_candidates(),
        }
        runner = runner_with()
        if mode == "no network":
            result = run(runner, tmp_path, EGRESS, env=env)
        else:
            with MockEnvironment() as mocks:
                result = run(
                    runner,
                    tmp_path,
                    EGRESS,
                    env={**env, "MORPH_CONTROL": "1"},
                    network=mocks.network,
                    run_id=mocks.run_id,
                )
                assert result.json_stdout()["control"] == "CONNECTED", (
                    "positive control: the mocks must be reachable"
                )
        data = result.json_stdout()
        connected = [k for k, v in data.items() if v == "CONNECTED" and k != "control"]
        resolved = {k: v for k, v in data["dns"].items() if isinstance(v, str) and v[:1].isdigit()}
        ok = not connected and not listener.connections and not resolved
        record(
            f"network egress ({mode})", "NETWORK_BLOCKED", "NETWORK_BLOCKED" if ok else "ESCAPED",
            f"public+host connects: {len(connected)}, host listener saw {len(listener.connections)}, public DNS resolved: {len(resolved)}",
        )  # fmt: skip
    finally:
        server.close()


ADMIN_ATTACK = """
import json, os, urllib.request, urllib.error
def call(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError as e:
        return str(e)
key = os.environ["MORPH_CRM_API_KEY"]
out = {}
for name, base in {"crm": os.environ["MORPH_CRM_URL"], "support": os.environ["MORPH_SUPPORT_URL"]}.items():
    for label, headers in {
        "no_token": {},
        "default_token": {"X-Admin-Token": "admin-dev-token"},
        "api_key_as_token": {"X-Admin-Token": key},
        "bearer": {"Authorization": "Bearer " + os.environ["MORPH_SUPPORT_TOKEN"]},
    }.items():
        out[name + ":" + label + ":state"] = call(base + "/__admin/state", headers=headers)
        out[name + ":" + label + ":reset"] = call(base + "/__admin/reset", "POST", headers)
        out[name + ":" + label + ":put"] = call(base + "/__admin/state", "PUT", {**headers, "Content-Type": "application/json"}, b'{"records": []}')
out["crm_api_ok"] = call(os.environ["MORPH_CRM_URL"] + "/customers", headers={"X-API-Key": key})
print(json.dumps(out))
"""


def _admin(url: str, token: str, path: str) -> Any:
    import urllib.request

    request = urllib.request.Request(url + path, headers={"X-Admin-Token": token})  # noqa: S310
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
        return json.loads(response.read())


def test_the_sandbox_cannot_use_the_admin_api(tmp_path: Path) -> None:
    with MockEnvironment() as mocks:
        before = (
            _admin(mocks.crm_admin_url, mocks.crm_admin_token, "/__admin/state"),
            _admin(mocks.support_admin_url, mocks.support_admin_token, "/__admin/state"),
        )
        result = run(
            runner_with(),
            tmp_path,
            ADMIN_ATTACK,
            env=mocks.sandbox_env(),
            network=mocks.network,
            run_id=mocks.run_id,
        )
        data = result.json_stdout()
        admin_calls = {k: v for k, v in data.items() if k != "crm_api_ok"}
        after = (
            _admin(mocks.crm_admin_url, mocks.crm_admin_token, "/__admin/state"),
            _admin(mocks.support_admin_url, mocks.support_admin_token, "/__admin/state"),
        )
        ok = all(v == 401 for v in admin_calls.values()) and before == after
        record(
            "admin api", "ADMIN_DENIED", "ADMIN_DENIED" if ok else "ADMIN_REACHED",
            f"{len(admin_calls)} admin calls all 401, mock state unchanged; normal API still {data['crm_api_ok']}",
        )  # fmt: skip
        assert data["crm_api_ok"] == 200


def test_mock_credentials_are_random_per_run() -> None:
    with MockEnvironment() as a, MockEnvironment() as b:
        assert a.crm_key != b.crm_key and a.crm_admin_token != b.crm_admin_token
        assert a.network != b.network


# ---- hygiene -----------------------------------------------------------------------------------


def test_zz_nothing_is_left_behind() -> None:
    containers = docker(
        "ps", "-a", "--filter", "label=morph.owner=morph", "--format", "{{.Names}}"
    ).stdout.split()
    networks = docker(
        "network", "ls", "--filter", "label=morph.owner=morph", "--format", "{{.Name}}"
    ).stdout.split()
    record(
        "cleanup",
        "NO_LEFTOVERS",
        "NO_LEFTOVERS" if not containers and not networks else "LEFTOVERS",
        f"containers {containers}, networks {networks}",
    )
