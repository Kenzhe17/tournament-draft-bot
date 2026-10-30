import React, { useState, useEffect } from 'react'
import { io } from 'socket.io-client'
import Card from './Card'
import { canBeat, canAddToAttack } from '../game/deck'
import './GameTable.css'

const GameTable = ({ gameState, player, onGameStateChange }) => {
  const [selectedCard, setSelectedCard] = useState(null)
  const [game, setGame] = useState(gameState)
  const [socket, setSocket] = useState(null)

  // Connect to Socket.IO server
  useEffect(() => {
    const newSocket = io('http://localhost:3001')
    setSocket(newSocket)

    // Join game
    newSocket.emit('join_game', {
      gameId: game?.id || 'default',
      player
    })

    // Listen for game updates
    newSocket.on('game_update', (updatedGame) => {
      setGame(updatedGame)
      onGameStateChange(updatedGame)
    })

    newSocket.on('game_start', (startedGame) => {
      setGame(startedGame)
      onGameStateChange(startedGame)
    })

    return () => {
      newSocket.disconnect()
    }
  }, [player])

  const handleCardClick = (card) => {
    if (selectedCard?.id === card.id) {
      setSelectedCard(null)
    } else {
      setSelectedCard(card)
    }
  }

  const handleAttack = () => {
    if (!selectedCard || !socket || !game) return
    
    const isAttacker = player.id === game.currentAttacker
    if (!isAttacker) return
    
    const hand = game.hands[player.id] || []
    const card = hand.find(c => c.id === selectedCard.id)
    
    if (card && (game.table.length === 0 || canAddToAttack(card, game.table))) {
      socket.emit('game_action', {
        gameId: game.id,
        action: 'attack',
        data: { card }
      })
      setSelectedCard(null)
    }
  }

  const handleDefend = () => {
    if (!selectedCard || !socket || !game) return
    
    const isDefender = player.id === game.currentDefender
    if (!isDefender) return
    
    const hand = game.hands[player.id] || []
    const card = hand.find(c => c.id === selectedCard.id)
    const lastAttack = game.table[game.table.length - 1]
    
    if (card && lastAttack && !lastAttack.defender && canBeat(lastAttack.card, card, game.trumpSuit)) {
      socket.emit('game_action', {
        gameId: game.id,
        action: 'defend',
        data: { card }
      })
      setSelectedCard(null)
    }
  }

  const handleTake = () => {
    if (!socket || !game) return
    
    const isDefender = player.id === game.currentDefender
    if (!isDefender || game.table.length === 0) return
    
    socket.emit('game_action', {
      gameId: game.id,
      action: 'take',
      data: {}
    })
  }

  const handleDone = () => {
    if (!socket || !game) return
    
    const isAttacker = player.id === game.currentAttacker
    if (!isAttacker || game.table.length === 0) return
    
    socket.emit('game_action', {
      gameId: game.id,
      action: 'done',
      data: {}
    })
  }

  const handleTransfer = () => {
    if (!selectedCard || !socket || !game) return
    
    const isDefender = player.id === game.currentDefender
    if (!isDefender || game.mode !== 'perevodnoy') return
    
    const hand = game.hands[player.id] || []
    const card = hand.find(c => c.id === selectedCard.id)
    const lastAttack = game.table[game.table.length - 1]
    
    if (card && lastAttack && card.rank === lastAttack.card.rank) {
      socket.emit('game_action', {
        gameId: game.id,
        action: 'transfer',
        data: { card }
      })
      setSelectedCard(null)
    }
  }

  if (!game) {
    return <div className="loading">Загрузка игры...</div>
  }

  const hand = game.hands[player.id] || []
  const isAttacker = player.id === game.currentAttacker
  const isDefender = player.id === game.currentDefender
  const isMyTurn = isAttacker || isDefender

  return (
    <div className="game-table">
      {/* Other players */}
      <div className="other-players">
        {game.players
          .filter(p => p.id !== player.id)
          .map((otherPlayer, index) => (
            <div key={otherPlayer.id} className="player-slot">
              <div className="player-name">
                {otherPlayer.global_name || otherPlayer.username}
                {game.currentAttacker === otherPlayer.id && <span className="role-badge">⚔️</span>}
                {game.currentDefender === otherPlayer.id && <span className="role-badge">🛡️</span>}
              </div>
              <div className="cards-back">
                {'🂠 '.repeat(Math.min(6, game.hands[otherPlayer.id]?.length || 0))}
              </div>
            </div>
          ))}
      </div>

      {/* Center area */}
      <div className="center-area">
        {/* Deck and Trump */}
        <div className="deck-area">
          <div className="deck">
            <div className="card-back">🂠</div>
            <div className="deck-count">{game.drawPile?.length || 0}</div>
          </div>
          {game.trump && (
            <div className="trump-card">
              <Card card={game.trump} size="small" disabled />
            </div>
          )}
        </div>

        {/* Game status */}
        <div className="game-status">
          {game.phase === 'waiting' && <p>Ожидание игроков...</p>}
          {game.phase === 'playing' && (
            <p>
              {isAttacker ? 'Ваш ход: Атакуйте!' : isDefender ? 'Ваш ход: Защищайтесь!' : 'Ожидание хода...'}
            </p>
          )}
          {game.phase === 'finished' && (
            <p className="winner-text">
              {game.winner === player.id ? '🎉 Вы победили!' : '😢 Вы проиграли!'}
            </p>
          )}
        </div>

        {/* Table */}
        <div className="table">
          {game.table.map((pair, index) => (
            <div key={index} className="card-pair">
              <Card card={pair.card} size="normal" disabled />
              {pair.defender && <Card card={pair.defender} size="normal" disabled />}
            </div>
          ))}
        </div>
      </div>

      {/* Player's hand */}
      <div className="player-hand">
        <div className="hand-label">
          Ваша рука ({hand.length} карт)
          {isAttacker && <span className="turn-indicator">⚔️ Атакуете</span>}
          {isDefender && <span className="turn-indicator">🛡️ Защищаетесь</span>}
        </div>
        <div className="cards">
          {hand.map((card) => (
            <Card
              key={card.id}
              card={card}
              onClick={() => isMyTurn && handleCardClick(card)}
              selected={selectedCard?.id === card.id}
              disabled={!isMyTurn}
              size="normal"
            />
          ))}
        </div>
      </div>

      {/* Game controls */}
      {game.phase === 'playing' && (
        <div className="game-controls">
          {isAttacker && (
            <>
              <button className="control-button attack" onClick={handleAttack} disabled={!selectedCard}>
                Атаковать
              </button>
              <button className="control-button done" onClick={handleDone} disabled={game.table.length === 0}>
                Бито
              </button>
            </>
          )}
          {isDefender && (
            <>
              <button className="control-button defend" onClick={handleDefend} disabled={!selectedCard}>
                Защищаться
              </button>
              <button className="control-button take" onClick={handleTake} disabled={game.table.length === 0}>
                Взять
              </button>
              {game.mode === 'perevodnoy' && (
                <button className="control-button transfer" onClick={handleTransfer} disabled={!selectedCard}>
                  Перевести
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

export default GameTable
