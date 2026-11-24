# Deploy BheedBus for free (Hugging Face Spaces)

You get a permanent address like `https://YOURNAME-bheedbus.hf.space`, which works when your Mac is off. That keeps the WhatsApp webhook and QR codes stable, with no tunnel. It's free and needs no card: 16 GB RAM, 2 CPUs. The Space sleeps after about 48 h without visitors, and the next visit wakes it in about a minute.

**What runs online:** the planner, maps, operators page, WhatsApp bot with menus, voice notes (Whisper), and the English/Hinglish question parser.

**What doesn't:** the local AI (Qwen 7B), which is too slow on free CPUs, and rebuilding the model from uploads. Your Mac keeps the full version for live demos.

> Data check: the Space publishes aggregated crowding estimates (no passenger data). Make sure the hackathon's data terms allow that, or choose **Private** in step 2 and share access with the judges.

## 1. Account and token (once)

1. Sign up at <https://huggingface.co/join> and confirm your email.
2. Go to **Settings → Access Tokens → Create new token**, choose type **Write**, and copy it.
3. Log in from the project folder. Paste the token when asked; it isn't shown on screen:

```bash
.venv/bin/hf auth login
```

## 2. Create the Space and upload

Replace `YOURNAME` with your Hugging Face username. Add `--private` to the first command if you want a private Space.

```bash
.venv/bin/hf repos create YOURNAME/bheedbus --repo-type space --space-sdk docker
```
```bash
./deploy/prepare_space.sh
```
```bash
.venv/bin/hf upload YOURNAME/bheedbus build/hf-space . --repo-type space
```

The upload is about 210 MB. Then open `https://huggingface.co/spaces/YOURNAME/bheedbus`. The first build takes about 5–8 minutes; watch the **Logs** tab. When it says *Running*, the app is live at `https://YOURNAME-bheedbus.hf.space`.

**To update later:** run `prepare_space.sh` and the `hf upload` command again.

## 3. Secrets for WhatsApp (in the Space, never in the code)

On the Space page, go to **Settings → Variables and secrets → New secret** and add each of these. Copy the values from your local `.env`.

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from @BotFather (only one running copy may use it) |
| `WHATSAPP_TOKEN` | your Meta access token |
| `WHATSAPP_PHONE_ID` | Meta app → WhatsApp → API Setup → Phone number ID |
| `WHATSAPP_NUMBER` | the bot's number in international form, e.g. `15551234567` |
| `WHATSAPP_VERIFY_TOKEN` | same as in `.env` |
| `WHATSAPP_APP_SECRET` | Meta app → App settings → Basic → App secret (recommended: rejects fake webhooks) |
| `PUBLIC_URL` | `https://YOURNAME-bheedbus.hf.space` |

The Space restarts by itself after you save secrets.

## 4. Point Meta at the new address

In your Meta app:

- **Use cases → WhatsApp → Step 2 → Configure Webhooks → Edit:** Callback URL `https://YOURNAME-bheedbus.hf.space/webhook/whatsapp`, with the same verify token → **Verify and save**.
- **App settings → Basic → Privacy Policy URL:** `https://YOURNAME-bheedbus.hf.space/privacy`

You can now stop the cloudflared tunnel. WhatsApp talks to the Space directly.

## 5. Keep the token alive

Meta's temporary token expires after **24 hours**. For a lasting one:

1. Go to business.facebook.com → **Settings → Users → System users → Add**, with the Admin role.
2. Click **Assign assets**: add the BheedBus app (full control) and the WhatsApp account.
3. Click **Generate token**: choose the BheedBus app, set expiry to *Never*, and tick `whatsapp_business_messaging` and `whatsapp_business_management`.
4. Put the new token in the Space secret `WHATSAPP_TOKEN` and in your local `.env`.
