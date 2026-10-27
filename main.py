"""Hort Manager - attendance, appointments and daily log. Run: uvicorn main:app --reload"""
import hashlib
import secrets
from datetime import datetime, time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

BASE = Path(__file__).parent
engine = create_engine(f"sqlite:///{BASE / 'hort.db'}", connect_args={"check_same_thread": False})
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
    timestamp = Column(DateTime, default=datetime.now)
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
    day_of_week = Column(Integer, nullable=False)  # 0=Monday .. 6=Sunday
    kind = Column(String, default="program")  # "program" | "lunch"
    title = Column(String, nullable=False)
    start_time = Column(String, default="")  # HH:MM optional for lunch
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
    if p.startswith("/api/") and p not in PUBLIC:
        db = SessionLocal()
        try:
            u = user_from(request, db)
            if not u:
                return JSONResponse({"detail": "Please sign in."}, 401)
            if u.role != "manager" and p.startswith(MANAGER_ONLY) and (request.method != "GET" or p.startswith("/api/users")):
                return JSONResponse({"detail": "Only the manager can do this."}, 403)
        finally:
            db.close()
    return await call_next(request)


class SettingsIn(BaseModel):
    hort_name: str = Field(min_length=1, max_length=60)
    logo: str = Field("", max_length=400_000)  # small data: URL
    theme: str = Field("wald", pattern="^(wald|himmel|sonne|beere|kontrast)$")
    language: str = Field("de", pattern="^(de|en)$")
    simple_mode: bool = False


class SetupIn(SettingsIn):
    name: str = Field(min_length=1)
    username: str = Field(min_length=2)
    password: str = Field(min_length=6)
    demo: bool = False


class LoginIn(BaseModel):
    username: str
    password: str


class UserIn(BaseModel):
    name: str = Field(min_length=1)
    username: str = Field(min_length=2)
    password: Optional[str] = Field(None, min_length=6)
    role: str = Field("staff", pattern="^(manager|staff)$")


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


@app.post("/api/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    u = db.query(User).filter_by(username=body.username.strip().lower()).first()
    if not u or not check_pw(body.password, u.pw_hash):
        raise HTTPException(401, "Wrong username or password.")
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
    db.delete(c)
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
                          note=(body.note or "").strip() or None, timestamp=datetime.now())
    db.add(entry)
    db.commit()
    return d(entry)


@app.get("/api/logs")
def list_logs(child_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Today's log feed, newest first."""
    start = datetime.combine(datetime.now().date(), time.min)
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
    n = datetime.now()
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
    result = []
    for c in db.query(Child).order_by(Child.first_name):
        ls = last_status.get(c.id)
        st = statuses.get(ls.status_id) if ls else default
        ln = last_note.get(c.id)
        result.append({**d(c), "status": d(st) if st else None,
                       "status_since": ls.timestamp.isoformat() if ls else None,
                       "last_note": {"note": ln.note, "timestamp": ln.timestamp.isoformat()} if ln else None,
                       "appointments": sorted(appts.get(c.id, []), key=lambda a: a["start_time"])})
    return result


# ---------------------------------------------------------------- seed + startup
def seed(lang="de", demo=True):
    db = SessionLocal()
    if db.query(StatusDefinition).count() == 0:
        db.add_all([
            StatusDefinition(label="Expected", color_code="#64748b", is_default=True, icon="🕑"),
            StatusDefinition(label="Present", color_code="#15803d", icon="✅"),
            StatusDefinition(label="Absent", color_code="#b91c1c", icon="🚫"),
            StatusDefinition(label="En Route", color_code="#b45309", icon="🚶"),
            StatusDefinition(label="In Appointment", color_code="#1d4ed8", icon="🩺"),
            StatusDefinition(label="Picked Up", color_code="#7e22ce", icon="👋"),
        ])
        db.add_all([
            AppointmentType(name="Physio", color="#0d9488", default_lead_minutes=15),
            AppointmentType(name="Music Lesson", color="#c026d3", default_lead_minutes=10),
            AppointmentType(name="Tutoring", color="#ca8a04", default_lead_minutes=10),
            AppointmentType(name="Sports Practice", color="#ea580c", default_lead_minutes=15),
            AppointmentType(name="Therapy", color="#2563eb", default_lead_minutes=30),
        ])
        kids = [
            ("Mia", "Keller", "Group A", {"Allergies": "Peanuts", "Bus Route": "12", "Authorized Pickups": "Mother, Grandma Rosa"}),
            ("Noah", "Brunner", "Group A", {"Dietary Restrictions": "Vegetarian", "Emergency Contact": "+41 79 555 01 02"}),
            ("Lina", "Meier", "Group B", {"Allergies": "Lactose", "Authorized Pickups": "Father, Uncle Tom"}),
            ("Luca", "Frei", "Group B", {"Bus Route": "7"}),
            ("Emma", "Huber", "Group A", {"Emergency Contact": "+41 78 555 03 04"}),
            ("Elias", "Weber", "Group C", {"Allergies": "Bee stings", "Authorized Pickups": "Both parents"}),
            ("Sofia", "Gerber", "Group C", {}),
            ("Jonas", "Baumann", "Group B", {"Dietary Restrictions": "Halal"}),
        ]
        children = [Child(first_name=f, last_name=l, group_name=g, custom_fields=cf) for f, l, g, cf in kids]
        db.add_all(children)
        db.flush()
        n = datetime.now()
        wd, base = n.weekday(), n.hour * 60 + n.minute

        def hhmm(m):
            m %= 1440
            return f"{m // 60:02d}:{m % 60:02d}"

        db.add_all([  # relative to "now" so the alert banner is visible in a demo
            Appointment(child_id=children[0].id, title="Physio session", category="Physio", day_of_week=wd,
                        start_time=hhmm(base + 12), duration_minutes=45, alert_lead_minutes=15, is_recurring=True),
            Appointment(child_id=children[3].id, title="Piano lesson", category="Music Lesson", day_of_week=wd,
                        start_time=hhmm(base + 70), duration_minutes=30, alert_lead_minutes=10, is_recurring=True),
            Appointment(child_id=children[5].id, title="Speech therapy", category="Therapy", day_of_week=1,
                        start_time="15:30", duration_minutes=50, alert_lead_minutes=30, is_recurring=True),
            Appointment(child_id=children[1].id, title="Maths tutoring", category="Tutoring", is_recurring=False,
                        date=n.date().isoformat(), start_time=hhmm(base + 150), duration_minutes=60,
                        alert_lead_minutes=10),
        ])
        # Sample weekly Stundenplan + lunches (Mon–Fri)
        sample_plan = [
            # Lunches
            WeeklyPlan(day_of_week=0, kind="lunch", title="Spaghetti Bolognese", start_time="12:00", notes="mit Parmesan"),
            WeeklyPlan(day_of_week=1, kind="lunch", title="Gemüsecurry mit Reis", start_time="12:00", notes="vegetarisch"),
            WeeklyPlan(day_of_week=2, kind="lunch", title="Pouletgeschnetzeltes", start_time="12:00", notes="mit Rösti"),
            WeeklyPlan(day_of_week=3, kind="lunch", title="Fischstäbchen & Kartoffeln", start_time="12:00", notes=""),
            WeeklyPlan(day_of_week=4, kind="lunch", title="Pizza Margherita", start_time="12:00", notes=""),
            # Programme
            WeeklyPlan(day_of_week=0, kind="program", title="Freispiel & Garten", start_time="14:00", end_time="16:00", sort_order=1),
            WeeklyPlan(day_of_week=1, kind="program", title="Basteln / Werken", start_time="14:00", end_time="15:30", sort_order=1),
            WeeklyPlan(day_of_week=2, kind="program", title="Ausflug am Nachmittag", start_time="13:30", end_time="16:30", notes="Wetterabhängig – Rucksack mitnehmen", sort_order=1),
            WeeklyPlan(day_of_week=3, kind="program", title="Sport / Bewegung", start_time="14:00", end_time="15:30", sort_order=1),
            WeeklyPlan(day_of_week=4, kind="program", title="Spiele-Nachmittag", start_time="14:00", end_time="16:00", sort_order=1),
        ]
        db.add_all(sample_plan)
        db.flush()
        if not demo:  # clean install: keep statuses + types, drop sample kids & plan
            db.query(Appointment).delete()
            db.query(Child).delete()
            db.query(WeeklyPlan).delete()
        elif lang == "de":
            TR = {"Group": "Gruppe", "Physio session": "Physiotherapie", "Piano lesson": "Klavierstunde",
                  "Speech therapy": "Logopädie", "Maths tutoring": "Mathe-Nachhilfe", "Allergies": "Allergien",
                  "Dietary Restrictions": "Ernährung", "Emergency Contact": "Notfallkontakt",
                  "Authorized Pickups": "Abholberechtigte", "Bus Route": "Busline"}
            for a in db.query(Appointment):
                a.title, a.category = TR.get(a.title, a.title), DE_TYPES.get(a.category, a.category)
            for c in db.query(Child):
                c.group_name = c.group_name.replace("Group", "Gruppe")
                c.custom_fields = {TR.get(k, k): v for k, v in c.custom_fields.items()}
        else:
            # English sample titles for demo
            EN_PLAN = {
                "Spaghetti Bolognese": "Spaghetti Bolognese",
                "Gemüsecurry mit Reis": "Vegetable curry with rice",
                "Pouletgeschnetzeltes": "Chicken strips",
                "Fischstäbchen & Kartoffeln": "Fish fingers & potatoes",
                "Pizza Margherita": "Pizza Margherita",
                "Freispiel & Garten": "Free play & garden",
                "Basteln / Werken": "Crafts / workshop",
                "Ausflug am Nachmittag": "Afternoon excursion",
                "Sport / Bewegung": "Sports / movement",
                "Spiele-Nachmittag": "Games afternoon",
                "mit Parmesan": "with Parmesan",
                "vegetarisch": "vegetarian",
                "mit Rösti": "with rösti",
                "Wetterabhängig – Rucksack mitnehmen": "Weather-dependent – bring backpack",
            }
            for p in db.query(WeeklyPlan):
                p.title = EN_PLAN.get(p.title, p.title)
                p.notes = EN_PLAN.get(p.notes, p.notes)
        if lang == "de":
            for s in db.query(StatusDefinition):
                s.label = DE_STATUS.get(s.label, s.label)
            for t in db.query(AppointmentType):
                t.name = DE_TYPES.get(t.name, t.name)
        db.commit()
    db.close()


DE_STATUS = {"Expected": "Erwartet", "Present": "Anwesend", "Absent": "Abwesend", "En Route": "Unterwegs",
             "In Appointment": "Im Termin", "Picked Up": "Abgeholt"}
DE_TYPES = {"Music Lesson": "Musikstunde", "Tutoring": "Nachhilfe", "Sports Practice": "Sporttraining",
            "Therapy": "Therapie"}

Base.metadata.create_all(engine)


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")
