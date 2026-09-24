#!/usr/bin/env python3
"""Put Kuma monitors under a timed maintenance window.

    kuma-silence.py <title> <minutes> <monitor-id> [<monitor-id> ...]

Used by scripts/renovate-run.sh before it merges image bumps, so the pages
that a rollout would raise (a 503 while the new image pulls) are held for the
duration of the window. Kuma marks the beats "maintenance" (status 3) and
sends nothing; when the window ends the monitors resume and page for real if
the service is still down.

Runs on the console container: credentials come from KeePass on the jumphost
(`ssh pi-02 kp-get ...`, the same path every Kuma script here uses), never
from a file. Prints the maintenance id. Exit 0 on success; non-zero and a
message on stderr otherwise -- the caller logs and carries on, because an
upgrade that pages is better than an upgrade that does not happen.

Kuma 2.x: the uptime-kuma-api library's socket calls time out now and then
(the server is chatty on login); login is retried, and the window is created
with strategy "single" over an explicit UTC date range, which is the one
shape that has been seen to work.
"""
import datetime as dt
import subprocess
import sys
import time

from uptime_kuma_api import MaintenanceStrategy, UptimeKumaApi


def kp(entry):
    return subprocess.run(["ssh", "-4", "-n", "-o", "BatchMode=yes", "pi-02", f'kp-get "{entry}"'],
                          check=True, capture_output=True, text=True).stdout.strip()


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    title, minutes, ids = sys.argv[1], int(sys.argv[2]), [int(x) for x in sys.argv[3:]]
    cred = kp("Cloudflare Kuma Service Token")
    cid, csec = cred.split(":", 1) if ":" in cred else cred.split(None, 1)
    headers = {"CF-Access-Client-Id": cid.strip(), "CF-Access-Client-Secret": csec.strip()}
    password = kp("Kuma")
    api = None
    for attempt in range(4):
        try:
            api = UptimeKumaApi("https://kuma.pub.example.com", headers=headers, timeout=90)
            api.login("admin", password)
            break
        except Exception as e:  # noqa: BLE001
            print(f"kuma login retry {attempt}: {e!r}", file=sys.stderr)
            time.sleep(8)
    if api is None:
        sys.exit("kuma-silence: cannot log in")
    now = dt.datetime.now(dt.timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"
    r = api.add_maintenance(title=title, description="opened by scripts/renovate-run.sh before an automerge; ends on its own",
                            strategy=MaintenanceStrategy.SINGLE, active=True,
                            dateRange=[now.strftime(fmt), (now + dt.timedelta(minutes=minutes)).strftime(fmt)],
                            timezoneOption="UTC")
    mid = r["maintenanceID"]
    api.add_monitor_maintenance(mid, [{"id": i} for i in ids])
    api.disconnect()
    print(mid)


if __name__ == "__main__":
    main()
