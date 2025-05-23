# oauth_server.py
import os
import requests
import discord
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from replit import db
from datetime import datetime, timezone
from log import notify_staff_sync

app = FastAPI()

# ── Config ──
CLIENT_ID     = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
BOT_TOKEN     = os.environ["BOT_TOKEN"]
REDIRECT_URI  = os.environ["REDIRECT_URI"]
WEBHOOK_URL   = os.environ["LOG_WEBHOOK_URL"]

# ── IP Rate-Limit Store ──
ip_requests = {}
RATE_LIMIT  = 5    # calls
RATE_PERIOD = 60   # seconds

def record_ip(ip: str) -> bool:
    """Return True if rate limit exceeded."""
    now = datetime.now(timezone.utc)
    lst = ip_requests.get(ip, [])
    # keep only recent
    lst = [t for t in lst if (now - t).total_seconds() < RATE_PERIOD]
    lst.append(now)
    ip_requests[ip] = lst
    return len(lst) > RATE_LIMIT

@app.get("/oauth/callback")
async def oauth_callback(request: Request):
    ip = request.client.host
    if record_ip(ip):
        notify_staff_sync(
            "🚫 Rate Limit Exceeded",
            f"IP {ip} exceeded OAuth callback rate limit.",
            discord.Color.red()
        )
        raise HTTPException(429, "Too many requests")

    code  = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        notify_staff_sync(
            "⚠️ Invalid OAuth State",
            f"Missing code or state. ip={ip}",
            discord.Color.orange()
        )
        raise HTTPException(400, "Missing code or state")

    state_key = f"state:{state}"
    if state_key not in db:
        notify_staff_sync(
            "⚠️ Invalid OAuth State",
            f"State not found or expired: {state} (ip={ip})",
            discord.Color.orange()
        )
        raise HTTPException(400, "Invalid state")

    rec = db[state_key]
    discord_id = rec["user_id"]
    user_db_key = f"user:{discord_id}"
    now = datetime.now(timezone.utc)

    # Persist link record (first-time only)
    if user_db_key in db:
        notify_staff_sync(
            "🚫 Duplicate OAuth Attempt",
            f"<@{discord_id}> tried to re-link.",
            discord.Color.red()
        )
    else:
        db[user_db_key] = {
            "discord_id": discord_id,
            "email": None,               # placeholder until fetched
            "first_linked_at": now.isoformat()
        }

    # purge the OAuth state immediately
    del db[state_key]

    # Exchange code → token & fetch email
    try:
        token_resp = requests.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "scope": "identify email"
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        user_resp = requests.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        user_resp.raise_for_status()
        user_data = user_resp.json()
        email = user_data.get("email")
        if not email:
            raise Exception("Email scope missing")

        # update email in user record
        rec = db[user_db_key]
        rec["email"] = email
        db[user_db_key] = rec

        # log successful link
        notify_staff_sync(
            "🔗 Discord Linked",
            f"<@{discord_id}> linked ({email}).",
            discord.Color.green()
        )

    except Exception as e:
        notify_staff_sync(
            "🔥 Bot Error",
            f"OAuth token/user fetch error for <@{discord_id}>: {e}",
            discord.Color.red()
        )
        raise HTTPException(500, "OAuth failure")

    # ── Auto-dispense first key and JIT remove from pool ──
    for k, v in list(db.items()):
        if k.startswith("key:"):
            key_str = k.split("key:")[1]

            # remove from pool
            del db[k]

            # annotate user record
            rec = db[user_db_key]
            rec["dispensed_key"]     = key_str
            rec["last_dispensed_at"] = now.isoformat()
            db[user_db_key] = rec

            # DM via Discord API with an embed
            try:
                # 1) open a DM channel
                dm = requests.post(
                    "https://discord.com/api/v10/users/@me/channels",
                    headers={
                        "Authorization": f"Bot {BOT_TOKEN}",
                        "Content-Type": "application/json"
                    },
                    json={"recipient_id": discord_id}
                )
                dm.raise_for_status()
                cid = dm.json()["id"]

                # 2) build the embed payload
                embed = {
                    "title": "🎉 Your SkySpoofer Trial Key",
                    "description": (
                        f"**Key:** ```{key_str}```\n"
                        "**To Use:**\n"
                        "- Make an account [here](https://skyspoofer.com/register)\n"
                        "- Activate the key in the license tab on the dashboard.\n"
                        "- Download the software, unzip it, and run SkySpoofer.exe.\n"
                        "- After you get the message `Authentication successful!`, press connect loader in the hardware tab.\n"
                        "- Your serials will be scanned, and you may press apply changes.\n"
                        "- *Do not spoof any module you do not have or have disabled*\n\n"
                        "**Note:** This is a **temporary trial key** to showcase the software’s functionality before purchase."
                        " It is not intended for removing a hardware unban, purchase a license if you wish to do so.\n"
                        "With the trial license, serials reset on shutdown and it **won’t** bypass advanced anti-cheats like Vanguard.\n\n"
                        "To purchase a key with advanced anti-cheat bypass, visit [SkySpoofer Pricing](https://skyspoofer.com/#pricing).\n\n"
                        "You may claim another free trial in 30 days."
                    ),
                    "color": discord.Color.blurple().value,
                    "timestamp": now.isoformat()
                }


                # 3) send the embed
                requests.post(
                    f"https://discord.com/api/v10/channels/{cid}/messages",
                    headers={
                        "Authorization": f"Bot {BOT_TOKEN}",
                        "Content-Type": "application/json"
                    },
                    json={"embeds": [embed]}
                )

            except Exception as e:
                notify_staff_sync(
                    "📭 DM Delivery Failed",
                    f"Could not DM <@{discord_id}> **{key_str}**: {e}",
                    discord.Color.orange()
                )


            # log dispense
            notify_staff_sync(
                "🔑 Key Dispensed",
                f"<@{discord_id}> was issued **{key_str}**.",
                discord.Color.green()
            )
            break

    return RedirectResponse("https://skyspoofer.com")
