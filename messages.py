def get_read_configs_message(
    loaded_addons: list[str], disabled_addons: list[str], missing_addons: list[str]
) -> str:
    """Get feedback message after reading configs"""
    message = ""
    if loaded_addons:
        message += f"<b>✓ Loaded {len(loaded_addons)} addon config(s):</b><br>"
        for addon_id in loaded_addons[:10]:  # Show first 10
            status = " <i>(will be disabled)</i>" if addon_id in disabled_addons else ""
            message += f"&nbsp;&nbsp;• {addon_id}{status}<br>"
        if len(loaded_addons) > 10:
            message += f"&nbsp;&nbsp;• ... and {len(loaded_addons) - 10} more<br>"
        message += "<br>"

        if disabled_addons:
            message += (
                f"<i>Note: {len(disabled_addons)} addon(s) are marked as disabled in the synced"
                " config.</i><br><br>"
            )

    if missing_addons:
        message += (
            f"<b>⚠ Found {len(missing_addons)} addon config(s) but addon(s) not installed:</b><br>"
        )
        message += "<i>Install these addons first, then run Read Configs again.</i><br><br>"
        message += "<b>Addon codes to install:</b><br>"
        for addon_id in missing_addons:
            message += f"&nbsp;&nbsp;• <b>{addon_id}</b><br>"
        message += "<br>"
        message += (
            "<i>To install: Go to Tools → Add-ons → Get Add-ons...<br>and enter each code"
            " above.</i><br><br>"
        )

    if loaded_addons:
        message += (
            "<i>Some addons may require a restart of Anki to apply the loaded configs and"
            " enable/disable states.</i>"
        )

    return message


def get_save_configs_message(
    saved_addons: list[str],
    disabled_addons: list[str],
    skipped_addons: list[str],
    is_menu_action: bool = False,
) -> str:
    """Get feedback message after saving configs"""
    message = ""

    if saved_addons:
        message += f"<b>✓ Saved {len(saved_addons)} addon config(s):</b><br>"
        for addon_id in saved_addons[:10]:  # Show first 10
            status = " <i>(disabled)</i>" if addon_id in disabled_addons else ""
            message += f"&nbsp;&nbsp;• {addon_id}{status}<br>"
        if len(saved_addons) > 10:
            message += f"&nbsp;&nbsp;• ... and {len(saved_addons) - 10} more<br>"
        message += "<br>"

        if disabled_addons:
            message += (
                f"<i>Note: {len(disabled_addons)} addon(s) are disabled and will sync as"
                " disabled.</i><br><br>"
            )

    if skipped_addons:
        message += f"<b>⊘ Skipped {len(skipped_addons)} addon(s) (no meta.json):</b><br>"
        for addon_id in skipped_addons[:5]:  # Show first 5
            message += f"&nbsp;&nbsp;• {addon_id}<br>"
        if len(skipped_addons) > 5:
            message += f"&nbsp;&nbsp;• ... and {len(skipped_addons) - 5} more<br>"
        message += "<br>"

    if is_menu_action:
        message += "<i>Remember to sync to upload configs to AnkiWeb!</i>"

    return message
