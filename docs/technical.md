# How EMIL NetMon works

EMIL NetMon checks every link of the chain every 10 seconds (Wi-Fi radio → router → next hop /
ISP edge → internet → DNS → web) and shows a live dashboard at `http://127.0.0.1:8787`:

- **Where it breaks:** each outage is blamed on the lowest link that failed while everything above it failed too.
- **When it happens:** failed or slow checks by day and hour, plus evenings compared with the rest of the day.
- **Wi-Fi context:** signal, tx rate, channel, roams, how many nearby networks share your channel,
  and how many devices share your local network.
- **Security tunnels** (Netskope, Zscaler, …) are detected, so their hiccups aren't blamed on your ISP.
- **Building view:** hourly summaries from every apartment that shares, side by side, with what failed and the moments when many apartments failed at once.
- **Sleep is not an outage:** with the lid closed, macOS wakes briefly every 15–30 minutes. The check interrupted by sleep and the first 45 s after each wake are ignored.

macOS 12+ only. A single Python file ([`netmon.py`](../netmon.py)) using only the standard library,
plus a tiny Swift helper for Wi-Fi stats. Data lives in `~/.netmon`; a launchd agent
(`com.netmon.agent`) keeps it running.

## Run from source

```bash
python3 netmon.py install      # runs in the background at login, dashboard on http://127.0.0.1:8787
python3 netmon.py report --open
python3 netmon.py uninstall    # keeps logs in ~/.netmon
```

## Building view

Made for one building (about 100 apartments). Each app that shares sends one summary
per hour, with the apartment number, to the building's hub, and reads every apartment's summaries back for the Building view.
**Download the building data (CSV)** in the Building view gives a file to attach for building IT.

The hub is a Google Sheet with a small Apps Script web app, deployed once by whoever publishes
the app ([`hub/README.md`](../hub/README.md)). Its URL is `DEFAULT_HUB` in `netmon.py`.

## Build the app

```bash
./app/build_app.sh                              # → dist/EMIL-NetMon.zip, uses DEFAULT_HUB
./app/build_app.sh --hub "<web app URL>"        # same, but with another hub
```

The app bundles `netmon.py`, a universal (Apple Silicon + Intel) Wi-Fi helper and a launcher. It is
ad-hoc signed, so the first launch needs **Open Anyway**. The launcher uses the system Python from
Apple's Command Line Tools (or Homebrew's) and installs or updates the background agent.

## Releases and updates

Running apps read `latest.json` from the newest GitHub release once a day and show **Update now**
when it lists a higher version. The click downloads `EMIL-NetMon.zip`, checks its SHA-256 against
`latest.json`, replaces the app the launcher last ran from (`~/.netmon/app_path`) and restarts the
background agent. Nothing updates without that click. Neither the launcher nor `install` ever replaces
a newer installed version with an older one.

To publish a release, bump `VERSION` in `netmon.py`, then:

```bash
./app/build_app.sh --notes "What's new, in one sentence"
gh release create vX.Y dist/EMIL-NetMon.zip dist/latest.json --title "EMIL NetMon X.Y" --notes "..."
```

## What it can and can't tell apart

From one wireless laptop, the access point and the router behind it look like a single hop. To prove
a Wi-Fi problem, compare against an Ethernet connection. Routers answer the "TTL expired" probe from
their control plane, so a slow router reply with fast internet is not by itself a problem; the
dashboard only blames the local network when the internet is slow as well.
