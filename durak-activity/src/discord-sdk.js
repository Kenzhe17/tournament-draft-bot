import { discordSdk } from '@discord/embedded-app-sdk'

// Configure Discord SDK
discordSdk.configuration = {
  clientId: '1782609823367873392',
  scopes: ['identify', 'guilds'],
}

export { discordSdk }
