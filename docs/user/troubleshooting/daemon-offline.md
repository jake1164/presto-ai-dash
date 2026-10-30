# Daemon offline and Wi-Fi drops

**Daemon offline** is the app's generic label for "the last poll did not
work". It does not mean the daemon is broken. The device cannot tell a
dead daemon from a dead network, so it says the same thing for both.

Work through the list in order.

## 1. Is the daemon actually up?

On the machine that runs the daemon:

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
     -H "Authorization: Bearer your-token-here" \
     http://127.0.0.1:8787/api/v1/status
```

`200` means the daemon is alive and the token is right. `401` means the
token is wrong. No answer at all means the daemon is not running.

## 2. Is the device pointing at the right address?

In `/ai_dash_files/config.py` on the device:

```python
DAEMON_HOST = "192.168.1.50"
DAEMON_PORT = 8787
```

`DAEMON_HOST` must be the LAN address of the machine running the daemon,
not `localhost`. Use an address rather than a name: name lookup can block on
MicroPython and stall the touch loop.

If the computer's address changes - a new DHCP lease, a different network -
the device has to be updated. A fixed address for the daemon, or a DHCP
reservation in the router, avoids that.

To update it, open **Set up a Presto** on the daemon's admin page, prepare a
download with the new address, and run the script on the computer the Presto is
plugged into: see
[One process for every case](../getting-started/set-up-the-presto.md#one-process-for-every-case).
Developers can run `presto-ai-dash set-daemon --port COM3 --daemon-host <address>`,
which also puts the old settings back if the Presto cannot reach the daemon. See
[Moving a Presto to a different daemon](../getting-started/set-up-the-presto.md#moving-a-presto-to-a-different-daemon).

## 3. Can the device reach the port?

From any other machine on the same LAN:

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
     -H "Authorization: Bearer your-token-here" \
     http://192.168.1.50:8787/api/v1/status
```

If that works but the device does not, the difference is the device's
network, so continue below.

## 4. Wi-Fi, from the router's side

This is where most of the real problems are. Open your router's admin page
and look at its **client list**, **connected devices** or **DHCP leases**
panel.

| What you see | What it means |
| --- | --- |
| The Presto is not listed at all | It never joined, or it joined and left |
| It is listed but the signal is very weak | Move it, or move the router |
| It is listed with a recent "disconnect" event | The radio is dropping the association |
| It is listed on the 5 GHz band | The Presto's radio is 2.4 GHz only |
| It has a very short lease | The lease is being renewed constantly, or not at all |

Things to change on the router:

- **Signal strength.** A weak signal is the most common cause. The Presto
  sits where you put it, often behind a monitor.
- **Band.** Make sure the 2.4 GHz network is enabled and has a fixed
  channel. A 5 GHz-only network will never work.
- **Fast roaming.** Turn **fast roaming** or **802.11r/k/v** off on the
  access point. A client that roams between two access points mid-request
  can lose the connection in exactly the way the dashboard sees.
- **Band steering.** Turn it off for the 2.4 GHz network, or give the
  device a hint to stay on 2.4 GHz.
- **Pin the access point.** If you have more than one, pin the Presto to
  one of them. Roaming between access points is a common source of
  short-lived drops.
- **DHCP lease time.** A long lease - a day or more - means fewer renewals
  to go wrong. Make sure the router actually hands out a lease to the
  device's MAC address, and that the address it hands out is the one in
  `DAEMON_HOST`.
- **Bandwidth limits and device policies.** Some routers apply a per-device
  bandwidth cap or a "low power" schedule that suspends the radio.

## 5. Wi-Fi, from the computer's side

While the device is failing, look at the computer that runs the daemon:

```powershell
Get-NetNeighbor -IPAddress 192.168.1.50
Test-Connection 192.168.1.50 -Count 12
netstat -an | Select-String 8787
```

| What you see | What it means |
| --- | --- |
| The neighbour entry is `Unreachable` or `Incomplete` | The device is not answering at the link layer at all |
| Most pings are lost | The radio or the signal |
| A connection from the device stuck in `SYN_RECEIVED` | The device started a connection and never finished it |
| The gateway answers every ping | Your LAN and the router are fine |

`Unreachable` with no MAC address is the strongest signal: the device is not
answering ARP, which is a radio or signal problem, not a daemon problem.
A power cycle of the Presto usually brings it back for a while.

## 6. What the device tells you itself

The app records the last failure, and shows it on the offline screen and in
the USB console log: the exception type, where the fetch stopped
(connection, send, headers, body or parse), the number of failures, the
uptime, the free memory and the Wi-Fi state.

Read it over the serial console. It is the difference between "the network
is gone" and "the daemon answered with something the device could not
parse".

## 7. The daemon's own view

The admin page's **Status** tab lists every client that fetched the status
endpoint: its name, its address, when it last fetched, how often, and the
app revision it reports. A client that has not been seen for three poll
intervals is shown as *not connected*.

If the Presto is listed there and fetching normally, the problem is on the
device's network side, not in the daemon.

### The device's own columns

The same table carries five more columns, filled in from what the device
reported about itself in its `X-Presto-Diag` header:

| Column | What it says |
| --- | --- |
| **Last error** | The type of the last failed fetch and where it stopped, for example `OSError 110 (conn)`. It stays after the device recovers, and it is cleared when the device restarts, so it always belongs to the device's current run |
| **Failures** | How many times the fetch has failed since the device was switched on |
| **Free** | The free heap on the device, in KiB |
| **Signal** | The Wi-Fi signal, in dBm |
| **Uptime** | How long the app has been running |

A dash means the device did not report that value. It is not an error: an
older device, or one that has never polled, sends nothing.

**Read "Last seen" first.** A device that cannot reach the daemon cannot
report a problem either: the request that failed never arrived. So a row with
an old "Last seen" and empty diagnostic columns means the device is not
getting through at all, and the columns above are the wrong place to look.
When the device does get through, the next poll carries the failure that
happened before it, so the columns fill in.

The daemon also writes one line to its log when a client reports a problem
and that problem changes:

```text
client "presto" reported a fetch problem: err=OSError 110 st=conn n=3
```

## 8. Other causes

| Symptom | Cause |
| --- | --- |
| **Unauthorised** | `SHARED_TOKEN` on the device does not match `daemon.shared_token` |
| **No network** | The SSID or the password in `/secrets.py` is wrong, the network is 5 GHz only, or the Wi-Fi changed. Run **Set up a Presto** from the admin page with the Wi-Fi box ticked: see [Changing the Wi-Fi network](../getting-started/set-up-the-presto.md#changing-the-wi-fi-network) |
| The daemon is up but the port is closed | A firewall on the daemon machine. Allow the private LAN |
| The device works, then stops after a while | Look at the router's client list and the DHCP lease |

## If you cannot fix it

Put the daemon behind a reverse proxy with TLS, or move it and the device
onto a private network. That does not fix a broken radio, but it removes the
plain-HTTP exposure while you work on it.

## See also

- [The admin page will not open](admin-page.md)
- [Stale readings](stale-readings.md)
- [Ports and endpoints](../reference/ports-and-endpoints.md)
