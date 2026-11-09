"""Graph rendering to PNG. Deterministic, headless, one panel per datapoint."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

from . import config, realtime, store  # noqa: E402

INK = "#e8e8ea"
MUTED = "#9aa0a6"
GRID = "#2a2d34"
BG = "#14161a"
ACCENT = "#5cc8ff"
WARN = "#f5a524"
ALERT = "#f0533f"

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

    plt.rcParams.update({
        "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG,
        "text.color": INK, "axes.labelcolor": INK, "xtick.color": MUTED,
        "ytick.color": MUTED, "axes.edgecolor": GRID, "font.size": 9,
    })
    fig, axes = plt.subplots(len(panels), 1, figsize=(11, 2.5 * len(panels) + 1.3),
                             sharex=True, squeeze=False)
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

    ax = axes[-1, 0]
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
