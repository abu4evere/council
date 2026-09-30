"""Sending verification codes.

WHAT THIS NEEDS FROM YOU. An SMTP account. A personal Gmail works and is the
practical choice when there is no domain yet: turn on 2-step verification, make
an App Password, and put it in SMTP_PASSWORD. Roughly 500 messages a day, free,
no domain required.

A WARNING ABOUT THAT. A Gmail App Password grants full SMTP access to a real
email account -- it is a more sensitive credential than any API key here, and
it belongs in .env and nowhere else. A dedicated address rather than a primary
one is the safer choice.

WITHOUT CREDENTIALS, codes are printed to the server console instead of sent.
That is not a stub to be replaced later: it is how the flow is developed and
tested without a mail account, and how a self-hoster runs a private instance
for themselves. A feature that cannot be exercised without credentials is a
feature nobody tests.

DELIVERABILITY, stated plainly rather than discovered later: mail from a
personal Gmail to strangers frequently lands in spam. Verification codes are
the kind of message people look for, so it is survivable, but anyone testing
this should be told to check that folder.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip()
                and os.environ.get("SMTP_USER", "").strip()
                and os.environ.get("SMTP_PASSWORD", "").strip())


def _from_address() -> str:
    return (os.environ.get("SMTP_FROM", "").strip()
            or os.environ.get("SMTP_USER", "").strip())


def _body(code: str) -> tuple[str, str]:
    text = (
        f"Your Unstuck verification code is {code}\n\n"
        "It expires in 15 minutes. If you did not ask for this, ignore it --\n"
        "nobody can create an account without the code.\n"
    )
    html = f"""<!doctype html><html><body style="margin:0;background:#141413;
      font-family:ui-sans-serif,-apple-system,'Segoe UI',system-ui,sans-serif;">
      <div style="max-width:420px;margin:40px auto;padding:30px;background:#1c1c1a;
                  border:1px solid #302f2c;border-radius:16px;color:#f0eee6;">
        <div style="font-size:22px;margin-bottom:6px;">&#9878; Unstuck</div>
        <p style="color:#a5a29a;font-size:14px;line-height:1.6;margin:0 0 22px;">
          Here is the code to finish creating your account.</p>
        <div style="font-size:30px;font-weight:700;letter-spacing:.22em;
                    background:#232321;border:1px solid #302f2c;border-radius:10px;
                    padding:16px;text-align:center;">{code}</div>
        <p style="color:#6e6b63;font-size:12px;line-height:1.6;margin:22px 0 0;">
          It expires in 15 minutes. If you did not ask for this, ignore it &mdash;
          nobody can create an account without the code.</p>
      </div></body></html>"""
    return text, html


def send_code(to_address: str, code: str) -> tuple[bool, str]:
    """Deliver a code. Returns (sent, detail).

    Never raises. A mail server that is down or misconfigured must produce a
    message the user can act on, not a traceback -- and never a 500 that leaves
    them unable to tell whether an account was created.
    """
    if not configured():
        # Development and private self-hosting: the operator reads the console.
        print(f"\n[mail] verification code for {to_address}: {code}\n", flush=True)
        return True, "console"

    msg = EmailMessage()
    msg["Subject"] = f"{code} is your Unstuck code"
    msg["From"] = _from_address()
    msg["To"] = to_address
    text, html = _body(code)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    host = os.environ["SMTP_HOST"].strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"].strip()
    password = os.environ["SMTP_PASSWORD"].strip()

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20,
                                  context=ssl.create_default_context()) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, password)
                s.send_message(msg)
        return True, "sent"
    except smtplib.SMTPAuthenticationError:
        return False, ("the mail account rejected the login -- for Gmail this "
                       "means an App Password is required, not the normal one")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"
