"""Hort Manager - multi-hort attendance, appointments, daily log, Excel-style Stundenplan.
Run: uvicorn main:app --reload
"""
import hashlib
import secrets
from datetime import datetime, time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, create_engine, UniqueConstraint
from sqlalchemy.orm import Session, declarative_base, sessionmaker

BASE = Path(__file__).parent
engine = create_engine(f"sqlite:///{BASE / 'hort.db'}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


# ---------------------------------------------------------------- models
class Hort(Base):
    """One independent after-school care centre."""
    __tablename__ = "horts"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)  # short login code, e.g. "sonne"
    name = Column(String, nullable=False)
    logo = Column(String, default="")
    theme = Column(String, default="wald")
    language = Column(String, default="de")
    simple_mode = Column(Boolean, default=False)
    # list of {"ip": "...", "user_id": int, "label": "..."}
    trusted_ips = Column(JSON, default=list)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    hort_id = Column(Integer, ForeignKey("horts.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    username = Column(String, nullable=False)
    pw_hash = Column(String, nullable=False)
    role = Column(String, default="staff")  # manager | staff
    __table_args__ = (UniqueConstraint("hort_id", "username", name="uq_hort_user"),)


class UserSession(Base):
    __tablename__ = "user_sessions"
    token = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    hort_id = Column(Integer, ForeignKey("horts.id", ondelete="CASCADE"))


class Child(Base):
    __tablename__ = "children"
    id = Column(Integer, primary_key=True)
    hort_id = Column(Integer, ForeignKey("horts.id", ondelete="CASCADE"), nullable=False)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    group_name = Column(String, default="")
    custom_fields = Column(JSON, default=dict)


class StatusDefinition(Base):
    __tablename__ = "status_definitions"
    id = Column(Integer, primary_key=True)
    hort_id = Column(Integer, ForeignKey("horts.id", ondelete="CASCADE"), nullable=False)
    label = Column(String, nullable=False)
    color_code = Column(String, default="#64748b")
    is_default = Column(Boolean, default=False)
    icon = Column(String, default="•")


class AppointmentType(Base):
    __tablename__ = "appointment_types"
    id = Column(Integer, primary_key=True)
    hort_id = Column(Integer, ForeignKey("horts.id", ondelete="CASCADE"), nullable=False)
    name = Column(String, nullable=False)
    color = Column(String, default="#6366f1")
    default_lead_minutes = Column(Integer, default=15)


class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    hort_id = Column(Integer, ForeignKey("horts.id", ondelete="CASCADE"), nullable=False)
    child_id = Column(Integer, ForeignKey("children.id", ondelete="CASCADE"), nullable=False)
    title = Column(String, nullable=False)
    category = Column(String, default="")
    day_of_week = Column(Integer, nullable=True)  # 0=Monday .. 6=Sunday
    date = Column(String, nullable=True)  # YYYY-MM-DD
    start_time = Column(String, nullable=False)
    duration_minutes = Column(Integer, default=45)
    alert_lead_minutes = Column(Integer, default=15)
    is_recurring = Column(Boolean, default=True)


class AttendanceLog(Base):
    __tablename__ = "attendance_logs"
    id = Column(Integer, primary_key=True)
    hort_id = Column(Integer, ForeignKey("horts.id", ondelete="CASCADE"), nullable=False)
    child_id = Column(Integer, ForeignKey("children.id", ondelete="CASCADE"), nullable=False)
    status_id = Column(Integer, ForeignKey("status_definitions.id"), nullable=True)
    timestamp = Column(DateTime, default=datetime.now)
    note = Column(String, nullable=True)


class TimeSlot(Base):
    """Configurable period rows for the Excel-style Stundenplan."""
    __tablename__ = "time_slots"
    id = Column(Integer, primary_key=True)
    hort_id = Column(Integer, ForeignKey("horts.id", ondelete="CASCADE"), nullable=False)
    label = Column(String, default="")  # e.g. "1. Lektion" or empty
    start_time = Column(String, nullable=False)  # HH:MM
    end_time = Column(String, nullable=False)
    sort_order = Column(Integer, default=0)


class ScheduleCell(Base):
    """One cell in the Stundenplan grid.
    group_name == "" means the shared plan (all groups).
    """
    __tablename__ = "schedule_cells"
    id = Column(Integer, primary_key=True)
    hort_id = Column(Integer, ForeignKey("horts.id", ondelete="CASCADE"), nullable=False)
    group_name = Column(String, default="")  # "" = Gemeinsam / all groups
    day_of_week = Column(Integer, nullable=False)  # 0=Mon .. 6=Sun
    slot_id = Column(Integer, ForeignKey("time_slots.id", ondelete="CASCADE"), nullable=False)
    title = Column(String, default="")  # subject / activity
    room = Column(String, default="")
    teacher = Column(String, default="")
    notes = Column(String, default="")
    kind = Column(String, default="program")  # program | lunch
    __table_args__ = (
        UniqueConstraint("hort_id", "group_name", "day_of_week", "slot_id", name="uq_cell"),
    )


# ---------------------------------------------------------------- helpers
def d(obj):
    if obj is None:
        return None
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_or_404(db, model, pk):
    r = db.get(model, pk)
    if not r:
        raise HTTPException(404, "Not found.")
    return r


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def check_pw(pw, stored):
    return secrets.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or ""
    if xff:
        return xff.split(",")[0].strip()
    return (request.client.host if request.client else "") or ""


def ud(u):
    return {"id": u.id, "name": u.name, "username": u.username, "role": u.role, "hort_id": u.hort_id}


def hort_public(h: Hort):
    return {
        "id": h.id,
        "code": h.code,
        "hort_name": h.name,
        "logo": h.logo or "",
        "theme": h.theme or "wald",
        "language": h.language or "de",
        "simple_mode": bool(h.simple_mode),
    }


def minutes(hhmm: str) -> int:
    if not hhmm or ":" not in hhmm:
        return 0
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------- app + auth
app = FastAPI(title="Hort Manager")
PUBLIC = {"/api/public", "/api/setup", "/api/login", "/api/trusted-login"}
MANAGER_WRITE = ("/api/statuses", "/api/appointment-types", "/api/users", "/api/time-slots", "/api/schedule")
# settings + trusted-ips are manager-only write via method checks below


def user_from(request, db) -> Optional[User]:
    tok = request.headers.get("authorization", "").removeprefix("Bearer ")
    s = db.get(UserSession, tok) if tok else None
    return db.get(User, s.user_id) if s else None


def require_user(request: Request, db: Session) -> User:
    u = user_from(request, db)
    if not u:
        raise HTTPException(401, "Please sign in.")
    return u


def require_manager(request: Request, db: Session) -> User:
    u = require_user(request, db)
    if u.role != "manager":
        raise HTTPException(403, "Only the manager can do this.")
    return u


def hort_of(u: User, db: Session) -> Hort:
    h = db.get(Hort, u.hort_id)
    if not h:
        raise HTTPException(400, "Hort missing.")
    return h


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    p = request.url.path
    if p.startswith("/api/") and p not in PUBLIC:
        db = SessionLocal()
        try:
            u = user_from(request, db)
            if not u:
                return JSONResponse({"detail": "Please sign in."}, 401)
            # manager-only writes
            if u.role != "manager" and request.method not in ("GET", "HEAD", "OPTIONS"):
                if any(p.startswith(prefix) for prefix in MANAGER_WRITE) or p.startswith("/api/settings") or p.startswith("/api/trusted"):
                    return JSONResponse({"detail": "Only the manager can do this."}, 403)
            if u.role != "manager" and p.startswith("/api/users"):
                return JSONResponse({"detail": "Only the manager can do this."}, 403)
        finally:
            db.close()
    return await call_next(request)


# ---------------------------------------------------------------- schemas
class SetupIn(BaseModel):
    hort_code: str = Field(min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    hort_name: str = Field(min_length=1, max_length=60)
    logo: str = Field("", max_length=400_000)
    theme: str = Field("wald", pattern="^(wald|himmel|sonne|beere|kontrast)$")
    language: str = Field("de", pattern="^(de|en)$")
    simple_mode: bool = False
    name: str = Field(min_length=1)
    username: str = Field(min_length=2)
    password: str = Field(min_length=6)
    demo: bool = False


class LoginIn(BaseModel):
    hort_code: str
    username: str
    password: str
    trust_ip: bool = False


class TrustedLoginIn(BaseModel):
    hort_code: str


class SettingsIn(BaseModel):
    hort_name: str = Field(min_length=1, max_length=60)
    logo: str = Field("", max_length=400_000)
    theme: str = Field("wald", pattern="^(wald|himmel|sonne|beere|kontrast)$")
    language: str = Field("de", pattern="^(de|en)$")
    simple_mode: bool = False


class UserIn(BaseModel):
    name: str = Field(min_length=1)
    username: str = Field(min_length=2)
    password: Optional[str] = Field(None, min_length=6)
    role: str = Field("staff", pattern="^(manager|staff)$")


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


class TimeSlotIn(BaseModel):
    label: str = ""
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    sort_order: int = 0


class ScheduleCellIn(BaseModel):
    group_name: str = ""
    day_of_week: int = Field(ge=0, le=6)
    slot_id: int
    title: str = Field("", max_length=200)
    room: str = Field("", max_length=80)
    teacher: str = Field("", max_length=80)
    notes: str = Field("", max_length=500)
    kind: str = Field("program", pattern="^(program|lunch)$")


class BulkScheduleIn(BaseModel):
    """Upsert many cells at once (Excel-style save)."""
    cells: list[ScheduleCellIn]


def new_session(db, u: User):
    t = secrets.token_urlsafe(32)
    db.add(UserSession(token=t, user_id=u.id, hort_id=u.hort_id))
    db.commit()
    return {"token": t, "user": ud(u)}


def apply_default_flag(db, s: StatusDefinition):
    if s.is_default:
        db.query(StatusDefinition).filter(
            StatusDefinition.hort_id == s.hort_id,
            StatusDefinition.id != s.id,
        ).update({"is_default": False})


# ---------------------------------------------------------------- public / setup / login
@app.get("/api/public")
def public(request: Request, db: Session = Depends(get_db)):
    """No hort selected yet — just whether any hort exists + client IP hint."""
    ip = client_ip(request)
    # Find horts that trust this IP
    trusted = []
    for h in db.query(Hort).all():
        for entry in (h.trusted_ips or []):
            if entry.get("ip") == ip:
                trusted.append({
                    "hort_code": h.code,
                    "hort_name": h.name,
                    "user_id": entry.get("user_id"),
                    "label": entry.get("label") or h.name,
                })
                break
    return {
        "has_horts": db.query(Hort).count() > 0,
        "client_ip": ip,
        "trusted": trusted,
    }


@app.post("/api/setup", status_code=201)
def setup(body: SetupIn, db: Session = Depends(get_db)):
    code = body.hort_code.strip().lower()
    if db.query(Hort).filter_by(code=code).first():
        raise HTTPException(400, "Hort code already taken.")
    h = Hort(
        code=code,
        name=body.hort_name.strip(),
        logo=body.logo or "",
        theme=body.theme,
        language=body.language,
        simple_mode=body.simple_mode,
        trusted_ips=[],
    )
    db.add(h)
    db.flush()
    u = User(
        hort_id=h.id,
        name=body.name.strip(),
        username=body.username.strip().lower(),
        pw_hash=hash_pw(body.password),
        role="manager",
    )
    db.add(u)
    db.commit()
    seed(h.id, body.language, body.demo)
    return {**new_session(db, u), "hort": hort_public(h)}


@app.post("/api/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    code = body.hort_code.strip().lower()
    h = db.query(Hort).filter_by(code=code).first()
    if not h:
        raise HTTPException(401, "Wrong hort code, username or password.")
    u = db.query(User).filter_by(hort_id=h.id, username=body.username.strip().lower()).first()
    if not u or not check_pw(body.password, u.pw_hash):
        raise HTTPException(401, "Wrong hort code, username or password.")
    if body.trust_ip:
        ip = client_ip(request)
        if ip:
            trusted = list(h.trusted_ips or [])
            # replace existing entry for this IP
            trusted = [e for e in trusted if e.get("ip") != ip]
            trusted.append({"ip": ip, "user_id": u.id, "label": u.name})
            h.trusted_ips = trusted
            db.commit()
    return {**new_session(db, u), "hort": hort_public(h)}


@app.post("/api/trusted-login")
def trusted_login(body: TrustedLoginIn, request: Request, db: Session = Depends(get_db)):
    """Auto-login if client IP is trusted for this hort."""
    code = body.hort_code.strip().lower()
    h = db.query(Hort).filter_by(code=code).first()
    if not h:
        raise HTTPException(401, "Not trusted.")
    ip = client_ip(request)
    entry = next((e for e in (h.trusted_ips or []) if e.get("ip") == ip), None)
    if not entry:
        raise HTTPException(401, "Not trusted.")
    u = db.get(User, entry.get("user_id"))
    if not u or u.hort_id != h.id:
        raise HTTPException(401, "Not trusted.")
    return {**new_session(db, u), "hort": hort_public(h)}


@app.get("/api/me")
def me(request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    h = hort_of(u, db)
    return {"user": ud(u), "hort": hort_public(h), "client_ip": client_ip(request)}


@app.post("/api/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    s = db.get(UserSession, request.headers.get("authorization", "").removeprefix("Bearer "))
    if s:
        db.delete(s)
        db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- settings + trusted IPs
@app.get("/api/settings")
def read_settings(request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    return hort_public(hort_of(u, db))


@app.put("/api/settings")
def update_settings(body: SettingsIn, request: Request, db: Session = Depends(get_db)):
    u = require_manager(request, db)
    h = hort_of(u, db)
    h.name = body.hort_name
    h.logo = body.logo or ""
    h.theme = body.theme
    h.language = body.language
    h.simple_mode = body.simple_mode
    db.commit()
    return hort_public(h)


@app.get("/api/trusted-ips")
def list_trusted(request: Request, db: Session = Depends(get_db)):
    u = require_manager(request, db)
    h = hort_of(u, db)
    return h.trusted_ips or []


@app.delete("/api/trusted-ips")
def remove_trusted(request: Request, db: Session = Depends(get_db), ip: str = ""):
    u = require_manager(request, db)
    h = hort_of(u, db)
    if not ip:
        raise HTTPException(400, "ip required")
    h.trusted_ips = [e for e in (h.trusted_ips or []) if e.get("ip") != ip]
    db.commit()
    return h.trusted_ips or []


# ---------------------------------------------------------------- users
@app.get("/api/users")
def list_users(request: Request, db: Session = Depends(get_db)):
    u = require_manager(request, db)
    return [ud(x) for x in db.query(User).filter_by(hort_id=u.hort_id).order_by(User.name)]


def other_managers(db, hort_id, uid):
    return db.query(User).filter(User.hort_id == hort_id, User.role == "manager", User.id != uid).count()


@app.post("/api/users", status_code=201)
def create_user(body: UserIn, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    name = body.username.strip().lower()
    if db.query(User).filter_by(hort_id=me.hort_id, username=name).first() or not body.password:
        raise HTTPException(400, "Name already taken.")
    u = User(hort_id=me.hort_id, name=body.name, username=name, pw_hash=hash_pw(body.password), role=body.role)
    db.add(u)
    db.commit()
    return ud(u)


@app.put("/api/users/{uid}")
def update_user(uid: int, body: UserIn, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    u = get_or_404(db, User, uid)
    if u.hort_id != me.hort_id:
        raise HTTPException(404, "Not found.")
    name = body.username.strip().lower()
    if db.query(User).filter(User.hort_id == me.hort_id, User.username == name, User.id != uid).first():
        raise HTTPException(400, "Name already taken.")
    if u.role == "manager" and body.role != "manager" and not other_managers(db, me.hort_id, uid):
        raise HTTPException(400, "At least one manager is required.")
    u.name, u.username, u.role = body.name, name, body.role
    if body.password:
        u.pw_hash = hash_pw(body.password)
    db.commit()
    return ud(u)


@app.delete("/api/users/{uid}")
def delete_user(uid: int, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    u = get_or_404(db, User, uid)
    if u.hort_id != me.hort_id:
        raise HTTPException(404, "Not found.")
    if me.id == uid:
        raise HTTPException(400, "You cannot delete your own account.")
    if u.role == "manager" and not other_managers(db, me.hort_id, uid):
        raise HTTPException(400, "At least one manager is required.")
    db.query(UserSession).filter_by(user_id=uid).delete()
    db.delete(u)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- children
@app.get("/api/children")
def list_children(request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    return [d(c) for c in db.query(Child).filter_by(hort_id=u.hort_id).order_by(Child.last_name, Child.first_name)]


@app.post("/api/children", status_code=201)
def create_child(body: ChildIn, request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    c = Child(hort_id=u.hort_id, **body.model_dump())
    db.add(c)
    db.commit()
    return d(c)


@app.put("/api/children/{cid}")
def update_child(cid: int, body: ChildIn, request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    c = get_or_404(db, Child, cid)
    if c.hort_id != u.hort_id:
        raise HTTPException(404, "Not found.")
    for k, v in body.model_dump().items():
        setattr(c, k, v)
    db.commit()
    return d(c)


@app.delete("/api/children/{cid}")
def delete_child(cid: int, request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    c = get_or_404(db, Child, cid)
    if c.hort_id != u.hort_id:
        raise HTTPException(404, "Not found.")
    db.query(Appointment).filter_by(child_id=cid).delete()
    db.query(AttendanceLog).filter_by(child_id=cid).delete()
    db.delete(c)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- statuses
@app.get("/api/statuses")
def list_statuses(request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    return [d(s) for s in db.query(StatusDefinition).filter_by(hort_id=u.hort_id).order_by(StatusDefinition.id)]


@app.post("/api/statuses", status_code=201)
def create_status(body: StatusIn, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    s = StatusDefinition(hort_id=me.hort_id, **body.model_dump())
    db.add(s)
    db.flush()
    apply_default_flag(db, s)
    db.commit()
    return d(s)


@app.put("/api/statuses/{sid}")
def update_status(sid: int, body: StatusIn, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    s = get_or_404(db, StatusDefinition, sid)
    if s.hort_id != me.hort_id:
        raise HTTPException(404, "Not found.")
    if s.is_default and not body.is_default:
        raise HTTPException(400, "Default status cannot be changed this way.")
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    apply_default_flag(db, s)
    db.commit()
    return d(s)


@app.delete("/api/statuses/{sid}")
def delete_status(sid: int, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    s = get_or_404(db, StatusDefinition, sid)
    if s.hort_id != me.hort_id:
        raise HTTPException(404, "Not found.")
    if s.is_default:
        raise HTTPException(400, "Default status cannot be changed this way.")
    db.query(AttendanceLog).filter_by(status_id=sid).update({"status_id": None})
    db.delete(s)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- appointment types
@app.get("/api/appointment-types")
def list_types(request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    return [d(t) for t in db.query(AppointmentType).filter_by(hort_id=u.hort_id).order_by(AppointmentType.name)]


@app.post("/api/appointment-types", status_code=201)
def create_type(body: TypeIn, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    t = AppointmentType(hort_id=me.hort_id, **body.model_dump())
    db.add(t)
    db.commit()
    return d(t)


@app.put("/api/appointment-types/{tid}")
def update_type(tid: int, body: TypeIn, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    t = get_or_404(db, AppointmentType, tid)
    if t.hort_id != me.hort_id:
        raise HTTPException(404, "Not found.")
    for k, v in body.model_dump().items():
        setattr(t, k, v)
    db.commit()
    return d(t)


@app.delete("/api/appointment-types/{tid}")
def delete_type(tid: int, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    t = get_or_404(db, AppointmentType, tid)
    if t.hort_id != me.hort_id:
        raise HTTPException(404, "Not found.")
    db.delete(t)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- appointments
@app.get("/api/appointments")
def list_appointments(request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    return [d(a) for a in db.query(Appointment).filter_by(hort_id=u.hort_id).order_by(Appointment.start_time)]


@app.post("/api/appointments", status_code=201)
def create_appointment(body: AppointmentIn, request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    c = get_or_404(db, Child, body.child_id)
    if c.hort_id != u.hort_id:
        raise HTTPException(404, "Not found.")
    a = Appointment(hort_id=u.hort_id, **body.model_dump())
    db.add(a)
    db.commit()
    return d(a)


@app.put("/api/appointments/{aid}")
def update_appointment(aid: int, body: AppointmentIn, request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    a = get_or_404(db, Appointment, aid)
    if a.hort_id != u.hort_id:
        raise HTTPException(404, "Not found.")
    for k, v in body.model_dump().items():
        setattr(a, k, v)
    db.commit()
    return d(a)


@app.delete("/api/appointments/{aid}")
def delete_appointment(aid: int, request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    a = get_or_404(db, Appointment, aid)
    if a.hort_id != u.hort_id:
        raise HTTPException(404, "Not found.")
    db.delete(a)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- attendance / logs
@app.post("/api/attendance", status_code=201)
def mark_attendance(body: AttendanceIn, request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    c = get_or_404(db, Child, body.child_id)
    if c.hort_id != u.hort_id:
        raise HTTPException(404, "Not found.")
    if body.status_id is None and not (body.note or "").strip():
        raise HTTPException(400, "Status or note required.")
    log = AttendanceLog(
        hort_id=u.hort_id, child_id=body.child_id, status_id=body.status_id,
        note=(body.note or "").strip() or None, timestamp=datetime.now(),
    )
    db.add(log)
    db.commit()
    return d(log)


@app.get("/api/logs")
def list_logs(request: Request, db: Session = Depends(get_db), limit: int = 80):
    u = require_user(request, db)
    q = db.query(AttendanceLog).filter_by(hort_id=u.hort_id)
    children = {c.id: c for c in db.query(Child).filter_by(hort_id=u.hort_id)}
    statuses = {s.id: s for s in db.query(StatusDefinition).filter_by(hort_id=u.hort_id)}
    out = []
    for l in q.order_by(AttendanceLog.timestamp.desc()).limit(limit):
        c, s = children.get(l.child_id), statuses.get(l.status_id)
        out.append({
            **d(l),
            "child_name": f"{c.first_name} {c.last_name}" if c else "?",
            "status": d(s) if s else None,
        })
    return out


# ---------------------------------------------------------------- time slots + schedule (Excel Stundenplan)
@app.get("/api/time-slots")
def list_slots(request: Request, db: Session = Depends(get_db)):
    u = require_user(request, db)
    rows = db.query(TimeSlot).filter_by(hort_id=u.hort_id).order_by(TimeSlot.sort_order, TimeSlot.start_time).all()
    return [d(r) for r in rows]


@app.post("/api/time-slots", status_code=201)
def create_slot(body: TimeSlotIn, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    row = TimeSlot(hort_id=me.hort_id, **body.model_dump())
    db.add(row)
    db.commit()
    return d(row)


@app.put("/api/time-slots/{sid}")
def update_slot(sid: int, body: TimeSlotIn, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    row = get_or_404(db, TimeSlot, sid)
    if row.hort_id != me.hort_id:
        raise HTTPException(404, "Not found.")
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    db.commit()
    return d(row)


@app.delete("/api/time-slots/{sid}")
def delete_slot(sid: int, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    row = get_or_404(db, TimeSlot, sid)
    if row.hort_id != me.hort_id:
        raise HTTPException(404, "Not found.")
    db.query(ScheduleCell).filter_by(slot_id=sid).delete()
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.get("/api/schedule")
def list_schedule(request: Request, db: Session = Depends(get_db), group: Optional[str] = None):
    """Return cells. Optional ?group= filters (use empty string for shared only)."""
    u = require_user(request, db)
    q = db.query(ScheduleCell).filter_by(hort_id=u.hort_id)
    if group is not None:
        q = q.filter_by(group_name=group)
    return [d(r) for r in q.order_by(ScheduleCell.day_of_week, ScheduleCell.slot_id).all()]


@app.put("/api/schedule/cell")
def upsert_cell(body: ScheduleCellIn, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    slot = get_or_404(db, TimeSlot, body.slot_id)
    if slot.hort_id != me.hort_id:
        raise HTTPException(404, "Not found.")
    existing = db.query(ScheduleCell).filter_by(
        hort_id=me.hort_id,
        group_name=body.group_name,
        day_of_week=body.day_of_week,
        slot_id=body.slot_id,
    ).first()
    data = body.model_dump()
    # empty title → delete cell
    if not (data.get("title") or "").strip() and not (data.get("room") or "").strip() and not (data.get("teacher") or "").strip():
        if existing:
            db.delete(existing)
            db.commit()
        return {"ok": True, "deleted": True}
    if existing:
        for k, v in data.items():
            setattr(existing, k, v)
        db.commit()
        return d(existing)
    row = ScheduleCell(hort_id=me.hort_id, **data)
    db.add(row)
    db.commit()
    return d(row)


@app.put("/api/schedule/bulk")
def bulk_schedule(body: BulkScheduleIn, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    out = []
    for cell in body.cells:
        slot = db.get(TimeSlot, cell.slot_id)
        if not slot or slot.hort_id != me.hort_id:
            continue
        existing = db.query(ScheduleCell).filter_by(
            hort_id=me.hort_id,
            group_name=cell.group_name,
            day_of_week=cell.day_of_week,
            slot_id=cell.slot_id,
        ).first()
        data = cell.model_dump()
        empty = not (data.get("title") or "").strip() and not (data.get("room") or "").strip() and not (data.get("teacher") or "").strip()
        if empty:
            if existing:
                db.delete(existing)
            continue
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
            out.append(existing)
        else:
            row = ScheduleCell(hort_id=me.hort_id, **data)
            db.add(row)
            out.append(row)
    db.commit()
    return [d(r) for r in out]


@app.delete("/api/schedule/{cid}")
def delete_cell(cid: int, request: Request, db: Session = Depends(get_db)):
    me = require_manager(request, db)
    row = get_or_404(db, ScheduleCell, cid)
    if row.hort_id != me.hort_id:
        raise HTTPException(404, "Not found.")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- roster
@app.get("/api/roster")
def roster(request: Request, now: Optional[str] = None, db: Session = Depends(get_db)):
    u = require_user(request, db)
    hid = u.hort_id
    n = datetime.now()
    cur = minutes(now) if now else n.hour * 60 + n.minute
    today = n.date()
    default = db.query(StatusDefinition).filter_by(hort_id=hid, is_default=True).first()
    statuses = {s.id: s for s in db.query(StatusDefinition).filter_by(hort_id=hid)}
    logs = (
        db.query(AttendanceLog)
        .filter(AttendanceLog.hort_id == hid, AttendanceLog.timestamp >= datetime.combine(today, time.min))
        .order_by(AttendanceLog.timestamp)
        .all()
    )
    last_status, last_note = {}, {}
    for l in logs:
        if l.status_id in statuses:
            last_status[l.child_id] = l
        if l.note:
            last_note[l.child_id] = l
    types = {t.name: t for t in db.query(AppointmentType).filter_by(hort_id=hid)}
    appts = {}
    for a in db.query(Appointment).filter_by(hort_id=hid):
        today_hit = (a.is_recurring and a.day_of_week == today.weekday()) or (
            not a.is_recurring and a.date == today.isoformat()
        )
        if not today_hit:
            continue
        s = minutes(a.start_time)
        e = s + (a.duration_minutes or 45)
        lead = a.alert_lead_minutes or 0
        state = "done" if cur >= e else "away" if cur >= s else "upcoming" if cur >= s - lead else "later"
        t = types.get(a.category)
        appts.setdefault(a.child_id, []).append(
            {**d(a), "state": state, "minutes_until": s - cur, "color": t.color if t else "#64748b"}
        )
    result = []
    for c in db.query(Child).filter_by(hort_id=hid).order_by(Child.first_name):
        ls = last_status.get(c.id)
        st = statuses.get(ls.status_id) if ls else default
        ln = last_note.get(c.id)
        result.append({
            **d(c),
            "status": d(st) if st else None,
            "status_since": ls.timestamp.isoformat() if ls else None,
            "last_note": {"note": ln.note, "timestamp": ln.timestamp.isoformat()} if ln else None,
            "appointments": sorted(appts.get(c.id, []), key=lambda a: a["start_time"]),
        })
    return result


# ---------------------------------------------------------------- seed + startup
DEFAULT_SLOTS = [
    ("", "07:45", "08:30", 0),
    ("", "08:35", "09:20", 1),
    ("", "09:25", "10:10", 2),
    ("", "10:30", "11:15", 3),
    ("", "11:20", "12:05", 4),
    ("", "12:15", "13:00", 5),
    ("", "13:30", "14:15", 6),
    ("", "14:20", "15:05", 7),
    ("", "15:20", "16:05", 8),
    ("", "16:10", "16:55", 9),
    ("", "17:00", "17:45", 10),
]


def seed(hort_id: int, lang="de", demo=True):
    db = SessionLocal()
    try:
        if db.query(StatusDefinition).filter_by(hort_id=hort_id).count() == 0:
            labels = (
                [
                    ("Expected", "Present", "Absent", "En Route", "In Appointment", "Picked Up"),
                    ("Physio", "Music Lesson", "Tutoring", "Sports Practice", "Therapy"),
                ]
                if lang == "en"
                else [
                    ("Erwartet", "Anwesend", "Abwesend", "Unterwegs", "Im Termin", "Abgeholt"),
                    ("Physio", "Musikstunde", "Nachhilfe", "Sporttraining", "Therapie"),
                ]
            )
            db.add_all([
                StatusDefinition(hort_id=hort_id, label=labels[0][0], color_code="#64748b", is_default=True, icon="🕑"),
                StatusDefinition(hort_id=hort_id, label=labels[0][1], color_code="#15803d", icon="✅"),
                StatusDefinition(hort_id=hort_id, label=labels[0][2], color_code="#b91c1c", icon="🚫"),
                StatusDefinition(hort_id=hort_id, label=labels[0][3], color_code="#b45309", icon="🚶"),
                StatusDefinition(hort_id=hort_id, label=labels[0][4], color_code="#1d4ed8", icon="🩺"),
                StatusDefinition(hort_id=hort_id, label=labels[0][5], color_code="#7e22ce", icon="👋"),
            ])
            db.add_all([
                AppointmentType(hort_id=hort_id, name=labels[1][0], color="#0d9488", default_lead_minutes=15),
                AppointmentType(hort_id=hort_id, name=labels[1][1], color="#c026d3", default_lead_minutes=10),
                AppointmentType(hort_id=hort_id, name=labels[1][2], color="#ca8a04", default_lead_minutes=10),
                AppointmentType(hort_id=hort_id, name=labels[1][3], color="#ea580c", default_lead_minutes=15),
                AppointmentType(hort_id=hort_id, name=labels[1][4], color="#2563eb", default_lead_minutes=30),
            ])
            for label, start, end, order in DEFAULT_SLOTS:
                db.add(TimeSlot(hort_id=hort_id, label=label, start_time=start, end_time=end, sort_order=order))
            db.flush()
            slots = db.query(TimeSlot).filter_by(hort_id=hort_id).order_by(TimeSlot.sort_order).all()

            if demo:
                kids = [
                    ("Mia", "Keller", "Gruppe A", {"Allergien": "Erdnüsse"}),
                    ("Noah", "Brunner", "Gruppe A", {"Ernährung": "Vegetarisch"}),
                    ("Lina", "Meier", "Gruppe B", {}),
                    ("Leo", "Schmid", "Gruppe B", {}),
                    ("Emma", "Weber", "Gruppe C", {}),
                ]
                children = []
                for fn, ln, g, cf in kids:
                    c = Child(hort_id=hort_id, first_name=fn, last_name=ln, group_name=g, custom_fields=cf)
                    db.add(c)
                    children.append(c)
                db.flush()
                # sample shared lunch Mon–Fri at slot index 5 (12:15)
                lunches_de = ["Spaghetti Bolognese", "Gemüsecurry", "Pouletgeschnetzeltes", "Fischstäbchen", "Pizza"]
                lunches_en = ["Spaghetti Bolognese", "Veggie curry", "Chicken strips", "Fish fingers", "Pizza"]
                lunches = lunches_en if lang == "en" else lunches_de
                if len(slots) > 5:
                    for day, title in enumerate(lunches):
                        db.add(ScheduleCell(
                            hort_id=hort_id, group_name="", day_of_week=day, slot_id=slots[5].id,
                            title=title, kind="lunch", room="Mensa",
                        ))
                # sample group programs
                if len(slots) > 6:
                    db.add(ScheduleCell(
                        hort_id=hort_id, group_name="Gruppe A", day_of_week=0, slot_id=slots[6].id,
                        title="Freispiel & Garten", room="Garten", teacher="",
                    ))
                    db.add(ScheduleCell(
                        hort_id=hort_id, group_name="Gruppe B", day_of_week=1, slot_id=slots[6].id,
                        title="Basteln", room="Werkraum", teacher="Julia",
                    ))
                    db.add(ScheduleCell(
                        hort_id=hort_id, group_name="", day_of_week=2, slot_id=slots[6].id,
                        title="Ausflug", room="", teacher="", notes="Wetterabhängig",
                    ))
            db.commit()
    finally:
        db.close()


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")
