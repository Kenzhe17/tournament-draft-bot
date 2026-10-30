import React, { useState } from 'react'
import Card from './Card'
import { createDeck, canBeat, canAddToAttack } from '../game/deck'
import './GameTable.css'

const GameTable = ({ gameState, player, onGameStateChange }) => {
  const [selectedCard, setSelectedCard] = useState(null)
  const [table, setTable] = useState([])
  const [hand, setHand] = useState([])
  const [trump, setTrump] = useState(null)
  const [deck, setDeck] = useState([])

  // Initialize game when component mounts
  React.useEffect(() => {
    const newDeck = createDeck()
    const trumpCard = newDeck[newDeck.length - 1]
    setDeck(newDeck.slice(0, -1))
    setTrump(trumpCard)
    setHand(newDeck.slice(0, 6))
  }, [])

  const handleCardClick = (card) => {
    if (selectedCard?.id === card.id) {
      setSelectedCard(null)
    } else {
      setSelectedCard(card)
    }
  }

  const handleAttack = () => {
    if (!selectedCard) return
    if (table.length === 0 || canAddToAttack(selectedCard, table)) {
      setTable([...table, { card: selectedCard, defender: null }])
      setHand(hand.filter(c => c.id !== selectedCard.id))
      setSelectedCard(null)
    }
  }

  const handleDefend = () => {
    if (!selectedCard || table.length === 0) return
    const lastAttack = table[table.length - 1]
    if (!lastAttack.defender && canBeat(lastAttack.card, selectedCard, trump?.suit)) {
      const newTable = [...table]
      newTable[newTable.length - 1].defender = selectedCard
      setTable(newTable)
      setHand(hand.filter(c => c.id !== selectedCard.id))
      setSelectedCard(null)
    }
  }

  const handleTake = () => {
    const cardsToTake = table.flatMap(t => [t.card, t.defender].filter(Boolean))
    setHand([...hand, ...cardsToTake])
    setTable([])
  }

  const handleDone = () => {
    // Clear table and draw cards
    setTable([])
    const cardsToDraw = Math.min(6 - hand.length, deck.length)
    setHand([...hand, ...deck.slice(0, cardsToDraw)])
    setDeck(deck.slice(cardsToDraw))
  }

  return (
    <div className="game-table">
      {/* Other players */}
      <div className="other-players">
        <div className="player-slot">
          <div className="player-name">Игрок 2</div>
          <div className="cards-back">🂠 🂠 🂠 🂠 🂠 🂠</div>
        </div>
      </div>

      {/* Center area */}
      <div className="center-area">
        {/* Deck and Trump */}
        <div className="deck-area">
          <div className="deck">
            <div className="card-back">🂠</div>
            <div className="deck-count">{deck.length}</div>
          </div>
          {trump && (
            <div className="trump-card">
              <Card card={trump} size="small" disabled />
            </div>
          )}
        </div>

        {/* Table */}
        <div className="table">
          {table.map((pair, index) => (
            <div key={index} className="card-pair">
              <Card card={pair.card} size="normal" disabled />
              {pair.defender && <Card card={pair.defender} size="normal" disabled />}
            </div>
          ))}
        </div>
      </div>

      {/* Player's hand */}
      <div className="player-hand">
        <div className="hand-label">Ваша рука</div>
        <div className="cards">
          {hand.map((card) => (
            <Card
              key={card.id}
              card={card}
              onClick={() => handleCardClick(card)}
              selected={selectedCard?.id === card.id}
              size="normal"
            />
          ))}
        </div>
      </div>

      {/* Game controls */}
      <div className="game-controls">
        <button className="control-button attack" onClick={handleAttack} disabled={!selectedCard}>
          Атаковать
        </button>
        <button className="control-button defend" onClick={handleDefend} disabled={!selectedCard}>
          Защищаться
        </button>
        <button className="control-button take" onClick={handleTake} disabled={table.length === 0}>
          Взять
        </button>
        <button className="control-button done" onClick={handleDone} disabled={table.length === 0}>
          Бито
        </button>
      </div>
    </div>
  )
}

export default GameTable
