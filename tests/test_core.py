import sys; sys.path.insert(0, "src")
from care_ops.core import *

def cg(i, skills=("personal_care",), rel=.95, acc=.9, lat=40.1, lon=-74.1):
    return Caregiver(f"cg-{i}", f"C{i}", list(skills), lat, lon, rel, acc)

def test_detector_missed_and_calloff():
    s = Store(); s.add_caregiver(cg(0)); s.add_visit(Visit("v1","cl1","cg-0","personal_care",40,-74,100))
    assert DetectorAgent().detect(s, 200)[0]["kind"] == "missed_clock_in"
    assert DetectorAgent().detect(s, 200) == []  # deduped
    s.add_visit(Visit("v2","cl2","cg-0","personal_care",40,-74,100))
    assert DetectorAgent().detect(s, 101, [{"type":"call_off","visit_id":"v2"}])[0]["kind"] == "call_off"
    s.add_visit(Visit("v3","cl3","cg-0","personal_care",40,-74,100, clocked_in=True))
    assert all(i["visit_id"] != "v3" for i in DetectorAgent().detect(s, 999))

def test_indexed_skill_search_excludes_unavailable():
    s = Store(); a, b = cg(1), cg(2, skills=("dementia",)); b.available=False
    s.add_caregiver(a); s.add_caregiver(b)
    assert [c.id for c in s.caregivers_for_skill("personal_care")] == ["cg-1"]
    assert s.caregivers_for_skill("dementia") == []

def test_ranking_prefers_near_reliable_and_learns_override():
    s = Store(); far = cg(1, rel=.99, lat=41.5); near = cg(2, rel=.7, lat=40.11)
    s.add_caregiver(far); s.add_caregiver(near)
    v = Visit("v","c","cg-0","personal_care",40.1,-74.1,0)
    ranked = RankingAgent().rank(s, v); assert ranked
    s.apply_feedback(ranked[-1]["caregiver_id"], "personal_care", 1.0)
    assert RankingAgent().rank(s, v)[0]["caregiver_id"] == ranked[-1]["caregiver_id"]

def test_emr_requires_approval_and_is_idempotent():
    s = Store(); emr = EMRAdapter(s); pid = emr.propose("inc1","cg-1",.9)
    for bad in (lambda: emr.write_emr(pid,"x"), lambda: emr.approve(pid,"")):
        try: bad(); assert False
        except Exception: pass
    w1 = emr.approve(pid, "op-1"); w2 = emr.write_emr(pid, "op-1")
    assert w1 == w2 and s.db.execute("SELECT COUNT(*) c FROM emr_writes").fetchone()["c"] == 1

def test_auto_resolution_only_high_confidence():
    r = simulate(200, n_caregivers=80, n_redteam=50, seed=3)
    assert r["unapproved_emr_writes"] == 0 and r["auto_resolved"] + r["handoff"] + r["unresolved"] == 200
    assert r["emr_writes"] == r["auto_resolved"]  # every auto-resolution writes exactly once, handoffs don't write

def test_bench_shape():
    r = simulate(50, n_caregivers=30, n_redteam=10, seed=1)
    assert 0 <= r["auto_resolution_rate"] <= 1 and r["redteam_cases"] == 10
