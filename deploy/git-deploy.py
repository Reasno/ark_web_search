#!/usr/bin/env python3
"""Deploy a published commit to a Docker Home Assistant host over verified SSH."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def run(*command, cwd=None):
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def wait_ready():
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:8123/api/", timeout=3).close()
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("Home Assistant API did not become ready")


def receive(args):
    source = Path(args.source)
    source.mkdir(parents=True, exist_ok=True)
    if not (source / ".git").exists():
        if any(source.iterdir()):
            raise RuntimeError("Source directory is not empty and is not a Git checkout")
        run("git", "init", "-q", str(source))
    if run("git", "status", "--porcelain", cwd=source):
        raise RuntimeError("Remote source checkout has uncommitted changes")
    run("git", "fetch", args.bundle, "HEAD", cwd=source)
    revision = run("git", "rev-parse", "FETCH_HEAD", cwd=source)
    run("git", "checkout", "--detach", revision, cwd=source)
    config = Path(args.config)
    target = config / "custom_components/ark_web_search"
    backup = config / ".deployment-backups" / f"ark-{time.time_ns()}"
    backup.parent.mkdir(mode=0o700, exist_ok=True)
    backup.parent.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix=".ark-preflight-", dir=config) as temporary:
        stage = Path(temporary)
        for directory in ("custom_components", "tools", "tests"):
            shutil.copytree(source / directory, stage / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        container_stage = f"/config/{stage.name}"
        subprocess.run([
            "docker", "exec", "homeassistant", "python",
            f"{container_stage}/tools/verify_content_runtime.py",
        ], check=True)
        subprocess.run([
            "docker", "exec", "homeassistant", "python", "-m", "unittest", "discover",
            "-s", f"{container_stage}/tests", "-q",
        ], check=True)
        installed = False
        existed = target.exists()
        try:
            if existed:
                target.rename(backup)
            installed = True
            shutil.copytree(stage / "custom_components/ark_web_search", target)
            run("docker", "restart", "homeassistant")
            wait_ready()
        except BaseException:
            if installed:
                shutil.rmtree(target, ignore_errors=True)
                if existed:
                    backup.rename(target)
                run("docker", "restart", "homeassistant")
            raise
    print(json.dumps({"commit": revision, "backup": str(backup), "ha_api": "ready"}))


def deploy(args):
    root = Path(__file__).resolve().parents[1]
    if run("git", "status", "--porcelain", cwd=root):
        raise RuntimeError("Commit all changes before deployment")
    revision = run("git", "rev-parse", "HEAD", cwd=root)
    published = run("git", "ls-remote", "origin", "refs/heads/main", cwd=root).split()
    if not published or published[0] != revision:
        raise RuntimeError("Push HEAD to origin/main before deployment")
    options = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    if args.proxy:
        options += ["-o", f"ProxyCommand={args.proxy}"]
    ssh = ["ssh", *options, args.host]
    remote_dir = run(*ssh, "mktemp -d /tmp/ark-git.XXXXXXXX")
    try:
        with tempfile.TemporaryDirectory(prefix="ark-git-") as temporary:
            bundle = Path(temporary) / "source.bundle"
            run("git", "bundle", "create", str(bundle), "HEAD", cwd=root)
            run("scp", "-q", *options, str(bundle), f"{args.host}:{remote_dir}/source.bundle")
            command = shlex.join([
                "python3", "-", "--receive", "--source", args.source,
                "--config", args.config, "--bundle", f"{remote_dir}/source.bundle",
            ])
            subprocess.run([*ssh, command], input=Path(__file__).read_text(), text=True, check=True)
    finally:
        run(*ssh, shlex.join(["rm", "-rf", "--", remote_dir]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="root@homeassistant.tail023c3.ts.net")
    parser.add_argument("--proxy", default="/usr/local/bin/home nc %h %p")
    parser.add_argument("--source", default="/opt/ark-web-search")
    parser.add_argument("--config", default="/var/lib/homeassistant/homeassistant")
    parser.add_argument("--receive", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--bundle", help=argparse.SUPPRESS)
    args = parser.parse_args()
    receive(args) if args.receive else deploy(args)
