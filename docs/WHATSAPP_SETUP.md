# Connect BheedBus to WhatsApp

You'll get a WhatsApp number that answers text messages and **voice notes** (English, Hinglish, Hindi), with a text answer plus an optional voice-note reply. Meta's free **test number** is enough for a demo: it can message up to 5 phone numbers that you verify.

You can try the bot first without any of this: open **Data → WhatsApp bot** in BheedBus. It uses exactly the same code path.

## 1. Create the Meta app (about 10 minutes, once)

1. Go to <https://developers.facebook.com> → **My Apps** → **Create app**. Choose the use case **"Connect with customers through WhatsApp"** (type *Business*).
2. In the app, open **WhatsApp → API Setup**. Meta gives you a **test phone number**. Note two values:
   - **Phone number ID**: goes in `WHATSAPP_PHONE_ID`.
   - **Temporary access token**: goes in `WHATSAPP_TOKEN`. It lasts 24 h; for longer, create a System User token in Business Settings.
3. Under **"To"**, add and verify your own WhatsApp number. Test numbers can only message verified numbers.
4. **App settings → Basic → App secret**: goes in `WHATSAPP_APP_SECRET`, used to check that webhooks really come from Meta.

## 2. Put the secrets in `.env`

Create a file named `.env` in the project folder (next to `config.json`). **Never share or commit this file.**

```
WHATSAPP_TOKEN=EAAG...your token...
WHATSAPP_PHONE_ID=123456789012345
WHATSAPP_VERIFY_TOKEN=bheedbus-any-words-you-like
WHATSAPP_APP_SECRET=abc123...
WHATSAPP_NUMBER=15551234567
WHATSAPP_VOICE_REPLY=1
```

`WHATSAPP_NUMBER` is the bot's number with country code and digits only. It is used for the QR code.

## 3. Give your Mac a public HTTPS address

Meta must reach your server over HTTPS. The easiest free option is a Cloudflare quick tunnel (no account needed):

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
```

It prints an address like `https://random-words.trycloudflare.com`. Keep that terminal open. Then add this line to `.env`, so the QR codes use the public link:

```
PUBLIC_URL=https://random-words.trycloudflare.com
```

Restart BheedBus after editing `.env`. Note that the quick-tunnel address changes every time you restart cloudflared.

## 4. Register the webhook

In the Meta app: **WhatsApp → Configuration → Webhook → Edit**.

- **Callback URL:** `https://random-words.trycloudflare.com/webhook/whatsapp`
- **Verify token:** the same text as `WHATSAPP_VERIFY_TOKEN`

Click **Verify and save**, then under **Webhook fields** subscribe to **messages**.

## 5. Try it

From your verified phone, message the test number:

- *Anand Vihar to Connaught Place at 6 pm, I want a seat*
- a voice note: *"Badarpur se Saket subah nau baje"*

BheedBus marks the message read, transcribes voice notes on your Mac (Whisper, nothing sent elsewhere), asks the local AI, replies with text, and for voice notes also sends a voice-note answer.

## How it works

```
WhatsApp ──webhook──▶ /webhook/whatsapp ──▶ voice note? ──▶ Whisper (local) ──▶ text
                                                                                   │
WhatsApp ◀── text + voice note ◀── macOS voice + ffmpeg ◀── BheedBus AI + planner ◀─┘
```

## Notes

- **Cost:** Meta's test number is free. Replies to users who message you first are free within 24 hours under WhatsApp's pricing for user-started chats. Check Meta's current pricing before going public.
- **Privacy:** voice notes are transcribed locally and deleted after use. Conversation memory (the last 3 exchanges per number) is kept only in memory.
- **QR codes:** the 📱 **Share** button and `/poster` (a printable A4 bus-stop poster) show a QR for the website and one that opens a WhatsApp chat with the bot (`wa.me` link).

## Ride feedback ("how full was it?")

The bot only ever replies; it never sends a message on its own. After a trip answer, it remembers that trip (for 24 hours, on disk, so restarts and going back to the menu don't lose it). The rider taps **⭐ Rate my last ride** in the menu to say how full the bus was. Answers are scored in the City tab, under **Riders check the model**.
