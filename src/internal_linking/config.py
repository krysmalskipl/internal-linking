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
    cfg = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    path = config_path(explicit)
    if explicit and not path.exists():
        print(f"  ! {path} does not exist yet - using the default thresholds")
    if path != DEFAULT_PATH and path.exists():
        try:
            own = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            raise RuntimeError(f"{path} is not valid JSON ({e}) - fix it or delete it to use the defaults") from None
        if not isinstance(own, dict):
            raise RuntimeError(f"{path} must hold an object like {{\"score_min\": 0.9}}")
        cfg.update(own)
    return cfg


def save_config(cfg: dict, explicit: str | None = None) -> Path:
    """Writes to --config or ./config.json - never over the packaged defaults."""
    path = Path(explicit) if explicit else Path.cwd() / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
