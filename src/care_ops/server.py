"""Stdlib HTTP API for the operator console: incidents, agent audit, approvals, overrides."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from .core import demo_state

STORE, ORCH = demo_state()

def snapshot():
    db = STORE.db
    incidents = [dict(r) for r in db.execute("SELECT * FROM incidents ORDER BY created DESC")]
    for inc in incidents:
        inc["proposals"] = [dict(r) for r in db.execute("SELECT * FROM proposals WHERE incident_id=?", (inc["id"],))]
    return {"incidents": incidents,
            "audit": [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100")],
            "emr_writes": [dict(r) for r in db.execute("SELECT * FROM emr_writes ORDER BY created DESC LIMIT 50")],
            "calls": [dict(r) for r in db.execute("SELECT * FROM call_log ORDER BY id DESC LIMIT 50")]}

class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_OPTIONS(self): self._send({})
    def do_GET(self):
        self._send(snapshot()) if self.path.startswith("/api/state") else self._send({"error": "not found"}, 404)
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        try:
            if self.path == "/api/approve": ORCH.operator_approve(data["proposal_id"], data.get("operator", "operator-1"))
            elif self.path == "/api/reject": ORCH.emr.reject(data["proposal_id"], data.get("operator", "operator-1"))
            elif self.path == "/api/override": ORCH.operator_override(data["proposal_id"], data["caregiver_id"], data.get("operator", "operator-1"), data.get("skill", "personal_care"))
            else: return self._send({"error": "not found"}, 404)
            self._send(snapshot())
        except Exception as e: self._send({"error": str(e)}, 400)
    def log_message(self, *a): pass

def main(port=8000):
    print(f"Care Ops API on http://localhost:{port}/api/state")
    HTTPServer(("127.0.0.1", port), H).serve_forever()
if __name__ == "__main__": main()
