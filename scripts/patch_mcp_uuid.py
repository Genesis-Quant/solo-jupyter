"""按 UUID 调用特征修复命令工具包，保留原资产并更新加载引用。"""

import argparse
import json
import sys
from pathlib import Path


PACKAGE_VERSION = "0.2.0"
OLD_UUID = 'const n=crypto.randomUUID(),i=new a.Token("jupyterlab-commands-toolkit:IWebClientId")'
NEW_UUID = 'const n=a.UUID.uuid4(),i=new a.Token("jupyterlab-commands-toolkit:IWebClientId")'
LUMINO_MODULE = '262:()=>g("default","@lumino/coreutils",!1,[1,2,0,0])'
REMOTE_NAME = "remoteEntry.solo-uuid.js"


def patch_toolkit(root):
    root = Path(root)
    package_path = root / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    if package.get("name") != "jupyterlab-commands-toolkit" or package.get("version") != PACKAGE_VERSION:
        raise RuntimeError("UUID patch requires jupyterlab-commands-toolkit 0.2.0")
    static = root / "static"
    chunks = [(path, content) for path in static.glob("*.js")
              if OLD_UUID in (content := path.read_text(encoding="utf-8"))]
    (chunk_path, chunk), = chunks
    if chunk.count(OLD_UUID) != 1 or 'var a=o(262),' not in chunk:
        raise RuntimeError("Toolkit UUID expression or Lumino module binding changed")
    chunk_id, reference = chunk_path.stem.split(".", 1)
    chunk_name = f"{chunk_id}.solo-uuid.js"
    expected_load = f"static/{REMOTE_NAME}"
    build = package["jupyterlab"]["_build"]
    if build["load"] == expected_load:
        return {"package_version": PACKAGE_VERSION, "uuid_generator": "@lumino/coreutils.UUID.uuid4",
                "chunk": chunk_name, "remote": REMOTE_NAME, "original_assets_preserved": True}
    remote_path = root / build["load"]
    if remote_path.parent != static or not remote_path.name.startswith("remoteEntry."):
        raise RuntimeError("Toolkit entry point was modified by another patch")
    remote = remote_path.read_text(encoding="utf-8")
    if LUMINO_MODULE not in remote or reference not in remote:
        raise RuntimeError("Toolkit chunk reference or Lumino module binding changed")
    with (static / chunk_name).open("x", encoding="utf-8") as stream:
        stream.write(chunk.replace(OLD_UUID, NEW_UUID))
    with (static / REMOTE_NAME).open("x", encoding="utf-8") as stream:
        stream.write(remote.replace(reference, "solo-uuid"))
    build["load"] = expected_load
    temporary = root / "package.json.solo-uuid.tmp"
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(package, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(package_path)
    return {"package_version": PACKAGE_VERSION, "uuid_generator": "@lumino/coreutils.UUID.uuid4",
            "chunk": chunk_name, "remote": REMOTE_NAME, "original_assets_preserved": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path,
                        default=Path(sys.prefix) / "share/jupyter/labextensions/jupyterlab-commands-toolkit")
    arguments = parser.parse_args()
    print(json.dumps(patch_toolkit(arguments.root)))
