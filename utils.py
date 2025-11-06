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


def get_existing_addons(anki_addons_path: Path) -> set[str]:
    """Get a set of existing addon IDs in the Anki addons folder."""
    return {addon_dir.name for addon_dir in anki_addons_path.iterdir() if addon_dir.is_dir()}


def get_configs_in_media(media_path: Path) -> list[str]:
    """Get a list of addon IDs that have synced config files in the media folder."""

    synced_config_files = [f for f in media_path.glob("_*_meta.json")]
    # Remove leading _ and trailing _meta.json
    synced_addon_ids = [f.name[1:-10] for f in synced_config_files]
    return synced_addon_ids
