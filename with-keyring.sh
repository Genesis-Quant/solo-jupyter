#!/usr/bin/env bash
set -euo pipefail

keyring_password_file="${JUPYTER_KEYRING_PASSWORD_FILE:-/run/secrets/jupyter-keyring-password}"
if [[ ! -r "$keyring_password_file" || ! -s "$keyring_password_file" ]]; then
    echo "The Jupyter keyring password file is missing, unreadable, or empty." >&2
    exit 1
fi

# Only the Secret Service component is needed; read its unlock password from a file.
gnome-keyring-daemon --unlock --components=secrets --daemonize < "$keyring_password_file" > /dev/null
export PYTHON_KEYRING_BACKEND=keyring.backends.SecretService.Keyring

# Check the collection without opening an interactive unlock prompt.
python -B - <<'PY'
import keyring
import secretstorage

collection = secretstorage.get_default_collection(secretstorage.dbus_init())
if collection.is_locked():
    raise SystemExit("The system keyring is locked; check the keyring password file.")
backend = keyring.get_keyring()
print(f"System keyring ready: {type(backend).__module__}.{type(backend).__name__}", flush=True)
PY

exec "$@"
