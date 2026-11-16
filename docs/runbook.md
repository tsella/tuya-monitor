# Runbook

## Device stopped reporting

`monitor.cli devices` shows `fail=N` and `last_error`. The collector disables a device
after 5 consecutive failures and raises a `device_offline` alert.

1. `journalctl --user -u tuya-monitor-collect -n 50` — read the actual error.
2. Ping the IP; check it still holds the DHCP lease (`ip neigh | grep <mac>`).
3. `monitor.cli verify <dev_id>` — a `914` means the key rotated (re-pair needed), a
   `904` means framing, a timeout means the device is off the network.
4. Re-pair if the key is stale: `monitor.cli pair`, approve, then `verify`.

## Key rotated (device re-paired in the app)

`localKey` changes on every re-pair and nothing can derive it locally. Run the pairing
flow again — the row is updated in place, history is untouched.

## Alert storm

Cooldowns are per rule; a spike in noise usually means a threshold is wrong for the room.
Adjust `THRESHOLDS` in `monitor/config.py`, then `systemctl --user restart
tuya-monitor-collect`. Silence all rules temporarily with `anomaly_enabled = false`.

## Disk pressure

Samples are the only thing that grows. `monitor.cli prune` drops rows past
`retention_days` and PNGs past 30 days. If space is still tight, lower `retention_days`
and prune again; per-device files mean you can also just delete the noisiest one.

## API unreachable

`systemctl --user status tuya-monitor-api`. Collection is independent of the API; a dead
API does not stop data.

## Cloud path broken (pairing fails)

A `SING_VALIDATE_FALED_4` means the app identity in `monitor/config.py` is stale — the
constants come from the app build, so re-extract them per the
`tuya-app-api-local-key` skill. Existing devices keep collecting regardless.

## Rotating the API key

Edit `api_key` in `~/.config/tuya-monitor/config.toml`, `systemctl --user restart
tuya-monitor-api`. Empty string disables auth entirely (localhost only).

## Alert tuning

A monitor that cries wolf gets muted, so three brakes stand in front of every alert. All three
are config values; `config.example.toml` documents the defaults.

- `confirm_samples` (default 3) — the value must violate the rule on that many consecutive
  polls. Polls are 30 s apart, so a stray sample cannot fire, and the streak resets the moment
  the value comes back into bounds.
- `cooldown_min` (default 120) — multiplied per rule: alerts 1×, warnings 2×, baseline hits 3×.
  "Unusual for this hour" is the noisiest rule, so it stays quiet for six hours.
- Baseline band is `max(5 × MAD, floor)` with floors of 150 ppm CO₂, 3 °C, 8 %RH. The floors
  matter more than the MAD term for a well-behaved room: a median-absolute-deviation band on
  indoor temperature shrinks to about 0.5 °C and flags ordinary afternoon warmth.

Symptom to watch for: a rule re-firing on the same value every cooldown. That is a threshold
sitting inside the daily range, not an anomaly — move the edge, do not shorten the cooldown.

## Time and daylight saving

Everything is stored as epoch seconds, so a clock change never rewrites history and no row is ever
duplicated or lost when the zone shifts. Wall clocks are only ever a rendering choice:

- the baseline hour is the *local* hour in `timezone` (default `America/New_York`), not the UTC
  hour. A window that contains a clock change therefore selects two UTC hours for the same local
  hour and pools both, so a baseline does not jump a bucket when the clocks move;
- the spring-forward hour has one day fewer in its window and the autumn hour has two copies. Both
  are handled by the same pool — the 12-point minimum is met from the other days either way;
- charts format in `timezone` and name the zone in the title (`%Z`, so it says EST or EDT rather
  than lying). A week spanning a change prints the repeated hour twice, which is what the clock did;
- outside weather is requested in UTC and additionally corrected by `utc_offset_seconds` from the
  reply, so flipping that request to `timezone=auto` cannot shift every stored hour by four or
  five hours without complaint.

Changing `timezone` re-labels the baselines but does not move the data. A bad zone falls back to
UTC and logs nothing — the rules keep running rather than stopping.

## Weather context

The outside reading is the evidence for an indoor deviation, so every baseline alert carries it and
says which way it went. Warnings are never suppressed for weather — a weather-shaped wobble is
still worth a line — but the verdict changes how hard the rule pushes:

- **Supported** (the room moved the way the weather pushes: cold outside with a cold room, wet
  outside with a humid room) — the band widens 1.9×, so only a larger supported swing fires at all.
- **Unsupported** (an extreme inside while it is ordinary out: 80 °F indoors on a 68 °F day, a damp
  room while it is dry out) — the band stays exactly as it was and the severity bar drops from
  2.5× to 1.6×, because an unexplained extreme is the case worth waking up for.

Messages read `(outside 20°C, 55% RH) — the outside does not account for it`, or `— in line with
the outside`, so the reader sees the judgment instead of having to ask for the weather.

Supported is decided against the hour's own median: temperature needs the outside 5 °C beyond it on
the same side as the deviation; humidity needs 68 % or more for a damp room and 45 % or less for a
dry one. Anything else is unsupported.

`weather_enabled = false` restores the weather-blind rules.

Outside conditions are stored, not just fetched: `weather` in the registry holds one row per hour
(temperature, humidity, precipitation, WMO code, AQI), back-filled 14 days by
`tuya-monitor-weather.timer` every ten minutes. The alert path prefers a stored reading under 20
minutes old, so a brief network failure changes nothing; the chart reads the table and only calls
open-meteo when a window has almost no rows. `monitor.cli weather --days N` back-fills by hand and
`prune` ages the table out with the samples.

The graphs carry the same evidence: outside temperature and humidity are drawn as a dashed series
over the indoor ones, and the strip beneath them marks the hour's weather and its air quality, so a
swing can be judged on the chart instead of guessed at. If the history endpoints fail the chart
still renders, just without the outside.

A failed lookup is not allowed to blind the rules: the last good reading stands in for up to an
hour (flagged `stale`), a failure is retried after two minutes rather than the usual fifteen, and
the failure is logged to the journal. One blip used to silence the context for a quarter of an
hour — and a cold-snap alert went out bare because of it.
