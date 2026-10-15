"""APEX Logistics API - Zoho Catalyst Advanced I/O function (Python).

Served at  /server/apex_api/...  on the same domain as the web client, so no CORS setup is needed.

Routes
  GET   /track/<trackingId>             public
  POST  /bookings                       public
  POST  /admin/login                    public  -> signed admin token
  GET   /admin/dashboard                admin token
  PATCH /admin/shipments/<trackingId>   admin token

Pricing rules MUST match CONFIG.PRICING in client/index.html. The server recomputes every price.

Data Store tables (create in the Catalyst console, see README):  Shipments, Fleet
Admin login is configured with three variables (ADMIN_EMAIL, ADMIN_PASSWORD_HASH, TOKEN_SECRET).
"""
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import zcatalyst_sdk
from flask import Request, make_response

try:  # optional fallback if function-level environment variables are not available
    import config as _cfg
except ImportError:
    _cfg = None

log = logging.getLogger()


def setting(name):
    return os.getenv(name) or (getattr(_cfg, name, "") if _cfg else "")


GST_PCT = 0.18
MODES = {
    "road":  dict(base=1800, per=5.5, min_charge=3500,  fuel=0.09, circ=1.25, t_base=1, kmpd=450,  min_kg=1,   max_kg=40000,  label="Road"),
    "ocean": dict(base=9000, per=2.2, min_charge=15000, fuel=0.06, circ=1.35, t_base=6, kmpd=700,  min_kg=100, max_kg=500000, label="Ocean"),
    "air":   dict(base=6000, per=9.0, min_charge=8000,  fuel=0.12, circ=1.0,  t_base=2, kmpd=7000, min_kg=1,   max_kg=5000,   label="Air"),
}
HUBS = {h[0]: dict(id=h[0], country=h[1], lat=h[2], lon=h[3], port=h[4]) for h in [
    ("mumbai", "IN", 19.08, 72.88, True), ("delhi", "IN", 28.61, 77.21, False),
    ("chennai", "IN", 13.08, 80.27, True), ("kolkata", "IN", 22.57, 88.36, True),
    ("bengaluru", "IN", 12.97, 77.59, False), ("hyderabad", "IN", 17.39, 78.49, False),
    ("bhubaneswar", "IN", 20.30, 85.82, False), ("paradeep", "IN", 20.26, 86.67, True),
    ("visakhapatnam", "IN", 17.69, 83.22, True), ("kochi", "IN", 9.93, 76.27, True),
    ("singapore", "SG", 1.35, 103.82, True), ("dubai", "AE", 25.20, 55.27, True),
    ("shanghai", "CN", 31.23, 121.47, True), ("hongkong", "HK", 22.32, 114.17, True),
    ("rotterdam", "NL", 51.92, 4.48, True), ("london", "GB", 51.51, -0.13, True),
    ("frankfurt", "DE", 50.11, 8.68, False), ("losangeles", "US", 34.05, -118.24, True),
    ("newyork", "US", 40.71, -74.01, True),
]}
HUB_NAMES = {"paradeep": "Paradeep Port", "hongkong": "Hong Kong", "losangeles": "Los Angeles", "newyork": "New York"}
STATUSES = ["Pending", "Confirmed", "In Transit", "At Hub", "Out for Delivery", "Delivered", "Delayed", "Cancelled"]
ACTIVE = {"Confirmed", "In Transit", "At Hub", "Out for Delivery", "Delayed"}

TRACKING_RE = re.compile(r"^APX-\d{8}-[A-Z0-9]{8}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TOKEN_TTL = 3600
PUBLIC_FIELDS = ("trackingId", "status", "customerName", "origin", "destination", "currentLocation",
                 "mode", "weightKg", "estimatedDelivery", "createdAt")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def hub_name(hub_id):
    return HUB_NAMES.get(hub_id, hub_id.capitalize())


def js_round(x):
    return int(math.floor(x + 0.5))


def haversine(a, b):
    r = 6371
    d_lat, d_lon = math.radians(b["lat"] - a["lat"]), math.radians(b["lon"] - a["lon"])
    h = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(a["lat"])) * math.cos(math.radians(b["lat"])) * math.sin(d_lon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def estimate(origin, destination, mode, weight):
    o, d, rule = HUBS.get(origin), HUBS.get(destination), MODES.get(mode)
    if not o or not d:
        raise ApiError(400, "Select an origin and a destination.")
    if o["id"] == d["id"]:
        raise ApiError(400, "Origin and destination must be different.")
    if not rule:
        raise ApiError(400, "Select a transport mode.")
    if not weight > 0:
        raise ApiError(400, "Enter the cargo weight in kg.")
    if weight < rule["min_kg"] or weight > rule["max_kg"]:
        raise ApiError(400, f"{rule['label']} freight accepts {rule['min_kg']:,} to {rule['max_kg']:,} kg.")
    if mode == "road" and not (o["country"] == "IN" and d["country"] == "IN"):
        raise ApiError(400, "Road freight covers domestic (India) lanes only. Choose air or ocean for this lane.")
    if mode == "ocean" and not (o["port"] and d["port"]):
        raise ApiError(400, "Ocean freight needs a port at both ends. Pick port cities such as Mumbai or Paradeep, or choose air.")
    km = js_round(haversine(o, d) * rule["circ"])
    freight = js_round(max(rule["min_charge"], rule["base"] + rule["per"] * weight * (km / 1000)))
    fuel = js_round(freight * rule["fuel"])
    gst = js_round((freight + fuel) * GST_PCT)
    return dict(total=freight + fuel + gst, transit_days=rule["t_base"] + math.ceil(km / rule["kmpd"]))


# ---------------------------------------------------------------- admin auth (HMAC-signed token)
def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def check_password(password):
    stored = setting("ADMIN_PASSWORD_HASH")  # pbkdf2$iterations$salt$hash  (see scripts/make_admin_hash.py)
    try:
        _, iters, salt, want = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), unb64(salt), int(iters))
        return hmac.compare_digest(got, unb64(want))
    except ValueError:
        return False


def sign(payload):
    body = b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(setting("TOKEN_SECRET").encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{b64(sig)}"


def require_admin(request):
    token = request.headers.get("X-Apex-Token") or request.headers.get("Authorization") or ""
    secret = setting("TOKEN_SECRET")
    try:
        body, sig = token.split(".")
        want = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
        ok = bool(secret) and hmac.compare_digest(unb64(sig), want)
        if ok and json.loads(unb64(body))["exp"] > time.time():
            return
    except (ValueError, KeyError, TypeError):
        pass
    raise ApiError(401, "Your session has expired. Sign in again.")


# ---------------------------------------------------------------- data helpers
def parse_body(request):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "Request body must be a JSON object.")
    return data


def zcql_rows(app, query, table):
    """ZCQL returns [{'Table': {...columns...}}, ...] - unwrap it."""
    return [r[table] for r in app.zcql().execute_query(query)]


def fetch_all(app, table):
    rows, offset = [], 0
    while True:  # ZCQL returns at most 300 rows per query
        page = zcql_rows(app, f"SELECT * FROM {table} LIMIT {offset},300", table)
        rows += page
        if len(page) < 300:
            return rows
        offset += 300


def clean_shipment(r):
    out = dict(r)
    out["estimatedCost"] = int(float(out.get("estimatedCost") or 0))
    out["weightKg"] = float(out.get("weightKg") or 0)
    if out["weightKg"].is_integer():
        out["weightKg"] = int(out["weightKg"])
    return out


def find_shipment(app, tid):
    rows = zcql_rows(app, f"SELECT * FROM Shipments WHERE trackingId = '{tid}'", "Shipments")  # tid is regex-validated
    return rows[0] if rows else None


# ---------------------------------------------------------------- handlers
def track(app, request, tid):
    tid = tid.upper()
    if not TRACKING_RE.match(tid):
        raise ApiError(400, "Tracking IDs look like APX-20261012-AB12CD34.")
    row = find_shipment(app, tid)
    if not row:
        raise ApiError(404, "No shipment found for that tracking ID.")
    row = clean_shipment(row)
    return 200, {k: row[k] for k in PUBLIC_FIELDS if k in row}


def book(app, request):
    b = parse_body(request)
    name, email = str(b.get("customerName", "")).strip(), str(b.get("email", "")).strip().lower()
    if not 2 <= len(name) <= 80:
        raise ApiError(400, "Customer name must be 2-80 characters.")
    if not EMAIL_RE.match(email) or len(email) > 120:
        raise ApiError(400, "Enter a valid email address.")
    try:
        weight = float(b.get("weightKg"))
    except (TypeError, ValueError):
        raise ApiError(400, "Enter the cargo weight in kg.")
    q = estimate(b.get("origin"), b.get("destination"), b.get("mode"), weight)
    now = datetime.now(timezone.utc)
    tid = next((t for t in (f"APX-{now:%Y%m%d}-" + "".join(secrets.choice(ID_ALPHABET) for _ in range(8)) for _ in range(5))
                if not find_shipment(app, t)), None)
    if not tid:
        raise ApiError(500, "Could not allocate a tracking ID. Try again.")
    row = {"trackingId": tid, "status": "Pending", "customerName": name, "email": email,
           "origin": b["origin"], "destination": b["destination"], "currentLocation": hub_name(b["origin"]),
           "mode": b["mode"], "weightKg": weight, "estimatedCost": q["total"],
           "estimatedDelivery": (now + timedelta(days=q["transit_days"])).date().isoformat(),
           "createdAt": now.isoformat(), "updatedAt": now.isoformat()}
    app.datastore().table("Shipments").insert_row(row)
    return 201, {k: row[k] for k in ("trackingId", "status", "estimatedCost", "estimatedDelivery", "createdAt")}


def login(app, request):
    b = parse_body(request)
    email, password = str(b.get("email", "")).strip().lower(), str(b.get("password", ""))
    admin = setting("ADMIN_EMAIL").strip().lower()
    ok = bool(admin) and hmac.compare_digest(email.encode(), admin.encode())
    if not (check_password(password) and ok):  # always run the hash check
        time.sleep(0.6)
        raise ApiError(401, "Invalid email or password.")
    return 200, {"idToken": sign({"sub": email, "exp": int(time.time()) + TOKEN_TTL}), "expiresIn": TOKEN_TTL}


def dashboard(app, request):
    require_admin(request)
    items = sorted((clean_shipment(r) for r in fetch_all(app, "Shipments")), key=lambda s: s["createdAt"], reverse=True)
    fleet = sorted(fetch_all(app, "Fleet"), key=lambda v: v["vehicleId"])
    now = datetime.now(timezone.utc)
    months, rev = [], {}
    for i in range(5, -1, -1):
        y, m = now.year, now.month - i
        while m < 1:
            m, y = m + 12, y - 1
        months.append((f"{y}-{m:02d}", datetime(y, m, 1).strftime("%b %y")))
        rev[months[-1][0]] = 0
    modes, total, pending, active = {"road": 0, "ocean": 0, "air": 0}, 0, 0, 0
    for s in items:
        modes[s["mode"]] = modes.get(s["mode"], 0) + 1
        if s["status"] == "Cancelled":
            continue
        total += s["estimatedCost"]
        if s["createdAt"][:7] in rev:
            rev[s["createdAt"][:7]] += s["estimatedCost"]
        pending += s["status"] == "Pending"
        active += s["status"] in ACTIVE
    summary = {"totalRevenue": total, "monthRevenue": rev[months[-1][0]], "pendingCount": pending, "activeCount": active,
               "modeBreakdown": modes, "monthlyRevenue": [{"month": l, "revenue": rev[k]} for k, l in months]}
    return 200, {"summary": summary, "shipments": items, "fleet": fleet}


def update_shipment(app, request, tid):
    require_admin(request)
    if not TRACKING_RE.match(tid):
        raise ApiError(400, "Invalid tracking ID.")
    b = parse_body(request)
    changes = {}
    if "status" in b:
        if b["status"] not in STATUSES:
            raise ApiError(400, "Unknown status.")
        changes["status"] = b["status"]
    if "currentLocation" in b:
        loc = str(b["currentLocation"]).strip()
        if not 1 <= len(loc) <= 120:
            raise ApiError(400, "Location must be 1-120 characters.")
        changes["currentLocation"] = loc
    if not changes:
        raise ApiError(400, "Nothing to update.")
    row = find_shipment(app, tid)
    if not row:
        raise ApiError(404, "Shipment not found.")
    changes["updatedAt"] = datetime.now(timezone.utc).isoformat()
    app.datastore().table("Shipments").update_row({"ROWID": row["ROWID"], **changes})
    return 200, clean_shipment({**row, **changes})


def route(app, request):
    parts, method = [p for p in request.path.split("/") if p], request.method
    if method == "GET" and len(parts) == 2 and parts[0] == "track":
        return track(app, request, parts[1])
    if method == "POST" and parts == ["bookings"]:
        return book(app, request)
    if method == "POST" and parts == ["admin", "login"]:
        return login(app, request)
    if method == "GET" and parts == ["admin", "dashboard"]:
        return dashboard(app, request)
    if method == "PATCH" and len(parts) == 3 and parts[:2] == ["admin", "shipments"]:
        return update_shipment(app, request, parts[2])
    raise ApiError(404, "Not found.")


def handler(request: Request):
    try:
        status, body = route(zcatalyst_sdk.initialize(), request)
    except ApiError as e:
        status, body = e.status, {"message": e.message}
    except Exception:
        log.exception("unhandled error")
        status, body = 500, {"message": "Something went wrong on our side. Try again."}
    resp = make_response(json.dumps(body), status)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Cache-Control"] = "no-store"
    return resp
