const vscode = require("vscode");

function activate(context) {
  const command = vscode.commands.registerCommand("vial.openChat", async () => {
    const message = await vscode.window.showInputBox({ prompt: "Ask VIAL about this workspace" });
    if (!message) return;
    try {
      const response = await fetch("http://127.0.0.1:8765/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ message })
      });
      const value = await response.json();
      vscode.window.showInformationMessage(`VIAL session ${value.session_id} updated`);
    } catch (error) {
      vscode.window.showErrorMessage(`VIAL web server is unavailable: ${error}`);
    }
  });
  context.subscriptions.push(command);
}

function deactivate() {}

module.exports = { activate, deactivate };
