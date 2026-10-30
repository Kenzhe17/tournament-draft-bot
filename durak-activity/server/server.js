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
    currentDefender: null
  }
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
      
      // Notify all players
      io.to(gameId).emit('game_update', game)
      
      // Start game if enough players
      if (game.players.length >= game.playerCount) {
        game.phase = 'playing'
        // Initialize deck and deal cards (simplified)
        game.currentAttacker = game.players[0].id
        game.currentDefender = game.players[1].id
        io.to(gameId).emit('game_start', game)
      }
    }
  })

  socket.on('game_action', ({ gameId, action, data }) => {
    const game = games.get(gameId)
    if (!game) return
    
    // Handle game actions (attack, defend, take, done)
    // This will be expanded with full game logic
    console.log('Game action:', gameId, action, data)
    
    io.to(gameId).emit('game_update', game)
  })

  socket.on('disconnect', () => {
    console.log('Client disconnected:', socket.id)
  })
})

const PORT = process.env.PORT || 3001
httpServer.listen(PORT, () => {
  console.log(`Durak server running on port ${PORT}`)
})
