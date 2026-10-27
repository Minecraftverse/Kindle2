"""Hort Manager upgrades: meal checklist (Mittagessen / Zvieri), bulk check-in,
CSV import and database indexes. Activated from main.py with install(...)."""

import csv
import io
from datetime import date, datetime
from typing import List

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

HEAD = {
    "first_name": ("first_name", "first name", "firstname", "vorname"),
    "last_name": ("last_name", "last name", "lastname", "nachname", "name"),
    "group_name": ("group", "group_name", "gruppe"),
}


def install(app, engine, Base, get_db, Child, AttendanceLog, StatusDefinition):
    class MealLog(Base):
        """One row = this child ate this meal on this day. No row = not yet."""
        __tablename__ = "meal_logs"
        id = Column(Integer, primary_key=True)
        child_id = Column(Integer, ForeignKey("children.id", ondelete="CASCADE"), nullable=False)
        day = Column(String, nullable=False)  # YYYY-MM-DD, so the list resets every day
        meal = Column(String, nullable=False)  # lunch | zvieri
        timestamp = Column(DateTime, default=datetime.now)
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
        for m in db.query(MealLog).filter_by(day=date.today().isoformat()):
            out.setdefault(m.child_id, {})[m.meal] = True
        return out

    @app.post("/api/meals")
    def set_meals(body: MealIn, db=Depends(get_db)):
        today = date.today().isoformat()
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
        now = datetime.now()
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

    Base.metadata.create_all(engine)  # creates meal_logs
    with engine.begin() as c:
        c.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_att_child_ts ON attendance_logs (child_id, timestamp)")
        c.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_appt_child ON appointments (child_id)")
        c.exec_driver_sql("DELETE FROM meal_logs WHERE day < date('now', '-30 day')")
