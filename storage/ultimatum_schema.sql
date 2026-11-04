-- SQL Schema for Ultimatum Solo Tournament System

-- Main tournaments table
CREATE TABLE IF NOT EXISTS ultimatum_tournaments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    guild_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT',  -- DRAFT, REGISTRATION, READY, IN_PROGRESS, ROUND_IN_PROGRESS, ROUND_COMPLETED, GRAND_FINAL, COMPLETED, CANCELLED
    current_round INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_by BIGINT NOT NULL
);

-- Tournament participants
CREATE TABLE IF NOT EXISTS ultimatum_players (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    discord_id BIGINT NOT NULL,
    username TEXT NOT NULL,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE, ELIMINATED, GRAND_FINALIST, CHAMPION, GRAND_FINAL_LOSER
    final_place INTEGER,
    elimination_round INTEGER,  -- Round when player was eliminated
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tournament_id, discord_id),
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);

-- Tournament matches
CREATE TABLE IF NOT EXISTS ultimatum_matches (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    round_number INTEGER NOT NULL,
    player1_id BIGINT NOT NULL,
    player2_id BIGINT NOT NULL,
    winner_id BIGINT,
    loser_id BIGINT,
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING, IN_PROGRESS, COMPLETED
    match_type TEXT NOT NULL DEFAULT 'NORMAL',  -- NORMAL, GRAND_FINAL
    room_id TEXT,
    room_password TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);

-- Tournament rounds
CREATE TABLE IF NOT EXISTS ultimatum_rounds (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    round_number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING, IN_PROGRESS, COMPLETED
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    UNIQUE(tournament_id, round_number),
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);

-- Final rankings
CREATE TABLE IF NOT EXISTS ultimatum_rankings (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    player_id BIGINT NOT NULL,
    final_place INTEGER NOT NULL,
    rank_group TEXT,  -- For grouping similar performances
    finalized BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tournament_id, player_id),
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);

-- Match history (for avoiding rematches)
CREATE TABLE IF NOT EXISTS ultimatum_match_history (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    player1_id BIGINT NOT NULL,
    player2_id BIGINT NOT NULL,
    round_number INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);

-- Audit log for all actions
CREATE TABLE IF NOT EXISTS ultimatum_audit_log (
    id SERIAL PRIMARY KEY,
    tournament_id TEXT NOT NULL,
    actor_discord_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    details JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tournament_id) REFERENCES ultimatum_tournaments(id) ON DELETE CASCADE
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_ultimatum_tournaments_guild ON ultimatum_tournaments(guild_id);
CREATE INDEX IF NOT EXISTS idx_ultimatum_tournaments_status ON ultimatum_tournaments(status);
CREATE INDEX IF NOT EXISTS idx_ultimatum_players_tournament ON ultimatum_players(tournament_id);
CREATE INDEX IF NOT EXISTS idx_ultimatum_players_status ON ultimatum_players(status);
CREATE INDEX IF NOT EXISTS idx_ultimatum_matches_tournament ON ultimatum_matches(tournament_id);
CREATE INDEX IF NOT EXISTS idx_ultimatum_matches_round ON ultimatum_matches(round_number);
CREATE INDEX IF NOT EXISTS idx_ultimatum_matches_status ON ultimatum_matches(status);
CREATE INDEX IF NOT EXISTS idx_ultimatum_match_history_players ON ultimatum_match_history(player1_id, player2_id);
CREATE INDEX IF NOT EXISTS idx_ultimatum_audit_log_tournament ON ultimatum_audit_log(tournament_id);

-- Migration: Add elimination_round column if it doesn't exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'ultimatum_players' AND column_name = 'elimination_round'
    ) THEN
        ALTER TABLE ultimatum_players ADD COLUMN elimination_round INTEGER;
    END IF;
END $$;

