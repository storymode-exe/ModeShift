#!/usr/bin/env python3
"""
twitch_username_watcher.py

Watches a Twitch username and pings you (via ntfy.sh, or a Discord webhook)
the moment it's no longer tied to an active account. Free, no third-party
subscription, uses only Twitch's own public API.

------------------------------------------------------------------------
SETUP (one-time)
------------------------------------------------------------------------
1. Register a free Twitch app:
   - Go to https://dev.twitch.tv/console/apps -> "Register Your Application"
   - Name: anything (e.g. "username-watcher")
   - OAuth Redirect URL: http://localhost  (unused, but required to fill in)
   - Category: "Application Integration"
   - Save, then copy the Client ID, and click "New Secret" to get a Client Secret.

2. Pick a notification method (ntfy.sh is the simplest, zero signup):
   - Go to https://ntfy.sh, pick a random/private topic name
     (e.g. "story-twitch-watch-8f3k2") -- anything not easily guessable,
     since anyone who knows the topic name can read your notifications.
   - Subscribe to that same topic in the ntfy phone app or at https://ntfy.sh/<topic>
     in a browser tab to actually receive the alert.
   - (Alternative: use a Discord webhook URL instead -- see NOTIFY_METHOD below.)

3. Fill in the CONFIG block below, then run:
     python3 twitch_username_watcher.py

4. Automate it with cron so it checks periodically (e.g. every 15 min):
     crontab -e
   Add a line like:
     */15 * * * * /usr/bin/python3 /path/to/twitch_username_watcher.py >> /path/to/watcher.log 2>&1

------------------------------------------------------------------------
NOTE
------------------------------------------------------------------------
An "available" result here means no ACTIVE account currently holds the
name. Twitch holds renamed/deleted usernames for a minimum of 6 months
before they're eligible to be released, and inactive-account recycling
happens in unannounced batches -- so "available" via the API doesn't
always mean signup will accept it immediately. Keep the watcher running;
it'll keep telling you "still available" once the hold expires and
you'll want to move fast.
"""

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse

# ------------------------- CONFIG -------------------------

TWITCH_CLIENT_ID = "4xgx1do7wtge966k4yobsa0y75yiaz"
TWITCH_CLIENT_SECRET = "mcgk7t8c91cbnyk4f3u12c2ntpc8bt"

# Watch as many usernames as you want -- each is checked and notified independently.
TARGET_USERNAMES = [
    "story_mode",
    "storymode",
]

# "ntfy" or "discord"
NOTIFY_METHOD = "ntfy"

# For ntfy: the topic you picked at https://ntfy.sh
NTFY_TOPIC = "story-twitch-watch-8f3k2"

# For discord: a webhook URL from Server Settings -> Integrations -> Webhooks
DISCORD_WEBHOOK_URL = ""

# Where to cache the app access token + notification state, so we don't
# re-auth every run and don't spam you every 15 minutes once it's free.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".twitch_watcher_state.json")

# ------------------------------------------------------------


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def get_app_token(state):
    """Get (and cache) an app access token via the client_credentials flow."""
    token = state.get("access_token")
    if token:
        return token

    data = urllib.parse.urlencode({
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }).encode()

    req = urllib.request.Request("https://id.twitch.tv/oauth2/token", data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        payload = json.loads(resp.read().decode())

    state["access_token"] = payload["access_token"]
    save_state(state)
    return payload["access_token"]


def check_username(username, token, state, retried=False):
    """Return True if no active account currently holds `username`."""
    url = "https://api.twitch.tv/helix/users?" + urllib.parse.urlencode({"login": username})
    req = urllib.request.Request(url)
    req.add_header("Client-Id", TWITCH_CLIENT_ID)
    req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401 and not retried:
            # Token expired/invalid -- clear it and get a fresh one, once.
            state.pop("access_token", None)
            save_state(state)
            new_token = get_app_token(state)
            return check_username(username, new_token, state, retried=True)
        raise

    return len(payload.get("data", [])) == 0


def notify(message):
    if NOTIFY_METHOD == "ntfy":
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode(),
            method="POST",
        )
        urllib.request.urlopen(req)
    elif NOTIFY_METHOD == "discord":
        data = json.dumps({"content": message}).encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req)
    else:
        print(f"[notify] Unknown NOTIFY_METHOD '{NOTIFY_METHOD}': {message}")


def main():
    if "--test-notify" in sys.argv:
        # Sends a real notification through the exact same notify() function
        # and NOTIFY_METHOD/NTFY_TOPIC/DISCORD_WEBHOOK_URL the real run uses --
        # proves the server -> ntfy/Discord -> phone path actually works,
        # independent of Twitch entirely.
        notify("Test notification from twitch_username_watcher.py -- if you got this, your server-to-phone path works.")
        print("Test notification sent.")
        return

    if "PUT_YOUR_CLIENT_ID_HERE" in TWITCH_CLIENT_ID:
        sys.exit("Fill in TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET at the top of this script first.")

    state = load_state()
    token = get_app_token(state)

    # Per-username notification flags live under state["notified"][username]
    notified = state.setdefault("notified", {})

    for username in TARGET_USERNAMES:
        available = check_username(username, token, state)

        if available:
            print(f"'{username}' looks available (no active account).")
            # Only spam you once per "newly available" event, not every 15 min.
            if not notified.get(username):
                notify(
                    f"Twitch username '{username}' looks available! "
                    f"No active account is using it right now. Go check "
                    f"https://www.twitch.tv/{username} and try to claim it. "
                    f"(Note: could still be in a 6-month hold if recently renamed/deleted.)"
                )
                notified[username] = True
                save_state(state)
        else:
            print(f"'{username}' is still taken.")
            if notified.get(username):
                notified[username] = False
                save_state(state)


if __name__ == "__main__":
    main()
