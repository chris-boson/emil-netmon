# netmon building hub (Google Sheet)

Collects anonymous hourly summaries from neighbours' netmon apps into one Google Sheet,
and serves them back so every netmon dashboard can show a **Building view**.

## Set up (about 5 minutes, once)

1. Use a **personal** Google account (work accounts often block public web apps).
2. Create a new Google Sheet, e.g. "Building network log".
3. **Extensions → Apps Script**. Delete the sample code and paste all of [`Code.gs`](Code.gs).
4. Change `BUILDING_CODE` at the top to something hard to guess, e.g. `maple-street-7f3k`. Save.
5. **Deploy → New deployment → ⚙ Select type: Web app**
   - Execute as: **Me**
   - Who has access: **Anyone**
   - Click **Deploy**, then **Authorize access**. Google warns "Google hasn't verified this app"
     because it is your own private script: **Advanced → Go to (project name)** → Allow.
6. Copy the **Web app URL** (ends in `/exec`).
7. Build the app with the hub pre-filled so neighbours only click "Share":
   ```bash
   ./app/build_app.sh --hub "https://script.google.com/macros/s/…/exec" --building "maple-street-7f3k"
   ```
   Or enter the URL and code in an existing netmon's dashboard under **Sharing and settings**.

Check it works: open `<web app URL>?action=building&b=<your code>` in a browser. You should see `{"ok":true,"rows":[]}`.

## Permissions and privacy

| Who | Can do what |
|---|---|
| You (sheet owner) | See and edit all raw rows in the sheet. Share it read-only with building IT as evidence. |
| Anyone with URL **and** building code | Send summaries and read the building view. Without the code every request is rejected. |
| Anyone with only the URL | Nothing (gets "wrong building code"). |
| Neighbours | Sharing is off until they click **Share** in their own dashboard; they can turn it off at any time. |

What is sent, per complete hour: number of checks, failed and slow checks, router / next-hop /
internet latency percentiles, router loss, median Wi-Fi signal, number of nearby networks on the
same channel block, number of devices visible on the LAN, the local gateway IP (e.g. 192.168.1.1),
a random device id and an optional label the neighbour types (e.g. "3B").

Never sent: raw checks, websites, Wi-Fi network names, MAC addresses, public IP addresses,
names or email addresses.

To rotate the code: change `BUILDING_CODE`, then **Deploy → Manage deployments → Edit → Version: New version → Deploy**
(the URL stays the same) and tell neighbours the new code. To remove someone's data, delete their rows
(column `device`) in the `samples` sheet.

Limits: Apps Script handles this easily for a building (one request per Mac every 30 minutes,
rows older than 90 days are pruned).
