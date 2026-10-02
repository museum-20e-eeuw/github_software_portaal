document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("new-project-details");
    if (!form) return;

    const name = document.getElementById("new-project-name");
    const path = document.getElementById("new-project-path");
    const workspaceRoot = form.dataset.workspaceRoot;

    name.addEventListener("input", () => {
        path.textContent = `${workspaceRoot}\\${name.value || "projectnaam"}`;
    });
});