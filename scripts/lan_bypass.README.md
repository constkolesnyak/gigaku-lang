# LAN client-isolation bypass (why the scroll wheel needs this)

If the scroll-wheel volume suddenly stops working and the daemon log
(`/tmp/gigaku-sound.log`) is full of:

```
Reconnecting...
  Apple TV unreachable: ...
```

…the cause is almost certainly **Wi-Fi client isolation**, not the Apple TV and
not gigaku. This is the runbook.

## Symptom → diagnosis in 30 seconds

The Mac can reach the **gateway** but no other LAN device. Run from the Mac:

```bash
# ping works to peers but TCP does not == client isolation
ping -c1 192.168.0.187            # Apple TV — succeeds
python3 - <<'PY'
import socket
for ip,port in [('192.168.0.1',80),('192.168.0.187',49153)]:
    s=socket.socket(); s.settimeout(2)
    try: s.connect((ip,port)); print(ip,'OPEN')
    except OSError as e: print(ip, e.strerror)   # gateway OPEN, Apple TV "No route to host"
    finally: s.close()
PY
```

If the gateway is `OPEN` but the Apple TV is `No route to host` (instant, 0 ms)
while `ping` to it works → **client isolation, confirmed.**

> **Second failure mode (seen 2026-07-03): mDNS itself dies.** The bypass depends
> on knowing the Apple TV's IP, and the AP's isolation *also* wedges multicast, so
> `Living-Room-….local` can stop resolving for hours. Tell-tale: the lan_bypass
> log spams `can't resolve …local; skip` and `/tmp/gigaku-atv-ip` is missing,
> while `arp -a` still lists the Apple TV. The daemon no longer relies on mDNS —
> it now finds the IP by the device's **hardware MAC** in the ARP table
> (`config.APPLETV_MAC`, the hex baked into the hostname). Confirm resolution
> with: `arp -an | grep -i 02:ad:be`. If that finds it, the wheel will recover on
> the daemon's next 30 s tick with no intervention.

> ⚠️ Test reachability from a normal Terminal — a sandboxed shell can fail these
> connects even when the real machine is fine — or read the **daemon's** log.
> And never toggle Wi-Fi to "fix" it.

## Root cause

The ISP's router runs **per-SSID client isolation**: it forwards
`Mac ↔ gateway` and `Mac ↔ internet` but drops `Mac ↔ peer` unicast. ICMP echo is
allowed (so `ping` lies), but TCP/UDP to peers returns `EHOSTUNREACH`. It is
selective by SSID — the Mac's SSID isolates; other devices on a different SSID
(same radio, different BSSID) are unaffected. It can switch on by itself after a
router auto-config/reboot. **No Mac software can make the AP bridge those frames.**

## The fix (this directory)

Force the Mac to send Apple TV traffic to the **gateway's MAC** instead of the
Apple TV's. The router then **L3-hairpins** it on to the Apple TV — a path the AP
*does* allow. We do that with a static ARP entry, not a route (a route gets
shadowed by the auto-cloned on-link `/32`).

| File | Role |
|------|------|
| `lan_bypass.sh` | Resolves the Apple TV IP (mDNS → **ARP-by-MAC** → cache), pins its ARP entry to the gateway MAC, publishes the IP to `/tmp/gigaku-atv-ip`, and self-heals a poisoned negative route. |
| `com.gigaku.lanbypass.plist` | Root LaunchDaemon: runs the script at boot + every 30 s. |
| `lib/appletv/control.py` | `connect()` resolves the IP (mDNS → ARP-by-MAC → published cache) and does a **unicast host-scan** of it — pyatv's multicast device scan is unreliable under isolation. Connects **Companion-only** so `audio.main_instance.api` exists (RAOP must not win). |

Two non-obvious gotchas baked into the design:

- **Negative-route poison.** A correct ARP pin is not enough: once a connect
  fails, the kernel caches `EHOSTUNREACH` and *re-pinning the same value via
  `arp -d` + `arp -s` is what flushes it.* The daemon re-pins (not skips) when the
  Companion port is unreachable. This is why "the pin looks right but it still
  fails" — flush it.
- **mDNS is unreliable — never the sole way to find the IP.** From Python
  `getaddrinfo` returns `EAI_NONAME` on a cold cache; worse, the AP's isolation can
  kill mDNS entirely so even `dscacheutil` returns nothing for hours (2026-07-03).
  So resolution is layered: try mDNS, then grep the ARP table for the device's
  fixed hardware MAC (`config.APPLETV_MAC`), then the last published IP. The MAC is
  the hex in the hostname and never changes, so ARP-by-MAC is DHCP-proof too, and
  it self-corrects if the Apple TV's DHCP lease moves it to a new IP.

## Install

```bash
sudo cp scripts/com.gigaku.lanbypass.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.gigaku.lanbypass.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.gigaku.lanbypass.plist
```

## Verify

```bash
arp -n 192.168.0.187          # → "at 0:66:77:88:99:aa ... permanent"  (the GATEWAY's MAC)
cat /tmp/gigaku-atv-ip        # → 192.168.0.187
cat /tmp/gigaku-lanbypass.log # → "pinned ..." / "flushed ..." (empty is fine: already pinned)
uv run python -m lib.appletv.control   # self-test: one volume step, the TV's bar moves
```

## If it breaks again

1. **Apple TV re-paired / replaced** → update `APPLETV_HOSTNAME` **and
   `APPLETV_MAC`** in `lib/config.py`, and `ATV_HOST`/`ATV_MAC` in `lan_bypass.sh`.
   Find them with: `dns-sd -L "<name>" _companion-link._tcp` (host) and
   `arp -an` after a `ping` (MAC — it's also the hex in the hostname).
1b. **mDNS died but the device is up** (`can't resolve …local; skip` in the log,
   no `/tmp/gigaku-atv-ip`) → nothing to do; ARP-by-MAC recovers it within 30 s.
   Confirm the device is in the table: `arp -an | grep -i 02:ad:be`.
2. **Gateway changed** → the daemon re-derives it from the default route; nothing
   to do.
3. **Stuck despite a correct-looking pin** → it's the negative cache:
   `sudo arp -d 192.168.0.187 && sudo arp -s 192.168.0.187 <gateway-MAC>` (the
   daemon does this within 30 s on its own).
4. **Real fix on the router** (if you ever want to drop all this): disable
   "client/AP isolation" for the Mac's SSID. Out of scope here — we don't touch
   the router.

Discovered & built 2026-06-25/26. The dead-ends we ruled out: it's not the MAC
(private vs real address made no difference), not a VPN/pf/Little Snitch on the
Mac, not the Apple TV. A Raspberry-Pi relay was prototyped and scrapped.
