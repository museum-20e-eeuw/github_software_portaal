function showToast(message, isError) {
    const host = document.getElementById("toast-host");
    if (!host) {
        return;
    }

    const toast = document.createElement("div");
    toast.className = `toast${isError ? " error" : ""}`;
    toast.textContent = message;
    host.appendChild(toast);

    window.setTimeout(() => {
        toast.remove();
    }, 3500);
}

async function submitApiForm(form) {
    const formData = new FormData(form);
    const response = await fetch(form.action, {
        method: form.method || "POST",
        body: formData,
        headers: { Accept: "application/json" },
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
        throw new Error(payload.message || "Actie mislukt.");
    }
    showToast(payload.message, false);
    if (payload.url) {
        window.setTimeout(() => {
            window.open(payload.url, "_blank", "noopener");
        }, 300);
    }
    if (form.dataset.reload === "true") {
        window.setTimeout(() => {
            window.location.reload();
        }, 700);
    }
}

function bindApiForms() {
    const apiForms = document.querySelectorAll("form[data-api-form='true']");
    for (const form of apiForms) {
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            try {
                await submitApiForm(form);
            } catch (error) {
                showToast(error.message || "Actie mislukt.", true);
            }
        });
    }
}

document.addEventListener("DOMContentLoaded", () => {
    bindApiForms();
    bindProjectDetail();
    bindFolderPicker();
});

function bindFolderPicker() {
    const browseBtn = document.getElementById("browse-folder-btn");
    const workspaceInput = document.getElementById("workspace-root-input");
    const backdrop = document.getElementById("folder-modal-backdrop");
    if (!browseBtn || !workspaceInput || !backdrop) {
        return;
    }

    const currentPathLabel = document.getElementById("folder-current-path");
    const folderList = document.getElementById("folder-list");
    const upBtn = document.getElementById("folder-up-btn");
    const cancelBtn = document.getElementById("folder-cancel-btn");
    const selectBtn = document.getElementById("folder-select-btn");

    let activePath = "";
    let parentPath = null;

    async function loadFolder(path) {
        const url = path ? `/api/browse-folders?path=${encodeURIComponent(path)}` : "/api/browse-folders";
        const response = await fetch(url, { headers: { Accept: "application/json" } });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
            showToast(payload.message || "Kon map niet laden.", true);
            return;
        }

        activePath = payload.current_path || "";
        parentPath = payload.parent_path;
        currentPathLabel.textContent = activePath || "Schijven";
        upBtn.disabled = !activePath;

        folderList.innerHTML = "";
        if (!payload.folders.length) {
            const empty = document.createElement("div");
            empty.className = "folder-list-empty";
            empty.textContent = "Geen submappen gevonden.";
            folderList.appendChild(empty);
            return;
        }

        for (const folder of payload.folders) {
            const item = document.createElement("button");
            item.type = "button";
            item.className = "folder-list-item";
            item.textContent = `📁 ${folder.name}`;
            item.addEventListener("click", () => loadFolder(folder.path));
            folderList.appendChild(item);
        }
    }

    browseBtn.addEventListener("click", () => {
        backdrop.hidden = false;
        loadFolder(workspaceInput.value.trim());
    });

    upBtn.addEventListener("click", () => {
        if (parentPath) {
            loadFolder(parentPath);
        } else {
            loadFolder("");
        }
    });

    cancelBtn.addEventListener("click", () => {
        backdrop.hidden = true;
    });

    selectBtn.addEventListener("click", () => {
        if (activePath) {
            workspaceInput.value = activePath;
        }
        backdrop.hidden = true;
    });
}

function bindProjectDetail() {
    const root = document.getElementById("project-root");
    if (!root) {
        return;
    }

    const repoName = root.dataset.repo;
    const cloneBtn = document.getElementById("clone-project-btn");
    const updateBtn = document.getElementById("update-project-btn");
    const fileSelect = document.getElementById("file-select");
    const openFileBtn = document.getElementById("open-file-btn");
    const openFileStatus = document.getElementById("open-file-status");
    const commitBackdrop = document.getElementById("commit-modal-backdrop");
    const commitChangedFiles = document.getElementById("commit-changed-files");
    const commitAuthor = document.getElementById("commit-author");
    const commitVersion = document.getElementById("commit-version");
    const commitSummary = document.getElementById("commit-summary");
    const commitCancelBtn = document.getElementById("commit-cancel-btn");
    const commitConfirmBtn = document.getElementById("commit-confirm-btn");

    async function postJson(url, body) {
        const response = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json", Accept: "application/json" },
            body: JSON.stringify(body || {}),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
            throw new Error(payload.message || "Actie mislukt.");
        }
        return payload;
    }

    cloneBtn?.addEventListener("click", async () => {
        cloneBtn.disabled = true;
        try {
            const payload = await postJson(`/api/projects/${encodeURIComponent(repoName)}/clone`);
            showToast(payload.message, false);
            window.setTimeout(() => window.location.reload(), 600);
        } catch (error) {
            showToast(error.message, true);
            cloneBtn.disabled = false;
        }
    });

    updateBtn?.addEventListener("click", async () => {
        updateBtn.disabled = true;
        try {
            const payload = await postJson(`/api/projects/${encodeURIComponent(repoName)}/update`);
            showToast(payload.message, false);
            window.setTimeout(() => window.location.reload(), 600);
        } catch (error) {
            showToast(error.message, true);
            updateBtn.disabled = false;
        }
    });

    let pendingSessionId = null;

    async function pollOpenFileSession(sessionId) {
        const poll = async () => {
            const response = await fetch(`/api/open-file-sessions/${sessionId}`, {
                headers: { Accept: "application/json" },
            });
            const payload = await response.json();
            if (!payload.ok) {
                return;
            }
            if (!payload.closed) {
                window.setTimeout(poll, 1500);
                return;
            }
            if (payload.changed_files && payload.changed_files.length) {
                openCommitModal(payload.changed_files);
            } else {
                showToast("Geen wijzigingen gevonden na sluiten van het bestand.", false);
            }
        };
        window.setTimeout(poll, 1500);
    }

    openFileBtn?.addEventListener("click", async () => {
        if (!fileSelect || !fileSelect.value) {
            return;
        }
        openFileBtn.disabled = true;
        if (openFileStatus) {
            openFileStatus.textContent = "Bestand wordt geopend...";
        }
        try {
            const payload = await postJson(`/api/projects/${encodeURIComponent(repoName)}/open-file`, {
                file_path: fileSelect.value,
            });
            showToast(payload.message, false);
            pendingSessionId = payload.session_id;
            if (openFileStatus) {
                openFileStatus.textContent = "Wachten tot je het bestand sluit in de tool...";
            }
            pollOpenFileSession(pendingSessionId);
        } catch (error) {
            showToast(error.message, true);
        } finally {
            openFileBtn.disabled = false;
        }
    });

    function openCommitModal(changedFiles) {
        if (!commitBackdrop) {
            return;
        }
        commitChangedFiles.textContent = `Gewijzigde bestanden: ${changedFiles.join(", ")}`;
        commitAuthor.value = "";
        commitVersion.value = "";
        commitSummary.value = "";
        commitBackdrop.hidden = false;
        if (openFileStatus) {
            openFileStatus.textContent = "Wijzigingen gevonden. Vul de gegevens in om te syncen.";
        }
    }

    commitCancelBtn?.addEventListener("click", () => {
        commitBackdrop.hidden = true;
    });

    commitConfirmBtn?.addEventListener("click", async () => {
        if (!commitAuthor.value.trim() || !commitVersion.value.trim() || !commitSummary.value.trim()) {
            showToast("Vul naam, versienummer en wijziging in.", true);
            return;
        }
        commitConfirmBtn.disabled = true;
        try {
            const payload = await postJson(`/api/projects/${encodeURIComponent(repoName)}/commit`, {
                author_name: commitAuthor.value.trim(),
                version: commitVersion.value.trim(),
                summary: commitSummary.value.trim(),
            });
            showToast(payload.message, false);
            commitBackdrop.hidden = true;
            window.setTimeout(() => window.location.reload(), 700);
        } catch (error) {
            showToast(error.message, true);
        } finally {
            commitConfirmBtn.disabled = false;
        }
    });
}
