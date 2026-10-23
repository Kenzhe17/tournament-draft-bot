'use client';

import { useState, useEffect } from 'react';

interface Player {
  user_id: number;
  name: string;
  elo: number;
  wins: number;
  games: number;
  level: number;
  avatar_url: string | null;
  total_kills: number;
  total_deaths: number;
  win_rate: string;
  kd_ratio: string;
}

interface LeaderboardProps {
  guildId: string;
}

export default function Leaderboard({ guildId }: LeaderboardProps) {
  const [players, setPlayers] = useState<Player[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchLeaderboard();
  }, [guildId]);

  const fetchLeaderboard = async () => {
    try {
      setLoading(true);
      const response = await fetch(`/api/leaderboard?guild_id=${guildId}&limit=10`);
      const data = await response.json();

      if (response.ok) {
        setPlayers(data.players);
        setError(null);
      } else {
        setError(data.error || 'Failed to fetch leaderboard');
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
        <div className="text-r1z3-text">Loading leaderboard...</div>
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

  return (
    <div className="max-w-4xl mx-auto">
      <div className="bg-white rounded-lg shadow-md p-6 border border-r1z3-matcha">
        <h2 className="text-3xl font-bold text-r1z3-dark mb-6">Leaderboard</h2>
        
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-r1z3-matcha">
                <th className="text-left py-3 px-4 text-r1z3-dark font-semibold">Rank</th>
                <th className="text-left py-3 px-4 text-r1z3-dark font-semibold">Player</th>
                <th className="text-left py-3 px-4 text-r1z3-dark font-semibold">Level</th>
                <th className="text-left py-3 px-4 text-r1z3-dark font-semibold">ELO</th>
                <th className="text-left py-3 px-4 text-r1z3-dark font-semibold">Win Rate</th>
                <th className="text-left py-3 px-4 text-r1z3-dark font-semibold">K/D</th>
                <th className="text-left py-3 px-4 text-r1z3-dark font-semibold">Wins</th>
                <th className="text-left py-3 px-4 text-r1z3-dark font-semibold">Games</th>
              </tr>
            </thead>
            <tbody>
              {players.map((player, index) => (
                <tr key={player.user_id} className="border-b border-r1z3-lavender hover:bg-r1z3-beige transition-colors">
                  <td className="py-3 px-4">
                    <span className={`font-bold ${
                      index === 0 ? 'text-yellow-500' :
                      index === 1 ? 'text-gray-400' :
                      index === 2 ? 'text-amber-600' :
                      'text-r1z3-text'
                    }`}>
                      #{index + 1}
                    </span>
                  </td>
                  <td className="py-3 px-4">
                    <div className="flex items-center gap-3">
                      {player.avatar_url && (
                        <img
                          src={player.avatar_url}
                          alt={player.name}
                          className="w-10 h-10 rounded-full"
                        />
                      )}
                      <span className="font-semibold text-r1z3-dark">{player.name}</span>
                    </div>
                  </td>
                  <td className="py-3 px-4">
                    <span className="bg-r1z3-peach text-r1z3-dark px-3 py-1 rounded-full text-sm font-semibold">
                      Lvl {player.level}
                    </span>
                  </td>
                  <td className="py-3 px-4 font-semibold text-r1z3-dark">{player.elo}</td>
                  <td className="py-3 px-4 text-r1z3-text">{player.win_rate}%</td>
                  <td className="py-3 px-4 text-r1z3-text">{player.kd_ratio}</td>
                  <td className="py-3 px-4 text-r1z3-text">{player.wins}</td>
                  <td className="py-3 px-4 text-r1z3-text">{player.games}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
