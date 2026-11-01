import * as DiscordSDK from '@discord/embedded-app-sdk'

// Create Discord SDK instance
const discordSdk = new DiscordSDK({
  clientId: '1279474396079329331',
  scopes: ['identify', 'guilds'],
})

export { discordSdk }
