"""Ультиматум 2.0 — Double Elimination (Соло 1×1 и Дуо 2×2).

Публичные имена ULTIMATUM_SCHEMA / ELO_SCHEMA импортируются из
storage/db.py при init_db(). Старые таблицы ultimatum_* и
elo_ratings/elo_history НЕ удаляются — история сохраняется.
"""

from ultimatum.bracket import (  # noqa: F401
    Bracket,
    Entrant,
    EntrantStatus,
    Format,
    MatchSpec,
    MatchState,
    MatchStatus,
    Slot,
    entrant_count,
    simulate,
)

# ---------------------------------------------------------------------------
# Схема PostgreSQL
# ---------------------------------------------------------------------------

ULTIMATUM_SCHEMA = """
-- ===== ЛЕГАСИ-ТАБЛИЦЫ (Ультиматум 1.0) — только чтение/история =====
CREATE TABLE IF NOT EXISTS ultimatum_tournaments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    guild_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    current_round INTEGER DEFAULT 0,
    is_open BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_by BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS ultimatum_players (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    discord_id BIGINT NOT NULL,
    username TEXT NOT NULL,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    final_place INTEGER,
    elimination_round INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tournament_id, discord_id),
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ultimatum_matches (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    round_number INTEGER NOT NULL,
    player1_id BIGINT NOT NULL,
    player2_id BIGINT NOT NULL,
    winner_id BIGINT,
    loser_id BIGINT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    match_type TEXT NOT NULL DEFAULT 'NORMAL',
    room_id TEXT,
    room_password TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ultimatum_rounds (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    round_number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    UNIQUE(tournament_id, round_number),
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ultimatum_rankings (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    player_id BIGINT NOT NULL,
    final_place INTEGER NOT NULL,
    rank_group TEXT,
    finalized BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tournament_id, player_id),
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ultimatum_match_history (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    player1_id BIGINT NOT NULL,
    player2_id BIGINT NOT NULL,
    round_number INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ultimatum_audit_log (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    actor_discord_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    details JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS ultimatum_votes (
    id TEXT PRIMARY KEY,
    match_id INTEGER NOT NULL,
    user_id BIGINT NOT NULL,
    voted_winner_id BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (match_id) REFERENCES ultimatum_matches(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ultimatum_tournaments_guild ON ultimatum_tournaments(guild_id);
CREATE INDEX IF NOT EXISTS idx_ultimatum_tournaments_status ON ultimatum_tournaments(status);
CREATE INDEX IF NOT EXISTS idx_ultimatum_players_tournament ON ultimatum_players(tournament_id);
CREATE INDEX IF NOT EXISTS idx_ultimatum_matches_tournament ON ultimatum_matches(tournament_id);
CREATE INDEX IF NOT EXISTS idx_ultimatum_audit_log_tournament ON ultimatum_audit_log(tournament_id);
CREATE INDEX IF NOT EXISTS idx_ultimatum_votes_match ON ultimatum_votes(match_id);

-- ===== УЛЬТИМАТУМ 2.0 =====

-- Турниры (Соло и Дуо независимы: уникальность по guild+format)
CREATE TABLE IF NOT EXISTS ult2_tournaments (
    id TEXT PRIMARY KEY,
    format TEXT NOT NULL CHECK (format IN ('solo','duo')),
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'REGISTRATION',
    is_open BOOLEAN NOT NULL DEFAULT FALSE,
    channel_id BIGINT NOT NULL DEFAULT 0,
    message_id BIGINT NOT NULL DEFAULT 0,
    bracket_message_id BIGINT NOT NULL DEFAULT 0,
    created_by BIGINT NOT NULL,
    is_test BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    champion_entrant_id BIGINT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ult2_active
    ON ult2_tournaments (guild_id, format)
    WHERE status IN ('REGISTRATION','IN_PROGRESS');
CREATE INDEX IF NOT EXISTS idx_ult2_tournaments_guild
    ON ult2_tournaments (guild_id, format, status);

-- Участники/команды (entrant). member_ids — Discord ID игроков.
CREATE TABLE IF NOT EXISTS ult2_entrants (
    id BIGSERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL REFERENCES ult2_tournaments(id) ON DELETE CASCADE,
    seed INTEGER,
    member_ids BIGINT[] NOT NULL,
    member_names JSONB NOT NULL DEFAULT '[]'::jsonb,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    final_place INTEGER,
    place_group TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ult2_entrants_tournament
    ON ult2_entrants (tournament_id);
-- Один игрок не может быть в двух командах одного турнира.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ult2_entrant_member
    ON ult2_entrants (tournament_id, (member_ids[1]))
    WHERE cardinality(member_ids) = 1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ult2_entrant_member_pair
    ON ult2_entrants (tournament_id, (member_ids[1]), (member_ids[2]))
    WHERE cardinality(member_ids) = 2;

-- Матчи сетки. side_*_src — сериализованные Slot из bracket.py.
CREATE TABLE IF NOT EXISTS ult2_matches (
    id BIGSERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL REFERENCES ult2_tournaments(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    bracket TEXT NOT NULL,
    round_name TEXT NOT NULL DEFAULT '',
    gf_part INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'WAITING',
    side_a_src TEXT NOT NULL,
    side_b_src TEXT NOT NULL,
    entrant_a BIGINT,
    entrant_b BIGINT,
    winner_entrant BIGINT,
    loser_entrant BIGINT,
    room_id TEXT,
    room_password TEXT,
    room_updated_by BIGINT,
    room_updated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (tournament_id, key)
);
CREATE INDEX IF NOT EXISTS idx_ult2_matches_tournament
    ON ult2_matches (tournament_id, key);

-- Голоса за победителя. UNIQUE(match_id,user_id) — один голос
-- на пользователя; повторное нажатие обновляет выбор.
CREATE TABLE IF NOT EXISTS ult2_votes (
    id BIGSERIAL PRIMARY KEY,
    match_id BIGINT NOT NULL REFERENCES ult2_matches(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    voter_entrant BIGINT,
    voted_entrant BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (match_id, user_id)
);

-- Приглашения в дуэт (Дуо)
CREATE TABLE IF NOT EXISTS ult2_duo_invites (
    id BIGSERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL REFERENCES ult2_tournaments(id) ON DELETE CASCADE,
    captain_id BIGINT NOT NULL,
    partner_id BIGINT,
    partner_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/accepted/declined/cancelled/expired
    message_id BIGINT NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ult2_invites_tournament
    ON ult2_duo_invites (tournament_id, status);

-- Журнал действий организаторов
CREATE TABLE IF NOT EXISTS ult2_audit (
    id BIGSERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    actor_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ult2_audit_tournament
    ON ult2_audit (tournament_id, id);
"""

ELO_SCHEMA = """
-- ===== ЛЕГАСИ ELO (Ультиматум 1.0) — только чтение/история =====
CREATE TABLE IF NOT EXISTS elo_ratings (
    id BIGSERIAL PRIMARY KEY,
    discord_id BIGINT UNIQUE NOT NULL,
    username VARCHAR(100),
    elo INTEGER NOT NULL DEFAULT 1000,
    peak_elo INTEGER NOT NULL DEFAULT 1000,
    rated_matches INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS elo_history (
    id BIGSERIAL PRIMARY KEY,
    match_id BIGINT,
    tournament_id TEXT,
    player_id BIGINT NOT NULL,
    opponent_id BIGINT NOT NULL,
    elo_before INTEGER NOT NULL,
    elo_after INTEGER NOT NULL,
    elo_change INTEGER NOT NULL,
    opponent_elo_before INTEGER NOT NULL,
    expected_score NUMERIC(10,8),
    k_factor INTEGER NOT NULL,
    result VARCHAR(10) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_elo_ratings_discord ON elo_ratings(discord_id);
CREATE INDEX IF NOT EXISTS idx_elo_ratings_elo ON elo_ratings(elo DESC);
CREATE INDEX IF NOT EXISTS idx_elo_history_player ON elo_history(player_id);
CREATE INDEX IF NOT EXISTS idx_elo_history_match ON elo_history(match_id);
CREATE INDEX IF NOT EXISTS idx_elo_history_tournament ON elo_history(tournament_id);

-- ===== УЛЬТИМАТУМ 2.0 ELO: отдельные рейтинги Соло и Дуо =====
CREATE TABLE IF NOT EXISTS ult_elo_ratings (
    discord_id BIGINT NOT NULL,
    format TEXT NOT NULL CHECK (format IN ('solo','duo')),
    elo INTEGER NOT NULL DEFAULT 1000,
    peak_elo INTEGER NOT NULL DEFAULT 1000,
    rated_matches INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (discord_id, format)
);
CREATE TABLE IF NOT EXISTS ult_elo_history (
    id BIGSERIAL PRIMARY KEY,
    match_id BIGINT NOT NULL,
    tournament_id TEXT NOT NULL,
    format TEXT NOT NULL,
    player_id BIGINT NOT NULL,
    result TEXT NOT NULL,               -- WIN / LOSS
    side_avg_elo NUMERIC(10,2),         -- средний ELO своей команды
    opp_avg_elo NUMERIC(10,2),          -- средний ELO команды соперника
    elo_before INTEGER NOT NULL,
    elo_after INTEGER NOT NULL,
    elo_change INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (match_id, player_id)        -- защита от двойного начисления
);
CREATE INDEX IF NOT EXISTS idx_ult_elo_history_player
    ON ult_elo_history (player_id, format, id DESC);
CREATE INDEX IF NOT EXISTS idx_ult_elo_history_match
    ON ult_elo_history (match_id);
CREATE INDEX IF NOT EXISTS idx_ult_elo_ratings_elo
    ON ult_elo_ratings (format, elo DESC);
"""
