# EMIL NetMon

See what's actually wrong with a flaky internet connection, and collect evidence you can show
whoever runs the network.

EMIL NetMon checks every link of the chain every 10 seconds (Wi-Fi radio → router → next hop /
ISP edge → internet → DNS → web) and shows a live dashboard at `http://127.0.0.1:8787`:

- **Where it breaks:** each outage is blamed on the lowest link that failed while everything above it failed too.
- **When it happens:** failed or slow checks by day and hour, plus evenings compared with the rest of the day.
- **Wi-Fi context:** signal, tx rate, channel, roams, how many nearby networks share your channel,
  and how many devices share your local network.
- **Security tunnels** (Netskope, Zscaler, …) are detected, so their hiccups aren't blamed on your ISP.
- **Building view (optional):** neighbours can share anonymous hourly summaries to one Google Sheet,
  and every dashboard shows whether all apartments see the same pattern.

macOS 12+ only. A single Python file using only the standard library, plus a tiny Swift helper for Wi-Fi stats.

## Install

**App:** download `EMIL-NetMon.zip` from [Releases](../../releases), unzip, and open `EMIL NetMon.app`.
It isn't signed by Apple, so the first launch needs **System Settings → Privacy & Security → Open Anyway**.
See [`app/README.txt`](app/README.txt).

**From source:**
```bash
python3 netmon.py install      # runs in the background at login, dashboard on http://127.0.0.1:8787
python3 netmon.py report --open
python3 netmon.py uninstall    # keeps logs in ~/.netmon
```

## Building view

1. Set up the Google Sheet hub: [`hub/README.md`](hub/README.md) (about 5 minutes, no server needed).
2. Enter the hub URL and building code under **Sharing and settings** in your dashboard.
3. Copy the **invite link** from there and send it to neighbours who have installed the app.
   The building code never has to be in the public download.

## Build the app

```bash
./app/build_app.sh                                       # → dist/EMIL-NetMon.zip
./app/build_app.sh --hub "<web app URL>" --building "<code>"   # private build with the hub pre-filled (don't publish this one)
```

## What it can and can't tell apart

From one wireless laptop, the access point and the router behind it look like a single hop. To prove
a Wi-Fi problem, compare against an Ethernet connection. Routers answer the "TTL expired" probe from
their control plane, so a slow router reply with fast internet is not by itself a problem; the
dashboard only blames the local network when the internet is slow as well.
