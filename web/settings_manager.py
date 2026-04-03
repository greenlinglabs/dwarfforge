import json
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULTS: dict = {
    "save_destination":   "local",
    "network_share_path": "",
    "share_type":         "smb",
    "smb_host":           "",
    "smb_username":       "",
    "smb_password":       "",
    "auto_mount":         False,
}


def get_settings() -> dict:
    """Return current settings merged with defaults. Adds computed save_destination_path."""
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
            result = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
        except (json.JSONDecodeError, OSError):
            result = dict(DEFAULTS)
    else:
        result = dict(DEFAULTS)

    # Computed field — not persisted to disk.
    # For both local and network, saves land in SAVES_DIR (/saves).
    # Network shares are mounted *to* /saves at boot by entrypoint.sh;
    # network_share_path / smb_host are only used in the mount command.
    result["save_destination_path"] = os.environ.get("SAVES_DIR", "/saves")

    return result


def save_settings(data: dict) -> None:
    """Write only known settings keys to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current = get_settings()
    for k in DEFAULTS:
        if k in data:
            current[k] = data[k]
    to_write = {k: current[k] for k in DEFAULTS}
    SETTINGS_FILE.write_text(json.dumps(to_write, indent=2))
