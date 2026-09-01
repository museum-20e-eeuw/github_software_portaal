from __future__ import annotations

import ctypes
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
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
    url_for,
)


APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
SESSION_COOKIE_NAME = "museum_github_sid"
GITHUB_API_BASE = "https://api.github.com"


@dataclass
class AuthSession:
    username: str
    token: str
    created_at: datetime


class GitHubApiError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def load_app_config() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_app_config(updates: dict[str, Any]) -> None:
    APP_CONFIG.update(updates)
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(APP_CONFIG, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


APP_CONFIG = load_app_config()
SESSION_STORE: dict[str, AuthSession] = {}

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, "templates"),
    static_folder=os.path.join(APP_DIR, "static"),
)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = os.getenv("MUSEUM_GITHUB_APP_SECRET", secrets.token_hex(32))


def parse_github_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_full_datetime(value: str | None) -> str:
    parsed = parse_github_datetime(value)
    if parsed is None:
        return "-"
    return parsed.astimezone().strftime("%d-%m-%Y %H:%M")


def format_relative_datetime(value: str | None) -> str:
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


def parse_version_tuple(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    match = VERSION_TAG_PATTERN.search(value)
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def compare_versions(local: str | None, latest: str | None) -> str:
    """Returns 'ok', 'outdated' or 'unknown'."""
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
            return str(payload.get("tag_name") or payload.get("name") or "")
    except (HTTPError, URLError, TimeoutError, Exception):
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
    git_local = detect_git_version()
    vscode_local = detect_vscode_version()
    arduino_local = detect_arduino_ide_version()

    git_latest = fetch_latest_github_release("git-for-windows/git")
    vscode_latest = fetch_latest_github_release("microsoft/vscode")
    arduino_latest = fetch_latest_github_release("arduino/arduino-ide")

    return [
        build_system_check("Git", git_local, git_latest),
        build_system_check("Visual Studio Code", vscode_local, vscode_latest),
        build_system_check("Arduino IDE", arduino_local, arduino_latest),
    ]


def get_system_check_results() -> list[dict[str, Any]]:
    with SYSTEM_CHECK_LOCK:
        now = time.time()
        if now - SYSTEM_CHECK_CACHE["checked_at"] < SYSTEM_CHECK_CACHE_SECONDS and SYSTEM_CHECK_CACHE["results"]:
            return SYSTEM_CHECK_CACHE["results"]

    results = run_system_checks()

    with SYSTEM_CHECK_LOCK:
        SYSTEM_CHECK_CACHE["checked_at"] = time.time()
        SYSTEM_CHECK_CACHE["results"] = results
    return results


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
            if response.status in (204, 205) or not response_body:
                return None
            return json.loads(response_body.decode("utf-8"))
    except HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        raise GitHubApiError(exc.code, extract_error_message(raw_body)) from exc
    except URLError as exc:
        raise GitHubApiError(503, f"GitHub is niet bereikbaar: {exc.reason}") from exc


def require_login(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if g.auth is None:
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
    encoded_org = quote(current_org(), safe="")
    encoded_repo = quote(repo_name, safe="")
    return f"/repos/{encoded_org}/{encoded_repo}{suffix}"


def workspace_root() -> str:
    root = str(APP_CONFIG.get("workspace_root") or os.path.join(APP_DIR, "workspace"))
    os.makedirs(root, exist_ok=True)
    return root


def local_repo_path(repo_name: str) -> str:
    return os.path.join(workspace_root(), repo_name)


def is_repo_cloned_locally(repo_name: str) -> bool:
    return os.path.isdir(os.path.join(local_repo_path(repo_name), ".git"))


def run_git(repo_name: str, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=local_repo_path(repo_name),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


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
    except (OSError, subprocess.SubprocessError):
        pass

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

    return {
        "cloned": True,
        "in_sync": in_sync,
        "local_branch": local_branch,
        "ahead": ahead,
        "behind": behind,
        "has_local_changes": bool(changed_files),
        "changed_files": changed_files,
    }


def clone_repository_locally(repo_name: str, clone_url: str, token: str) -> None:
    target = local_repo_path(repo_name)
    if os.path.isdir(target):
        return
    authed_url = clone_url.replace("https://", f"https://{quote(token, safe='')}@", 1)
    result = subprocess.run(
        ["git", "clone", authed_url, target],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise GitHubApiError(500, f"Klonen is mislukt: {result.stderr.strip() or result.stdout.strip()}")


def update_repository_locally(repo_name: str, default_branch: str) -> str:
    """Zorgt dat de lokale map exact overeenkomt met github.com (harde reset op default branch)."""
    run_git(repo_name, ["fetch", "--quiet", "origin"], timeout=60)
    checkout = run_git(repo_name, ["checkout", default_branch])
    if checkout.returncode != 0:
        run_git(repo_name, ["checkout", "-B", default_branch, f"origin/{default_branch}"])
    reset = run_git(repo_name, ["reset", "--hard", f"origin/{default_branch}"])
    if reset.returncode != 0:
        raise GitHubApiError(500, f"Bijwerken is mislukt: {reset.stderr.strip() or reset.stdout.strip()}")
    return reset.stdout.strip() or "Lokale map is bijgewerkt."


PROJECT_FILE_IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".vs", ".vscode"}


def list_project_files(repo_name: str) -> list[dict[str, Any]]:
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
    extension = os.path.splitext(file_path)[1].lower()
    return TOOL_FOR_EXTENSION.get(extension, "system")


def resolve_tool_command(tool: str, absolute_path: str) -> list[str]:
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


def commit_and_push_changes(
    repo_name: str,
    default_branch: str,
    author_name: str,
    version: str,
    summary: str,
) -> str:
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

    return "Wijzigingen zijn gecommit en gepusht naar GitHub."


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
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    return SESSION_STORE.get(session_id)


def parse_form_or_json() -> dict[str, Any]:
    if request.is_json:
        return dict(request.get_json(silent=False) or {})
    return {key: value for key, value in request.form.items()}


def api_success(message: str, **extra: Any):
    payload = {"ok": True, "message": message}
    payload.update(extra)
    return jsonify(payload)


def api_error(message: str, status_code: int):
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
    except GitHubApiError:
        return None
    github_login = str(profile.get("login", ""))
    expected_login = str(APP_CONFIG["default_username"])
    if github_login.lower() != expected_login.lower():
        return None

    session_id = secrets.token_urlsafe(24)
    SESSION_STORE[session_id] = AuthSession(
        username=github_login,
        token=token,
        created_at=datetime.now(timezone.utc),
    )
    AUTO_LOGIN_CACHE["session_id"] = session_id
    AUTO_LOGIN_CACHE["verified_at"] = time.time()
    return session_id


@app.before_request
def load_request_context():
    g.auth = get_current_auth()
    g.new_auto_login_session_id = None
    if g.auth is None:
        session_id = auto_login_from_config()
        if session_id:
            g.auth = SESSION_STORE.get(session_id)
            g.new_auto_login_session_id = session_id


@app.after_request
def apply_auto_login_cookie(response):
    session_id = getattr(g, "new_auto_login_session_id", None)
    if session_id:
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_id,
            httponly=True,
            samesite="Lax",
            secure=False,
            max_age=60 * 60 * 8,
        )
    return response


@app.context_processor
def inject_template_context():
    return {
        "app_name": APP_CONFIG["app_name"],
        "organization": current_org(),
        "default_username": APP_CONFIG["default_username"],
        "today": datetime.now().strftime("%d-%m-%Y"),
        "now_time": datetime.now().strftime("%H:%M"),
        "nav_items": get_nav_items(),
        "current_user": g.auth.username if g.auth else None,
        "system_checks": get_system_check_results(),
    }


@app.errorhandler(GitHubApiError)
def handle_github_error(exc: GitHubApiError):
    if request.path.startswith("/api/"):
        return api_error(exc.message, exc.status_code)
    flash(exc.message, "error")
    if g.auth is None:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/")
def home():
    if g.auth is None:
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        token = (request.form.get("token") or "").strip()
        next_url = (request.form.get("next") or "").strip()
        if not token:
            flash("Een GitHub token is verplicht.", "error")
            return render_template("login.html", next_url=next_url)

        profile = github_request(token, "GET", "/user")
        github_login = str(profile.get("login", ""))
        expected_login = str(APP_CONFIG["default_username"])
        if github_login.lower() != expected_login.lower():
            flash(
                f"Deze app is ingericht voor {expected_login}. Je gebruikte token hoort bij {github_login}.",
                "error",
            )
            return render_template("login.html", next_url=next_url)

        session_id = secrets.token_urlsafe(24)
        SESSION_STORE[session_id] = AuthSession(
            username=github_login,
            token=token,
            created_at=datetime.now(timezone.utc),
        )
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
        return response

    return render_template("login.html", next_url=(request.args.get("next") or ""))


@app.post("/logout")
def logout():
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        SESSION_STORE.pop(session_id, None)
    response = make_response(redirect(url_for("login")))
    response.delete_cookie(SESSION_COOKIE_NAME)
    flash("Je bent uitgelogd.", "success")
    return response


@app.route("/dashboard")
@require_login
def dashboard():
    repos = get_org_repositories(g.auth.token)
    pulls = get_org_pull_requests(g.auth.token)
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
    return render_template(
        "dashboard.html",
        active_nav="dashboard",
        repositories=repos[:8],
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
        repositories=repos,
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
    repo = get_repository(g.auth.token, repo_name)
    working_on = get_who_is_working_on(g.auth.token, repo_name)
    local_status = get_local_git_status(repo_name)
    files = list_project_files(repo_name) if local_status["cloned"] else []
    return render_template(
        "project_detail.html",
        active_nav="dashboard",
        repo=repo,
        working_on=working_on,
        local_status=local_status,
        files=files,
    )


@app.post("/api/projects/<repo_name>/clone")
@require_login
def clone_project(repo_name: str):
    repo = get_repository(g.auth.token, repo_name)
    clone_repository_locally(repo_name, repo["clone_url"], g.auth.token)
    return api_success(f"{repo_name} is lokaal opgehaald.")


@app.post("/api/projects/<repo_name>/update")
@require_login
def update_project(repo_name: str):
    repo = get_repository(g.auth.token, repo_name)
    message = update_repository_locally(repo_name, repo["default_branch"])
    return api_success(message)


@app.get("/api/projects/<repo_name>/status")
@require_login
def project_status(repo_name: str):
    return jsonify({"ok": True, "status": get_local_git_status(repo_name)})


@app.post("/api/projects/<repo_name>/open-file")
@require_login
def open_project_file(repo_name: str):
    payload = parse_form_or_json()
    file_path = str(payload.get("file_path", "")).strip()
    if not file_path:
        return api_error("Kies eerst een bestand.", 400)
    result = open_file_in_tool(repo_name, file_path)
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
    payload = parse_form_or_json()
    author_name = str(payload.get("author_name", "")).strip()
    version = str(payload.get("version", "")).strip()
    summary = str(payload.get("summary", "")).strip()
    if not author_name:
        return api_error("Vul je naam in.", 400)
    if not version:
        return api_error("Vul een versienummer in.", 400)
    if not summary:
        return api_error("Beschrijf wat je hebt gewijzigd.", 400)

    repo = get_repository(g.auth.token, repo_name)
    message = commit_and_push_changes(repo_name, repo["default_branch"], author_name, version, summary)
    return api_success(message)


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        new_workspace_root = (request.form.get("workspace_root") or "").strip()
        new_token = (request.form.get("personal_access_token") or "").strip()
        if not new_workspace_root:
            flash("Vul een geldig mappad in.", "error")
            return redirect(url_for("settings_page"))
        try:
            os.makedirs(new_workspace_root, exist_ok=True)
        except OSError as exc:
            flash(f"Kon de map niet aanmaken/gebruiken: {exc}", "error")
            return redirect(url_for("settings_page"))

        save_app_config({"workspace_root": new_workspace_root, "personal_access_token": new_token})
        AUTO_LOGIN_CACHE["session_id"] = None
        AUTO_LOGIN_CACHE["verified_at"] = 0.0
        flash("Instellingen zijn opgeslagen.", "success")
        return redirect(url_for("settings_page"))

    return render_template(
        "settings.html",
        active_nav="settings_page",
        workspace_root=workspace_root(),
        personal_access_token=str(APP_CONFIG.get("personal_access_token") or ""),
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
    with SYSTEM_CHECK_LOCK:
        SYSTEM_CHECK_CACHE["checked_at"] = 0.0
    results = get_system_check_results()
    return api_success("Systeemcontrole vernieuwd.", checks=results)


@app.post("/api/issues")
@require_login
def create_issue():
    payload = parse_form_or_json()
    repo_name = str(payload.get("repo_name", "")).strip()
    title = str(payload.get("title", "")).strip()
    body = str(payload.get("body", "")).strip()
    if not repo_name:
        return api_error("Kies eerst een softwareproject.", 400)
    if not title:
        return api_error("Een issue-titel is verplicht.", 400)

    issue = github_request(
        g.auth.token,
        "POST",
        repo_path(repo_name, "/issues"),
        payload={"title": title, "body": body},
    )
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
    return api_success(f"Issue #{issue_number} is gesloten.")


@app.post("/api/pulls/<repo_name>/<int:pull_number>/merge")
@require_login
def merge_pull_request(repo_name: str, pull_number: int):
    payload = parse_form_or_json()
    merge_method = str(payload.get("merge_method", "squash")).strip().lower() or "squash"
    if merge_method not in {"merge", "squash", "rebase"}:
        return api_error("Onbekende merge-methode.", 400)

    result = github_request(
        g.auth.token,
        "PUT",
        repo_path(repo_name, f"/pulls/{pull_number}/merge"),
        payload={"merge_method": merge_method},
    )
    return api_success(result.get("message", f"Pull request #{pull_number} is gemerged."))


@app.post("/api/workflows/<repo_name>/<int:run_id>/rerun")
@require_login
def rerun_workflow(repo_name: str, run_id: int):
    github_request(
        g.auth.token,
        "POST",
        repo_path(repo_name, f"/actions/runs/{run_id}/rerun"),
    )
    return api_success(f"Workflow run {run_id} is opnieuw gestart.")


@app.post("/api/workflows/<repo_name>/<int:run_id>/cancel")
@require_login
def cancel_workflow(repo_name: str, run_id: int):
    github_request(
        g.auth.token,
        "POST",
        repo_path(repo_name, f"/actions/runs/{run_id}/cancel"),
    )
    return api_success(f"Workflow run {run_id} is geannuleerd.")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5080"))
    app.run(host="127.0.0.1", port=port, debug=False)
