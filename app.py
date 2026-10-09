"""Museum GitHub Balie: Flask-webapp voor GitHub-beheer door museummedewerkers.

Applicatie: Museum GitHub Balie
Beschrijving: Beheer repositories, pull requests, issues en workflows voor
    Museum van de 20ste Eeuw.
Onderdeel: Hoofdapplicatie en GitHub API-koppeling
Gemaakt door: Eric Greuter
Project: museum-20e-eeuw/github_software_portaal
Copyright (c) 2026 Museum van de 20ste Eeuw
Techniek: Python, Flask en GitHub REST API
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session as flask_session,
    url_for,
)

from app_new import create_blueprint


APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SESSION_COOKIE_NAME = "museum_github_sid"
GITHUB_API_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# Configuratie en aanmeldgegevens
# ---------------------------------------------------------------------------


@dataclass
class AuthSession:
    """Bewaar de GitHub-aanmeldgegevens die bij een tijdelijke sessie horen."""

    username: str
    token: str
    created_at: datetime


class GitHubApiError(Exception):
    """GitHub-fout met de HTTP-statuscode voor de juiste afhandeling."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def load_app_config() -> dict[str, Any]:
    """Lees de lokale instellingen uit config.json."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_app_config(updates: dict[str, Any]) -> None:
    """Werk instellingen bij en sla de volledige configuratie op."""
    APP_CONFIG.update(updates)
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(APP_CONFIG, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    logger.info("Configuratie bijgewerkt; velden: %s", sorted(set(updates) - {"personal_access_token"}))


APP_CONFIG = load_app_config()
LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def configure_app_logging() -> None:
    """Schrijf gescheiden, roterende logbestanden in de ingestelde werkmap."""
    configured_level = str(APP_CONFIG.get("log_level") or "DEBUG").upper()
    log_level = LOG_LEVELS.get(configured_level, logging.DEBUG)
    log_directory = os.path.join(
        str(APP_CONFIG.get("workspace_root") or os.path.join(APP_DIR, "workspace")),
        "logging",
    )
    os.makedirs(log_directory, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for logger_name, filename in (
        ("museum_software_portaal.app", "app.log"),
        ("museum_software_portaal.app_new", "app_new.log"),
    ):
        module_logger = logging.getLogger(logger_name)
        module_logger.setLevel(log_level)
        module_logger.propagate = False
        log_path = os.path.abspath(os.path.join(log_directory, filename))
        for existing_handler in list(module_logger.handlers):
            if (
                isinstance(existing_handler, RotatingFileHandler)
                and os.path.abspath(existing_handler.baseFilename) != log_path
            ):
                module_logger.removeHandler(existing_handler)
                existing_handler.close()
        if not any(
            isinstance(handler, RotatingFileHandler)
            and os.path.abspath(handler.baseFilename) == log_path
            for handler in module_logger.handlers
        ):
            handler = RotatingFileHandler(
                log_path,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            handler.setFormatter(formatter)
            module_logger.addHandler(handler)
        for handler in module_logger.handlers:
            if isinstance(handler, RotatingFileHandler):
                handler.setLevel(log_level)


configure_app_logging()
logger = logging.getLogger("museum_software_portaal.app")
logger.info(
    "Logging gestart op niveau %s; logmap: %s",
    str(APP_CONFIG.get("log_level") or "DEBUG").upper(),
    os.path.join(str(APP_CONFIG.get("workspace_root") or os.path.join(APP_DIR, "workspace")), "logging"),
)

SESSION_STORE: dict[str, AuthSession] = {}
USER_ACTIVITY: dict[str, list[dict[str, str]]] = {}
USER_ACTIVITY_LIMIT = 50


def _activity_session_id() -> str | None:
    """Zoek de sessie waaraan een gebruikersactie gekoppeld moet worden."""
    session_id = flask_session.get("activity_session_id")
    if not session_id:
        session_id = secrets.token_urlsafe(18)
        flask_session["activity_session_id"] = session_id
    return str(session_id)


def record_user_activity(action: str, detail: str) -> None:
    """Bewaar een actie tijdelijk voor de huidige browsersessie."""
    session_id = _activity_session_id()
    events = USER_ACTIVITY.setdefault(session_id, [])
    events.insert(
        0,
        {
            "action": action,
            "detail": detail,
            "username": g.auth.username if g.auth else str(APP_CONFIG.get("default_username") or "Lokale gebruiker"),
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )
    del events[USER_ACTIVITY_LIMIT:]


def get_user_activity() -> list[dict[str, str]]:
    """Geef de recente acties van de huidige aanmeldsessie terug."""
    session_id = _activity_session_id()
    username = g.auth.username if g.auth else str(APP_CONFIG.get("default_username") or "Lokale gebruiker")
    return [event for event in USER_ACTIVITY.get(session_id, []) if event["username"] == username]


app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, "templates"),
    static_folder=os.path.join(APP_DIR, "static"),
)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = os.getenv("MUSEUM_GITHUB_APP_SECRET", secrets.token_hex(32))


# ---------------------------------------------------------------------------
# Datumweergave voor pagina's en lijsten
# ---------------------------------------------------------------------------


def parse_github_datetime(value: str | None) -> datetime | None:
    """Zet een GitHub-datum om naar een Python-datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_full_datetime(value: str | None) -> str:
    """Maak van een GitHub-datum een volledige datum- en tijdweergave."""
    parsed = parse_github_datetime(value)
    if parsed is None:
        return "-"
    return parsed.astimezone().strftime("%d-%m-%Y %H:%M")


def format_relative_datetime(value: str | None) -> str:
    """Toon hoe lang geleden een activiteit heeft plaatsgevonden."""
    parsed = parse_github_datetime(value)
    if parsed is None:
        return "-"

    now = datetime.now(timezone.utc)
    delta = now - parsed.astimezone(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "zojuist"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} min geleden"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} uur geleden"
    days = seconds // 86400
    return f"{days} dag(en) geleden"


app.jinja_env.filters["datetime_full"] = format_full_datetime
app.jinja_env.filters["datetime_relative"] = format_relative_datetime


# ---------------------------------------------------------------------------
# Systeemcontroles (Git, Visual Studio Code, Arduino IDE)
# ---------------------------------------------------------------------------

SYSTEM_CHECK_CACHE_SECONDS = 60 * 30
SYSTEM_CHECK_LOCK = threading.Lock()
SYSTEM_CHECK_CACHE: dict[str, Any] = {"checked_at": 0.0, "results": []}

VERSION_TAG_PATTERN = re.compile(r"\d+(?:\.\d+)+")
NUMERIC_VERSION_PATTERN = re.compile(r"(v?)(\d+(?:\.\d+)*)", re.IGNORECASE)


def parse_version_tuple(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    match = VERSION_TAG_PATTERN.search(value)
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def suggest_next_version(current: str | None) -> str:
    """Verhoog het laatste cijfer met behoud van notatie (1 -> 2, 1.0 -> 1.1, 1.2.3 -> 1.2.4, 0.01 -> 0.02)."""
    match = NUMERIC_VERSION_PATTERN.fullmatch((current or "").strip())
    if not match:
        return "0.1.0"
    prefix, numbers = match.groups()
    parts = numbers.split(".")
    last = parts[-1]
    parts[-1] = str(int(last) + 1).zfill(len(last))
    return f"{prefix}{'.'.join(parts)}"


def compare_versions(local: str | None, latest: str | None) -> str:
    """Vergelijk versies en geef 'ok', 'outdated' of 'unknown' terug."""
    local_tuple = parse_version_tuple(local)
    latest_tuple = parse_version_tuple(latest)
    if local_tuple is None or latest_tuple is None:
        return "unknown"
    if local_tuple >= latest_tuple:
        return "ok"
    return "outdated"


def fetch_latest_github_release(repo: str) -> str | None:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = Request(
        url=url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "museum-github-balie",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            version = str(payload.get("tag_name") or payload.get("name") or "")
            logger.debug("Laatste release opgehaald voor %s: %s", repo, version or "geen versietag")
            return version
    except (HTTPError, URLError, TimeoutError, Exception) as exc:
        logger.warning("Laatste release voor %s kon niet worden opgehaald: %s", repo, exc)
        return None


def detect_git_version() -> str | None:
    try:
        output = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if output.returncode != 0:
        return None
    return output.stdout.strip() or None


def detect_vscode_version() -> str | None:
    for command in ("code", "code.cmd"):
        try:
            output = subprocess.run(
                [command, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if output.returncode == 0 and output.stdout.strip():
            first_line = output.stdout.strip().splitlines()[0]
            return first_line
    return None


def read_windows_file_version(file_path: str) -> str | None:
    if not os.path.exists(file_path):
        return None
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(file_path, None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(file_path, 0, size, buffer)

        value = ctypes.c_void_p()
        value_size = wintypes.UINT()
        ctypes.windll.version.VerQueryValueW(
            buffer,
            "\\",
            ctypes.byref(value),
            ctypes.byref(value_size),
        )

        class VSFixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        info = ctypes.cast(value, ctypes.POINTER(VSFixedFileInfo)).contents
        major = info.dwFileVersionMS >> 16
        minor = info.dwFileVersionMS & 0xFFFF
        build = info.dwFileVersionLS >> 16
        revision = info.dwFileVersionLS & 0xFFFF
        return f"{major}.{minor}.{build}.{revision}"
    except Exception:
        return None


def detect_arduino_ide_version() -> str | None:
    candidates = [
        os.path.join(os.getenv("ProgramFiles", r"C:\Program Files"), "Arduino IDE", "Arduino IDE.exe"),
        os.path.join(
            os.getenv("LOCALAPPDATA", ""),
            "Programs",
            "Arduino IDE",
            "Arduino IDE.exe",
        ),
    ]
    for candidate in candidates:
        version = read_windows_file_version(candidate)
        if version:
            return version
    return None


SYSTEM_CHECK_ICONS = {
    "ok": "✓",
    "warning": "⚠",
    "error": "✕",
    "info": "ℹ",
}


def build_system_check(name: str, local_version: str | None, latest_version: str | None) -> dict[str, Any]:
    if local_version is None:
        return {
            "name": name,
            "level": "error",
            "icon": SYSTEM_CHECK_ICONS["error"],
            "local_version": None,
            "latest_version": latest_version,
            "message": f"{name} is niet geinstalleerd op deze computer.",
        }

    comparison = compare_versions(local_version, latest_version)
    if comparison == "outdated":
        return {
            "name": name,
            "level": "warning",
            "icon": SYSTEM_CHECK_ICONS["warning"],
            "local_version": local_version,
            "latest_version": latest_version,
            "message": (
                f"{name} is verouderd: geinstalleerd {local_version}, "
                f"laatste versie is {latest_version}."
            ),
        }
    if comparison == "unknown":
        return {
            "name": name,
            "level": "info",
            "icon": SYSTEM_CHECK_ICONS["info"],
            "local_version": local_version,
            "latest_version": latest_version,
            "message": (
                f"{name} gevonden ({local_version}). "
                "Kon de laatste versie niet ophalen om te vergelijken."
            ),
        }
    return {
        "name": name,
        "level": "ok",
        "icon": SYSTEM_CHECK_ICONS["ok"],
        "local_version": local_version,
        "latest_version": latest_version,
        "message": f"{name} is up-to-date ({local_version}).",
    }


def run_system_checks() -> list[dict[str, Any]]:
    logger.info("Systeemcontrole gestart.")
    git_local = detect_git_version()
    vscode_local = detect_vscode_version()
    arduino_local = detect_arduino_ide_version()

    git_latest = fetch_latest_github_release("git-for-windows/git")
    vscode_latest = fetch_latest_github_release("microsoft/vscode")
    arduino_latest = fetch_latest_github_release("arduino/arduino-ide")

    results = [
        build_system_check("Git", git_local, git_latest),
        build_system_check("Visual Studio Code", vscode_local, vscode_latest),
        build_system_check("Arduino IDE", arduino_local, arduino_latest),
    ]
    logger.info("Systeemcontrole afgerond voor %s hulpmiddelen.", len(results))
    return results


def get_system_check_results() -> list[dict[str, Any]]:
    with SYSTEM_CHECK_LOCK:
        now = time.time()
        if now - SYSTEM_CHECK_CACHE["checked_at"] < SYSTEM_CHECK_CACHE_SECONDS and SYSTEM_CHECK_CACHE["results"]:
            logger.debug("Systeemcontrole uit cache teruggegeven.")
            return SYSTEM_CHECK_CACHE["results"]

    results = run_system_checks()

    with SYSTEM_CHECK_LOCK:
        SYSTEM_CHECK_CACHE["checked_at"] = time.time()
        SYSTEM_CHECK_CACHE["results"] = results
    return results


# ---------------------------------------------------------------------------
# Navigatie en communicatie met de GitHub API
# ---------------------------------------------------------------------------


def get_nav_items() -> list[dict[str, str]]:
    return [
        {"endpoint": "dashboard", "label": "Dashboard"},
    ]


def build_submenu(active: str, repo_name: str) -> list[dict[str, str]]:
    items = [
        ("overview", "Overzicht"),
        ("pulls", "Pull Requests"),
        ("issues", "Issues"),
        ("workflows", "Workflows"),
        ("branches", "Branches"),
    ]
    return [
        {
            "label": label,
            "url": url_for("repository_detail", repo_name=repo_name, section=section),
            "active": "true" if section == active else "false",
        }
        for section, label in items
    ]


def extract_error_message(raw_body: str) -> str:
    """Haal een leesbare foutmelding uit de API-respons van GitHub."""
    if not raw_body:
        return "Onbekende fout vanuit GitHub."
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body
    if isinstance(payload, dict):
        if "errors" in payload and isinstance(payload["errors"], list):
            details = []
            for item in payload["errors"]:
                if isinstance(item, dict) and item.get("message"):
                    details.append(str(item["message"]))
            if details:
                return f"{payload.get('message', 'GitHub fout')}: {'; '.join(details)}"
        if payload.get("message"):
            return str(payload["message"])
    return raw_body


def github_request(
    token: str,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    """Voer een geauthenticeerd GitHub API-verzoek uit."""
    logger.debug("GitHub API-verzoek gestart: %s %s", method, path)
    query = f"?{urlencode(params, doseq=True)}" if params else ""
    url = f"{GITHUB_API_BASE}{path}{query}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "museum-github-balie",
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"

    req = Request(url=url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(req, timeout=30) as response:
            response_body = response.read()
            logger.debug("GitHub API-verzoek geslaagd: %s %s (HTTP %s)", method, path, response.status)
            if response.status in (204, 205) or not response_body:
                return None
            return json.loads(response_body.decode("utf-8"))
    except HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        message = extract_error_message(raw_body)
        logger.warning("GitHub API-verzoek mislukt: %s %s (HTTP %s).", method, path, exc.code)
        raise GitHubApiError(exc.code, message) from exc
    except URLError as exc:
        logger.error("GitHub API niet bereikbaar voor %s %s: %s", method, path, exc.reason)
        raise GitHubApiError(503, f"GitHub is niet bereikbaar: {exc.reason}") from exc


def require_login(view_func):
    """Bescherm een route en stuur niet-aangemelde bezoekers naar de login."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if g.auth is None:
            logger.warning("Toegang geweigerd voor niet-aangemelde aanvraag: %s %s", request.method, request.path)
            flash("Log eerst in om GitHub-gegevens op te halen.", "error")
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped


def current_org() -> str:
    return str(APP_CONFIG["organization"])


def current_page_size() -> int:
    return int(APP_CONFIG.get("page_size", 20))


def workflow_repo_limit() -> int:
    return int(APP_CONFIG.get("workflow_repo_limit", 6))


def preview_limit() -> int:
    return int(APP_CONFIG.get("repo_preview_limit", 5))


def repo_path(repo_name: str, suffix: str = "") -> str:
    """Bouw een URL-pad voor een repository binnen de ingestelde organisatie."""
    encoded_org = quote(current_org(), safe="")
    encoded_repo = quote(repo_name, safe="")
    return f"/repos/{encoded_org}/{encoded_repo}{suffix}"


def workspace_root() -> str:
    """Geef de lokale werkmap terug en maak die zo nodig aan."""
    root = str(APP_CONFIG.get("workspace_root") or os.path.join(APP_DIR, "workspace"))
    os.makedirs(root, exist_ok=True)
    return root


def local_repo_path(repo_name: str) -> str:
    return os.path.join(workspace_root(), repo_name)


def is_repo_cloned_locally(repo_name: str) -> bool:
    return os.path.isdir(os.path.join(local_repo_path(repo_name), ".git"))


def add_local_project_paths(repositories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Vul repositorygegevens aan met het lokale pad en de kloonstatus."""
    root = workspace_root()

    def sync_state(repo_name: str) -> str:
        try:
            return sync_state_label(get_local_git_status(repo_name))
        except (OSError, subprocess.SubprocessError, ValueError):
            return "out_of_sync"

    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda repo: sync_state(repo["name"]), repositories))
    return [
        {
            **repo,
            "local_path": os.path.abspath(os.path.join(root, repo["name"])),
            "cloned": state != "missing",
            "sync_state": state,
        }
        for repo, state in zip(repositories, states)
    ]


REPO_ACTIVITY_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Lokale projectmappen en Git-bewerkingen
# ---------------------------------------------------------------------------


def fetch_repo_activity(token: str, repo: dict[str, Any]) -> dict[str, Any]:
    """Laatste commit (wie/wanneer) en versie uit commit, release of tag."""
    name = repo["name"]
    cache_key = (name, str(repo.get("pushed_at") or ""))
    cached = REPO_ACTIVITY_CACHE.get(cache_key)
    if cached is not None:
        logger.debug("Repositoryactiviteit uit cache: %s", name)
        return cached

    activity: dict[str, Any] = {"last_commit_author": None, "last_commit_at": None, "version": None}
    try:
        commits = github_request(token, "GET", repo_path(name, "/commits"), params={"per_page": 1})
        if commits:
            commit = commits[0]
            activity["sha"] = str(commit.get("sha", ""))[:7]
            info = commit.get("commit", {})
            commit_message = str(info.get("message") or "")
            activity["version"] = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in commit_message.splitlines()
                    if line.strip().lower().startswith("versie:")
                    and line.split(":", 1)[1].strip()
                ),
                None,
            )
            activity["last_commit_author"] = (
                (commit.get("author") or {}).get("login") or info.get("author", {}).get("name")
            )
            activity["last_commit_at"] = info.get("author", {}).get("date")
    except GitHubApiError as exc:
        logger.debug("Laatste commit voor %s niet beschikbaar: %s", name, exc.message)

    try:
        release = github_request(token, "GET", repo_path(name, "/releases/latest"))
        if release and not activity["version"]:
            activity["version"] = release.get("tag_name") or release.get("name")
    except GitHubApiError as exc:
        logger.debug("Release voor %s niet beschikbaar: %s", name, exc.message)
    if not activity["version"]:
        try:
            tags = github_request(token, "GET", repo_path(name, "/tags"), params={"per_page": 1})
            if tags:
                activity["version"] = tags[0].get("name")
        except GitHubApiError as exc:
            logger.debug("Tags voor %s niet beschikbaar: %s", name, exc.message)

    if not activity["version"] and activity.get("sha"):
        activity["version"] = f"commit {activity['sha']}"

    REPO_ACTIVITY_CACHE[cache_key] = activity
    logger.debug("Repositoryactiviteit opgehaald: %s", name)
    return activity


def add_repo_activity(token: str, repositories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not repositories:
        return repositories
    with ThreadPoolExecutor(max_workers=8) as pool:
        activities = list(pool.map(lambda repo: fetch_repo_activity(token, repo), repositories))
    return [{**repo, **activity} for repo, activity in zip(repositories, activities)]


def run_git(repo_name: str, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Voer een Git-opdracht uit vanuit de lokale map van een project."""
    command = ["git", *args]
    logger.debug("Git-opdracht gestart voor %s: %s", repo_name, args[0] if args else "onbekend")
    try:
        result = subprocess.run(
            command,
            cwd=local_repo_path(repo_name),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        logger.exception("Git-opdracht kon niet worden uitgevoerd voor %s.", repo_name)
        raise
    if result.returncode:
        logger.warning("Git-opdracht mislukt voor %s (exitcode %s).", repo_name, result.returncode)
    else:
        logger.debug("Git-opdracht afgerond voor %s.", repo_name)
    return result


def get_local_git_status(repo_name: str) -> dict[str, Any]:
    """Bepaalt of de lokale kopie bestaat, en of die in sync is met github.com."""
    if not is_repo_cloned_locally(repo_name):
        return {
            "cloned": False,
            "in_sync": None,
            "local_branch": None,
            "ahead": 0,
            "behind": 0,
            "has_local_changes": False,
            "changed_files": [],
        }

    try:
        run_git(repo_name, ["fetch", "--quiet", "origin"], timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Remote status ophalen mislukt voor %s (%s).", repo_name, type(exc).__name__)

    branch_result = run_git(repo_name, ["rev-parse", "--abbrev-ref", "HEAD"])
    local_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None

    ahead = 0
    behind = 0
    if local_branch:
        counts = run_git(
            repo_name,
            ["rev-list", "--left-right", "--count", f"origin/{local_branch}...HEAD"],
        )
        if counts.returncode == 0 and counts.stdout.strip():
            parts = counts.stdout.strip().split()
            if len(parts) == 2:
                behind, ahead = int(parts[0]), int(parts[1])

    status_result = run_git(repo_name, ["status", "--porcelain"])
    changed_files = []
    if status_result.returncode == 0:
        for line in status_result.stdout.splitlines():
            if line.strip():
                changed_files.append(line[3:].strip())

    in_sync = ahead == 0 and behind == 0 and not changed_files

    def commit_lines(revision_range: str) -> list[str]:
        if not local_branch or not (ahead or behind):
            return []
        result = run_git(repo_name, ["log", "--format=%h %s (%an)", "-n", "20", revision_range])
        return [line for line in result.stdout.splitlines() if line.strip()] if result.returncode == 0 else []

    diff_lines: list[str] = []
    if local_branch and (ahead or behind):
        diff = run_git(repo_name, ["diff", "--name-status", f"HEAD...origin/{local_branch}"])
        if diff.returncode == 0:
            diff_lines = [line.replace("\t", "  ") for line in diff.stdout.splitlines() if line.strip()]

    return {
        "cloned": True,
        "in_sync": in_sync,
        "local_branch": local_branch,
        "ahead": ahead,
        "behind": behind,
        "has_local_changes": bool(changed_files),
        "changed_files": changed_files,
        "incoming_commits": commit_lines(f"HEAD..origin/{local_branch}"),
        "outgoing_commits": commit_lines(f"origin/{local_branch}..HEAD"),
        "remote_changed_files": diff_lines,
    }


def sync_state_label(status: dict[str, Any]) -> str:
    if not status["cloned"]:
        return "missing"
    return "in_sync" if status["in_sync"] else "out_of_sync"


def sync_repository_locally(repo_name: str, direction: str) -> str:
    """Breng de lokale map in sync: 'pull' (GitHub naar lokaal) of 'push' (lokaal naar GitHub)."""
    status = get_local_git_status(repo_name)
    branch = status["local_branch"]
    if not status["cloned"] or not branch:
        raise GitHubApiError(400, "Dit project staat niet (correct) lokaal.")
    if direction == "pull":
        reset = run_git(repo_name, ["reset", "--hard", f"origin/{branch}"], timeout=60)
        if reset.returncode != 0:
            raise GitHubApiError(500, f"Bijwerken is mislukt: {reset.stderr.strip() or reset.stdout.strip()}")
        clean = run_git(repo_name, ["clean", "-fd"], timeout=60)
        if clean.returncode != 0:
            raise GitHubApiError(500, f"Opschonen is mislukt: {clean.stderr.strip() or clean.stdout.strip()}")
        return "Lokale map is gelijkgetrokken met GitHub."
    if direction == "push":
        if status["behind"]:
            raise GitHubApiError(409, "GitHub heeft nieuwere commits. Kies eerst 'GitHub naar lokaal' of los dit handmatig op.")
        if status["has_local_changes"]:
            add = run_git(repo_name, ["add", "-A"])
            commit = run_git(repo_name, ["commit", "-m", "Synchronisatie vanuit softwareportaal"])
            if add.returncode != 0 or commit.returncode != 0:
                raise GitHubApiError(500, f"Committen is mislukt: {(commit.stderr or commit.stdout or add.stderr).strip()}")
        push = run_git(repo_name, ["push", "origin", branch], timeout=120)
        if push.returncode != 0:
            raise GitHubApiError(500, f"Pushen is mislukt: {push.stderr.strip() or push.stdout.strip()}")
        return "Lokale wijzigingen zijn naar GitHub gestuurd."
    raise GitHubApiError(400, "Onbekende synchronisatierichting.")


def clone_repository_locally(repo_name: str, clone_url: str, token: str) -> None:
    """Kloon een GitHub-repository naar de ingestelde lokale werkmap."""
    target = local_repo_path(repo_name)
    if os.path.isdir(target):
        logger.debug("Repository staat al lokaal: %s", repo_name)
        return
    logger.info("Lokale kloon gestart voor repository %s.", repo_name)
    authed_url = clone_url.replace("https://", f"https://{quote(token, safe='')}@", 1)
    result = subprocess.run(
        ["git", "clone", authed_url, target],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        logger.error("Klonen mislukt voor %s (exitcode %s).", repo_name, result.returncode)
        raise GitHubApiError(500, f"Klonen is mislukt: {result.stderr.strip() or result.stdout.strip()}")
    logger.info("Repository lokaal gekloond: %s.", repo_name)


def update_repository_locally(repo_name: str, default_branch: str) -> str:
    """Werk de lokale map bij naar de remote standaardbranch (harde reset)."""
    logger.info("Lokale repository bijwerken: %s (branch=%s).", repo_name, default_branch)
    run_git(repo_name, ["fetch", "--quiet", "origin"], timeout=60)
    checkout = run_git(repo_name, ["checkout", default_branch])
    if checkout.returncode != 0:
        run_git(repo_name, ["checkout", "-B", default_branch, f"origin/{default_branch}"])
    reset = run_git(repo_name, ["reset", "--hard", f"origin/{default_branch}"])
    if reset.returncode != 0:
        logger.error("Bijwerken van %s mislukt: %s", repo_name, reset.stderr.strip() or reset.stdout.strip())
        raise GitHubApiError(500, f"Bijwerken is mislukt: {reset.stderr.strip() or reset.stdout.strip()}")
    logger.info("Lokale repository bijgewerkt: %s.", repo_name)
    return reset.stdout.strip() or "Lokale map is bijgewerkt."


PROJECT_FILE_IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".vs", ".vscode"}


def list_project_files(repo_name: str) -> list[dict[str, Any]]:
    """Maak een lijst van projectbestanden en sla technische mappen over."""
    root = local_repo_path(repo_name)
    entries: list[dict[str, Any]] = []
    if not os.path.isdir(root):
        return entries

    for current_dir, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if name not in PROJECT_FILE_IGNORE_DIRS]
        rel_dir = os.path.relpath(current_dir, root)
        for file_name in sorted(file_names):
            rel_path = file_name if rel_dir == "." else os.path.join(rel_dir, file_name)
            rel_path = rel_path.replace("\\", "/")
            full_path = os.path.join(current_dir, file_name)
            try:
                size = os.path.getsize(full_path)
            except OSError:
                size = 0
            entries.append(
                {
                    "path": rel_path,
                    "name": file_name,
                    "size": size,
                    "extension": os.path.splitext(file_name)[1].lower(),
                }
            )
    entries.sort(key=lambda item: item["path"].lower())
    return entries


TOOL_FOR_EXTENSION = {
    ".ino": "arduino",
    ".pde": "arduino",
    ".py": "vscode",
    ".js": "vscode",
    ".ts": "vscode",
    ".json": "vscode",
    ".md": "vscode",
    ".html": "vscode",
    ".css": "vscode",
    ".yml": "vscode",
    ".yaml": "vscode",
    ".txt": "vscode",
    ".c": "vscode",
    ".cpp": "vscode",
    ".h": "vscode",
}


def guess_tool_for_file(file_path: str) -> str:
    """Kies op basis van de extensie welke editor een bestand opent."""
    extension = os.path.splitext(file_path)[1].lower()
    return TOOL_FOR_EXTENSION.get(extension, "system")


def resolve_tool_command(tool: str, absolute_path: str) -> list[str]:
    """Zoek de startopdracht voor de gekozen editor of standaardapp."""
    if tool == "arduino":
        candidates = [
            os.path.join(os.getenv("ProgramFiles", r"C:\Program Files"), "Arduino IDE", "Arduino IDE.exe"),
            os.path.join(os.getenv("LOCALAPPDATA", ""), "Programs", "Arduino IDE", "Arduino IDE.exe"),
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return [candidate, absolute_path]
        raise GitHubApiError(500, "Arduino IDE is niet gevonden op deze computer.")
    if tool == "vscode":
        for command in ("code", "code.cmd"):
            found = shutil.which(command)
            if found:
                return [found, "--wait", absolute_path]
        raise GitHubApiError(500, "Visual Studio Code is niet gevonden op deze computer.")
    # Onbekend bestandstype: open met de standaard Windows-app.
    return ["cmd", "/c", "start", "", absolute_path]


OPEN_FILE_SESSIONS: dict[str, dict[str, Any]] = {}
OPEN_FILE_LOCK = threading.Lock()


def _watch_editor_process(session_id: str, popen: "subprocess.Popen[Any]", tool: str) -> None:
    popen.wait()
    with OPEN_FILE_LOCK:
        session = OPEN_FILE_SESSIONS.get(session_id)
        if session is not None:
            session["closed"] = True
            session["closed_at"] = time.time()


def open_file_in_tool(repo_name: str, file_path: str) -> dict[str, Any]:
    """Open een lokaal projectbestand en registreer de editor-sessie."""
    root = local_repo_path(repo_name)
    absolute_path = os.path.normpath(os.path.join(root, file_path))
    if not absolute_path.startswith(os.path.normpath(root)) or not os.path.isfile(absolute_path):
        raise GitHubApiError(404, "Bestand niet gevonden in de lokale projectmap.")

    tool = guess_tool_for_file(file_path)
    command = resolve_tool_command(tool, absolute_path)

    try:
        popen = subprocess.Popen(command)
    except OSError as exc:
        raise GitHubApiError(500, f"Kon de tool niet starten: {exc}") from exc

    session_id = secrets.token_urlsafe(12)
    with OPEN_FILE_LOCK:
        OPEN_FILE_SESSIONS[session_id] = {
            "repo_name": repo_name,
            "file_path": file_path,
            "tool": tool,
            "closed": False,
            "opened_at": time.time(),
        }

    if tool == "vscode":
        # `code --wait` blokkeert al tot het tabblad sluit, dus watcher-thread volstaat.
        pass

    watcher = threading.Thread(target=_watch_editor_process, args=(session_id, popen, tool), daemon=True)
    watcher.start()

    return {"session_id": session_id, "tool": tool}


def get_open_file_session(session_id: str) -> dict[str, Any] | None:
    with OPEN_FILE_LOCK:
        session = OPEN_FILE_SESSIONS.get(session_id)
        return dict(session) if session else None


def create_github_release(
    token: str, repo_name: str, tag: str, branch: str, notes: str
) -> str:
    """Maakt een GitHub-release; geeft een melding terug en gooit geen fout bij mislukken."""
    try:
        github_request(
            token,
            "POST",
            repo_path(repo_name, "/releases"),
            payload={"tag_name": tag, "target_commitish": branch, "name": tag, "body": notes},
        )
    except GitHubApiError as exc:
        return f" Release {tag} kon niet worden aangemaakt: {exc.message}"
    return f" Release {tag} is aangemaakt."


def commit_and_push_changes(
    token: str,
    repo_name: str,
    default_branch: str,
    author_name: str,
    version: str,
    summary: str,
) -> str:
    """Commit lokale wijzigingen, push ze en maak daarna een GitHub-release."""
    add_result = run_git(repo_name, ["add", "-A"])
    if add_result.returncode != 0:
        raise GitHubApiError(500, f"Kon wijzigingen niet stagen: {add_result.stderr.strip()}")

    commit_message = f"{summary}\n\nDoor: {author_name}\nVersie: {version}"
    commit_result = run_git(repo_name, ["commit", "-m", commit_message])
    if commit_result.returncode != 0:
        combined = f"{commit_result.stdout}\n{commit_result.stderr}".strip()
        if "nothing to commit" in combined.lower():
            raise GitHubApiError(400, "Er zijn geen wijzigingen om in te checken.")
        raise GitHubApiError(500, f"Commit is mislukt: {combined}")

    push_result = run_git(repo_name, ["push", "origin", f"HEAD:{default_branch}"], timeout=60)
    if push_result.returncode != 0:
        raise GitHubApiError(500, f"Push is mislukt: {push_result.stderr.strip() or push_result.stdout.strip()}")

    notes = f"{summary}\n\nDoor: {author_name}"
    release_message = create_github_release(token, repo_name, version, default_branch, notes)
    return "Wijzigingen zijn gecommit en gepusht naar GitHub." + release_message


def get_who_is_working_on(token: str, repo_name: str) -> list[dict[str, Any]]:
    """Geeft open pull requests / branches van anderen als indicatie van lopend werk."""
    pulls = get_repository_pulls(token, repo_name, per_page=10)
    working: list[dict[str, Any]] = []
    for pull in pulls:
        working.append(
            {
                "user": pull.get("user", {}).get("login", "onbekend"),
                "branch": pull.get("head", {}).get("ref", ""),
                "title": pull.get("title", ""),
                "updated_at": pull.get("updated_at"),
                "url": pull.get("html_url"),
            }
        )
    return working


def search_items(token: str, query: str, per_page: int | None = None) -> dict[str, Any]:
    """Zoek issues of pull requests via de GitHub-zoek-API."""
    result = github_request(
        token,
        "GET",
        "/search/issues",
        params={
            "q": query,
            "sort": "updated",
            "order": "desc",
            "per_page": per_page or current_page_size(),
        },
    )
    return result


def get_org_repositories(token: str) -> list[dict[str, Any]]:
    """Haal repositories van de ingestelde GitHub-organisatie op."""
    return github_request(
        token,
        "GET",
        f"/orgs/{quote(current_org(), safe='')}/repos",
        params={"sort": "pushed", "direction": "desc", "per_page": 100, "type": "all"},
    )


def get_repository(token: str, repo_name: str) -> dict[str, Any]:
    return github_request(token, "GET", repo_path(repo_name))


def get_repository_pulls(token: str, repo_name: str, per_page: int | None = None) -> list[dict[str, Any]]:
    return github_request(
        token,
        "GET",
        repo_path(repo_name, "/pulls"),
        params={"state": "open", "per_page": per_page or current_page_size()},
    )


def get_repository_issues(token: str, repo_name: str, per_page: int | None = None) -> list[dict[str, Any]]:
    issues = github_request(
        token,
        "GET",
        repo_path(repo_name, "/issues"),
        params={"state": "open", "per_page": per_page or current_page_size()},
    )
    return [issue for issue in issues if "pull_request" not in issue]


def parse_version_commit_message(message: str) -> dict[str, str | None]:
    """Haal versie, naam en wijzigingsomschrijving uit een commitbericht van het portaal."""
    version = author = None
    summary_lines: list[str] = []
    for line in message.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "versie" and value.strip() and version is None:
            version = value.strip()
        elif key == "door" and value.strip() and author is None:
            author = value.strip()
        else:
            summary_lines.append(line)
    return {"version": version, "author": author, "summary": "\n".join(summary_lines).strip()}


def get_version_history(token: str, repo_name: str, limit: int = 100) -> list[dict[str, Any]]:
    """Versiehistorie: versie, datum, wie en wat er gewijzigd is, nieuwste eerst."""
    try:
        commits = github_request(token, "GET", repo_path(repo_name, "/commits"), params={"per_page": limit})
    except GitHubApiError as exc:
        logger.debug("Commits voor %s niet beschikbaar: %s", repo_name, exc.message)
        commits = []
    history: list[dict[str, Any]] = []
    for commit in commits or []:
        info = commit.get("commit", {})
        parsed = parse_version_commit_message(str(info.get("message") or ""))
        if not parsed["version"]:
            continue
        history.append(
            {
                "version": parsed["version"],
                "date": info.get("author", {}).get("date"),
                "author": parsed["author"]
                or (commit.get("author") or {}).get("login")
                or info.get("author", {}).get("name"),
                "summary": parsed["summary"],
            }
        )

    # Neem releases mee zonder versiecommit, zoals de startrelease van een nieuw project.
    try:
        releases = github_request(token, "GET", repo_path(repo_name, "/releases"), params={"per_page": 30})
    except GitHubApiError as exc:
        logger.debug("Releases voor %s niet beschikbaar: %s", repo_name, exc.message)
        releases = []
    known_versions = {item["version"] for item in history}
    for release in releases or []:
        version = release.get("tag_name") or release.get("name")
        if not version or version in known_versions:
            continue
        history.append(
            {
                "version": version,
                "date": release.get("published_at") or release.get("created_at"),
                "author": (release.get("author") or {}).get("login"),
                "summary": str(release.get("body") or "").strip(),
            }
        )
    history.sort(key=lambda item: str(item["date"] or ""), reverse=True)
    return history


def get_repository_branches(token: str, repo_name: str, per_page: int | None = None) -> list[dict[str, Any]]:
    return github_request(
        token,
        "GET",
        repo_path(repo_name, "/branches"),
        params={"per_page": per_page or current_page_size()},
    )


def get_repository_workflow_runs(
    token: str,
    repo_name: str,
    per_page: int | None = None,
) -> list[dict[str, Any]]:
    payload = github_request(
        token,
        "GET",
        repo_path(repo_name, "/actions/runs"),
        params={"per_page": per_page or current_page_size()},
    )
    return payload.get("workflow_runs", [])


def get_recent_workflow_runs(token: str, repositories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for repo in repositories[: workflow_repo_limit()]:
        repo_name = repo["name"]
        for run in get_repository_workflow_runs(token, repo_name, per_page=4):
            run["repo_name"] = repo_name
            runs.append(run)
    runs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return runs[: current_page_size()]


def add_repo_name(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in items:
        repository_url = item.get("repository_url") or ""
        item["repo_name"] = repository_url.rsplit("/", 1)[-1] if repository_url else ""
    return items


def get_org_pull_requests(token: str) -> dict[str, Any]:
    query = f"org:{current_org()} is:pr state:open archived:false"
    result = search_items(token, query)
    result["items"] = add_repo_name(result.get("items", []))
    return result


def get_org_issues(token: str) -> dict[str, Any]:
    query = f"org:{current_org()} is:issue state:open archived:false"
    result = search_items(token, query)
    result["items"] = add_repo_name(result.get("items", []))
    return result


def get_current_auth() -> AuthSession | None:
    """Zoek de ingelogde gebruiker op aan de hand van de sessiecookie."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    return SESSION_STORE.get(session_id)


def parse_form_or_json() -> dict[str, Any]:
    if request.is_json:
        return dict(request.get_json(silent=False) or {})
    return {key: value for key, value in request.form.items()}


def api_success(message: str, **extra: Any):
    """Maak een consistente JSON-respons voor een geslaagde API-actie."""
    payload = {"ok": True, "message": message}
    payload.update(extra)
    return jsonify(payload)


def api_error(message: str, status_code: int):
    """Maak een consistente JSON-foutrespons met de juiste HTTP-status."""
    return jsonify({"ok": False, "message": message}), status_code


AUTO_LOGIN_CACHE: dict[str, Any] = {"session_id": None, "verified_at": 0.0}
AUTO_LOGIN_RECHECK_SECONDS = 300


def auto_login_from_config() -> str | None:
    """Zorgt voor automatisch inloggen op basis van het PAT in config.json.

    Geeft de sessie-id terug die in de cookie gezet moet worden, of None.
    """
    token = str(APP_CONFIG.get("personal_access_token") or "").strip()
    if not token:
        return None

    cached_session_id = AUTO_LOGIN_CACHE.get("session_id")
    if cached_session_id and cached_session_id in SESSION_STORE:
        if time.time() - AUTO_LOGIN_CACHE["verified_at"] < AUTO_LOGIN_RECHECK_SECONDS:
            return cached_session_id

    try:
        profile = github_request(token, "GET", "/user")
    except GitHubApiError as exc:
        logger.warning("Automatisch aanmelden mislukt (HTTP %s).", exc.status_code)
        return None
    github_login = str(profile.get("login", ""))
    expected_login = str(APP_CONFIG["default_username"])
    if github_login.lower() != expected_login.lower():
        logger.warning("Automatisch aanmelden geweigerd: GitHub-account komt niet overeen met configuratie.")
        return None

    session_id = secrets.token_urlsafe(24)
    SESSION_STORE[session_id] = AuthSession(
        username=github_login,
        token=token,
        created_at=datetime.now(timezone.utc),
    )
    g.auth = SESSION_STORE[session_id]
    record_user_activity("Aangemeld", "Automatisch via de opgeslagen configuratie")
    AUTO_LOGIN_CACHE["session_id"] = session_id
    AUTO_LOGIN_CACHE["verified_at"] = time.time()
    logger.info("Automatisch aangemeld als %s.", github_login)
    return session_id


# ---------------------------------------------------------------------------
# Flask-hooks voor sessies, templates en foutmeldingen
# ---------------------------------------------------------------------------


@app.before_request
def load_request_context():
    """Vul de request-context met een bestaande of automatisch gemaakte sessie."""
    g.auth = get_current_auth()
    logger.debug(
        "Aanvraag ontvangen: %s %s (aangemeld=%s).",
        request.method,
        request.path,
        g.auth is not None,
    )
    g.new_auto_login_session_id = None
    if g.auth is None:
        session_id = auto_login_from_config()
        if session_id:
            g.auth = SESSION_STORE.get(session_id)
            g.new_auto_login_session_id = session_id


@app.after_request
def apply_auto_login_cookie(response):
    """Plaats een sessiecookie wanneer automatisch aanmelden is gelukt."""
    session_id = getattr(g, "new_auto_login_session_id", None)
    if session_id:
        logger.debug("Automatische sessiecookie toegevoegd.")
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_id,
            httponly=True,
            samesite="Lax",
            secure=False,
            max_age=60 * 60 * 8,
        )
    return response


LIGHTBAR_CACHE_SECONDS = 30
LIGHTBAR_CACHE: dict[str, Any] = {"key": None, "checked_at": 0.0, "data": None}


def build_active_work(pulls: dict[str, Any]) -> list[dict[str, Any]]:
    active_work = [
        {
            "user": pull.get("user", {}).get("login", "onbekend"),
            "repo_name": pull.get("repo_name", ""),
            "title": pull.get("title", ""),
            "updated_at": pull.get("updated_at"),
            "url": pull.get("html_url"),
        }
        for pull in pulls.get("items", [])
    ]
    active_work.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return active_work


def get_lightbar_data() -> dict[str, Any]:
    """Gegevens voor de altijd zichtbare lichtbalk, kort gecachet."""
    empty = {"repo_count": 0, "active_work": []}
    if not g.get("auth") or request.endpoint in (None, "static"):
        return empty
    key = (g.auth.username, current_org())
    cached = LIGHTBAR_CACHE["data"]
    if (
        cached is not None
        and LIGHTBAR_CACHE["key"] == key
        and time.time() - LIGHTBAR_CACHE["checked_at"] < LIGHTBAR_CACHE_SECONDS
    ):
        return cached
    try:
        repos = get_org_repositories(g.auth.token)
        pulls = get_org_pull_requests(g.auth.token)
    except Exception as exc:
        logger.debug("Lichtbalkgegevens niet beschikbaar: %s", exc)
        return empty
    data = {"repo_count": len(repos), "active_work": build_active_work(pulls)[:6]}
    LIGHTBAR_CACHE.update(key=key, checked_at=time.time(), data=data)
    return data


@app.context_processor
def inject_template_context():
    """Deel algemene app-, gebruiker- en systeemgegevens met templates."""
    return {
        "app_name": APP_CONFIG["app_name"],
        "organization": current_org(),
        "default_username": APP_CONFIG["default_username"],
        "today": datetime.now().strftime("%d-%m-%Y"),
        "now_time": datetime.now().strftime("%H:%M"),
        "nav_items": get_nav_items(),
        "current_user": g.auth.username if g.auth else None,
        "system_checks": get_system_check_results(),
        "user_activity": get_user_activity(),
        "lightbar": get_lightbar_data(),
    }


@app.errorhandler(GitHubApiError)
def handle_github_error(exc: GitHubApiError):
    """Vertaal GitHub-fouten naar JSON-antwoorden of een melding in de pagina."""
    logger.warning("GitHub-fout afgehandeld voor %s %s (HTTP %s).", request.method, request.path, exc.status_code)
    if request.path.startswith("/api/"):
        return api_error(exc.message, exc.status_code)
    flash(exc.message, "error")
    if g.auth is None:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Paginaroutes voor dashboard en GitHub-informatie
# ---------------------------------------------------------------------------


@app.route("/")
def home():
    if g.auth is None:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        token = (request.form.get("token") or "").strip()
        username = (request.form.get("username") or "").strip()
        next_url = (request.form.get("next") or "").strip()
        logger.info("Handmatige GitHub-aanmelding gestart voor gebruikersnaam %s.", username or "(ontbreekt)")
        if not token or not username:
            logger.warning("Aanmelding afgewezen: gebruikersnaam of token ontbreekt.")
            flash("Een GitHub username en token zijn verplicht.", "error")
            return render_template("login.html", next_url=next_url)

        profile = github_request(token, "GET", "/user")
        github_login = str(profile.get("login", ""))
        if github_login.lower() != username.lower():
            logger.warning("Aanmelding afgewezen: token hoort bij een ander GitHub-account dan opgegeven.")
            flash(
                f"Je token hoort bij {github_login}, niet bij {username}.",
                "error",
            )
            return render_template("login.html", next_url=next_url)

        save_app_config({"default_username": github_login, "personal_access_token": token})
        AUTO_LOGIN_CACHE["session_id"] = None
        AUTO_LOGIN_CACHE["verified_at"] = 0.0

        session_id = secrets.token_urlsafe(24)
        SESSION_STORE[session_id] = AuthSession(
            username=github_login,
            token=token,
            created_at=datetime.now(timezone.utc),
        )
        g.auth = SESSION_STORE[session_id]
        record_user_activity("Aangemeld", "Handmatig met GitHub")
        target = next_url or url_for("dashboard")
        response = make_response(redirect(target))
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_id,
            httponly=True,
            samesite="Lax",
            secure=False,
            max_age=60 * 60 * 8,
        )
        logger.info("Gebruiker %s is aangemeld.", github_login)
        return response

    return render_template("login.html", next_url=(request.args.get("next") or ""))


@app.post("/logout")
def logout():
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    auth = SESSION_STORE.get(session_id) if session_id else None
    if auth:
        record_user_activity("Afgemeld", "GitHub-sessie beëindigd")
    if session_id:
        SESSION_STORE.pop(session_id, None)
    logger.info("Gebruiker uitgelogd: %s.", auth.username if auth else "onbekend")
    response = make_response(redirect(url_for("login")))
    response.delete_cookie(SESSION_COOKIE_NAME)
    flash("Je bent uitgelogd.", "success")
    return response


@app.route("/dashboard")
@require_login
def dashboard():
    repos = get_org_repositories(g.auth.token)
    pulls = get_org_pull_requests(g.auth.token)
    active_work = build_active_work(pulls)

    active_repo_names = {item["repo_name"] for item in active_work if item["repo_name"]}
    repos_with_work = [repo for repo in repos if repo["name"] in active_repo_names]
    other_repos = [repo for repo in repos if repo["name"] not in active_repo_names]
    ordered_repos = add_repo_activity(
        g.auth.token, add_local_project_paths(repos_with_work + other_repos)
    )
    logger.debug("Dashboardgegevens geladen: %s repositories, %s actieve items.", len(repos), len(active_work))

    return render_template(
        "dashboard.html",
        active_nav="dashboard",
        repositories=ordered_repos,
        repo_count=len(repos),
        active_work=active_work[:6],
    )


@app.route("/repositories")
@require_login
def repositories():
    repos = get_org_repositories(g.auth.token)
    return render_template(
        "repositories.html",
        active_nav="repositories",
        repositories=add_repo_activity(g.auth.token, add_local_project_paths(repos)),
    )


@app.route("/pull-requests")
@require_login
def pull_requests():
    result = get_org_pull_requests(g.auth.token)
    repositories = get_org_repositories(g.auth.token)
    return render_template(
        "pull_requests.html",
        active_nav="pull_requests",
        pulls=result["items"],
        pull_total=result.get("total_count", 0),
        repositories=repositories,
    )


@app.route("/issues")
@require_login
def issues():
    result = get_org_issues(g.auth.token)
    repositories = get_org_repositories(g.auth.token)
    return render_template(
        "issues.html",
        active_nav="issues",
        issues=result["items"],
        issue_total=result.get("total_count", 0),
        repositories=repositories,
    )


@app.route("/workflows")
@require_login
def workflows():
    repositories = get_org_repositories(g.auth.token)
    workflow_runs = get_recent_workflow_runs(g.auth.token, repositories)
    return render_template(
        "workflows.html",
        active_nav="workflows",
        workflow_runs=workflow_runs,
    )


@app.route("/repository/<repo_name>")
@require_login
def repository_detail(repo_name: str):
    section = (request.args.get("section") or "overview").strip().lower()
    if section not in {"overview", "pulls", "issues", "workflows", "branches"}:
        abort(404)

    repo = get_repository(g.auth.token, repo_name)
    page_data: dict[str, Any] = {}
    if section == "overview":
        page_data["pulls"] = get_repository_pulls(g.auth.token, repo_name, per_page=preview_limit())
        page_data["issues"] = get_repository_issues(g.auth.token, repo_name, per_page=preview_limit())
        page_data["workflow_runs"] = get_repository_workflow_runs(
            g.auth.token,
            repo_name,
            per_page=preview_limit(),
        )
        page_data["branches"] = get_repository_branches(g.auth.token, repo_name, per_page=preview_limit())
    elif section == "pulls":
        page_data["pulls"] = get_repository_pulls(g.auth.token, repo_name)
    elif section == "issues":
        page_data["issues"] = get_repository_issues(g.auth.token, repo_name)
    elif section == "workflows":
        page_data["workflow_runs"] = get_repository_workflow_runs(g.auth.token, repo_name)
    elif section == "branches":
        page_data["branches"] = get_repository_branches(g.auth.token, repo_name)

    return render_template(
        "repository_detail.html",
        active_nav="repositories",
        repo=repo,
        section=section,
        submenu_items=build_submenu(section, repo_name),
        **page_data,
    )


@app.route("/project/<repo_name>")
@require_login
def project_detail(repo_name: str):
    """Toon de GitHub-status en lokale bestanden van een softwareproject."""
    repo = get_repository(g.auth.token, repo_name)
    working_on = get_who_is_working_on(g.auth.token, repo_name)
    local_status = get_local_git_status(repo_name)
    files = list_project_files(repo_name) if local_status["cloned"] else []
    record_user_activity("Softwareproject geopend", repo_name)
    return render_template(
        "project_detail.html",
        active_nav="dashboard",
        repo=repo,
        working_on=working_on,
        version_history=get_version_history(g.auth.token, repo_name),
        local_status=local_status,
        files=files,
    )


# ---------------------------------------------------------------------------
# API-routes voor lokale projecten en bestandsbewerking
# ---------------------------------------------------------------------------


@app.post("/api/projects/<repo_name>/clone")
@require_login
def clone_project(repo_name: str):
    """Kloon een repository op verzoek vanuit de projectpagina."""
    logger.info("Lokale kloon aangevraagd voor repository %s.", repo_name)
    repo = get_repository(g.auth.token, repo_name)
    clone_repository_locally(repo_name, repo["clone_url"], g.auth.token)
    record_user_activity("Softwareproject lokaal opgehaald", repo_name)
    return api_success(f"{repo_name} is lokaal opgehaald.")


@app.post("/api/projects/<repo_name>/delete")
@require_login
def delete_project(repo_name: str):
    """Verwijder de GitHub-repository na expliciete naambevestiging."""
    logger.warning("Verwijdering van GitHub-repository aangevraagd: %s.", repo_name)
    payload = parse_form_or_json()
    confirm_name = str(payload.get("confirm_name", ""))
    try:
        keystrokes = int(payload.get("keystrokes", 0))
    except (TypeError, ValueError):
        keystrokes = 0
    if confirm_name != repo_name:
        return api_error("De ingetypte naam komt niet overeen met de repository.", 400)
    if keystrokes < len(repo_name):
        return api_error("De naam moet handmatig worden getypt (plakken is niet toegestaan).", 400)

    try:
        github_request(g.auth.token, "DELETE", repo_path(repo_name))
    except GitHubApiError as exc:
        if exc.status_code in (403, 404):
            return api_error(
                "Verwijderen is geweigerd. Je token heeft het recht 'delete_repo' nodig en je moet "
                f"beheerder van de repository zijn. GitHub meldt: {exc.message}",
                exc.status_code,
            )
        raise
    for key in [key for key in REPO_ACTIVITY_CACHE if key[0] == repo_name]:
        REPO_ACTIVITY_CACHE.pop(key, None)
    logger.info("GitHub-repository verwijderd: %s.", repo_name)
    record_user_activity("Softwareproject van GitHub verwijderd", repo_name)
    return api_success(
        f"Repository {repo_name} is verwijderd van GitHub. De lokale map is niet aangeraakt.",
        redirect=url_for("dashboard"),
    )


@app.post("/api/projects/<repo_name>/update")
@require_login
def update_project(repo_name: str):
    """Werk de lokale projectmap bij vanaf GitHub."""
    logger.info("Lokale update aangevraagd voor repository %s.", repo_name)
    repo = get_repository(g.auth.token, repo_name)
    message = update_repository_locally(repo_name, repo["default_branch"])
    record_user_activity("Softwareproject bijgewerkt vanaf GitHub", repo_name)
    return api_success(message)


@app.post("/api/projects/<repo_name>/sync")
@require_login
def sync_project(repo_name: str):
    """Breng de lokale map in sync met GitHub in de gekozen richting."""
    direction = str(parse_form_or_json().get("direction", ""))
    logger.info("Lokale synchronisatie aangevraagd voor %s (richting=%s).", repo_name, direction)
    message = sync_repository_locally(repo_name, direction)
    record_user_activity("Softwareproject gesynchroniseerd", f"{repo_name} ({direction})")
    return api_success(message)


@app.get("/api/projects/<repo_name>/status")
@require_login
def project_status(repo_name: str):
    return jsonify({"ok": True, "status": get_local_git_status(repo_name)})


@app.get("/api/projects/<repo_name>/version-suggestion")
@require_login
def project_version_suggestion(repo_name: str):
    repo = get_repository(g.auth.token, repo_name)
    activity = fetch_repo_activity(g.auth.token, repo)
    current_version = activity.get("version")
    return jsonify(
        {
            "ok": True,
            "current_version": current_version,
            "suggested_version": suggest_next_version(current_version),
        }
    )


@app.post("/api/projects/<repo_name>/open-file")
@require_login
def open_project_file(repo_name: str):
    payload = parse_form_or_json()
    file_path = str(payload.get("file_path", "")).strip()
    if not file_path:
        logger.warning("Bestand openen afgewezen: geen bestandspad ontvangen.")
        return api_error("Kies eerst een bestand.", 400)
    result = open_file_in_tool(repo_name, file_path)
    logger.info("Bestand geopend voor project %s met %s.", repo_name, result["tool"])
    record_user_activity("Projectbestand geopend", f"{repo_name} · {file_path}")
    return api_success(f"{file_path} wordt geopend met {result['tool']}.", **result)


@app.get("/api/open-file-sessions/<session_id>")
@require_login
def open_file_session_status(session_id: str):
    session = get_open_file_session(session_id)
    if session is None:
        return api_error("Onbekende sessie.", 404)
    changed_files: list[str] = []
    if session["closed"]:
        status = get_local_git_status(session["repo_name"])
        changed_files = status["changed_files"]
    return jsonify(
        {
            "ok": True,
            "closed": session["closed"],
            "repo_name": session["repo_name"],
            "file_path": session["file_path"],
            "changed_files": changed_files,
        }
    )


@app.post("/api/projects/<repo_name>/commit")
@require_login
def commit_project_changes(repo_name: str):
    """Valideer commitgegevens en publiceer lokale wijzigingen naar GitHub."""
    payload = parse_form_or_json()
    author_name = str(payload.get("author_name", "")).strip()
    version = str(payload.get("version", "")).strip()
    summary = str(payload.get("summary", "")).strip()
    if not author_name:
        logger.warning("Commit afgewezen voor %s: naam ontbreekt.", repo_name)
        return api_error("Vul je naam in.", 400)
    if not version:
        logger.warning("Commit afgewezen voor %s: versienummer ontbreekt.", repo_name)
        return api_error("Vul een versienummer in.", 400)
    if not summary:
        logger.warning("Commit afgewezen voor %s: samenvatting ontbreekt.", repo_name)
        return api_error("Beschrijf wat je hebt gewijzigd.", 400)

    repo = get_repository(g.auth.token, repo_name)
    message = commit_and_push_changes(
        g.auth.token, repo_name, repo["default_branch"], author_name, version, summary
    )
    for key in [key for key in REPO_ACTIVITY_CACHE if key[0] == repo_name]:
        REPO_ACTIVITY_CACHE.pop(key, None)
    logger.info("Lokale wijzigingen gecommit en gepusht voor %s (versie=%s).", repo_name, version)
    record_user_activity("Wijzigingen opgeslagen en naar GitHub gepusht", f"{repo_name} · versie {version}")
    return api_success(message)


# ---------------------------------------------------------------------------
# Instellingen, hulp en ondersteunende acties
# ---------------------------------------------------------------------------


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        new_workspace_root = (request.form.get("workspace_root") or "").strip()
        new_token = (request.form.get("personal_access_token") or "").strip()
        new_log_level = (request.form.get("log_level") or "DEBUG").strip().upper()
        if new_log_level not in LOG_LEVELS:
            logger.warning("Instellingen niet opgeslagen: ongeldig logniveau.")
            flash("Kies een geldig logniveau.", "error")
            return redirect(url_for("settings_page"))
        if not new_workspace_root:
            logger.warning("Instellingen niet opgeslagen: werkmap ontbreekt.")
            flash("Vul een geldig mappad in.", "error")
            return redirect(url_for("settings_page"))
        try:
            os.makedirs(new_workspace_root, exist_ok=True)
            os.makedirs(os.path.join(new_workspace_root, "logging"), exist_ok=True)
        except OSError as exc:
            logger.exception("Werkmap kon niet worden aangemaakt of gebruikt.")
            flash(f"Kon de map niet aanmaken/gebruiken: {exc}", "error")
            return redirect(url_for("settings_page"))

        changed_fields = []
        if APP_CONFIG.get("workspace_root") != new_workspace_root:
            changed_fields.append("werkmap")
        if str(APP_CONFIG.get("log_level") or "DEBUG").upper() != new_log_level:
            changed_fields.append("logniveau")
        if str(APP_CONFIG.get("personal_access_token") or "") != new_token:
            changed_fields.append("GitHub-token")

        save_app_config(
            {
                "workspace_root": new_workspace_root,
                "personal_access_token": new_token,
                "log_level": new_log_level,
            }
        )
        configure_app_logging()
        logger.info("Instellingen opgeslagen; werkmap gewijzigd naar %s.", new_workspace_root)
        record_user_activity(
            "Instellingen opgeslagen",
            ", ".join(changed_fields) if changed_fields else "Instellingen gecontroleerd",
        )
        AUTO_LOGIN_CACHE["session_id"] = None
        AUTO_LOGIN_CACHE["verified_at"] = 0.0
        return redirect(url_for("dashboard"))

    record_user_activity("Instellingen geopend", "")
    return render_template(
        "settings.html",
        active_nav="settings_page",
        workspace_root=workspace_root(),
        personal_access_token=str(APP_CONFIG.get("personal_access_token") or ""),
        log_level=str(APP_CONFIG.get("log_level") or "DEBUG").upper(),
    )


@app.get("/api/browse-folders")
def browse_folders():
    requested_path = (request.args.get("path") or "").strip()

    if not requested_path:
        # Toon de beschikbare schijven als startpunt.
        drives = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = f"{letter}:\\"
            if os.path.isdir(drive):
                drives.append({"name": drive, "path": drive})
        return jsonify({"ok": True, "current_path": "", "parent_path": None, "folders": drives})

    if not os.path.isdir(requested_path):
        logger.warning("Mappen bladeren afgewezen: opgegeven pad bestaat niet.")
        return api_error("Deze map bestaat niet.", 404)

    try:
        subfolders = []
        with os.scandir(requested_path) as iterator:
            for entry in iterator:
                try:
                    if entry.is_dir():
                        subfolders.append({"name": entry.name, "path": entry.path})
                except OSError:
                    continue
        subfolders.sort(key=lambda item: item["name"].lower())
    except PermissionError:
        logger.warning("Geen toegang tot de aangevraagde map.")
        return api_error("Geen toegang tot deze map.", 403)

    normalized = os.path.normpath(requested_path)
    parent = os.path.dirname(normalized)
    is_drive_root = normalized.rstrip("\\") == os.path.splitdrive(normalized)[0]
    parent_path = None if is_drive_root else parent

    return jsonify(
        {
            "ok": True,
            "current_path": requested_path,
            "parent_path": parent_path,
            "folders": subfolders,
        }
    )


@app.route("/help")
def help_page():
    return render_template("help.html", active_nav="help_page")


@app.post("/api/system-checks/refresh")
def refresh_system_checks():
    logger.info("Verversing van systeemcontrole aangevraagd.")
    with SYSTEM_CHECK_LOCK:
        SYSTEM_CHECK_CACHE["checked_at"] = 0.0
    results = get_system_check_results()
    return api_success("Systeemcontrole vernieuwd.", checks=results)


# ---------------------------------------------------------------------------
# API-acties voor issues, pull requests en workflows
# ---------------------------------------------------------------------------


@app.post("/api/issues")
@require_login
def create_issue():
    """Maak een nieuw issue aan in de gekozen repository."""
    payload = parse_form_or_json()
    repo_name = str(payload.get("repo_name", "")).strip()
    title = str(payload.get("title", "")).strip()
    body = str(payload.get("body", "")).strip()
    if not repo_name:
        logger.warning("Issue aanmaken afgewezen: repository ontbreekt.")
        return api_error("Kies eerst een softwareproject.", 400)
    if not title:
        logger.warning("Issue aanmaken afgewezen voor %s: titel ontbreekt.", repo_name)
        return api_error("Een issue-titel is verplicht.", 400)

    issue = github_request(
        g.auth.token,
        "POST",
        repo_path(repo_name, "/issues"),
        payload={"title": title, "body": body},
    )
    logger.info("Issue #%s aangemaakt in %s.", issue["number"], repo_name)
    record_user_activity("Issue aangemaakt", f"{repo_name} · #{issue['number']}")
    return api_success(
        f"Issue #{issue['number']} is aangemaakt in {repo_name}.",
        url=issue["html_url"],
    )


@app.post("/api/issues/<repo_name>/<int:issue_number>/close")
@require_login
def close_issue(repo_name: str, issue_number: int):
    github_request(
        g.auth.token,
        "PATCH",
        repo_path(repo_name, f"/issues/{issue_number}"),
        payload={"state": "closed"},
    )
    logger.info("Issue #%s gesloten in %s.", issue_number, repo_name)
    record_user_activity("Issue gesloten", f"{repo_name} · #{issue_number}")
    return api_success(f"Issue #{issue_number} is gesloten.")


@app.post("/api/pulls/<repo_name>/<int:pull_number>/merge")
@require_login
def merge_pull_request(repo_name: str, pull_number: int):
    """Voeg een pull request samen met de gekozen GitHub-methode."""
    payload = parse_form_or_json()
    merge_method = str(payload.get("merge_method", "squash")).strip().lower() or "squash"
    if merge_method not in {"merge", "squash", "rebase"}:
        logger.warning("Pull request #%s in %s afgewezen: ongeldige merge-methode.", pull_number, repo_name)
        return api_error("Onbekende merge-methode.", 400)

    result = github_request(
        g.auth.token,
        "PUT",
        repo_path(repo_name, f"/pulls/{pull_number}/merge"),
        payload={"merge_method": merge_method},
    )
    logger.info("Pull request #%s samengevoegd in %s (methode=%s).", pull_number, repo_name, merge_method)
    record_user_activity("Pull request samengevoegd", f"{repo_name} · #{pull_number}")
    return api_success(result.get("message", f"Pull request #{pull_number} is gemerged."))


@app.post("/api/workflows/<repo_name>/<int:run_id>/rerun")
@require_login
def rerun_workflow(repo_name: str, run_id: int):
    """Start een mislukte of afgeronde GitHub Actions-run opnieuw."""
    github_request(
        g.auth.token,
        "POST",
        repo_path(repo_name, f"/actions/runs/{run_id}/rerun"),
    )
    logger.info("Workflow-run %s opnieuw gestart in %s.", run_id, repo_name)
    record_user_activity("Workflow opnieuw gestart", f"{repo_name} · run {run_id}")
    return api_success(f"Workflow run {run_id} is opnieuw gestart.")


@app.post("/api/workflows/<repo_name>/<int:run_id>/cancel")
@require_login
def cancel_workflow(repo_name: str, run_id: int):
    """Annuleer een workflow-run die nog bezig is."""
    github_request(
        g.auth.token,
        "POST",
        repo_path(repo_name, f"/actions/runs/{run_id}/cancel"),
    )
    logger.info("Workflow-run %s geannuleerd in %s.", run_id, repo_name)
    record_user_activity("Workflow geannuleerd", f"{repo_name} · run {run_id}")
    return api_success(f"Workflow run {run_id} is geannuleerd.")


# Koppel de projectaanmaakroutes aan dezelfde authenticatie en GitHub-functies.
app.register_blueprint(
    create_blueprint(
        require_login=require_login,
        get_org_repositories=get_org_repositories,
        github_request=github_request,
        current_org=current_org,
        workspace_root=workspace_root,
        github_api_error=GitHubApiError,
        record_activity=record_user_activity,
    )
)


if __name__ == "__main__":
    # Start de lokale webapp en open het dashboard in de standaardbrowser.
    port = int(os.getenv("PORT", "5080"))
    threading.Timer(1.0, webbrowser.open, args=(f"http://127.0.0.1:{port}/dashboard",)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
