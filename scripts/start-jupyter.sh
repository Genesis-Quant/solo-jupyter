#!/usr/bin/env bash
set -euo pipefail

umask 0002
seed=/opt/solo-defaults
mkdir -p "$HOME"/{projects,runs,artifacts} "$HOME/.jupyter/kernels" "$HOME/.local/share/uv/python" "$HOME/.agents/skills"
if [[ ! -f "$HOME/.bashrc" && -f /etc/skel/.bashrc ]]; then
    cp /etc/skel/.bashrc "$HOME/.bashrc"
fi

# uv resolves this directory from HOME; no installation-directory override.
# Skip an existing interpreter directory as a unit, including user modifications.
for python in "$seed/uv-python"/*; do
    [[ -e "$python" ]] || continue
    target="$HOME/.local/share/uv/python/${python##*/}"
    if [[ ! -e "$target" && ! -L "$target" ]]; then
        cp -a "$python" "$target"
    fi
done

# Copy initial settings only when HOME has no settings directory at all.
if [[ ! -e "$HOME/.jupyter/lab/user-settings" && ! -L "$HOME/.jupyter/lab/user-settings" ]]; then
    mkdir -p "$HOME/.jupyter/lab"
    cp -a "$seed/jupyter-settings" "$HOME/.jupyter/lab/user-settings"
fi

# OMP discovers standard ~/.agents/skills. Keep defaults in the image and
# expose missing skills without overwriting user-authored skill directories.
for skill in "$seed/skills"/*; do
    [[ -d "$skill" ]] || continue
    target="$HOME/.agents/skills/${skill##*/}"
    if [[ ! -e "$target" && ! -L "$target" ]]; then
        ln -s "$skill" "$target"
    fi
done

cd "$HOME"
exec start-notebook.py --ServerApp.allow_root=True --ServerApp.root_dir="$HOME" --ServerApp.default_url=/lab "$@"
