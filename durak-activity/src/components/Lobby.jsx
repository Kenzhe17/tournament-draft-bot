import React, { useState, useEffect } from 'react'
import { io } from 'socket.io-client'
import './Lobby.css'

const Lobby = ({ player, onStartGame, onGameStateChange }) => {
  const [gameMode, setGameMode] = useState('podkidnoy')
  const [playerCount, setPlayerCount] = useState(2)
  const [lobbyId, setLobbyId] = useState(null)
  const [gameState, setLocalGameState] = useState(null)
  const [isHost, setIsHost] = useState(false)
  const [socket, setSocket] = useState(null)
  const [players, setPlayers] = useState([])
  const [status, setStatus] = useState('waiting') // waiting, ready, playing

  useEffect(() => {
    // Connect to Socket.IO server
    const serverUrl = import.meta.env.VITE_SERVER_URL || 'http://localhost:3001'
    const newSocket = io(serverUrl)
    setSocket(newSocket)

    newSocket.on('lobby_created', ({ lobbyId: id, game }) => {
      console.log('Lobby created:', id, game)
      setLobbyId(id)
      setIsHost(true)
      setPlayers(game.players)
      setLocalGameState(game)
    })

    newSocket.on('game_update', (game) => {
      console.log('Game update:', game)
      setPlayers(game.players)
      setLocalGameState(game)
      setStatus(game.phase === 'waiting' ? 'ready' : 'playing')
      
      if (game.phase === 'playing') {
        onGameStateChange(game)
        onStartGame()
      }
    })

    newSocket.on('game_start', (game) => {
      console.log('Game started:', game)
      setLocalGameState(game)
      setStatus('playing')
      onGameStateChange(game)
      onStartGame()
    })

    newSocket.on('error', ({ message }) => {
      alert(message)
    })

    return () => {
      newSocket.disconnect()
    }
  }, [])

  const handleCreateLobby = () => {
    if (!socket) return
    
    socket.emit('create_lobby', {
      settings: {
        mode: gameMode,
        playerCount
      },
      player
    })
  }

  const handleJoinLobby = (id) => {
    if (!socket) return
    
    socket.emit('join_lobby', {
      lobbyId: id,
      player
    })
  }

  const handleStartGame = () => {
    if (isHost && gameState && players.length >= gameState.playerCount) {
      socket.emit('start_game', { lobbyId })
    }
  }

  if (status === 'playing') {
    return null // Game is running, show GameTable instead
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

        {!lobbyId ? (
          <div className="game-settings">
            <h2>Создать лобби</h2>

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

            <button className="start-button" onClick={handleCreateLobby}>
              Создать лобби
            </button>
          </div>
        ) : (
          <div className="lobby-room">
            <h2>Лобби</h2>
            <div className="lobby-info">
              <p>Код лобби: <code>{lobbyId}</code></p>
              <p className="players-count">Игроков: {players.length}/{gameState?.playerCount || playerCount}</p>
            </div>

            <div className="players-list">
              {players.map((p, i) => (
                <div key={p.id} className="player-item">
                  <span className="player-number">{i + 1}.</span>
                  <span className="player-name">{p.global_name || p.username}</span>
                  {isHost && p.id === player.id && <span className="host-badge"> (вы)</span>}
                </div>
              ))}
            </div>

            {isHost && players.length >= (gameState?.playerCount || playerCount) && (
              <button className="start-button" onClick={handleStartGame}>
                Начать игру
              </button>
            )}

            {!isHost && (
              <p className="waiting-text">Ожидание хоста...</p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

export default Lobby
