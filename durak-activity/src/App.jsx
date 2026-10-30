import { useState, useEffect } from 'react'
import { discordSdk } from './discord-sdk'
import GameTable from './components/GameTable'
import Lobby from './components/Lobby'
import './App.css'

function App() {
  const [isConnected, setIsConnected] = useState(false)
  const [inLobby, setInLobby] = useState(true)
  const [gameState, setGameState] = useState(null)
  const [player, setPlayer] = useState(null)

  useEffect(() => {
    // Initialize Discord SDK
    const initDiscord = async () => {
      try {
        await discordSdk.ready()
        const { user } = await discordSdk.commands.authenticate()
        setPlayer(user)
        setIsConnected(true)
      } catch (error) {
        console.error('Failed to initialize Discord SDK:', error)
      }
    }

    initDiscord()
  }, [])

  if (!isConnected) {
    return (
      <div className="loading-screen">
        <div className="loading-spinner"></div>
        <p>Подключение к Discord...</p>
      </div>
    )
  }

  if (inLobby) {
    return (
      <Lobby
        player={player}
        onStartGame={() => setInLobby(false)}
        onGameStateChange={setGameState}
      />
    )
  }

  return (
    <GameTable
      gameState={gameState}
      player={player}
      onGameStateChange={setGameState}
    />
  )
}

export default App
