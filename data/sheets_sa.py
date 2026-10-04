"""Google Sheets transport via service account (for PaaS deploys like Render).

Active ONLY when the GOOGLE_SERVICE_ACCOUNT_JSON env var is set. The var must
contain the full JSON of a Google Cloud service-account key
(Google Cloud Console > IAM & Admin > Service Accounts > Keys > JSON).

The service account's client_email must be SHARED on every spreadsheet this
app touches (Lead DB, Tasks, Agent Registry): Viewer role is enough for reads,
Editor is needed for appends. Without sharing, every call fails with 403.

Local dev is untouched: when the env var is absent, sheets_db keeps shelling
out to hatch_gws_cli exactly as before.
"""
import json
import os

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_ENV_VAR = "GOOGLE_SERVICE_ACCOUNT_JSON"


def _raw():
    return os.environ.get(_ENV_VAR, "").strip()


def is_enabled():
    """True when a service-account key is configured via env var."""
    return bool(_raw())


def validate():
    """Parse and sanity-check the configured key.

    Returns the key dict, or None when the env var is unset.
    Raises RuntimeError with a clear, actionable message when the env var
    is set but unusable.
    """
    raw = _raw()
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "%s is set but is not valid JSON (%s). It must be the full "
            "service-account key JSON downloaded from Google Cloud Console > "
            "IAM & Admin > Service Accounts > Keys." % (_ENV_VAR, exc))
    if not isinstance(info, dict) or not info.get("client_email"):
        raise RuntimeError(
            "%s does not look like a service-account key (missing "
            "'client_email'). Download a JSON key from Google Cloud Console > "
            "IAM & Admin > Service Accounts > Keys." % _ENV_VAR)
    if not info.get("private_key"):
        raise RuntimeError(
            "%s is missing 'private_key'. Download a fresh JSON key from "
            "Google Cloud Console > IAM & Admin > Service Accounts > Keys."
            % _ENV_VAR)
    return info


def _service(info):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError(
            "%s is set but the Google client libraries are not installed. "
            "Add google-api-python-client and google-auth to requirements.txt."
            % _ENV_VAR)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=SCOPES)
    # cache_discovery=False: no discovery-doc cache written to disk
    # (matters on read-only/ephemeral PaaS filesystems).
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


_service_singleton = None


def _get_service():
    global _service_singleton
    if _service_singleton is None:
        _service_singleton = _service(validate())
    return _service_singleton


def cli(*args):
    """Drop-in mirror of sheets_db._cli for the service-account path.

    Accepts the same arg shape the CLI wrapper uses:
        ("sheets", "spreadsheets", "values", "get",
         "--params", '{"spreadsheetId": ..., "range": ...}')
        ("sheets", "spreadsheets", "values", "append",
         "--params", '{"spreadsheetId": ..., "range": ..., "valueInputOption": ...}',
         "--json", '{"values": [...]}')
    and executes the equivalent Sheets API v4 call. Only the operations
    sheets_db actually uses (values.get / values.append) are implemented.
    """
    svc = _get_service()
    if len(args) < 4 or tuple(args[0:3]) != ("sheets", "spreadsheets", "values"):
        raise RuntimeError("sheets_sa: unsupported operation %r" % (args[:4],))
    op = args[3]
    params, body = {}, {}
    rest = list(args[4:])
    while rest:
        tok = rest.pop(0)
        if tok == "--params":
            params = json.loads(rest.pop(0))
        elif tok == "--json":
            body = json.loads(rest.pop(0))
    values = svc.spreadsheets().values()
    if op == "get":
        resp = values.get(spreadsheetId=params["spreadsheetId"],
                          range=params["range"]).execute()
        # Mirror the CLI wrapper: callers do d.get("values", []).
        resp.setdefault("values", [])
        return resp
    if op == "append":
        return values.append(
            spreadsheetId=params["spreadsheetId"],
            range=params["range"],
            valueInputOption=params.get("valueInputOption", "USER_ENTERED"),
            body=body).execute()
    raise RuntimeError("sheets_sa: unsupported values operation %r" % op)
