"""Hort Manager - attendance, appointments, meals and daily log (single file).
Run locally: uvicorn main:app --reload
Persistence: set DATABASE_URL (Postgres, e.g. Neon) for permanent storage; without it a local SQLite file is used."""

import csv
import hashlib
import io
import os
import secrets
import time as pytime
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import (JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint,
                        create_engine, text)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

BASE = Path(__file__).parent

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("HORT_TZ", "Europe/Zurich"))
except Exception:  # tz database missing -> fall back to UTC+1
    from datetime import timezone
    TZ = timezone(timedelta(hours=1))


def local_now():
    """Local Hort time (the server itself runs in UTC)."""
    return datetime.now(TZ).replace(tzinfo=None)


DB_URL = os.environ.get("DATABASE_URL", "").strip()
for _prefix in ("postgres://", "postgresql://"):  # newer SQLAlchemy defaults to "psycopg" (v3), so name psycopg2 explicitly
    if DB_URL.startswith(_prefix):
        DB_URL = "postgresql+psycopg2://" + DB_URL[len(_prefix):]
        break
if DB_URL:
    engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=300)
else:  # local / paid-disk fallback: HORT_DB=/var/data/hort.db
    engine = create_engine(f"sqlite:///{os.environ.get('HORT_DB', BASE / 'hort.db')}",
                           connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()

# ---------------------------------------------------------------- models

class Child(Base):
    __tablename__ = "children"
    id = Column(Integer, primary_key=True)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    group_name = Column(String, default="")
    custom_fields = Column(JSON, default=dict)  # arbitrary key-value pairs


class StatusDefinition(Base):
    __tablename__ = "status_definitions"
    id = Column(Integer, primary_key=True)
    label = Column(String, nullable=False)
    color_code = Column(String, default="#64748b")
    is_default = Column(Boolean, default=False)
    icon = Column(String, default="•")


class AppointmentType(Base):
    __tablename__ = "appointment_types"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    color = Column(String, default="#6366f1")
    default_lead_minutes = Column(Integer, default=15)


class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    child_id = Column(Integer, ForeignKey("children.id", ondelete="CASCADE"), nullable=False)
    title = Column(String, nullable=False)
    category = Column(String, default="")
    day_of_week = Column(Integer, nullable=True)  # 0=Monday .. 6=Sunday (recurring)
    date = Column(String, nullable=True)  # YYYY-MM-DD (one-off)
    start_time = Column(String, nullable=False)  # HH:MM
    duration_minutes = Column(Integer, default=45)
    alert_lead_minutes = Column(Integer, default=15)
    is_recurring = Column(Boolean, default=True)


class AttendanceLog(Base):
    __tablename__ = "attendance_logs"
    id = Column(Integer, primary_key=True)
    child_id = Column(Integer, ForeignKey("children.id", ondelete="CASCADE"), nullable=False)
    status_id = Column(Integer, ForeignKey("status_definitions.id"), nullable=True)  # null = note only
    timestamp = Column(DateTime, default=local_now)
    note = Column(String, nullable=True)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    username = Column(String, unique=True, nullable=False)
    pw_hash = Column(String, nullable=False)
    role = Column(String, default="staff")  # manager | staff


class UserSession(Base):
    __tablename__ = "user_sessions"
    token = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(JSON)


class WeeklyPlan(Base):
    """Weekly Stundenplan: programme items and daily lunches (day 0=Mon .. 6=Sun)."""
    __tablename__ = "weekly_plans"
    id = Column(Integer, primary_key=True)
    day_of_week = Column(Integer, nullable=False)
    kind = Column(String, default="program")  # "program" | "lunch"
    title = Column(String, nullable=False)
    start_time = Column(String, default="")
    end_time = Column(String, default="")
    notes = Column(String, default="")
    sort_order = Column(Integer, default=0)

# ---------------------------------------------------------------- schemas

class ChildIn(BaseModel):
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    group_name: str = ""
    custom_fields: dict = {}


class StatusIn(BaseModel):
    label: str = Field(min_length=1)
    color_code: str = Field("#64748b", pattern=r"^#[0-9a-fA-F]{6}$")
    is_default: bool = False
    icon: str = "•"


class TypeIn(BaseModel):
    name: str = Field(min_length=1)
    color: str = Field("#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")
    default_lead_minutes: int = Field(15, ge=0, le=240)


class AppointmentIn(BaseModel):
    child_id: int
    title: str = Field(min_length=1)
    category: str = ""
    day_of_week: Optional[int] = Field(None, ge=0, le=6)
    date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int = Field(45, ge=5, le=600)
    alert_lead_minutes: int = Field(15, ge=0, le=240)
    is_recurring: bool = True


class AttendanceIn(BaseModel):
    child_id: int
    status_id: Optional[int] = None
    note: Optional[str] = None


class WeeklyPlanIn(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    kind: str = Field("program", pattern="^(program|lunch)$")
    title: str = Field(min_length=1, max_length=200)
    start_time: str = Field("", pattern=r"^(|([01]\d|2[0-3]):[0-5]\d)$")
    end_time: str = Field("", pattern=r"^(|([01]\d|2[0-3]):[0-5]\d)$")
    notes: str = Field("", max_length=500)
    sort_order: int = 0

# ---------------------------------------------------------------- helpers

app = FastAPI(title="Hort Manager")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def d(o):
    out = {}
    for c in o.__table__.columns:
        v = getattr(o, c.name)
        out[c.name] = v.isoformat() if isinstance(v, datetime) else v
    return out


def get_or_404(db, model, oid):
    obj = db.get(model, oid)
    if not obj:
        raise HTTPException(404, f"{model.__name__} {oid} not found")
    return obj


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def apply_default_flag(db, obj):
    if obj.is_default:
        db.query(StatusDefinition).filter(StatusDefinition.id != obj.id).update({"is_default": False})

# ---------------------------------------------------------------- auth, branding, accounts

DEFAULTS = {"hort_name": "Hort", "logo": "", "theme": "wald", "language": "de", "simple_mode": False}
PUBLIC = {"/api/public", "/api/setup", "/api/login"}
MANAGER_ONLY = ("/api/statuses", "/api/appointment-types", "/api/settings", "/api/users", "/api/weekly-plan")


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def check_pw(pw, stored):
    return secrets.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)


def ud(u):
    return {"id": u.id, "name": u.name, "username": u.username, "role": u.role}


def user_from(request, db):
    tok = request.headers.get("authorization", "").removeprefix("Bearer ")
    s = db.get(UserSession, tok) if tok else None
    return db.get(User, s.user_id) if s else None


def get_settings(db):
    s = dict(DEFAULTS)
    s.update({r.key: r.value for r in db.query(Setting)})
    s["feedback_email"] = os.environ.get("FEEDBACK_EMAIL", "KontaktKindle.CH@gmail.com")  # shown as a feedback link in the app
    s["version"] = "1.0"
    return s


def put_settings(db, vals):
    for k, v in vals.items():
        r = db.get(Setting, k)
        if r:
            r.value = v
        else:
            db.add(Setting(key=k, value=v))


def new_session(db, u):
    t = secrets.token_urlsafe(32)
    db.add(UserSession(token=t, user_id=u.id))
    db.commit()
    return {"token": t, "user": ud(u)}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    p = request.url.path
    who = ""
    if p.startswith("/api/") and p not in PUBLIC:
        db = SessionLocal()
        try:
            u = user_from(request, db)
            if not u:
                return JSONResponse({"detail": "Please sign in."}, 401)
            who = u.name
            manager_area = p.startswith(("/api/backup", "/api/audit")) or (
                p.startswith(MANAGER_ONLY) and (request.method != "GET" or p.startswith("/api/users")))
            if u.role != "manager" and manager_area:
                return JSONResponse({"detail": "Only the manager can do this."}, 403)
        finally:
            db.close()
    resp = await call_next(request)
    if who and request.method != "GET" and resp.status_code < 400 and p != "/api/logout":
        db = SessionLocal()
        try:  # audit trail: who changed what, and when
            db.add(AuditLog(user_name=who, method=request.method, path=p, timestamp=local_now()))
            db.commit()
        finally:
            db.close()
    return resp


class SettingsIn(BaseModel):
    hort_name: str = Field(min_length=1, max_length=60)
    logo: str = Field("", max_length=400_000)
    theme: str = Field("wald", pattern="^(wald|himmel|sonne|beere|ozean|lavendel|korall|mint|schoggi|bonbon|nacht|kontrast)$")
    language: str = Field("de", pattern="^(de|en)$")
    simple_mode: bool = False


class SetupIn(SettingsIn):
    name: str = Field(min_length=1)
    username: str = Field(min_length=2)
    password: str = Field(min_length=8)
    demo: bool = False


class LoginIn(BaseModel):
    username: str
    password: str


class UserIn(BaseModel):
    name: str = Field(min_length=1)
    username: str = Field(min_length=2)
    password: Optional[str] = Field(None, min_length=8)
    role: str = Field("staff", pattern="^(manager|staff)$")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/public")
def public(db: Session = Depends(get_db)):
    return {**get_settings(db), "configured": db.query(User).count() > 0}


@app.post("/api/setup", status_code=201)
def setup(body: SetupIn, db: Session = Depends(get_db)):
    if db.query(User).count():
        raise HTTPException(400, "Already set up.")
    data = body.model_dump()
    u = User(name=data.pop("name"), username=data.pop("username").strip().lower(),
             pw_hash=hash_pw(data.pop("password")), role="manager")
    demo = data.pop("demo")
    put_settings(db, data)
    db.add(u)
    db.commit()
    seed(body.language, demo)
    return new_session(db, u)


FAILS = {}  # (ip, username) -> timestamps of recent failed logins


@app.post("/api/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    key, now = (ip, body.username.strip().lower()), pytime.time()
    FAILS[key] = [t for t in FAILS.get(key, []) if now - t < 600]
    if len(FAILS[key]) >= 5:
        raise HTTPException(429, "Too many attempts. Please wait 10 minutes.")
    u = db.query(User).filter_by(username=key[1]).first()
    if not u or not check_pw(body.password, u.pw_hash):
        FAILS[key].append(now)
        raise HTTPException(401, "Wrong username or password.")
    FAILS.pop(key, None)
    return new_session(db, u)


@app.get("/api/me")
def me(request: Request, db: Session = Depends(get_db)):
    return ud(user_from(request, db))


@app.post("/api/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    s = db.get(UserSession, request.headers.get("authorization", "").removeprefix("Bearer "))
    if s:
        db.delete(s)
        db.commit()
    return {"ok": True}


@app.get("/api/settings")
def read_settings(db: Session = Depends(get_db)):
    return get_settings(db)


@app.put("/api/settings")
def update_settings(body: SettingsIn, db: Session = Depends(get_db)):
    put_settings(db, body.model_dump())
    db.commit()
    return get_settings(db)


@app.get("/api/users")
def list_users(db: Session = Depends(get_db)):
    return [ud(u) for u in db.query(User).order_by(User.name)]


@app.post("/api/users", status_code=201)
def create_user(body: UserIn, db: Session = Depends(get_db)):
    name = body.username.strip().lower()
    if db.query(User).filter_by(username=name).first() or not body.password:
        raise HTTPException(400, "Name already taken.")
    u = User(name=body.name, username=name, pw_hash=hash_pw(body.password), role=body.role)
    db.add(u)
    db.commit()
    return ud(u)


def other_managers(db, uid):
    return db.query(User).filter(User.role == "manager", User.id != uid).count()


@app.put("/api/users/{uid}")
def update_user(uid: int, body: UserIn, db: Session = Depends(get_db)):
    u = get_or_404(db, User, uid)
    name = body.username.strip().lower()
    if db.query(User).filter(User.username == name, User.id != uid).first():
        raise HTTPException(400, "Name already taken.")
    if u.role == "manager" and body.role != "manager" and not other_managers(db, uid):
        raise HTTPException(400, "At least one manager is required.")
    u.name, u.username, u.role = body.name, name, body.role
    if body.password:
        u.pw_hash = hash_pw(body.password)
    db.commit()
    return ud(u)


@app.delete("/api/users/{uid}")
def delete_user(uid: int, request: Request, db: Session = Depends(get_db)):
    u = get_or_404(db, User, uid)
    if user_from(request, db).id == uid:
        raise HTTPException(400, "You cannot delete your own account.")
    if u.role == "manager" and not other_managers(db, uid):
        raise HTTPException(400, "At least one manager is required.")
    db.query(UserSession).filter_by(user_id=uid).delete()
    db.delete(u)
    db.commit()
    return {"ok": True}

# ---------------------------------------------------------------- children

@app.get("/api/children")
def list_children(db: Session = Depends(get_db)):
    return [d(c) for c in db.query(Child).order_by(Child.last_name, Child.first_name)]


@app.post("/api/children", status_code=201)
def create_child(body: ChildIn, db: Session = Depends(get_db)):
    c = Child(**body.model_dump())
    db.add(c)
    db.commit()
    return d(c)


@app.put("/api/children/{cid}")
def update_child(cid: int, body: ChildIn, db: Session = Depends(get_db)):
    c = get_or_404(db, Child, cid)
    for k, v in body.model_dump().items():
        setattr(c, k, v)
    db.commit()
    return d(c)


@app.delete("/api/children/{cid}")
def delete_child(cid: int, db: Session = Depends(get_db)):
    c = get_or_404(db, Child, cid)
    db.query(Appointment).filter_by(child_id=cid).delete()
    db.query(AttendanceLog).filter_by(child_id=cid).delete()
    db.query(Handover).filter_by(child_id=cid).delete()
    db.delete(c)  # meal logs go with it (ON DELETE CASCADE)
    db.commit()
    return {"ok": True}

# ---------------------------------------------------------------- statuses

@app.get("/api/statuses")
def list_statuses(db: Session = Depends(get_db)):
    return [d(s) for s in db.query(StatusDefinition).order_by(StatusDefinition.id)]


@app.post("/api/statuses", status_code=201)
def create_status(body: StatusIn, db: Session = Depends(get_db)):
    s = StatusDefinition(**body.model_dump())
    db.add(s)
    db.flush()
    apply_default_flag(db, s)
    db.commit()
    return d(s)


@app.put("/api/statuses/{sid}")
def update_status(sid: int, body: StatusIn, db: Session = Depends(get_db)):
    s = get_or_404(db, StatusDefinition, sid)
    if s.is_default and not body.is_default:
        raise HTTPException(400, "Default status cannot be changed this way.")
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    apply_default_flag(db, s)
    db.commit()
    return d(s)


@app.delete("/api/statuses/{sid}")
def delete_status(sid: int, db: Session = Depends(get_db)):
    s = get_or_404(db, StatusDefinition, sid)
    if s.is_default:
        raise HTTPException(400, "Default status cannot be changed this way.")
    db.query(AttendanceLog).filter_by(status_id=sid).update({"status_id": None})
    db.delete(s)
    db.commit()
    return {"ok": True}

# ---------------------------------------------------------------- appointment types

@app.get("/api/appointment-types")
def list_types(db: Session = Depends(get_db)):
    return [d(t) for t in db.query(AppointmentType).order_by(AppointmentType.name)]


@app.post("/api/appointment-types", status_code=201)
def create_type(body: TypeIn, db: Session = Depends(get_db)):
    if db.query(AppointmentType).filter_by(name=body.name).first():
        raise HTTPException(400, "Name already taken.")
    t = AppointmentType(**body.model_dump())
    db.add(t)
    db.commit()
    return d(t)


@app.put("/api/appointment-types/{tid}")
def update_type(tid: int, body: TypeIn, db: Session = Depends(get_db)):
    t = get_or_404(db, AppointmentType, tid)
    for k, v in body.model_dump().items():
        setattr(t, k, v)
    db.commit()
    return d(t)


@app.delete("/api/appointment-types/{tid}")
def delete_type(tid: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, AppointmentType, tid))
    db.commit()
    return {"ok": True}

# ---------------------------------------------------------------- appointments

@app.get("/api/appointments")
def list_appointments(child_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(Appointment)
    if child_id:
        q = q.filter_by(child_id=child_id)
    return [d(a) for a in q.order_by(Appointment.day_of_week, Appointment.start_time)]


@app.post("/api/appointments", status_code=201)
def create_appointment(body: AppointmentIn, db: Session = Depends(get_db)):
    get_or_404(db, Child, body.child_id)
    if body.is_recurring and body.day_of_week is None:
        raise HTTPException(400, "Recurring appointments need a weekday.")
    if not body.is_recurring and not body.date:
        raise HTTPException(400, "One-off appointments need a date.")
    a = Appointment(**body.model_dump())
    db.add(a)
    db.commit()
    return d(a)


@app.delete("/api/appointments/{aid}")
def delete_appointment(aid: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, Appointment, aid))
    db.commit()
    return {"ok": True}

# ---------------------------------------------------------------- attendance + log

@app.post("/api/attendance", status_code=201)
def log_attendance(body: AttendanceIn, db: Session = Depends(get_db)):
    get_or_404(db, Child, body.child_id)
    if body.status_id is None and not (body.note or "").strip():
        raise HTTPException(400, "Provide a status, a note, or both.")
    if body.status_id is not None:
        get_or_404(db, StatusDefinition, body.status_id)
    entry = AttendanceLog(child_id=body.child_id, status_id=body.status_id,
                          note=(body.note or "").strip() or None, timestamp=local_now())
    db.add(entry)
    db.commit()
    return d(entry)


@app.get("/api/logs")
def list_logs(child_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Today's log feed, newest first."""
    start = datetime.combine(local_now().date(), time.min)
    q = db.query(AttendanceLog).filter(AttendanceLog.timestamp >= start)
    if child_id:
        q = q.filter_by(child_id=child_id)
    children = {c.id: c for c in db.query(Child)}
    statuses = {s.id: s for s in db.query(StatusDefinition)}
    out = []
    for l in q.order_by(AttendanceLog.timestamp.desc()):
        c, s = children.get(l.child_id), statuses.get(l.status_id)
        out.append({**d(l), "child_name": f"{c.first_name} {c.last_name}" if c else "?",
                    "status": d(s) if s else None})
    return out

# ---------------------------------------------------------------- weekly plan (Stundenplan + lunches)

@app.get("/api/weekly-plan")
def list_weekly_plan(db: Session = Depends(get_db)):
    rows = db.query(WeeklyPlan).order_by(WeeklyPlan.day_of_week, WeeklyPlan.sort_order, WeeklyPlan.id).all()
    return [d(r) for r in rows]


@app.post("/api/weekly-plan", status_code=201)
def create_weekly_plan(body: WeeklyPlanIn, db: Session = Depends(get_db)):
    row = WeeklyPlan(**body.model_dump())
    db.add(row)
    db.commit()
    return d(row)


@app.put("/api/weekly-plan/{pid}")
def update_weekly_plan(pid: int, body: WeeklyPlanIn, db: Session = Depends(get_db)):
    row = get_or_404(db, WeeklyPlan, pid)
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    db.commit()
    return d(row)


@app.delete("/api/weekly-plan/{pid}")
def delete_weekly_plan(pid: int, db: Session = Depends(get_db)):
    db.delete(get_or_404(db, WeeklyPlan, pid))
    db.commit()
    return {"ok": True}

# ---------------------------------------------------------------- roster

@app.get("/api/roster")
def roster(now: Optional[str] = None, db: Session = Depends(get_db)):
    """Today's roster. Optional ?now=HH:MM simulates the clock (handy for demos)."""
    n = local_now()
    cur = minutes(now) if now else n.hour * 60 + n.minute
    today = n.date()
    default = db.query(StatusDefinition).filter_by(is_default=True).first()
    statuses = {s.id: s for s in db.query(StatusDefinition)}
    logs = (db.query(AttendanceLog).filter(AttendanceLog.timestamp >= datetime.combine(today, time.min))
            .order_by(AttendanceLog.timestamp).all())
    last_status, last_note = {}, {}
    for l in logs:
        if l.status_id in statuses:
            last_status[l.child_id] = l
        if l.note:
            last_note[l.child_id] = l
    types = {t.name: t for t in db.query(AppointmentType)}
    appts = {}
    for a in db.query(Appointment):
        today_hit = (a.is_recurring and a.day_of_week == today.weekday()) or \
                    (not a.is_recurring and a.date == today.isoformat())
        if not today_hit:
            continue
        s = minutes(a.start_time)
        e = s + (a.duration_minutes or 45)
        lead = a.alert_lead_minutes or 0
        state = "done" if cur >= e else "away" if cur >= s else "upcoming" if cur >= s - lead else "later"
        t = types.get(a.category)
        appts.setdefault(a.child_id, []).append(
            {**d(a), "state": state, "minutes_until": s - cur, "color": t.color if t else "#64748b"})
    hov = {}
    for h in db.query(Handover).filter_by(day=today.isoformat()).order_by(Handover.id):
        hov[h.child_id] = {"left_at": h.left_at.isoformat(), "left_with": h.left_with,
                           "back_at": h.back_at.isoformat() if h.back_at else None}
    result = []
    for c in db.query(Child).order_by(Child.first_name):
        ls = last_status.get(c.id)
        st = statuses.get(ls.status_id) if ls else default
        ln = last_note.get(c.id)
        result.append({**d(c), "status": d(st) if st else None, "handover": hov.get(c.id),
                       "status_since": ls.timestamp.isoformat() if ls else None,
                       "last_note": {"note": ln.note, "timestamp": ln.timestamp.isoformat()} if ln else None,
                       "appointments": sorted(appts.get(c.id, []), key=lambda a: a["start_time"])})
    return result

# ---------------------------------------------------------------- seed (statuses, types, optional demo data)

def seed(lang="de", demo=True):
    pick = (lambda en, de_: de_) if lang == "de" else (lambda en, de_: en)
    db = SessionLocal()
    if db.query(StatusDefinition).count() == 0:
        db.add_all([StatusDefinition(label=pick(en, de_), color_code=c, is_default=df, icon=i) for en, de_, c, df, i in [
            ("Expected", "Erwartet", "#64748b", True, "🕑"), ("Present", "Anwesend", "#15803d", False, "✅"),
            ("Absent", "Abwesend", "#b91c1c", False, "🚫"), ("En Route", "Unterwegs", "#b45309", False, "🚶"),
            ("In Appointment", "Im Termin", "#1d4ed8", False, "🩺"), ("Picked Up", "Abgeholt", "#7e22ce", False, "👋")]])
        db.add_all([AppointmentType(name=pick(en, de_), color=c, default_lead_minutes=m) for en, de_, c, m in [
            ("Physio", "Physio", "#0d9488", 15), ("Music Lesson", "Musikstunde", "#c026d3", 10),
            ("Tutoring", "Nachhilfe", "#ca8a04", 10), ("Sports Practice", "Sporttraining", "#ea580c", 15),
            ("Therapy", "Therapie", "#2563eb", 30)]])
    if demo and db.query(Child).count() == 0:
        g = lambda x: pick(f"Group {x}", f"Gruppe {x}")
        al, di, em = pick("Allergies", "Allergien"), pick("Dietary Restrictions", "Ernährung"), pick("Emergency Contact", "Notfallkontakt")
        pu, bus = pick("Authorized Pickups", "Abholberechtigte"), pick("Bus Route", "Busline")
        kids = [("Mia", "Keller", g("A"), {al: pick("Peanuts", "Erdnüsse"), bus: "12", pu: pick("Mother, Grandma Rosa", "Mutter, Oma Rosa")}),
                ("Noah", "Brunner", g("A"), {di: pick("Vegetarian", "Vegetarisch"), em: "+41 79 555 01 02"}),
                ("Lina", "Meier", g("B"), {al: pick("Lactose", "Laktose"), pu: pick("Father, Uncle Tom", "Vater, Onkel Tom")}),
                ("Luca", "Frei", g("B"), {bus: "7"}),
                ("Emma", "Huber", g("A"), {em: "+41 78 555 03 04"}),
                ("Elias", "Weber", g("C"), {al: pick("Bee stings", "Bienenstiche"), pu: pick("Both parents", "Beide Eltern")}),
                ("Sofia", "Gerber", g("C"), {}),
                ("Jonas", "Baumann", g("B"), {di: "Halal"})]
        children = [Child(first_name=f, last_name=l, group_name=gr, custom_fields=cf) for f, l, gr, cf in kids]
        db.add_all(children)
        db.flush()
        n = local_now()
        wd, base = n.weekday(), n.hour * 60 + n.minute

        def hhmm(m):
            m %= 1440
            return f"{m // 60:02d}:{m % 60:02d}"

        t_physio, t_music, t_tutor, t_ther = pick("Physio", "Physio"), pick("Music Lesson", "Musikstunde"), pick("Tutoring", "Nachhilfe"), pick("Therapy", "Therapie")
        db.add_all([  # relative to "now" so the alert banner is visible in a demo
            Appointment(child_id=children[0].id, title=pick("Physio session", "Physiotherapie"), category=t_physio, day_of_week=wd,
                        start_time=hhmm(base + 12), duration_minutes=45, alert_lead_minutes=15, is_recurring=True),
            Appointment(child_id=children[3].id, title=pick("Piano lesson", "Klavierstunde"), category=t_music, day_of_week=wd,
                        start_time=hhmm(base + 70), duration_minutes=30, alert_lead_minutes=10, is_recurring=True),
            Appointment(child_id=children[5].id, title=pick("Speech therapy", "Logopädie"), category=t_ther, day_of_week=1,
                        start_time="15:30", duration_minutes=50, alert_lead_minutes=30, is_recurring=True),
            Appointment(child_id=children[1].id, title=pick("Maths tutoring", "Mathe-Nachhilfe"), category=t_tutor, is_recurring=False,
                        date=n.date().isoformat(), start_time=hhmm(base + 150), duration_minutes=60, alert_lead_minutes=10)])
        plan = [  # (day, kind, (en, de), start, end, (note en, note de), sort)
            (0, "lunch", ("Spaghetti Bolognese", "Spaghetti Bolognese"), "12:00", "", ("with Parmesan", "mit Parmesan"), 0),
            (1, "lunch", ("Vegetable curry with rice", "Gemüsecurry mit Reis"), "12:00", "", ("vegetarian", "vegetarisch"), 0),
            (2, "lunch", ("Chicken strips", "Pouletgeschnetzeltes"), "12:00", "", ("with rösti", "mit Rösti"), 0),
            (3, "lunch", ("Fish fingers & potatoes", "Fischstäbchen & Kartoffeln"), "12:00", "", ("", ""), 0),
            (4, "lunch", ("Pizza Margherita", "Pizza Margherita"), "12:00", "", ("", ""), 0),
            (0, "program", ("Free play & garden", "Freispiel & Garten"), "14:00", "16:00", ("", ""), 1),
            (1, "program", ("Crafts / workshop", "Basteln / Werken"), "14:00", "15:30", ("", ""), 1),
            (2, "program", ("Afternoon excursion", "Ausflug am Nachmittag"), "13:30", "16:30",
             ("Weather-dependent – bring backpack", "Wetterabhängig – Rucksack mitnehmen"), 1),
            (3, "program", ("Sports / movement", "Sport / Bewegung"), "14:00", "15:30", ("", ""), 1),
            (4, "program", ("Games afternoon", "Spiele-Nachmittag"), "14:00", "16:00", ("", ""), 1)]
        db.add_all([WeeklyPlan(day_of_week=dw, kind=k, title=pick(*ti), start_time=s, end_time=e, notes=pick(*no), sort_order=so)
                    for dw, k, ti, s, e, no, so in plan])
    db.commit()
    db.close()


class Handover(Base):
    """A child was handed over to someone (e.g. for physio) and later brought back."""
    __tablename__ = "handovers"
    id = Column(Integer, primary_key=True)
    child_id = Column(Integer, ForeignKey("children.id", ondelete="CASCADE"), nullable=False)
    day = Column(String, nullable=False)
    left_at = Column(DateTime, default=local_now)
    left_by = Column(String, default="")  # staff member who handed the child over
    left_with = Column(String, default="")  # who took the child
    back_at = Column(DateTime, nullable=True)
    back_by = Column(String, default="")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    user_name = Column(String, default="")
    method = Column(String, default="")
    path = Column(String, default="")
    timestamp = Column(DateTime, default=local_now)


class HandoverIn(BaseModel):
    child_id: int
    action: str = Field(pattern="^(out|back)$")
    person: str = Field("", max_length=80)


@app.post("/api/handover", status_code=201)
def handover(body: HandoverIn, request: Request, db: Session = Depends(get_db)):
    get_or_404(db, Child, body.child_id)
    u = user_from(request, db)
    who, now, person = (u.name if u else ""), local_now(), body.person.strip()
    today = now.date().isoformat()
    open_ = (db.query(Handover).filter_by(child_id=body.child_id, day=today, back_at=None)
             .order_by(Handover.id.desc()).first())
    if body.action == "out":
        if open_:
            raise HTTPException(400, "Child is already out.")
        db.add(Handover(child_id=body.child_id, day=today, left_at=now, left_by=who, left_with=person))
        note = "Übergeben an " + person if person else "Übergeben"
    else:
        if not open_:
            raise HTTPException(400, "Child is not out.")
        open_.back_at, open_.back_by = now, person or who
        note = "Zurück" + (" mit " + person if person else "")
    db.add(AttendanceLog(child_id=body.child_id, note=f"{note} ({who})", timestamp=now))
    db.commit()
    return {"ok": True}


@app.get("/api/audit")
def audit(db: Session = Depends(get_db)):
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(60)
    return [{"user": r.user_name, "what": f"{r.method} {r.path}", "at": r.timestamp.isoformat()} for r in rows]


Base.metadata.create_all(engine)

# ---------------------------------------------------------------- upgrades: meals, bulk check-in, CSV import, backup

HEAD = {
    "first_name": ("first_name", "first name", "firstname", "vorname"),
    "last_name": ("last_name", "last name", "lastname", "nachname", "name"),
    "group_name": ("group", "group_name", "gruppe"),
}


def install_upgrades():
    class MealLog(Base):
        """One row = this child ate this meal on this day. No row = not yet."""
        __tablename__ = "meal_logs"
        id = Column(Integer, primary_key=True)
        child_id = Column(Integer, ForeignKey("children.id", ondelete="CASCADE"), nullable=False)
        day = Column(String, nullable=False)  # YYYY-MM-DD, so the list resets every day
        meal = Column(String, nullable=False)  # lunch | zvieri
        timestamp = Column(DateTime, default=local_now)
        __table_args__ = (UniqueConstraint("child_id", "day", "meal"),)

    class MealIn(BaseModel):
        child_ids: List[int] = Field(min_length=1)
        meal: str = Field(pattern="^(lunch|zvieri)$")
        ate: bool = True

    class BulkIn(BaseModel):
        child_ids: List[int] = Field(min_length=1)
        status_id: int

    class ImportIn(BaseModel):
        csv_text: str = Field(max_length=2_000_000)

    @app.get("/api/meals")
    def meals_today(db=Depends(get_db)):
        """{child_id: {"lunch": true, "zvieri": true}} for today."""
        out = {}
        for m in db.query(MealLog).filter_by(day=local_now().date().isoformat()):
            out.setdefault(m.child_id, {})[m.meal] = True
        return out

    @app.post("/api/meals")
    def set_meals(body: MealIn, db=Depends(get_db)):
        today = local_now().date().isoformat()
        ids = {i for (i,) in db.query(Child.id).filter(Child.id.in_(body.child_ids))}
        q = db.query(MealLog).filter(MealLog.day == today, MealLog.meal == body.meal,
                                     MealLog.child_id.in_(ids))
        if body.ate:
            have = {m.child_id for m in q}
            db.add_all(MealLog(child_id=i, day=today, meal=body.meal) for i in ids - have)
        else:
            q.delete(synchronize_session=False)
        db.commit()
        return {"ok": True, "count": len(ids)}

    @app.post("/api/attendance/bulk", status_code=201)
    def bulk_attendance(body: BulkIn, db=Depends(get_db)):
        if not db.get(StatusDefinition, body.status_id):
            raise HTTPException(404, "Status not found.")
        ids = [i for (i,) in db.query(Child.id).filter(Child.id.in_(body.child_ids))]
        now = local_now()
        db.add_all(AttendanceLog(child_id=i, status_id=body.status_id, timestamp=now) for i in ids)
        db.commit()
        return {"updated": len(ids)}

    @app.post("/api/children/import", status_code=201)
    def import_children(body: ImportIn, db=Depends(get_db)):
        text = body.csv_text.lstrip("\ufeff").strip()
        if not text:
            raise HTTPException(400, "The file is empty.")
        head = text.splitlines()[0]
        delim = ";" if head.count(";") > head.count(",") else ("\t" if head.count("\t") > head.count(",") else ",")
        seen = {(c.first_name.lower(), c.last_name.lower(), (c.group_name or "").lower())
                for c in db.query(Child)}
        added = skipped = 0
        for raw in csv.DictReader(io.StringIO(text), delimiter=delim):
            vals, extra = {}, {}
            for k, v in raw.items():
                if not k:
                    continue
                low, v = k.strip().lower(), (v or "").strip()
                field = next((f for f, names in HEAD.items() if low in names and f not in vals), None)
                if field:
                    vals[field] = v
                elif v:
                    extra[k.strip()] = v  # any other column becomes an info field (Allergien, Bus ...)
            key = (vals.get("first_name", "").lower(), vals.get("last_name", "").lower(),
                   vals.get("group_name", "").lower())
            if not key[0] or not key[1] or key in seen:
                skipped += 1
                continue
            seen.add(key)
            db.add(Child(first_name=vals["first_name"], last_name=vals["last_name"],
                         group_name=vals.get("group_name", ""), custom_fields=extra))
            added += 1
        db.commit()
        return {"added": added, "skipped": skipped}

    @app.get("/api/backup")
    def backup(db=Depends(get_db)):
        """Manager-only full export (no passwords or sessions) as JSON."""
        out = {}
        for t in Base.metadata.sorted_tables:
            if t.name in ("users", "user_sessions"):
                continue
            out[t.name] = [{k: (v.isoformat() if isinstance(v, (datetime, date)) else v)
                            for k, v in r._mapping.items()} for r in db.execute(t.select())]
        return out

    Base.metadata.create_all(engine)  # creates meal_logs
    with engine.begin() as c:
        c.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_att_child_ts ON attendance_logs (child_id, timestamp)")
        c.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_appt_child ON appointments (child_id)")
        c.execute(text("DELETE FROM audit_log WHERE timestamp < :t"), {"t": local_now() - timedelta(days=90)})
        cut = (local_now().date() - timedelta(days=30)).isoformat()
        c.execute(text("DELETE FROM meal_logs WHERE day < :cut"), {"cut": cut})


install_upgrades()


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


@app.get("/manifest.json")
def manifest():
    return JSONResponse({"name": "Kindle", "short_name": "Kindle", "start_url": "/", "display": "standalone",
                         "background_color": "#0f6b8a", "theme_color": "#0f6b8a",
                         "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})


@app.get("/icon.svg")
def icon():
    return Response('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="#0f6b8a"/>'
                    '<text x="50" y="68" font-size="58" text-anchor="middle" fill="#fff" font-family="sans-serif" font-weight="700">K</text></svg>',
                    media_type="image/svg+xml")


@app.get("/sw.js")
def sw():
    return Response("self.addEventListener('fetch',()=>{});", media_type="application/javascript")
