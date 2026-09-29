"""Thresholds and limits. Defaults ship with the package (config.json next to this file);
a config.json in the working directory, or one passed with --config, overrides them."""
import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("config.json")


def config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    local = Path.cwd() / "config.json"
    return local if local.exists() else DEFAULT_PATH


def load_config(explicit: str | None = None) -> dict:
    cfg = json.loads(DEFAULT_PATH.read_text())
    path = config_path(explicit)
    if path != DEFAULT_PATH and path.exists():
        cfg.update(json.loads(path.read_text()))
    return cfg


def save_config(cfg: dict, explicit: str | None = None) -> Path:
    """Writes to --config or ./config.json - never over the packaged defaults."""
    path = Path(explicit) if explicit else Path.cwd() / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return path
