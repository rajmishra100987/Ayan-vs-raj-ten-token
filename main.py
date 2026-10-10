import requests
import json
import time
import os
import threading
import http.server
import socketserver

# ============================================================
# 🚀 TOKEN-BASED MESSENGER BOT — FINAL VERSION
# - Multi-token round robin
# - Fail token → skip + background retry (same cycle)
# - Exponential backoff (10s → 5min)
# - Auto-remove after 20 fails
# - SAFETY RULE: 3 ya kam healthy tokens → 2x delay
# - Zero blocking loop
# ============================================================

DEBUG = os.environ.get("DEBUG") == "1"

# ==================== CONSTANTS ====================
SEND_TIMEOUT = 15
BACKOFF_STEPS = [10, 30, 60, 120, 300]
MAX_LOG_LINES = 200
AUTO_REMOVE_FAIL_THRESHOLD = 20
SAFETY_TOKEN_THRESHOLD = 3       # ✅ 3 ya kam tokens hone par throttle
SAFETY_DELAY_MULTIPLIER = 2      # ✅ Delay 2x karo

# ==================== LOGS ====================
logs = []

def push_log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    logs.append(line)
    if len(logs) > MAX_LOG_LINES:
        del logs[:len(logs) - MAX_LOG_LINES]
    if DEBUG:
        print(line)

# ==================== HEALTH SERVER ====================
class MyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        try:
            token_status = []
            if GLOBAL_STATES:
                for s in GLOBAL_STATES:
                    token_status.append({
                        "idx": s.idx,
                        "healthy": s.healthy,
                        "removed": s.removed,
                        "fail_count": s.fail_count,
                        "retrying": s.retry_in_progress
                    })

            payload = json.dumps({
                "status": "ONLINE",
                "total_tokens": len(GLOBAL_STATES) if GLOBAL_STATES else 0,
                "active_tokens": sum(1 for s in (GLOBAL_STATES or []) if not s.removed),
                "healthy_tokens": sum(1 for s in (GLOBAL_STATES or []) if s.healthy and not s.removed),
                "removed_tokens": sum(1 for s in (GLOBAL_STATES or []) if s.removed),
                "safety_mode": GLOBAL_SAFETY_MODE,   # ✅ Safety mode status
                "token_status": token_status,
                "logs": logs[-40:],
            }).encode()
            self.wfile.write(payload)
        except Exception as e:
            self.wfile.write(f'{{"error": "{e}"}}'.encode())

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()

    def log_message(self, format, *args):
        return

def execute_server():
    PORT = int(os.environ.get("PORT", 3000))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), MyHandler) as httpd:
        push_log(f"Server running on port {PORT}")
        httpd.serve_forever()

# ==================== HELPERS ====================
def backoff_seconds(fail_count):
    idx = min(max(fail_count - 1, 0), len(BACKOFF_STEPS) - 1)
    return BACKOFF_STEPS[idx]

def read_file(name, default=""):
    try:
        with open(name, 'r') as f:
            return f.read().strip()
    except:
        return default

# ==================== GLOBAL STATE ====================
GLOBAL_STATES = None
GLOBAL_SAFETY_MODE = False

# ==================== TOKEN STATE ====================
class TokenState:
    def __init__(self, idx, token):
        self.idx = idx
        self.token = token
        self.healthy = True
        self.fail_count = 0
        self.next_retry_at = 0
        self.retry_in_progress = False
        self.removed = False

# ==================== SEND FUNCTION ====================
def send_via_token(token, convo_id, message, headers):
    url = f"https://graph.facebook.com/v17.0/t_{convo_id}/"
    params = {'access_token': token, 'message': message}
    try:
        response = requests.post(url, json=params, headers=headers, timeout=SEND_TIMEOUT)
        if response.ok:
            return True, None
        else:
            try:
                err = response.json().get('error', {})
                reason = f"HTTP {response.status_code}: {err.get('message', 'unknown')}"
            except:
                reason = f"HTTP {response.status_code}"
            return False, reason
    except requests.exceptions.Timeout:
        return False, f"timeout {SEND_TIMEOUT}s"
    except Exception as e:
        return False, str(e)

# ==================== BACKGROUND RETRY ====================
def background_retry(state, convo_id, headers):
    try:
        if state.removed:
            return

        ok, reason = send_via_token(state.token, convo_id, "Hi", headers)
        if ok:
            state.healthy = True
            state.fail_count = 0
            state.next_retry_at = 0
            push_log(f"✅ Token {state.idx} recovered")
        else:
            state.fail_count += 1

            if state.fail_count >= AUTO_REMOVE_FAIL_THRESHOLD:
                state.removed = True
                state.healthy = False
                push_log(f"💀 Token {state.idx} AUTO-REMOVED after {state.fail_count} fails")
                return

            wait = backoff_seconds(state.fail_count)
            state.next_retry_at = time.time() + wait
            push_log(f"❌ Token {state.idx} retry fail ({state.fail_count}/{AUTO_REMOVE_FAIL_THRESHOLD}): {reason} — next in {wait}s")
    except Exception as e:
        state.fail_count += 1
        if state.fail_count >= AUTO_REMOVE_FAIL_THRESHOLD:
            state.removed = True
            state.healthy = False
            push_log(f"💀 Token {state.idx} AUTO-REMOVED after {state.fail_count} fails (exception)")
            return
        wait = backoff_seconds(state.fail_count)
        state.next_retry_at = time.time() + wait
        push_log(f"❌ Token {state.idx} exception: {e}")
    finally:
        state.retry_in_progress = False

# ==================== MAIN BOT ====================
def send_messages_from_file():
    global GLOBAL_STATES, GLOBAL_SAFETY_MODE

    convo_id = read_file('convo.txt')
    haters_name = read_file('hatersname.txt')
    try:
        speed = int(read_file('time.txt', '60') or 60)
    except:
        speed = 60
    speed = max(2, speed)

    with open('File.txt', 'r') as f:
        messages = [m.strip() for m in f.readlines() if m.strip()]

    with open('tokennum.txt', 'r') as f:
        token_list = [t.strip() for t in f.readlines() if t.strip()]

    if not messages or not token_list or not convo_id:
        push_log("[CONFIG ERROR] File.txt / tokennum.txt / convo.txt check karo")
        return

    push_log(f"[INIT] {len(token_list)} token(s), {len(messages)} message(s)")
    push_log(f"[INIT] Auto-remove: {AUTO_REMOVE_FAIL_THRESHOLD} fails")
    push_log(f"[INIT] Safety mode: ≤{SAFETY_TOKEN_THRESHOLD} tokens → {SAFETY_DELAY_MULTIPLIER}x delay")

    states = [TokenState(i + 1, tok) for i, tok in enumerate(token_list)]
    GLOBAL_STATES = states

    headers = {
        'Connection': 'keep-alive',
        'User-Agent': 'Mozilla/5.0 (Linux; Android 8.0.0; Samsung Galaxy S9)',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    msg_index = 0
    rr_index = 0
    loop_count = 1

    # ============ SEND LOOP (ZERO BLOCK) ============
    while True:
        loop_start = time.time()

        # -------- HEALTHY TOKEN COUNT --------
        active_healthy = [s for s in states if s.healthy and not s.removed]

        # -------- GRACEFUL EXIT (all dead) --------
        if not any(not s.removed for s in states):
            push_log("🛑 SAARE TOKENS AUTO-REMOVED. Loop exit.")
            return

        # -------- ✅ SAFETY RULE (≤3 tokens → 2x delay) --------
        effective_delay = speed
        if len(active_healthy) <= SAFETY_TOKEN_THRESHOLD and len(active_healthy) > 0:
            effective_delay = speed * SAFETY_DELAY_MULTIPLIER
            if not GLOBAL_SAFETY_MODE:
                GLOBAL_SAFETY_MODE = True
                push_log(f"🛡️ SAFETY MODE ON — Only {len(active_healthy)} healthy tokens. Delay {speed}s → {effective_delay}s")
        else:
            if GLOBAL_SAFETY_MODE:
                GLOBAL_SAFETY_MODE = False
                push_log(f"✅ SAFETY MODE OFF — {len(active_healthy)} healthy tokens. Delay back to {speed}s")

        # -------- ROUND LOGIC --------
        if msg_index >= len(messages):
            msg_index = 0
            loop_count += 1
            push_log(f"🔄 Round {loop_count} starting")

        raw_msg = messages[msg_index]
        final_msg = f"{haters_name} {raw_msg}" if haters_name else raw_msg

        attempts = 0
        sent = False
        total_states = len(states)

        while attempts < total_states:
            st = states[rr_index % len(states)]
            rr_index += 1

            # Auto-removed → skip
            if st.removed:
                attempts += 1
                continue

            # Unhealthy → background retry + skip
            if not st.healthy:
                if not st.retry_in_progress and time.time() >= st.next_retry_at:
                    st.retry_in_progress = True
                    threading.Thread(
                        target=background_retry,
                        args=(st, convo_id, headers),
                        daemon=True
                    ).start()
                attempts += 1
                continue

            # Healthy → send
            ok, reason = send_via_token(st.token, convo_id, final_msg, headers)
            if ok:
                push_log(f"🚀 [Token {st.idx}] Sent: {final_msg}")
                st.fail_count = 0
                sent = True
                break
            else:
                st.fail_count += 1

                if st.fail_count >= AUTO_REMOVE_FAIL_THRESHOLD:
                    st.removed = True
                    st.healthy = False
                    push_log(f"💀 Token {st.idx} AUTO-REMOVED after {st.fail_count} fails")
                    attempts += 1
                    continue

                wait = backoff_seconds(st.fail_count)
                st.healthy = False
                st.next_retry_at = time.time() + wait
                st.retry_in_progress = False
                push_log(f"⚠️ [Token {st.idx}] fail ({st.fail_count}/{AUTO_REMOVE_FAIL_THRESHOLD}): {reason} — backoff {wait}s")
                attempts += 1

        if not sent:
            push_log("⚠️ Koi token ready nahi. Round skip (delay continue).")

        msg_index += 1

        # -------- DELAY (with safety multiplier) --------
        elapsed = time.time() - loop_start
        remaining = effective_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

# ==================== MAIN ====================
def main():
    server_thread = threading.Thread(target=execute_server, daemon=True)
    server_thread.start()
    send_messages_from_file()

if __name__ == '__main__':
    main()
