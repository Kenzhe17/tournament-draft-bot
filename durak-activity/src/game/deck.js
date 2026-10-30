// Card suits and ranks for Russian Durak (36 cards)
export const SUITS = ['♠', '♥', '♦', '♣']
export const SUIT_NAMES = { '♠': 'Пики', '♥': 'Черви', '♦': 'Бубны', '♣': 'Трефы' }
export const RANKS = ['6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
export const RANK_NAMES = {
  '6': 'Шестёрка',
  '7': 'Семёрка',
  '8': 'Восьмёрка',
  '9': 'Девятка',
  '10': 'Десятка',
  'J': 'Валет',
  'Q': 'Дама',
  'K': 'Король',
  'A': 'Туз'
}

export const RANK_VALUES = {
  '6': 6, '7': 7, '8': 8, '9': 9, '10': 10,
  'J': 11, 'Q': 12, 'K': 13, 'A': 14
}

// Create a standard 36-card deck
export function createDeck() {
  const deck = []
  for (const suit of SUITS) {
    for (const rank of RANKS) {
      deck.push({
        id: `${rank}${suit}`,
        rank,
        suit,
        value: RANK_VALUES[rank],
        isRed: suit === '♥' || suit === '♦'
      })
    }
  }
  return shuffle(deck)
}

// Fisher-Yates shuffle
function shuffle(array) {
  const shuffled = [...array]
  for (let i = shuffled.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]]
  }
  return shuffled
}

// Check if card can beat another card
export function canBeat(attacker, defender, trumpSuit) {
  if (defender.suit === trumpSuit && attacker.suit !== trumpSuit) {
    return true
  }
  if (defender.suit === attacker.suit && defender.value > attacker.value) {
    return true
  }
  return false
}

// Check if card can be added to attack (same rank as cards on table)
export function canAddToAttack(card, tableCards) {
  if (tableCards.length === 0) return true
  const ranksOnTable = new Set(tableCards.map(c => c.rank))
  return ranksOnTable.has(card.rank)
}
