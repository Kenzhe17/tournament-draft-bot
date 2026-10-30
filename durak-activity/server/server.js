import express from 'express'
import { createServer } from 'http'
import { Server } from 'socket.io'
import cors from 'cors'

const app = express()
app.use(cors())
app.use(express.json())

const httpServer = createServer(app)
const io = new Server(httpServer, {
  cors: {
    origin: '*',
    methods: ['GET', 'POST']
  }
})

// Store active games
const games = new Map()

// Card game logic
const SUITS = ['♠', '♥', '♦', '♣']
const RANKS = ['6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
const RANK_VALUES = {
  '6': 6, '7': 7, '8': 8, '9': 9, '10': 10,
  'J': 11, 'Q': 12, 'K': 13, 'A': 14
}

function createDeck() {
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

function shuffle(array) {
  const shuffled = [...array]
  for (let i = shuffled.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]]
  }
  return shuffled
}

function canBeat(attacker, defender, trumpSuit) {
  if (defender.suit === trumpSuit && attacker.suit !== trumpSuit) {
    return true
  }
  if (defender.suit === attacker.suit && defender.value > attacker.value) {
    return true
  }
  return false
}

function canAddToAttack(card, tableCards) {
  if (tableCards.length === 0) return true
  const ranksOnTable = new Set()
  tableCards.forEach(pair => {
    ranksOnTable.add(pair.card.rank)
    if (pair.defender) ranksOnTable.add(pair.defender.rank)
  })
  return ranksOnTable.has(card.rank)
}

// Game logic helpers
function createGame(gameId, settings) {
  return {
    id: gameId,
    mode: settings.mode || 'podkidnoy',
    playerCount: settings.playerCount || 2,
    players: [],
    phase: 'waiting',
    deck: [],
    hands: {},
    table: [],
    trump: null,
    currentAttacker: null,
    currentDefender: null,
    attackerIndex: 0,
    defenderIndex: 1,
    drawPile: [],
    discardPile: []
  }
}

function initializeGame(game) {
  const deck = createDeck()
  const trump = deck[deck.length - 1]
  const trumpSuit = trump.suit
  
  // Deal cards to each player
  game.hands = {}
  game.players.forEach(player => {
    game.hands[player.id] = deck.splice(0, 6)
  })
  
  game.deck = deck
  game.trump = trump
  game.trumpSuit = trumpSuit
  game.drawPile = deck
  game.phase = 'playing'
  game.currentAttacker = game.players[0].id
  game.currentDefender = game.players[1].id
  game.attackerIndex = 0
  game.defenderIndex = 1
}

function drawCards(game, playerId, count) {
  const hand = game.hands[playerId] || []
  const toDraw = Math.min(count, game.drawPile.length)
  game.hands[playerId] = [...hand, ...game.drawPile.splice(0, toDraw)]
  return toDraw
}

function refillHands(game) {
  game.players.forEach(player => {
    const hand = game.hands[player.id] || []
    const needed = 6 - hand.length
    if (needed > 0 && game.drawPile.length > 0) {
      drawCards(game, player.id, needed)
    }
  })
}

function checkGameOver(game) {
  // Check if any player has no cards and deck is empty
  if (game.drawPile.length === 0) {
    for (const player of game.players) {
      const hand = game.hands[player.id] || []
      if (hand.length === 0) {
        game.phase = 'finished'
        game.winner = player.id
        return true
      }
    }
  }
  return false
}

function getNextPlayerIndex(currentIndex, totalPlayers) {
  return (currentIndex + 1) % totalPlayers
}

io.on('connection', (socket) => {
  console.log('Client connected:', socket.id)

  socket.on('join_game', ({ gameId, player }) => {
    console.log('Player joining game:', gameId, player)
    
    let game = games.get(gameId)
    
    if (!game) {
      game = createGame(gameId, { mode: 'podkidnoy', playerCount: 2 })
      games.set(gameId, game)
    }
    
    // Add player to game
    if (!game.players.find(p => p.id === player.id)) {
      game.players.push(player)
      socket.join(gameId)
      socket.gameId = gameId
      socket.playerId = player.id
      
      // Notify all players
      io.to(gameId).emit('game_update', game)
      
      // Start game if enough players
      if (game.players.length >= game.playerCount) {
        initializeGame(game)
        io.to(gameId).emit('game_start', game)
      }
    }
  })

  socket.on('game_action', ({ gameId, action, data }) => {
    const game = games.get(gameId)
    if (!game) return
    
    const playerId = socket.playerId
    const isAttacker = playerId === game.currentAttacker
    const isDefender = playerId === game.currentDefender
    
    console.log('Game action:', gameId, action, playerId, data)
    
    switch (action) {
      case 'attack':
        if (isAttacker && data.card) {
          const hand = game.hands[playerId] || []
          const card = hand.find(c => c.id === data.card.id)
          
          if (card && (game.table.length === 0 || canAddToAttack(card, game.table))) {
            game.table.push({ card, defender: null })
            game.hands[playerId] = hand.filter(c => c.id !== card.id)
            io.to(gameId).emit('game_update', game)
          }
        }
        break
        
      case 'defend':
        if (isDefender && data.card) {
          const hand = game.hands[playerId] || []
          const card = hand.find(c => c.id === data.card.id)
          const lastAttack = game.table[game.table.length - 1]
          
          if (card && lastAttack && !lastAttack.defender && canBeat(lastAttack.card, card, game.trumpSuit)) {
            lastAttack.defender = card
            game.hands[playerId] = hand.filter(c => c.id !== card.id)
            io.to(gameId).emit('game_update', game)
          }
        }
        break
        
      case 'take':
        if (isDefender) {
          // Defender takes all cards from table
          const cardsToTake = game.table.flatMap(pair => [pair.card, pair.defender].filter(Boolean))
          game.hands[playerId] = [...(game.hands[playerId] || []), ...cardsToTake]
          game.table = []
          
          // Move to next turn
          game.attackerIndex = getNextPlayerIndex(game.attackerIndex, game.players.length)
          game.defenderIndex = getNextPlayerIndex(game.defenderIndex, game.players.length)
          game.currentAttacker = game.players[game.attackerIndex].id
          game.currentDefender = game.players[game.defenderIndex].id
          
          refillHands(game)
          checkGameOver(game)
          
          io.to(gameId).emit('game_update', game)
        }
        break
        
      case 'done':
        if (isAttacker && game.table.length > 0) {
          // All cards are beaten, clear table
          game.discardPile.push(...game.table.flatMap(pair => [pair.card, pair.defender].filter(Boolean)))
          game.table = []
          
          // Move to next turn
          game.attackerIndex = getNextPlayerIndex(game.attackerIndex, game.players.length)
          game.defenderIndex = getNextPlayerIndex(game.defenderIndex, game.players.length)
          game.currentAttacker = game.players[game.attackerIndex].id
          game.currentDefender = game.players[game.defenderIndex].id
          
          refillHands(game)
          checkGameOver(game)
          
          io.to(gameId).emit('game_update', game)
        }
        break
        
      case 'transfer':
        if (game.mode === 'perevodnoy' && isDefender && data.card) {
          const hand = game.hands[playerId] || []
          const card = hand.find(c => c.id === data.card.id)
          const lastAttack = game.table[game.table.length - 1]
          
          // Can only transfer if same rank
          if (card && lastAttack && card.rank === lastAttack.card.rank) {
            // Transfer attack to next player
            game.table.push({ card, defender: null })
            game.hands[playerId] = hand.filter(c => c.id !== card.id)
            
            // Move defender to next player
            game.defenderIndex = getNextPlayerIndex(game.defenderIndex, game.players.length)
            game.currentDefender = game.players[game.defenderIndex].id
            
            io.to(gameId).emit('game_update', game)
          }
        }
        break
    }
  })

  socket.on('disconnect', () => {
    console.log('Client disconnected:', socket.id)
    if (socket.gameId) {
      const game = games.get(socket.gameId)
      if (game) {
        game.players = game.players.filter(p => p.id !== socket.playerId)
        if (game.players.length === 0) {
          games.delete(socket.gameId)
        } else {
          io.to(socket.gameId).emit('game_update', game)
        }
      }
    }
  })
})

const PORT = process.env.PORT || 3001
httpServer.listen(PORT, () => {
  console.log(`Durak server running on port ${PORT}`)
})
