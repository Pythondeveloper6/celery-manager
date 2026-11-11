"""Real-process stack (launches examples/full_stack, the shipped example) for the end-to-end happy path.

Starts (as subprocesses) a worker, beat with the database scheduler, the
events listener and the dashboard against a real Redis broker and a real
database, and tears the whole process tree down afterwards.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

E2E_DIR = Path(__file__).parent
ROOT = E2E_DIR.parents[1]
EXAMPLE_DIR = ROOT / "examples" / "full_stack"
BIN = Path(sys.executable).parent
SECRET = "e2e-secret-" + uuid.uuid4().hex
ADMIN_PASSWORD = "e2e-admin-password"


def eventually(fn, timeout=30, interval=0.3, message="condition"):
    """Poll fn() until it returns something truthy; return that value."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if last:
                return last
        except (httpx.HTTPError, AssertionError, KeyError, IndexError) as exc:
            last = exc
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {message}; last={last!r}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _exe(name):
    path = BIN / name
    return str(path) if path.exists() else name


class Stack:
    def __init__(self, workdir, broker_url, db_url):
        self.workdir = workdir
        self.broker_url = broker_url
        self.db_url = db_url
        self.procs = {}
        self.logs = {}
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(EXAMPLE_DIR), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep),
            "CM_EXAMPLE_BROKER_URL": broker_url,
            "CM_EXAMPLE_DB_URL": db_url,
            "CM_EXAMPLE_SECRET": SECRET,
            "CM_EXAMPLE_TICK_SECONDS": "3",
            "PATH": str(BIN) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "FORKED_BY_MULTIPROCESSING": "1",
        }
        self.env.pop("CELERY_BROKER_URL", None)

    def log_tail(self, lines=40):
        out = []
        for name, path in self.logs.items():
            try:
                text = Path(path).read_text(errors="replace").splitlines()[-lines:]
            except OSError:
                text = ["<unreadable>"]
            out.append(f"--- {name} ---\n" + "\n".join(text))
        return "\n".join(out)

    def run(self, args, stdin=None, check=True, timeout=120):
        proc = subprocess.run(
            args, check=False, input=stdin, capture_output=True, text=True, cwd=self.workdir,
            env=self.env, timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"{args} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        return proc

    def cli(self, *args, **kw):
        return self.run([_exe("celery-manager"), *args], **kw)

    def spawn(self, name, args):
        path = self.workdir / f"{name}.log"
        handle = open(path, "wb")  # noqa: SIM115 - owned by the child for its lifetime
        self.logs[name] = path
        self.procs[name] = subprocess.Popen(
            args, cwd=self.workdir, env=self.env, stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return self.procs[name]

    def assert_alive(self):
        for name, proc in self.procs.items():
            if proc.poll() is not None:
                raise AssertionError(f"{name} exited with {proc.returncode}\n{self.log_tail()}")

    def stop_all(self):
        for proc in self.procs.values():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 15
        for proc in self.procs.values():
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()

    def client(self):
        return httpx.Client(base_url=self.base_url, timeout=20)

    def login(self):
        c = self.client()
        r = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        return c


@pytest.fixture(scope="session")
def redis_url():
    env_url = os.environ.get("CM_E2E_REDIS_URL")
    if env_url:
        yield env_url
        return
    if not shutil.which("docker"):
        pytest.skip("e2e needs Docker or CM_E2E_REDIS_URL")
    port = free_port()
    name = "cm-e2e-redis-" + uuid.uuid4().hex[:8]
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:6379", "redis:7-alpine"],
        check=False, capture_output=True, text=True,
    )
    if started.returncode != 0:
        pytest.skip(f"cannot start redis container: {started.stderr.strip()}")
    try:
        def ping():
            r = subprocess.run(["docker", "exec", name, "redis-cli", "ping"], check=False, capture_output=True, text=True)
            return "PONG" in r.stdout
        eventually(ping, timeout=30, message="redis PING")
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


@pytest.fixture(scope="session")
def stack(redis_url, tmp_path_factory):
    workdir = tmp_path_factory.mktemp("cm_e2e")
    db_url = os.environ.get("CM_E2E_DB_URL") or f"sqlite:///{workdir / 'e2e.db'}"
    # Isolate this run's queue/keys from anything else on a shared broker.
    s = Stack(workdir, redis_url, db_url)
    try:
        s.cli("migrate", "--db-url", db_url)
        s.cli("create-user", "admin", "--role", "admin", "--db-url", db_url, "--password-stdin",
              stdin=ADMIN_PASSWORD + "\n")
        s.spawn("worker", [_exe("celery"), "-A", "project", "worker", "--pool=threads", "--concurrency=4",
                           "-E", "-Q", "celery", "--loglevel=INFO", "-n", "demo@%h"])
        s.spawn("events", [_exe("celery-manager"), "events", "--config", "project:config"])
        s.spawn("dashboard", [sys.executable, "-m", "uvicorn", "--factory", "project:make_dashboard",
                              "--host", "127.0.0.1", "--port", str(s.port), "--log-level", "info"])

        def up():
            s.assert_alive()
            return httpx.get(s.base_url + "/api/auth/options", timeout=3).status_code == 200
        eventually(up, timeout=60, message="dashboard up")

        c = s.login()

        def worker_online():
            s.assert_alive()
            return c.get("/api/workers").json()["workers"]
        eventually(worker_online, timeout=60, message="worker online")
        # Start beat last so the schedule table already exists and is migrated.
        s.spawn("beat", [_exe("celery"), "-A", "project", "beat", "-S", "celery_manager.beat:DatabaseScheduler",
                         "-s", str(workdir / "celerybeat-schedule"), "--loglevel=INFO"])
        c.close()
        yield s
    except BaseException as exc:
        tail = s.log_tail()
        s.stop_all()
        raise AssertionError(f"e2e stack failed to start: {exc!r}\n{tail}") from exc
    finally:
        s.stop_all()


@pytest.fixture(scope="session")
def client(stack):
    c = stack.login()
    yield c
    c.close()


@pytest.fixture(scope="session")
def wait_run(client, stack):
    def _wait(task_id, status=None, timeout=40):
        def check():
            r = client.get(f"/api/tasks/{task_id}")
            if r.status_code != 200:
                return None
            d = r.json()
            if status and d["status"] != status:
                return None
            if not status and d["status"] in ("queued", "started"):
                return None
            return d
        try:
            return eventually(check, timeout=timeout, message=f"run {task_id} -> {status}")
        except AssertionError as exc:
            raise AssertionError(f"{exc}\n{stack.log_tail()}") from None
    return _wait


@pytest.fixture(scope="session")
def start_task(client):
    def _start(name, args=None):
        r = client.post("/api/tasks/start", json={"task_name": name, "args": args or []})
        assert r.status_code == 200, r.text
        return r.json()["task_id"]
    return _start


def mcp_session(stack, role):
    """Return a helper that talks JSON-RPC to `celery-manager mcp` over stdio."""
    proc = subprocess.Popen(
        [_exe("celery-manager"), "mcp", "--config", "project:config", "--role", role],
        cwd=stack.workdir, env=stack.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1, start_new_session=True,
    )
    counter = iter(range(1, 10_000))

    def send(method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            msg["id"] = next(counter)
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if notify:
            return None
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("mcp server closed: " + proc.stderr.read())
        return json.loads(line)

    def close():
        try:
            proc.stdin.close()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            os.killpg(proc.pid, signal.SIGKILL)

    return send, close
