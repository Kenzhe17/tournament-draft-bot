import { DiscordSDK } from '@discord/embedded-app-sdk'

// Check if running inside Discord Activity (has frame_id parameter)
const urlParams = new URLSearchParams(window.location.search)
const hasFrameId = urlParams.has('frame_id')

let discordSdk = null

if (hasFrameId) {
  // Initialize SDK only when running inside Discord
  discordSdk = new DiscordSDK({
    clientId: '1279474396079329331',
    scopes: ['identify', 'guilds'],
  })
  console.log('Discord SDK initialized (running in Discord Activity)')
} else {
  console.warn('Running outside Discord Activity (no frame_id), SDK not initialized')
}

export { discordSdk, hasFrameId }
