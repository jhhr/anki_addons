import json
from pathlib import Path


def is_addon_disabled(meta_json_path: Path) -> bool:
    """Check if an addon is marked as disabled in its meta.json file."""
    try:
        with open(meta_json_path, "r", encoding="utf-8") as f:
            meta_data = json.load(f)
            return meta_data.get("disabled", False)
    except Exception:
        return False  # If we can't read the file, assume it's not disabled
