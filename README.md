# APRS to Swarm arrival helper

This service watches numeric `-1` through `-15` SSIDs for the base callsign
kept in its private configuration. It waits for two position reports at least
five minutes apart and within 100 metres, then uses Foursquare Places to
suggest nearby venues. It sends a Pushover notification with a Swarm deep
link; it does not check in automatically.

## Protected configuration

The service reads `~/.config/aprs-swarm-checkin.env`. It must have mode `600`
and contain:

```text
FSQ_BEARER_TOKEN=...
PUSHOVER_APP_TOKEN=...
PUSHOVER_USER_KEY=...
PUSHOVER_DEVICES=your-iphone,your-desktop
APRS_BASE_CALLSIGN=K1ABC
```

Do not save this file in the project and do not commit it.

## Local checks

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python watcher.py --simulate fixtures/stationary_stop.jsonl
```

The simulation never calls Foursquare or Pushover. It prints a demo Swarm URL.

To send an explicit direct-link smoke test after installation:

```bash
.venv/bin/python watcher.py --test-pushover YOUR_FOURSQUARE_PLACE_ID
```

Verify the notification on the iPhone and Mac. Tap the supplementary link on
the iPhone to confirm Swarm accepts that venue ID before enabling the service.

## Service management

```bash
systemctl --user status aprs-swarm-checkin.service
journalctl --user -u aprs-swarm-checkin.service -f
```

The process writes structured JSON events to the user journal and never logs
credentials.
