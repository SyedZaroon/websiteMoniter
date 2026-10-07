"""Email alerts for sites that are down or redirect to another domain."""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Sequence

from models import SiteReport


class AlertError(Exception):
    """Raised when an alert cannot be configured or delivered."""


_EMAIL_SETTINGS = {
    "WEBSITE_MONITOR_EMAIL_TO",
    "WEBSITE_MONITOR_EMAIL_FROM",
    "WEBSITE_MONITOR_SMTP_HOST",
    "WEBSITE_MONITOR_SMTP_PORT",
    "WEBSITE_MONITOR_SMTP_USERNAME",
    "WEBSITE_MONITOR_SMTP_PASSWORD",
}


def _load_settings(base_dir: Path) -> Dict[str, str]:
    settings: Dict[str, str] = {}
    env_file = base_dir / ".env"
    if env_file.exists():
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AlertError("Could not read email settings from .env: %s" % exc)
        for line_number, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if not separator:
                raise AlertError(".env line %d must use KEY=VALUE format" % line_number)
            name = name.strip()
            if name not in _EMAIL_SETTINGS:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            settings[name] = value

    for name in _EMAIL_SETTINGS:
        settings[name] = os.environ.get(name, settings.get(name, ""))

    settings["WEBSITE_MONITOR_SMTP_HOST"] = (
        settings["WEBSITE_MONITOR_SMTP_HOST"] or "smtp.gmail.com"
    )
    settings["WEBSITE_MONITOR_SMTP_PORT"] = (
        settings["WEBSITE_MONITOR_SMTP_PORT"] or "587"
    )
    settings["WEBSITE_MONITOR_EMAIL_FROM"] = (
        settings["WEBSITE_MONITOR_EMAIL_FROM"]
        or settings["WEBSITE_MONITOR_SMTP_USERNAME"]
    )
    return settings


def _alert_lines(reports: Sequence[SiteReport]) -> List[str]:
    lines: List[str] = []
    for report in reports:
        if report.status == "DOWN":
            lines.append("DOWN: %s (homepage: %s)" % (report.website, report.website))
        for result in report.results:
            if result.redirect_category == "cross_domain":
                lines.append(
                    "Redirect to another domain: %s -> %s"
                    % (result.url, result.final_url or result.first_redirect_location)
                )
    return lines


def send_alert(reports: Sequence[SiteReport], base_dir: Path) -> bool:
    """Send one email for this run if any monitored site is down or cross-domain redirects."""
    lines = _alert_lines(reports)
    if not lines:
        return False

    settings = _load_settings(base_dir)
    required = (
        "WEBSITE_MONITOR_EMAIL_TO",
        "WEBSITE_MONITOR_SMTP_USERNAME",
        "WEBSITE_MONITOR_SMTP_PASSWORD",
    )
    missing = [name for name in required if not settings[name].strip()]
    if missing:
        raise AlertError(
            "Email alert settings are incomplete; set %s in .env"
            % ", ".join(missing)
        )
    try:
        port = int(settings["WEBSITE_MONITOR_SMTP_PORT"])
    except ValueError:
        raise AlertError("WEBSITE_MONITOR_SMTP_PORT must be a number")
    if not 1 <= port <= 65535:
        raise AlertError("WEBSITE_MONITOR_SMTP_PORT must be between 1 and 65535")

    message = EmailMessage()
    message["Subject"] = "[Website Monitor] %d issue(s) detected" % len(lines)
    message["From"] = settings["WEBSITE_MONITOR_EMAIL_FROM"]
    message["To"] = settings["WEBSITE_MONITOR_EMAIL_TO"]
    message.set_content(
        "The website monitor found the following issue(s):\n\n"
        + "\n".join("- " + line for line in lines)
    )

    try:
        with smtplib.SMTP(
            settings["WEBSITE_MONITOR_SMTP_HOST"], port, timeout=30
        ) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(
                settings["WEBSITE_MONITOR_SMTP_USERNAME"],
                settings["WEBSITE_MONITOR_SMTP_PASSWORD"],
            )
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise AlertError("Email alert delivery failed: %s" % exc)
    return True
