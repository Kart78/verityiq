"""Local secret store for credentials typed into Settings.

Order of preference:
  1. the operating system's credential vault via `keyring` (Windows Credential Manager, macOS Keychain)
  2. fallback: data/secrets.json next to the app database (readable only by the current user where supported)

Config files only ever hold a reference such as ${secret:prod.data.client_secret}; the value is
resolved on the server when a connector is created and is never returned to the browser.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

SERVICE = "bi-qa-assistant"


def _file() -> Path:
    from .environment import ROOT
    return Path(os.environ.get("BIQA_SECRETS_FILE", ROOT / "data" / "secrets.json"))


def _keyring():
    try:
        import keyring  # type: ignore
        kr = keyring.get_keyring()
        if "fail" in type(kr).__name__.lower() or "null" in type(kr).__name__.lower():
            return None
        return keyring
    except Exception:  # noqa: BLE001
        return None


def backend() -> str:
    return "os-vault" if _keyring() else "local-file"


def put_secret(name: str, value: str) -> str:
    kr = _keyring()
    if kr:
        kr.set_password(SERVICE, name, value)
    else:
        f = _file()
        f.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(f.read_text()) if f.exists() else {}
        data[name] = value
        f.write_text(json.dumps(data))
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
    return "${secret:" + name + "}"


def get_secret(name: str) -> Optional[str]:
    kr = _keyring()
    if kr:
        v = kr.get_password(SERVICE, name)
        if v is not None:
            return v
    f = _file()
    if f.exists():
        return json.loads(f.read_text()).get(name)
    return None
