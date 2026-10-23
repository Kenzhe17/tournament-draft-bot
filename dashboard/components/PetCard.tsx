'use client';

import { useState, useEffect } from 'react';

interface Pet {
  pet_id: number;
  owner_1_id: number;
  owner_2_id: number | null;
  pet_type: string;
  rarity: string;
  pet_name: string;
  level: number;
  exp: number;
  fullness: number;
  energy: number;
  last_fed_at: string;
  created_at: string;
}

interface PetCardProps {
  userId: string;
  guildId: string;
}

export default function PetCard({ userId, guildId }: PetCardProps) {
  const [pet, setPet] = useState<Pet | null>(null);
  const [loading, setLoading] = useState(true);
  const [feeding, setFeeding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchPet();
  }, [userId]);

  const fetchPet = async () => {
    try {
      setLoading(true);
      const response = await fetch(`/api/pet?user_id=${userId}`);
      const data = await response.json();

      if (response.ok) {
        setPet(data);
        setError(null);
      } else {
        setError(data.error || 'Failed to fetch pet');
      }
    } catch (err) {
      setError('Network error');
    } finally {
      setLoading(false);
    }
  };

  const handleFeed = async () => {
    try {
      setFeeding(true);
      const response = await fetch('/api/pet/feed', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, guild_id: guildId }),
      });

      const data = await response.json();

      if (response.ok) {
        setPet(data.pet);
      } else {
        setError(data.error || 'Failed to feed pet');
      }
    } catch (err) {
      setError('Network error');
    } finally {
      setFeeding(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[40vh]">
        <div className="text-r1z3-text">Loading pet...</div>
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

  if (!pet) {
    return (
      <div className="bg-white rounded-lg shadow-md p-6 border border-r1z3-matcha">
        <p className="text-r1z3-text">No pet found. Visit the shop to get one!</p>
      </div>
    );
  }

  const fullnessPercent = pet.fullness;
  const energyPercent = pet.energy;
  const expPercent = Math.min((pet.exp / (pet.level * 50)) * 100, 100);

  return (
    <div className="max-w-2xl mx-auto">
      <div className="bg-white rounded-lg shadow-md p-6 border border-r1z3-matcha">
        <div className="flex items-start justify-between mb-6">
          <div>
            <h2 className="text-3xl font-bold text-r1z3-dark">{pet.pet_name}</h2>
            <p className="text-r1z3-text capitalize">{pet.pet_type.replace('_', ' ')}</p>
            <span className={`inline-block px-3 py-1 rounded-full text-sm mt-2 ${
              pet.rarity === 'legendary' ? 'bg-yellow-200 text-yellow-800' :
              pet.rarity === 'epic' ? 'bg-purple-200 text-purple-800' :
              pet.rarity === 'uncommon' ? 'bg-blue-200 text-blue-800' :
              'bg-gray-200 text-gray-800'
            }`}>
              {pet.rarity}
            </span>
          </div>
          <div className="text-right">
            <p className="text-2xl font-bold text-r1z3-dark">Level {pet.level}</p>
            <p className="text-r1z3-text">EXP: {pet.exp}</p>
          </div>
        </div>

        <div className="space-y-4 mb-6">
          <div>
            <div className="flex justify-between mb-1">
              <span className="text-r1z3-text">Fullness</span>
              <span className="text-r1z3-dark font-semibold">{fullnessPercent}%</span>
            </div>
            <div className="w-full bg-r1z3-lavender rounded-full h-3">
              <div
                className="bg-r1z3-peach h-3 rounded-full transition-all duration-300"
                style={{ width: `${fullnessPercent}%` }}
              />
            </div>
          </div>

          <div>
            <div className="flex justify-between mb-1">
              <span className="text-r1z3-text">Energy</span>
              <span className="text-r1z3-dark font-semibold">{energyPercent}%</span>
            </div>
            <div className="w-full bg-r1z3-lavender rounded-full h-3">
              <div
                className="bg-r1z3-matcha h-3 rounded-full transition-all duration-300"
                style={{ width: `${energyPercent}%` }}
              />
            </div>
          </div>

          <div>
            <div className="flex justify-between mb-1">
              <span className="text-r1z3-text">Experience</span>
              <span className="text-r1z3-dark font-semibold">{expPercent.toFixed(1)}%</span>
            </div>
            <div className="w-full bg-r1z3-lavender rounded-full h-3">
              <div
                className="bg-r1z3-lavender-dark h-3 rounded-full transition-all duration-300"
                style={{ width: `${expPercent}%`, backgroundColor: '#D8E2DC' }}
              />
            </div>
          </div>
        </div>

        <div className="mb-6 p-4 bg-r1z3-beige rounded-lg">
          <h3 className="font-semibold text-r1z3-dark mb-2">Guardians</h3>
          <p className="text-r1z3-text">
            Owner: <span className="font-semibold">{pet.owner_1_id}</span>
            {pet.owner_2_id && (
              <span className="ml-4">
                Co-owner: <span className="font-semibold">{pet.owner_2_id}</span>
              </span>
            )}
          </p>
        </div>

        <button
          onClick={handleFeed}
          disabled={feeding || fullnessPercent >= 100}
          className={`w-full py-3 rounded-lg font-semibold transition-colors ${
            feeding || fullnessPercent >= 100
              ? 'bg-r1z3-lavender text-r1z3-text cursor-not-allowed'
              : 'bg-r1z3-peach text-r1z3-dark hover:bg-r1z3-matcha'
          }`}
        >
          {feeding ? 'Feeding...' : fullnessPercent >= 100 ? 'Already Full' : 'Feed Pet (+20 Fullness)'}
        </button>
      </div>
    </div>
  );
}
