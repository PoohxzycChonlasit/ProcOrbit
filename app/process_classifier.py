from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass


PYTHON = "Python"
JAVASCRIPT = "Node.js / JavaScript runtimes"
DEVELOPMENT = "Other development runtimes"
BROWSERS = "Browsers"
GAMES = "Games and launchers"
DESKTOP = "Desktop applications"
SYSTEM = "System processes"
UNKNOWN = "Unknown"

CATEGORIES = (
    PYTHON,
    JAVASCRIPT,
    DEVELOPMENT,
    BROWSERS,
    GAMES,
    DESKTOP,
    SYSTEM,
    UNKNOWN,
)


@dataclass(frozen=True, slots=True)
class Classification:
    category: str
    reason: str


_PYTHON_RE = re.compile(r"python(?:w|3(?:\.\d+)?)?\.exe\Z")
_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    (JAVASCRIPT, frozenset({"node.exe", "node", "deno.exe", "deno", "bun.exe", "bun"})),
    (
        DEVELOPMENT,
        frozenset({
            "java.exe", "javaw.exe", "dotnet.exe", "ruby.exe", "perl.exe",
            "php.exe", "lua.exe", "r.exe", "rscript.exe", "julia.exe",
            "pwsh.exe", "powershell.exe",
        }),
    ),
    (
        BROWSERS,
        frozenset({
            "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
            "opera.exe", "opera_gx.exe", "vivaldi.exe", "waterfox.exe",
        }),
    ),
    (
        GAMES,
        frozenset({
            "steam.exe", "epicgameslauncher.exe", "battle.net.exe",
            "riotclientservices.exe", "ubisoftconnect.exe", "goggalaxy.exe",
            "ea.exe", "xboxpcapp.exe", "robloxplayerbeta.exe",
            "fortniteclient-win64-shipping.exe", "minecraft.exe",
            "minecraftlauncher.exe",
        }),
    ),
    (
        DESKTOP,
        frozenset({
            "explorer.exe", "notepad.exe", "notepad++.exe", "mspaint.exe",
            "calc.exe", "calculatorapp.exe", "code.exe", "devenv.exe",
            "winword.exe", "excel.exe", "outlook.exe", "teams.exe",
            "slack.exe", "discord.exe", "zoom.exe", "obs64.exe",
            "spotify.exe",
        }),
    ),
    (
        SYSTEM,
        frozenset({
            "system", "system idle process", "registry", "secure system",
            "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
            "services.exe", "lsass.exe", "svchost.exe", "taskhostw.exe",
            "dwm.exe", "fontdrvhost.exe", "audiodg.exe", "spoolsv.exe",
            "conhost.exe",
        }),
    ),
)


def _basename(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip().strip('"')
    name = ntpath.basename(text).lower()
    return name or None


def classify_process(
    process_name: str | None,
    executable_path: str | None = None,
) -> Classification:
    """Classify only explicit, conservative process-name/executable matches."""

    candidates: list[tuple[str, str]] = []
    process_basename = _basename(process_name)
    executable_basename = _basename(executable_path)
    if process_basename:
        candidates.append((process_basename, "process name"))
    if executable_basename and executable_basename != process_basename:
        candidates.append((executable_basename, "executable basename"))

    for token, source in candidates:
        if token == "py.exe" or _PYTHON_RE.fullmatch(token):
            return Classification(PYTHON, f"Matched Python interpreter {source}: {token}.")

    for category, names in _RULES:
        for token, source in candidates:
            if token in names:
                return Classification(category, f"Matched known {category.lower()} {source}: {token}.")

    return Classification(
        UNKNOWN,
        "No conservative process-name or executable rule matched.",
    )
