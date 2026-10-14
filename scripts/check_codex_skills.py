"""Verify the image's research skills through Codex's local app-server protocol."""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

EXPECTED = frozenset({"dsl", "dolphindb", "data-query", "factor-analysis", "backtest"})


def verify_response(response, skills_root):
    entries = response.get("data", [])
    if len(entries) != 1:
        raise ValueError("Expected exactly one workspace in skills/list")
    entry = entries[0]
    if entry.get("errors"):
        raise ValueError(f"Codex skill discovery failed: {entry['errors']}")
    found = {}
    for skill in entry.get("skills", []):
        name = skill.get("name")
        if name not in EXPECTED:
            continue
        if name in found:
            raise ValueError(f"Duplicate research skill: {name}")
        expected_path = skills_root / name / "SKILL.md"
        if Path(skill["path"]) != expected_path:
            raise ValueError(f"{name} was loaded from an unexpected location: {skill['path']}")
        if skill.get("scope") != "admin" or skill.get("enabled") is not True:
            raise ValueError(f"{name} is not an enabled system skill")
        found[name] = {key: skill[key] for key in ("name", "path", "scope", "enabled")}
    missing = EXPECTED - found.keys()
    if missing:
        raise ValueError(f"Missing research skills: {', '.join(sorted(missing))}")
    return [found[name] for name in sorted(found)]


async def discover(cwd, skills_root, timeout):
    with tempfile.TemporaryDirectory(prefix="solo-codex-skills-") as temporary:
        home = Path(temporary) / "home"
        codex_home = home / ".codex"
        codex_home.mkdir(parents=True)
        env = {name: os.environ[name] for name in ("PATH", "SystemRoot") if name in os.environ}
        env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex_home),
                   TMPDIR=temporary, TMP=temporary, TEMP=temporary, LANG="C.UTF-8")
        process = await asyncio.create_subprocess_exec(
            "codex", "app-server", "--stdio", cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024,
        )

        async def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()

        async def receive(identifier):
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise RuntimeError("Codex exited without a protocol response")
                message = json.loads(line)
                if message.get("id") != identifier:
                    continue
                if "error" in message:
                    raise RuntimeError(f"Codex protocol error: {message['error']}")
                return message["result"]

        try:
            async with asyncio.timeout(timeout):
                await send({"id": 1, "method": "initialize", "params": {
                    "clientInfo": {"name": "solo-skill-check", "version": "1.0.0"},
                    "capabilities": {},
                }})
                await receive(1)
                await send({"method": "initialized", "params": {}})
                await send({"id": 2, "method": "skills/list", "params": {
                    "cwds": [str(cwd)], "forceReload": True,
                }})
                return verify_response(await receive(2), skills_root)
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--skills-root", type=Path, default=Path("/etc/codex/skills"))
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    cwd = args.cwd.resolve()
    if not cwd.is_dir():
        parser.error("--cwd must be an existing directory")
    result = asyncio.run(discover(cwd, args.skills_root.resolve(), args.timeout))
    print(json.dumps({"success": True, "skills": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
