"""Shared Telegram notifier for Tranchi monitoring scripts.

INVARIANT — Telegram rejects any sendMessage whose `text` exceeds 4096 chars with
HTTP 400 Bad Request. The quality_audit digest grew to 5,252 chars, so every nightly
alert silently 400'd (discovered 2026-06-21) — the monitoring fired into the void
while 43.7k Wayne blight leads were being wrongly retired and nobody was paged. ALWAYS
chunk to <=4096 (we use 4000 for prefix margin), splitting on line boundaries. The three
monitoring scripts (quality_audit, audit_scrapers, playwright_source_check) and the
run.py collapse tripwire all route through here so the bug can't be reintroduced per-copy.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("notify")

TELEGRAM_TOKEN_PATH = Path("/home/ubuntu/.secrets/tranchi/telegram-bot-token")
TELEGRAM_CHAT_ID = "8360510944"  # @intelleq_monitor_bot (shared with Gotham)
_MAX = 4000  # safety margin under Telegram's 4096 hard limit (leaves room for the chunk prefix)


def _chunks(message: str) -> list[str]:
    """Split into <=_MAX-char parts on line boundaries; hard-split any single long line."""
    if len(message) <= _MAX:
        return [message]
    out: list[str] = []
    buf = ""
    for line in message.split("\n"):
        while len(line) > _MAX:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:_MAX])
            line = line[_MAX:]
        if not buf:
            buf = line
        elif len(buf) + 1 + len(line) > _MAX:
            out.append(buf)
            buf = line
        else:
            buf = buf + "\n" + line
    if buf:
        out.append(buf)
    return out


def send_telegram(message: str, *, chat_id: str | None = None) -> bool:
    """Send a (chunked) Telegram message. Best-effort: never raises.

    Returns True only if every chunk delivered. Returns False (and logs) when the token
    file is missing or any send fails — callers treat alerting as non-fatal.
    """
    if not TELEGRAM_TOKEN_PATH.exists():
        logger.warning("Telegram token not found at %s — alert logged only.", TELEGRAM_TOKEN_PATH)
        return False
    try:
        import httpx

        token = TELEGRAM_TOKEN_PATH.read_text().strip()
        cid = chat_id or TELEGRAM_CHAT_ID
        parts = _chunks(message)
        with httpx.Client(timeout=10.0) as c:
            for i, part in enumerate(parts):
                prefix = f"({i + 1}/{len(parts)}) " if len(parts) > 1 else ""
                r = c.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": cid, "text": prefix + part},
                )
                r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never crash a job
        logger.error("Telegram send failed (non-fatal): %s", exc)
        return False
