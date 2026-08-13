from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .session import SessionStore
from .model import OpenCodeProvider


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765,
          provider: OpenCodeProvider | None = None, runtime=None) -> None:
    store = SessionStore(root / ".vial-sessions")
    executor = ThreadPoolExecutor(max_workers=2)
    jobs: dict[str, dict[str, object]] = {}

    def run_chat(job_id: str, session_id: str, message: str, request_provider: OpenCodeProvider) -> None:
        try:
            response = request_provider.chat(message, root) if request_provider is not None else None
            if response is not None and response.returncode == 0:
                store.append(session_id, "assistant", response.text)
                jobs[job_id] = {"status": "done", "session_id": session_id}
            else:
                jobs[job_id] = {
                    "status": "error", "session_id": session_id,
                    "error": (response.stderr if response is not None else "provider unavailable"),
                }
        except Exception as error:
            jobs[job_id] = {"status": "error", "session_id": session_id, "error": str(error)}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._json({"status": "ok"})
                return
            if parsed.path == "/chat":
                query = parse_qs(parsed.query)
                job_id = query.get("job_id", [""])[0]
                if job_id in jobs:
                    session_id = str(jobs[job_id].get("session_id", ""))
                    self._json({**jobs[job_id], "messages": [m.__dict__ for m in store.messages(session_id)]})
                else:
                    self.send_error(404, "job not found")
                return
            if parsed.path == "/models":
                try:
                    selected_provider = parse_qs(parsed.query).get("provider", [None])[0]
                    self._json({"models": provider.list_models(selected_provider) if provider else ""})
                except Exception as error:
                    self._json({"error": str(error)}, 500)
                return
            if parsed.path == "/org":
                if runtime is None:
                    self.send_error(404, "VIAL runtime is unavailable")
                    return
                self._json(runtime.snapshot())
                return
            self._html(APP_HTML)

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/chat":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            session_id = data.get("session_id") or store.create()
            message = str(data.get("message", ""))
            if not message:
                self.send_error(400, "message is required")
                return
            store.append(session_id, "user", message)
            if provider is not None:
                model = str(data.get("model", "")).strip()
                request_provider = provider
                if model:
                    request_provider = OpenCodeProvider(
                        model, provider.executable, provider.auto_approve, provider.agent
                    )
                job_id = uuid.uuid4().hex
                jobs[job_id] = {"status": "running", "session_id": session_id}
                executor.submit(run_chat, job_id, session_id, message, request_provider)
                self._json({"status": "running", "job_id": job_id, "session_id": session_id}, status=202)
                return
            self._json({"status": "done", "session_id": session_id, "messages": [m.__dict__ for m in store.messages(session_id)]})

        def _json(self, value: object, status: int = 200) -> None:
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: str) -> None:
            data = f"<!doctype html><html><body>{body}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()


APP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VIAL Code Agent</title>
<style>
:root{color-scheme:dark;--bg:#090909;--panel:#151515;--panel2:#1e1e1e;--muted:#858585;--text:#ededed;--blue:#65a8ff;--line:#252525}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px ui-monospace,SFMono-Regular,Consolas,monospace;min-height:100vh}
 button,input,textarea,select{font:inherit;color:inherit}button{cursor:pointer;border:0;background:none}select{background:var(--panel2);border:0;padding:5px;max-width:100%}.screen{min-height:100vh}
.landing{display:grid;place-items:center;padding:32px}.landing-inner{width:min(680px,100%);margin-top:-8vh}.wordmark{text-align:center;font-size:58px;letter-spacing:-6px;font-weight:800;color:#ddd;margin-bottom:38px}.wordmark span{color:#777}
.prompt{background:var(--panel2);border-left:3px solid var(--blue);padding:16px 20px;box-shadow:0 10px 40px #0008}.prompt textarea{display:block;width:100%;height:44px;resize:none;border:0;outline:0;background:transparent;color:var(--text)}.prompt textarea::placeholder{color:#777}
.prompt-meta{display:flex;gap:14px;color:var(--muted);margin-top:10px}.prompt-meta b{color:var(--blue)}.hint{margin:16px 0 0;color:#858585}.hint strong{color:#f0ad4e}.landing-footer{position:fixed;bottom:22px;left:24px;color:#555}
.workspace{display:grid;grid-template-columns:minmax(0,1fr) 340px;min-height:100vh}.main{display:flex;flex-direction:column;min-width:0}.messages{flex:1;padding:28px 3%;overflow:auto}.message{max-width:900px;margin:0 auto 22px;padding:16px 20px;border-left:2px solid var(--blue);background:#121212;white-space:pre-wrap;line-height:1.55}.message.assistant{border-left-color:#555;background:transparent}.composer-wrap{padding:0 3% 26px}.composer{max-width:900px;margin:auto;background:var(--panel2);border-left:3px solid var(--blue);padding:14px 20px}.composer textarea{width:100%;height:52px;resize:none;border:0;outline:0;background:transparent}.composer-row{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}.send{color:var(--blue)}.side{border-left:1px solid var(--line);background:#121212;padding:24px 18px;color:var(--muted)}.side h2{margin:0;color:#eee;font-size:15px}.side section{margin:32px 0}.side strong{display:block;color:#eee;margin-bottom:6px}.back{color:var(--blue);margin-top:40px}
.hidden{display:none!important}@media(max-width:760px){.workspace{display:block}.side{border-left:0;border-top:1px solid var(--line)}.landing-inner{margin-top:-2vh}.wordmark{font-size:46px}}
</style></head>
<body>
<section id="landing" class="screen landing"><div class="landing-inner">
<div class="wordmark"><span>v</span>ial</div>
<div class="prompt"><textarea id="first" autofocus placeholder="Ask anything...  &#34;What is the tech stack of this project?&#34;"></textarea><div class="prompt-meta"><b>Build</b><span>·</span><select id="first-model"><option>Loading models...</option></select></div></div>
<p class="hint">● <strong>Tip</strong> Start with a focused task and let VIAL select the relevant context.</p></div><div class="landing-footer">vial-code-agent</div></section>
<section id="workspace" class="screen workspace hidden"><main class="main"><div id="messages" class="messages"></div><div class="composer-wrap"><div class="composer"><textarea id="next" placeholder="Ask VIAL to inspect, explain, or change the workspace..."></textarea><div class="composer-row"><span>Enter send · Shift+Enter newline</span><button class="send" id="send">Send</button></div></div></div></main><aside class="side"><h2>New session</h2><section><strong>Model</strong><select id="model"></select></section><section><strong>Context</strong><span id="context">Selective workspace context</span></section><section><strong>VIAL</strong><span>Organization cognitive runtime</span></section><section><strong>Resource</strong><span>OpenCode execution resource</span></section><button class="back" id="back">/ new session</button></aside></section>
<script>
let sessionId=null;const landing=document.querySelector('#landing'),workspace=document.querySelector('#workspace'),messages=document.querySelector('#messages');
function selectedModel(){return document.querySelector('#model').value||document.querySelector('#first-model').value}
function openWork(text){document.querySelector('#model').value=document.querySelector('#first-model').value;landing.classList.add('hidden');workspace.classList.remove('hidden');add('user',text);send(text)}
function add(role,text){const el=document.createElement('div');el.className='message '+role;el.textContent=text;messages.appendChild(el);messages.scrollTop=messages.scrollHeight}
async function send(text){const command=text.trim();if(command.startsWith('/model ')){const wanted=command.slice(7).trim();const select=document.querySelector('#model');const option=[...select.options].find(item=>item.value===wanted);if(option){select.value=wanted;add('assistant','Model selected: '+wanted)}else add('assistant','Model not found. Use /models.');return}if(command==='/models'){const r=await fetch('/models');const data=await r.json();add('assistant',data.models||'No models available');return}if(command==='/status'){add('assistant','Session: '+(sessionId||'new')+'\nModel: '+selectedModel());return}if(command==='/clear'){location.reload();return}const button=document.querySelector('#send');button.disabled=true;button.textContent='Sending...';try{const r=await fetch('/chat',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({session_id:sessionId,message:text,model:selectedModel()})});const data=await r.json();if(!r.ok)throw new Error(data.error||'request failed');sessionId=data.session_id;if(data.job_id){await poll(data.job_id)}else{const last=data.messages[data.messages.length-1];if(last&&last.role==='assistant')add('assistant',last.content)}}catch(e){add('assistant','VIAL web server error: '+e)}finally{button.disabled=false;button.textContent='Send'}}
async function poll(jobId){for(let i=0;i<360;i++){await new Promise(r=>setTimeout(r,500));const r=await fetch('/chat?job_id='+encodeURIComponent(jobId));const data=await r.json();if(data.status==='done'){const last=data.messages[data.messages.length-1];if(last&&last.role==='assistant')add('assistant',last.content);return}if(data.status==='error')throw new Error(data.error||'model request failed')}}
document.querySelector('#first').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();const v=e.target.value.trim();if(v)openWork(v)}});
document.querySelector('#send').onclick=()=>{const el=document.querySelector('#next'),v=el.value.trim();if(v){el.value='';add('user',v);send(v)}};
document.querySelector('#next').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();document.querySelector('#send').click()}});
document.querySelector('#back').onclick=()=>location.reload();
async function loadModels(){try{const r=await fetch('/models');const data=await r.json();const models=(data.models||'').split(/\r?\n/).filter(Boolean);for(const id of ['first-model','model']){const el=document.querySelector('#'+id);el.innerHTML='';models.forEach(model=>{const option=document.createElement('option');option.value=model;option.textContent=model;el.appendChild(option)});if(models.length)el.value=models.find(m=>m.includes('gpt-5.6-luna-fast'))||models[0]}}catch(e){for(const id of ['first-model','model'])document.querySelector('#'+id).innerHTML='<option value="openai/gpt-5.6-luna-fast">openai/gpt-5.6-luna-fast</option>'}}
loadModels();
</script></body></html>"""
