"""Atomic JSON state and log redaction. No network and no secrets in the file format."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def redact_text(text: str, secrets: list[str] | tuple[str, ...] | None) -> str:
    """Replace known secret values. Short strings are ignored so blanks cannot wipe logs."""
    if not text or not secrets:
        return text
    out = text
    for secret in secrets:
        if not secret or not isinstance(secret, str):
            continue
        if len(secret) < 8:
            continue
        if secret in out:
            out = out.replace(secret, "[redacted]")
    return out


def redact_obj(obj, secrets):
    if isinstance(obj, str):
        return redact_text(obj, secrets)
    if isinstance(obj, dict):
        return {k: redact_obj(v, secrets) for k, v in obj.items() if k not in _SECRET_KEYS}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, secrets) for v in obj]
    return obj


_SECRET_KEYS = {
    "signature",
    "private_key",
    "api_secret",
    "secret",
    "passphrase",
    "apiKey",
    "api_key",
}


def replace_is_locked(exc: BaseException) -> bool:
    """True when a replace failed because another process still has the destination open.

    Windows denies os.replace in that case. Other errors, such as a full disk, are not locks.
    """
    if isinstance(exc, PermissionError):
        return True
    winerror = getattr(exc, "winerror", None)
    return winerror in {5, 32}


def atomic_write(path: str | os.PathLike, obj, retries: int = 8) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(obj, default=str, separators=(",", ":")).encode("utf-8")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    delay = 0.02
    last = None
    for attempt in range(max(1, retries)):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last = exc
            if not replace_is_locked(exc) or attempt + 1 == max(1, retries):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)
    if last is not None:
        raise last


def read_json(path: str | os.PathLike, default=None):
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
        return default


def load_env_file(path: str | os.PathLike, environ: dict | None = None) -> list[str]:
    """Load KEY=VALUE pairs into environ without overriding variables already set.

    Returns the names that were set. Values are never returned.
    """
    environ = os.environ if environ is None else environ
    path = Path(path)
    set_names: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set_names
    except OSError:
        return set_names
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or name in environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        environ[name] = value
        set_names.append(name)
    return set_names
