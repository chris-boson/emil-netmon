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
- **Building view (optional):** neighbours click **Share anonymous stats**, and every dashboard
  shows whether all apartments see the same pattern.

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

Made for one building (about 100 apartments). Each neighbour installs the app and clicks
**Share anonymous stats**. That's all. The app sends one anonymous summary per hour to the
building's hub, and every dashboard shows the Building view with all apartments side by side.
**Download the building data (CSV)** under the Building view gives a file to attach for building IT.

The hub is a Google Sheet with a small Apps Script web app, deployed once by whoever publishes
the app ([`hub/README.md`](hub/README.md)). Its URL goes into `DEFAULT_HUB` in `netmon.py`.

## Build the app

```bash
./app/build_app.sh                              # → dist/EMIL-NetMon.zip, uses DEFAULT_HUB
./app/build_app.sh --hub "<web app URL>"        # same, but with another hub
```

## What it can and can't tell apart

From one wireless laptop, the access point and the router behind it look like a single hop. To prove
a Wi-Fi problem, compare against an Ethernet connection. Routers answer the "TTL expired" probe from
their control plane, so a slow router reply with fast internet is not by itself a problem; the
dashboard only blames the local network when the internet is slow as well.
