-- Arbiter schema: esports referee & tournament ops (port of prisma schema.prisma)
-- All IDs are TEXT; JSON fields stored as JSONB.

CREATE TABLE IF NOT EXISTS arb_organizations (
    id TEXT PRIMARY KEY,
    discord_guild_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS arb_org_settings (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL UNIQUE REFERENCES arb_organizations(id) ON DELETE CASCADE,
    admin_role_id TEXT,
    referee_role_id TEXT,
    match_category_id TEXT,
    match_log_channel_id TEXT,
    evidence_channel_id TEXT,
    web_permissions JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS arb_user_profiles (
    id TEXT PRIMARY KEY,
    discord_user_id TEXT NOT NULL UNIQUE,
    display_name TEXT,
    country_code TEXT,
    bio TEXT,
    profile_visibility TEXT NOT NULL DEFAULT 'private',
    open_to_work BOOLEAN NOT NULL DEFAULT FALSE,
    avatar_url TEXT,
    contact_email TEXT,
    show_contact_email BOOLEAN NOT NULL DEFAULT FALSE,
    social_links JSONB,
    game_experiences JSONB NOT NULL DEFAULT '[]',
    field_roles JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS arb_org_members (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES arb_organizations(id) ON DELETE CASCADE,
    user_profile_id TEXT NOT NULL REFERENCES arb_user_profiles(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'PLAYER',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, user_profile_id)
);
CREATE INDEX IF NOT EXISTS arb_org_members_profile_idx ON arb_org_members(user_profile_id);

CREATE TABLE IF NOT EXISTS arb_teams (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES arb_organizations(id) ON DELETE CASCADE,
    tournament_id TEXT,
    name TEXT NOT NULL,
    captain_profile_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_teams_org_idx ON arb_teams(organization_id);
CREATE INDEX IF NOT EXISTS arb_teams_tournament_idx ON arb_teams(tournament_id);
CREATE INDEX IF NOT EXISTS arb_teams_captain_idx ON arb_teams(captain_profile_id);

CREATE TABLE IF NOT EXISTS arb_team_members (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES arb_teams(id) ON DELETE CASCADE,
    user_profile_id TEXT REFERENCES arb_user_profiles(id) ON DELETE SET NULL,
    display_name TEXT NOT NULL,
    team_role TEXT NOT NULL DEFAULT 'player',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_team_members_team_idx ON arb_team_members(team_id);

CREATE TABLE IF NOT EXISTS arb_tournaments (
    id TEXT PRIMARY KEY,
    public_code TEXT NOT NULL UNIQUE,
    organization_id TEXT NOT NULL REFERENCES arb_organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    game_title TEXT,
    format TEXT NOT NULL DEFAULT 'single_elimination',
    status TEXT NOT NULL DEFAULT 'draft',
    best_of INT NOT NULL DEFAULT 1,
    third_place BOOLEAN NOT NULL DEFAULT FALSE,
    referee_claim_mode TEXT NOT NULL DEFAULT 'open',
    referee_claim_limit INT NOT NULL DEFAULT 1,
    champion_name TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_tournaments_org_idx ON arb_tournaments(organization_id);

CREATE TABLE IF NOT EXISTS arb_tournament_entries (
    id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL REFERENCES arb_tournaments(id) ON DELETE CASCADE,
    team_id TEXT REFERENCES arb_teams(id) ON DELETE SET NULL,
    team_name TEXT NOT NULL,
    seed INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tournament_id, seed)
);
CREATE INDEX IF NOT EXISTS arb_entries_tournament_idx ON arb_tournament_entries(tournament_id);

CREATE TABLE IF NOT EXISTS arb_bracket_nodes (
    id TEXT PRIMARY KEY,
    tournament_id TEXT NOT NULL REFERENCES arb_tournaments(id) ON DELETE CASCADE,
    bracket TEXT NOT NULL DEFAULT 'winners',
    round_index INT NOT NULL,
    slot_index INT NOT NULL,
    label TEXT,
    best_of INT NOT NULL DEFAULT 1,
    team_a_entry_id TEXT,
    team_b_entry_id TEXT,
    team_a_name TEXT,
    team_b_name TEXT,
    team_a_source TEXT,
    team_b_source TEXT,
    team_a_score INT NOT NULL DEFAULT 0,
    team_b_score INT NOT NULL DEFAULT 0,
    team_a_result TEXT,
    team_b_result TEXT,
    winner_slot TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    match_id TEXT UNIQUE,
    winner_to_node_id TEXT,
    winner_to_slot TEXT,
    loser_to_node_id TEXT,
    loser_to_slot TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tournament_id, bracket, round_index, slot_index)
);
CREATE INDEX IF NOT EXISTS arb_bracket_tournament_idx ON arb_bracket_nodes(tournament_id);

CREATE TABLE IF NOT EXISTS arb_matches (
    id TEXT PRIMARY KEY,
    public_code TEXT NOT NULL UNIQUE,
    organization_id TEXT NOT NULL REFERENCES arb_organizations(id) ON DELETE CASCADE,
    tournament_id TEXT REFERENCES arb_tournaments(id) ON DELETE SET NULL,
    channel_id TEXT,
    control_message_id TEXT,
    created_by_id TEXT,
    team_a_name TEXT NOT NULL,
    team_b_name TEXT NOT NULL,
    best_of INT NOT NULL DEFAULT 1,
    rules_preset TEXT NOT NULL DEFAULT 'generic',
    veto_mode TEXT NOT NULL DEFAULT 'final_map_ban',
    veto_starting_team TEXT NOT NULL DEFAULT 'teamA',
    veto_timer_seconds INT NOT NULL DEFAULT 60,
    veto_timeout_action TEXT NOT NULL DEFAULT 'referee_choice',
    character_ban_mode TEXT NOT NULL DEFAULT 'none',
    character_ban_timer_seconds INT NOT NULL DEFAULT 30,
    character_ban_started_at TIMESTAMPTZ,
    character_pool JSONB,
    map_pool JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'PENDING',
    team_a_score INT NOT NULL DEFAULT 0,
    team_b_score INT NOT NULL DEFAULT 0,
    team_a_result TEXT,
    team_b_result TEXT,
    allow_player_reports BOOLEAN NOT NULL DEFAULT FALSE,
    assigned_referee_id TEXT,
    referee_claim_mode TEXT NOT NULL DEFAULT 'open',
    referee_claim_limit INT NOT NULL DEFAULT 1,
    dispute_reason TEXT,
    team_a_role_id TEXT,
    team_b_role_id TEXT,
    roster_locked_at TIMESTAMPTZ,
    roster_locked_by_id TEXT,
    final_map TEXT,
    external_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_matches_org_idx ON arb_matches(organization_id);
ALTER TABLE arb_matches ADD COLUMN IF NOT EXISTS external_ref TEXT;
CREATE INDEX IF NOT EXISTS arb_matches_extref_idx ON arb_matches(external_ref);
CREATE INDEX IF NOT EXISTS arb_matches_status_idx ON arb_matches(status);
CREATE INDEX IF NOT EXISTS arb_matches_tournament_idx ON arb_matches(tournament_id);

CREATE TABLE IF NOT EXISTS arb_match_participants (
    id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL REFERENCES arb_matches(id) ON DELETE CASCADE,
    team_id TEXT NOT NULL REFERENCES arb_teams(id) ON DELETE CASCADE,
    slot TEXT NOT NULL,
    UNIQUE (match_id, slot)
);

CREATE TABLE IF NOT EXISTS arb_referee_assignments (
    id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL REFERENCES arb_matches(id) ON DELETE CASCADE,
    user_profile_id TEXT NOT NULL REFERENCES arb_user_profiles(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'assigned',
    status TEXT NOT NULL DEFAULT 'active',
    assigned_by_id TEXT REFERENCES arb_user_profiles(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (match_id, user_profile_id)
);
CREATE INDEX IF NOT EXISTS arb_ref_assign_match_idx ON arb_referee_assignments(match_id, status);
CREATE INDEX IF NOT EXISTS arb_ref_assign_profile_idx ON arb_referee_assignments(user_profile_id);

CREATE TABLE IF NOT EXISTS arb_veto_actions (
    id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL REFERENCES arb_matches(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    team_slot TEXT NOT NULL,
    map_name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'team',
    note TEXT,
    actor_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_veto_match_idx ON arb_veto_actions(match_id);

CREATE TABLE IF NOT EXISTS arb_char_ban_actions (
    id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL REFERENCES arb_matches(id) ON DELETE CASCADE,
    team_slot TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'ban',
    character TEXT NOT NULL,
    game_role TEXT,
    source TEXT NOT NULL DEFAULT 'team',
    note TEXT,
    actor_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_charban_match_idx ON arb_char_ban_actions(match_id);

CREATE TABLE IF NOT EXISTS arb_map_results (
    id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL REFERENCES arb_matches(id) ON DELETE CASCADE,
    map_index INT NOT NULL,
    map_name TEXT NOT NULL,
    team_a_score INT NOT NULL DEFAULT 0,
    team_b_score INT NOT NULL DEFAULT 0,
    team_a_result TEXT,
    team_b_result TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    note TEXT,
    actor_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (match_id, map_index)
);
CREATE INDEX IF NOT EXISTS arb_mapresults_match_idx ON arb_map_results(match_id);

CREATE TABLE IF NOT EXISTS arb_score_reports (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES arb_organizations(id) ON DELETE CASCADE,
    match_id TEXT NOT NULL REFERENCES arb_matches(id) ON DELETE CASCADE,
    team_a_score INT NOT NULL,
    team_b_score INT NOT NULL,
    team_a_result TEXT,
    team_b_result TEXT,
    scoring_type TEXT NOT NULL DEFAULT 'match',
    comment TEXT,
    attachments JSONB,
    status TEXT NOT NULL DEFAULT 'pending',
    submitted_by_id TEXT,
    reviewed_by_id TEXT,
    reviewed_at TIMESTAMPTZ,
    review_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_scorereports_org_idx ON arb_score_reports(organization_id);
CREATE INDEX IF NOT EXISTS arb_scorereports_match_idx ON arb_score_reports(match_id);
CREATE INDEX IF NOT EXISTS arb_scorereports_status_idx ON arb_score_reports(status);

CREATE TABLE IF NOT EXISTS arb_pause_logs (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    match_id TEXT NOT NULL REFERENCES arb_matches(id) ON DELETE CASCADE,
    pause_type TEXT NOT NULL DEFAULT 'team',
    team_name TEXT,
    duration_minutes INT NOT NULL,
    reason TEXT NOT NULL,
    actor_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_pauses_org_idx ON arb_pause_logs(organization_id);
CREATE INDEX IF NOT EXISTS arb_pauses_match_idx ON arb_pause_logs(match_id);

CREATE TABLE IF NOT EXISTS arb_warnings (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    match_id TEXT NOT NULL REFERENCES arb_matches(id) ON DELETE CASCADE,
    team_name TEXT,
    player TEXT NOT NULL,
    rule TEXT NOT NULL,
    note TEXT,
    actor_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_warnings_org_idx ON arb_warnings(organization_id);
CREATE INDEX IF NOT EXISTS arb_warnings_match_idx ON arb_warnings(match_id);

CREATE TABLE IF NOT EXISTS arb_evidence (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    match_id TEXT NOT NULL REFERENCES arb_matches(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'submitted',
    reviewed_by_id TEXT,
    reviewed_at TIMESTAMPTZ,
    review_note TEXT,
    submitted_by_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_evidence_org_idx ON arb_evidence(organization_id);
CREATE INDEX IF NOT EXISTS arb_evidence_match_idx ON arb_evidence(match_id);

CREATE TABLE IF NOT EXISTS arb_roster_submissions (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    match_id TEXT NOT NULL REFERENCES arb_matches(id) ON DELETE CASCADE,
    team_slot TEXT NOT NULL,
    team_name TEXT NOT NULL,
    players JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'submitted',
    note TEXT,
    submitted_by_id TEXT,
    reviewed_by_id TEXT,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (match_id, team_slot)
);
CREATE INDEX IF NOT EXISTS arb_roster_org_idx ON arb_roster_submissions(organization_id);
CREATE INDEX IF NOT EXISTS arb_roster_status_idx ON arb_roster_submissions(status);

CREATE TABLE IF NOT EXISTS arb_checkins (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    match_id TEXT NOT NULL REFERENCES arb_matches(id) ON DELETE CASCADE,
    user_profile_id TEXT NOT NULL REFERENCES arb_user_profiles(id) ON DELETE CASCADE,
    game_account TEXT,
    validation JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_checkins_org_idx ON arb_checkins(organization_id);
CREATE INDEX IF NOT EXISTS arb_checkins_profile_idx ON arb_checkins(user_profile_id);

CREATE TABLE IF NOT EXISTS arb_reminders (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    match_id TEXT REFERENCES arb_matches(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    channel_id TEXT,
    user_id TEXT,
    payload JSONB,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_reminders_org_idx ON arb_reminders(organization_id);
CREATE INDEX IF NOT EXISTS arb_reminders_due_idx ON arb_reminders(due_at);
CREATE INDEX IF NOT EXISTS arb_reminders_delivered_idx ON arb_reminders(delivered_at);

CREATE TABLE IF NOT EXISTS arb_rulebook_entries (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    category TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, key)
);

CREATE TABLE IF NOT EXISTS arb_match_rooms (
    id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL UNIQUE REFERENCES arb_matches(id) ON DELETE CASCADE,
    text_channel_id TEXT,
    voice_channel_id TEXT,
    category_id TEXT,
    player_message_id TEXT,
    team_a_text_channel_id TEXT,
    team_a_voice_channel_id TEXT,
    team_a_message_id TEXT,
    team_b_text_channel_id TEXT,
    team_b_voice_channel_id TEXT,
    team_b_message_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS arb_audit_log (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_audit_org_idx ON arb_audit_log(organization_id);

CREATE TABLE IF NOT EXISTS arb_referee_shifts (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    user_profile_id TEXT NOT NULL REFERENCES arb_user_profiles(id) ON DELETE CASCADE,
    on_shift BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, user_profile_id)
);

CREATE TABLE IF NOT EXISTS arb_rules_presets (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES arb_organizations(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    label TEXT NOT NULL,
    game_title TEXT,
    map_pool JSONB NOT NULL DEFAULT '[]',
    character_pool JSONB,
    veto_mode TEXT NOT NULL DEFAULT 'final_map_ban',
    notes TEXT,
    created_by_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, key)
);
CREATE INDEX IF NOT EXISTS arb_rulespresets_org_idx ON arb_rules_presets(organization_id);

CREATE TABLE IF NOT EXISTS arb_ruling_presets (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES arb_organizations(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    label TEXT NOT NULL,
    result_label TEXT NOT NULL,
    default_subject_score INT NOT NULL DEFAULT 0,
    default_opponent_score INT NOT NULL DEFAULT 0,
    applies_to TEXT NOT NULL DEFAULT 'subject_loses',
    notes TEXT,
    created_by_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, key)
);
CREATE INDEX IF NOT EXISTS arb_rulingpresets_org_idx ON arb_ruling_presets(organization_id);

CREATE TABLE IF NOT EXISTS arb_br_lobbies (
    id TEXT PRIMARY KEY,
    public_code TEXT NOT NULL UNIQUE,
    organization_id TEXT NOT NULL REFERENCES arb_organizations(id) ON DELETE CASCADE,
    channel_id TEXT,
    control_message_id TEXT,
    created_by_id TEXT,
    name TEXT NOT NULL,
    game TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    games_planned INT NOT NULL DEFAULT 6,
    kill_points INT NOT NULL DEFAULT 1,
    placement_points JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_brlobbies_org_idx ON arb_br_lobbies(organization_id);

CREATE TABLE IF NOT EXISTS arb_br_teams (
    id TEXT PRIMARY KEY,
    lobby_id TEXT NOT NULL REFERENCES arb_br_lobbies(id) ON DELETE CASCADE,
    linked_team_id TEXT REFERENCES arb_teams(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    seed INT,
    discord_role_id TEXT,
    category_channel_id TEXT,
    text_channel_id TEXT,
    voice_channel_id TEXT,
    team_message_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (lobby_id, name)
);
CREATE INDEX IF NOT EXISTS arb_brteams_lobby_idx ON arb_br_teams(lobby_id);
CREATE INDEX IF NOT EXISTS arb_brteams_linked_idx ON arb_br_teams(linked_team_id);

CREATE TABLE IF NOT EXISTS arb_br_game_results (
    id TEXT PRIMARY KEY,
    lobby_id TEXT NOT NULL REFERENCES arb_br_lobbies(id) ON DELETE CASCADE,
    br_team_id TEXT NOT NULL REFERENCES arb_br_teams(id) ON DELETE CASCADE,
    game_number INT NOT NULL,
    placement INT NOT NULL DEFAULT 0,
    kills INT NOT NULL DEFAULT 0,
    points INT NOT NULL DEFAULT 0,
    actor_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (lobby_id, br_team_id, game_number)
);
CREATE INDEX IF NOT EXISTS arb_brgameresults_lobby_idx ON arb_br_game_results(lobby_id);

CREATE TABLE IF NOT EXISTS arb_br_adjustments (
    id TEXT PRIMARY KEY,
    lobby_id TEXT NOT NULL REFERENCES arb_br_lobbies(id) ON DELETE CASCADE,
    br_team_id TEXT NOT NULL REFERENCES arb_br_teams(id) ON DELETE CASCADE,
    points INT NOT NULL DEFAULT 0,
    kills INT NOT NULL DEFAULT 0,
    game_number INT,
    reason TEXT NOT NULL,
    actor_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_bradjustments_lobby_idx ON arb_br_adjustments(lobby_id);

CREATE TABLE IF NOT EXISTS arb_br_logs (
    id TEXT PRIMARY KEY,
    lobby_id TEXT NOT NULL REFERENCES arb_br_lobbies(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    br_team_id TEXT REFERENCES arb_br_teams(id) ON DELETE SET NULL,
    subject TEXT,
    game_number INT,
    summary TEXT,
    details TEXT,
    rule TEXT,
    duration_minutes INT,
    attachment_url TEXT,
    attachment_name TEXT,
    actor_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_brlogs_lobby_idx ON arb_br_logs(lobby_id);

CREATE TABLE IF NOT EXISTS arb_standalone_logs (
    id TEXT PRIMARY KEY,
    user_profile_id TEXT NOT NULL REFERENCES arb_user_profiles(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    event TEXT,
    teams TEXT,
    subject TEXT,
    summary TEXT,
    details TEXT,
    result TEXT,
    attachment_url TEXT,
    attachment_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_standalonelogs_profile_idx ON arb_standalone_logs(user_profile_id);

CREATE TABLE IF NOT EXISTS arb_linked_accounts (
    id TEXT PRIMARY KEY,
    user_profile_id TEXT NOT NULL REFERENCES arb_user_profiles(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    handle TEXT NOT NULL,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, handle)
);
CREATE INDEX IF NOT EXISTS arb_linkedaccounts_profile_idx ON arb_linked_accounts(user_profile_id);

CREATE TABLE IF NOT EXISTS arb_announcements (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES arb_organizations(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    target_kind TEXT NOT NULL DEFAULT 'everyone',
    target_value TEXT,
    created_by_id TEXT,
    expires_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_announcements_org_idx ON arb_announcements(organization_id);

CREATE TABLE IF NOT EXISTS arb_discord_sync_jobs (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES arb_organizations(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'refresh',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arb_syncjobs_status_idx ON arb_discord_sync_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS arb_syncjobs_org_idx ON arb_discord_sync_jobs(organization_id);
