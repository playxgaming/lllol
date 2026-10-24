# Telegram Auto-Forwarder

Runs as **your own Telegram account** and automatically copies or forwards new
posts from chosen chats/channels to other chats/channels, so you don't have to
do it by hand.

- `copy` mode: re-posts the content as your own post (no "Forwarded from")
- `forward` mode: normal Telegram forward
- Albums (multiple photos/videos) stay together
- Keyword/regex filters, media-only/text-only, text replace, footer text
- Handles Telegram flood limits and runs 24/7 on a server

## 1. Get your API keys

1. Open https://my.telegram.org and log in with your phone number.
2. Go to **API development tools**, create an app (any name).
3. Copy the `api_id` and `api_hash`.

## 2. Set up

```bash
git clone <this repo> telegram-forwarder && cd telegram-forwarder
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                  # put your API_ID and API_HASH here
cp config.example.yaml config.yaml    # put your rules here
```

## 3. Log in (once)

```bash
python forwarder.py login
```

Enter your phone number, the code Telegram sends you, and your 2FA password if
you have one. The session is saved in `sessions/`. **Keep that folder and `.env`
private: anyone with them can use your account.**

## 4. Find chat IDs and write rules

```bash
python forwarder.py chats
```

prints every chat with its ID. Use the ID (or `@username`) in `config.yaml`.
See `config.example.yaml` for all options.

## 5. Run

```bash
python forwarder.py run       # add -v for debug logs
```

## Running 24/7 on a server (VPS)

Do steps 2–4 on the server (login needs the terminal once). Then either:

**systemd**

```bash
sudo cp telegram-forwarder.service /etc/systemd/system/   # edit User/paths first
sudo systemctl enable --now telegram-forwarder
journalctl -u telegram-forwarder -f                       # watch logs
```

**Docker**

```bash
docker compose run --rm forwarder python forwarder.py login   # once
docker compose up -d
docker compose logs -f
```

After changing `config.yaml`, restart the service/container.

## Notes

- Chats with "Restrict saving content" turned on can't be copied or forwarded;
  the error is logged and other rules keep working.
- Only new posts are handled, not old history.
- Automating a personal account is allowed by Telegram's API terms, but
  spamming or mass-forwarding into many chats can get the account limited.
  Keep the volume reasonable.
