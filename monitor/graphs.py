"""Graph rendering to PNG. Deterministic, headless, one panel per datapoint."""
from __future__ import annotations

import time

import numpy as np
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

from . import config, realtime, store, weather  # noqa: E402

INK = "#e8e8ea"
MUTED = "#9aa0a6"
GRID = "#2a2d34"
BG = "#14161a"
ACCENT = "#5cc8ff"
WARN = "#f5a524"
ALERT = "#f0533f"
OUTSIDE = "#8b93a7"          # the world outside the window
AQI_BANDS = ((20, "#4caf50"), (40, "#cddc39"), (60, "#ffc107"),
             (80, "#ff7043"), (100, "#e53935"), (10_000, "#8e24aa"))

# WMO weather codes, as glyphs the default font actually has
SYMBOLS = ((0, "☀"), (2, "☀"), (3, "☁"), (48, "≡"), (57, "☂"), (67, "☂"),
           (77, "❄"), (82, "☂"), (99, "⚡"))


def _symbol(code: float | None) -> str:
    if code is None:
        return ""
    for top, glyph in SYMBOLS:
        if code <= top:
            return glyph
    return ""


def _aqi_colour(aqi: float | None) -> str:
    for top, colour in AQI_BANDS:
        if aqi is not None and aqi <= top:
            return colour
    return OUTSIDE

RULE_KEYS = (("warn", WARN), ("warn_high", WARN), ("warn_low", WARN),
             ("alert", ALERT), ("alert_high", ALERT), ("alert_low", ALERT))


def _bucket_for(span: int) -> int:
    if span <= 3600:
        return 30
    if span <= 6 * 3600:
        return 120
    if span <= 86400:
        return 300
    if span <= 7 * 86400:
        return 1800
    return 7200


def _paint(ax, band_lo: float, band_hi: float, thresholds: dict, xform) -> None:
    """Draw every threshold; ones outside the band are pinned to the edge with an arrow.

    Several off-band thresholds share an edge, so each is offset further from it — otherwise
    they print on top of each other and only the last one is readable.
    """
    edge_n = {"hi": 0, "lo": 0}
    for key, colour in RULE_KEYS:
        if key not in thresholds:
            continue
        raw = thresholds[key]
        y = xform(raw)
        inside = band_lo <= y <= band_hi
        ax.axhline(y, color=colour, lw=1.1, ls="--", alpha=0.65 if inside else 0.35)
        label = f"{key.replace('_', ' ').upper()} {raw:g}"
        if inside:
            ax.annotate(label, xy=(0.004, y), xycoords=("axes fraction", "data"),
                        xytext=(0, 3), textcoords="offset points", ha="left", va="bottom",
                        fontsize=8, color=colour, annotation_clip=True,
                        bbox={"boxstyle": "round,pad=0.15", "fc": BG, "ec": "none", "alpha": 0.75})
        else:
            high = y > band_hi
            side = "hi" if high else "lo"
            step = -11 * edge_n[side]
            edge_n[side] += 1
            ax.annotate(("↑ " if high else "↓ ") + label,
                        xy=(0.996, 1.0 if high else 0.0), xycoords="axes fraction",
                        xytext=(0, (-2 if high else 2) + step), textcoords="offset points",
                        ha="right", va="top" if high else "bottom",
                        fontsize=7.5, color=colour, alpha=0.85,
                        bbox={"boxstyle": "round,pad=0.15", "fc": BG, "ec": "none", "alpha": 0.7})


def render(dev: dict, start: int, end: int, dps: list[str] | None = None,
           out_dir: Path | None = None, title_suffix: str = "",
           settings: config.Settings | None = None) -> Path:
    settings = settings or config.load()
    meta_all = dev.get("dp_meta") or {}
    category = dev.get("category", "")
    if dps is None:
        dps = [dp for dp, m in meta_all.items() if m.get("kind") not in (None, "enum")]
    bucket = _bucket_for(end - start)

    panels = []
    for dp in dps:
        rows = store.series(dev["dev_id"], start, end, dp=dp, bucket=bucket)
        rows = [r for r in rows if r.get("avg") is not None]
        if rows:
            panels.append((dp, rows))
    if not panels:
        raise ValueError("no numeric samples in range")

    # the outside world, for the panels it explains: temperature and humidity only
    wants_out = any(realtime.meta_for(category, dp).get("kind") in ("temp", "humidity")
                    for dp, _ in panels)
    outside = weather.history(settings, start, end) if wants_out else None
    if outside and not outside["ts"]:
        outside = None

    plt.rcParams.update({
        "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG,
        "text.color": INK, "axes.labelcolor": INK, "xtick.color": MUTED,
        "ytick.color": MUTED, "axes.edgecolor": GRID, "font.size": 9,
    })
    rows_n = len(panels) + (1 if outside else 0)
    heights = [1.0] * len(panels) + ([0.18] if outside else [])
    fig = plt.figure(figsize=(11, 2.5 * len(panels) + (0.8 if outside else 0) + 1.3))
    gs = fig.add_gridspec(rows_n, 1, height_ratios=heights, hspace=0.16)
    axes = np.array([[fig.add_subplot(gs[i, 0])] for i in range(len(panels))])
    for ax in axes[:, 0][:-1]:
        ax.sharex(axes[-1, 0])
    strip = fig.add_subplot(gs[-1, 0], sharex=axes[0, 0]) if outside else None
    for ax, (dp, rows) in zip(axes[:, 0], panels):
        meta = realtime.meta_for(category, dp)
        kind = meta.get("kind", "")
        # temperature is stored in °C; the display unit is a render concern only
        if kind == "temp" and settings.temp_unit == "f":
            xform = config.c_to_f
            unit = "°F"
        else:
            xform = lambda v: v  # noqa: E731
            unit = "°C" if kind == "temp" else meta.get("unit", "")

        ts = [datetime.fromtimestamp(r["t"]) for r in rows]
        lo = [xform(r["lo"]) for r in rows]
        hi = [xform(r["hi"]) for r in rows]
        avg = [xform(r["avg"]) for r in rows]
        ax.fill_between(ts, lo, hi, color=ACCENT, alpha=0.16, lw=0)
        ax.plot(ts, avg, color=ACCENT, lw=1.6)

        lo_v, hi_v = min(lo), max(hi)
        outside_line = None
        if outside:
            key = "temperature_2m" if kind == "temp" else "relative_humidity_2m" if kind == "humidity" else None
            if key:
                pts = [(datetime.fromtimestamp(t), xform(v)) for t, v in zip(outside["ts"], outside[key])
                       if v is not None]
                if pts:
                    xs, ys = zip(*pts)
                    outside_line, = ax.plot(xs, ys, color=OUTSIDE, lw=1.2, ls=(0, (4, 3)),
                                            alpha=0.9, label="outside")
                    lo_v, hi_v = min(lo_v, min(ys)), max(hi_v, max(ys))
        th = config.THRESHOLDS.get(kind) or {}
        # floor the window, not just the padding: a 3-point axis turns integer jitter into
        # fake drama, so a minimum span centred on the data is enforced per kind
        center = (hi_v + lo_v) / 2
        half = max(hi_v - lo_v, th.get("min_span", 0.0), abs(hi_v) * 0.02, 0.5) / 2
        pad = half * 0.1
        band_lo, band_hi = center - half - pad, center + half + pad
        span_v = band_hi - band_lo
        ax.set_ylim(band_lo, band_hi)
        if len(rows) > 1:
            _paint(ax, band_lo, band_hi, settings.thresholds.get(kind) or {}, xform)

        title = meta.get("name", dp)
        if kind == "temp":
            title += " (°C / °F)" if settings.temp_unit == "both" else f" ({unit})"
        elif unit:
            title += f" ({unit})"
        ax.set_title(title, loc="left", fontsize=10, color=INK, pad=8)
        ax.set_ylabel(unit or "value", fontsize=9, color=MUTED)
        if outside_line is not None:
            outside_handle = outside_line
        if strip is not None:
            ax.tick_params(axis="x", labelbottom=False)
        ax.grid(True, color=GRID, lw=0.6, alpha=0.7)
        ax.yaxis.set_major_locator(MaxNLocator(4))
        ax.tick_params(labelsize=8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)

        if kind == "temp" and settings.temp_unit == "both":
            sec = ax.secondary_yaxis("right", functions=(config.c_to_f, config.f_to_c))
            sec.set_ylabel("°F", fontsize=9, color=MUTED)
            sec.tick_params(labelsize=8, colors=MUTED)
            sec.spines["right"].set_visible(True)
            sec.spines["right"].set_color(GRID)

    # the outside strip: what the weather was doing, and how the air read
    if strip is not None and outside:
        step = max(1, len(outside["ts"]) // 48)
        for t_, code in list(zip(outside["ts"], outside["weather_code"]))[::step]:
            glyph = _symbol(code)
            if glyph:
                strip.text(datetime.fromtimestamp(t_), 0.66, glyph, ha="center", va="center",
                           fontsize=9, color=INK, alpha=0.85)
        aq = [(datetime.fromtimestamp(t_), v) for t_, v in zip(outside["ts"], outside["aqi"])
              if v is not None]
        if aq:
            strip.scatter([a for a, _ in aq], [0.24] * len(aq), s=26,
                          c=[_aqi_colour(v) for _, v in aq], alpha=1.0, linewidths=0)
        key = [Line2D([], [], color=OUTSIDE, lw=1.2, ls=(0, (4, 3)), label="outside")]
        key += [Line2D([], [], marker="o", ls="", ms=4.5, color=c, label=lab)
                for lab, c in (("20", AQI_BANDS[0][1]), ("40", AQI_BANDS[1][1]),
                               ("60", AQI_BANDS[2][1]), ("80+", AQI_BANDS[4][1]))]
        strip.legend(handles=key, loc="upper right", fontsize=7, frameon=False,
                     ncol=5, handletextpad=0.3, columnspacing=0.9, borderpad=0.2)
        strip.set_ylim(0, 1)
        strip.set_xmargin(0.01)
        strip.set_yticks([])
        strip.grid(False)
        strip.set_ylabel("outside / AQI", fontsize=8, color=MUTED)
        for side in ("top", "right", "left"):
            strip.spines[side].set_visible(False)
        strip.tick_params(axis="x", labelsize=8)

    ax = strip if strip is not None else axes[-1, 0]
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(
        mdates.DateFormatter("%H:%M:%S" if end - start <= 7200
                             else ("%d %H:%M" if end - start > 86400 else "%H:%M")))
    ax.xaxis.get_offset_text().set_visible(False)
    ax.tick_params(axis="x", labelsize=8)

    start_s = time.strftime("%Y-%m-%d %H:%M", time.localtime(start))
    end_s = time.strftime("%Y-%m-%d %H:%M", time.localtime(end))
    first = min(p[1][0]["t"] for p in panels)
    lag = " · first sample " + time.strftime("%H:%M:%S", time.localtime(first)) if first - start > 300 else ""
    tz = time.strftime("%Z", time.localtime(start))
    fig.suptitle(f"{dev['name']} — window {start_s} → {end_s} {tz}{lag}"
                 f"{(' · ' + title_suffix) if title_suffix else ''}",
                 x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.97))

    target_dir = out_dir or config.REPORTS
    target_dir.mkdir(parents=True, exist_ok=True)
    span = end - start
    unit_name = f"{span // 3600}h" if span >= 3600 else f"{max(span // 60, 1)}m"
    name = (f"{dev['dev_id']}_{time.strftime('%Y%m%dT%H%M', time.localtime(start))}_"
            f"{unit_name}{('_' + title_suffix) if title_suffix else ''}.png")
    path = target_dir / name
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return path


def prune_reports(keep_days: int) -> int:
    if keep_days <= 0:
        return 0
    cutoff = time.time() - keep_days * 86400
    n = 0
    for f in config.REPORTS.glob("*.png"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            n += 1
    return n
