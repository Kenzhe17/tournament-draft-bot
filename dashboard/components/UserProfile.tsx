'use client';

import { useState, useEffect } from 'react';

interface Profile {
  guild_id: number;
  user_id: number;
  name: string;
  elo: number;
  wins: number;
  finals: number;
  games: number;
  current_streak: number;
  best_win_streak: number;
  best_loss_streak: number;
  total_kills: number;
  total_deaths: number;
  best_match_kills: number;
  total_elo_change: number;
  last_elo_change: number;
  xp: number;
  level: number;
  xp_to_next_level: number;
  total_earnings: number;
  tournament_participations: number;
  description: string;
  avatar_url: string | null;
  daily_tournament_coins: number;
  daily_coins_date: string;
  win_rate: string;
  kd_ratio: string;
}

interface UserProfileProps {
  userId: string;
  guildId: string;
}

export default function UserProfile({ userId, guildId }: UserProfileProps) {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchProfile();
  }, [userId, guildId]);

  const fetchProfile = async () => {
    try {
      setLoading(true);
      const response = await fetch(`/api/user/profile?user_id=${userId}&guild_id=${guildId}`);
      const data = await response.json();

      if (response.ok) {
        setProfile(data);
        setError(null);
      } else {
        setError(data.error || 'Failed to fetch profile');
      }
    } catch (err) {
      setError('Network error');
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[40vh]">
        <div className="text-r1z3-text">Loading profile...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-white rounded-lg shadow-md p-6 border border-r1z3-matcha">
        <p className="text-red-500">{error}</p>
      </div>
    );
  }

  if (!profile) {
    return (
      <div className="bg-white rounded-lg shadow-md p-6 border border-r1z3-matcha">
        <p className="text-r1z3-text">Profile not found</p>
      </div>
    );
  }

  const expPercent = (profile.xp / profile.xp_to_next_level) * 100;

  return (
    <div className="max-w-4xl mx-auto">
      <div className="bg-white rounded-lg shadow-md p-6 border border-r1z3-matcha">
        <div className="flex items-start gap-6 mb-8">
          {profile.avatar_url ? (
            <img
              src={profile.avatar_url}
              alt={profile.name}
              className="w-24 h-24 rounded-full border-4 border-r1z3-peach"
            />
          ) : (
            <div className="w-24 h-24 rounded-full bg-r1z3-lavender flex items-center justify-center text-4xl">
              👤
            </div>
          )}
          
          <div className="flex-1">
            <h2 className="text-3xl font-bold text-r1z3-dark">{profile.name}</h2>
            <p className="text-r1z3-text mb-4">ID: {profile.user_id}</p>
            
            <div className="flex gap-4 mb-4">
              <span className="bg-r1z3-peach text-r1z3-dark px-4 py-2 rounded-lg font-semibold">
                Level {profile.level}
              </span>
              <span className="bg-r1z3-matcha text-r1z3-dark px-4 py-2 rounded-lg font-semibold">
                ELO: {profile.elo}
              </span>
            </div>

            <div className="mb-4">
              <div className="flex justify-between mb-1">
                <span className="text-r1z3-text">XP Progress</span>
                <span className="text-r1z3-dark font-semibold">{profile.xp} / {profile.xp_to_next_level}</span>
              </div>
              <div className="w-full bg-r1z3-lavender rounded-full h-3">
                <div
                  className="bg-r1z3-peach h-3 rounded-full transition-all duration-300"
                  style={{ width: `${expPercent}%` }}
                />
              </div>
            </div>

            {profile.description && (
              <p className="text-r1z3-text italic">{profile.description}</p>
            )}
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
          <div className="bg-r1z3-beige rounded-lg p-4">
            <h3 className="font-semibold text-r1z3-dark mb-3">Statistics</h3>
            <div className="space-y-2">
              <div className="flex justify-between">
                <span className="text-r1z3-text">Wins</span>
                <span className="font-semibold text-r1z3-dark">{profile.wins}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-r1z3-text">Finals</span>
                <span className="font-semibold text-r1z3-dark">{profile.finals}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-r1z3-text">Games</span>
                <span className="font-semibold text-r1z3-dark">{profile.games}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-r1z3-text">Win Rate</span>
                <span className="font-semibold text-r1z3-dark">{profile.win_rate}%</span>
              </div>
              <div className="flex justify-between">
                <span className="text-r1z3-text">K/D Ratio</span>
                <span className="font-semibold text-r1z3-dark">{profile.kd_ratio}</span>
              </div>
            </div>
          </div>

          <div className="bg-r1z3-beige rounded-lg p-4">
            <h3 className="font-semibold text-r1z3-dark mb-3">Performance</h3>
            <div className="space-y-2">
              <div className="flex justify-between">
                <span className="text-r1z3-text">Total Kills</span>
                <span className="font-semibold text-r1z3-dark">{profile.total_kills}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-r1z3-text">Total Deaths</span>
                <span className="font-semibold text-r1z3-dark">{profile.total_deaths}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-r1z3-text">Best Match Kills</span>
                <span className="font-semibold text-r1z3-dark">{profile.best_match_kills}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-r1z3-text">Current Streak</span>
                <span className="font-semibold text-r1z3-dark">{profile.current_streak}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-r1z3-text">Best Win Streak</span>
                <span className="font-semibold text-r1z3-dark">{profile.best_win_streak}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="bg-r1z3-beige rounded-lg p-4">
          <h3 className="font-semibold text-r1z3-dark mb-3">ELO History</h3>
          <div className="space-y-2">
            <div className="flex justify-between">
              <span className="text-r1z3-text">Total ELO Change</span>
              <span className={`font-semibold ${profile.total_elo_change >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                {profile.total_elo_change >= 0 ? '+' : ''}{profile.total_elo_change}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-r1z3-text">Last ELO Change</span>
              <span className={`font-semibold ${profile.last_elo_change >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                {profile.last_elo_change >= 0 ? '+' : ''}{profile.last_elo_change}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
