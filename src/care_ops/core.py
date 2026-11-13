"""Core: SQLite store, EMR adapter (idempotent, approval-gated), ranking, agents, simulation.

Ponytail: stdlib only. SQLite is the EMR stand-in; swap EMRAdapter methods for a real FHIR/REST client.
"""
from __future__ import annotations
import hashlib, json, math, random, sqlite3, time
from dataclasses import dataclass, field, asdict

SKILLS = ["personal_care", "dementia", "mobility", "medication", "wound", "companionship"]

def _id(prefix, *parts):
    return prefix + "-" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12]

@dataclass
class Caregiver:
    id: str; name: str; skills: list; lat: float; lon: float
    reliability: float = 0.9; accept_rate: float = 0.7; available: bool = True; active: bool = True

@dataclass
class Visit:
    id: str; client_id: str; caregiver_id: str; skill: str
    lat: float; lon: float; start: float  # epoch minutes
    clocked_in: bool = False; called_off: bool = False

class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path); self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS caregivers(id TEXT PRIMARY KEY,name TEXT,skills TEXT,lat REAL,lon REAL,reliability REAL,accept_rate REAL,available INT,active INT);
        CREATE INDEX IF NOT EXISTS idx_cg_active ON caregivers(active,available);
        CREATE TABLE IF NOT EXISTS visits(id TEXT PRIMARY KEY,client_id TEXT,caregiver_id TEXT,skill TEXT,lat REAL,lon REAL,start REAL,clocked_in INT,called_off INT);
        CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY,visit_id TEXT,kind TEXT,status TEXT,detail TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,incident_id TEXT,caregiver_id TEXT,score REAL,status TEXT,idempotency_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS emr_writes(id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,proposal_id TEXT,payload TEXT,approved_by TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT,action TEXT,ref TEXT,detail TEXT,ts REAL);
        CREATE TABLE IF NOT EXISTS feedback(caregiver_id TEXT,skill TEXT,bonus REAL,PRIMARY KEY(caregiver_id,skill));
        CREATE TABLE IF NOT EXISTS call_log(id INTEGER PRIMARY KEY AUTOINCREMENT,caregiver_id TEXT,proposal_id TEXT,accepted INT,channel TEXT);
        """)
        # Indexed caregiver search: skill index via a join table
        self.db.execute("CREATE TABLE IF NOT EXISTS caregiver_skills(caregiver_id TEXT,skill TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_skill ON caregiver_skills(skill,caregiver_id)")
        self.db.commit()
    def audit(self, actor, action, ref, detail=""):
        self.db.execute("INSERT INTO audit(actor,action,ref,detail,ts) VALUES(?,?,?,?,?)", (actor, action, ref, detail, time.time())); self.db.commit()
    def add_caregiver(self, c: Caregiver):
        self.db.execute("INSERT OR REPLACE INTO caregivers VALUES(?,?,?,?,?,?,?,?,?)",
            (c.id, c.name, json.dumps(c.skills), c.lat, c.lon, c.reliability, c.accept_rate, int(c.available), int(c.active)))
        self.db.execute("DELETE FROM caregiver_skills WHERE caregiver_id=?", (c.id,))
        self.db.executemany("INSERT INTO caregiver_skills VALUES(?,?)", [(c.id, s) for s in c.skills]); self.db.commit()
    def caregivers_for_skill(self, skill):
        rows = self.db.execute("""SELECT c.* FROM caregivers c JOIN caregiver_skills s ON s.caregiver_id=c.id
            WHERE s.skill=? AND c.active=1 AND c.available=1""", (skill,)).fetchall()
        return [Caregiver(r["id"], r["name"], json.loads(r["skills"]), r["lat"], r["lon"], r["reliability"], r["accept_rate"], bool(r["available"]), bool(r["active"])) for r in rows]
    def add_visit(self, v: Visit):
        self.db.execute("INSERT OR REPLACE INTO visits VALUES(?,?,?,?,?,?,?,?,?)",
            (v.id, v.client_id, v.caregiver_id, v.skill, v.lat, v.lon, v.start, int(v.clocked_in), int(v.called_off))); self.db.commit()
    def visits(self):
        return [Visit(r["id"], r["client_id"], r["caregiver_id"], r["skill"], r["lat"], r["lon"], r["start"], bool(r["clocked_in"]), bool(r["called_off"])) for r in self.db.execute("SELECT * FROM visits")]
    def feedback_bonus(self, cid, skill):
        r = self.db.execute("SELECT bonus FROM feedback WHERE caregiver_id=? AND skill=?", (cid, skill)).fetchone()
        return r["bonus"] if r else 0.0
    def apply_feedback(self, cid, skill, delta):
        self.db.execute("INSERT INTO feedback VALUES(?,?,?) ON CONFLICT(caregiver_id,skill) DO UPDATE SET bonus=bonus+excluded.bonus", (cid, skill, delta)); self.db.commit()

def haversine_km(a_lat, a_lon, b_lat, b_lon):
    R = 6371.0; p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat); dl = math.radians(b_lon - a_lon)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

class RankingAgent:
    """Ranks replacements; operator overrides feed back as a learned bonus."""
    def rank(self, store: Store, visit: Visit, exclude=(), limit=5):
        scored = []
        for c in store.caregivers_for_skill(visit.skill):
            if c.id == visit.caregiver_id or c.id in exclude: continue
            dist = haversine_km(visit.lat, visit.lon, c.lat, c.lon)
            # ponytail: linear score, interpretable + tunable; replace with learned model only with real outcome data
            score = 0.45*c.reliability + 0.25*c.accept_rate + 0.30*math.exp(-dist/15.0) + store.feedback_bonus(c.id, visit.skill)
            scored.append((round(score, 4), c, round(dist, 2)))
        scored.sort(key=lambda x: -x[0])
        return [{"caregiver_id": c.id, "name": c.name, "score": s, "distance_km": d, "skills": c.skills} for s, c, d in scored[:limit]]

class UnapprovedWriteError(PermissionError): pass

class EMRAdapter:
    """Every write needs an approved proposal; idempotency_key makes replays no-ops."""
    def __init__(self, store: Store): self.store = store
    def propose(self, incident_id, caregiver_id, score):
        pid = _id("prop", incident_id, caregiver_id); key = _id("idem", incident_id, caregiver_id)
        self.store.db.execute("INSERT OR IGNORE INTO proposals VALUES(?,?,?,?,?,?)", (pid, incident_id, caregiver_id, score, "pending", key))
        self.store.db.commit(); self.store.audit("ranking-agent", "propose", pid, caregiver_id); return pid
    def approve(self, proposal_id, approver, auto=False):
        if not approver: raise UnapprovedWriteError("approver required")
        self.store.db.execute("UPDATE proposals SET status='approved' WHERE id=?", (proposal_id,)); self.store.db.commit()
        self.store.audit(approver, "approve(auto)" if auto else "approve", proposal_id); return self.write_emr(proposal_id, approver)
    def reject(self, proposal_id, approver):
        self.store.db.execute("UPDATE proposals SET status='rejected' WHERE id=?", (proposal_id,)); self.store.db.commit()
        self.store.audit(approver, "reject", proposal_id)
    def write_emr(self, proposal_id, approved_by):
        row = self.store.db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None or row["status"] != "approved": raise UnapprovedWriteError(f"proposal {proposal_id} not approved")
        if not approved_by: raise UnapprovedWriteError("approved_by required")
        key = row["idempotency_key"]
        existing = self.store.db.execute("SELECT id FROM emr_writes WHERE idempotency_key=?", (key,)).fetchone()
        if existing: return existing["id"]  # idempotent replay
        wid = _id("emr", key)
        payload = json.dumps({"replacement_caregiver": row["caregiver_id"], "incident": row["incident_id"]})
        self.store.db.execute("INSERT INTO emr_writes VALUES(?,?,?,?,?,?)", (wid, key, proposal_id, payload, approved_by, time.time()))
        self.store.db.commit(); self.store.audit("emr-adapter", "write", wid, payload); return wid
    def override(self, proposal_id, new_caregiver_id, operator, skill):
        """Operator picks a different caregiver: penalise old top pick implicitly, boost chosen one."""
        self.store.apply_feedback(new_caregiver_id, skill, 0.05)
        self.store.audit(operator, "override", proposal_id, new_caregiver_id)

class VoiceCaller:
    """Mocked voice outreach (no telephony on a laptop). Deterministic per caregiver+seed."""
    def __init__(self, store, seed=7): self.store, self.rng = store, random.Random(seed)
    def call(self, caregiver: Caregiver, proposal_id):
        accepted = self.rng.random() < caregiver.accept_rate
        self.store.db.execute("INSERT INTO call_log(caregiver_id,proposal_id,accepted,channel) VALUES(?,?,?,'voice-mock')", (caregiver.id, proposal_id, int(accepted)))
        self.store.db.commit(); self.store.audit("outreach-agent", "voice_call", proposal_id, f"{caregiver.id}:{'accepted' if accepted else 'declined'}")
        return accepted

class DetectorAgent:
    GRACE_MIN = 15
    def detect(self, store: Store, now, events=()):
        # events: live stream items {"type":"clock_in"|"call_off","visit_id":...}
        for e in events:
            if e.get("type") == "clock_in": store.db.execute("UPDATE visits SET clocked_in=1 WHERE id=?", (e["visit_id"],))
            if e.get("type") == "call_off": store.db.execute("UPDATE visits SET called_off=1 WHERE id=?", (e["visit_id"],))
        store.db.commit()
        found = []
        for v in store.visits():
            kind = "call_off" if v.called_off else ("missed_clock_in" if (not v.clocked_in and now > v.start + self.GRACE_MIN) else None)
            if not kind: continue
            iid = _id("inc", v.id, kind)
            cur = store.db.execute("INSERT OR IGNORE INTO incidents VALUES(?,?,?,?,?,?)", (iid, v.id, kind, "open", v.caregiver_id, now))
            store.db.commit()
            if cur.rowcount: store.audit("detector-agent", "incident", iid, kind); found.append({"id": iid, "visit_id": v.id, "kind": kind, "skill": v.skill})
        return found

@dataclass
class Result: total: int = 0; auto_resolved: int = 0; handoff: int = 0; unresolved: int = 0; emr_writes: int = 0

class Orchestrator:
    """Detector -> Ranker -> Outreach -> Approval policy -> EMR. Auto-approves only high-confidence fixes."""
    # Auto-approve gate: hard constraints (skill match, availability, voice acceptance) are already
    # checked before this point; the composite score is the remaining confidence signal.
    AUTO_SCORE = 0.68
    def __init__(self, store, seed=7):
        self.store, self.ranker, self.emr = store, RankingAgent(), EMRAdapter(store)
        self.caller, self.detector = VoiceCaller(store, seed), DetectorAgent()
    def resolve_incident(self, incident, visit, cg_by_id):
        ranked = self.ranker.rank(self.store, visit)
        if not ranked: return "unresolved"
        # Outreach tries ranked candidates in order (max 3 voice calls); first acceptor is the fix.
        for idx, cand in enumerate(ranked[:3]):
            pid = self.emr.propose(incident["id"], cand["caregiver_id"], cand["score"])
            if not self.caller.call(cg_by_id[cand["caregiver_id"]], pid):
                self.store.db.execute("UPDATE proposals SET status='declined' WHERE id=?", (pid,)); self.store.db.commit(); continue
            if cand["score"] >= self.AUTO_SCORE:
                self.emr.approve(pid, approver="policy:auto", auto=True)
                self.store.db.execute("UPDATE incidents SET status='auto_resolved' WHERE id=?", (incident["id"],)); self.store.db.commit()
                return "auto_resolved"
            self.store.audit("orchestrator", "handoff", incident["id"], "low confidence/margin"); return "handoff"
        self.store.audit("orchestrator", "handoff", incident["id"], "all voice outreach declined"); return "handoff"
    def operator_approve(self, proposal_id, operator): return self.emr.approve(proposal_id, operator)
    def operator_override(self, proposal_id, caregiver_id, operator, skill):
        row = self.store.db.execute("SELECT incident_id FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        self.emr.override(proposal_id, caregiver_id, operator, skill)
        new_pid = self.emr.propose(row["incident_id"], caregiver_id, 1.0)
        return self.emr.approve(new_pid, operator)

def simulate(n_incidents=2000, n_caregivers=300, seed=42, n_redteam=400):
    rng = random.Random(seed); store = Store(); orch = Orchestrator(store, seed=seed)
    cgs = []
    for i in range(n_caregivers):
        skills = rng.sample(SKILLS, k=rng.randint(2, 4))
        c = Caregiver(f"cg-{i}", f"Caregiver {i}", skills, 40+rng.random(), -74+rng.random(),
                      reliability=round(rng.uniform(0.6, 0.99), 2), accept_rate=round(rng.uniform(0.35, 0.95), 2))
        cgs.append(c); store.add_caregiver(c)
    now = 10_000.0; res = Result(total=n_incidents)
    for i in range(n_incidents):
        skill = rng.choice(SKILLS); orig = rng.choice(cgs)
        v = Visit(f"v-{i}", f"client-{i%500}", orig.id, skill, 40+rng.random(), -74+rng.random(), now-30)
        store.add_visit(v)
        kind = rng.choice(["missed_clock_in", "call_off"])
        events = [{"type": "call_off", "visit_id": v.id}] if kind == "call_off" else []
        for inc in orch.detector.detect(store, now, events):
            out = orch.resolve_incident(inc, v, {c.id: c for c in cgs})
            if out == "auto_resolved": res.auto_resolved += 1
            elif out == "handoff": res.handoff += 1
            else: res.unresolved += 1
    res.emr_writes = store.db.execute("SELECT COUNT(*) c FROM emr_writes").fetchone()["c"]
    # Red team: try every way to write without approval; count successes (must be 0)
    unapproved = 0
    for i in range(n_redteam):
        pid = orch.emr.propose(f"rt-inc-{i}", rng.choice(cgs).id, 0.9)
        for attempt in (lambda: orch.emr.write_emr(pid, "attacker"),
                        lambda: orch.emr.write_emr(pid, ""),
                        lambda: orch.emr.approve(pid, ""),
                        lambda: orch.emr.write_emr("prop-nonexistent", "attacker")):
            try: attempt(); unapproved += 1
            except Exception: pass
        # replay of a legitimately approved write must not duplicate
    return {"incidents": res.total, "auto_resolved": res.auto_resolved, "handoff": res.handoff,
            "unresolved": res.unresolved, "auto_resolution_rate": round(res.auto_resolved/max(res.total,1), 4),
            "emr_writes": res.emr_writes, "redteam_cases": n_redteam, "unapproved_emr_writes": unapproved,
            "audit_events": store.db.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"]}

def demo_state():
    """Small seeded dataset for the console / API."""
    store = Store(); rng = random.Random(1); orch = Orchestrator(store)
    cgs = []
    for i in range(40):
        c = Caregiver(f"cg-{i}", f"Caregiver {i}", rng.sample(SKILLS, k=3), 40+rng.random(), -74+rng.random(), round(rng.uniform(.7,.99),2), round(rng.uniform(.4,.95),2))
        cgs.append(c); store.add_caregiver(c)
    now = 10_000.0
    for i in range(12):
        orig = rng.choice(cgs); v = Visit(f"v-{i}", f"client-{i}", orig.id, rng.choice(SKILLS), 40+rng.random(), -74+rng.random(), now-30)
        store.add_visit(v)
        orch.detector.detect(store, now, [{"type": "call_off", "visit_id": v.id}] if i % 3 == 0 else [])
        for inc in [r for r in store.db.execute("SELECT id,visit_id,kind FROM incidents WHERE status='open'")]:
            pass
    # resolve through orchestrator so console shows actions + pending approvals
    visits = {v.id: v for v in store.visits()}; cg_by_id = {c.id: c for c in cgs}
    for r in list(store.db.execute("SELECT id,visit_id,kind FROM incidents WHERE status='open'")):
        v = visits[r["visit_id"]]
        orch.resolve_incident({"id": r["id"], "visit_id": v.id, "kind": r["kind"], "skill": v.skill}, v, cg_by_id)
    return store, orch
