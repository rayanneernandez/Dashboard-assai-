import hashlib
from datetime import date
from typing import List, Dict, Tuple

import pandas as pd
import pytz
from sqlalchemy import (
    create_engine, Column, Integer, String, Date, Float, TIMESTAMP,
    func, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.dialects.postgresql import insert

# ===== Config =====
DATABASE_URL = "postgresql://assai_user:kcs2kMoOIsz9iI8GIBf0Rlt85QOfh1Ob@dpg-d468aqa4d50c73cfc0ug-a.oregon-postgres.render.com/assai"
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()
brazil_tz = pytz.timezone("America/Sao_Paulo")

# ===== Models =====
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(TIMESTAMP, server_default=func.now())

class DashboardDaily(Base):
    __tablename__ = "dashboard_daily"
    day = Column(Date, primary_key=True, nullable=False)             # PK parte 1
    store_id = Column(String, primary_key=True, nullable=False)      # PK parte 2
    total_visitors = Column(Integer, default=0)
    male = Column(Integer, default=0)
    female = Column(Integer, default=0)
    avg_age_sum = Column(Float, default=0.0)
    avg_age_count = Column(Integer, default=0)
    age_18_25 = Column(Integer, default=0)
    age_26_35 = Column(Integer, default=0)
    age_36_45 = Column(Integer, default=0)
    age_46_60 = Column(Integer, default=0)
    age_60_plus = Column(Integer, default=0)
    monday = Column(Integer, default=0)
    tuesday = Column(Integer, default=0)
    wednesday = Column(Integer, default=0)
    thursday = Column(Integer, default=0)
    friday = Column(Integer, default=0)
    saturday = Column(Integer, default=0)
    sunday = Column(Integer, default=0)
    __table_args__ = (UniqueConstraint("day", "store_id", name="uq_day_store"),)

# ===== Init =====
def init_db():
    Base.metadata.create_all(engine)

# ===== Users API =====
def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def create_user(email: str, password: str) -> None:
    hashed = _hash_password(password)
    with SessionLocal() as s:
        from sqlalchemy.dialects.postgresql import insert
        stmt = insert(User).values(email=email, password_hash=hashed)
        stmt = stmt.on_conflict_do_update(
            index_elements=[User.email],
            set_={"password_hash": hashed}
        )
        s.execute(stmt)
        s.commit()

def verify_user(email: str, password: str) -> bool:
    with SessionLocal() as s:
        u = s.query(User).filter(User.email == email).first()
        return bool(u and u.password_hash == _hash_password(password))

# ===== Dashboard aggregation =====
def _aggregate_by_day(visitors: List[Dict]) -> Dict[Tuple[date, str], Dict]:
    df = pd.DataFrame(visitors or [])
    if df.empty:
        return {}

    for col in ["sex", "age", "start"]:
        if col not in df.columns:
            df[col] = None

    if "id" in df.columns:
        df = df.drop_duplicates(subset=["id"])
    else:
        subset_cols = [c for c in ["visitor_id", "start"] if c in df.columns]
        if subset_cols:
            df = df.sort_values(by=subset_cols).drop_duplicates(subset=subset_cols, keep="first")

    dt_iso = pd.to_datetime(df["start"], errors="coerce", utc=True)
    start_numeric = pd.to_numeric(df["start"], errors="coerce")
    dt_epoch = pd.to_datetime(start_numeric, unit="s", errors="coerce", utc=True)
    df["start_dt_utc"] = dt_iso.fillna(dt_epoch)
    df = df.dropna(subset=["start_dt_utc"])
    df["start_dt_brt"] = df["start_dt_utc"].dt.tz_convert(brazil_tz)
    df["day"] = df["start_dt_brt"].dt.date
    df["weekday_en"] = df["start_dt_brt"].dt.day_name()

    # Normalização robusta de sexo
    def normalize_sex(val):
        if pd.isna(val):
            return None
        s = str(val).strip().lower()
        if s in {"1", "m", "male"}:
            return "male"
        if s in {"2", "f", "female"}:
            return "female"
        return None
    sex_norm = df["sex"].apply(normalize_sex)
    df["male"] = (sex_norm == "male").astype(int)
    df["female"] = (sex_norm == "female").astype(int)

    # Faixas etárias e média
    ages = pd.to_numeric(df["age"], errors="coerce")
    df["age_18_25"] = ((ages >= 18) & (ages <= 25)).astype(int)
    df["age_26_35"] = ((ages >= 26) & (ages <= 35)).astype(int)
    df["age_36_45"] = ((ages >= 36) & (ages <= 45)).astype(int)
    df["age_46_60"] = ((ages >= 46) & (ages <= 60)).astype(int)
    df["age_60_plus"] = ((ages > 60)).astype(int)
    df["avg_age_sum"] = ages.fillna(0)
    df["avg_age_count"] = (~ages.isna()).astype(int)

    grouped = df.groupby("day").agg({
        "male": "sum", "female": "sum",
        "age_18_25": "sum", "age_26_35": "sum",
        "age_36_45": "sum", "age_46_60": "sum", "age_60_plus": "sum",
        "avg_age_sum": "sum", "avg_age_count": "sum"
    })
    totals = df.groupby("day").size().rename("total_visitors")
    weekday_series = df.groupby("day")["weekday_en"].first()

    result = {}
    for day_dt in grouped.index:
        weekday_en = weekday_series.loc[day_dt]
        m = grouped.loc[day_dt].to_dict()
        m["total_visitors"] = int(totals.loc[day_dt])
        for w in ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]:
            m[w.lower()] = 0
        m[weekday_en.lower()] = int(totals.loc[day_dt])
        result[(day_dt, weekday_en)] = m
    return result

def upsert_daily_from_visitors(visitors: List[Dict], store_id: str) -> None:
    daily = _aggregate_by_day(visitors)
    if not daily:
        return
    with SessionLocal() as s:
        for (day_dt, _weekday), m in daily.items():
            # cria o Insert
            ins = insert(DashboardDaily).values(
                day=day_dt, store_id=str(store_id),
                total_visitors=int(m.get("total_visitors", 0)),
                male=int(m.get("male", 0)),
                female=int(m.get("female", 0)),
                avg_age_sum=float(m.get("avg_age_sum", 0.0)),
                avg_age_count=int(m.get("avg_age_count", 0)),
                age_18_25=int(m.get("age_18_25", 0)),
                age_26_35=int(m.get("age_26_35", 0)),
                age_36_45=int(m.get("age_36_45", 0)),
                age_46_60=int(m.get("age_46_60", 0)),
                age_60_plus=int(m.get("age_60_plus", 0)),
                monday=int(m.get("monday", 0)),
                tuesday=int(m.get("tuesday", 0)),
                wednesday=int(m.get("wednesday", 0)),
                thursday=int(m.get("thursday", 0)),
                friday=int(m.get("friday", 0)),
                saturday=int(m.get("saturday", 0)),
                sunday=int(m.get("sunday", 0)),
            )

            update_vals = {
                "total_visitors": ins.excluded.total_visitors,
                "male": ins.excluded.male,
                "female": ins.excluded.female,
                "avg_age_sum": ins.excluded.avg_age_sum,
                "avg_age_count": ins.excluded.avg_age_count,
                "age_18_25": ins.excluded.age_18_25,
                "age_26_35": ins.excluded.age_26_35,
                "age_36_45": ins.excluded.age_36_45,
                "age_46_60": ins.excluded.age_46_60,
                "age_60_plus": ins.excluded.age_60_plus,
                "monday": ins.excluded.monday,
                "tuesday": ins.excluded.tuesday,
                "wednesday": ins.excluded.wednesday,
                "thursday": ins.excluded.thursday,
                "friday": ins.excluded.friday,
                "saturday": ins.excluded.saturday,
                "sunday": ins.excluded.sunday,
            }

            stmt = ins.on_conflict_do_update(
                index_elements=["day", "store_id"],
                set_=update_vals
            )
            s.execute(stmt)
        s.commit()

def get_aggregated_stats(store_id: str, start_day: date, end_day: date) -> Dict:
    with SessionLocal() as s:
        q = s.query(
            func.sum(DashboardDaily.total_visitors),
            func.sum(DashboardDaily.male),
            func.sum(DashboardDaily.female),
            func.sum(DashboardDaily.avg_age_sum),
            func.sum(DashboardDaily.avg_age_count),
            func.sum(DashboardDaily.age_18_25),
            func.sum(DashboardDaily.age_26_35),
            func.sum(DashboardDaily.age_36_45),
            func.sum(DashboardDaily.age_46_60),
            func.sum(DashboardDaily.age_60_plus),
            func.sum(DashboardDaily.monday),
            func.sum(DashboardDaily.tuesday),
            func.sum(DashboardDaily.wednesday),
            func.sum(DashboardDaily.thursday),
            func.sum(DashboardDaily.friday),
            func.sum(DashboardDaily.saturday),
            func.sum(DashboardDaily.sunday),
        ).filter(
            DashboardDaily.store_id == str(store_id),
            DashboardDaily.day.between(start_day, end_day)
        ).one()
    vals = [v or 0 for v in q]
    (total_visitors, male, female, avg_age_sum, avg_age_count,
     a1825, a2635, a3645, a4660, a60p,
     mon, tue, wed, thu, fri, sat, sun) = vals
    avg_age = round(float(avg_age_sum) / avg_age_count, 1) if avg_age_count > 0 else 0.0
    return {
        "total_visitors": int(total_visitors),
        "male": int(male),
        "female": int(female),
        "avg_age": avg_age,
        "age_distribution": {
            "18-25": int(a1825), "26-35": int(a2635),
            "36-45": int(a3645), "46-60": int(a4660), "60+": int(a60p)
        },
        "weekday_visits": {
            "Monday": int(mon), "Tuesday": int(tue), "Wednesday": int(wed),
            "Thursday": int(thu), "Friday": int(fri), "Saturday": int(sat), "Sunday": int(sun)
        }
    }

def get_last_cached_stats(store_id: str) -> Dict:
    """
    Retorna o último agregado disponível no banco para a loja,
    usando o dia mais recente com qualquer quantidade > 0.
    """
    with SessionLocal() as s:
        last = (
            s.query(DashboardDaily)
            .filter(DashboardDaily.store_id == str(store_id))
            .order_by(DashboardDaily.day.desc())
            .first()
        )
    if not last:
        return {
            "total_visitors": 0,
            "male": 0,
            "female": 0,
            "avg_age": 0.0,
            "age_distribution": {"18-25": 0, "26-35": 0, "36-45": 0, "46-60": 0, "60+": 0},
            "weekday_visits": {"Monday": 0, "Tuesday": 0, "Wednesday": 0, "Thursday": 0, "Friday": 0, "Saturday": 0, "Sunday": 0},
        }
    avg_age = round(float(last.avg_age_sum or 0.0) / (last.avg_age_count or 1), 1) if (last.avg_age_count or 0) > 0 else 0.0
    return {
        "total_visitors": int(last.total_visitors or 0),
        "male": int(last.male or 0),
        "female": int(last.female or 0),
        "avg_age": avg_age,
        "age_distribution": {
            "18-25": int(last.age_18_25 or 0),
            "26-35": int(last.age_26_35 or 0),
            "36-45": int(last.age_36_45 or 0),
            "46-60": int(last.age_46_60 or 0),
            "60+": int(last.age_60_plus or 0),
        },
        "weekday_visits": {
            "Monday": int(last.monday or 0),
            "Tuesday": int(last.tuesday or 0),
            "Wednesday": int(last.wednesday or 0),
            "Thursday": int(last.thursday or 0),
            "Friday": int(last.friday or 0),
            "Saturday": int(last.saturday or 0),
            "Sunday": int(last.sunday or 0),
        },
    }


def get_cached_days(store_id: str, start_day: date, end_day: date):
    with SessionLocal() as s:
        rows = s.query(DashboardDaily.day).filter(
            DashboardDaily.store_id == str(store_id),
            DashboardDaily.day.between(start_day, end_day)
        ).all()
    return set(d for (d,) in rows)