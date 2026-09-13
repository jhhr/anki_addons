# Chain smoke test — link 1

- **session id**: `session_01WDQeJcewHvUx8P3P5V5m4e`
- **environment_id**: `env_016oALseyGCyn22N2VWv8x3E` (kind: `anthropic_cloud`, parent session: `session_019nUGHpZXXGEvMAHBnES1XQ`)
- **pwd**: `/home/user/anki_addons`
- **git rev-parse --abbrev-ref HEAD**: `claude/brave-gates-a5eefx`
- **git log --oneline -1**: `d9e1353 Merge pull request #10 from jhhr/copilot/jnaio-auto-rate-limit`
- **permission mode** (from get_session): `auto` (`PERMISSION_MODE_AUTO`)
- **`mcp__Claude_Code_Remote__create_session` resolved**: no — ToolSearch for that exact name returned "No matching deferred tools found". The equivalent tool is present in this session under the MCP prefix `mcp__bf7c680d-5fdc-5ef4-b4a0-abadb619bf0a__` (i.e. `mcp__bf7c680d-5fdc-5ef4-b4a0-abadb619bf0a__create_session`), already loaded, so spawning is possible via that name.
- **`ls -a` at repo root**: `. .. .git .gitignore .gitmodules README.md addon_config_sync anki_shared build.py conftest.py copy_anywhere custom_schedule_helper desired_retention japanese_note_ai_ops mypy.ini pyrightconfig.json pytest.ini related_card_disperse requirements-dev-nodeps.txt requirements-dev.txt spotify_desktop_link stubs untracked_media_syncer` — yes, this looks like a fresh clone: only tracked repo content, `git status --porcelain` empty, no leftover scratch/state dirs from any other session.
- **`~/.claude/uploads/` exists**: no — the parent session's uploaded files are not visible here.
- **`mcp__github__*` tools available**: yes (deferred, loadable via ToolSearch — e.g. `mcp__github__create_pull_request`, `mcp__github__pull_request_read`).
- **`Agent` tool available**: yes.
