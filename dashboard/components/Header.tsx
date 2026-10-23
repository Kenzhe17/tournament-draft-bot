import React from 'react';

interface HeaderProps {
  activeTab: 'pets' | 'leaderboard' | 'profile';
  onTabChange: (tab: 'pets' | 'leaderboard' | 'profile') => void;
  userId: string | null;
  onLogin: (id: string) => void;
}

export default function Header({ activeTab, onTabChange, userId, onLogin }: HeaderProps) {
  return (
    <header className="bg-white shadow-sm border-b border-r1z3-matcha">
      <div className="container mx-auto px-4 py-4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold text-r1z3-dark">r!z3</h1>
          
          <nav className="flex gap-6">
            <button
              onClick={() => onTabChange('pets')}
              className={`px-4 py-2 rounded-lg transition-colors ${
                activeTab === 'pets'
                  ? 'bg-r1z3-peach text-r1z3-dark'
                  : 'text-r1z3-text hover:bg-r1z3-lavender'
              }`}
            >
              Pets
            </button>
            <button
              onClick={() => onTabChange('leaderboard')}
              className={`px-4 py-2 rounded-lg transition-colors ${
                activeTab === 'leaderboard'
                  ? 'bg-r1z3-peach text-r1z3-dark'
                  : 'text-r1z3-text hover:bg-r1z3-lavender'
              }`}
            >
              Leaderboard
            </button>
            <button
              onClick={() => onTabChange('profile')}
              className={`px-4 py-2 rounded-lg transition-colors ${
                activeTab === 'profile'
                  ? 'bg-r1z3-peach text-r1z3-dark'
                  : 'text-r1z3-text hover:bg-r1z3-lavender'
              }`}
            >
              Profile
            </button>
          </nav>

          {userId ? (
            <button
              onClick={() => {
                localStorage.removeItem('r1z3_user_id');
                window.location.reload();
              }}
              className="px-4 py-2 bg-r1z3-lavender text-r1z3-dark rounded-lg hover:bg-r1z3-matcha transition-colors"
            >
              Logout
            </button>
          ) : (
            <button
              onClick={() => {
                const id = prompt('Enter your Discord User ID:');
                if (id) onLogin(id);
              }}
              className="px-4 py-2 bg-r1z3-peach text-r1z3-dark rounded-lg hover:bg-r1z3-matcha transition-colors"
            >
              Discord Login
            </button>
          )}
        </div>
      </div>
    </header>
  );
}
