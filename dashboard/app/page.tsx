'use client';

import { useState, useEffect } from 'react';
import Header from '@/components/Header';
import PetCard from '@/components/PetCard';
import Leaderboard from '@/components/Leaderboard';
import UserProfile from '@/components/UserProfile';

const GUILD_ID = '1196165488565289012';

export default function Home() {
  const [activeTab, setActiveTab] = useState<'pets' | 'leaderboard' | 'profile'>('pets');
  const [userId, setUserId] = useState<string | null>(null);

  useEffect(() => {
    // For demo purposes, you can set a user ID
    // In production, this would come from Discord OAuth2 session
    const demoUserId = localStorage.getItem('r1z3_user_id');
    if (demoUserId) {
      setUserId(demoUserId);
    }
  }, []);

  const handleLogin = (id: string) => {
    setUserId(id);
    localStorage.setItem('r1z3_user_id', id);
  };

  return (
    <div className="min-h-screen bg-r1z3-beige">
      <Header activeTab={activeTab} onTabChange={setActiveTab} userId={userId} onLogin={handleLogin} />
      
      <main className="container mx-auto px-4 py-8">
        {!userId ? (
          <div className="flex flex-col items-center justify-center min-h-[60vh]">
            <h1 className="text-4xl font-bold text-r1z3-dark mb-4">Welcome to r!z3 Dashboard</h1>
            <p className="text-r1z3-text mb-8">Please enter your Discord User ID to continue</p>
            <div className="flex gap-4">
              <input
                type="text"
                placeholder="Enter Discord User ID"
                className="px-4 py-2 rounded-lg border border-r1z3-matcha bg-white text-r1z3-dark focus:outline-none focus:ring-2 focus:ring-r1z3-peach"
                onChange={(e) => setUserId(e.target.value)}
              />
              <button
                onClick={() => userId && handleLogin(userId)}
                className="px-6 py-2 bg-r1z3-peach text-r1z3-dark rounded-lg hover:bg-r1z3-matcha transition-colors"
              >
                Enter
              </button>
            </div>
          </div>
        ) : (
          <>
            {activeTab === 'pets' && <PetCard userId={userId} guildId={GUILD_ID} />}
            {activeTab === 'leaderboard' && <Leaderboard guildId={GUILD_ID} />}
            {activeTab === 'profile' && <UserProfile userId={userId} guildId={GUILD_ID} />}
          </>
        )}
      </main>
    </div>
  );
}
