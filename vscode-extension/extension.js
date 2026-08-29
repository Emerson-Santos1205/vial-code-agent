const vscode = require("vscode");

function activate(context) {
  let sessionId = context.workspaceState.get("vial.sessionId");
  const command = vscode.commands.registerCommand("vial.openChat", async () => {
    const message = await vscode.window.showInputBox({ prompt: "Ask VIAL about this workspace" });
    if (!message) return;
    try {
      const response = await fetch("http://127.0.0.1:8765/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ message, session_id: sessionId })
      });
      if (!response.ok) throw new Error((await response.json()).error || response.statusText);
      const value = await response.json();
      sessionId = value.session_id;
      await context.workspaceState.update("vial.sessionId", sessionId);
      const document = await vscode.workspace.openTextDocument({
        content: value.text || "(empty response)", language: "markdown"
      });
      await vscode.window.showTextDocument(document, { preview: true });
    } catch (error) {
      vscode.window.showErrorMessage(`VIAL web server is unavailable: ${error}`);
    }
  });
  context.subscriptions.push(command);
}

function deactivate() {}

module.exports = { activate, deactivate };
