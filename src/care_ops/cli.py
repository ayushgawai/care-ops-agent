import argparse, json
from .core import simulate, demo_state
from . import server

def main():
    p = argparse.ArgumentParser(prog="care-ops")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bench"); b.add_argument("--incidents", type=int, default=2000); b.add_argument("--redteam", type=int, default=400); b.add_argument("--seed", type=int, default=42)
    sub.add_parser("serve"); sub.add_parser("demo")
    a = p.parse_args()
    if a.cmd == "bench":
        r = simulate(a.incidents, seed=a.seed, n_redteam=a.redteam)
        print(json.dumps(r, indent=2))
        print(f"\nAuto-resolved {r['auto_resolved']}/{r['incidents']} = {r['auto_resolution_rate']:.1%} without operator hand-off; "
              f"unapproved EMR writes: {r['unapproved_emr_writes']}/{r['redteam_cases']} red-team cases")
    elif a.cmd == "serve": server.main()
    else:
        s, _ = demo_state(); snap = server.snapshot() if False else None
        print(f"demo incidents: {s.db.execute('SELECT COUNT(*) c FROM incidents').fetchone()['c']}, "
              f"proposals: {s.db.execute('SELECT COUNT(*) c FROM proposals').fetchone()['c']}, "
              f"emr_writes: {s.db.execute('SELECT COUNT(*) c FROM emr_writes').fetchone()['c']}")
if __name__ == "__main__": main()
