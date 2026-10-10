"""修复固定版本命令工具包的 UUID 调用，并为新静态资源生成缓存版本。"""

import argparse
import hashlib
import json
import sys
from pathlib import Path


PACKAGE_VERSION = "0.2.0"
CHUNK_NAME = "509.d3237d7e45401cb7bbcc.js"
REMOTE_NAME = "remoteEntry.9bb0fbe7c46fa3784c70.js"
CHUNK_SHA256 = "d3237d7e45401cb7bbcc01b7de165b24438a7adf626d54079afe6eec65e9137d"
REMOTE_SHA256 = "9bb0fbe7c46fa3784c7017519736fa82acdabfdd953b50390703f03118e0c308"
OLD_UUID = 'const n=crypto.randomUUID(),i=new a.Token("jupyterlab-commands-toolkit:IWebClientId")'
NEW_UUID = 'const n=a.UUID.uuid4(),i=new a.Token("jupyterlab-commands-toolkit:IWebClientId")'
LUMINO_MODULE = '262:()=>g("default","@lumino/coreutils",!1,[1,2,0,0])'


def _verified(path, digest):
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise RuntimeError(f"Unexpected toolkit asset: {path.name}; revalidate the upstream version before patching")
    return content.decode("utf-8")


def _write_new(path, content):
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(f"Refusing to replace an unrelated asset: {path.name}")
        return
    path.write_bytes(content)


def patch_toolkit(root):
    root = Path(root)
    package_path = root / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    if package.get("name") != "jupyterlab-commands-toolkit" or package.get("version") != PACKAGE_VERSION:
        raise RuntimeError("UUID patch requires jupyterlab-commands-toolkit 0.2.0")
    static = root / "static"
    chunk = _verified(static / CHUNK_NAME, CHUNK_SHA256)
    remote = _verified(static / REMOTE_NAME, REMOTE_SHA256)
    if chunk.count(OLD_UUID) != 1 or 'var a=o(262),' not in chunk or remote.count(LUMINO_MODULE) != 1:
        raise RuntimeError("Toolkit UUID expression or Lumino module binding changed")
    updated_chunk = chunk.replace(OLD_UUID, NEW_UUID).encode("utf-8")
    chunk_hash = hashlib.sha256(updated_chunk).hexdigest()[:20]
    updated_chunk_name = f"509.{chunk_hash}.js"
    old_hash = CHUNK_NAME.split(".")[1]
    if remote.count(old_hash) != 2:
        raise RuntimeError("Toolkit chunk cache references changed")
    updated_remote = remote.replace(old_hash, chunk_hash).encode("utf-8")
    remote_hash = hashlib.sha256(updated_remote).hexdigest()[:20]
    updated_remote_name = f"remoteEntry.{remote_hash}.js"
    build = package["jupyterlab"]["_build"]
    expected_load = f"static/{updated_remote_name}"
    if build.get("load") not in {f"static/{REMOTE_NAME}", expected_load}:
        raise RuntimeError("Toolkit entry point was modified by another patch")
    _write_new(static / updated_chunk_name, updated_chunk)
    _write_new(static / updated_remote_name, updated_remote)
    build["load"] = expected_load
    serialized = (json.dumps(package, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if package_path.read_bytes() != serialized:
        temporary = root / "package.json.solo-uuid.tmp"
        if temporary.exists():
            raise RuntimeError("Unsettled toolkit UUID patch temporary file")
        temporary.write_bytes(serialized)
        temporary.replace(package_path)
    return {"package_version": PACKAGE_VERSION, "uuid_generator": "@lumino/coreutils.UUID.uuid4",
            "chunk": updated_chunk_name, "remote": updated_remote_name,
            "original_assets_preserved": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path,
                        default=Path(sys.prefix) / "share/jupyter/labextensions/jupyterlab-commands-toolkit")
    arguments = parser.parse_args()
    print(json.dumps(patch_toolkit(arguments.root)))
