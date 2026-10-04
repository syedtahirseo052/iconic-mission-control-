"""Mission Control dashboard v2 — FastAPI app.

v1 was read-only. v2 adds, behind a single-user login (Tahir):
  - "Assign Task": modal form -> one Tasks-sheet row via sheets_db.add_task().
  - "Talk to Agent": per-agent message thread; messages are Tasks-sheet rows
    (objective prefixed "Direct message from Tahir: "). Agent "replies" are
    ONLY real task output from the sheet — never simulated.
  - "Add Agent": appends an Agent Registry row + generates agents/<slug>/SOP.md.

Data reads come from Google Sheets via data/sheets_db.py (60s cache).
Writes go through sheets_db only. All numbers on screen come from real
sheet data; failures render honest error states. API errors return the
real error message — never a fake success.
"""
import os
import re
import sys
import time
import json
import secrets
from datetime import datetime

# dashboard/ lives inside iconicnewswire-ai-team/; add it to sys.path for the data layer
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
AGENTS_DIR = os.path.join(PROJECT_DIR, "agents")
sys.path.insert(0, PROJECT_DIR)

from fastapi import FastAPI, Request, Form
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import bcrypt

from data import sheets_db

app = FastAPI(title="IconicNewswire — Mission Control")

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")),
          name="static")

REPORTS_DIR = os.path.join(PROJECT_DIR, "reports")
CACHE_TTL = 60  # seconds
_cache = {}     # key -> {"ts": float, "value": any}


# ------------------------------------------------------------------ auth ---
SESSION_COOKIE = "mc_session"
LOGIN_USER = "tahir"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days


def _session_secret():
    """Signing secret: env var wins; otherwise a persisted random file."""
    env = os.environ.get("DASHBOARD_SECRET")
    if env:
        return env
    path = os.path.join(BASE_DIR, ".session_secret")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    token = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(token)
    os.chmod(path, 0o600)
    return token


_serializer = URLSafeTimedSerializer(_session_secret())


def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("u") if data.get("u") == LOGIN_USER else None


def _verify_password(password: str) -> bool:
    """Compare against the bcrypt hash in DASHBOARD_PASSWORD. Never logs it."""
    expected = os.environ.get("DASHBOARD_PASSWORD", "")
    if not expected or not password:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"),
                              expected.encode("utf-8"))
    except Exception:
        return False


def need_login_page(request: Request):
    """RedirectResponse to /login when logged out, else None."""
    if current_user(request):
        return None
    return RedirectResponse("/login", status_code=303)


def need_login_api(request: Request):
    """401 JSON when logged out, else None."""
    if current_user(request):
        return None
    return JSONResponse({"ok": False, "error": "Not logged in."},
                        status_code=401)


def api_error(message, status=400):
    return JSONResponse({"ok": False, "error": message}, status_code=status)


# ----------------------------------------------------------------- cache ---
def get_cached(key, loader):
    """Return (value, error). Stale cache is reused when a refresh fails."""
    now = time.time()
    entry = _cache.get(key)
    if entry and (now - entry["ts"]) < CACHE_TTL:
        return entry["value"], None
    try:
        value = loader()
        _cache[key] = {"ts": now, "value": value}
        return value, None
    except Exception as exc:  # noqa: BLE001 — surface honestly on the page
        if entry:
            return entry["value"], "refresh failed (%s) — showing cached data" % exc
        return None, "data unavailable (%s)" % exc


def load_agents():
    return get_cached("agents", sheets_db.read_registry)


def load_tasks():
    return get_cached("tasks", sheets_db.read_tasks)


def load_leads():
    return get_cached("leads", sheets_db.read_leads)


def invalidate(*keys):
    for k in keys:
        _cache.pop(k, None)


# ------------------------------------------------------------ targets ---
# Monthly revenue targets live here — Tahir edits them from the dashboard
# (Overview target card → Edit). No hardcoded target amounts in code.
TARGETS_PATH = os.path.join(BASE_DIR, "targets.json")


def load_targets():
    """Monthly targets. TARGETS_JSON env var (a JSON string like
    '{"2026-10":10000}') wins when set — it is the source of truth on PaaS
    free tiers where the filesystem is ephemeral. Falls back to
    dashboard/targets.json for local dev."""
    env = os.environ.get("TARGETS_JSON", "").strip()
    if env:
        try:
            data = json.loads(env)
            return {str(k): float(v) for k, v in data.items()}
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
            return {}
    try:
        with open(TARGETS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): float(v) for k, v in data.items()}
    except Exception:
        return {}


def _write_targets_file(clean):
    tmp = TARGETS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2, sort_keys=True)
    os.replace(tmp, TARGETS_PATH)


def save_targets(data):
    clean = {}
    for k, v in (data or {}).items():
        key = str(k).strip()
        if not re.fullmatch(r"\d{4}-\d{2}", key):
            raise ValueError("Month must be YYYY-MM.")
        amt = float(v)
        if amt < 0:
            raise ValueError("Target must be >= 0.")
        clean[key] = amt
    if os.environ.get("TARGETS_JSON", "").strip():
        # PaaS free tiers have an ephemeral filesystem: the env var is the
        # source of truth, so a file edit would not survive a restart.
        # Still attempt the write (harmless locally), but never crash on
        # a read-only filesystem.
        try:
            _write_targets_file(clean)
        except OSError:
            pass
        return clean
    _write_targets_file(clean)
    return clean


def month_revenue(leads, ym):
    """Actual revenue toward a month's target.

    COMPUTATION (exact):
        sum(float(lead['revenue'])
            for lead in leads
            if float(lead['revenue']) > 0
            and lead['deal_status'].strip().upper() == 'WON'
            and lead['updated_at'][:7] == ym)
    Revenue counts only when the deal is marked WON in the sheet and its
    last update is dated in that month (TEAM.md: WON = revenue logged).
    """
    total = 0.0
    count = 0
    for lead in (leads or []):
        try:
            rev = float(str(lead.get("revenue") or "0").replace(",", "").strip() or 0)
        except (ValueError, TypeError):
            continue
        deal_status = str(lead.get("deal_status") or "").strip().upper()
        updated_at = str(lead.get("updated_at") or "")
        if rev > 0 and deal_status == "WON" and updated_at[:7] == ym:
            total += rev
            count += 1
    return total, count


def month_label(ym):
    try:
        return datetime.strptime(ym, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return ym


# ------------------------------------------------------------ departments ---
# Display cards for the overview. Each maps to actual registry department
# values. SEO exists in the ORG_CHART v2 spec but has no rows in the registry
# sheet yet — it renders an honest empty state.
DEPT_CARDS = [
    {"key": "sales", "label": "Sales", "color": "#2BF381",
     "icon": "megaphone",
     "registry_depts": ["Client Acquisition", "Sales", "Supply", "Inventory"],
     "blurb": "The hunting machine — biggest department"},
    {"key": "seo", "label": "SEO", "color": "#8B5CF6",
     "icon": "search",
     "registry_depts": ["SEO"],
     "blurb": "New department per ORG_CHART v2 — growth engine"},
    {"key": "social", "label": "Social Media Marketing", "color": "#3B82F6",
     "icon": "share",
     "registry_depts": ["Social Media Marketing", "Marketing"],
     "blurb": "Company social handles — drafts-first"},
    {"key": "tech", "label": "Technology", "color": "#F59E0B",
     "icon": "cpu",
     "registry_depts": ["Technology"],
     "blurb": "Website quality · tech tickets"},
    {"key": "ceo", "label": "CEO Office", "color": "#94A3B8",
     "icon": "crown",
     "registry_depts": ["Executive", "Data", "Finance", "Support", "Personal"],
     "blurb": "Full system ownership · monthly target"},
]

TASK_OPEN_STATES = {"OPEN", "IN_PROGRESS"}


def dept_key_index(agents):
    """agent_name.upper() -> dept card key (for attributing tasks)."""
    idx = {}
    for card in DEPT_CARDS:
        for a in (agents or []):
            if a.get("department") in card["registry_depts"]:
                idx[str(a.get("agent_name") or "").strip().upper()] = card["key"]
    return idx


def spark_points(tasks, dept_index, dept_key, days=14):
    """DONE tasks per day for the last `days` days for one dept.

    Real data from the Tasks sheet's completed_at. Flat honest zeros when
    there is nothing — never invented.
    """
    today = datetime.now().date()
    buckets = [0] * days
    for t in (tasks or []):
        if norm_status(t.get("status")) != "DONE":
            continue
        ca = str(t.get("completed_at") or "")[:10]
        try:
            d = datetime.strptime(ca, "%Y-%m-%d").date()
        except ValueError:
            continue
        delta = (today - d).days
        if 0 <= delta < days:
            if dept_index.get(str(t.get("assigned_to") or "").strip().upper()) == dept_key:
                buckets[days - 1 - delta] += 1
    return buckets


def spark_svg_points(points, w=280, h=56, pad=6):
    """'x,y x,y …' point string for polyline/polygon. Flat line when empty."""
    n = len(points)
    mx = max(points) if points and max(points) > 0 else 1
    xs = [pad + i * (w - 2 * pad) / (n - 1) for i in range(n)]
    ys = [h - pad - (p / mx) * (h - 2 * pad) for p in points]
    return " ".join("%.1f,%.1f" % (x, y) for x, y in zip(xs, ys))


# ORG_CHART v2 (SPEC LOCKED) defines 55 agents; the registry sheet currently
# holds 37. These 18 are in the approved spec but not yet registered in the
# sheet — they render on /teams clearly labelled "pending registration in
# sheet" so the page shows all 55 without inventing live data.
SPEC_AGENTS = [
    # cross-dept (report to CEO)
    ("Dispatcher Agent", "Executive", "Iconic AI CEO",
     "Chief of Staff — routes Tahir's instructions to departments"),
    ("Daily Reporting Agent", "Executive", "Iconic AI CEO",
     "Collects all-agent reporting; sends Tahir one daily briefing"),
    # sales hunters (report to Client Acquisition Manager)
    ("LinkedIn Prospect Hunter", "Client Acquisition", "Client Acquisition Manager",
     "READ-ONLY prospecting: small agencies. NO DMs, NO connection requests"),
    ("Facebook Lead Hunter", "Client Acquisition", "Client Acquisition Manager",
     "Facebook lead discovery in our niches"),
    ("Instagram Lead Hunter", "Client Acquisition", "Client Acquisition Manager",
     "Instagram lead discovery in our niches"),
    ("Telegram Lead Hunter", "Client Acquisition", "Client Acquisition Manager",
     "Telegram lead discovery in our niches"),
    ("Google Maps Hunter", "Client Acquisition", "Client Acquisition Manager",
     "Discovers businesses by country/city; review signals as INTEL only"),
    ("Blog Audit PDF Agent", "Client Acquisition", "Client Acquisition Manager",
     "Site audit -> PDF report -> value-first share. NO instant pitch"),
    ("WhatsApp Group Agent", "Client Acquisition", "Client Acquisition Manager",
     "Tahir's warm groups only. MAX 10 msgs/day. Drafts-first"),
    # SEO department (new in v2)
    ("Iconic SEO Head", "SEO", "Iconic AI CEO",
     "Head of the SEO department"),
    ("SEO Expert", "SEO", "Iconic SEO Head", "On-page SEO"),
    ("Off-Page SEO Expert", "SEO", "Iconic SEO Head", "Link building, off-page"),
    ("Technical SEO Expert", "SEO", "Iconic SEO Head", "Technical SEO audits"),
    ("Content Writer", "SEO", "Iconic SEO Head", "SEO content"),
    # social wing
    ("Graphic Designer", "Marketing", "Marketing Brand Manager",
     "Brand creatives — dark black/teal visual system"),
    ("Social Content Creator", "Marketing", "Marketing Brand Manager",
     "Social post copy and content packs"),
    ("Video Editing Expert", "Marketing", "Marketing Brand Manager",
     "Video edits for social"),
    # tech
    ("Website Tech Developer", "Technology", "Technology Website Manager",
     "Theme-file and development work on iconicnewswire.com"),
]


def spec_agent_rows():
    return [{"agent_name": n, "department": d, "reports_to": r,
             "autonomy_level": "", "kpis": k,
             "status": "PENDING REGISTRATION",
             "notes": "ORG_CHART v2 spec — not yet in registry sheet",
             "spec_only": True}
            for (n, d, r, k) in SPEC_AGENTS]


# Teams page department sections (display label -> actual registry department
# values, normalized from ORG_CHART v2 naming to the values found in the sheet).
TEAM_SECTIONS = [
    ("CEO Office", ["Executive"]),
    ("Sales", ["Client Acquisition", "Sales"]),
    ("SEO", ["SEO"]),
    ("Social Media Marketing", ["Social Media Marketing", "Marketing"]),
    ("Technology", ["Technology"]),
    ("Other teams", None),  # everything else in the registry (cross-functional)
]


def team_sections_data(agents, tasks):
    """Build department sections for /teams; each agent carries open task count."""
    by_agent = {}
    for t in (tasks or []):
        key = str(t.get("assigned_to") or "").strip().upper()
        if not key:
            continue
        if norm_status(t.get("status")) in TASK_OPEN_STATES:
            by_agent[key] = by_agent.get(key, 0) + 1

    enriched = []
    known = {str(a.get("agent_name") or "").strip().upper()
             for a in (agents or [])}
    for a in (agents or []):
        a = dict(a)
        a["open_tasks"] = by_agent.get(
            str(a.get("agent_name") or "").strip().upper(), 0)
        enriched.append(a)
    for a in spec_agent_rows():
        if str(a.get("agent_name") or "").strip().upper() in known:
            continue  # already registered in the sheet — sheet wins
        a["open_tasks"] = 0
        enriched.append(a)

    used = set()
    sections = []
    for label, dept_values in TEAM_SECTIONS:
        if dept_values is None:
            members = [a for a in enriched if a.get("department") not in used]
        else:
            members = [a for a in enriched if a.get("department") in dept_values]
            used.update(dept_values)
        if not members:
            continue
        members.sort(key=lambda a: (a.get("status") != "ACTIVE",
                                    str(a.get("agent_name") or "")))
        sections.append({
            "label": label,
            "members": members,
            "active": sum(1 for a in members
                          if str(a.get("status") or "") == "ACTIVE"),
        })
    return sections


def norm_status(value):
    return str(value or "").strip().upper().replace("-", "_")


TASK_GROUP_TITLES = {
    "OPEN": "Open",
    "IN_PROGRESS": "In Progress",
    "DONE": "Done",
    "CANCELLED": "Cancelled",
}

PRIORITY_RANK = {"P0": 0, "P1": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def group_tasks(tasks):
    """Group tasks by normalized status; unknown statuses get their own group."""
    groups = {}
    order = []
    for t in (tasks or []):
        key = norm_status(t.get("status")) or "OPEN"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(t)
    # canonical groups first, then any others, in discovery order
    preferred = ["OPEN", "IN_PROGRESS", "DONE", "CANCELLED"]
    ordered = [k for k in preferred if k in groups]
    ordered += [k for k in order if k not in preferred]

    def prio_rank(t):
        return PRIORITY_RANK.get(norm_status(t.get("priority")), 4)
    for k in ordered:
        groups[k].sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)
        groups[k].sort(key=prio_rank)
    return [(k, TASK_GROUP_TITLES.get(k, k.replace("_", " ").title()), groups[k])
            for k in ordered]


def is_approval_status(value):
    n = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    return ("APPROV" in n) or ("REVIEW" in n) or ("PENDING" in n)


def reports_listing():
    """Newest-first listing of files in reports/, with mtimes."""
    items = []
    try:
        for name in os.listdir(REPORTS_DIR):
            path = os.path.join(REPORTS_DIR, name)
            if os.path.isfile(path):
                mtime = os.path.getmtime(path)
                items.append({
                    "name": name,
                    "mtime": mtime,
                    "mtime_str": datetime.fromtimestamp(mtime).strftime(
                        "%Y-%m-%d %H:%M"),
                    "is_today": datetime.fromtimestamp(mtime).date() == datetime.now().date(),
                })
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items, None
    except FileNotFoundError:
        return [], "reports directory not found"
    except Exception as exc:  # noqa: BLE001
        return [], "could not list reports (%s)" % exc


def read_briefing():
    """Today's daily briefing file, if present."""
    today = datetime.now().strftime("%Y-%m-%d")
    for name in ("daily-briefing-%s.md" % today, "daily-briefing.md"):
        path = os.path.join(REPORTS_DIR, name)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read(), name, None
            except Exception as exc:  # noqa: BLE001
                return None, name, "could not read briefing (%s)" % exc
    return None, None, None


# ------------------------------------------------------- registry helpers ---
def registry_index():
    """Upper-cased agent_name -> registry row. Returns (index, error)."""
    agents, err = load_agents()
    if err or not agents:
        return None, err or "Agent Registry sheet is unreachable."
    idx = {}
    for a in agents:
        name = str(a.get("agent_name") or "").strip()
        if name and name.upper() not in idx:
            idx[name.upper()] = a
    return idx, None


def validate_assignee(agent_name):
    """Returns (row, error). Blocks unknown, spec-only, and PAUSED agents."""
    if not agent_name or not agent_name.strip():
        return None, "Agent is required."
    idx, err = registry_index()
    if err:
        return None, err
    row = idx.get(agent_name.strip().upper())
    if row is None:
        return None, "Unknown agent '%s' — choose one from the registry." % agent_name.strip()
    if row.get("spec_only"):
        return None, "%s is in the spec but not registered in the sheet yet." % row.get("agent_name")
    if norm_status(row.get("status")) == "PAUSED":
        return None, "%s is PAUSED — activate it before assigning work." % row.get("agent_name")
    return row, None


# ------------------------------------------------------------------ login ---
@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "error": None, "hide_nav": True,
    })


@app.post("/login", include_in_schema=False)
def login_submit(request: Request, username: str = Form(""),
                 password: str = Form("")):
    if username.strip().lower() == LOGIN_USER and _verify_password(password):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE,
                        _serializer.dumps({"u": LOGIN_USER}),
                        httponly=True, samesite="lax",
                        max_age=SESSION_MAX_AGE)
        return resp
    return templates.TemplateResponse(request, "login.html", {
        "request": request,
        "error": "Invalid username or password.",
        "hide_nav": True,
    }, status_code=401)


@app.get("/logout", include_in_schema=False)
def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ------------------------------------------------------------------ pages ---
PAGE_TITLES = {
    "overview": "Dashboard Overview",
    "live": "Live Workflow",
    "agents": "Agents",
    "teams": "Company Teams",
    "tasks": "Task Board",
    "approvals": "Approvals",
    "activity": "Activity Log",
}


def chrome(request, active, tasks, extra):
    """Shared topbar/sidebar context for every page."""
    d = {
        "request": request, "logged_in": True, "active": active,
        "page_title": PAGE_TITLES.get(active, "Mission Control"),
        "today_str": datetime.now().strftime("%A, %B %d, %Y"),
        "approval_count": sum(1 for t in (tasks or [])
                              if is_approval_status(t.get("status"))),
        "open_count": sum(1 for t in (tasks or [])
                          if norm_status(t.get("status")) in TASK_OPEN_STATES),
    }
    d.update(extra)
    return d


def _page_guard(request):
    return need_login_page(request)


@app.get("/", include_in_schema=False)
def overview(request: Request):
    guard = _page_guard(request)
    if guard:
        return guard
    agents, agents_err = load_agents()
    tasks, tasks_err = load_tasks()
    leads, leads_err = load_leads()

    errors = [e for e in (agents_err, tasks_err, leads_err) if e]

    ym = datetime.now().strftime("%Y-%m")
    targets = load_targets()
    target = targets.get(ym, 10000.0)
    rev_actual, rev_won_count = month_revenue(leads, ym)
    pct = min(100.0, (rev_actual / target) * 100) if target else 0

    dindex = dept_key_index(agents)
    today = datetime.now().date()
    week_ago = today.toordinal() - 7

    def done_in_days(t, days):
        if norm_status(t.get("status")) != "DONE":
            return False
        ca = str(t.get("completed_at") or "")[:10]
        try:
            return 0 <= (today - datetime.strptime(ca, "%Y-%m-%d").date()).days < days
        except ValueError:
            return False

    cards = []
    for card in DEPT_CARDS:
        dept_agents = [a for a in (agents or [])
                       if a.get("department") in card["registry_depts"]]
        active = [a for a in dept_agents
                  if str(a.get("status") or "").strip().upper() == "ACTIVE"]
        open_tasks = [t for t in (tasks or [])
                      if dindex.get(str(t.get("assigned_to") or "").strip().upper()) == card["key"]
                      and norm_status(t.get("status")) in TASK_OPEN_STATES]
        done7 = [t for t in (tasks or [])
                 if dindex.get(str(t.get("assigned_to") or "").strip().upper()) == card["key"]
                 and done_in_days(t, 7)]
        pend_appr = [t for t in (tasks or [])
                     if dindex.get(str(t.get("assigned_to") or "").strip().upper()) == card["key"]
                     and is_approval_status(t.get("status"))]
        pts = spark_points(tasks, dindex, card["key"])
        cards.append({
            "key": card["key"], "label": card["label"], "color": card["color"],
            "icon": card["icon"], "blurb": card["blurb"],
            "agents": len(dept_agents), "active": len(active),
            "open_tasks": len(open_tasks), "done7": len(done7),
            "pending_appr": len(pend_appr),
            "spark": spark_svg_points(pts),
        })

    reg_total = len(agents or [])
    active_total = sum(1 for a in (agents or [])
                       if str(a.get("status") or "").strip().upper() == "ACTIVE")
    open_total = sum(1 for t in (tasks or [])
                     if norm_status(t.get("status")) in TASK_OPEN_STATES)

    reports, reports_err = reports_listing()
    if reports_err:
        errors.append(reports_err)
    today_reports = [r for r in reports if r["is_today"]]

    # (a) tasks by status — actual status values found in the Tasks sheet
    tasks_by_status = []
    for key, title, items in group_tasks(tasks):
        tasks_by_status.append({
            "key": key, "title": title, "count": len(items),
            "css": "st-" + key.lower(),
        })
    # (b) drafts/messages awaiting Tahir's approval (with counts)
    approval_items = [t for t in (tasks or [])
                      if is_approval_status(t.get("status"))]
    # (c) tasks snapshot strip — top open tasks by priority
    def prio_rank(t):
        return PRIORITY_RANK.get(norm_status(t.get("priority")), 4)
    snapshot = sorted(
        [t for t in (tasks or [])
         if norm_status(t.get("status")) in TASK_OPEN_STATES],
        key=lambda t: (prio_rank(t), str(t.get("created_at") or "")),
    )[:6]

    return templates.TemplateResponse(request, "overview.html", chrome(
        request, "overview", tasks, {
            "errors": errors,
            "target": target, "target_ym": ym,
            "target_label": month_label(ym) + " Target",
            "actual": rev_actual, "won_count": rev_won_count, "pct": pct,
            "targets_map": targets,
            "cards": cards,
            "reg_total": reg_total, "active_total": active_total,
            "open_total": open_total,
            "tasks_by_status": tasks_by_status,
            "approval_items": approval_items,
            "approval_count": len(approval_items),
            "reports": reports[:8], "today_reports": today_reports,
            "latest_reports": reports[:5],
            "snapshot": snapshot,
        }))


@app.get("/agents", include_in_schema=False)
def agents_page(request: Request):
    guard = _page_guard(request)
    if guard:
        return guard
    agents, agents_err = load_agents()
    agents = agents or []

    dept_filter = (request.query_params.get("department") or "").strip()
    status_filter = (request.query_params.get("status") or "").strip().upper()

    all_depts = sorted({str(a.get("department") or "—") for a in agents})
    all_statuses = sorted({str(a.get("status") or "—").strip().upper() for a in agents})

    rows = agents
    if dept_filter:
        rows = [a for a in rows if str(a.get("department") or "") == dept_filter]
    if status_filter:
        rows = [a for a in rows
                if str(a.get("status") or "").strip().upper() == status_filter]

    tasks, tasks_err = load_tasks()
    return templates.TemplateResponse(request, "agents.html", chrome(
        request, "agents", tasks, {
            "errors": [e for e in (agents_err, tasks_err) if e],
            "rows": rows, "total": len(agents),
            "all_depts": all_depts, "all_statuses": all_statuses,
            "dept_filter": dept_filter, "status_filter": status_filter,
            "spec_total": 55,  # ORG_CHART v2 locked spec
        }))


@app.get("/teams", include_in_schema=False)
def teams_page(request: Request):
    guard = _page_guard(request)
    if guard:
        return guard
    agents, agents_err = load_agents()
    tasks, tasks_err = load_tasks()
    status_filter = (request.query_params.get("status") or "").strip().upper()

    sections = team_sections_data(agents, tasks)
    if status_filter:
        filtered = []
        for s in sections:
            members = [a for a in s["members"]
                       if str(a.get("status") or "").strip().upper() == status_filter]
            if members:
                filtered.append({**s, "members": members,
                                 "active": sum(1 for a in members
                                               if str(a.get("status") or "") == "ACTIVE")})
        sections = filtered
    total = sum(len(s["members"]) for s in sections)

    return templates.TemplateResponse(request, "teams.html", chrome(
        request, "teams", tasks, {
            "errors": [e for e in (agents_err, tasks_err) if e],
            "sections": sections, "total": total,
            "status_filter": status_filter,
        }))


@app.get("/tasks", include_in_schema=False)
def tasks_page(request: Request):
    guard = _page_guard(request)
    if guard:
        return guard
    tasks, tasks_err = load_tasks()
    groups = group_tasks(tasks)
    total = len(tasks or [])
    open_count = sum(1 for t in (tasks or [])
                     if norm_status(t.get("status")) in TASK_OPEN_STATES)
    return templates.TemplateResponse(request, "tasks.html", chrome(
        request, "tasks", tasks, {
            "errors": [tasks_err] if tasks_err else [],
            "groups": groups, "total": total, "open_count": open_count,
        }))


@app.get("/approvals", include_in_schema=False)
def approvals_page(request: Request):
    guard = _page_guard(request)
    if guard:
        return guard
    tasks, tasks_err = load_tasks()
    pending = [t for t in (tasks or []) if is_approval_status(t.get("status"))]

    def prio_rank(t):
        return PRIORITY_RANK.get(norm_status(t.get("priority")), 4)
    pending.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)
    pending.sort(key=prio_rank)
    return templates.TemplateResponse(request, "approvals.html", chrome(
        request, "approvals", tasks, {
            "errors": [tasks_err] if tasks_err else [],
            "pending": pending,
        }))


@app.get("/activity", include_in_schema=False)
def activity_page(request: Request):
    guard = _page_guard(request)
    if guard:
        return guard
    reports, reports_err = reports_listing()
    briefing, briefing_name, briefing_err = read_briefing()
    whatsapp, wa_err = None, None
    wa_path = os.path.join(REPORTS_DIR, "daily-brief-whatsapp.txt")
    if os.path.isfile(wa_path):
        try:
            with open(wa_path, "r", encoding="utf-8") as fh:
                whatsapp = fh.read()
        except Exception as exc:  # noqa: BLE001
            wa_err = "could not read WhatsApp briefing (%s)" % exc
    errors = [e for e in (reports_err, briefing_err, wa_err) if e]
    return templates.TemplateResponse(request, "activity.html", chrome(
        request, "activity", None, {
            "errors": errors,
            "reports": reports,
            "briefing": briefing, "briefing_name": briefing_name,
            "whatsapp": whatsapp, "wa_len": len(whatsapp) if whatsapp else 0,
        }))


# -------------------------------------------------------------------- api ---
MSG_PREFIX = "Direct message from Tahir: "


# ------------------------------------------------------------- live data ---
_TS_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _parse_ts(value):
    """Parse a sheet timestamp to datetime. None when unparseable — never faked."""
    s = str(value or "").strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _rel_time(value):
    """Honest relative time ('5m ago', '2h ago', '3d ago')."""
    dt = _parse_ts(value)
    if not dt:
        return "no recorded activity"
    secs = int((datetime.now() - dt).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return "%dm ago" % (secs // 60)
    if secs < 86400:
        return "%dh ago" % (secs // 3600)
    days = secs // 86400
    if days < 30:
        return "%dd ago" % days
    return dt.strftime("%Y-%m-%d")


def build_live_data():
    """Everything on /live, all from real sheet data. No invented activity."""
    agents, agents_err = load_agents()
    tasks, tasks_err = load_tasks()
    errors = [e for e in (agents_err, tasks_err) if e]
    agents = agents or []
    tasks = tasks or []
    dindex = dept_key_index(agents)
    reg = {str(a.get("agent_name") or "").strip().upper(): a for a in agents}

    def dept_of(t):
        dep = str(t.get("department") or "").strip()
        if dep:
            return dep
        a = reg.get(str(t.get("assigned_to") or "").strip().upper())
        return str(a.get("department") or "") if a else ""

    # 1 — now working: tasks with status IN PROGRESS
    inprog = [t for t in tasks if norm_status(t.get("status")) == "IN_PROGRESS"]
    inprog.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)
    now_working = [{
        "agent": str(t.get("assigned_to") or ""),
        "department": dept_of(t),
        "task_id": str(t.get("task_id") or ""),
        "objective": str(t.get("objective") or ""),
        "since": _rel_time(t.get("created_at")),
    } for t in inprog]

    # 2 — dispatch feed: latest 20 assignments
    ordered = sorted(tasks, key=lambda t: str(t.get("created_at") or ""),
                     reverse=True)[:20]
    dispatch = [{
        "task_id": str(t.get("task_id") or ""),
        "created_by": str(t.get("created_by") or ""),
        "assigned_to": str(t.get("assigned_to") or ""),
        "department": dept_of(t),
        "objective": str(t.get("objective") or ""),
        "status": norm_status(t.get("status")),
        "when": _rel_time(t.get("created_at")),
    } for t in ordered]

    # 3 — message feed: latest 15 direct-message rows (+ real outputs only)
    msgs = [t for t in tasks
            if str(t.get("objective") or "").startswith(MSG_PREFIX)]
    msgs.sort(key=lambda t: str(t.get("created_at") or ""), reverse=True)
    message_feed = []
    for t in msgs[:15]:
        agent = str(t.get("assigned_to") or "")
        message_feed.append({
            "task_id": str(t.get("task_id") or ""),
            "agent": agent,
            "direction": "out",
            "text": str(t.get("objective") or "")[len(MSG_PREFIX):],
            "when": _rel_time(t.get("created_at")),
        })
        out = str(t.get("output") or "").strip()
        if out:  # agent reply — ONLY when the sheet genuinely has output
            message_feed.append({
                "task_id": str(t.get("task_id") or ""),
                "agent": agent,
                "direction": "in",
                "text": out,
                "when": _rel_time(t.get("completed_at") or t.get("created_at")),
            })

    # 4 — agent pulse: last real activity per agent from task timestamps
    last = {}
    for t in tasks:
        name = str(t.get("assigned_to") or "").strip()
        if not name:
            continue
        ts = _parse_ts(t.get("completed_at")) or _parse_ts(t.get("created_at"))
        if ts:
            key = name.upper()
            if key not in last or ts > last[key][0]:
                last[key] = (ts, name)
    pulse_rows = []
    for a in agents:
        name = str(a.get("agent_name") or "")
        ent = last.get(name.strip().upper())
        pulse_rows.append({
            "sort_ts": ent[0] if ent else None,
            "agent": name,
            "department": str(a.get("department") or ""),
            "status": str(a.get("status") or "").strip().upper(),
            "last_activity": _rel_time(ent[0].strftime("%Y-%m-%d %H:%M"))
                             if ent else "no recorded activity",
        })
    pulse_rows.sort(key=lambda r: (r["sort_ts"] is None,
                                   -(r["sort_ts"].timestamp()
                                     if r["sort_ts"] else 0),
                                   r["agent"]))
    for r in pulse_rows:
        r.pop("sort_ts", None)

    return {"now_working": now_working, "dispatch_feed": dispatch,
            "message_feed": message_feed, "agent_pulse": pulse_rows,
            "errors": errors,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/live", include_in_schema=False)
def live_page(request: Request):
    guard = _page_guard(request)
    if guard:
        return guard
    tasks, tasks_err = load_tasks()
    errors = [e for e in (tasks_err,) if e]
    return templates.TemplateResponse(request, "live.html", chrome(
        request, "live", tasks, {"errors": errors}))


@app.get("/api/live", include_in_schema=False)
def api_live(request: Request):
    denied = need_login_api(request)
    if denied:
        return denied
    return {"ok": True, "live": build_live_data()}


@app.post("/api/assign-task", include_in_schema=False)
async def api_assign_task(request: Request):
    denied = need_login_api(request)
    if denied:
        return denied
    try:
        data = await request.json()
    except Exception:
        return api_error("Invalid JSON body.", 400)

    agent = str(data.get("agent") or "").strip()
    objective = str(data.get("objective") or "").strip()
    priority = str(data.get("priority") or "MEDIUM").strip().upper()
    deadline = str(data.get("deadline") or "").strip()
    input_text = str(data.get("input_text") or "").strip()

    if not objective:
        return api_error("Objective is required.")
    if len(objective) > 2000:
        return api_error("Objective is too long (max 2000 characters).")
    if priority not in {"LOW", "MEDIUM", "HIGH"}:
        return api_error("Priority must be LOW, MEDIUM or HIGH.")
    if deadline and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
        return api_error("Deadline must be a date in YYYY-MM-DD format.")

    row, err = validate_assignee(agent)
    if err:
        return api_error(err, 400 if "Unknown agent" in err or "PAUSED" in err
                         or "not registered" in err else 500)

    try:
        tid = sheets_db.add_task(
            assigned_to=str(row.get("agent_name")),
            objective=objective,
            department=str(row.get("department") or ""),
            priority=priority,
            created_by="Tahir (dashboard)",
            input_text=input_text,
            deadline=deadline,
        )
    except Exception as exc:  # noqa: BLE001 — real error, never fake success
        return api_error("Sheet write failed: %s" % exc, 500)
    invalidate("tasks")
    return {"ok": True, "task_id": tid}


@app.post("/api/message", include_in_schema=False)
async def api_message(request: Request):
    """Save a direct message to an agent as a Tasks-sheet row."""
    denied = need_login_api(request)
    if denied:
        return denied
    try:
        data = await request.json()
    except Exception:
        return api_error("Invalid JSON body.", 400)

    agent = str(data.get("agent") or "").strip()
    text = str(data.get("text") or "").strip()
    if not text:
        return api_error("Message text is required.")
    if len(text) > 2000:
        return api_error("Message is too long (max 2000 characters).")

    row, err = validate_assignee(agent)
    if err:
        return api_error(err, 400 if "Unknown agent" in err or "PAUSED" in err
                         or "not registered" in err else 500)

    try:
        tid = sheets_db.add_task(
            assigned_to=str(row.get("agent_name")),
            objective=MSG_PREFIX + text,
            department=str(row.get("department") or ""),
            priority="MEDIUM",
            created_by="Tahir (dashboard)",
        )
    except Exception as exc:  # noqa: BLE001
        return api_error("Sheet write failed: %s" % exc, 500)
    invalidate("tasks")
    return {"ok": True, "task_id": tid}


@app.get("/api/thread", include_in_schema=False)
def api_thread(request: Request, agent: str = ""):
    """Message thread for one agent: Tahir's direct messages + real outputs.

    Replies appear ONLY when the sheet has real task output — never simulated.
    """
    denied = need_login_api(request)
    if denied:
        return denied
    tasks, err = load_tasks()
    if err:
        return api_error("Could not read tasks: %s" % err, 500)
    want = agent.strip().upper()
    rows = []
    for t in (tasks or []):
        if str(t.get("assigned_to") or "").strip().upper() != want:
            continue
        if str(t.get("created_by") or "") != "Tahir (dashboard)":
            continue
        obj = str(t.get("objective") or "")
        if not obj.startswith(MSG_PREFIX):
            continue
        rows.append({
            "task_id": t.get("task_id"),
            "text": obj[len(MSG_PREFIX):],
            "created_at": t.get("created_at"),
            "status": t.get("status"),
            "output": t.get("output") or "",
        })
    rows.sort(key=lambda r: str(r.get("created_at") or ""))
    return {"ok": True, "messages": rows}


@app.get("/api/meta", include_in_schema=False)
def api_meta(request: Request):
    """Departments + agent names for the Add-Agent / Assign dropdowns."""
    denied = need_login_api(request)
    if denied:
        return denied
    agents, err = load_agents()
    if err:
        return api_error("Could not read registry: %s" % err, 500)
    depts = sorted({str(a.get("department") or "").strip()
                    for a in (agents or []) if str(a.get("department") or "").strip()})
    names = sorted({str(a.get("agent_name") or "").strip()
                    for a in (agents or []) if str(a.get("agent_name") or "").strip()})
    return {"ok": True, "departments": depts, "agents": names}


@app.get("/api/targets", include_in_schema=False)
def api_targets_get(request: Request):
    """Monthly revenue targets (from dashboard/targets.json)."""
    denied = need_login_api(request)
    if denied:
        return denied
    return {"ok": True, "targets": load_targets()}


@app.post("/api/targets", include_in_schema=False)
async def api_targets_set(request: Request):
    """Set a month's target. Tahir edits these from the Overview card."""
    denied = need_login_api(request)
    if denied:
        return denied
    try:
        data = await request.json()
    except Exception:
        return api_error("Invalid JSON body.", 400)
    month = str(data.get("month") or "").strip()
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return api_error("Amount must be a number.")
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        return api_error("Month must be YYYY-MM.")
    if amount < 0 or amount > 100_000_000:
        return api_error("Amount looks wrong — must be between 0 and 100,000,000.")
    try:
        targets = load_targets()
        targets[month] = amount
        saved = save_targets(targets)
    except ValueError as exc:
        return api_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return api_error("Could not save targets: %s" % exc, 500)
    resp = {"ok": True, "targets": saved}
    if os.environ.get("TARGETS_JSON", "").strip():
        resp["ephemeral"] = ("TARGETS_JSON env var is set: this edit applies "
                             "until the next restart/redeploy.")
    return resp


SOP_TEMPLATE = """# SOP — {name} (IconicNewswire)

**Role:** {role}
**Reports to:** {reports_to}.
**Status:** {status}. (Added via the Mission Control dashboard by Tahir on {date}.)

## Inputs
- Tasks assigned via the Tasks sheet (created by Tahir or the Dispatcher Agent).
- {department}-specific context and assets as the task requires.

## Operating procedure

1. **Read the task first.** Open the assigned Tasks-sheet row: objective,
   input, deadline, priority. If anything is ambiguous, ask ONE short
   clarifying question — never guess.
2. **Do the work in the open.** Write every output where the SOP's Outputs
   section says it goes (sheet, file path, or draft folder) — never keep
   results only in chat.
3. **Update the task row.** When done, record the result in the task's output
   field; on blockers, record them in the error field and escalate.
4. **Drafts-first.** Nothing sends, publishes, or goes live without Tahir's
   explicit approval of the exact content.

## Outputs (where they get written)
- Deliverables as specified per task; drafts live with their review notes so
  approval sees content + context together.

## Hard rules (inherited, no exceptions)
- Drafts-first: nothing sends/publishes without Tahir's explicit approval.
- NEVER invent metrics, outlets, people, results, or reach. Sheets = only lead DB.
- No agent speaks as Tahir. No impersonation in any message.
- LinkedIn: only the LinkedIn Prospect Hunter touches LinkedIn, per its SOP caps.
  No other agent uses LinkedIn in any way.
- WhatsApp: warm groups/contacts only, max 10/day.
- No bulk scraping, no ToS violations.

## KPIs
- {kpis}
"""


@app.post("/api/add-agent", include_in_schema=False)
async def api_add_agent(request: Request):
    """Admin: register a new agent (registry row + SOP file)."""
    denied = need_login_api(request)
    if denied:
        return denied
    try:
        data = await request.json()
    except Exception:
        return api_error("Invalid JSON body.", 400)

    name = str(data.get("name") or "").strip()
    department = str(data.get("department") or "").strip()
    reports_to = str(data.get("reports_to") or "").strip()
    role = str(data.get("role_summary") or "").strip()
    kpis = str(data.get("kpis") or "").strip()
    status = str(data.get("status") or "STANDBY").strip().upper()

    if not name:
        return api_error("Agent name is required.")
    if len(name) > 80:
        return api_error("Agent name is too long (max 80 characters).")
    if not department:
        return api_error("Department is required.")
    if not reports_to:
        return api_error("Reports-to is required.")
    if not role:
        return api_error("Role summary is required.")
    if status not in {"ACTIVE", "STANDBY"}:
        return api_error("Status must be ACTIVE or STANDBY.")

    idx, err = registry_index()
    if err:
        return api_error(err, 500)
    if name.upper() in idx:
        return api_error("An agent named '%s' already exists." % name)

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        return api_error("Could not derive a usable id from that name.")
    sop_dir = os.path.join(AGENTS_DIR, slug)
    if os.path.exists(sop_dir):
        return api_error("Agent id '%s' is already taken." % slug)

    valid_depts = {str(a.get("department") or "") for a in idx.values()}
    if department not in valid_depts:
        return api_error("Unknown department '%s'." % department)
    valid_bosses = {str(a.get("agent_name") or "") for a in idx.values()}
    if reports_to not in valid_bosses and reports_to != "Iconic AI CEO":
        return api_error("Reports-to must be an existing agent or 'Iconic AI CEO'.")

    # (a) registry row
    try:
        sheets_db.append_registry_row({
            "agent_name": name,
            "department": department,
            "reports_to": reports_to,
            "autonomy_level": "2 - Draft",
            "kpis": kpis,
            "status": status,
            "notes": "Added via Mission Control dashboard by Tahir on %s."
                     % datetime.now().strftime("%Y-%m-%d"),
        })
    except Exception as exc:  # noqa: BLE001
        return api_error("Registry write failed: %s" % exc, 500)

    # (b) SOP file
    try:
        os.makedirs(sop_dir, exist_ok=False)
        with open(os.path.join(sop_dir, "SOP.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(SOP_TEMPLATE.format(
                name=name, role=role, reports_to=reports_to,
                status=status, date=datetime.now().strftime("%Y-%m-%d"),
                department=department, kpis=kpis or "To be defined with Tahir.",
            ))
    except Exception as exc:  # noqa: BLE001
        return api_error("SOP write failed after registry row was added: %s. "
                         "Remove the registry row manually." % exc, 500)

    invalidate("agents")
    return {"ok": True, "slug": slug, "name": name}
