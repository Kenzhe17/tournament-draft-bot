import { NextRequest, NextResponse } from 'next/server';
import pool from '@/lib/db';

export async function GET(request: NextRequest) {
  try {
    const searchParams = request.nextUrl.searchParams;
    const user_id = searchParams.get('user_id');
    const guild_id = searchParams.get('guild_id');

    if (!user_id || !guild_id) {
      return NextResponse.json({ error: 'user_id and guild_id are required' }, { status: 400 });
    }

    const result = await pool.query(
      `SELECT 
        guild_id,
        user_id,
        name,
        elo,
        wins,
        finals,
        games,
        current_streak,
        best_win_streak,
        best_loss_streak,
        total_kills,
        total_deaths,
        best_match_kills,
        total_elo_change,
        last_elo_change,
        xp,
        level,
        xp_to_next_level,
        total_earnings,
        tournament_participations,
        description,
        avatar_url,
        daily_tournament_coins,
        daily_coins_date
       FROM player_stats 
       WHERE guild_id = $1 AND user_id = $2`,
      [guild_id, user_id]
    );

    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'User profile not found' }, { status: 404 });
    }

    const profile = result.rows[0];
    return NextResponse.json({
      guild_id: profile.guild_id,
      user_id: profile.user_id,
      name: profile.name,
      elo: profile.elo,
      wins: profile.wins,
      finals: profile.finals,
      games: profile.games,
      current_streak: profile.current_streak,
      best_win_streak: profile.best_win_streak,
      best_loss_streak: profile.best_loss_streak,
      total_kills: profile.total_kills,
      total_deaths: profile.total_deaths,
      best_match_kills: profile.best_match_kills,
      total_elo_change: profile.total_elo_change,
      last_elo_change: profile.last_elo_change,
      xp: profile.xp,
      level: profile.level,
      xp_to_next_level: profile.xp_to_next_level,
      total_earnings: profile.total_earnings,
      tournament_participations: profile.tournament_participations,
      description: profile.description,
      avatar_url: profile.avatar_url,
      daily_tournament_coins: profile.daily_tournament_coins,
      daily_coins_date: profile.daily_coins_date,
      win_rate: profile.games > 0 ? ((profile.wins / profile.games) * 100).toFixed(1) : '0.0',
      kd_ratio: profile.total_deaths > 0 ? (profile.total_kills / profile.total_deaths).toFixed(2) : '0.00',
    });
  } catch (error) {
    console.error('User profile API error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
