# BheedBus on Telegram

The same chat as the WhatsApp bot (menus, trip plans with seat chance, the 🚌🚌 pairs tip, route crowding,
busiest routes, voice notes in English/Hinglish/Hindi with spoken replies, map images, ⭐ ride ratings), plus:

- **📍 Share my location**: when the bot asks where you're starting or going, tap the button and it lists the nearest stops;
- **commands** in Telegram's menu button: /menu /trip /route /busy /rate /help.

Telegram needs **no business verification and no public URL**. The server asks Telegram for new messages
(long polling), so it works from a laptop on any Wi-Fi: no tunnel, no webhook.

## Setup (about 2 minutes)

1. In Telegram, open a chat with **@BotFather** (the blue tick one) and send `/newbot`.
2. Give it a name (e.g. *BheedBus*) and a username ending in `bot` (e.g. `bheedbus_delhi_bot`).
3. BotFather replies with a **token**. Copy it and add one line to `.env`:

   ```
   TELEGRAM_BOT_TOKEN=<the token>
   ```

   Keep the token secret: anyone with it can control the bot. Never commit `.env`.
4. Restart the server. The terminal prints `telegram: polling as @your_bot`.
5. Open `https://t.me/<your_bot>` (or scan the QR under 📱 **Share** on the website) and send **hi**.

Optional, in BotFather: `/setdescription`, `/setuserpic` and `/setabouttext` for a nicer profile.

## Notes

- **One copy at a time.** Only one running server may poll the same bot token. If the Hugging Face copy also has
  the token, Telegram rejects the second one (error 409). Give the token to whichever copy you demo from.
  The same goes for the n8n Telegram workflow in `web/n8n/`: give that one a different bot.
- `TELEGRAM_VOICE_REPLY=0` turns off spoken replies to voice notes.
- `GET /api/telegram/status` shows whether the bot is configured and its username.
- The bot only replies; it never messages anyone first.
- Ratings from Telegram are scored with the others under **City → Riders check the model** (channel "telegram").
- `WHATSAPP_PAUSED=1` in `.env` hides the WhatsApp QR (for example while that number waits for Meta business
  verification); the Telegram QR stays.
