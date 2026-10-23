import { NextRequest, NextResponse } from 'next/server';
import pool from '@/lib/db';

export async function GET(request: NextRequest) {
  try {
    const searchParams = request.nextUrl.searchParams;
    const user_id = searchParams.get('user_id');

    if (!user_id) {
      return NextResponse.json({ error: 'user_id is required' }, { status: 400 });
    }

    const result = await pool.query(
      `SELECT 
        pet_id,
        owner_1_id,
        owner_2_id,
        pet_type,
        rarity,
        pet_name,
        level,
        exp,
        fullness,
        energy,
        last_fed_at,
        created_at,
        protected_item_id,
        hunger_started_at
       FROM pets 
       WHERE owner_1_id = $1 OR owner_2_id = $1`,
      [user_id]
    );

    if (result.rows.length === 0) {
      return NextResponse.json({ error: 'Pet not found' }, { status: 404 });
    }

    const pet = result.rows[0];
    return NextResponse.json({
      pet_id: pet.pet_id,
      owner_1_id: pet.owner_1_id,
      owner_2_id: pet.owner_2_id,
      pet_type: pet.pet_type,
      rarity: pet.rarity,
      pet_name: pet.pet_name,
      level: pet.level,
      exp: pet.exp,
      fullness: pet.fullness,
      energy: pet.energy,
      last_fed_at: pet.last_fed_at,
      created_at: pet.created_at,
      protected_item_id: pet.protected_item_id,
      hunger_started_at: pet.hunger_started_at,
    });
  } catch (error) {
    console.error('Pet API error:', error);
    return NextResponse.json({ error: 'Internal server error' }, { status: 500 });
  }
}
