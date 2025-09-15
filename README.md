# QRZ Silent Key Monitor

Small, simple Python tool that checks a list of QRZ profile pages and lets you know when someone has been marked Silent Key or when their page was last updated.

#### Author: Ian (VK1HF) • Email: ian.vk1hf@gmail.com

Why I built it: I realized that VK2QA and VK5QD had passed away—voices I’d always heard and hoped to work again. Looking up their QRZ pages, I found they were now Silent Keys. More recently, with the passing of Markus (DL6YYM) from BaMaTech, that was the final nudge. I wrote this for myself and decided to share it. If you use it and find it helpful, I’d love to hear from you.

# What it does

- Checks QRZ pages for a list of callsigns.
    - Flags Silent Key status if any of these signals appear:
    - The page title or final URL includes /SKYYYY (e.g. /SK2025)
    - The page redirects to a /CALL/SKYYYY alias
    - The header text contains “Silent Key” (singular)
    - The header QSL line reads “QSL: Reported Silent Key”

Extracts and tracks the “Last modified:” date shown on the profile.

Sends push notifications via Pushover when:
- Someone becomes SK (was not SK before), or
- The page’s Last modified date changes

On the configured heartbeat day (default Sunday, Australia/Sydney):
- If no changes were detected, it sends a small “no changes” summary so you know the job ran.

### Designed for Linux / Raspberry Pi

This is intentionally simple and designed to run on any Linux box—especially a Raspberry Pi doing shack utility jobs.

You only need three files(the rest will be created if they don't exist):
1. qrz_sk_monitor.py — the Python script (main program)
2. qrz_callsigns_list.cfg — callsigns to check (one per line)
3. api_key.cfg — JSON with your Pushover keys (and optional QRZ cookie)

Keep the names above for simplicity.

###  Why Pushover?

I use pushover.net
- for notifications; it’s a great way to ping your phone reliably—way better than SMS or email, and it’s a one-time, very low cost for lifetime use.
- check this good video out : https://www.youtube.com/watch?v=z_e39lmd5b4

### Requirements

Python 3.9+ recommended (for zoneinfo)

### Packages:
pip install requests beautifulsoup4 python-dateutil

### Setup
1) Location & permissions

Put the files somewhere on your machine (e.g., /home/bitnami/SK_WATCH/):

/home/bitnami/SK_WATCH/
  qrz_sk_monitor.py
  qrz_callsigns_list.cfg
  api_key.cfg


Make the script executable:

chmod +x /home/bitnami/SK_WATCH/qrz_sk_monitor.py

2) Callsign list

qrz_callsigns_list.cfg — one callsign per line.
Comments (#) and blank lines are OK; junk after the first token is ignored.

#Callsigns to watch (example)
VK1HF
K4SWL
VK2QA
VK5QD
VK1TX
DL6YYM

These are fine too:
VK2QA   # comment on the same line
VK5QD,extra,stuff
VK1TX ; notes here


Please keep the list ≤ 30 callsigns per run (see “Courtesy to QRZ.com”).

3) Pushover keys (and optional QRZ cookie)

api_key.cfg — JSON file:

{
  "pushover": {
    "token": "YOUR_PUSHOVER_APP_TOKEN",
    "user": "YOUR_PUSHOVER_USER_KEY",
    "device": null,
    "priority": 0
  },
  "qrz_session_cookie": "qrz=YOUR_QRZ_COOKIE; other=optional"
}


token = your Pushover Application/API Token
user = your Pushover User Key

qrz_session_cookie is optional, but can help QRZ show full header/QSL info. (not needed!)

Lock it down:
chmod 600 /home/bitnami/SK_WATCH/api_key.cfg

(You can override with env vars PUSHOVER_TOKEN, PUSHOVER_USER, and QRZ_SESSION if you prefer.)

### Running it

Run once to test:
/home/bitnami/SK_WATCH/qrz_sk_monitor.py

A log file (e.g. qrz_silentkey.log) and a state file (qrz_silentkey_state.json) will appear in the same folder.

Cron (weekly recommended)
To be kind to QRZ’s servers, please run it only once per week and limit to ~30 callsigns.

Example (Sundays 7:30 pm Australia/Sydney):

CRON_TZ=Australia/Sydney
30 19 * * 0 /home/bitnami/SK_WATCH/qrz_sk_monitor.py >> /home/bitnami/SK_WATCH/cron.out 2>&1


(If you worry about overlapping runs, wrap the command with flock -n /tmp/qrz_sk.lock.)

## Courtesy to QRZ.com

Be fair: weekly is plenty for this type of monitoring.

Keep the list ≤ 30 callsigns per run.

The script has a “heartbeat” summary on your chosen day (default Sunday) only when there are no changes, so you still know it’s alive without hammering QRZ.

### Basic Linux skills required

This is a tiny utility aimed at hams comfortable with Linux basics (editing config files, cron, permissions, etc.). I wrote it for my own use and decided to share it—it isn’t polished for every environment. If you want to use it and get stuck, email me and I’ll see if I can help: ian.vk1hf@gmail.com


### Troubleshooting tips

No SK detected but you think there is:
Some details require being logged in to QRZ. Add your qrz_session_cookie to api_key.cfg.

### Too many callsigns:
Trim your list to ~30 per run.

### No notifications:
Double-check your Pushover token/user, and try a quick curl test from the server.

### License

Shared “as is”, no warranties. Use kindly and responsibly. If it helps you, drop me a note—I’d love to hear about it.
