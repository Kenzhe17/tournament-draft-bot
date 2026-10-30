import React, { useState } from 'react'
import './Lobby.css'

const Lobby = ({ player, onStartGame, onGameStateChange }) => {
  const [gameMode, setGameMode] = useState('podkidnoy') // podkidnoy or perevodnoy
  const [playerCount, setPlayerCount] = useState(2)

  const handleStart = () => {
    const gameState = {
      mode: gameMode,
      playerCount,
      players: [{ id: player.id, username: player.username, global_name: player.global_name }],
      phase: 'waiting',
      deck: [],
      hands: {},
      table: [],
      trump: null,
      currentAttacker: null,
      currentDefender: null
    }
    onGameStateChange(gameState)
    onStartGame()
  }

  return (
    <div className="lobby">
      <div className="lobby-content">
        <h1 className="lobby-title">🃏 Дурак</h1>
        <div className="player-info">
          <div className="avatar">
            {player?.global_name || player?.username}
          </div>
        </div>

        <div className="game-settings">
          <h2>Настройки игры</h2>

          <div className="setting-group">
            <label>Режим игры:</label>
            <select
              value={gameMode}
              onChange={(e) => setGameMode(e.target.value)}
            >
              <option value="podkidnoy">Подкидной дурак</option>
              <option value="perevodnoy">Переводной дурак</option>
            </select>
          </div>

          <div className="setting-group">
            <label>Количество игроков:</label>
            <select
              value={playerCount}
              onChange={(e) => setPlayerCount(parseInt(e.target.value))}
            >
              <option value={2}>2 игрока</option>
              <option value={3}>3 игрока</option>
              <option value={4}>4 игрока</option>
              <option value={5}>5 игроков</option>
              <option value={6}>6 игроков</option>
            </select>
          </div>

          <button className="start-button" onClick={handleStart}>
            Начать игру
          </button>
        </div>

        <div className="lobby-info">
          <p>Ожидание других игроков...</p>
          <p className="players-count">Игроков: 1/{playerCount}</p>
        </div>
      </div>
    </div>
  )
}

export default Lobby
