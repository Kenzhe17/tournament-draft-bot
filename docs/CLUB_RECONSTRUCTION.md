# r!z3 reconstruction — current integration checkpoint

Base: `feat/classic-riz3-cv2` at `e6c9bac55b5958dedf8e4cebf71c8ea14334b8cc` (verified 2026-11-16).
Status: **local integration; not deployed, no PR because GitHub writes return 403**.

## Current integration

The current base already contains Control Center, moderation, tournament journal and SHOP 2.0. They were brought in by merging the user-designated working branch, resolving three conflicts in MM, tournament help and room-message tests. Their implementations, payment/recovery paths and persistent state were preserved. The historical section below describes the earlier checkpoint only.

Hub economy now displays available REP as well as coins; REP read failures are shown as unavailable rather than as zero. Shop and inventory open the base's SHOP 2.0 handlers. Support buttons open private report/appeal forms and invoke the existing registered moderation command handlers with their checks, validation, throttles, ownership and audit behavior. Native `/report` and `/appeal` remain available.

`MATCHMAKING_ENABLED=false` is the retirement gate and overrides `MM_ENABLED` and Control Center settings. To explicitly roll back to queues all applicable gates must be enabled. Retirement keeps result recovery for existing matches, but starts no new queue timers, notifications, pool initialization or matches.

Shop role assignment now checks Discord permissions, configured organizer/manager/mute role IDs, dynamic moderation roles, managed/@everyone roles and explicit channel overwrites. Roles that unlock otherwise hidden channels or grant moderation permissions cannot be assigned through the shop. This intentionally also rejects paid private-channel access roles until a separate reviewed policy exists. Revalidation occurs when assigning a purchased role, activating a color, and restoring a removed role after an interrupted sale. A database failure reading staff configuration denies a new assignment. Existing roles are not removed. Ineligible restore operations remain unresolved for manual review rather than being falsely marked restored. Third-party bot ACLs and role-name authorization outside the reviewed project are not covered.

## Validation and actual services

- Focused integration suite: 140 passed, including support ownership/dispatch, REP degraded mode, shop role policy and recovery guards.
- Full suite before the final two focused regression additions: 874 passed, 6 skipped; 136 environment errors, all `PermissionError: [Errno 1] Operation not permitted` in PostgreSQL fixtures. No test assertion failures in that run. These errors are not passing financial integration tests.
- Control Center panel: `npm ci` and `npm run build` succeeded (TypeScript + Vite).
- New/updated standalone modules passed Ruff; Python compileall and git diff --check passed.
- Railway production/core already runs `e6c9bac`, deployment `cfc804cd-34e7-4447-a008-5ece0554bafe`, SUCCESS. This integration has not been deployed.
- GitHub branch creation returned 403 Resource not accessible by integration. No remote branch or PR was created by this work.

## Remaining gates

Authorized live Discord inspection, PostgreSQL/volume backup and restore check, PostgreSQL concurrency/recovery tests, real Discord/mobile/staging scenarios, complete effective-per-member permission audit, notification role preferences, scheduling/calendar, unified organizer flow and application of the channel plan remain incomplete. Production security changes still require the separate approval specified by the user after a concrete migration is validated.

## Prior checkpoint (historical, base 2f142c1)



Base: `feat/classic-riz3-cv2` at `2f142c1b36839430850a6c0b109f57c8dc3ed99b`.
Status: **draft, not deployed; full reconstruction is not complete**.

## Implemented in this branch

- `/hub`: persistent Components V2 navigation. Public buttons open private pages; private navigation edits the existing response. The tournament list reads CS/BR/Ultimatum stores and filters guild, hidden channels, CS test events and legacy MM. Profile, match history, balance, shop/inventory entry points and existing leaderboard are reused.
- `/server hub_publish`: administrator-only publication, durable message ID, update-in-place, in-process serialization and a persisted pending marker to prevent retries duplicating a message after an uncertain send/save outcome.
- New MM queue creation, joins and the deepest match-start entry point are denied by default. `/mm` is removed before command sync. Queue notifications and timers stop. Existing matches retain their result buttons, finalization recovery and channel release. Tables, rating/history, inventories and balances are untouched. Internal MM modules are deliberately retained until all old matches and finalization records have drained.
- CS room cards no longer disclose credentials. The existing `rcard:id:*` IDs still dispatch, but validate guild, current tournament, match roster and scoped staff before sending ID/password privately.
- `/server room_cards_secure`: explicit, administrator-only migration of known room messages. Edits existing bot-authored messages only, including archived references; does not send new messages, touch room records, alter match results or ping users. Missing/failed messages are counted. It cannot discover older untracked duplicates or recalled credentials.
- `is_admin` and `is_admin_check` no longer grant administration through organizer roles. Role-name matching is replaced by configured IDs. Tournament management checks allow configured managers, administrators, creator and assigned tournament/channel organizers; cross-guild calls fail closed. This does not assert that every independent permission check elsewhere has been migrated.
- Public help and information-center text move to organizer tournaments. Existing `ranked` content key is retained to edit its stored message rather than duplicate a section. Shared palette uses emerald/graphite; Discord's native message background and button colors cannot be changed with those constants.

## Verified existing architecture

`Dockerfile` starts the root `bot.py`. The separate `bot/` package is not the production entry point. Root `bot.py` loads 32 extensions after this change; the static declaration inventory is in [CLUB_COMMAND_INVENTORY.md](CLUB_COMMAND_INVENTORY.md). It includes Python declarations, not a claimed live Discord command listing.

CS: `models/tournament.py`, `storage/json_store.py`, `services/tournament_lifecycle.py`, phase-specific views. BR: `models/br_tournament.py`, `storage/br_store.py`. Ultimatum: `ultimatum/service.py` and PostgreSQL `ult2_*`. Statistics and economy mix PG-backed stores with JSON/fallback paths. Both PostgreSQL and `/app/data` must be backed up.

MM dependencies outside its cog include CS completion/deletion, `views/mm_match_view.py`, VAC cleanup, shared tournament models, match numbering and finalization. Deleting `cogs/mm.py` or its schema now would break imports/recovery.

Unmerged branches were fetched and their relationship checked, not merged:

| Branch | Reviewed tip | Relationship / consequence |
|---|---|---|
| `feat/tournament-journal` | `f036eab` | PR #13 remains open against the working branch |
| `feat/classic-control-center` | `74b58e6` | Common ancestor `7b74d97`; 82 changed files, including moderation/settings/financial actions |
| `feat/moderation-suite-cv2` | `63efeab` | Separate implementation of staff actions; absent from current production base |
| `feat/shop-2.0` | `97d6edd` | Durable purchase/charge changes; require real PG concurrency and recovery checks before integration |

The base's `cogs/moderation.py` is GIF-triggered moderation; the richer staff suite must not be described as deployed based on another branch's files. The base `/daily` reward and cooldown update are separate operations; the Hub deliberately does not add another claim button before that path's atomicity is addressed.

## Target structure (proposal, not applied)

| Category | Channels / surfaces | Visibility and purpose |
|---|---|---|
| Информация | `старт`, `объявления`, `роли` | Public Hub, compact rules and announcements; only staff/bot publish |
| Турниры | `турнир-1`, `турнир-2`, `турнир-3`, `battle-royale`, `результаты`, `данные-комнаты` | Reuse existing IDs; registration via cards; room credentials private |
| Сообщество | `чат`, `моменты` | Members write; retain existing forum history |
| Голосовые | existing lobby, create-room controls and live team voices | Preserve TempVoice integration and ownership; participant-only connection where required |
| Дополнительно | existing mini-games/music/social channels | Optional roles/Onboarding; profile, shop and ratings live in Hub, not new channels |
| Команда | organizer controls, reports, staff audit, system logs | Staff only; incident routing by role and responsible organizer |
| Архив | old MM/test/redundant channels | Hide first; preserve IDs/history; no deletion during migration |

No usage analytics or message-history audit is available. Channel consolidation and Onboarding changes therefore remain proposals, not conclusions about unused channels.

## Target authorization matrix

| Group | Tournament management | Moderation | Configuration / economy admin | Room credentials |
|---|---|---|---|---|
| Owner / admin | All | All authorized actions | Yes | Yes |
| Organizer manager | All assigned oversight | No by default | No | Managed events |
| Organizer | Assigned event/channel | Tournament incidents only | No | Own event |
| Moderator | No by default | Warn/timeout/report/appeal with hierarchy checks | No | Only if assigned |
| Captain / participant | Player actions only | Submit reports | Own purchases only | Own match |
| VAC | No new tournament registration | Appeal | Existing inventory preserved | No new match access |
| Notification / cosmetic role | None | None | None | None |
| Bot | Required capabilities only | Only supported workflows | No unnecessary Administrator | Server-side processing |

Full enforcement of this target matrix across BR/Ultimatum, shop roles, moderation and all legacy buttons remains release work. Do not infer it from the helper change alone.

## Release gates and rollback

1. Obtain authorized live Discord access and inspect effective permissions, integration ownership, pinned messages, Onboarding/Server Guide/AutoMod, active matches, queue state, and bot hierarchy. The supplied export is a point-in-time snapshot, not live access.
2. Back up PostgreSQL, `/app/data`, current Discord categories/ACLs/role hierarchy and resolved configuration. Validate restoration separately. Do not commit secrets or the full private server export to the public repository.
3. Run PG economy/MM/Ultimatum integration tests and validate concurrent actions, restarts and real Discord components in staging. Do not point a second gateway instance at the production bot token.
4. Verify the now-integrated journal binding, Control Center, moderation and Shop 2.0 against staging and real PostgreSQL. Do not merge their financial paths based solely on offline tests.
5. Review role/permission/channel plan and get the separately required approval for production security changes. Merge/release only after those gates; current branch remains a draft.
6. Deploy a new build, confirm deployed SHA and healthy startup, check global/guild command sync and old match finalization. Publish Hub via `/server hub_publish` in the selected existing start channel; a later call edits the saved panel.
7. Run `/server room_cards_secure` and inspect its counts. Update information panels via `/info update`. Check for untracked old credential cards. Existing knowledge of a previously public password cannot be revoked by editing a message; active room credentials need rotation by the organizer.
8. Hide/archive drained MM channels only after dependency and active-state checks. Do not delete SQL tables or historical MM results.

Rollback: redeploy the recorded previous build and restore configuration/ACL snapshot as appropriate. `MATCHMAKING_ENABLED=true` restores the retained queue implementation on this branch, but does not reverse other changes. Returning to old room-publisher code also restores its public-password behavior; do not treat that as a safe security rollback. No destructive schema migration is included.

If Hub state contains `pending`, do not erase it blindly: inspect the saved channel for the bot's existing Hub, then reconcile `club_hub.json` to its actual `channel_id`/`message_id` (or confirm send never happened). Stop the relevant process while repairing the file and preserve a backup.

## Not completed

Live server reconstruction; full effective-per-member permission audit; backups; staging multiplayer and restart checks; production release; calendar/scheduling, notification role preferences, support/appeals center, complete visual redesign, unified organizer workflow, full security audit, moderation/SHOP/Control Center integration. The code checkpoint does not satisfy the master prompt's final completion criteria.
