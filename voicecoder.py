#!/usr/bin/env python3
"""
voicecoder - hands-free voice companion for Codex CLI and Claude Code.

  * Speaks out loud when the agent finishes, including what it wrote.
  * Keeps the laptop awake while you work.
  * Listens for your next spoken command and sends it to the agent.

Modes
  python voicecoder.py loop --agent codex  [--cwd PATH]   full voice loop (talk -> agent works -> it speaks -> talk ...)
  python voicecoder.py loop --agent claude [--cwd PATH]
  python voicecoder.py awake                              just keep the laptop awake until Ctrl+C
  python voicecoder.py hooks                              print config to make your normal Codex/Claude sessions speak
  python voicecoder.py test                               test speaker + microphone
  python voicecoder.py say "hello"                        speak some text

Approvals (on by default in `loop`): when the agent wants to run a command or change files,
voicecoder reads the request out loud and waits for "yes", "no", "always" or "details".
Unclear answers count as NO. `--no-approvals` restores the old auto behaviour.

Hook entry points (called by the agents, not by you)
  python voicecoder.py codex-notify '<json>'              Codex `notify` program
  python voicecoder.py claude-stop  < json-on-stdin        Claude Code `Stop` hook
  python voicecoder.py claude-notify < json-on-stdin       Claude Code `Notification` hook (announces approvals)
  python voicecoder.py mcp-approver                        Claude --permission-prompt-tool helper (started by the loop)
"""
import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

SYSTEM = platform.system()  # "Windows", "Darwin", "Linux"
IN_LOOP_ENV = "VOICECODER_LOOP"  # set while the loop runs an agent, so hooks don't speak twice
MAX_CHARS = int(os.environ.get("VOICECODER_MAX_CHARS", "700"))
RATE = int(os.environ.get("VOICECODER_RATE", "0"))  # Windows: -10..10, macOS: words/min (0 = default)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HERE = os.path.abspath(__file__)

YES_WORDS = {"yes", "yeah", "yep", "yup", "send", "go", "ok", "okay", "sure", "correct",
             "haan", "ha", "han", "theek", "chalo", "do", "proceed", "confirm", "right"}
STOP_WORDS = {"stop", "exit", "quit", "goodbye", "bye", "stop listening", "band karo", "shut down"}
REPEAT_WORDS = {"repeat", "repeat that", "say that again", "again", "what did you say"}
NEW_WORDS = {"new session", "new task", "start over", "fresh start", "reset"}
TYPE_WORDS = {"keyboard", "let me type", "type mode", "typing"}

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------- speech out

def clean_for_speech(text, limit=MAX_CHARS):
    """Turn markdown-heavy agent output into something pleasant to listen to."""
    if not text:
        return ""
    t = re.sub(r"```.*?```", " (code block, see screen) ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", " a link ", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"[*_~|>#]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        cut = t[:limit]
        dot = cut.rfind(". ")
        if dot > limit * 0.5:
            cut = cut[: dot + 1]
        t = cut + " ... The rest is on screen."
    return t


def speak(text):
    """Blocking text-to-speech using the OS's built-in voice. No extra installs."""
    text = (text or "").strip()
    if not text:
        return
    print(f"\n[voice] {text}\n", flush=True)
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="voicecoder_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if SYSTEM == "Windows":
            p = path.replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Speech;"
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                f"$s.Rate={max(-10, min(10, RATE))};"
                f"$s.Speak((Get-Content -Raw -Encoding UTF8 -LiteralPath '{p}'))"
            )
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           check=False, creationflags=NO_WINDOW)
        elif SYSTEM == "Darwin":
            cmd = ["say", "-f", path] + (["-r", str(RATE)] if RATE > 0 else [])
            subprocess.run(cmd, check=False)
        else:
            for exe in ("espeak-ng", "espeak"):
                if shutil.which(exe):
                    subprocess.run([exe, "-f", path], check=False)
                    break
            else:
                if shutil.which("spd-say"):
                    subprocess.run(["spd-say", "-w", text], check=False)
                else:
                    print("(no speech engine found - install espeak-ng)", file=sys.stderr)
    except Exception as e:  # never crash because of audio
        print(f"(speech failed: {e})", file=sys.stderr)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def speak_detached(text):
    """Speak in a background process so a hook returns immediately and never blocks the agent."""
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if SYSTEM == "Windows":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | NO_WINDOW  # DETACHED | NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, HERE, "say", text], **kwargs)


# --------------------------------------------------------------------------- keep awake

class KeepAwake:
    """Stops the laptop from idle-sleeping while active. (Closing the lid is a separate OS setting.)"""

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __enter__(self):
        self.proc = None
        try:
            if SYSTEM == "Windows":
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
            elif SYSTEM == "Darwin" and shutil.which("caffeinate"):
                self.proc = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
            elif shutil.which("systemd-inhibit"):
                self.proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=idle:sleep", "--who=voicecoder",
                     "--why=Waiting for coding agent", "--mode=block", "sleep", "infinity"])
            else:
                print("(could not find a way to block sleep on this system)")
                return self
            print("[awake] Sleep is blocked while voicecoder runs.")
        except Exception as e:
            print(f"(keep-awake failed: {e})")
        return self

    def __exit__(self, *exc):
        try:
            if SYSTEM == "Windows":
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
            if self.proc:
                self.proc.terminate()
        except Exception:
            pass
        return False


# --------------------------------------------------------------------------- speech in

class SoundDeviceMic:
    """Microphone capture with `sounddevice` (has wheels for every Python, unlike pyaudio)."""
    RATE, BLOCK = 16000, 1600  # 0.1 s blocks

    def __init__(self, sr, pause):
        import numpy as np
        import sounddevice as sd
        self.np, self.sd, self.sr, self.pause = np, sd, sr, pause
        print("[mic] Calibrating to room noise, stay quiet for a second...")
        rec = sd.rec(int(self.RATE * 1.2), samplerate=self.RATE, channels=1, dtype="int16")
        sd.wait()
        noise = self._rms(rec)
        self.threshold = max(noise * 3.0, 250.0)

    def _rms(self, a):
        a = a.astype(self.np.float64)
        return float(self.np.sqrt(self.np.mean(a * a))) if a.size else 0.0

    def listen(self, max_seconds=120):
        from collections import deque
        pre = deque(maxlen=5)
        frames, started, silence, voiced = [], False, 0.0, 0.0
        with self.sd.InputStream(samplerate=self.RATE, channels=1, dtype="int16",
                                 blocksize=self.BLOCK) as stream:
            while True:
                block, _ = stream.read(self.BLOCK)
                loud = self._rms(block) > self.threshold
                if not started:
                    pre.append(block.copy())
                    if loud:
                        started, frames = True, list(pre)
                    continue
                frames.append(block.copy())
                if loud:
                    silence, voiced = 0.0, voiced + 0.1
                else:
                    silence += 0.1
                if silence >= self.pause:
                    if voiced < 0.3:  # just a click or cough, keep waiting
                        frames, started, silence, voiced = [], False, 0.0, 0.0
                        pre.clear()
                        continue
                    break
                if len(frames) * 0.1 >= max_seconds:
                    break
        data = b"".join(f.tobytes() for f in frames)
        return self.sr.AudioData(data, self.RATE, 2)


class Ears:
    def __init__(self, lang="en-IN", typed=False, pause=1.5):
        self.lang = lang
        self.voice = False
        if typed:
            return
        try:
            import speech_recognition as sr
            self.sr = sr
            self.r = sr.Recognizer()
        except Exception as e:
            print(f"[mic] Voice input unavailable ({e}). Falling back to typing.\n"
                  "      Install with:  pip install SpeechRecognition sounddevice numpy")
            return
        try:  # preferred: sounddevice (works on new Python versions)
            self.mic = SoundDeviceMic(sr, pause)
            self.voice = True
            return
        except Exception as e:
            first_error = e
        try:  # fallback: SpeechRecognition's own pyaudio microphone
            self.r.pause_threshold = pause
            self.r.dynamic_energy_threshold = True
            pa_mic = sr.Microphone()
            with pa_mic as src:
                self.r.adjust_for_ambient_noise(src, duration=1.2)
            self.mic = pa_mic
            self.voice = True
        except Exception:
            print(f"[mic] No usable microphone ({first_error}). Falling back to typing.\n"
                  "      Install with:  pip install sounddevice numpy")

    def _capture(self):
        if isinstance(self.mic, SoundDeviceMic):
            return self.mic.listen()
        with self.mic as src:
            return self.r.listen(src, timeout=None, phrase_time_limit=120)

    def hear(self):
        if not self.voice:
            return input("You> ").strip()
        while True:
            print("[listening...] (speak now, Ctrl+C to quit)", flush=True)
            audio = self._capture()
            try:
                text = self.r.recognize_google(audio, language=self.lang).strip()
                if text:
                    print(f"You said> {text}")
                    return text
            except self.sr.UnknownValueError:
                continue  # noise / mumble, keep waiting
            except self.sr.RequestError as e:
                print(f"[mic] Speech service error: {e}")
                speak("I can't reach the speech service. Please type your command.")
                return input("You> ").strip()


def _norm(s):
    return re.sub(r"[^\w\s]", "", (s or "").lower()).strip()


# --------------------------------------------------------------------------- agents

DEFAULT_ARGS = {
    "codex": "--sandbox workspace-write",
    "claude": "--permission-mode acceptEdits",
}


def _find(exe):
    path = shutil.which(exe)
    if not path:  # native installers put the binary here; PATH may not be refreshed yet
        for cand in (os.path.join(os.path.expanduser("~"), ".local", "bin", exe + (".exe" if SYSTEM == "Windows" else "")),):
            if os.path.isfile(cand):
                path = cand
    if not path:
        raise SystemExit(f"\n'{exe}' is not installed on this computer yet."
                         + ("\nDouble-click '0 - Install Claude Code.bat' first, then '4 - Log in to Claude.bat'." if exe == "claude"
                            else "\nInstall Codex first:  npm install -g @openai/codex   then run  codex  once to sign in."))
    return path


def run_codex(prompt, cwd, session, extra):
    cmd = [_find("codex"), "exec", "--json", *extra]
    if session:
        cmd += ["resume", session]
    # prompt goes in on stdin (no prompt argument) -> safe with quotes/& etc. on Windows too
    return _stream(cmd, prompt, cwd, _codex_event)


def run_claude(prompt, cwd, session, extra):
    cmd = [_find("claude"), "-p", "--output-format", "stream-json", "--verbose", *extra]
    if session:
        cmd += ["--resume", session]
    return _stream(cmd, prompt, cwd, _claude_event)


def _codex_event(ev, state):
    t = ev.get("type")
    if t == "thread.started":
        state["session"] = ev.get("thread_id") or state["session"]
    elif t in ("item.started", "item.completed"):
        item = ev.get("item") or {}
        it = item.get("type")
        if t == "item.completed" and it == "agent_message":
            state["message"] = item.get("text") or state["message"]
            print(f"\n{state['message']}\n")
        elif t == "item.started" and it == "command_execution":
            print(f"  $ {item.get('command', '')}")
        elif t == "item.completed" and it == "file_change":
            files = [c.get("path", "?") for c in item.get("changes", []) if isinstance(c, dict)]
            print(f"  ~ edited {', '.join(files) or 'files'}")
    elif t in ("turn.failed", "error"):
        err = ev.get("error") or ev.get("message") or ev
        state["error"] = err.get("message") if isinstance(err, dict) else str(err)
        print(f"  ! {state['error']}")


def _claude_event(ev, state):
    t = ev.get("type")
    if ev.get("session_id"):
        state["session"] = ev["session_id"]
    if t == "assistant":
        for block in (ev.get("message") or {}).get("content", []) or []:
            if block.get("type") == "text" and block.get("text"):
                print(f"\n{block['text']}\n")
            elif block.get("type") == "tool_use":
                inp = block.get("input") or {}
                detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                print(f"  > {block.get('name')} {detail}")
    elif t == "result":
        state["message"] = ev.get("result") or state["message"]
        if ev.get("is_error"):
            state["error"] = ev.get("result") or "error"


def _stream(cmd, prompt, cwd, handler):
    env = dict(os.environ, **{IN_LOOP_ENV: "1"})
    state = {"session": None, "message": "", "error": None}
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=None)
    proc.stdin.write(prompt.encode("utf-8"))
    proc.stdin.close()
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            handler(json.loads(line), state)
        except (json.JSONDecodeError, AttributeError):
            print(line)
    proc.wait()
    return proc.returncode, state


# --------------------------------------------------------------------------- approvals

DESK_ENV = "VOICECODER_DESK"  # host:port:token of the running loop's approval desk
NO_WORDS = {"no", "nope", "deny", "reject", "dont", "don't", "cancel", "skip", "nahi", "nako", "na", "decline"}
ALLOW_WORDS = YES_WORDS | {"allow", "approve", "approved", "accept", "fine"}
ALWAYS_WORDS = {"always", "everything"}
DETAIL_WORDS = {"details", "detail", "explain", "what", "full", "read"}
RISKY = re.compile(r"\b(rm|del|rmdir|rd|erase|format|mkfs|dd|shutdown|reboot|curl|wget|"
                   r"Invoke-WebRequest|iwr|git\s+push|git\s+reset|git\s+clean|npm\s+publish|"
                   r"Remove-Item|chmod|chown|sudo|reg)\b", re.I)


def describe_claude_request(tool, inp):
    """Plain-language one-liner + full detail for a Claude Code permission request."""
    inp = inp if isinstance(inp, dict) else {}
    detail = json.dumps(inp, indent=2, ensure_ascii=False)[:2000]
    if tool == "Bash":
        cmd = str(inp.get("command", ""))
        why = f" ({inp['description']})" if inp.get("description") else ""
        return f"run the command: {cmd}{why}", detail, "Bash:" + (cmd.split() or [""])[0], cmd
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or "a file"
        verb = "create or overwrite" if tool == "Write" else "edit"
        return f"{verb} the file {os.path.basename(str(path))}", detail, tool, ""
    if tool == "WebFetch":
        url = str(inp.get("url", ""))
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        return f"open the web page {host}", detail, "WebFetch:" + host, ""
    if tool == "WebSearch":
        return f"search the web for {inp.get('query', '')}", detail, "WebSearch", ""
    return f"use the tool {tool}", detail, tool, ""


class ApprovalDesk:
    """Asks you (by voice or keyboard) whether the agent may do something.

    The loop owns the microphone, so approvals from other processes (Claude's permission
    helper) are sent here over a localhost socket protected by a random token.
    """

    def __init__(self, ears, agent_name, listen=True):
        import threading
        self.ears, self.name = ears, agent_name
        self.always = set()
        self.lock = threading.Lock()
        self.port = self.token = None
        if listen:
            import secrets
            import socket
            self.token = secrets.token_hex(16)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.bind(("127.0.0.1", 0))
            self.sock.listen(4)
            self.port = self.sock.getsockname()[1]
            threading.Thread(target=self._serve, daemon=True).start()

    @property
    def address(self):
        return f"127.0.0.1:{self.port}:{self.token}"

    def _serve(self):
        import threading
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            try:
                f = conn.makefile("rwb")
                req = json.loads(f.readline().decode("utf-8") or "{}")
                if req.get("token") != self.token:
                    return
                ok, always = self.decide(req.get("summary", "do something"), req.get("detail", ""),
                                         req.get("key", ""), req.get("risky", ""))
                f.write((json.dumps({"approve": ok, "always": always}) + "\n").encode("utf-8"))
                f.flush()
            except Exception as e:
                print(f"(approval error: {e})")

    def decide(self, summary, detail="", key="", risky_text=""):
        """Returns (approved, always). Anything unclear counts as NO."""
        with self.lock:
            if key and key in self.always:
                print(f"\n[approval] auto-allowed (you said 'always'): {summary}")
                return True, True
            print("\n" + "=" * 60 + f"\n[APPROVAL NEEDED] {self.name} wants to {summary}")
            if detail:
                print(detail)
            print("Answer: yes / no / always / details" + "\n" + "=" * 60)
            risky = bool(risky_text and RISKY.search(risky_text))
            if self.ears.voice:
                warn = " Careful, this one can delete or change things outside your project." if risky else ""
                speak(f"{self.name} wants your permission to {clean_for_speech(summary, 300)}.{warn} "
                      "Allow it? Say yes, no, or always.")
                for attempt in range(3):
                    try:
                        words = set(_norm(self.ears.hear()).split())
                    except (KeyboardInterrupt, EOFError):
                        words = set()
                    if words & NO_WORDS:
                        speak("Okay, denied.")
                        return False, False
                    if words & ALWAYS_WORDS and not risky:
                        if key:
                            self.always.add(key)
                        speak("Allowed, and I won't ask again for this in this session.")
                        return True, True
                    if words & ALLOW_WORDS:
                        speak("Allowed.")
                        return True, False
                    if words & DETAIL_WORDS:
                        spoken = re.sub(r'[{}\[\]",]+', " ", detail or summary).replace(":", ",")
                        speak(clean_for_speech(spoken, 500) + " ... Allow it? Yes or no.")
                        continue
                    if attempt < 2:
                        speak("Sorry, I didn't catch that. Please say yes or no.")
                speak("I couldn't understand, so I denied it to be safe.")
                return False, False
            ans = input("Allow? [y]es / [n]o / [a]lways : ").strip().lower()
            if ans.startswith("a") and not risky:
                if key:
                    self.always.add(key)
                return True, True
            return ans.startswith("y"), False


def ask_desk(address, summary, detail="", key="", risky=""):
    """Client side (used by the Claude permission helper). Unreachable desk -> deny."""
    import socket
    try:
        host, port, token = address.rsplit(":", 2)
        with socket.create_connection((host, int(port)), timeout=10) as s:
            s.settimeout(None)  # the human may take a while
            f = s.makefile("rwb")
            f.write((json.dumps({"token": token, "summary": summary, "detail": detail,
                                 "key": key, "risky": risky}) + "\n").encode("utf-8"))
            f.flush()
            resp = json.loads(f.readline().decode("utf-8") or "{}")
            return bool(resp.get("approve")), bool(resp.get("always"))
    except Exception as e:
        print(f"voicecoder approver: desk unreachable ({e})", file=sys.stderr)
        return False, False


# ---- Claude: --permission-prompt-tool backed by a tiny stdio MCP server -------------

APPROVER_TOOL = "mcp__voicecoder__approve"


def cmd_mcp_approver(a):
    """Minimal MCP server (stdio, newline-delimited JSON-RPC) exposing one tool: approve."""
    address = os.environ.get(DESK_ENV, "")
    out = sys.stdout

    def send(msg):
        out.write(json.dumps(msg) + "\n")
        out.flush()

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue  # notification (e.g. notifications/initialized)
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "voicecoder", "version": "1.1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
                "name": "approve",
                "description": "Ask the user out loud whether a tool call may run.",
                "inputSchema": {"type": "object", "properties": {
                    "tool_name": {"type": "string"},
                    "input": {"type": "object"},
                    "tool_use_id": {"type": "string"}},
                    "required": ["tool_name", "input"]}}]}})
        elif method == "tools/call":
            args = params.get("arguments") or {}
            tool, inp = args.get("tool_name", "?"), args.get("input") or {}
            summary, detail, key, risky = describe_claude_request(tool, inp)
            ok, _ = ask_desk(address, summary, detail, key, risky)
            decision = ({"behavior": "allow", "updatedInput": inp} if ok
                        else {"behavior": "deny", "message": "The user said no to this action. "
                              "Do not retry it; ask or choose another way."})
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(decision)}]}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})


def claude_approval_args(desk):
    """Extra CLI flags that route Claude's permission prompts to our approver."""
    cfg = {"mcpServers": {"voicecoder": {
        "command": sys.executable, "args": [HERE, "mcp-approver"],
        "env": {DESK_ENV: desk.address, IN_LOOP_ENV: "1"}}}}
    fd, path = tempfile.mkstemp(suffix=".json", prefix="voicecoder_mcp_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return ["--permission-mode", "default", "--mcp-config", path,
            "--permission-prompt-tool", APPROVER_TOOL], path


# ---- Codex: app-server (JSON-RPC over stdio), which sends real approval requests ----

class CodexAppServer:
    def __init__(self, cwd, desk, approval_policy="on-request", sandbox="workspaceWrite", session=None):
        self.cwd, self.desk = cwd, desk
        self.approval_policy, self.sandbox = approval_policy, sandbox
        self.thread_id = session
        self.resumed = False
        self.items = {}
        self.next_id = 0
        env = dict(os.environ, **{IN_LOOP_ENV: "1"})
        self.proc = subprocess.Popen([_find("codex"), "app-server"], cwd=cwd, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)
        self.request("initialize", {"clientInfo": {"name": "voicecoder", "title": "voicecoder",
                                                    "version": "1.1"}})
        self._send({"method": "initialized"})

    # -- plumbing
    def _send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _read(self):
        raw = self.proc.stdout.readline()
        if not raw:
            raise RuntimeError("Codex app-server stopped unexpectedly.")
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            print(raw.decode("utf-8", "replace").rstrip())
            return None

    def request(self, method, params, state=None):
        self.next_id += 1
        rid = self.next_id
        self._send({"method": method, "id": rid, "params": params})
        while True:
            msg = self._read()
            if msg is None:
                continue
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise RuntimeError((msg["error"] or {}).get("message", str(msg["error"])))
                return msg.get("result") or {}
            self._dispatch(msg, state)

    # -- events
    def _dispatch(self, msg, state):
        method, params = msg.get("method"), msg.get("params") or {}
        if "id" in msg and method:  # server -> client request
            self._answer_server_request(msg["id"], method, params)
            return
        if state is None or not method:
            return
        item = params.get("item") or {}
        itype = item.get("type")
        if item.get("id"):
            self.items[item["id"]] = item
        if method == "item/started" and itype == "commandExecution":
            print(f"  $ {self._cmd_text(item.get('command'))}")
        elif method == "item/completed" and itype == "agentMessage":
            state["message"] = item.get("text") or state["message"]
            print(f"\n{state['message']}\n")
        elif method == "item/completed" and itype == "fileChange":
            print(f"  ~ edited {', '.join(self._files(item)) or 'files'}")
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            state["done"] = True
            if turn.get("status") == "failed":
                state["error"] = ((turn.get("error") or {}).get("message")) or "turn failed"
                print(f"  ! {state['error']}")
        elif method == "error":
            err = params.get("error") or params
            state["error"] = err.get("message") if isinstance(err, dict) else str(err)
            print(f"  ! {state['error']}")

    @staticmethod
    def _cmd_text(cmd):
        return " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd or "")

    @staticmethod
    def _files(item):
        return [c.get("path", "?") for c in item.get("changes", []) or [] if isinstance(c, dict)]

    def _answer_server_request(self, rid, method, params):
        if method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            cmd = self._cmd_text(params.get("command"))
            why = f" Reason: {params['reason']}" if params.get("reason") else ""
            ok, always = self.desk.decide(f"run the command: {cmd}", f"in {params.get('cwd', self.cwd)}.{why}",
                                          "cmd:" + (cmd.split() or [""])[0], cmd)
        elif method in ("item/fileChange/requestApproval", "applyPatchApproval"):
            item = self.items.get(params.get("itemId"), {})
            files = self._files(item) or list((params.get("fileChanges") or {}).keys())
            why = f" Reason: {params['reason']}" if params.get("reason") else ""
            ok, always = self.desk.decide(f"change {len(files) or 'some'} file(s): "
                                          f"{', '.join(os.path.basename(f) for f in files[:5])}",
                                          "\n".join(files) + why, "files", "")
        else:
            self._send({"id": rid, "error": {"code": -32601, "message": f"voicecoder can't handle {method}"}})
            return
        if method.startswith("item/"):
            result = {"decision": "accept" if ok else "decline"}
            if ok and method.startswith("item/commandExecution"):
                result["acceptSettings"] = {"forSession": bool(always)}
        else:  # legacy v1 approval names
            result = {"decision": ("approved_for_session" if always else "approved") if ok else "denied"}
        self._send({"id": rid, "result": result})

    # -- public
    def new_session(self):
        self.thread_id, self.resumed = None, False

    def _ensure_thread(self, state):
        if self.thread_id and not self.resumed:
            self.request("thread/resume", {"threadId": self.thread_id}, state)
            self.resumed = True
            return
        if self.thread_id:
            return
        params = {"cwd": self.cwd, "approvalPolicy": self.approval_policy, "sandbox": self.sandbox}
        try:
            res = self.request("thread/start", params, state)
        except RuntimeError as e:  # older/newer builds spell the enum differently
            print(f"  (thread/start: {e}; retrying with alternate spelling)")
            params["approvalPolicy"] = re.sub(r"-(\w)", lambda m: m.group(1).upper(), self.approval_policy)
            res = self.request("thread/start", params, state)
        self.thread_id = (res.get("thread") or {}).get("id")
        self.resumed = True

    def run_turn(self, prompt):
        state = {"session": None, "message": "", "error": None, "done": False}
        try:
            self._ensure_thread(state)
            self.request("turn/start", {"threadId": self.thread_id,
                                        "input": [{"type": "text", "text": prompt}]}, state)
            while not state["done"]:
                msg = self._read()
                if msg is not None:
                    self._dispatch(msg, state)
        except RuntimeError as e:
            state["error"] = str(e)
            print(f"  ! {e}")
            return 1, state
        state["session"] = self.thread_id
        return 0, state

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
        except Exception:
            pass


# --------------------------------------------------------------------------- commands

def cmd_loop(a):
    cwd = os.path.abspath(a.cwd)
    if not os.path.isdir(cwd):
        raise SystemExit(f"Folder not found: {cwd}")
    name = "Codex" if a.agent == "codex" else "Claude"
    runner = run_codex if a.agent == "codex" else run_claude
    extra = shlex.split(a.agent_args if a.agent_args is not None else DEFAULT_ARGS[a.agent])
    session, last = a.session, ""
    _find(a.agent)  # fail fast, before you start talking
    codex_srv = mcp_cfg = None

    with KeepAwake():
        ears = Ears(lang=a.lang, typed=a.type)
        if not a.no_approvals:
            if a.agent == "claude":
                desk = ApprovalDesk(ears, name)
                appr_args, mcp_cfg = claude_approval_args(desk)
                base = [] if a.agent_args is None else extra  # drop the auto-accept default
                extra = base + appr_args
                os.environ[DESK_ENV] = desk.address
            else:
                try:
                    codex_srv = CodexAppServer(cwd, ApprovalDesk(ears, name, listen=False),
                                               a.codex_approval, a.codex_sandbox, session)
                    runner = lambda prompt, _cwd, _session, _extra: codex_srv.run_turn(prompt)
                except Exception as e:
                    print(f"[approvals] Codex app-server unavailable ({e}). "
                          "Falling back to 'codex exec' without spoken approvals.")
                    codex_srv = None
        mode = "approvals ON - I'll ask before risky steps" if (codex_srv or mcp_cfg) else "approvals off"
        print(f"\nvoicecoder: {name} in {cwd}  ({mode})\n"
              "Say your task. Also: 'repeat', 'new session', 'keyboard', 'stop'.\n")
        speak(f"Voice coder ready. {name} is working in {os.path.basename(cwd) or cwd}. What should I do?")
        try:
            last = _converse(a, ears, name, cwd, runner, extra, session, codex_srv)
        finally:
            if codex_srv:
                codex_srv.close()
            if mcp_cfg:
                try:
                    os.remove(mcp_cfg)
                except OSError:
                    pass
    print("voicecoder stopped. Sleep is allowed again.")


def _converse(a, ears, name, cwd, runner, extra, session, codex_srv):
    last = ""
    while True:
        try:
            heard = ears.hear()
        except (KeyboardInterrupt, EOFError):
            break
        low = _norm(heard)
        if not low:
            continue
        if low in STOP_WORDS:
            speak("Okay, stopping. Bye.")
            break
        if low in REPEAT_WORDS:
            speak(clean_for_speech(last) if last else "Nothing to repeat yet.")
            continue
        if low in NEW_WORDS:
            session = None
            if codex_srv:
                codex_srv.new_session()
            speak("Okay, starting a fresh session. What next?")
            continue
        if low in TYPE_WORDS:
            ears.voice = False
            print("Keyboard mode. Type 'voice' to switch back.")
            continue
        if low == "voice" and hasattr(ears, "mic"):
            ears.voice = True
            speak("Voice mode on.")
            continue

        if ears.voice and not a.no_confirm:
            speak(f"I heard: {heard}. Shall I send it?")
            try:
                answer = set(_norm(ears.hear()).split())
            except (KeyboardInterrupt, EOFError):
                break
            if not answer & YES_WORDS:
                speak("Cancelled. Tell me again.")
                continue

        speak("On it.")
        t0 = time.time()
        try:
            code, st = runner(heard, cwd, session, extra)
        except KeyboardInterrupt:
            speak("Interrupted. What next?")
            continue
        session = st["session"] or session
        mins = (time.time() - t0) / 60
        took = f" after {mins:.0f} minutes" if mins >= 1.5 else ""
        if st["message"]:
            last = st["message"]
            speak(f"{name} has finished{took}. {clean_for_speech(last)} ... What should I do next?")
        else:
            last = st["error"] or ""
            speak(f"{name} stopped with a problem{took}. "
                  f"{clean_for_speech(last, 250) if last else 'Please check the screen.'} What next?")
    return last


def cmd_codex_notify(a):
    if os.environ.get(IN_LOOP_ENV):
        return
    try:
        data = json.loads(a.payload)
    except Exception:
        return
    if data.get("type") == "agent-turn-complete":
        msg = clean_for_speech(data.get("last-assistant-message") or "")
        speak_detached(f"Codex has finished. {msg}" if msg else "Codex has finished its turn.")
    elif data.get("type") == "approval-requested":
        what = data.get("command") or data.get("reason") or data.get("message") or ""
        if isinstance(what, list):
            what = " ".join(map(str, what))
        what = clean_for_speech(str(what), 200)
        speak_detached("Codex is waiting for your approval" + (f" to: {what}" if what else "")
                       + ". Please check the screen.")


def cmd_claude_notify(a):
    """Claude Code `Notification` hook: announce permission prompts in the normal UI."""
    if os.environ.get(IN_LOOP_ENV):
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    msg = clean_for_speech(str(data.get("message") or ""), 200)
    if "permission" in msg.lower() or data.get("notification_type") == "permission_prompt":
        speak_detached(f"{msg}. Please check the screen." if msg else "Claude needs your permission.")


def cmd_claude_stop(a):
    if os.environ.get(IN_LOOP_ENV):
        return
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    if data.get("stop_hook_active"):
        return
    msg = clean_for_speech(data.get("last_assistant_message") or "")
    speak_detached(f"Claude has finished. {msg}" if msg else "Claude has finished.")


def cmd_awake(a):
    with KeepAwake():
        print("Keeping the laptop awake. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass


def cmd_hooks(a):
    py, me = sys.executable, HERE
    print("=" * 70)
    print("CODEX  ->  add this line near the TOP of ~/.codex/config.toml")
    print("          (before any [section] header; Windows: %USERPROFILE%\\.codex\\config.toml)\n")
    print(f"notify = ['{py}', '{me}', 'codex-notify']\n")
    print("=" * 70)
    print("CLAUDE CODE  ->  merge into ~/.claude/settings.json\n")
    stop = f'"{py}" "{me}" claude-stop'
    note = f'"{py}" "{me}" claude-notify'
    print(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": stop}]}],
        "Notification": [{"hooks": [{"type": "command", "command": note}]}]}}, indent=2))
    print("\n(In the normal UI the voice only ANNOUNCES approvals; use `loop` to answer them by voice.)")
    print("\nRestart Codex / Claude Code afterwards. Run `python voicecoder.py awake` alongside")
    print("them if you also want sleep blocked while using the normal interactive UI.")


def cmd_test(a):
    speak("Hello! This is voice coder. If you can hear me, the speaker works.")
    ears = Ears(lang=a.lang)
    if ears.voice:
        speak("Now say something when the terminal shows listening.")
        heard = ears.hear()
        speak(f"I heard: {heard}")


def main():
    p = argparse.ArgumentParser(description="Voice companion for Codex CLI and Claude Code")
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("loop", help="talk to the agent hands-free")
    lp.add_argument("--agent", choices=["codex", "claude"], default="codex")
    lp.add_argument("--cwd", default=".", help="project folder (default: current folder)")
    lp.add_argument("--lang", default="en-IN", help="speech language, e.g. en-IN, en-US, hi-IN")
    lp.add_argument("--agent-args", default=None,
                    help=f"extra CLI flags. defaults: codex='{DEFAULT_ARGS['codex']}', claude='{DEFAULT_ARGS['claude']}'")
    lp.add_argument("--session", default=None, help="resume an existing session/thread id")
    lp.add_argument("--no-confirm", action="store_true", help="send spoken commands without read-back")
    lp.add_argument("--type", action="store_true", help="keyboard only, no microphone")
    lp.add_argument("--no-approvals", action="store_true",
                    help="don't ask before actions (old behaviour: Claude auto-accepts edits, Codex uses exec)")
    lp.add_argument("--codex-approval", default="on-request",
                    choices=["untrusted", "on-request", "on-failure", "never"],
                    help="when Codex asks: 'untrusted' = almost every command, 'on-request' = when it needs more access")
    lp.add_argument("--codex-sandbox", default="workspaceWrite",
                    choices=["readOnly", "workspaceWrite", "dangerFullAccess"])
    lp.set_defaults(fn=cmd_loop)

    cn = sub.add_parser("codex-notify")
    cn.add_argument("payload", nargs="?", default="{}")
    cn.set_defaults(fn=cmd_codex_notify)

    sub.add_parser("claude-stop").set_defaults(fn=cmd_claude_stop)
    sub.add_parser("claude-notify").set_defaults(fn=cmd_claude_notify)
    sub.add_parser("mcp-approver", help="internal: Claude permission helper").set_defaults(fn=cmd_mcp_approver)
    sub.add_parser("awake").set_defaults(fn=cmd_awake)
    sub.add_parser("hooks").set_defaults(fn=cmd_hooks)

    tp = sub.add_parser("test")
    tp.add_argument("--lang", default="en-IN")
    tp.set_defaults(fn=cmd_test)

    sp = sub.add_parser("say")
    sp.add_argument("text", nargs="+")
    sp.set_defaults(fn=lambda a: speak(" ".join(a.text)))

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
