"""Strip credentials from text before it leaves its own agent.

Transcripts hold whatever was pasted into a chat, keys included; nothing kopir
copies into another agent's context, its cache or the memory may carry one.
"""
import re

# Shapes of common credentials.
KNOWN = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
    r"|\b\d{8,10}:[A-Za-z0-9_-]{35}\b"            # Telegram bot token
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")  # JWT
# NAME=value / "name": "value" where the name says it is secret.
ASSIGNED = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIALS?|AUTH)[A-Z0-9_]*)"
    r"(\"?\s*[:=]\s*\"?)([^\s\"',;]{6,})")
# A long run that mixes upper case, lower case and digits is a key, not a word
# (hex hashes and commit ids are lower case only and survive).
OPAQUE = re.compile(r"\b(?=[A-Za-z0-9+/_-]*[A-Z])(?=[A-Za-z0-9+/_-]*[a-z])"
                    r"(?=[A-Za-z0-9+/_-]*\d)[A-Za-z0-9+/_-]{32,}={0,2}")

# scheme://user:password@host
URL_AUTH = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]+)(@)")

MASK = "[redacted]"


def redact(text: str) -> str:
    text = KNOWN.sub(MASK, text)
    text = URL_AUTH.sub(lambda m: m.group(1) + MASK + m.group(3), text)
    text = ASSIGNED.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    return OPAQUE.sub(MASK, text)


def has_secret(text: str) -> bool:
    return redact(text) != text
