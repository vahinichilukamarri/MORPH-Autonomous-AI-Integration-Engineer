"""Docker sandbox: runs generated code in a locked-down container, never in this process.

Safety rules enforced here (and tested against hostile modules):

* every container and network created carries ``morph.owner=morph`` and ``morph.run=<id>``;
  only resources this module created, and that still carry those labels, are ever removed;
* no docker socket, no privileged mode, no host network or pid namespace, no host mounts except
  the bundle, mounted read-only;
* nothing is pruned: there is no unscoped ``prune``/``rm`` anywhere in this module.

``SandboxRunner.run`` refuses to execute anything until the configured limits have been
verified to be in force inside a real container (``LimitsNotEnforced`` otherwise).
"""

import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any

SANDBOX_IMAGE = "morph-sandbox:dev"
MOCK_IMAGE = "morph-sbx-mock:dev"
CRM_IMAGE = MOCK_IMAGE
SUPPORT_IMAGE = MOCK_IMAGE
OWNER_KEY = "morph.owner"
OWNER_VALUE = "morph"
RUN_KEY = "morph.run"
SANDBOX_UID = 10001
CRM_PORT = 8101
SUPPORT_PORT = 8102
_ENV_PREFIX = "MORPH_"


class SandboxError(Exception):
    pass


class LimitsNotEnforced(SandboxError):
    """The container did not get the limits that were asked for. Nothing may run."""


class Outcome(StrEnum):
    OK = "OK"
    NONZERO = "NONZERO"
    TIMEOUT = "TIMEOUT"
    OOM = "OOM"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    START_FAILED = "START_FAILED"


@dataclass(frozen=True)
class SandboxLimits:
    memory_mb: int = 256
    cpus: float = 0.5
    pids: int = 64
    timeout_s: float = 60.0
    scratch_mb: int = 32
    tmp_mb: int = 16
    nofile: int = 128
    fsize_mb: int = 10
    output_cap_bytes: int = 1_000_000

    def as_dict(self) -> dict[str, float | int]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass(frozen=True)
class SandboxResult:
    outcome: Outcome
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    container: str
    inspect: dict[str, Any] = field(default_factory=dict)

    def json_stdout(self) -> Any:
        return json.loads(self.stdout)


def docker(
    *args: str, timeout: float = 120.0, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run one docker CLI command (fixed argv, never a shell)."""
    result = subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise SandboxError(f"docker {args[0]} failed: {result.stderr.strip()[:500]}")
    return result


def labels(run_id: str) -> list[str]:
    return ["--label", f"{OWNER_KEY}={OWNER_VALUE}", "--label", f"{RUN_KEY}={run_id}"]


def _label_of(kind: str, name: str, key: str) -> str | None:
    path = "Config.Labels" if kind == "container" else "Labels"
    template = f'{{{{index .{path} "{key}"}}}}'
    result = docker(kind, "inspect", "--format", template, name, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def remove_container(name: str, run_id: str) -> None:
    """Remove a container only if it still carries our owner and run labels."""
    if _label_of("container", name, OWNER_KEY) == OWNER_VALUE and (
        _label_of("container", name, RUN_KEY) == run_id
    ):
        docker("rm", "--force", "--volumes", name, check=False)


def remove_network(name: str, run_id: str) -> None:
    if _label_of("network", name, OWNER_KEY) == OWNER_VALUE and (
        _label_of("network", name, RUN_KEY) == run_id
    ):
        docker("network", "rm", name, check=False)


def remove_labelled_leftovers() -> list[str]:
    """Remove stale resources of earlier crashed runs, selected by our owner label only."""
    removed: list[str] = []
    selector = f"label={OWNER_KEY}={OWNER_VALUE}"
    for line in docker("ps", "-a", "--filter", selector, "--format", "{{.ID}}").stdout.split():
        if docker("rm", "--force", "--volumes", line, check=False).returncode == 0:
            removed.append(line)
    for line in docker("network", "ls", "--filter", selector, "--format", "{{.ID}}").stdout.split():
        if docker("network", "rm", line, check=False).returncode == 0:
            removed.append(line)
    return removed


class _Capture:
    """Reads a pipe in a thread and stops collecting at the cap."""

    def __init__(self, stream: Any, cap: int, on_overflow: Any) -> None:
        self.chunks: list[bytes] = []
        self.size = 0
        self.overflowed = False
        self._cap = cap
        self._on_overflow = on_overflow
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream: Any) -> None:
        for chunk in iter(lambda: stream.read(65536), b""):
            self.size += len(chunk)
            if self.size > self._cap:
                if not self.overflowed:
                    self.overflowed = True
                    self._on_overflow()
                continue  # keep draining so the CLI never blocks, but store nothing more
            self.chunks.append(chunk)

    def text(self) -> str:
        self._thread.join(timeout=5)
        return b"".join(self.chunks).decode("utf-8", errors="replace")


def make_readable(directory: Path) -> None:
    """Let the sandbox user (uid 10001) read a bundle that is mounted read-only.

    On a Linux host a temporary directory is private (mode 0700) and the container's non-root
    user could not even import the code. Directories become 0755 and files 0644; nothing gets
    write or execute rights it did not need.
    """
    if os.name != "posix":
        return
    for root, dirs, files in os.walk(directory):
        os.chmod(root, 0o755)
        for name in dirs:
            os.chmod(Path(root) / name, 0o755)
        for name in files:
            os.chmod(Path(root) / name, 0o644)


class SandboxRunner:
    def __init__(self, image: str = SANDBOX_IMAGE, limits: SandboxLimits | None = None) -> None:
        self.image = image
        self.limits = limits or SandboxLimits()
        self._verified = False

    # -- running ----------------------------------------------------------------------------

    def run(
        self,
        bundle_dir: Path,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        network: str | None = None,
        run_id: str | None = None,
    ) -> SandboxResult:
        """Run ``argv`` in the sandbox with the bundle mounted read-only at /app/bundle."""
        if not self._verified:
            self.verify_limits()
        return self._run(bundle_dir, argv, env=env, network=network, run_id=run_id)

    def _run(
        self,
        bundle_dir: Path,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None,
        network: str | None,
        run_id: str | None,
    ) -> SandboxResult:
        run_id = run_id or uuid.uuid4().hex[:12]
        name = f"morph-sbx-{run_id}-{secrets.token_hex(3)}"
        make_readable(bundle_dir)
        command = self._command(name, run_id, bundle_dir, argv, env or {}, network)
        started = time.monotonic()
        timed_out = False
        process = subprocess.Popen(  # noqa: S603
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL
        )
        assert process.stdout is not None and process.stderr is not None

        def kill() -> None:
            docker("kill", name, check=False, timeout=30)

        def kill_in_background() -> None:
            # the readers must keep draining the pipes while the daemon stops the container
            threading.Thread(target=kill, daemon=True).start()

        cap = self.limits.output_cap_bytes
        out = _Capture(process.stdout, cap, kill_in_background)
        err = _Capture(process.stderr, cap, kill_in_background)
        try:
            try:
                process.wait(timeout=self.limits.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill()
                process.wait(timeout=30)
            inspect = self._inspect(name)
        finally:
            if process.poll() is None:
                process.kill()
            remove_container(name, run_id)
        state = inspect.get("State", {})
        exit_code = state.get("ExitCode") if state else process.returncode
        if timed_out:
            outcome = Outcome.TIMEOUT
        elif state.get("OOMKilled"):
            outcome = Outcome.OOM
        elif out.overflowed or err.overflowed:
            outcome = Outcome.OUTPUT_LIMIT
        elif not state:
            outcome = Outcome.START_FAILED
        elif exit_code == 0:
            outcome = Outcome.OK
        else:
            outcome = Outcome.NONZERO
        return SandboxResult(
            outcome=outcome,
            exit_code=exit_code,
            stdout=out.text(),
            stderr=err.text(),
            duration_s=time.monotonic() - started,
            container=name,
            inspect=inspect,
        )

    def _command(
        self,
        name: str,
        run_id: str,
        bundle_dir: Path,
        argv: Sequence[str],
        env: Mapping[str, str],
        network: str | None,
    ) -> list[str]:
        for key in env:
            if not key.startswith(_ENV_PREFIX):
                raise SandboxError(f"environment variable {key!r} is not allowed in the sandbox")
        limits = self.limits
        command = [
            "docker", "run", "--name", name, *labels(run_id),
            "--log-driver", "none",
            "--read-only",
            "--tmpfs", f"/scratch:rw,noexec,nosuid,nodev,size={limits.scratch_mb}m,mode=1777",
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmp_mb}m,mode=1777",
            "--network", network or "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", str(limits.pids),
            "--memory", f"{limits.memory_mb}m",
            "--memory-swap", f"{limits.memory_mb}m",
            "--cpus", str(limits.cpus),
            "--user", f"{SANDBOX_UID}:{SANDBOX_UID}",
            "--ulimit", f"nofile={limits.nofile}:{limits.nofile}",
            "--ulimit", f"fsize={limits.fsize_mb * 1024 * 1024}:{limits.fsize_mb * 1024 * 1024}",
            "--mount", f"type=bind,source={bundle_dir.resolve()},target=/app/bundle,readonly",
            "-w", "/app/bundle",
        ]  # fmt: skip
        for key, value in env.items():
            command += ["-e", f"{key}={value}"]
        return [*command, self.image, *argv]

    @staticmethod
    def _inspect(name: str) -> dict[str, Any]:
        result = docker("inspect", name, check=False)
        if result.returncode != 0:
            return {}
        parsed = json.loads(result.stdout)
        return dict(parsed[0]) if parsed else {}

    # -- verification -----------------------------------------------------------------------

    def verify_limits(self) -> dict[str, Any]:
        """Run a probe and check that every limit is in force. Raises LimitsNotEnforced."""
        with tempfile.TemporaryDirectory(prefix="morph-probe-") as tmp:
            package = Path(tmp) / "integration"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "__main__.py").write_text(PROBE_SOURCE)
            result = self._run(
                Path(tmp), ["python", "-E", "-s", "-B", "-m", "integration"],
                env={}, network=None, run_id=None,
            )  # fmt: skip
        problems = self.check_probe(result)
        if problems:
            raise LimitsNotEnforced("; ".join(problems))
        self._verified = True
        return {"probe": json.loads(result.stdout), "limits": self.limits.as_dict()}

    def check_probe(self, result: SandboxResult) -> list[str]:
        limits = self.limits
        problems: list[str] = []
        if result.outcome is not Outcome.OK:
            return [f"probe did not run: {result.outcome.value} {result.stderr[:200]}"]
        probe = json.loads(result.stdout)
        host = result.inspect.get("HostConfig", {})
        expected_host = {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "NetworkMode": "none",
            "PidsLimit": limits.pids,
            "Memory": limits.memory_mb * 1024 * 1024,
            "MemorySwap": limits.memory_mb * 1024 * 1024,
            "NanoCpus": int(limits.cpus * 1_000_000_000),
            "CapDrop": ["ALL"],
            "PidMode": "",
        }
        for key, value in expected_host.items():
            if host.get(key) != value:
                problems.append(f"HostConfig.{key} is {host.get(key)!r}, expected {value!r}")
        if "no-new-privileges" not in str(host.get("SecurityOpt")):
            problems.append("no-new-privileges is not set")
        binds = [m for m in result.inspect.get("Mounts", []) if m.get("Type") == "bind"]
        if (
            len(binds) != 1
            or binds[0].get("RW") is not False
            or binds[0].get("Destination") != "/app/bundle"
        ):
            problems.append(f"unexpected bind mounts: {binds}")
        in_container = {
            "uid": SANDBOX_UID,
            "cap_eff": "0000000000000000",
            "no_new_privs": 1,
            "root_writable": False,
            "scratch_writable": True,
            "docker_socket": False,
            "env_keys": [],
        }
        for key, value in in_container.items():
            if probe.get(key) != value:
                problems.append(f"in-container {key} is {probe.get(key)!r}, expected {value!r}")
        if probe.get("memory_limit") != limits.memory_mb * 1024 * 1024:
            problems.append(f"memory cgroup limit is {probe.get('memory_limit')!r}")
        if probe.get("pids_limit") != limits.pids:
            problems.append(f"pids cgroup limit is {probe.get('pids_limit')!r}")
        expected_cpu = limits.cpus
        cpu = probe.get("cpu_cores")
        if cpu is None or abs(cpu - expected_cpu) > 0.01:
            problems.append(f"cpu quota is {cpu!r} cores, expected {expected_cpu}")
        if probe.get("interfaces") != ["lo"]:
            problems.append(f"network interfaces are {probe.get('interfaces')!r}, expected only lo")
        options = probe.get("tmpfs_options", {})
        for mount in ("/scratch", "/tmp"):
            flags = options.get(mount, "")
            if not all(f in flags.split(",") for f in ("noexec", "nosuid", "nodev")):
                problems.append(f"{mount} mount options are {flags!r}")
        return problems


PROBE_SOURCE = """
import json, os, re

def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None

def first(*paths):
    for p in paths:
        v = read(p)
        if v is not None:
            return v

def number(text):
    return None if text in (None, "max") else int(text)

status = read("/proc/self/status") or ""
def status_field(name):
    m = re.search(r"^" + name + r":\\s*(.*)$", status, re.M)
    return m.group(1).strip() if m else None

mem = number(first("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"))
pids = number(first("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max"))
cpu = first("/sys/fs/cgroup/cpu.max")
if cpu:
    quota, period = cpu.split()
    cores = None if quota == "max" else int(quota) / int(period)
else:
    quota = number(read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
    period = number(read("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
    cores = quota / period if quota and quota > 0 and period else None

def writable(path):
    try:
        with open(path, "w"):
            pass
        os.unlink(path)
        return True
    except OSError:
        return False

mounts = {}
for line in (read("/proc/mounts") or "").splitlines():
    parts = line.split()
    if len(parts) >= 4 and parts[1] in ("/scratch", "/tmp"):
        mounts[parts[1]] = parts[3]

print(json.dumps({
    "uid": os.getuid(),
    "cap_eff": status_field("CapEff"),
    "no_new_privs": int(status_field("NoNewPrivs") or -1),
    "root_writable": writable("/probe-root"),
    "scratch_writable": writable("/scratch/probe"),
    "docker_socket": os.path.exists("/var/run/docker.sock"),
    "env_keys": sorted(k for k in os.environ if k.startswith("MORPH_")),
    "memory_limit": mem,
    "pids_limit": pids,
    "cpu_cores": cores,
    "interfaces": sorted(
        line.split(":")[0].strip() for line in (read("/proc/net/dev") or "").splitlines()[2:]
    ),
    "tmpfs_options": mounts,
}))
"""


# -- ephemeral mock systems ---------------------------------------------------------------------


def _http(url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 3.0) -> int:
    request = urllib.request.Request(url, headers=dict(headers or {}))  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0


class MockEnvironment:
    """Fresh CRM and Support mocks on a throwaway internal network, with random credentials.

    The sandbox joins the internal network (no gateway, no internet) and reaches the mocks as
    ``crm`` and ``support``. The mocks also publish loopback ports so trusted host code can seed
    state and read the request log through the admin interface. The admin tokens never enter
    the sandbox.
    """

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.network = f"morph-sbx-net-{self.run_id}"
        self.crm_key = secrets.token_hex(16)
        self.support_token = secrets.token_hex(16)
        self.crm_admin_token = secrets.token_hex(16)
        self.support_admin_token = secrets.token_hex(16)
        self.crm_host_port = 0
        self.support_host_port = 0
        self._containers: list[str] = []
        self._network_created = False

    @property
    def crm_admin_url(self) -> str:
        return f"http://127.0.0.1:{self.crm_host_port}"

    @property
    def support_admin_url(self) -> str:
        return f"http://127.0.0.1:{self.support_host_port}"

    def sandbox_env(self) -> dict[str, str]:
        return {
            "MORPH_CRM_URL": f"http://crm:{CRM_PORT}",
            "MORPH_CRM_API_KEY": self.crm_key,
            "MORPH_SUPPORT_URL": f"http://support:{SUPPORT_PORT}",
            "MORPH_SUPPORT_TOKEN": self.support_token,
        }

    def credential(self, system: str) -> str:
        return self.crm_key if system == "crm" else self.support_token

    def role_env(
        self,
        source: str,
        target: str,
        *,
        source_credential: str | None = None,
        target_credential: str | None = None,
    ) -> dict[str, str]:
        """Environment for a generated integration: ``source`` and ``target`` are system names.

        A credential of ``""`` leaves that variable out entirely (to test missing credentials).
        """
        port = {"crm": CRM_PORT, "support": SUPPORT_PORT}
        env = {
            "MORPH_SOURCE_URL": f"http://{source}:{port[source]}",
            "MORPH_TARGET_URL": f"http://{target}:{port[target]}",
        }
        for role, system, override in (
            ("SOURCE", source, source_credential),
            ("TARGET", target, target_credential),
        ):
            value = self.credential(system) if override is None else override
            if value:
                env[f"MORPH_{role}_CREDENTIAL"] = value
        return env

    def admin_url(self, system: str) -> str:
        return self.crm_admin_url if system == "crm" else self.support_admin_url

    def admin_token(self, system: str) -> str:
        return self.crm_admin_token if system == "crm" else self.support_admin_token

    def __enter__(self) -> "MockEnvironment":
        try:
            docker("network", "create", "--internal", *labels(self.run_id), self.network)
            self._network_created = True
            self.crm_host_port = self._start(
                "crm", CRM_IMAGE, CRM_PORT, "crm.main:app",
                {"CRM_API_KEY": self.crm_key, "ADMIN_TOKEN": self.crm_admin_token},
            )  # fmt: skip
            self.support_host_port = self._start(
                "support", SUPPORT_IMAGE, SUPPORT_PORT, "support.main:app",
                {"SUPPORT_TOKEN": self.support_token, "ADMIN_TOKEN": self.support_admin_token},
            )  # fmt: skip
            self._wait_ready(self.crm_host_port)
            self._wait_ready(self.support_host_port)
        except BaseException:
            self.cleanup()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.cleanup()

    def _start(self, alias: str, image: str, port: int, app: str, env: dict[str, str]) -> int:
        name = f"morph-sbx-{alias}-{self.run_id}"
        command = [
            "run", "-d", "--name", name, *labels(self.run_id),
            "--log-driver", "none",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "128", "--memory", "256m", "--memory-swap", "256m",
            "-p", f"127.0.0.1::{port}",
        ]  # fmt: skip
        for key, value in env.items():
            command += ["-e", f"{key}={value}"]
        command += [image, "uvicorn", app, "--host", "0.0.0.0", "--port", str(port)]
        docker(*command)
        self._containers.append(name)
        docker("network", "connect", "--alias", alias, self.network, name)
        mapping = docker("port", name, f"{port}/tcp").stdout.strip().splitlines()[0]
        return int(mapping.rsplit(":", 1)[1])

    @staticmethod
    def _wait_ready(port: int, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _http(f"http://127.0.0.1:{port}/openapi.json") == 200:
                return
            time.sleep(0.25)
        raise SandboxError(f"mock on port {port} did not become ready")

    def cleanup(self) -> None:
        for name in reversed(self._containers):
            remove_container(name, self.run_id)
        self._containers = []
        if self._network_created:
            remove_network(self.network, self.run_id)
            self._network_created = False
