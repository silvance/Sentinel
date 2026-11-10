"""Plain-language formatting of times, durations and sizes for operator screens."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo


def local_dt(epoch: float, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(epoch, tz)


def hm(epoch: float, tz: tzinfo, seconds: bool = False) -> str:
    return local_dt(epoch, tz).strftime("%H:%M:%S" if seconds else "%H:%M")


def hm_utc(epoch: float, seconds: bool = False) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%H:%M:%S" if seconds else "%H:%M") + " UTC"


def day_prefix(epoch: float, tz: tzinfo, now: float) -> str:
    """'' for today, 'Yesterday ' for yesterday, else e.g. '3 Nov ' (all in the display timezone)."""
    d = local_dt(epoch, tz).date()
    today = local_dt(now, tz).date()
    if d == today:
        return ""
    if (today - d).days == 1:
        return "Yesterday "
    return f"{d.day} {d.strftime('%b')} "


def when(epoch: float, tz: tzinfo, now: float, seconds: bool = False) -> str:
    """Local time with a day prefix when not today, e.g. 'Yesterday 22:10'."""
    return f"{day_prefix(epoch, tz, now)}{hm(epoch, tz, seconds)}"


def duration(seconds: float) -> str:
    """Compact duration: '45 s', '4 min', '1 h 05 min', '2 days 3 h'."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} s"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h} h {m:02d} min"
    d, h = divmod(h, 24)
    return f"{d} days {h} h" if h else f"{d} days"


def approx_remaining(seconds: float) -> str:
    """Rounded, honest-sounding estimate: 'about 6 days', 'about 14 hours', 'less than a minute'."""
    s = max(0.0, float(seconds))
    if s < 60:
        return "less than a minute"
    if s < 3600:
        n = int(round(s / 60))
        return f"about {n} minute{'s' if n != 1 else ''}"
    if s < 48 * 3600:
        n = int(round(s / 3600))
        return f"about {n} hour{'s' if n != 1 else ''}"
    n = int(round(s / 86400))
    return f"about {n} days"


def size(nbytes: float) -> str:
    """Decimal units, as drive makers and Windows Explorer's 'GB' labels are read by most people."""
    n = float(nbytes)
    if n >= 1e12:
        return f"{n / 1e12:.1f} TB"
    if n >= 1e9:
        return f"{n / 1e9:.0f} GB" if n >= 100e9 else f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e3:.0f} kB"


def utc_offset_text(tz: tzinfo, now: float) -> str:
    off = local_dt(now, tz).utcoffset()
    if off is None:
        return "UTC"
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "−"
    h, m = divmod(abs(total) // 60, 60)
    return f"UTC{sign}{h:02d}:{m:02d}"
