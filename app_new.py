from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable
from urllib.parse import quote

from flask import Blueprint, flash, g, redirect, render_template, request, url_for


class ProjectCreationError(Exception):
    pass


def run_git(token: str, args: list[str], cwd: str, *, identity: str | None = None) -> None:
    command = ["git"]
    if token:
        credential = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        command.extend(["-c", f"http.extraheader=AUTHORIZATION: basic {credential}"])
    if identity:
        command.extend(["-c", f"user.name={identity}", "-c", f"user.email={identity}@users.noreply.github.com"])
    command.extend(args)
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectCreationError(f"Git kon niet worden gestart: {exc}") from exc
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise ProjectCreationError(details or "Een Git-opdracht is mislukt.")


def create_starter_files(directory: str, name: str, starter_type: str) -> None:
    readme = f"# {name}\n\nNieuw softwareproject aangemaakt via Software Portaal.\n"
    with open(os.path.join(directory, "README.md"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(readme)

    if starter_type == "python":
        with open(os.path.join(directory, "main.py"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write('def main():\n    print("Hello, world!")\n\n\nif __name__ == "__main__":\n    main()\n')
        return

    sketch_name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not sketch_name or sketch_name[0].isdigit():
        sketch_name = f"Project_{sketch_name}"
    sketch = (
        "void setup() {\n"
        "  Serial.begin(9600);\n"
        '  Serial.println("Hello, world!");\n'
        "}\n\n"
        "void loop() {\n"
        "}\n"
    )
    with open(os.path.join(directory, f"{sketch_name}.ino"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(sketch)


def create_blueprint(
    *,
    require_login: Callable,
    get_org_repositories: Callable,
    github_request: Callable,
    current_org: Callable,
    workspace_root: Callable,
    github_api_error: type[Exception],
) -> Blueprint:
    blueprint = Blueprint("app_new", __name__)

    def load_repositories() -> list[dict[str, Any]]:
        return get_org_repositories(g.auth.token)

    def render_workflow(
        step: str,
        repositories: list[dict[str, Any]] | None = None,
        values: dict[str, str] | None = None,
    ):
        return render_template(
            "app_new.html",
            active_nav="dashboard",
            repositories=repositories or [],
            workspace_root=workspace_root() if step == "details" else "",
            step=step,
            values=values or {},
        )

    @blueprint.get("/projects/new")
    @require_login
    def create_project():
        return render_workflow("choice")

    @blueprint.post("/projects/new/choose")
    @require_login
    def choose_creation_mode():
        creation_mode = request.form.get("creation_mode", "").strip()
        if creation_mode == "empty":
            return render_workflow("starter", values={"creation_mode": "empty"})
        if creation_mode == "copy":
            try:
                repositories = load_repositories()
            except github_api_error as exc:
                flash(f"Projecten van GitHub konden niet worden opgehaald: {exc}", "error")
                repositories = []
            return render_workflow("source", repositories)
        flash("Kies of je een nieuw project of een kopie wilt starten.", "error")
        return render_workflow("choice")

    @blueprint.route("/projects/new/starter", methods=["GET", "POST"])
    @require_login
    def choose_starter_type():
        if request.method == "GET":
            return render_workflow(
                "starter",
                values={
                    "creation_mode": "empty",
                    "starter_type": request.args.get("starter_type", "python"),
                },
            )
        starter_type = request.form.get("starter_type", "").strip()
        if starter_type not in {"python", "arduino"}:
            flash("Kies Python of Arduino als startproject.", "error")
            return render_workflow("starter", values={"creation_mode": "empty"})
        return render_workflow(
            "details",
            values={"creation_mode": "empty", "starter_type": starter_type},
        )

    @blueprint.route("/projects/new/source", methods=["GET", "POST"])
    @require_login
    def choose_source_project():
        try:
            repositories = load_repositories()
        except github_api_error as exc:
            flash(f"Projecten van GitHub konden niet worden opgehaald: {exc}", "error")
            repositories = []
        if request.method == "GET":
            return render_workflow(
                "source",
                repositories,
                values={"creation_mode": "copy", "source_repo": request.args.get("source_repo", "")},
            )
        source_repo = request.form.get("source_repo", "").strip()
        if not any(repo.get("name") == source_repo for repo in repositories):
            flash("Kies een bestaand softwareproject uit de lijst.", "error")
            return render_workflow("source", repositories)
        return render_workflow(
            "details",
            values={"creation_mode": "copy", "source_repo": source_repo},
        )

    @blueprint.post("/projects/new")
    @require_login
    def create_project_from_form():
        try:
            repositories = load_repositories()
        except github_api_error as exc:
            flash(f"Projecten van GitHub konden niet worden opgehaald: {exc}", "error")
            repositories = []

        values = {
            "name": request.form.get("name", "").strip(),
            "creation_mode": request.form.get("creation_mode", "empty").strip(),
            "source_repo": request.form.get("source_repo", "").strip(),
            "starter_type": request.form.get("starter_type", "python").strip(),
            "description": request.form.get("description", "").strip(),
            "visibility": request.form.get("visibility", "private").strip(),
        }
        name = values["name"]
        creation_mode = values["creation_mode"]
        starter_type = values["starter_type"]
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name) or name in {".", ".."}:
            flash("Gebruik een projectnaam van 1 tot 100 letters, cijfers, punten, streepjes of underscores.", "error")
            return render_workflow("details", values=values)
        if creation_mode not in {"empty", "copy"}:
            flash("Kies een geldige startmethode.", "error")
            return render_workflow("choice")
        if creation_mode == "empty" and starter_type not in {"python", "arduino"}:
            flash("Kies Python of Arduino als startproject.", "error")
            return render_workflow("starter", values=values)
        if values["visibility"] not in {"private", "public"}:
            flash("Kies een geldige zichtbaarheid voor de GitHub-repository.", "error")
            return render_workflow("details", values=values)

        source = next((repo for repo in repositories if repo.get("name") == values["source_repo"]), None)
        if creation_mode == "copy" and source is None:
            flash("Kies een bestaand softwareproject om te kopieren.", "error")
            return render_workflow("source", repositories)
        if creation_mode == "copy" and name.casefold() == values["source_repo"].casefold():
            flash("Geef de kopie een andere naam dan het bronproject.", "error")
            return render_workflow("details", values=values)

        root = workspace_root()
        target = os.path.abspath(os.path.join(root, name))
        if os.path.commonpath([os.path.abspath(root), target]) != os.path.abspath(root):
            flash("De projectmap moet binnen de ingestelde werkmap staan.", "error")
            return render_workflow("details", values=values)
        if os.path.lexists(target):
            flash(f"De lokale map bestaat al: {target}", "error")
            return render_workflow("details", values=values)

        staging_root = tempfile.mkdtemp(prefix=".new-project-", dir=root)
        staging_project = os.path.join(staging_root, name)
        github_created = False
        local_project_ready = False
        try:
            if creation_mode == "copy":
                source_url = str(source.get("clone_url") or "")
                if not source_url:
                    raise ProjectCreationError("De GitHub-bronrepository heeft geen clone-URL.")
                run_git(g.auth.token, ["clone", "--depth", "1", source_url, staging_project], cwd=root)
                shutil.rmtree(os.path.join(staging_project, ".git"))
            else:
                os.makedirs(staging_project)
                create_starter_files(staging_project, name, starter_type)

            run_git("", ["init", "--initial-branch=main"], cwd=staging_project)
            run_git("", ["add", "--force", "--all"], cwd=staging_project)
            initial_commit = "Initial Hello World project" if creation_mode == "empty" else f"Initial copy of {source['name']}"
            run_git("", ["commit", "-m", initial_commit], cwd=staging_project, identity=g.auth.username)

            organization = current_org()
            created_repo = github_request(
                g.auth.token,
                "POST",
                f"/orgs/{quote(organization, safe='')}/repos",
                payload={
                    "name": name,
                    "description": values["description"],
                    "private": values["visibility"] == "private",
                    "auto_init": False,
                },
            )
            github_created = True
            os.rename(staging_project, target)
            local_project_ready = True
            remote_url = str(created_repo.get("clone_url") or f"https://github.com/{organization}/{name}.git")
            run_git(g.auth.token, ["remote", "add", "origin", remote_url], cwd=target)
            run_git(g.auth.token, ["push", "--set-upstream", "origin", "HEAD:main"], cwd=target)
        except github_api_error as exc:
            flash(f"GitHub kon de repository niet aanmaken: {exc}", "error")
            return render_workflow("details", values=values)
        except ProjectCreationError as exc:
            if github_created and local_project_ready:
                flash(
                    f"GitHub-repository {name} is aangemaakt, maar pushen is mislukt. "
                    f"De lokale bestanden staan in {target}. Fout: {exc}",
                    "error",
                )
            elif github_created:
                flash(f"GitHub-repository {name} is aangemaakt, maar de lokale projectmap kon niet worden klaargezet: {exc}", "error")
            else:
                flash(f"Project is niet volledig aangemaakt: {exc}", "error")
            return render_workflow("details", values=values)
        except OSError as exc:
            if github_created:
                flash(f"GitHub-repository {name} is aangemaakt, maar de lokale projectmap kon niet worden klaargezet: {exc}", "error")
            else:
                flash(f"De lokale projectmap kon niet worden klaargezet: {exc}", "error")
            return render_workflow("details", values=values)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

        flash(f"Project {name} is lokaal aangemaakt en geregistreerd op GitHub.", "success")
        return redirect(url_for("dashboard"))

    return blueprint