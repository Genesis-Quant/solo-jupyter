"""Read Solo's configured skill roots in the ACP adapter's existing refresh hook."""

import argparse
import json
import re
import shutil
from pathlib import Path

CONFIG = Path("/opt/jupyter-codex/skills.json")
PATTERN = re.compile(
    r'(?P<indent> +)const skillExtraRoots = additionalRoots\.map\(\(root\) => '
    r'(?P<path>\w+)\.join\(root, "\.agents", "skills"\)\);'
)
MARKER = "const soloConfiguredSkillRoots = JSON.parse("


def patch(entry: Path, config: Path = CONFIG) -> bool:
    source = entry.read_text(encoding="utf-8")
    if MARKER in source:
        return False
    matches = list(PATTERN.finditer(source))
    if len(matches) != 1:
        raise RuntimeError("Codex ACP skill refresh hook changed; update the integration before installing")
    match = matches[0]
    indent, path = match["indent"], match["path"]
    replacement = (
        f'{indent}{MARKER}await (await import("node:fs/promises"))'
        f'.readFile({json.dumps(str(config))}, "utf8")).extraRoots;\n'
        f'{indent}const skillExtraRoots = [...new Set([\n'
        f'{indent}  ...additionalRoots.map((root) => {path}.join(root, ".agents", "skills")),\n'
        f'{indent}  ...soloConfiguredSkillRoots\n'
        f'{indent}])];'
    )
    entry.write_text(source[:match.start()] + replacement + source[match.end():], encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", type=Path)
    args = parser.parse_args()
    entry = args.entry
    if entry is None:
        executable = shutil.which("codex-acp")
        if executable is None:
            parser.error("codex-acp is not installed")
        entry = Path(executable).resolve()
    changed = patch(entry)
    print(json.dumps({"entry": str(entry), "config": str(CONFIG), "updated": changed}))


if __name__ == "__main__":
    main()
