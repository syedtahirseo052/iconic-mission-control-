"""Sheets data layer for the IconicNewswire AI system.

Google Sheets is the ONLY database. SQLite (data/leads.db) is a local mirror.
All functions use hatch_gws_cli (connected Google account) — unless the
GOOGLE_SERVICE_ACCOUNT_JSON env var is set, in which case data/sheets_sa.py
talks to the Sheets API v4 with a service account (for PaaS deploys).
"""
import json
import os
import sqlite3
import subprocess
from datetime import datetime

try:
    from data import sheets_sa
except ImportError:  # direct script run: python data/sheets_db.py
    try:
        import sheets_sa
    except ImportError:
        sheets_sa = None

# Fail fast at startup when a service-account key is configured but broken,
# so a bad Render env var shows a clear error instead of silent 500s.
if sheets_sa is not None and sheets_sa.is_enabled():
    sheets_sa.validate()

LEAD_DB = "14ouEXDi67ibJ0dBFN6GDIL3ZQ1IjSI_CF78h9iNRKFA"
TASKS = "1RTR3XjGX7GdVMfNMhbRfZX3yGswXfL4RNtFo_SNTa6A"
REGISTRY = "1liJFMSO9CasD7BsrhVCZHbzESu9y9f5yM6j40aDFpbo"
ACCESS_QUEUE = "1Kw2ePYes_yieXtmfElPQDjQzkALD0_M-sQAqMGANG8k"

LEAD_HEADERS = ["lead_id", "company_name", "website", "industry", "contact_name",
                "contact_role", "email", "source", "campaign", "lead_type",
                "trigger", "recommended_service", "lead_score", "deal_estimate",
                "status", "owner_agent", "created_at", "updated_at",
                "revenue", "deal_status", "next_followup", "last_contact"]
TASK_HEADERS = ["task_id", "created_by", "assigned_to", "department", "priority",
                "objective", "input", "deadline", "status", "output", "error",
                "escalation", "created_at", "completed_at"]
REG_HEADERS = ["agent_name", "department", "reports_to", "autonomy_level",
               "kpis", "status", "notes"]


def _cli(*args):
    # Service-account path (Render/PaaS): env var presence switches it on.
    if sheets_sa is not None and sheets_sa.is_enabled():
        return sheets_sa.cli(*args)
    # Local path: unchanged behavior via the machine-local CLI.
    r = subprocess.run(["hatch_gws_cli"] + list(args), capture_output=True,
                       text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:500])
    return json.loads(r.stdout)


def read_leads():
    d = _cli("sheets", "spreadsheets", "values", "get", "--params",
             json.dumps({"spreadsheetId": LEAD_DB, "range": "A1:V1000"}))
    vals = d.get("values", [])
    if not vals:
        return []
    hdr = vals[0]
    return [dict(zip(hdr, row)) for row in vals[1:] if row]


def append_leads(rows):
    """rows: list of dicts keyed by LEAD_HEADERS. Returns count appended."""
    vals = [[r.get(h, "") for h in LEAD_HEADERS] for r in rows]
    _cli("sheets", "spreadsheets", "values", "append", "--params",
         json.dumps({"spreadsheetId": LEAD_DB, "range": "A:V",
                     "valueInputOption": "USER_ENTERED"}),
         "--json", json.dumps({"values": vals}))
    return len(rows)


def next_lead_id():
    leads = read_leads()
    nums = [int(l["lead_id"].split("-")[-1]) for l in leads
            if l.get("lead_id", "").startswith("LEAD-")
            and l["lead_id"].split("-")[-1].isdigit()]
    return "LEAD-%05d" % (max(nums, default=0) + 1)


def funnel_stats():
    leads = read_leads()
    by_status, rev_mtd, rev_total, pipe = {}, 0.0, 0.0, 0.0
    month = datetime.now().strftime("%Y-%m")
    for l in leads:
        s = l.get("status", "?")
        by_status[s] = by_status.get(s, 0) + 1
        rev = float(l.get("revenue") or 0)
        rev_total += rev
        if l.get("deal_status") == "WON" and (l.get("updated_at") or "")[:7] == month:
            rev_mtd += rev
        if s in ("HOT", "WARM", "QUALIFIED"):
            try:
                pipe += float(l.get("deal_estimate") or 0)
            except ValueError:
                pass
    hot = sum(1 for l in leads if l.get("status") == "HOT")
    warm = sum(1 for l in leads if l.get("status") == "WARM")
    weighted = sum(float(l.get("deal_estimate") or 0) * 0.5 for l in leads
                   if l.get("status") == "HOT")
    weighted += sum(float(l.get("deal_estimate") or 0) * 0.2 for l in leads
                    if l.get("status") == "WARM")
    return {"total": len(leads), "by_status": by_status,
            "revenue_mtd": rev_mtd, "revenue_total": rev_total,
            "pipeline": pipe, "weighted_pipeline": weighted,
            "hot": hot, "warm": warm}


def read_tasks(status=None):
    d = _cli("sheets", "spreadsheets", "values", "get", "--params",
             json.dumps({"spreadsheetId": TASKS, "range": "A1:N200"}))
    vals = d.get("values", [])
    if not vals:
        return []
    hdr = vals[0]
    tasks = [dict(zip(hdr, row)) for row in vals[1:] if row]
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    return tasks


def add_task(assigned_to, objective, department="", priority="MEDIUM",
             created_by="Iconic AI CEO", input_text="", deadline=""):
    tasks = read_tasks()
    nums = [int(t["task_id"].split("-")[-1]) for t in tasks
            if t.get("task_id", "").startswith("TASK-")
            and t["task_id"].split("-")[-1].isdigit()]
    tid = "TASK-%05d" % (max(nums, default=0) + 1)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    row = {"task_id": tid, "created_by": created_by, "assigned_to": assigned_to,
           "department": department, "priority": priority, "objective": objective,
           "input": input_text, "deadline": deadline, "status": "OPEN",
           "output": "", "error": "", "escalation": "", "created_at": now,
           "completed_at": ""}
    vals = [[row.get(h, "") for h in TASK_HEADERS]]
    _cli("sheets", "spreadsheets", "values", "append", "--params",
         json.dumps({"spreadsheetId": TASKS, "range": "A:N",
                     "valueInputOption": "USER_ENTERED"}),
         "--json", json.dumps({"values": vals}))
    return tid


def read_registry():
    d = _cli("sheets", "spreadsheets", "values", "get", "--params",
             json.dumps({"spreadsheetId": REGISTRY, "range": "A1:G100"}))
    vals = d.get("values", [])
    if not vals:
        return []
    hdr = vals[0]
    return [dict(zip(hdr, row)) for row in vals[1:] if row]


def append_registry_row(row):
    """Append one agent row to the Agent Registry sheet.

    row: dict keyed by REG_HEADERS
         (agent_name, department, reports_to, autonomy_level, kpis, status, notes).
    """
    vals = [[row.get(h, "") for h in REG_HEADERS]]
    _cli("sheets", "spreadsheets", "values", "append", "--params",
         json.dumps({"spreadsheetId": REGISTRY, "range": "A:G",
                     "valueInputOption": "USER_ENTERED"}),
         "--json", json.dumps({"values": vals}))


def merge_sqlite_leads(db_path="leads.db"):
    """One-time consolidation: SQLite working copy -> Sheets (dedupe by website)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    sqlite_leads = [dict(r) for r in con.execute("SELECT * FROM leads")]
    con.close()
    leads = read_leads()
    sites = {str(l.get("website", "")).rstrip("/").lower() for l in leads}
    nid = next_lead_id()
    n = int(nid.split("-")[-1])
    fresh = []
    for l in sqlite_leads:
        if str(l.get("website", "")).rstrip("/").lower() in sites:
            continue
        row = {"lead_id": "LEAD-%05d" % n, "company_name": l.get("company_name", ""),
               "website": l.get("website", ""), "industry": l.get("industry", ""),
               "contact_name": l.get("contact_name", ""), "contact_role": l.get("contact_role", ""),
               "email": l.get("email", ""), "source": l.get("source", ""),
               "campaign": l.get("campaign", ""), "lead_type": l.get("lead_type", ""),
               "trigger": l.get("trigger", ""), "recommended_service": l.get("recommended_service", ""),
               "lead_score": l.get("lead_score", 0), "deal_estimate": l.get("deal_estimate", 0),
               "status": "NEW", "owner_agent": l.get("owner_agent", ""),
               "created_at": l.get("created_at", ""), "updated_at": l.get("updated_at", ""),
               "revenue": 0, "deal_status": "", "next_followup": "", "last_contact": ""}
        fresh.append(row)
        n += 1
    if fresh:
        append_leads(fresh)
    return {"inserted": len(fresh), "skipped": len(sqlite_leads) - len(fresh)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "merge-sqlite":
        print(json.dumps(merge_sqlite_leads()))
    elif len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(funnel_stats(), indent=1))
    else:
        print("usage: sheets_db.py [merge-sqlite|stats]")
