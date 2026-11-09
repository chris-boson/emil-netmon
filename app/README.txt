EMIL NetMon - see what's wrong with the building's internet connection
======================================================================

EMIL NetMon checks your connection every 10 seconds (Wi-Fi signal, your router, the
building's line out, the internet, DNS, web) and shows a live dashboard in your
browser: when problems happen and which step of the chain fails.

Everything stays on your Mac unless you choose to share anonymous hourly
summaries with the building view.

INSTALL (Mac only, macOS 12 or newer)
1. Unzip, then move EMIL NetMon.app to your Applications folder.
2. Double-click EMIL NetMon.app. macOS will say it "cannot verify" the app, because
   it isn't from the App Store. Click Done (not Move to Trash).
3. Open System Settings -> Privacy & Security, scroll down, and click
   "Open Anyway" next to EMIL NetMon. Confirm with your password or Touch ID.
4. If asked to install "Command Line Tools", click Install, wait until it's
   done, then open EMIL NetMon again.
5. Your dashboard opens at http://127.0.0.1:8787. EMIL NetMon keeps running in the
   background, also after restarts. macOS may notify you that a background item
   ("python3") was added. That's EMIL NetMon, so leave it switched on.

SHARING WITH THE BUILDING (optional)
Click "Share anonymous stats" in the dashboard. That's all. Your dashboard then
also shows the Building view: every sharing apartment side by side.
Only per-hour counts and timings are shared: failed/slow checks, router delay,
Wi-Fi signal strength, how crowded your channel is. No websites, network
names, addresses or personal data. Click "Stop sharing" any time.

STOP / UNINSTALL
Click "Stop monitoring and uninstall" at the bottom of the dashboard.
Your logs stay in the hidden folder ~/.netmon. Delete it to remove everything.
