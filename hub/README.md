# EMIL NetMon hub (Google Sheet)

Collects the anonymous hourly summaries from the building's EMIL NetMon apps into one Google
Sheet and serves them back, so every dashboard can show the **Building view**. Neighbours never
touch this: it is deployed once by whoever publishes the app, and its URL is `DEFAULT_HUB` in `netmon.py`.

## Deploy (once, about 3 minutes)

1. Use a **personal** Google account (work accounts often block public web apps).
2. Create a new Google Sheet, e.g. "EMIL NetMon hub".
3. **Extensions → Apps Script**. Delete the sample code, paste all of [`Code.gs`](Code.gs), and save.
4. **Deploy → New deployment → ⚙ Select type: Web app**
   - Execute as: **Me**
   - Who has access: **Anyone**
   - Click **Deploy**, then **Authorize access**. Google warns "Google hasn't verified this app"
     because it is your own script: **Advanced → Go to (project name)** → Allow.
5. Copy the **Web app URL** (ends in `/exec`) into `DEFAULT_HUB` in `netmon.py` and rebuild the app.

Check it works: open `<URL>?action=building` in a browser. You should see `{"ok":true,"cols":[…],"rows":[]}`.

To update the script later: paste the new `Code.gs`, then **Deploy → Manage deployments → Edit →
Version: New version → Deploy**. The URL stays the same.

## Permissions and privacy

| Who | Can do what |
|---|---|
| Hub owner | Sees and edits all rows in the sheet. Can share it read-only with building IT. |
| Anyone with the app (the URL is in it) | Sends summaries and reads the Building view. |
| Neighbours | Nothing is sent until they click **Share anonymous stats**; **Stop sharing** stops it. |

What is sent, per complete hour: number of checks, failed and slow checks, router / next-hop /
internet latency percentiles, router loss, median Wi-Fi signal, number of nearby networks on the
same channel block, number of devices visible on the LAN, the local gateway IP (e.g. 192.168.1.1),
a random device id and an optional label the neighbour types (e.g. "3B").

Never sent: raw checks, websites, Wi-Fi network names, MAC addresses, public IP addresses,
names or email addresses.

## Limits and housekeeping

- Sized for one building of about 100 apartments: one request per Mac every 30 minutes (the hub
  rejects more than one per 5 minutes per device), and each dashboard reads the last 7 days at most
  every 10 minutes.
- Rows older than 60 days are pruned once a day; the sheet is capped at 250,000 rows.
- The URL is public (it ships in the app), so anyone could post made-up rows. The Building view only
  counts the network most apartments report, which filters out most junk. To remove one apartment's
  data, delete its rows (column `device`) in the `samples` sheet.
