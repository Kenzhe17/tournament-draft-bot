# r!z3 — read-only Discord server audit

## What it does

`/admin server_audit` exports **category/channel names, IDs, order, types, explicit permission overwrites, role IDs, roles/role permissions, and reviewed channel/role bindings** as UTF-8 JSON.

The JSON contains no message history, topics, room credentials, webhook URLs, invites, secrets, or per-member role lists. Member IDs occur only where a per-member channel overwrite exists. Guild metadata and names are included. Share the JSON privately with a trusted reviewer only.

## Security

- The caller's ID **must equal** `BOT_OWNER_ID` (no organizers/admin role bypass).
- The guild ID **must equal** `GUILD_ID`; both settings must be nonzero.
- Only Discord read APIs (`fetch_channels`, `fetch_roles`) are used.
- No changes to roles, channels, permissions, members, messages, or the database.
- The JSON is sent only as a private ephemeral Discord attachment.
- The file is built in memory, without saving to a persistent disk.
- For reports over 7 MiB, the command refuses delivery rather than uploading elsewhere.
- `default_permissions(administrator=True)` hides the command from most users, but **the in-code owner check** is the enforcement boundary.

## Setup and usage

1. Ensure Railway has the correct **nonzero** `BOT_OWNER_ID` and `GUILD_ID` (do not paste secrets into chat).
2. Install the three source files and register `cogs.server_audit` in `TournamentBot.setup_hook`, before the root command groups are added.
3. Run `pytest -q tests/test_server_audit.py` in your project's Python environment, plus the existing test suite.
4. Deploy to staging first. Check that `/admin server_audit` appears; run it with the configured owner account.
5. Inspect the JSON for missing bindings and correct permission overwrites. Then validate on production only after approving deployment.
6. Save the JSON locally and share it privately to plan the channel migration.

**Scope limits:** channel/role HTTP reads do not include Threads or actual messages. Runtime DB-backed channel overrides (such as dynamically persisted REP-log/info channel IDs) might not match env-only bindings. Member-specific effective permissions require a separate audit and are not inferred here. The command does not claim to make modifications to Discord.
