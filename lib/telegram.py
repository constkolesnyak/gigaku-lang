"""Telegram, the transport only: credentials and one raw API call.

Shared by `gigaku report` (the weekly post) and `gigaku backup` (failure alerts) — one
bot, one credentials file, one place that knows how the API is spoken to. What to say and
when to stay silent is each caller's judgement, not this module's.
"""
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

from lib.config import UserError, settings


def credentials():
    """`(token, chat)` from the environment, else from TELEGRAM_ENV_FILE.

    Deliberately *not* Settings fields: `gigaku info` prints every setting there is, and a
    bot token is one `gigaku info | pbcopy` away from a chat log. A file outside the repo
    also survives the repo being public.
    """
    token = os.environ.get("GIGAKU_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("GIGAKU_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    path = os.path.expanduser(settings.TELEGRAM_ENV_FILE)
    if (not token or not chat) and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip().strip("\"'")
                if key.strip().endswith("BOT_TOKEN"):
                    token = token or value
                elif key.strip().endswith("CHAT_ID"):
                    chat = chat or value
    if not token or not chat:
        raise UserError(
            f"No Telegram credentials: put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in "
            f"{path} (or the environment)."
        )
    return token, chat


def tg_call(token, method, fields, files=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        body = bytearray()
        for key, value in fields.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                     f"{value}\r\n").encode()
        for key, (filename, blob) in files.items():
            ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
                     f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
            body += blob + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        data, headers = bytes(body), {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    else:
        data = urllib.parse.urlencode(fields).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:  # the API explains itself in the body, not the code
        return json.loads(exc.read())
