import { NextRequest, NextResponse } from 'next/server';
import pool from '@/lib/db';

export async function GET(request: NextRequest) {
  try {
    const searchParams = request.nextUrl.searchParams;
    const guild_id = searchParams.get('guild_id');
    const limit = parseInt(searchParams.get('limit') || '10');

    if (!guild_id) {
      return NextResponse.json({ error: 'guild_id is required' }, { status: 400 });
    }

    const result = await pool.query(
      `SELECT 
        user_id, 
        name, 
        elo, 
        wins, 
        games, 
        level, 
        avatar_url,
        total_kills,
        total_deaths
       FROM player_stats 
       WHERE guild_id = $1 
       ORDER BY elo DESC 
       LIMIT $2`,
      [guild_id, limit]
    );

    const players = result.rows.map(row => ({
      user_id: row.user_id,
      name: row.name,
      elo: row.elo,
      wins: row.wins,
      games: row.games,
      level: row.level,
      avatar_url: row.avatar_url,
      total_kills: row.total_kills,
      total_deaths: row.total_deaths,
      win_rate: row.games > 0 ? ((row.wins / row.games) * 100).toFixed(1) : '0.0',
      kd_ratio: row.total_deaths > 0 ? (row.total_kills / row.total_deaths).toFixed(2) : '0.00',
    }));

    return NextResponse.json({ players });
  } catch (error) {
    console.error('Leaderboard API error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
