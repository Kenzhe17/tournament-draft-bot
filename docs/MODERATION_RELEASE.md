# r!z3 CLASSIC — MODERATION suite (feature package)

**Source base:** `Kenzhe17/tournament-draft-bot`, `feat/classic-riz3-cv2`, verified base HEAD `7b74d97aad530e6a015548e8c2d124818a8065ff` on 2026-11-15.  
**Apply to separate branch:** `feat/moderation-suite-cv2` (not `main`, not production branch).  
**Status:** local source package; NOT deployed, NOT live-verified in Discord, no GitHub PR created (connector returned HTTP 403 for writes).

## Installation

1. In your existing clone, preserve/commit unrelated local work. Ensure this is the correct r!z3 CLASSIC repository.
2. `git fetch origin`
3. `git switch feat/classic-riz3-cv2` and `git pull --ff-only origin feat/classic-riz3-cv2` **only if local tree is clean**.
4. `git switch -c feat/moderation-suite-cv2` (use an unused feature name if branch already exists).
5. Extract the package **outside** the repository. Run `python installer.py --repo "C:\\Users\\2006k\\Downloads\\tournament-draft-bot-main"` from the extracted package (replace destination with your actual repo path). It checks the branch and clean working tree, installs files, and edits `bot.py` safely.
6. Install dependencies from existing `requirements.txt` (`discord.py>=2.7` and `asyncpg` already specified).
7. `python -m compileall -q cogs/mod.py cogs/moderation.py services/moderation_service.py storage/moderation_store.py views/mod_panel.py`
8. `pytest -q tests/test_mod_suite.py`; then `pytest -q` and review failures, if any.
9. Configure a **staging** Discord app connected to a test server with permissions and privileged intents. Ensure staging PostgreSQL is isolated from production.
10. Start staging bot. Use `/mod setup` as an actual server administrator. This creates roles, `#mod-logs`, and `#mod-reports` and stores their IDs in PostgreSQL. Alternatively use `/mod configure` for existing roles/channels. Position roles visually as desired; **the bot's highest role must be above every person it sanctions.**
11. Manually verify warning, timeout, release, reports, case history, report claims/decisions, appeals, kick/ban on an expendable test account, GIF trigger integration, AutoMod audit, restart recovery, channel privacy and DM failures. Validate `/tournament` `/vac` `/shop` still work.
12. Open a PR from `feat/moderation-suite-cv2` to `feat/classic-riz3-cv2`. Resolve concurrent SHOP/journal branch changes before merge. Deploy production only after staging approval.

> **Important:** The developer must grant the GitHub App repository write permissions or apply the package locally. This assistant cannot push/merge/deploy it with the currently connected GitHub integration.

## Architecture

* `storage/moderation_store.py` — PostgreSQL tables `moderation_settings`, `moderation_cases`, `moderation_reports`, `moderation_appeals`, `moderation_events` with server scoping and append-only events. No JSON fallback for sanctions. Schema is created lazily (`CREATE TABLE IF NOT EXISTS`).
* `services/moderation_service.py` — RBAC independent of `utils.permissions.is_admin_check()` (which includes org roles), user/bot hierarchy checks, warning, timeout, kick, ban, revoke, status reconciliation; Discord CV2 log publication.
* `views/mod_panel.py` — private CV2 panel and report/appeal reviews; all callbacks re-check permissions.
* `cogs/mod.py` — staff slash command group, public reports/appeals and context menu.
* `cogs/moderation.py` — compatibility bridge: previously configured GIF triggers now issue native Discord Timeout, stored in PG, no `asyncio.sleep()` unmute timer.
* `bot.py` — load `cogs.mod` and enable AutoMod execution intent when supported by discord.py.

## Permissions

New roles default to no dangerous Discord permissions. This is intentional: moderation actions go through the bot and are audited. Give the **bot** sufficient `Moderate Members`, `Kick Members` (head moderators via commands), `Ban Members` (admins only), `Manage Messages`, `Manage Roles` and `Manage Channels` for `/mod setup`, plus appropriate channel permissions. The bot's highest role must be higher than targets. Native Discord rights for moderators can bypass the DB journal and should only be granted if intentionally desired.

Staff RBAC:

* MODERATOR: `warn`, `timeout`, `untimeout`, `note`, `history`, `case`, `reports`, `panel`, `stats`, `delete` (with checks), `evidence`, `voicemute`, `voiceunmute`, `voicedeafen`, `voiceundeafen`, `voicemove`.
* HEAD MODERATOR: above + `kick`, `purge`, `revoke` warnings, review appeals, `slowmode`.
* ADMIN: above + `ban`, `unban`, `/mod setup`, `/mod configure`.
* Organizer roles `org`, `ORG-1/2/3`, and managers are **not** automatically moderators. Non-admin staff cannot punish protected organizer roles. VAC, tournament results, ELO, balances and shop are not modified by this module.

Public commands: `/report user reason [message_url]`, `/appeal case_id reason`; context action **Пожаловаться в r!z3** on a message. Reports are limited to one per reporter per minute (PostgreSQL lock). No punishments are automatic after a report. Appeals are reviewed by HEAD MODERATOR; accepting an appeal records a decision but does not automatically unban/revoke without a staff action.

## Commands (Discord slash)

`/mod setup`, `/mod configure`, `/mod panel`, `/mod warn`, `/mod timeout`, `/mod untimeout`, `/mod kick`, `/mod ban`, `/mod unban`, `/mod revoke`, `/mod note`, `/mod history`, `/mod case`, `/mod reports`, `/mod appeals`, `/mod delete`, `/mod purge`, `/mod evidence`, `/mod stats`, `/mod voicemute`, `/mod voiceunmute`, `/mod voicedeafen`, `/mod voiceundeafen`, `/mod voicemove`, `/mod slowmode`.

Warnings expire after **30 days** and are retained in history. Discord-native timeout expires automatically, including when the bot is offline. A periodic reconciliation handles expired records and uncertain API results. Kick/ban never get blindly retried if Discord response is ambiguous. The webhook `MOD_LOG_WEBHOOK_URL` remains an optional fallback when `MOD_LOG_CHANNEL_ID`/stored log channel is unset.

## AutoMod and GIF

Discord AutoMod filtering itself must be configured in Discord server settings and tested separately. The bot records `on_automod_action` signals when the appropriate gateway intent is supported; it **does not auto-VAC/auto-ban**. GIF trigger IDs remain defined by `GIF_TRIGGER_USER_IDS`, plus `ADMIN_USER_IDS`; they must not be published or treated as an expandable unrestricted list. Old pre-release `MUTE_ROLE_ID` assignments are not silently removed; audit/remove any stranded old mutes before cut-over.

## Test/release safety gates

- Test all commands on staging; confirm role hierarchy restrictions and privacy of `mod-logs`, `mod-reports`.
- Verify restart and API timeout reconciliation against real Discord with a dedicated test server.
- Verify PostgreSQL schema permissions and recovery after a DB outage.
- Verify case ID boundaries: `guild_id` scopes every query. Test report races with two bot processes if production runs multiple replicas.
- Evaluate log/appeal data retention and staff access; no message bodies are copied by default, only URLs/reasons and actor IDs.
- Validate report notification delivery and explicit appeal resolution.
- Ensure no changes to VAC or tournament policy; no bans via junior moderator.

## Known limitations and unfinished external work

1. Source package is not pushed to GitHub and has not been executed in a running Discord client. Syntax compilation alone does not establish integration correctness.
2. Banned users cannot invoke guild slash commands to appeal. Provide an **out-of-band** appeal path (web form or staff DM) before using server bans in production.
3. Discord AutoMod rules are not auto-created; admins must configure them through Discord and should start in alert-only mode.
4. Direct moderator actions taken through Discord's native UI are visible in Discord Audit Log but are not guaranteed to be mirrored into this bot's PostgreSQL `moderation_events`; use bot commands for complete internal auditing.
5. Accepted appeals require an explicit separate sanction reversal; this prevents automated unbans from an ambiguous review decision.
6. Do not enable production until staging tests pass and administrators approve the logging, role and retention policy.
