import * as DiscordSDK from '@discord/embedded-app-sdk'

// Create Discord SDK instance
const discordSdk = new DiscordSDK.DiscordSDK({
  clientId: '1782609823367873392',
  scopes: ['identify', 'guilds'],
})

export { discordSdk }
