import { useState, useEffect } from 'react'
import { discordSdk, hasFrameId } from './discord-sdk'
import GameTable from './components/GameTable'
import Lobby from './components/Lobby'
import './App.css'

function App() {
  const [isConnected, setIsConnected] = useState(false)
  const [inLobby, setInLobby] = useState(true)
  const [gameState, setGameState] = useState(null)
  const [player, setPlayer] = useState(null)
  const [initError, setInitError] = useState(null)

  useEffect(() => {
    // If running outside Discord, use mock user immediately
    if (!hasFrameId) {
      console.log('Running outside Discord, using mock user')
      setPlayer({
        id: '123456789',
        username: 'TestUser',
        global_name: 'Test User'
      })
      setIsConnected(true)
      return
    }

    // Initialize Discord SDK only when running inside Discord
    const initDiscord = async () => {
      try {
        console.log('Initializing Discord SDK...')
        await discordSdk.ready()
        console.log('Discord SDK ready')
        const auth = await discordSdk.commands.authenticate()
        console.log('Authenticated:', auth)
        setPlayer(auth.user)
        setIsConnected(true)
      } catch (error) {
        console.error('Failed to initialize Discord SDK:', error)
        setInitError(error.message)
        // Fallback to mock user if SDK fails
        setPlayer({
          id: '123456789',
          username: 'TestUser',
          global_name: 'Test User'
        })
        setIsConnected(true)
      }
    }

    // Timeout after 5 seconds if SDK doesn't respond
    const timeout = setTimeout(() => {
      if (!isConnected) {
        console.warn('SDK initialization timeout, using fallback')
        setPlayer({
          id: '123456789',
          username: 'TestUser',
          global_name: 'Test User'
        })
        setIsConnected(true)
      }
    }, 5000)

    initDiscord()

    return () => clearTimeout(timeout)
  }, [])

  if (!isConnected) {
    return (
      <div className="loading-screen">
        <div className="loading-spinner"></div>
        <p>Подключение к Discord...</p>
        {initError && <p className="error">Ошибка: {initError}</p>}
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
