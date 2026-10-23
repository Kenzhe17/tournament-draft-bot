import { NextRequest, NextResponse } from 'next/server';
import pool from '@/lib/db';

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { user_id, guild_id } = body;

    if (!user_id || !guild_id) {
      return NextResponse.json({ error: 'user_id and guild_id are required' }, { status: 400 });
    }

    // Check if user has a pet
    const petResult = await pool.query(
      `SELECT pet_id, fullness FROM pets 
       WHERE owner_1_id = $1 OR owner_2_id = $1`,
      [user_id]
    );

    if (petResult.rows.length === 0) {
      return NextResponse.json({ error: 'Pet not found' }, { status: 404 });
    }

    const pet = petResult.rows[0];

    // Check if pet is already full
    if (pet.fullness >= 100) {
      return NextResponse.json({ error: 'Pet is already full' }, { status: 400 });
    }

    // Update pet fullness and last_fed_at
    const updateResult = await pool.query(
      `UPDATE pets 
       SET fullness = LEAST(100, fullness + 20),
           last_fed_at = NOW()
       WHERE pet_id = $1
       RETURNING pet_id, pet_name, fullness, energy, level, exp`,
      [pet.pet_id]
    );

    const updatedPet = updateResult.rows[0];

    return NextResponse.json({
      success: true,
      pet: {
        pet_id: updatedPet.pet_id,
        pet_name: updatedPet.pet_name,
        fullness: updatedPet.fullness,
        energy: updatedPet.energy,
        level: updatedPet.level,
        exp: updatedPet.exp,
      },
    });
  } catch (error) {
    console.error('Feed pet API error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
