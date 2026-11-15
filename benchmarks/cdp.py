"""Minimal Chrome DevTools Protocol client for taking dashboard screenshots.

Purpose
    Starts a headless Chromium-family browser, opens a page, evaluates JavaScript
    and captures PNG screenshots. Used by benchmarks/bench.py. Standard library
    only: the WebSocket handshake and framing are implemented here.

Why not `--screenshot`
    The dashboard keeps a Server-Sent Events connection open, so the browser's
    one-shot `--screenshot` flag either fires before the page has loaded its data
    or never fires at all. Driving the page over the DevTools port avoids both.

State mutations
    Starts one browser subprocess with a throwaway profile and writes the PNG files
    it is asked for. `close()` terminates the browser.

Side effects
    Opens a loopback debugging port (9333 by default) while the browser runs.

@rollback
    `CDP.close()` stops the browser; delete the PNG files to discard screenshots.
    To remove this file, `git rm benchmarks/cdp.py`. Python 3.9+.
"""
import base64
import glob
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request


def find_browser():
    """Return the path of a headless-capable Chromium-family browser, or "" if none is found.

    Order: MAID_BROWSER_BIN, a Playwright-cached headless shell, then common
    installed browsers.

    @rollback: none (read-only).
    """
    override = os.getenv("MAID_BROWSER_BIN")
    if override:
        return override if os.path.exists(override) else ""
    cache = os.path.expanduser("~/Library/Caches/ms-playwright")
    shells = sorted(glob.glob(os.path.join(cache, "chromium_headless_shell-*", "*", "chrome-headless-shell")))
    candidates = shells[::-1] + [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ] + [shutil.which(name) or "" for name in ("chromium", "chromium-browser", "google-chrome")]
    return next((path for path in candidates if path and os.path.exists(path)), "")


class CDP:
    """One browser page driven over the DevTools protocol.

    State mutations: owns a browser subprocess and a temporary profile directory.
    @rollback: `close()`.
    """

    def __init__(self, binary, port=9333, size=(1600, 1000)):
        self.profile = tempfile.mkdtemp(prefix="maid-browser-")
        argv = [
            binary, "--remote-debugging-port=%d" % port, "--window-size=%d,%d" % size, "--hide-scrollbars",
            "--user-data-dir=%s" % self.profile, "--no-first-run", "--no-default-browser-check", "about:blank",
        ]
        if "headless-shell" not in os.path.basename(binary):
            argv.insert(1, "--headless=new")
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target = None
        for _ in range(100):
            try:
                pages = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % port, timeout=1))
                target = next(page for page in pages if page["type"] == "page")
                break
            except Exception:
                time.sleep(0.1)
        if target is None:
            raise RuntimeError("browser did not start")
        url = target["webSocketDebuggerUrl"]
        host_port, path = url[len("ws://"):].split("/", 1)
        host, port_text = host_port.split(":")
        self.sock = socket.create_connection((host, int(port_text)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, host_port, key)).encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.sock.recv(4096)
        self.buffer = response.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0

    def _read(self, count):
        """Read exactly `count` bytes from the socket.

        @rollback: none needed.
        """
        while len(self.buffer) < count:
            data = self.sock.recv(65536)
            if not data:
                raise RuntimeError("socket closed")
            self.buffer += data
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def _recv(self):
        """Read one complete WebSocket message and return it as parsed JSON.

        @rollback: none needed.
        """
        message = b""
        while True:
            head = self._read(2)
            fin, opcode, length = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length)
            if opcode in (0, 1, 2):
                message += payload
                if fin:
                    return json.loads(message)

    def call(self, method, **params):
        """Send one DevTools command and return its result; raises RuntimeError on a protocol error.

        @rollback: none needed.
        """
        self.next_id += 1
        data = json.dumps({"id": self.next_id, "method": method, "params": params}).encode()
        mask = os.urandom(4)
        header = bytes([0x81])
        if len(data) < 126:
            header += bytes([0x80 | len(data)])
        elif len(data) < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(data))
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", len(data))
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        while True:
            reply = self._recv()
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply["result"]

    def goto(self, url, wait=1.5):
        """Navigate to `url` and wait `wait` seconds for the page to load its data.

        @rollback: none needed.
        """
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Page.navigate", url=url)
        time.sleep(wait)

    def js(self, expression):
        """Evaluate JavaScript in the page and return its value; raises RuntimeError if it throws.

        @rollback: none needed.
        """
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("exception", {}).get("description") or str(result["exceptionDetails"]))
        return result["result"].get("value")

    def type(self, text):
        """Insert text at the focused element.

        @rollback: none needed.
        """
        self.call("Input.insertText", text=text)

    def key(self, key, code=None, vk=0):
        """Press and release one key.

        @rollback: none needed.
        """
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", type=kind, key=key, code=code or key, windowsVirtualKeyCode=vk)

    def click(self, selector):
        """Click the centre of the first element matching a CSS selector.

        @rollback: none needed.
        """
        box = self.js("(()=>{const r=document.querySelector(%s).getBoundingClientRect(); return [r.left+r.width/2, r.top+r.height/2];})()" % json.dumps(selector))
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=kind, x=box[0], y=box[1], button="left", clickCount=1)

    def shot(self, path):
        """Save a PNG screenshot of the viewport to `path` and return the path.

        @rollback: none needed.
        """
        data = self.call("Page.captureScreenshot", format="png")["data"]
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(data))
        return path

    def close(self):
        """Stop the browser and remove its temporary profile.

        @rollback: none needed.
        """
        try:
            self.sock.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)
