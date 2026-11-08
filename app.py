import calendar
import hashlib
import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timezone
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "school.db"))
MAX_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
TEACHER_ID = os.getenv("TEACHER_MAX_ID", "").strip()
MAX_API = "https://platform-api2.max.ru"
MAX_CA_CERT_FILE = os.getenv("MAX_CA_CERT_FILE", "/certs/ministry-root.crt")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("studyquest")

def configure_max_ca():
    cert_path = Path(MAX_CA_CERT_FILE)
    if not cert_path.is_file():
        log.warning("MAX CA certificate not found at %s; MAX requests will use the default trust store", cert_path)
        return
    certificate = cert_path.read_bytes()
    if b"-----BEGIN CERTIFICATE-----" not in certificate:
        log.error("MAX CA certificate must be PEM encoded: %s", cert_path)
        return
    bundle = Path("/tmp/studyquest-ca-bundle.pem")
    default_bundle = Path(requests.certs.where()).read_bytes()
    bundle.write_bytes(default_bundle.rstrip() + b"\n" + certificate.strip() + b"\n")
    os.environ["REQUESTS_CA_BUNDLE"] = str(bundle)
    log.info("Added the configured MAX CA certificate to the HTTPS trust bundle")


configure_max_ca()


@contextmanager
def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS system_admins (
            user_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS admin_invites (
            code_hash TEXT PRIMARY KEY,
            target_user_id TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            used_at TEXT,
            max_uses INTEGER NOT NULL DEFAULT 6,
            used_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS admin_invite_uses (
            code_hash TEXT NOT NULL REFERENCES admin_invites(code_hash),
            user_id TEXT NOT NULL,
            used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(code_hash,user_id)
        );
        CREATE TABLE IF NOT EXISTS bot_states (
            user_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS schools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS school_teachers (
            school_id INTEGER NOT NULL REFERENCES schools(id),
            teacher_user_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(school_id,teacher_user_id)
        );
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL REFERENCES schools(id),
            name TEXT NOT NULL,
            join_code TEXT NOT NULL UNIQUE,
            homeroom_teacher_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(school_id,name)
        );
        CREATE TABLE IF NOT EXISTS subject_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL REFERENCES schools(id),
            name TEXT NOT NULL,
            teacher_user_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(school_id,name,teacher_user_id)
        );
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL REFERENCES classes(id),
            name TEXT NOT NULL,
            teacher_user_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            catalog_id INTEGER REFERENCES subject_catalog(id),
            UNIQUE(class_id,name)
        );
        CREATE TABLE IF NOT EXISTS students (
            user_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            class_id INTEGER REFERENCES classes(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            points INTEGER NOT NULL CHECK(points > 0),
            correct_answer TEXT NOT NULL DEFAULT '',
            subject_id INTEGER REFERENCES subjects(id),
            active INTEGER NOT NULL DEFAULT 1,
            due_at TEXT,
            class_number INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            user_id TEXT NOT NULL REFERENCES students(user_id),
            answer TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            awarded_points INTEGER
        );
        CREATE TABLE IF NOT EXISTS reward_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES students(user_id),
            reward TEXT NOT NULL,
            cost INTEGER NOT NULL CHECK(cost > 0),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            reviewed_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_reward_requests_status ON reward_requests(status);
        CREATE TABLE IF NOT EXISTS achievements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_user_id TEXT NOT NULL,
            class_id INTEGER NOT NULL REFERENCES classes(id),
            title TEXT NOT NULL,
            code_hash TEXT NOT NULL UNIQUE,
            code_value TEXT,
            reward_points INTEGER NOT NULL CHECK(reward_points >= 0),
            max_claims INTEGER NOT NULL CHECK(max_claims > 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS achievement_claims (
            achievement_id INTEGER NOT NULL REFERENCES achievements(id),
            user_id TEXT NOT NULL REFERENCES students(user_id),
            claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(achievement_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS point_achievement_awards (
            user_id TEXT NOT NULL REFERENCES students(user_id),
            achievement_key TEXT NOT NULL,
            awarded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,achievement_key)
        );
        CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
        """)
        if TEACHER_ID and TEACHER_ID.isdigit():
            conn.execute("INSERT OR IGNORE INTO system_admins(user_id) VALUES(?)", (TEACHER_ID,))
        achievement_columns={row["name"] for row in conn.execute("PRAGMA table_info(achievements)")}
        if "code_value" not in achievement_columns:
            conn.execute("ALTER TABLE achievements ADD COLUMN code_value TEXT")
        class_columns={row["name"] for row in conn.execute("PRAGMA table_info(classes)")}
        if "homeroom_teacher_id" not in class_columns:
            conn.execute("ALTER TABLE classes ADD COLUMN homeroom_teacher_id TEXT")
        invite_columns={row["name"] for row in conn.execute("PRAGMA table_info(admin_invites)")}
        if "max_uses" not in invite_columns:
            conn.execute("ALTER TABLE admin_invites ADD COLUMN max_uses INTEGER NOT NULL DEFAULT 1")
        if "used_count" not in invite_columns:
            conn.execute("ALTER TABLE admin_invites ADD COLUMN used_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE admin_invites SET used_count=1 WHERE used_at IS NOT NULL AND used_count=0")
        submission_columns = {row["name"] for row in conn.execute("PRAGMA table_info(submissions)")}
        if "awarded_points" not in submission_columns:
            conn.execute("ALTER TABLE submissions ADD COLUMN awarded_points INTEGER")
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "correct_answer" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN correct_answer TEXT NOT NULL DEFAULT ''")
        if "subject_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN subject_id INTEGER REFERENCES subjects(id)")
        if "due_at" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")
        if "class_number" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN class_number INTEGER")
        task_classes=conn.execute("SELECT t.id,s.class_id FROM tasks t JOIN subjects s ON s.id=t.subject_id WHERE t.class_number IS NULL ORDER BY s.class_id,t.id").fetchall()
        next_numbers={}
        for task_row in task_classes:
            class_id=task_row["class_id"]
            next_numbers[class_id]=next_numbers.get(class_id,0)+1
            conn.execute("UPDATE tasks SET class_number=? WHERE id=?",(next_numbers[class_id],task_row["id"]))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_class_number ON tasks(subject_id,class_number)")
        subject_columns = {row["name"] for row in conn.execute("PRAGMA table_info(subjects)")}
        if "catalog_id" not in subject_columns:
            conn.execute("ALTER TABLE subjects ADD COLUMN catalog_id INTEGER REFERENCES subject_catalog(id)")
        old_subjects = conn.execute("SELECT sub.id,sub.name,sub.teacher_user_id,c.school_id FROM subjects sub JOIN classes c ON c.id=sub.class_id WHERE sub.catalog_id IS NULL AND sub.teacher_user_id!='0'").fetchall()
        for old in old_subjects:
            conn.execute("INSERT OR IGNORE INTO subject_catalog(school_id,name,teacher_user_id) VALUES(?,?,?)", (old["school_id"],old["name"],old["teacher_user_id"]))
            catalog = conn.execute("SELECT id FROM subject_catalog WHERE school_id=? AND name=? AND teacher_user_id=?", (old["school_id"],old["name"],old["teacher_user_id"])).fetchone()
            conn.execute("UPDATE subjects SET catalog_id=? WHERE id=?", (catalog["id"],old["id"]))
        student_columns = {row["name"] for row in conn.execute("PRAGMA table_info(students)")}
        if "class_id" not in student_columns:
            conn.execute("ALTER TABLE students ADD COLUMN class_id INTEGER REFERENCES classes(id)")
        school = conn.execute("SELECT id FROM schools ORDER BY id LIMIT 1").fetchone()
        if not school:
            school_id = conn.execute("INSERT INTO schools(name) VALUES(?)", ("Моя школа",)).lastrowid
        else:
            school_id = school["id"]
        legacy_class = conn.execute("SELECT id FROM classes WHERE school_id=? AND name=?", (school_id, "Общий класс")).fetchone()
        if not legacy_class:
            legacy_class_id = conn.execute("INSERT INTO classes(school_id,name,join_code) VALUES(?,?,?)", (school_id, "Общий класс", create_join_code())).lastrowid
        else:
            legacy_class_id = legacy_class["id"]
        legacy_subject = conn.execute("SELECT id FROM subjects WHERE class_id=? AND name=?", (legacy_class_id, "Общий предмет")).fetchone()
        if not legacy_subject:
            legacy_subject_id = conn.execute("INSERT INTO subjects(class_id,name,teacher_user_id) VALUES(?,?,?)", (legacy_class_id, "Общий предмет", "0")).lastrowid
        else:
            legacy_subject_id = legacy_subject["id"]
        conn.execute("UPDATE tasks SET subject_id=? WHERE subject_id IS NULL", (legacy_subject_id,))
        conn.execute("UPDATE students SET class_id=? WHERE class_id IS NULL", (legacy_class_id,))
        conn.execute("INSERT OR IGNORE INTO school_teachers(school_id,teacher_user_id) SELECT DISTINCT c.school_id,sub.teacher_user_id FROM subjects sub JOIN classes c ON c.id=sub.class_id WHERE sub.teacher_user_id!='0' AND sub.teacher_user_id!=?", (TEACHER_ID or "",))
        conn.execute("UPDATE subjects SET teacher_user_id='0' WHERE teacher_user_id=? AND name='Общий предмет'", (TEACHER_ID or "",))
        conn.execute("DELETE FROM school_teachers WHERE teacher_user_id=? AND school_id=?", (TEACHER_ID or "", school_id))












def set_bot_state(user_id, state, data=None):
    with db() as conn:
        conn.execute("INSERT INTO bot_states(user_id,state,data,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,data=excluded.data,updated_at=CURRENT_TIMESTAMP", (str(user_id), state, json.dumps(data or {}, ensure_ascii=False)))


def get_bot_state(user_id):
    with db() as conn:
        row = conn.execute("SELECT state,data FROM bot_states WHERE user_id=?", (str(user_id),)).fetchone()
    return (row['state'], json.loads(row['data'])) if row else (None, {})


def clear_bot_state(user_id):
    with db() as conn:
        conn.execute("DELETE FROM bot_states WHERE user_id=?", (str(user_id),))




def keyboard(*rows):
    return [[{"type": "callback", "text": label, "payload": payload} for label, payload in row] for row in rows]


def max_send(user_id, text, buttons=None):
    if not MAX_TOKEN or not user_id:
        log.info("MAX is not configured; message to %s: %s", user_id, text)
        return False
    rows=list(buttons or [])
    has_menu=any(button.get("payload")=="menu" for row in rows for button in row)
    has_cancel=any(button.get("payload")=="wizard:cancel" for row in rows for button in row)
    if not has_menu and not has_cancel:
        rows.append([{"type":"callback","text":"🏠 Меню","payload":"menu"}])
    buttons=rows
    try:
        response = requests.post(
            f"{MAX_API}/messages",
            params={"user_id": str(user_id)},
            headers={"Authorization": MAX_TOKEN},
            json={"text": text, **({"attachments": [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]} if buttons else {})},
            timeout=20,
        )
        response.raise_for_status()
        response.json()
        return True
    except requests.RequestException:
        log.exception("Failed to send MAX message")
        return False




def get_student(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM students WHERE user_id=?", (str(user_id),)).fetchone()


def register_student(user_id, name):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO students(user_id,name) VALUES(?,?)",
            (str(user_id), name or f"Ученик {user_id}"),
        )


def save_student_full_name(user_id, full_name):
    with db() as conn:
        conn.execute(
            "INSERT INTO students(user_id,name) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
            (str(user_id), full_name),
        )


def level_for_points(points):
    points = max(0, int(points or 0))
    level = 1
    threshold = 100
    while points >= threshold:
        level += 1
        threshold += level * 100
    previous_threshold = threshold - level * 100 if level > 1 else 0
    next_threshold = threshold
    progress = points - previous_threshold
    span = next_threshold - previous_threshold
    return {"level": level, "xp": points, "progress_xp": progress, "level_xp": span, "next_level_xp": next_threshold}


TASK_ACHIEVEMENT_THRESHOLDS = (10, 20, 40, 80, 100)
POINT_TASK_ACHIEVEMENT_THRESHOLDS = (1, 3, 10)
LEVEL_ACHIEVEMENT_STEP = 5
META_ACHIEVEMENT_THRESHOLDS = (1, 3, 5, 10, 15, 20)
POINT_COLLECTION_ACHIEVEMENT_THRESHOLDS = (1, 3, 10)


def progress_meter(current, total, width=10):
    return ""


def achievement_progress_text(points, summary):
    task_goal=summary["next_task_achievement"]
    task_line=(f"Выполненные задания: {summary['completed_tasks']}/{task_goal}"
               if task_goal is not None else "Достижение за выполнение заданий получено.")
    level=level_for_points(points)
    level_line=f"Уровень {level['level']}: {level['progress_xp']}/{level['level_xp']} XP"
    collection_goal=summary["next_collection_achievement"]
    collection_line=(f"Коллекция достижений: {summary['base_achievement_count']}/{collection_goal}"
                     if collection_goal is not None else "Все цели коллекции достигнуты.")
    return "\n\n".join((task_line,level_line,collection_line))


def achievement_summary(points, accepted_count, custom_titles=()):
    level = level_for_points(points)["level"]
    task_awards = [f"Выполнено заданий: {n}" for n in TASK_ACHIEVEMENT_THRESHOLDS if accepted_count >= n]
    level_awards = [f"Уровень {n}" for n in range(LEVEL_ACHIEVEMENT_STEP, level + 1, LEVEL_ACHIEVEMENT_STEP)]
    custom_awards = list(dict.fromkeys(custom_titles))
    base_awards = task_awards + level_awards + custom_awards
    meta_awards = [f"Коллекционер: {n} достижений" for n in META_ACHIEVEMENT_THRESHOLDS if len(base_awards) >= n]
    return {
        "completed_tasks": accepted_count,
        "achievements": base_awards + meta_awards,
        "base_achievement_count": len(base_awards),
        "next_task_achievement": next((n for n in TASK_ACHIEVEMENT_THRESHOLDS if accepted_count < n), None),
        "next_level_achievement": ((level // LEVEL_ACHIEVEMENT_STEP) + 1) * LEVEL_ACHIEVEMENT_STEP,
        "next_collection_achievement": next((n for n in META_ACHIEVEMENT_THRESHOLDS if len(base_awards) < n), None),
    }


def show_achievement_tab(user_id,completed,page=0,selected=None):
    student=get_student(user_id)
    if not student:
        max_send(user_id,"\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u0443\u0439\u0442\u0435\u0441\u044c \u0432 \u0431\u043e\u0442\u0435.")
        return
    class_id=student["class_id"]; page=max(0,int(page))
    with db() as conn:
        task_count=conn.execute("SELECT COUNT(*) FROM submissions WHERE user_id=? AND status='approved'",(str(user_id),)).fetchone()[0]
        claimed=conn.execute("SELECT a.title,a.reward_points FROM achievement_claims ac JOIN achievements a ON a.id=ac.achievement_id WHERE ac.user_id=? ORDER BY ac.claimed_at",(str(user_id),)).fetchall()
        claimed_titles={r["title"] for r in claimed}
        available=conn.execute("SELECT title,reward_points FROM achievements WHERE class_id=? ORDER BY id",(class_id,)).fetchall() if class_id else []
    level=level_for_points(student["points"])["level"]
    done=[(f"\u0412\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043e \u0437\u0430\u0434\u0430\u043d\u0438\u0439 {n}",0) for n in TASK_ACHIEVEMENT_THRESHOLDS if task_count>=n]
    done.extend((f"\u0423\u0440\u043e\u0432\u0435\u043d\u044c {n}",0) for n in range(LEVEL_ACHIEVEMENT_STEP,level+1,LEVEL_ACHIEVEMENT_STEP))
    done.extend((r["title"],r["reward_points"]) for r in claimed)
    if completed:
        items=done;title="\u2705 \u0412\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043d\u044b\u0435 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f";empty="\u0412\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043d\u044b\u0445 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0439 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442."
    else:
        items=[(f"\u0412\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u044c \u0437\u0430\u0434\u0430\u043d\u0438\u0439 {n}",0) for n in TASK_ACHIEVEMENT_THRESHOLDS if task_count<n]
        next_level=((level//LEVEL_ACHIEVEMENT_STEP)+1)*LEVEL_ACHIEVEMENT_STEP
        items.extend((f"\u0414\u043e\u0441\u0442\u0438\u0447\u044c \u0443\u0440\u043e\u0432\u043d\u044f {n}",0) for n in range(next_level,next_level+LEVEL_ACHIEVEMENT_STEP*3,LEVEL_ACHIEVEMENT_STEP))
        items.extend((f"\u0421\u043e\u0431\u0440\u0430\u0442\u044c \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0439 {n}",0) for n in META_ACHIEVEMENT_THRESHOLDS if len(done)<n)
        items.extend((r["title"],r["reward_points"]) for r in available if r["title"] not in claimed_titles)
        title="\U0001f3af \u041d\u0435\u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043d\u044b\u0435 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f";empty="\u041d\u0435\u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043d\u044b\u0445 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0439 \u043d\u0435\u0442."
    count=max(1,(len(items)+4)//5);page=min(page,count-1);start=page*5;current=items[start:start+5]
    if selected is not None:
        selected=int(selected)
        if not 0<=selected<len(current):
            max_send(user_id,"Достижение на этой странице не найдено.")
            return
        name,reward=current[selected]
        status="Выполнено" if completed else "Ещё не выполнено"
        max_send(user_id,f"{name}\nНаграда: {reward} баллов\nСтатус: {status}",keyboard([("⬅ К списку",f"student:achievements:page:{'done' if completed else 'todo'}:{page}")],[ ("❌ Отмена","wizard:cancel")]))
        return
    buttons=[]
    for index,(name,reward) in enumerate(current,start):
        buttons.append([{"type":"callback","text":f"{name} - {reward} \u0431\u0430\u043b\u043b\u043e\u0432"[:48],"payload":f"student:achievement:info:{index}:{'done' if completed else 'todo'}:{page}"}])
    nav=[]
    if page>0: nav.append({"type":"callback","text":"<","payload":f"student:achievements:page:{'done' if completed else 'todo'}:{page-1}"})
    if page+1<count: nav.append({"type":"callback","text":">","payload":f"student:achievements:page:{'done' if completed else 'todo'}:{page+1}"})
    if nav: buttons.append(nav)
    buttons.append([{"type":"callback","text":"\u2705 \u0412\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043d\u044b\u0435","payload":"student:achievements:completed"},{"type":"callback","text":"\U0001f3af \u041d\u0435\u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043d\u044b\u0435","payload":"student:achievements:incomplete"}])
    buttons.append([{"type":"callback","text":"Отмена","payload":"wizard:cancel"}])
    max_send(user_id,f"{title} ({page+1}/{count}):",buttons) if current else max_send(user_id,empty,keyboard([("\u2705 \u0412\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043d\u044b\u0435","student:achievements:completed"),("\U0001f3af \u041d\u0435\u0432\u044b\u043f\u043e\u043b\u043d\u0435\u043d\u043d\u044b\u0435","student:achievements:incomplete")]))

def show_teacher_achievements(user_id,page=0):
    if not is_system_admin(user_id) and not is_subject_teacher(user_id):
        max_send(user_id,"\u0420\u0430\u0437\u0434\u0435\u043b \u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d \u0442\u043e\u043b\u044c\u043a\u043e \u0443\u0447\u0438\u0442\u0435\u043b\u044e.")
        return
    page=max(0,int(page)); offset=page*5
    with db() as conn:
        if is_system_admin(user_id):
            rows=conn.execute("SELECT a.id,a.title,a.reward_points,a.max_claims,c.name AS cls,s.name AS school FROM achievements a JOIN classes c ON c.id=a.class_id JOIN schools s ON s.id=c.school_id ORDER BY a.id DESC LIMIT 5 OFFSET ?",(offset,)).fetchall()
            total=conn.execute("SELECT COUNT(*) FROM achievements").fetchone()[0]
        else:
            rows=conn.execute("SELECT a.id,a.title,a.reward_points,a.max_claims,c.name AS cls,s.name AS school FROM achievements a JOIN classes c ON c.id=a.class_id JOIN schools s ON s.id=c.school_id WHERE a.teacher_user_id=? ORDER BY a.id DESC LIMIT 5 OFFSET ?",(str(user_id),offset)).fetchall()
            total=conn.execute("SELECT COUNT(*) FROM achievements WHERE teacher_user_id=?",(str(user_id),)).fetchone()[0]
    buttons=[[{"type":"callback","text":f"{r['title']} - {r['reward_points']} \u0431\u0430\u043b\u043b\u043e\u0432"[:28],"payload":f"achievement:manage:{r['id']}:{page}"}] for r in rows]
    nav=[]
    if page>0: nav.append({"type":"callback","text":"\u25c0 \u041d\u0430\u0437\u0430\u0434","payload":f"teacher:achievements:page:{page-1}"})
    if offset+5<total: nav.append({"type":"callback","text":"\u0414\u0430\u043b\u0435\u0435 \u25b6","payload":f"teacher:achievements:page:{page+1}"})
    if nav: buttons.append(nav)
    buttons.append([{"type":"callback","text":"\u2795 \u0421\u043e\u0437\u0434\u0430\u0442\u044c \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0435","payload":"teacher:create_achievement"}])
    msg=f"\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f - \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0430 {page+1}. \u041d\u0430\u0436\u043c\u0438\u0442\u0435 \u043d\u0430 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0435 \u0434\u043b\u044f \u0440\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u044f:"
    max_send(user_id,msg,buttons) if rows else max_send(user_id,"\u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0439 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442.",buttons)


def owns_achievement(user_id,achievement_id):
    if is_system_admin(user_id): return True
    with db() as conn: return conn.execute("SELECT 1 FROM achievements WHERE id=? AND teacher_user_id=?",(achievement_id,str(user_id))).fetchone() is not None




def achievement_status(user_id, notify=False):
    student=get_student(user_id)
    if not student:
        return None
    with db() as conn:
        accepted=conn.execute("SELECT COUNT(*) FROM submissions WHERE user_id=? AND status='approved'",(str(user_id),)).fetchone()[0]
        titles=[row["title"] for row in conn.execute("SELECT a.title FROM achievement_claims ac JOIN achievements a ON a.id=ac.achievement_id WHERE ac.user_id=? ORDER BY ac.claimed_at",(str(user_id),))]
    summary=achievement_summary(student["points"],accepted,titles)
    if notify:
        achievements="\n".join("• "+title for title in summary["achievements"]) or "Достижений пока нет."
        next_task=str(summary["next_task_achievement"]) if summary["next_task_achievement"] is not None else "все задания"
        next_collection=str(summary["next_collection_achievement"]) if summary["next_collection_achievement"] is not None else "полная"
        message=("🏆 Ваши достижения:\n"+achievements+
                 f"\nПрогресс: {summary['completed_tasks']} выполненных заданий; "
                 f"следующая цель — {next_task} заданий, уровень {summary['next_level_achievement']}, "
                 f"коллекция {next_collection}.")
        max_send(user_id,message)
    return summary


def award_threshold_points(user_id, completed_tasks=None, collected_achievements=None, conn=None):
    """Grant each task and collection threshold exactly once."""
    user_id=str(user_id); added=0
    def apply(connection):
        nonlocal added
        if not connection.execute("SELECT 1 FROM students WHERE user_id=?",(user_id,)).fetchone():
            return
        task_count=completed_tasks
        collection_count=collected_achievements
        if task_count is None:
            task_count=connection.execute("SELECT COUNT(*) FROM submissions WHERE user_id=? AND status='approved'",(user_id,)).fetchone()[0]
        if collection_count is None:
            collection_count=connection.execute("SELECT COUNT(*) FROM achievement_claims WHERE user_id=?",(user_id,)).fetchone()[0]
        milestones=[(f"tasks:{n}",n) for n in POINT_TASK_ACHIEVEMENT_THRESHOLDS if task_count>=n]
        milestones.extend((f"collection:{n}",n) for n in POINT_COLLECTION_ACHIEVEMENT_THRESHOLDS if collection_count>=n)
        for key,points in milestones:
            cur=connection.execute("INSERT OR IGNORE INTO point_achievement_awards(user_id,achievement_key) VALUES(?,?)",(user_id,key))
            if cur.rowcount: added+=points
        if added: connection.execute("UPDATE students SET points=points+? WHERE user_id=?",(added,user_id))
    if conn is not None:
        apply(conn)
    else:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            apply(connection)
    return added


def create_achievement_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def claim_achievement(user_id, code):
    code_hash=hashlib.sha256(str(code).strip().casefold().encode("utf-8")).hexdigest()
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            student=conn.execute("SELECT user_id,class_id FROM students WHERE user_id=?",(str(user_id),)).fetchone()
            achievement=conn.execute("SELECT * FROM achievements WHERE code_hash=?",(code_hash,)).fetchone()
            if not student or not student["class_id"] or not achievement or student["class_id"]!=achievement["class_id"]:
                return False,"Код не подходит для вашего класса. Проверьте его у учителя."
            if conn.execute("SELECT 1 FROM achievement_claims WHERE achievement_id=? AND user_id=?",(achievement["id"],str(user_id))).fetchone():
                return False,"Вы уже подтверждали это достижение."
            count=conn.execute("SELECT COUNT(*) FROM achievement_claims WHERE achievement_id=?",(achievement["id"],)).fetchone()[0]
            if count>=achievement["max_claims"]:
                return False,"Лимит подтверждений для этого достижения уже исчерпан."
            conn.execute("INSERT INTO achievement_claims(achievement_id,user_id) VALUES(?,?)",(achievement["id"],str(user_id)))
            if achievement["reward_points"]:
                conn.execute("UPDATE students SET points=points+? WHERE user_id=?",(achievement["reward_points"],str(user_id)))
            student_points=conn.execute("SELECT points FROM students WHERE user_id=?",(str(user_id),)).fetchone()["points"]
            current_count=conn.execute("SELECT COUNT(*) FROM submissions WHERE user_id=? AND status='approved'",(str(user_id),)).fetchone()[0]
            claimed_count=conn.execute("SELECT COUNT(*) FROM achievement_claims WHERE user_id=?",(str(user_id),)).fetchone()[0]
            titles=[r["title"] for r in conn.execute("SELECT a.title FROM achievement_claims ac JOIN achievements a ON a.id=ac.achievement_id WHERE ac.user_id=?",(str(user_id),))]
        bonus=award_threshold_points(user_id,current_count,claimed_count)
        student_points=get_student(user_id)["points"]
        summary=achievement_summary(student_points,current_count,titles)
        return True,f"🏆 Достижение «{achievement['title']}» подтверждено! Начислено {achievement['reward_points']} баллов. Бонус за пороги: {bonus}. Всего баллов: {student_points}. Ваших достижений: {summary['base_achievement_count']}."
    except sqlite3.IntegrityError:
        return False,"Это достижение уже было подтверждено вами."


def show_deadline_calendar(user_id, year=None, month=None):
    state,data=get_bot_state(user_id)
    if state!="newtask_due_at":
        max_send(user_id,"\u041c\u0430\u0441\u0442\u0435\u0440 \u0441\u043e\u0437\u0434\u0430\u043d\u0438\u044f \u0437\u0430\u0434\u0430\u043d\u0438\u044f \u043d\u0435 \u0430\u043a\u0442\u0438\u0432\u0435\u043d.")
        return
    today=datetime.now().astimezone().date()
    year=int(year or data.get("calendar_year") or today.year)
    month=int(month or data.get("calendar_month") or today.month)
    if not 1<=month<=12: return
    data["calendar_year"],data["calendar_month"]=year,month
    set_bot_state(user_id,"newtask_due_at",data)
    names="\u044f\u043d\u0432\u0430\u0440\u044f \u0444\u0435\u0432\u0440\u0430\u043b\u044f \u043c\u0430\u0440\u0442\u0430 \u0430\u043f\u0440\u0435\u043b\u044f \u043c\u0430\u044f \u0438\u044e\u043d\u044f \u0438\u044e\u043b\u044f \u0430\u0432\u0433\u0443\u0441\u0442\u0430 \u0441\u0435\u043d\u0442\u044f\u0431\u0440\u044f \u043e\u043a\u0442\u044f\u0431\u0440\u044f \u043d\u043e\u044f\u0431\u0440\u044f \u0434\u0435\u043a\u0430\u0431\u0440\u044f".split()
    previous=(year-1,12) if month==1 else (year,month-1)
    following=(year+1,1) if month==12 else (year,month+1)
    buttons=[[{"type":"callback","text":"<","payload":f"task:calendar:month:{previous[0]}-{previous[1]}"},{"type":"callback","text":f"{names[month-1]} {year}","payload":"task:calendar:noop"},{"type":"callback","text":">","payload":f"task:calendar:month:{following[0]}-{following[1]}"}]]
    buttons.append([{"type":"callback","text":d,"payload":"task:calendar:noop"} for d in ("\u041f\u043d","\u0412\u0442","\u0421\u0440","\u0427\u0442","\u041f\u0442","\u0421\u0431","\u0412\u0441")])
    for week in calendar.monthcalendar(year,month):
        buttons.append([{"type":"callback","text":str(day) if day else ".","payload":f"task:calendar:day:{year}-{month}-{day}" if day else "task:calendar:noop"} for day in week])
    buttons.append([{"type":"callback","text":"\u0411\u0435\u0437 \u0441\u0440\u043e\u043a\u0430","payload":"task:deadline:none"},{"type":"callback","text":"\u041e\u0442\u043c\u0435\u043d\u0430","payload":"wizard:cancel"}])
    max_send(user_id,"\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0434\u0430\u0442\u0443 \u0437\u0430\u043a\u0440\u044b\u0442\u0438\u044f \u0437\u0430\u0434\u0430\u043d\u0438\u044f:",buttons)


def normalize_due_at(value):
    value=str(value or "").strip()
    if not value:
        return None
    try:
        parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    except ValueError as exc:
        raise ValueError("Укажите корректные дату и время закрытия.") from exc
    if parsed.tzinfo is None:
        parsed=parsed.astimezone()
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def task_is_closed(due_at):
    if not due_at:
        return False
    try:
        deadline=datetime.fromisoformat(str(due_at).replace("Z","+00:00"))
        if deadline.tzinfo is None:
            deadline=deadline.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc)>=deadline.astimezone(timezone.utc)
    except ValueError:
        return False


def create_task(title, description, points, correct_answer="", subject_id=None, due_at=None):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        subject=conn.execute("SELECT class_id FROM subjects WHERE id=?",(subject_id,)).fetchone()
        if not subject:
            raise ValueError("Предмет не найден.")
        next_number=conn.execute("SELECT COALESCE(MAX(t.class_number),0)+1 FROM tasks t JOIN subjects s ON s.id=t.subject_id WHERE s.class_id=?",(subject["class_id"],)).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO tasks(title,description,points,correct_answer,subject_id,due_at,class_number) VALUES(?,?,?,?,?,?,?)",
            (title, description, points, correct_answer.strip(), subject_id, normalize_due_at(due_at),next_number),
        )
        return next_number


def is_system_admin(user_id):
    if user_id is None:
        return False
    user_id=str(user_id)
    with db() as conn:
        return conn.execute("SELECT 1 FROM system_admins WHERE user_id=?",(user_id,)).fetchone() is not None


def add_system_admin(user_id):
    user_id=str(user_id).strip()
    if not user_id.isdigit():
        return False
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO system_admins(user_id) VALUES(?)",(user_id,))
    return True


def admin_student_mode(user_id):
    with db() as conn:
        row=conn.execute("SELECT value FROM settings WHERE key=?",(f"admin_student_mode:{user_id}",)).fetchone()
    return bool(row and row["value"]=="1")


def set_admin_student_mode(user_id, enabled):
    if enabled:
        value="1"
    else:
        value="0"
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(f"admin_student_mode:{user_id}",value))


def issue_admin_invite(target_user_id, created_by):
    target_user_id=str(target_user_id or "").strip()
    if target_user_id and not target_user_id.isdigit():
        raise ValueError("MAX ID должен состоять из цифр.")
    code=create_achievement_code()+create_achievement_code()[:4]
    code_hash=hashlib.sha256(code.casefold().encode("utf-8")).hexdigest()
    with db() as conn:
        conn.execute("INSERT INTO admin_invites(code_hash,target_user_id,created_by,max_uses,used_count) VALUES(?,?,?,6,0)",(code_hash,"",str(created_by)))
    return code


def redeem_admin_invite(user_id, code):
    code_hash=hashlib.sha256(str(code).strip().casefold().encode("utf-8")).hexdigest()
    user_id=str(user_id)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        invite=conn.execute("SELECT target_user_id,max_uses,used_count,used_at FROM admin_invites WHERE code_hash=?",(code_hash,)).fetchone()
        if not invite or invite["used_at"] is not None or invite["used_count"]>=invite["max_uses"]:
            return False,"Код назначения администратора недействителен или исчерпал лимит активаций."
        if conn.execute("SELECT 1 FROM system_admins WHERE user_id=?",(user_id,)).fetchone():
            return False,"Вы уже системный администратор; код повторно не использован."
        conn.execute("INSERT INTO system_admins(user_id) VALUES(?)",(user_id,))
        conn.execute("INSERT INTO admin_invite_uses(code_hash,user_id) VALUES(?,?)",(code_hash,user_id))
        new_count=invite["used_count"]+1
        conn.execute("UPDATE admin_invites SET used_count=?,used_at=CASE WHEN ?>=max_uses THEN CURRENT_TIMESTAMP ELSE NULL END WHERE code_hash=?",(new_count,new_count,code_hash))
    return True,f"Приглашение принято. Вы системный администратор. Код использован {new_count} из {invite['max_uses']} раз. Отправьте /start, чтобы открыть меню администратора."


def is_teacher_for_subject(user_id, subject_id):
    if is_system_admin(user_id):
        with db() as conn:
            return conn.execute("SELECT 1 FROM subjects WHERE id=?",(subject_id,)).fetchone() is not None
    with db() as conn:
        return conn.execute("SELECT 1 FROM subjects sub JOIN classes c ON c.id=sub.class_id JOIN school_teachers st ON st.school_id=c.school_id WHERE sub.id=? AND sub.teacher_user_id=? AND st.teacher_user_id=?", (subject_id,str(user_id),str(user_id))).fetchone() is not None


def is_subject_teacher(user_id):
    if is_system_admin(user_id):
        return True
    with db() as conn:
        return conn.execute("SELECT 1 FROM school_teachers WHERE teacher_user_id=? LIMIT 1", (str(user_id),)).fetchone() is not None


def school_for_teacher(user_id):
    with db() as conn:
        return conn.execute("SELECT school_id FROM school_teachers WHERE teacher_user_id=? ORDER BY school_id LIMIT 1", (str(user_id),)).fetchone()


def has_subject_assignment(user_id):
    with db() as conn:
        return conn.execute("SELECT 1 FROM subjects WHERE teacher_user_id=? LIMIT 1", (str(user_id),)).fetchone() is not None


def is_school_teacher(user_id, school_id):
    with db() as conn:
        return conn.execute("SELECT 1 FROM school_teachers WHERE school_id=? AND teacher_user_id=?", (school_id,str(user_id))).fetchone() is not None


def create_join_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def teacher_menu(user_id):
    is_admin = is_system_admin(user_id)
    admin_view = is_admin and not admin_student_mode(user_id)
    if admin_view:
        rows = [[("\U0001f3eb \u0423\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0448\u043a\u043e\u043b\u0430\u043c\u0438", "admin:schools"), ("\U0001f469\u200d\U0001f3eb \u041d\u0430\u0437\u043d\u0430\u0447\u0438\u0442\u044c \u0443\u0447\u0438\u0442\u0435\u043b\u044f", "admin:assign_teacher")], [("\U0001f4d8 \u0421\u043e\u0437\u0434\u0430\u0442\u044c \u043f\u0440\u0435\u0434\u043c\u0435\u0442", "admin:create_subject"), ("\u2795 \u041a\u043b\u0430\u0441\u0441 \u043a \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u0443", "admin:choose_class")], [("\u2795 \u0421\u043e\u0437\u0434\u0430\u0442\u044c \u0437\u0430\u0434\u0430\u043d\u0438\u0435", "teacher:create"), ("\U0001f4e5 \u0412\u0441\u0435 \u0440\u0430\u0431\u043e\u0442\u044b", "teacher:pending")], [("\U0001f381 \u0417\u0430\u043f\u0440\u043e\u0441\u044b \u043d\u0430\u0433\u0440\u0430\u0434", "teacher:rewards"), ("\U0001f4da \u0412\u0441\u0435 \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u044b", "teacher:subjects")], [("\U0001f3c6 \u0421\u043e\u0437\u0434\u0430\u0442\u044c \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0435", "admin:create_achievement"), ("\U0001f4cb \u041d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f", "admin:subjects")], [("\U0001f510 \u0412\u044b\u0434\u0430\u0442\u044c \u043a\u043e\u0434 \u0430\u0434\u043c\u0438\u043d\u0430", "admin:add_admin"), ("\U0001f468\u200d\U0001f393 \u0420\u0435\u0436\u0438\u043c \u0443\u0447\u0435\u043d\u0438\u043a\u0430", "admin:student_mode")], [("\U0001f4da \u0417\u0430\u0434\u0430\u043d\u0438\u044f \u0443\u0447\u0435\u043d\u0438\u043a\u0430", "student:tasks"), ("\u2b50 \u041c\u043e\u0438 \u0431\u0430\u043b\u043b\u044b", "student:points")], [("\U0001f3c6 \u041c\u043e\u0438 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f", "student:achievements"), ("\U0001f947 \u0420\u0435\u0439\u0442\u0438\u043d\u0433", "student:top")], [("\U0001f3eb \u0412\u0441\u0442\u0443\u043f\u0438\u0442\u044c \u0432 \u043a\u043b\u0430\u0441\u0441", "student:join"), ("\u2754 \u041f\u043e\u043c\u043e\u0449\u044c", "help")]]
    else:
        rows = [[("\u2795 \u0421\u043e\u0437\u0434\u0430\u0442\u044c \u0437\u0430\u0434\u0430\u043d\u0438\u0435", "teacher:create"), ("\U0001f4e5 \u041c\u043e\u0438 \u0440\u0430\u0431\u043e\u0442\u044b", "teacher:pending")], [("\U0001f4da \u041c\u043e\u0438 \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u044b", "teacher:subjects"), ("\U0001f381 \u0417\u0430\u043f\u0440\u043e\u0441\u044b \u043d\u0430\u0433\u0440\u0430\u0434", "teacher:rewards")], [("\U0001f3c6 \u041c\u043e\u0438 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f", "teacher:achievements"), ("\u2795 \u0421\u043e\u0437\u0434\u0430\u0442\u044c \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u0435", "admin:create_achievement")], [("\u2754 \u041f\u043e\u043c\u043e\u0449\u044c", "help"), ("\u21bb \u041c\u0435\u043d\u044e", "menu")]]
        if is_admin:
            rows.append([("\U0001f6e1\ufe0f \u0412\u0435\u0440\u043d\u0443\u0442\u044c\u0441\u044f \u0432 \u0440\u0435\u0436\u0438\u043c \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0430", "admin:admin_mode"), ("\U0001f3e0 \u041c\u0435\u043d\u044e \u0443\u0447\u0435\u043d\u0438\u043a\u0430", "admin:student_home")])
    rows.insert(0, [("\u2796 \u0421\u043f\u0438\u0441\u0430\u0442\u044c \u0431\u0430\u043b\u043b\u044b", "teacher:deduct_points")])
    max_send(user_id, "\u0420\u0435\u0436\u0438\u043c \u0441\u0438\u0441\u0442\u0435\u043c\u043d\u043e\u0433\u043e \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0430" if admin_view else ("\u0420\u0435\u0436\u0438\u043c \u0443\u0447\u0438\u0442\u0435\u043b\u044f" if is_admin else "\u041a\u0430\u0431\u0438\u043d\u0435\u0442 \u0443\u0447\u0438\u0442\u0435\u043b\u044f"), keyboard(*rows))


def student_menu(user_id, name=""):
    rows=[[("\U0001f4da \u0417\u0430\u0434\u0430\u043d\u0438\u044f", "student:tasks"), ("\u2b50 \u041c\u043e\u0438 \u0431\u0430\u043b\u043b\u044b", "student:points")], [("\U0001f3c6 \u0414\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f", "student:achievements"), ("\U0001f381 \u0417\u0430\u043f\u0440\u043e\u0441\u0438\u0442\u044c \u043d\u0430\u0433\u0440\u0430\u0434\u0443", "student:reward")], [("\U0001f39f \u041a\u043e\u0434 \u0434\u043e\u0441\u0442\u0438\u0436\u0435\u043d\u0438\u044f", "student:claim_achievement"), ("\U0001f947 \u0420\u0435\u0439\u0442\u0438\u043d\u0433", "student:top")], [("\U0001f3eb \u0412\u0441\u0442\u0443\u043f\u0438\u0442\u044c \u0432 \u043a\u043b\u0430\u0441\u0441", "student:join"), ("\U0001f6aa \u0412\u044b\u0439\u0442\u0438 \u0438\u0437 \u043a\u043b\u0430\u0441\u0441\u0430", "student:leave_class")], [("\u2754 \u041f\u043e\u043c\u043e\u0449\u044c", "help")]]
    if is_system_admin(user_id):
        rows.append([("\U0001f6e1\ufe0f \u041c\u0435\u043d\u044e \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0430", "admin:admin_mode")])
    max_send(user_id, f"\u041f\u0440\u0438\u0432\u0435\u0442, {name or '\u0443\u0447\u0435\u043d\u0438\u043a'}! \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435:", keyboard(*rows))


def bot_help(user_id, is_admin=False, is_teacher=False):
    if is_admin:
        text="Администратору доступны функции учителя, администратора и ученика. Ученики могут запрашивать награду за баллы; одобрение запроса списывает указанное число баллов."
        max_send(user_id,text,keyboard([("🏫 Школы","admin:schools"),("👩‍🏫 Назначить учителя","admin:assign_teacher")],[("📘 Создать предмет","admin:create_subject"),("➕ Класс к предмету","admin:choose_class")],[("➕ Создать задание","teacher:create"),("📥 Все работы","teacher:pending")],[("📚 Все предметы","teacher:subjects"),("🏆 Достижения","admin:create_achievement")],[("📚 Задания ученика","student:tasks"),("⭐ Мои баллы","student:points")],[("↩ Меню","menu")]))
    elif is_teacher:
        max_send(user_id,"Помощь учителя. Кнопками можно создать задание, открыть работы на проверку, управлять достижениями класса и посмотреть свои предметы. При проверке ответа можно указать начисляемые баллы.",keyboard([("➕ Создать задание","teacher:create"),("📥 Мои работы","teacher:pending")],[("🏆 Достижение классу","admin:create_achievement"),("📚 Мои предметы","teacher:subjects")],[("❔ Помощь","help"),("↩ Меню","menu")]))
    else:
        max_send(user_id,"Помощь ученика. Кнопками можно открыть задания, баллы, достижения, рейтинг и присоединение к классу. Русские команды: /задания, /баллы, /достижения, /коддостижения КОД, /рейтинг, /вступить КОД.",keyboard([("📚 Задания","student:tasks"),("⭐ Баллы","student:points")],[("🏆 Достижения","student:achievements"),("🎟 Ввести код","student:claim_achievement")],[("🥇 Рейтинг","student:top"),("🏫 Вступить","student:join")],[("❔ Помощь","help")]))


def continue_task_wizard(user_id, text):
    state, data = get_bot_state(user_id)
    if not state:
        return False
    text = text.strip()
    if text.lower() in ("отмена", "/cancel", "отменить"):
        clear_bot_state(user_id)
        teacher_menu(user_id) if (is_system_admin(user_id) or is_subject_teacher(user_id)) else student_menu(user_id)
        return True
    if state == "deduct_student_points":
        if not text.isdigit() or int(text)<=0:
            max_send(user_id,"\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043f\u043e\u043b\u043e\u0436\u0438\u0442\u0435\u043b\u044c\u043d\u043e\u0435 \u0447\u0438\u0441\u043b\u043e \u0431\u0430\u043b\u043b\u043e\u0432 \u0438\u043b\u0438 \u00ab\u041e\u0442\u043c\u0435\u043d\u0430\u00bb.")
            return True
        amount=int(text); student_id=str(data.get("student_id",""))
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if is_system_admin(user_id):
                student=conn.execute("SELECT st.name,st.points FROM students st JOIN classes c ON c.id=st.class_id WHERE st.user_id=?",(student_id,)).fetchone()
            else:
                student=conn.execute("SELECT st.name,st.points FROM students st JOIN classes c ON c.id=st.class_id JOIN school_teachers assigned ON assigned.school_id=c.school_id WHERE st.user_id=? AND assigned.teacher_user_id=?",(student_id,str(user_id))).fetchone()
            if not student:
                result="\u0423\u0447\u0435\u043d\u0438\u043a \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d \u0438\u043b\u0438 \u043d\u0435 \u043e\u0442\u043d\u043e\u0441\u0438\u0442\u0441\u044f \u043a \u0432\u0430\u0448\u0438\u043c \u043a\u043b\u0430\u0441\u0441\u0430\u043c."
                balance=None
            else:
                updated=conn.execute("UPDATE students SET points=points-? WHERE user_id=? AND points>=?",(amount,student_id,amount))
                if not updated.rowcount:
                    result=f"\u041d\u0435\u043b\u044c\u0437\u044f \u0441\u043f\u0438\u0441\u0430\u0442\u044c {amount}: \u0443 \u0443\u0447\u0435\u043d\u0438\u043a\u0430 \u0441\u0435\u0439\u0447\u0430\u0441 {student['points']} \u0431\u0430\u043b\u043b\u043e\u0432."
                    balance=None
                else:
                    result=None
                    balance=student["points"]-amount
        if result:
            max_send(user_id,result)
            return True
        clear_bot_state(user_id)
        max_send(student_id,f"\u0423\u0447\u0438\u0442\u0435\u043b\u044c \u0441\u043f\u0438\u0441\u0430\u043b {amount} \u0431\u0430\u043b\u043b\u043e\u0432. \u041e\u0441\u0442\u0430\u0442\u043e\u043a: {balance}.")
        max_send(user_id,f"\u0423 {student['name']} \u0441\u043f\u0438\u0441\u0430\u043d\u043e {amount} \u0431\u0430\u043b\u043b\u043e\u0432. \u041e\u0441\u0442\u0430\u0442\u043e\u043a: {balance}.")
        teacher_menu(user_id)
        return True
    if state == "reward_request_text":
        reward=text.strip()
        if not reward or len(reward)>200:
            max_send(user_id,"Опишите желаемую награду текстом до 200 символов.",keyboard([("Отмена","wizard:cancel"),("🏠 Меню","menu")]))
            return True
        data["reward"]=reward
        set_bot_state(user_id,"reward_request_cost",data)
        student=get_student(user_id)
        max_send(user_id,f"Сколько баллов готовы обменять? Доступно: {student['points'] if student else 0}.",keyboard([("Отмена","wizard:cancel"),("🏠 Меню","menu")]))
        return True
    if state == "reward_request_cost":
        if not text.isdigit() or int(text)<=0:
            max_send(user_id,"Введите целое число баллов больше нуля.",keyboard([("Отмена","wizard:cancel"),("🏠 Меню","menu")]))
            return True
        cost=int(text)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            student=conn.execute("SELECT points,class_id FROM students WHERE user_id=?",(str(user_id),)).fetchone()
            if not student or not student["class_id"]:
                clear_bot_state(user_id)
                max_send(user_id,"Ученик или класс не найдены. Сначала вступите в класс.")
                return True
            pending=conn.execute("SELECT 1 FROM reward_requests WHERE user_id=? AND status='pending' LIMIT 1",(str(user_id),)).fetchone()
            if pending:
                max_send(user_id,"У вас уже есть запрос награды на проверке. Дождитесь решения учителя.",keyboard([("Отмена","wizard:cancel"),("🏠 Меню","menu")]))
                return True
            if cost>student["points"]:
                max_send(user_id,f"Недостаточно баллов. Сейчас доступно {student['points']}.",keyboard([("Отмена","wizard:cancel"),("🏠 Меню","menu")]))
                return True
            conn.execute("INSERT INTO reward_requests(user_id,reward,cost) VALUES(?,?,?)",(str(user_id),data["reward"],cost))
            request_id=conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        clear_bot_state(user_id)
        max_send(user_id,f"Запрос награды #{request_id} отправлен учителю: «{data['reward']}» за {cost} баллов. Списание будет только после одобрения.")
        with db() as conn:
            class_row=conn.execute("SELECT school_id,homeroom_teacher_id FROM classes WHERE id=?",(student["class_id"],)).fetchone()
            administrators=conn.execute("SELECT user_id FROM system_admins").fetchall()
            if class_row and class_row["homeroom_teacher_id"]:
                teacher_ids={str(class_row["homeroom_teacher_id"])}
            else:
                teacher_ids={r["teacher_user_id"] for r in conn.execute("SELECT teacher_user_id FROM school_teachers WHERE school_id=?",(class_row["school_id"],)).fetchall()} if class_row else set()
            teacher_ids.update(str(r["user_id"]) for r in administrators)
        for teacher_id in teacher_ids:
            max_send(teacher_id,f"🎁 Новый запрос награды #{request_id} от {user_id}: «{data['reward']}» за {cost} баллов. Откройте «Запросы наград» в меню.")
        student_menu(user_id)
        return True
    if state == "achievement_edit_title":
        title=text.strip()
        if not title or len(title)>100:
            max_send(user_id,"Название должно содержать от 1 до 100 символов.")
            return True
        with db() as conn: cur=conn.execute("UPDATE achievements SET title=? WHERE id=? AND (? OR teacher_user_id=?)",(title,int(data["achievement_id"]),int(is_system_admin(user_id)),str(user_id)))
        clear_bot_state(user_id)
        max_send(user_id,"Название обновлено." if cur.rowcount else "Нет доступа или достижение не найдено.")
        show_teacher_achievements(user_id,int(data.get("page",0)))
        return True
    if state == "achievement_edit_points":
        if not text.isdigit() or not 0<=int(text)<=1000:
            max_send(user_id,"Введите целое число баллов от 0 до 1000.")
            return True
        with db() as conn: cur=conn.execute("UPDATE achievements SET reward_points=? WHERE id=? AND (? OR teacher_user_id=?)",(int(text),int(data["achievement_id"]),int(is_system_admin(user_id)),str(user_id)))
        clear_bot_state(user_id)
        max_send(user_id,"Награда обновлена." if cur.rowcount else "Нет доступа или достижение не найдено.")
        show_teacher_achievements(user_id,int(data.get("page",0)))
        return True
    if state == "achievement_edit_limit":
        if not text.isdigit() or not 1<=int(text)<=10000:
            max_send(user_id,"Введите лимит активаций от 1 до 10000.",keyboard([("Отмена","wizard:cancel")]))
            return True
        with db() as conn:
            result=conn.execute("UPDATE achievements SET max_claims=? WHERE id=? AND (? OR teacher_user_id=?)",(int(text),int(data["achievement_id"]),int(is_system_admin(user_id)),str(user_id)))
        clear_bot_state(user_id)
        max_send(user_id,"Лимит активаций обновлён." if result.rowcount else "Нет доступа или достижение не найдено.")
        show_teacher_achievements(user_id,int(data.get("page",0)))
        return True
    if state == "claim_achievement_code":
        success,message=claim_achievement(user_id,text.strip())
        clear_bot_state(user_id)
        max_send(user_id,message)
        if success:
            student=get_student(user_id)
            if student and student["class_id"]:
                with db() as conn:
                    claimed_count=conn.execute("SELECT COUNT(*) FROM achievement_claims WHERE user_id=?",(str(user_id),)).fetchone()[0]
                    accepted_count=conn.execute("SELECT COUNT(*) FROM submissions WHERE user_id=? AND status='approved'",(str(user_id),)).fetchone()[0]
                total_base=claimed_count+len([n for n in TASK_ACHIEVEMENT_THRESHOLDS if accepted_count>=n])+len([n for n in range(5,level_for_points(student["points"])["level"]+1,5)])
                newly=[threshold for threshold in META_ACHIEVEMENT_THRESHOLDS if total_base==threshold]
                for threshold in newly:
                    max_send(user_id,f"🏆 Достижение коллекционера: собрано {threshold} базовых достижений!")
        return True
    if state == "achievement_title":
        title=text.strip()
        if not title or len(title)>100:
            max_send(user_id,"Введите название достижения длиной до 100 символов.",keyboard([("Отмена","wizard:cancel")]))
            return True
        data["title"]=title
        set_bot_state(user_id,"achievement_reward",data)
        max_send(user_id,"Введите целое число баллов за достижение (от 0 до 1000).",keyboard([("Отмена","wizard:cancel")]))
        return True
    if state == "achievement_reward":
        if not text.isdigit() or not 0<=int(text)<=1000:
            max_send(user_id,"Введите баллы числом от 0 до 1000.")
            return True
        data["reward_points"]=int(text)
        set_bot_state(user_id,"achievement_limit",data)
        max_send(user_id,"Сколько учеников могут подтвердить это достижение? Введите число от 1 до 10000.",keyboard([("Отмена","wizard:cancel")]))
        return True
    if state == "achievement_limit":
        if not text.isdigit() or not 1<=int(text)<=10000:
            max_send(user_id,"Введите лимит числом от 1 до 10000.")
            return True
        data["max_claims"]=int(text)
        code=create_achievement_code()
        code_hash=hashlib.sha256(code.casefold().encode("utf-8")).hexdigest()
        with db() as conn:
            class_row=conn.execute("SELECT id,school_id FROM classes WHERE id=?",(data.get("class_id"),)).fetchone()
            if not class_row or not conn.execute("SELECT 1 FROM school_teachers WHERE school_id=? AND teacher_user_id=?",(class_row["school_id"],str(user_id))).fetchone():
                clear_bot_state(user_id)
                max_send(user_id,"Класс или назначение учителя не найдены.")
                return True
            conn.execute("INSERT INTO achievements(teacher_user_id,class_id,title,code_hash,code_value,reward_points,max_claims) VALUES(?,?,?,?,?,?,?)",(str(user_id),class_row["id"],data["title"],code_hash,code,data["reward_points"],data["max_claims"]))
        clear_bot_state(user_id)
        max_send(user_id,f"Достижение «{data['title']}» создано для класса. Код: {code} . Лимит: {data['max_claims']} учеников; награда: {data['reward_points']} баллов. Передайте код ученикам.")
        teacher_menu(user_id)
        return True
    if state == "add_system_admin":
        if not is_system_admin(user_id):
            clear_bot_state(user_id)
            max_send(user_id,"⛔ Только текущий системный администратор может назначать администраторов.")
            return True
        new_admin_id="" if text.strip().casefold() in ("все","all","*") else text.strip()
        try:
            invite=issue_admin_invite(new_admin_id,user_id)
        except ValueError as exc:
            max_send(user_id,f"{exc} Введите числовой MAX ID или нажмите «Отмена».",keyboard([("Отмена","wizard:cancel")]))
            return True
        clear_bot_state(user_id)
        target_description=f"для MAX ID {new_admin_id}" if new_admin_id else "для шести доверенных пользователей"
        max_send(user_id,f"Код назначения администратора {target_description}: {invite}. Он допускает до 6 активаций разными MAX ID. Для активации отправьте этот код боту отдельным сообщением.")
        teacher_menu(user_id)
        return True
    if state == "review_points":
        try:
            awarded = int(text)
        except ValueError:
            max_send(user_id, "Введите целое число баллов от 0 до максимума задания.")
            return True
        max_points = int(data.get("max_points", -1))
        if not 0 <= awarded <= max_points:
            max_send(user_id, f"Введите число от 0 до {max_points}.")
            return True
        try:
            completed = approve_submission(int(data.get("submission_id", 0)), True, user_id, awarded)
        except ValueError as exc:
            max_send(user_id, str(exc))
            return True
        clear_bot_state(user_id)
        if not completed:
            max_send(user_id, "Работа уже проверена или недоступна вам.")
        else:
            teacher_menu(user_id)
        return True
    if state == "registration_name":
        name_parts = text.split()
        if len(name_parts) < 2 or any(len(part) < 2 for part in name_parts[:2]):
            max_send(user_id, "Введите имя и фамилию через пробел, например: Анна Иванова.")
            return True
        full_name = " ".join(name_parts[:2])
        save_student_full_name(user_id, full_name)
        clear_bot_state(user_id)
        max_send(user_id, f"Спасибо, {full_name}! Регистрация завершена.")
        if is_system_admin(user_id) or is_subject_teacher(user_id):
            teacher_menu(user_id)
            teacher_menu(user_id)
        else:
            student_menu(user_id, full_name)
        return True
    if state == "newtask_title":
        if not text or len(text) > 120:
            max_send(user_id, "Название обязательно и должно быть до 120 символов.",keyboard([("Отмена","wizard:cancel")]))
            return True
        data.update(title=text)
        set_bot_state(user_id, "newtask_description", data)
        max_send(user_id, "Напишите описание задания (до 1000 символов).",keyboard([("Отмена","wizard:cancel")]))
    elif state == "newtask_description":
        if not text or len(text) > 1000:
            max_send(user_id, "Описание обязательно и должно быть до 1000 символов. Попробуйте ещё раз.")
            return True
        data.update(description=text)
        set_bot_state(user_id, "newtask_points", data)
        max_send(user_id, "Сколько баллов начислять? Введите число от 1 до 1000.",keyboard([("Отмена","wizard:cancel")]))
    elif state == "newtask_points":
        if not text.isdigit() or not 1 <= int(text) <= 1000:
            max_send(user_id, "Введите целое число баллов от 1 до 1000.")
            return True
        data.update(points=int(text))
        set_bot_state(user_id, "newtask_answer", data)
        max_send(user_id, "Введите правильный ответ для автопроверки или напишите «ручная», если ответ проверит учитель.",keyboard([("Отмена","wizard:cancel")]))
    elif state == "newtask_answer":
        if len(text) > 500:
            max_send(user_id, "Ответ должен быть до 500 символов.")
            return True
        data["correct_answer"]="" if text.casefold() in ("ручная", "вручную", "нет") else text
        set_bot_state(user_id,"newtask_due_at",data)
        show_deadline_calendar(user_id)
    elif state == "newtask_due_at":
        if data.get("selected_date"):
            parts=text.split(":")
            if len(parts)!=2 or not all(part.isdigit() for part in parts) or not (0<=int(parts[0])<=23 and 0<=int(parts[1])<=59):
                max_send(user_id,"Введите время в формате ЧЧ:ММ, например 23:59.",keyboard([("Отмена","wizard:cancel")]))
                return True
            due_at=f"{data['selected_date']}T{int(parts[0]):02d}:{int(parts[1]):02d}:00"
        else:
            due_at=None if text.casefold() in ("без срока","нет","none") else text
        try:
            normalized_due=normalize_due_at(due_at)
        except ValueError as exc:
            max_send(user_id,f"{exc} Формат: ГГГГ-ММ-ДД ЧЧ:ММ.",keyboard([("Без срока","task:deadline:none"),("Отмена","wizard:cancel")]))
            return True
        if normalized_due and task_is_closed(normalized_due):
            max_send(user_id,"Срок должен быть в будущем. Введите новую дату и время или выберите «Без срока».",keyboard([("Без срока","task:deadline:none"),("Отмена","wizard:cancel")]))
            return True
        task_number=create_task(data["title"],data["description"],data["points"],data["correct_answer"],int(data["subject_id"]),normalized_due)
        clear_bot_state(user_id)
        deadline=f" Срок сдачи UTC: {normalized_due}." if normalized_due else " Без срока сдачи."
        max_send(user_id,f"✅ Задание №{task_number} создано для предмета «{data['subject_name']}»."+deadline+(" Ответы проверит учитель." if not data["correct_answer"] else " Ответы проверятся автоматически."))
        teacher_menu(user_id)
    elif state == "join_class":
        code = text.upper()
        with db() as conn:
            classroom = conn.execute("SELECT id,name FROM classes WHERE join_code=?", (code,)).fetchone()
            if not classroom:
                max_send(user_id, "Код класса не найден. Попробуйте ещё раз или напишите «Отмена».")
                return True
            conn.execute("UPDATE students SET class_id=? WHERE user_id=?", (classroom["id"], str(user_id)))
        clear_bot_state(user_id)
        max_send(user_id, f"Готово! Вы вступили в класс «{classroom['name']}».")
        student_menu(user_id)
    elif state == "newschool":
        if not is_system_admin(user_id):
            clear_bot_state(user_id)
            max_send(user_id,"Только системный администратор может создавать школы.")
            return True
        if not text or len(text) > 120:
            max_send(user_id, "Название школы обязательно (до 120 символов).")
            return True
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO schools(name) VALUES(?)", (text,))
        clear_bot_state(user_id)
        max_send(user_id, f"🏫 Школа «{text}» создана.")
        teacher_menu(user_id)
    elif state == "newsubject_name":
        if not is_system_admin(user_id):
            clear_bot_state(user_id)
            max_send(user_id,"Предметы создаёт системный администратор.")
            return True
        if not text or len(text)>80:
            max_send(user_id,"Название предмета обязательно (до 80 символов).")
            return True
        school_id=int(data.get("school_id",0))
        teacher_id=str(data.get("teacher_id",""))
        with db() as conn:
            school=conn.execute("SELECT name FROM schools WHERE id=?",(school_id,)).fetchone()
            assigned=conn.execute("SELECT 1 FROM school_teachers WHERE school_id=? AND teacher_user_id=?",(school_id,teacher_id)).fetchone()
            if not school or not assigned:
                clear_bot_state(user_id)
                max_send(user_id,"Школа или назначение учителя не найдены. Начните заново.")
                return True
            conn.execute("INSERT INTO subject_catalog(school_id,name,teacher_user_id) VALUES(?,?,?) ON CONFLICT(school_id,name,teacher_user_id) DO NOTHING",(school_id,text,teacher_id))
        clear_bot_state(user_id)
        max_send(user_id,f"Предмет «{text}» создан для школы «{school['name']}». Теперь добавьте классы через «Добавить класс к предмету».")
        teacher_menu(user_id)
    elif state == "newclass":
        if not is_system_admin(user_id):
            clear_bot_state(user_id)
            max_send(user_id,"Только системный администратор может создавать классы.")
            return True
        if not text or len(text)>80:
            max_send(user_id,"Название класса обязательно (до 80 символов).")
            return True
        school_id=int(data.get("school_id",0))
        with db() as conn:
            if not conn.execute("SELECT 1 FROM schools WHERE id=?",(school_id,)).fetchone():
                max_send(user_id,"Школа не найдена.")
                return True
            existing=conn.execute("SELECT join_code FROM classes WHERE school_id=? AND name=?",(school_id,text)).fetchone()
            if existing:
                code=existing["join_code"]
            else:
                code=create_join_code()
                conn.execute("INSERT INTO classes(school_id,name,join_code) VALUES(?,?,?)",(school_id,text,code))
        clear_bot_state(user_id)
        max_send(user_id,f"Класс «{text}» создан. Код для учеников: {code}")
        teacher_menu(user_id)
    elif state == "assign_teacher":
        if not is_system_admin(user_id):
            clear_bot_state(user_id)
            max_send(user_id,"Только системный администратор может назначать учителей.")
            return True
        teacher_id=text.strip()
        with db() as conn:
            school=conn.execute("SELECT name FROM schools WHERE id=?",(data.get('school_id'),)).fetchone()
            if not school or not teacher_id.isdigit():
                max_send(user_id,"Не удалось найти школу или MAX ID некорректен. Попробуйте снова.")
                return True
            conn.execute("INSERT OR IGNORE INTO school_teachers(school_id,teacher_user_id) VALUES(?,?)",(data['school_id'],teacher_id))
        clear_bot_state(user_id)
        max_send(user_id,f"Учитель MAX ID {teacher_id} добавлен в школу «{school['name']}». Системный администратор может назначить ему предмет.")
        teacher_menu(user_id)
    return True


def handle_callback(update):
    _handle_callback_inner(update)

def _handle_callback_inner(update):
    callback = update.get("callback") or {}
    user = callback.get("user") or {}
    user_id = user.get("user_id")
    payload = str(callback.get("payload", ""))
    callback_id = callback.get("callback_id")
    if not user_id:
        return
    is_lead = is_system_admin(user_id)
    is_teacher = is_subject_teacher(user_id)
    if payload == "admin:student_mode" and is_lead:
        set_admin_student_mode(user_id,True)
        student=get_student(user_id)
        student_menu(user_id,student["name"] if student else "ученик")
    elif payload == "admin:admin_mode" and is_lead:
        set_admin_student_mode(user_id,False)
        teacher_menu(user_id)
    elif payload == "admin:student_home" and is_lead:
        student=get_student(user_id)
        student_menu(user_id,student["name"] if student else "ученик")
    elif payload == "wizard:cancel":
        clear_bot_state(user_id)
        teacher_menu(user_id) if (is_lead or is_teacher) else student_menu(user_id)
    elif payload == "menu":
        clear_bot_state(user_id)
        if is_lead and admin_student_mode(user_id):
            student=get_student(user_id)
            student_menu(user_id,student["name"] if student else "ученик")
        elif is_lead or is_teacher:
            teacher_menu(user_id)
        else:
            student_menu(user_id)
    elif payload == "task:deadline:none":
        if get_bot_state(user_id)[0]=="newtask_due_at":
            continue_task_wizard(user_id,"без срока")
        else:
            max_send(user_id,"Мастер создания задания не активен.")
    elif payload == "student:tasks":
        process_bot_text(user_id, user.get("first_name", "Ученик"), "/задания")
    elif payload == "student:points":
        process_bot_text(user_id, user.get("first_name", "Ученик"), "/баллы")
    elif payload == "student:top":
        process_bot_text(user_id, user.get("first_name", "Ученик"), "/рейтинг")
    elif payload == "student:join":
        set_bot_state(user_id, "join_class")
        max_send(user_id, "Отправьте код класса, который выдал учитель.", keyboard([("Отмена", "menu")]))
    elif payload == "student:achievements":
        process_bot_text(user_id,user.get("first_name","Ученик"),"/достижения")
    elif payload == "student:claim_achievement":
        set_bot_state(user_id,"claim_achievement_code")
        max_send(user_id,"Введите код достижения, который выдал учитель. Напишите «Отмена», чтобы выйти.",keyboard([("Отмена","wizard:cancel"),("↩ Меню","menu")]))
    elif payload == "student:reward":
        student=get_student(user_id)
        if not student or not student["class_id"]:
            max_send(user_id,"Сначала вступите в класс, чтобы запросить награду у учителя.")
            return
        set_bot_state(user_id,"reward_request_text")
        max_send(user_id,f"Опишите желаемую награду. Ваш баланс: {student['points']} баллов.",keyboard([("Отмена","wizard:cancel"),("🏠 Меню","menu")]))
    elif payload == "teacher:rewards":
        if not is_teacher and not is_lead:
            max_send(user_id,"Раздел доступен только учителю.")
            return
        with db() as conn:
            if is_lead:
                requests_rows=conn.execute("SELECT rr.id,rr.user_id,rr.reward,rr.cost,st.name AS student_name,st.points AS balance,c.name AS class_name,s.name AS school_name FROM reward_requests rr JOIN students st ON st.user_id=rr.user_id JOIN classes c ON c.id=st.class_id JOIN schools s ON s.id=c.school_id WHERE rr.status='pending' ORDER BY rr.id").fetchall()
            else:
                requests_rows=conn.execute("SELECT rr.id,rr.user_id,rr.reward,rr.cost,st.name AS student_name,st.points AS balance,c.name AS class_name,s.name AS school_name FROM reward_requests rr JOIN students st ON st.user_id=rr.user_id JOIN classes c ON c.id=st.class_id JOIN schools s ON s.id=c.school_id JOIN school_teachers assigned ON assigned.school_id=c.school_id WHERE rr.status='pending' AND assigned.teacher_user_id=? ORDER BY rr.id",(str(user_id),)).fetchall()
        if not requests_rows:
            max_send(user_id,"Запросов наград на проверку нет.")
        else:
            for row in requests_rows[:20]:
                buttons=keyboard([("✅ Одобрить и списать",f"reward:{row['id']}:approve"),("❌ Отклонить",f"reward:{row['id']}:reject")],[ ("🔄 Обновить","teacher:rewards"),("🏠 Меню","menu")])
                max_send(user_id,f"🎁 Запрос #{row['id']} · {row['school_name']} / {row['class_name']}\nУченик: {row['student_name']} (ID {row['user_id']})\nНаграда: {row['reward']}\nЦена: {row['cost']} · баланс: {row['balance']}",buttons)
    elif payload == "teacher:create":
        if not is_teacher and not is_lead:
            max_send(user_id, "⛔ Создавать задания может только назначенный учитель или системный администратор.")
            return
        with db() as conn:
            if is_lead:
                rows=conn.execute("SELECT sub.id,sub.name,c.name AS class_name,s.name AS school_name FROM subjects sub JOIN classes c ON c.id=sub.class_id JOIN schools s ON s.id=c.school_id ORDER BY s.name,c.name,sub.name").fetchall()
            else:
                rows = conn.execute("SELECT sub.id,sub.name,c.name AS class_name,s.name AS school_name FROM subjects sub JOIN classes c ON c.id=sub.class_id JOIN schools s ON s.id=c.school_id WHERE sub.teacher_user_id=? AND EXISTS (SELECT 1 FROM school_teachers st WHERE st.school_id=c.school_id AND st.teacher_user_id=?) ORDER BY s.name,c.name,sub.name", (str(user_id),str(user_id))).fetchall()
        if not rows:
            max_send(user_id, "Предметы ещё не назначены системным администратором.")
            return
        buttons = [[{"type": "callback", "text": f"{r['class_name']} · {r['name']}"[:28], "payload": f"teacher:subject:{r['id']}"}] for r in rows[:25]]
        buttons.extend([[{"type":"callback","text":"📚 Мои предметы","payload":"teacher:subjects"}],[{"type": "callback", "text": "❌ Отмена", "payload": "menu"}]])
        max_send(user_id, "Выберите предмет для нового задания:", buttons)
    elif payload.startswith("teacher:subject:"):
        try:
            subject_id = int(payload.rsplit(":", 1)[1])
        except ValueError:
            return
        if not is_teacher_for_subject(user_id, subject_id):
            max_send(user_id, "⛔ Вы не назначены учителем этого предмета.")
            return
        with db() as conn:
            subject=conn.execute("SELECT sub.name,c.name AS class_name,s.name AS school_name FROM subjects sub JOIN classes c ON c.id=sub.class_id JOIN schools s ON s.id=c.school_id WHERE sub.id=?",(subject_id,)).fetchone()
        set_bot_state(user_id, "newtask_title", {"subject_id": subject_id, "subject_name": subject["name"], "class_name": subject["class_name"], "school_name":subject["school_name"]})
        max_send(user_id, f"Задание создаётся для класса «{subject['class_name']}», предмет «{subject['name']}» (школа «{subject['school_name']}»). Введите название.",keyboard([("Отмена","wizard:cancel")]))
    elif payload == "teacher:pending":
        if is_teacher or is_lead:
            process_bot_text(user_id, user.get("first_name", "Учитель"), "/работы")
        else:
            max_send(user_id, "⛔ Раздел доступен только учителю.")
    elif payload == "teacher:subjects":
        process_bot_text(user_id, user.get("first_name", "Учитель"), "/моипредметы")
    elif payload == "help":
        bot_help(user_id,is_admin=is_lead,is_teacher=is_teacher)
    elif payload == "admin:create_subject" and is_lead:
        with db() as conn:
            schools=conn.execute("SELECT id,name FROM schools ORDER BY name").fetchall()
        if not schools:
            max_send(user_id,"Сначала создайте школу.")
            return
        buttons=[[{"type":"callback","text":r["name"][:28],"payload":f"admin:subject_school:{r['id']}"}] for r in schools[:25]]
        max_send(user_id,"Выберите школу для нового предмета:",buttons)
    elif payload.startswith("admin:subject_school:") and is_lead:
        try:
            school_id=int(payload.rsplit(":",1)[1])
        except ValueError:
            return
        with db() as conn:
            school=conn.execute("SELECT id,name FROM schools WHERE id=?",(school_id,)).fetchone()
            teachers=conn.execute("SELECT teacher_user_id FROM school_teachers WHERE school_id=? ORDER BY teacher_user_id",(school_id,)).fetchall() if school else []
        if not school:
            max_send(user_id,"Школа не найдена.")
            return
        if not teachers:
            max_send(user_id,"Сначала назначьте учителя в эту школу.")
            return
        buttons=[[{"type":"callback","text":f"MAX {r['teacher_user_id']}","payload":f"admin:subject_teacher:{school_id}:{r['teacher_user_id']}"}] for r in teachers[:25]]
        max_send(user_id,f"Школа «{school['name']}». Выберите учителя предмета:",buttons)
    elif payload.startswith("admin:subject_teacher:") and is_lead:
        parts=payload.split(":")
        if len(parts)!=4 or not parts[2].isdigit() or not parts[3].isdigit():
            return
        school_id,teacher_id=int(parts[2]),parts[3]
        if not is_school_teacher(teacher_id,school_id):
            max_send(user_id,"Этот учитель не назначен в выбранную школу.")
            return
        set_bot_state(user_id,"newsubject_name",{"school_id":school_id,"teacher_id":teacher_id})
        max_send(user_id,"Введите название предмета (до 80 символов).")
    elif payload == "admin:create_school" and is_lead:
        set_bot_state(user_id,"newschool")
        max_send(user_id,"Введите название новой школы.")
    elif payload == "admin:schools" and is_lead:
        with db() as conn:
            schools=conn.execute("SELECT id,name FROM schools ORDER BY name").fetchall()
            teachers=conn.execute("SELECT st.school_id,st.teacher_user_id,s.name AS school FROM school_teachers st JOIN schools s ON s.id=st.school_id ORDER BY s.name,st.teacher_user_id").fetchall()
        rows=[[{"type":"callback","text":("🏫 "+s["name"])[:25],"payload":f"admin:class:{s['id']}"}] for s in schools[:15]]
        rows.extend([[{"type":"callback","text":f"Убрать {t['teacher_user_id']} · {t['school'][:12]}"[:28],"payload":f"admin:remove_teacher:{t['school_id']}:{t['teacher_user_id']}"}] for t in teachers[:10]])
        rows.extend([[{"type":"callback","text":"➕ Создать школу","payload":"admin:create_school"},{"type":"callback","text":"📋 Список школ","payload":"admin:schools_list"}],[{"type":"callback","text":"⬅ Меню","payload":"menu"}]])
        max_send(user_id,"🏫 Управление школами. Выберите школу для классов или удалите назначение учителя:",rows)
    elif payload == "admin:schools_list" and is_lead:
        with db() as conn:
            rows=conn.execute("SELECT s.id,s.name,st.teacher_user_id FROM schools s LEFT JOIN school_teachers st ON st.school_id=s.id ORDER BY s.name,st.teacher_user_id").fetchall()
        max_send(user_id,"Школы и назначенные учителя:\\n"+("\\n".join(f"{r['name']} — MAX {r['teacher_user_id']}" if r['teacher_user_id'] else f"{r['name']} — учителей пока нет" for r in rows) or "Школ пока нет"),keyboard([("⬅ Меню","menu")]))
    elif payload.startswith("admin:remove_teacher:") and is_lead:
        parts=payload.split(":")
        if len(parts)!=4 or not parts[2].isdigit() or not parts[3].isdigit():
            return
        school_id,teacher_id=int(parts[2]),parts[3]
        with db() as conn:
            catalog_ids=[r["id"] for r in conn.execute("SELECT id FROM subject_catalog WHERE school_id=? AND teacher_user_id=?",(school_id,teacher_id))]
            for cid in catalog_ids:
                conn.execute("UPDATE subjects SET teacher_user_id='0' WHERE catalog_id=?",(cid,))
            conn.execute("UPDATE subject_catalog SET teacher_user_id='0' WHERE school_id=? AND teacher_user_id=?",(school_id,teacher_id))
            conn.execute("DELETE FROM school_teachers WHERE school_id=? AND teacher_user_id=?",(school_id,teacher_id))
        max_send(user_id,f"Учитель MAX {teacher_id} удалён из школы и снят с её предметов.")
        teacher_menu(user_id)
    elif payload == "admin:create_achievement" and (is_teacher or is_lead):
        with db() as conn:
            if is_lead:
                rows=conn.execute("SELECT c.id,c.name,s.name AS school FROM classes c JOIN schools s ON s.id=c.school_id ORDER BY s.name,c.name").fetchall()
            else:
                rows=conn.execute("SELECT DISTINCT c.id,c.name,s.name AS school FROM classes c JOIN schools s ON s.id=c.school_id JOIN subjects sub ON sub.class_id=c.id WHERE sub.teacher_user_id=? ORDER BY s.name,c.name",(str(user_id),)).fetchall()
        if not rows:
            max_send(user_id,"Сначала создайте класс и назначьте учителя предмета.")
            return
        buttons=[[{"type":"callback","text":f"{r['school']} / {r['name']}"[:28],"payload":f"achievement:class:{r['id']}"}] for r in rows[:25]]
        buttons.append([{"type":"callback","text":"Отмена","payload":"menu"}])
        max_send(user_id,"Выберите класс для достижения:",buttons)
    elif payload.startswith("achievement:class:") and (is_teacher or is_lead):
        try:
            class_id=int(payload.rsplit(":",1)[1])
        except ValueError:
            return
        with db() as conn:
            allowed=is_lead or conn.execute("SELECT 1 FROM classes c JOIN school_teachers st ON st.school_id=c.school_id WHERE c.id=? AND st.teacher_user_id=?",(class_id,str(user_id))).fetchone()
            classname=conn.execute("SELECT name FROM classes WHERE id=?",(class_id,)).fetchone()
        if not allowed:
            max_send(user_id,"Вы не можете создавать достижения для этого класса.")
            return
        set_bot_state(user_id,"achievement_title",{"class_id":class_id})
        max_send(user_id,f"Создаём достижение для класса «{classname['name']}». Введите название.",keyboard([("Отмена","wizard:cancel")]))
    elif payload == "admin:add_admin" and is_lead:
        set_bot_state(user_id,"add_system_admin")
        max_send(user_id,"Отправьте числовой MAX ID нового системного администратора или напишите «все» для кода с шестью активациями. Выдавайте код только доверенным людям.")
    elif payload == "admin:newschool" and is_lead:
        set_bot_state(user_id, "newschool")
        max_send(user_id, "Введите название новой школы.")
    elif payload == "admin:assign_teacher" and is_lead:
        with db() as conn:
            rows = conn.execute("SELECT id,name FROM schools ORDER BY name").fetchall()
        if not rows:
            max_send(user_id, "Сначала создайте школу.")
            return
        buttons = [[{"type":"callback","text":r['name'][:20],"payload":f"admin:assign:{r['id']}"}] for r in rows[:25]]
        max_send(user_id, "Выберите школу, в которую добавить учителя:", buttons)
    elif payload == "admin:choose_class" and is_lead:
        with db() as conn:
            schools=conn.execute("SELECT id,name FROM schools WHERE EXISTS (SELECT 1 FROM subject_catalog sc WHERE sc.school_id=schools.id) ORDER BY name").fetchall()
        if not schools:
            max_send(user_id,"Сначала создайте предмет в панели администратора.")
        elif len(schools)==1:
            with db() as conn:
                subjects=conn.execute("SELECT id,name FROM subject_catalog WHERE school_id=? ORDER BY name",(schools[0]["id"],)).fetchall()
            buttons=[[{"type":"callback","text":r["name"][:28],"payload":f"admin:catalog:{r['id']}"}] for r in subjects[:25]]
            buttons.append([{"type":"callback","text":"Отмена","payload":"menu"}])
            max_send(user_id,f"Школа «{schools[0]['name']}». Выберите предмет, затем класс:",buttons)
        else:
            buttons=[[{"type":"callback","text":r["name"][:28],"payload":f"admin:attach_school:{r['id']}"}] for r in schools[:25]]
            buttons.append([{"type":"callback","text":"Отмена","payload":"menu"}])
            max_send(user_id,"Сначала выберите школу:",buttons)
    elif payload.startswith("admin:attach_school:") and is_lead:
        try:
            school_id=int(payload.rsplit(":",1)[1])
        except ValueError:
            return
        with db() as conn:
            school=conn.execute("SELECT id,name FROM schools WHERE id=?",(school_id,)).fetchone()
            subjects=conn.execute("SELECT id,name FROM subject_catalog WHERE school_id=? ORDER BY name",(school_id,)).fetchall() if school else []
        if not school:
            max_send(user_id,"Школа не найдена.")
            return
        buttons=[[{"type":"callback","text":r["name"][:28],"payload":f"admin:catalog:{r['id']}"}] for r in subjects[:25]]
        buttons.append([{"type":"callback","text":"⬅ Школы","payload":"admin:choose_class"}])
        max_send(user_id,f"Школа «{school['name']}». Выберите предмет:",buttons) if subjects else max_send(user_id,"В этой школе ещё нет предметов.")
    elif payload.startswith("admin:catalog:") and is_lead:
        try:
            catalog_id=int(payload.rsplit(":",1)[1])
        except ValueError:
            return
        with db() as conn:
            catalog=conn.execute("SELECT id,name,school_id FROM subject_catalog WHERE id=?",(catalog_id,)).fetchone()
            rows=conn.execute("SELECT id,name,join_code FROM classes WHERE school_id=? ORDER BY name",(catalog["school_id"],)).fetchall() if catalog else []
        if not catalog:
            max_send(user_id,"Предмет не найден.")
            return
        buttons=[[{"type":"callback","text":f"{r['name']} · код {r['join_code']}"[:28],"payload":f"admin:attach_class:{catalog_id}:{r['id']}"}] for r in rows[:25]]
        buttons.append([{"type":"callback","text":"⬅ Предметы","payload":f"admin:attach_school:{catalog['school_id']}"}])
        max_send(user_id,f"Предмет «{catalog['name']}». Выберите класс; код вступления показан рядом:",buttons) if rows else max_send(user_id,"В выбранной школе пока нет классов.")
    elif payload.startswith("admin:attach_class:") and is_lead:
        parts=payload.split(":")
        if len(parts)!=4 or not parts[2].isdigit() or not parts[3].isdigit():
            return
        catalog_id,class_id=map(int,parts[2:])
        with db() as conn:
            catalog=conn.execute("SELECT id,name,teacher_user_id,school_id FROM subject_catalog WHERE id=?",(catalog_id,)).fetchone()
            classroom=conn.execute("SELECT id,school_id FROM classes WHERE id=?",(class_id,)).fetchone()
            if not catalog or not classroom or classroom["school_id"]!=catalog["school_id"]:
                max_send(user_id,"Класс или предмет не найден, либо они принадлежат разным школам.")
                return
            conn.execute("INSERT INTO subjects(class_id,name,teacher_user_id,catalog_id) VALUES(?,?,?,?) ON CONFLICT(class_id,name) DO UPDATE SET teacher_user_id=excluded.teacher_user_id,catalog_id=excluded.catalog_id",(class_id,catalog["name"],catalog["teacher_user_id"],catalog_id))
        max_send(user_id,f"Класс добавлен к предмету «{catalog['name']}».")
        teacher_menu(user_id)
    elif payload.startswith("admin:subject_class:") and is_lead:
        max_send(user_id,"Теперь сначала создайте предмет в панели администратора, затем добавьте к нему один или несколько классов.")
    elif payload.startswith("admin:pickteacher:") and is_lead:
        max_send(user_id,"Старая кнопка создания предмета устарела. Создайте предмет в панели администратора, затем выберите для него классы.")
    elif payload.startswith("admin:assign:") and is_lead:
        try:
            school_id=int(payload.rsplit(":",1)[1])
        except ValueError:
            return
        set_bot_state(user_id,"assign_teacher",{"school_id":school_id})
        max_send(user_id,"Отправьте MAX ID учителя, которого нужно добавить в выбранную школу.")
    elif payload == "admin:subjects" and is_lead:
        with db() as conn:
            rows=conn.execute("SELECT sub.id,sub.name AS subject,sub.teacher_user_id,c.name AS class,s.name AS school FROM subjects sub JOIN classes c ON c.id=sub.class_id JOIN schools s ON s.id=c.school_id ORDER BY s.name,c.name,sub.name").fetchall()
        max_send(user_id,"📘 Предметы и назначенные учителя:\n"+("\n".join(f"#{r['id']} {r['school']} / {r['class']} / {r['subject']} — MAX ID {r['teacher_user_id']}" for r in rows) or "Предметов пока нет"),keyboard([("⬅ Меню","menu")]))
    elif payload == "student:leave_class":
        student=get_student(user_id)
        if not student or not student["class_id"]: max_send(user_id,"Вы не состоите в классе."); return
        set_bot_state(user_id,"confirm_leave_class",{"class_id":student["class_id"]})
        max_send(user_id,"Выйти из класса? Баллы сохранятся.",keyboard([("Выйти из класса","student:leave_class:confirm"),("Отмена","wizard:cancel")]))
    elif payload == "student:leave_class:confirm":
        state,data=get_bot_state(user_id)
        if state!="confirm_leave_class": max_send(user_id,"Подтверждение истекло."); return
        with db() as conn: conn.execute("UPDATE students SET class_id=NULL WHERE user_id=? AND class_id=?",(str(user_id),int(data["class_id"])))
        clear_bot_state(user_id);max_send(user_id,"Вы вышли из класса. Баллы сохранены.");student=get_student(user_id);student_menu(user_id,student["name"] if student else "Student")
    elif payload == "student:achievements:completed": show_achievement_tab(user_id,True,0)
    elif payload == "student:achievements:incomplete": show_achievement_tab(user_id,False,0)
    elif payload.startswith("student:achievements:page:"):
        q=payload.split(":")
        if len(q)==5 and q[3] in ("done","todo") and q[4].isdigit(): show_achievement_tab(user_id,q[3]=="done",int(q[4]))
    elif payload.startswith("student:achievement:info:"):
        q=payload.split(":")
        if len(q)==6 and q[3].isdigit() and q[4] in ("done","todo") and q[5].isdigit(): show_achievement_tab(user_id,q[4]=="done",int(q[5]),int(q[3]))
    elif payload.startswith("task:calendar:"):
        q=payload.split(":",3)
        if get_bot_state(user_id)[0]!="newtask_due_at": max_send(user_id,"Мастер создания задания не активен."); return
        if len(q)==4 and q[2]=="month":
            try: year,month=map(int,q[3].split("-"));show_deadline_calendar(user_id,year,month)
            except (ValueError,TypeError): max_send(user_id,"Некорректный месяц.")
        elif len(q)==4 and q[2]=="noop": return
        elif len(q)==4 and q[2]=="day":
            try: day=datetime.strptime(q[3],"%Y-%m-%d").date()
            except ValueError: max_send(user_id,"Некорректная дата."); return
            _,data=get_bot_state(user_id);data["selected_date"]=day.strftime("%Y-%m-%d");set_bot_state(user_id,"newtask_due_at",data)
            max_send(user_id,f"Выбрана дата {day:%d.%m.%Y}. Введите время ЧЧ:ММ.",keyboard([("Отмена","wizard:cancel")]))
    elif payload == "teacher:achievements": show_teacher_achievements(user_id,0)
    elif payload.startswith("teacher:achievements:page:"):
        q=payload.rsplit(":",1)[1]
        if q.isdigit(): show_teacher_achievements(user_id,int(q))
    elif payload == "teacher:deduct_points":
        if not is_teacher and not is_lead: max_send(user_id,"Раздел доступен только учителю."); return
        with db() as conn:
            if is_lead: rows=conn.execute("SELECT user_id,name,points FROM students WHERE user_id NOT IN (SELECT user_id FROM system_admins) ORDER BY name").fetchall()
            else: rows=conn.execute("SELECT DISTINCT s.user_id,s.name,s.points FROM students s JOIN classes c ON c.id=s.class_id JOIN school_teachers st ON st.school_id=c.school_id WHERE st.teacher_user_id=? AND s.user_id NOT IN (SELECT user_id FROM system_admins) ORDER BY s.name",(str(user_id),)).fetchall()
        buttons=[[{"type":"callback","text":f"{r['name']} - {r['points']} points"[:28],"payload":f"teacher:deduct_points:{r['user_id']}"}] for r in rows[:25]]
        max_send(user_id,"Выберите ученика для списания баллов:",buttons) if rows else max_send(user_id,"Ученики не найдены.")
    elif payload.startswith("teacher:deduct_points:"):
        sid=payload.rsplit(":",1)[1]
        if not sid.isdigit() or (not is_teacher and not is_lead): return
        set_bot_state(user_id,"deduct_student_points",{"student_id":sid});max_send(user_id,"Введите количество баллов для списания.",keyboard([("Отмена","wizard:cancel")]))
    elif payload.startswith("achievement:manage:"):
        q=payload.split(":")
        if len(q)!=4 or not q[2].isdigit() or not q[3].isdigit(): return
        aid,page=int(q[2]),int(q[3])
        if not owns_achievement(user_id,aid): max_send(user_id,"Нет доступа."); return
        with db() as conn: row=conn.execute("SELECT title,reward_points,max_claims,code_value FROM achievements WHERE id=?",(aid,)).fetchone()
        if not row: max_send(user_id,"Достижение не найдено."); return
        code=row["code_value"] or "Код не сохранён. Выпустите новый код."
        text=f"{row['title']}\nНаграда: {row['reward_points']} баллов\nЛимит активаций: {row['max_claims']}\nКод: {code}"
        buttons=keyboard([("Изменить название",f"achievement:edit_title:{aid}:{page}")],[("Изменить баллы",f"achievement:edit_points:{aid}:{page}")],[("Изменить лимит",f"achievement:edit_limit:{aid}:{page}")],[("Выпустить новый код",f"achievement:regenerate:{aid}:{page}")],[("Удалить",f"achievement:delete:{aid}:{page}")],[("Отмена","wizard:cancel")])
        max_send(user_id,text,buttons)
    elif payload.startswith("achievement:edit_title:") or payload.startswith("achievement:edit_points:") or payload.startswith("achievement:edit_limit:"):
        q=payload.split(":")
        if len(q)!=4 or not q[2].isdigit() or not q[3].isdigit(): return
        aid,page=int(q[2]),int(q[3])
        if not owns_achievement(user_id,aid): max_send(user_id,"Нет доступа."); return
        state={"edit_title":"achievement_edit_title","edit_points":"achievement_edit_points","edit_limit":"achievement_edit_limit"}[q[1]]
        prompt={"edit_title":"Введите новое название.","edit_points":"Введите награду в баллах от 0 до 1000.","edit_limit":"Введите лимит активаций от 1 до 10000."}[q[1]]
        set_bot_state(user_id,state,{"achievement_id":aid,"page":page});max_send(user_id,prompt,keyboard([("Отмена","wizard:cancel")]))
    elif payload.startswith("achievement:regenerate:"):
        q=payload.split(":")
        if len(q)!=4 or not q[2].isdigit() or not q[3].isdigit(): return
        aid,page=int(q[2]),int(q[3])
        if not owns_achievement(user_id,aid): max_send(user_id,"Нет доступа."); return
        code=create_achievement_code();digest=hashlib.sha256(code.casefold().encode("utf-8")).hexdigest()
        with db() as conn: conn.execute("UPDATE achievements SET code_hash=?,code_value=? WHERE id=?",(digest,code,aid))
        max_send(user_id,f"Новый код: {code}. Передайте его ученикам.",keyboard([("К достижению",f"achievement:manage:{aid}:{page}")],[("Отмена","wizard:cancel")]))
    elif payload.startswith("achievement:delete:"):
        q=payload.split(":")
        if len(q)!=4 or not q[2].isdigit() or not q[3].isdigit(): return
        aid,page=int(q[2]),int(q[3])
        if not owns_achievement(user_id,aid): max_send(user_id,"Нет доступа."); return
        with db() as conn:
            conn.execute("DELETE FROM achievement_claims WHERE achievement_id=?",(aid,));conn.execute("DELETE FROM achievements WHERE id=?",(aid,))
        max_send(user_id,"Достижение удалено.");show_teacher_achievements(user_id,page)
    elif payload.startswith("admin:class:") and is_lead:
        sid=payload.rsplit(":",1)[1]
        if not sid.isdigit(): return
        with db() as conn:
            school=conn.execute("SELECT name FROM schools WHERE id=?",(int(sid),)).fetchone()
            classes=conn.execute("SELECT name,join_code FROM classes WHERE school_id=? ORDER BY name",(int(sid),)).fetchall()
        if not school: max_send(user_id,"Школа не найдена."); return
        listing="\n".join(f"{c['name']} — код {c['join_code']}" for c in classes) or "Классов пока нет."
        max_send(user_id,f"{school['name']}\n{listing}",keyboard([("Создать класс",f"admin:newclass:{sid}")],[("Назначить учителя","admin:assign_teacher")],[("К достижению","admin:schools")]))
    elif payload.startswith("admin:newclass:") and is_lead:
        sid=payload.rsplit(":",1)[1]
        if not sid.isdigit(): return
        set_bot_state(user_id,"newclass",{"school_id":int(sid)});max_send(user_id,"Введите название класса.",keyboard([("Отмена","wizard:cancel")]))
    elif payload.startswith("reward:"):
        parts=payload.split(":")
        if len(parts)!=3 or not parts[1].isdigit() or parts[2] not in ("approve","reject"):
            return
        ok,message=review_reward_request(int(parts[1]),parts[2]=="approve",user_id)
        max_send(user_id,message)
        if ok:
            teacher_menu(user_id) if (is_lead or is_teacher) else student_menu(user_id)
    elif payload.startswith("review:"):
        parts=payload.split(":")
        if len(parts)!=3 or not parts[1].isdigit() or parts[2] not in ("approve","reject"):
            return
        submission_id=int(parts[1])
        if parts[2]=="approve":
            with db() as conn:
                task=conn.execute("SELECT t.points FROM submissions s JOIN tasks t ON t.id=s.task_id JOIN subjects sub ON sub.id=t.subject_id WHERE s.id=? AND s.status='pending' AND (? OR (sub.teacher_user_id=? AND EXISTS (SELECT 1 FROM classes c JOIN school_teachers st ON st.school_id=c.school_id WHERE c.id=sub.class_id AND st.teacher_user_id=?)))",(submission_id,int(is_system_admin(user_id)),str(user_id),str(user_id))).fetchone()
            if not task and is_system_admin(user_id):
                with db() as conn:
                    task=conn.execute("SELECT t.points FROM submissions s JOIN tasks t ON t.id=s.task_id WHERE s.id=? AND s.status='pending'",(submission_id,)).fetchone()
            if not task:
                max_send(user_id,"⛔ Заявка не найдена или у вас нет прав проверки.")
                return
            set_bot_state(user_id,"review_points",{"submission_id":submission_id,"max_points":task["points"]})
            max_send(user_id,f"Введите баллы за ответ от 0 до {task['points']}.",keyboard([("Отмена","wizard:cancel")]))
        elif not approve_submission(submission_id,False,user_id):
            max_send(user_id,"⛔ Заявка не найдена или у вас нет прав проверки.")
        else:
            max_send(user_id,"Работа отправлена на доработку.")

def process_bot_text(user_id, name, text):
    text = (text or "").strip()
    if not text:
        return
    if len(text)==12 and text.isalnum():
        code_hash=hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()
        with db() as conn:
            invite_exists=conn.execute("SELECT 1 FROM admin_invites WHERE code_hash=?",(code_hash,)).fetchone()
        if invite_exists:
            success,message=redeem_admin_invite(user_id,text)
            max_send(user_id,message)
            if success:
                teacher_menu(user_id)
            return
    if continue_task_wizard(user_id, text):
        return
    student = get_student(user_id)
    saved_name = (student["name"] or "").strip() if student else ""
    if not student or len(saved_name.split()) < 2:
        set_bot_state(user_id, "registration_name")
        max_send(user_id, "Добро пожаловать! Для регистрации напишите имя и фамилию через пробел, например: Анна Иванова.")
        return
    command, _, rest = text.partition(" ")
    command = command.lower().split("@", 1)[0]
    is_lead_teacher = is_system_admin(user_id)
    is_teacher = is_subject_teacher(user_id)

    if command in ("/start", "старт"):
        if is_lead_teacher and admin_student_mode(user_id):
            student_menu(user_id,saved_name)
        elif is_lead_teacher or is_teacher:
            teacher_menu(user_id)
        else:
            student_menu(user_id, saved_name)
    elif command in ("/режимученика", "/ученик") and is_lead_teacher:
        set_admin_student_mode(user_id,True)
        student_menu(user_id,saved_name)
    elif command in ("/режимадминистратора", "/админменю") and is_lead_teacher:
        set_admin_student_mode(user_id,False)
        teacher_menu(user_id)
    elif command in ("/help", "/помощь", "помощь"):
        bot_help(user_id,is_admin=is_lead_teacher,is_teacher=is_teacher)
    elif command == "/id":
        max_send(user_id, f"Ваш MAX ID: {user_id}. Для назначения учителем укажите этот ID в TEACHER_MAX_ID и перезапустите приложение.")
    elif command in ("/join", "/вступить"):
        code = rest.strip().upper()
        with db() as conn:
            classroom = conn.execute("SELECT id,name FROM classes WHERE join_code=?", (code,)).fetchone()
            if not classroom:
                max_send(user_id, "Код класса не найден. Попросите учителя проверить код.")
                return
            conn.execute("UPDATE students SET class_id=? WHERE user_id=?", (classroom["id"], str(user_id)))
        max_send(user_id, f"Вы вступили в класс «{classroom['name']}»! Напишите /tasks, чтобы увидеть задания.")
    elif command in ("/суперадмин", "/добавитьадмин", "/добавитьадмина", "/newadmin"):
        argument=rest.strip()
        if is_lead_teacher and (not argument or argument.isdigit()):
            try:
                invite=issue_admin_invite(argument,user_id)
            except ValueError as exc:
                max_send(user_id,f"{exc} Формат: /суперадмин [MAX_ID].")
                return
            target_label=f"для MAX ID {argument}" if argument else "для шести доверенных пользователей"
            max_send(user_id,f"Код назначения администратора {target_label}: {invite}. Допускает до 6 активаций разными MAX ID. Чтобы активировать роль, отправьте этот код отдельным сообщением боту.")
            return
        if argument:
            success,message=redeem_admin_invite(user_id,argument)
            max_send(user_id,message)
            if success:
                teacher_menu(user_id)
            return
        max_send(user_id,"Отправьте код назначения администратора отдельным сообщением, чтобы активировать роль.")
    elif command in ("/claimachievement", "/достижение", "/коддостижения"):
        code=rest.strip().upper()
        if not code:
            set_bot_state(user_id,"claim_achievement_code")
            max_send(user_id,"Введите код достижения, который выдал учитель.")
            return
        claim_achievement(user_id,code)
    elif command in ("/achievements", "/достижения"):
        student=get_student(user_id)
        count=0
        custom=[]
        if student:
            with db() as conn:
                count=conn.execute("SELECT COUNT(*) FROM submissions WHERE user_id=? AND status='approved'",(str(user_id),)).fetchone()[0]
                custom=[r["title"] for r in conn.execute("SELECT a.title FROM achievement_claims c JOIN achievements a ON a.id=c.achievement_id WHERE c.user_id=? ORDER BY c.claimed_at",(str(user_id),))]
        current_points=student["points"] if student else 0
        summary=achievement_summary(current_points,count,custom)
        achievement_lines="\n".join("• "+x for x in summary["achievements"]) or "Пока достижений нет."
        next_task=(f"{summary['next_task_achievement']} выполненных заданий" if summary["next_task_achievement"] is not None else "все цели по заданиям выполнены")
        next_collection=(f"число базовых достижений — {summary['next_collection_achievement']}" if summary["next_collection_achievement"] is not None else "все цели коллекции выполнены")
        message=("🏆 Ваши достижения:\n"+achievement_lines+
                 "\n\nПрогресс целей достижений и уровня:\n"+achievement_progress_text(current_points,summary)+
                 f"\n\nБлижайшие цели: {next_task}; следующий уровень {summary['next_level_achievement']}; коллекция: {next_collection}.")
        max_send(user_id,message,keyboard([("🎟 Ввести код","student:claim_achievement"),("↩ Меню","menu")]))
    elif command in ("/newschool", "/создатьшколу"):
        if not is_lead_teacher:
            max_send(user_id, "⛔ Создавать школы может только главный учитель, указанный в TEACHER_MAX_ID.")
            return
        school_name = rest.strip()[:120]
        if not school_name:
            max_send(user_id, "Формат: /создатьшколу Название школы")
            return
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO schools(name) VALUES(?)", (school_name,))
        max_send(user_id, f"🏫 Школа «{school_name}» добавлена.")
    elif command in ("/newclass", "/создатькласс"):
        if not is_lead_teacher:
            max_send(user_id, "⛔ Создавать классы может только главный учитель.")
            return
        parts = [x.strip() for x in rest.split("|", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            max_send(user_id, "Формат: /newclass Название школы | Название класса")
            return
        with db() as conn:
            school = conn.execute("SELECT id FROM schools WHERE name=?", (parts[0],)).fetchone()
            if not school:
                max_send(user_id, "Сначала создайте эту школу командой /newschool.")
                return
            classroom = conn.execute("SELECT id,join_code FROM classes WHERE school_id=? AND name=?", (school["id"], parts[1])).fetchone()
            if not classroom:
                code = create_join_code()
                cur = conn.execute("INSERT INTO classes(school_id,name,join_code) VALUES(?,?,?)", (school["id"], parts[1], code))
                classroom_id = cur.lastrowid
            else:
                classroom_id, code = classroom["id"], classroom["join_code"]
        max_send(user_id, f"Класс «{parts[1]}» создан. Код для учеников: {code}\nУченики присоединяются командой /join {code}.")
    elif command in ("/assignteacher", "/назначитьучителя"):
        if not is_lead_teacher:
            max_send(user_id, "⛔ Только системный администратор может добавлять учителей в школу.")
            return
        parts = [x.strip() for x in rest.split("|", 1)]
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            max_send(user_id, "Формат: /assignteacher ШКОЛА_ID | MAX_ID_учителя")
            return
        with db() as conn:
            school = conn.execute("SELECT name FROM schools WHERE id=?", (int(parts[0]),)).fetchone()
            if not school:
                max_send(user_id, "Школа не найдена. Посмотрите список командой /myclasses.")
                return
            conn.execute("INSERT OR IGNORE INTO school_teachers(school_id,teacher_user_id) VALUES(?,?)", (int(parts[0]),parts[1]))
        max_send(user_id, f"✅ Учитель MAX ID {parts[1]} назначен в школу «{school['name']}».")
    elif command in ("/newsubject", "/создатьпредмет"):
        if not is_lead_teacher:
            max_send(user_id, "⛔ Только системный администратор может создавать предметы.")
            return
        parts = [x.strip() for x in rest.split("|", 2)]
        if len(parts) != 3 or not all(parts) or not parts[2].isdigit():
            max_send(user_id, "Формат: /newsubject Название школы | Предмет | MAX_ID_учителя. Класс добавьте к предмету через панель администратора.")
            return
        with db() as conn:
            school = conn.execute("SELECT id FROM schools WHERE name=?", (parts[0],)).fetchone()
            if not school:
                max_send(user_id, "Школа не найдена.")
                return
            if not conn.execute("SELECT 1 FROM school_teachers WHERE school_id=? AND teacher_user_id=?", (school["id"],parts[2])).fetchone():
                max_send(user_id, "Сначала добавьте учителя в школу командой /assignteacher ШКОЛА_ID | MAX_ID_учителя.")
                return
            conn.execute("INSERT INTO subject_catalog(school_id,name,teacher_user_id) VALUES(?,?,?) ON CONFLICT(school_id,name,teacher_user_id) DO NOTHING", (school["id"],parts[1],parts[2]))
        max_send(user_id, f"Предмет «{parts[1]}» создан. Теперь добавьте к нему классы в панели администратора.")
    elif command == "/myclasses" and is_lead_teacher:
        with db() as conn:
            rows = conn.execute("SELECT s.name AS school,c.name AS class,c.join_code,sub.id AS subject_id,sub.name AS subject,sub.teacher_user_id FROM schools s JOIN classes c ON c.school_id=s.id LEFT JOIN subjects sub ON sub.class_id=c.id ORDER BY s.name,c.name,sub.name").fetchall()
        lines = [f"{r['school']} / {r['class']} — код {r['join_code']}" + (f"\n  Предмет #{r['subject_id']}: {r['subject']}; учитель MAX ID {r['teacher_user_id']}" if r['subject_id'] else "\n  Предметов пока нет") for r in rows]
        max_send(user_id, "🏫 Школы, классы и предметы:\n" + ("\n".join(lines) or "Пока пусто"))
    elif command in ("/mysubjects", "/моипредметы"):
        with db() as conn:
            if is_lead_teacher:
                rows=conn.execute("SELECT sub.id,sub.name AS subject,c.name AS class,s.name AS school FROM subjects sub JOIN classes c ON c.id=sub.class_id JOIN schools s ON s.id=c.school_id ORDER BY s.name,c.name,sub.name").fetchall()
            else:
                rows = conn.execute("SELECT sub.id,sub.name AS subject,c.name AS class,s.name AS school FROM subjects sub JOIN classes c ON c.id=sub.class_id JOIN schools s ON s.id=c.school_id WHERE sub.teacher_user_id=? ORDER BY s.name,c.name,sub.name", (str(user_id),)).fetchall()
        max_send(user_id, "📚 Ваши предметы:\n" + ("\n".join(f"#{r['id']} {r['school']} / {r['class']} / {r['subject']}" for r in rows) or "За вами пока не закреплены предметы."),keyboard([("➕ Создать задание","teacher:create"),("📥 Мои работы","teacher:pending")],[("📚 Обновить предметы","teacher:subjects"),("❔ Помощь","help")],[("↩ Меню","menu")]))
    elif command in ("/rewards", "/награды") and (is_teacher or is_lead_teacher):
        with db() as conn:
            if is_lead_teacher:
                rows=conn.execute("SELECT rr.id,rr.user_id,rr.reward,rr.cost,st.name AS student_name,st.points AS balance,c.name AS class_name,s.name AS school_name FROM reward_requests rr JOIN students st ON st.user_id=rr.user_id JOIN classes c ON c.id=st.class_id JOIN schools s ON s.id=c.school_id WHERE rr.status='pending' ORDER BY rr.id").fetchall()
            else:
                rows=conn.execute("SELECT rr.id,rr.user_id,rr.reward,rr.cost,st.name AS student_name,st.points AS balance,c.name AS class_name,s.name AS school_name FROM reward_requests rr JOIN students st ON st.user_id=rr.user_id JOIN classes c ON c.id=st.class_id JOIN schools s ON s.id=c.school_id JOIN school_teachers assigned ON assigned.school_id=c.school_id WHERE rr.status='pending' AND assigned.teacher_user_id=? ORDER BY rr.id",(str(user_id),)).fetchall()
        if not rows:
            max_send(user_id,"Запросов наград на проверку нет.",keyboard([("🔄 Обновить","teacher:rewards"),("🏠 Меню","menu")]))
        else:
            for row in rows[:20]:
                max_send(user_id,f"🎁 Запрос награды #{row['id']}\n{row['school_name']} / {row['class_name']}\n{row['student_name']} (ID {row['user_id']}): {row['reward']}\nЦена: {row['cost']} баллов; баланс: {row['balance']}.",keyboard([("✅ Одобрить и списать",f"reward:{row['id']}:approve"),("❌ Отклонить",f"reward:{row['id']}:reject")],[ ("🔄 Обновить","teacher:rewards"),("🏠 Меню","menu")]))
    elif command in ("/tasks", "/задания"):
        with db() as conn:
            student = conn.execute("SELECT class_id FROM students WHERE user_id=?", (str(user_id),)).fetchone()
            if not student or not student["class_id"]:
                max_send(user_id, "Сначала присоединитесь к классу командой /join КОД. Код попросите у учителя.")
                return
            tasks = conn.execute("SELECT t.id,t.class_number AS task_number,t.title,t.description,t.points,t.due_at,s.name AS subject,(SELECT x.status FROM submissions x WHERE x.task_id=t.id AND x.user_id=? ORDER BY x.id DESC LIMIT 1) AS submission_status FROM tasks t JOIN subjects s ON s.id=t.subject_id WHERE t.active=1 AND s.class_id=? ORDER BY s.name,t.id", (str(user_id),student["class_id"])).fetchall()
        if not tasks:
            max_send(user_id, "Пока нет активных заданий. Учитель скоро их добавит!")
        else:
            lines=["📚 Активные задания:"]
            for task in tasks:
                status=task["submission_status"]
                label="Выполнено" if status=="approved" else "На проверке" if status=="pending" else "Не выполнено"
                deadline=f"\nСрок UTC: {task['due_at']}" if task["due_at"] else "\nСрок не установлен"
                lines.append(f"\n№{task['task_number']} [{task['subject']}] {task['title']} — {task['points']} баллов\nСтатус: {label}{deadline}\n{task['description']}"+("" if status in ("approved","pending") or task_is_closed(task["due_at"]) else f"\nСдать: /submit {task['task_number']} ваш ответ"))
            max_send(user_id, "\n".join(lines))
    elif command in ("/points", "/баллы"):
        student = get_student(user_id)
        points = student["points"] if student else 0
        progress = level_for_points(points)
        remaining = progress["next_level_xp"] - progress["xp"]
        with db() as conn:
            accepted=conn.execute("SELECT COUNT(*) FROM submissions WHERE user_id=? AND status='approved'",(str(user_id),)).fetchone()[0]
            titles=[r["title"] for r in conn.execute("SELECT a.title FROM achievement_claims ac JOIN achievements a ON a.id=ac.achievement_id WHERE ac.user_id=? ORDER BY ac.claimed_at",(str(user_id),))]
        summary=achievement_summary(points,accepted,titles)
        max_send(user_id, f"⭐ Баллы: {points}\n✨ Уровень: {progress['level']}\nОпыт: {progress['progress_xp']}/{progress['level_xp']} до следующего уровня\n{progress_meter(progress['progress_xp'],progress['level_xp'])}\nДо уровня {progress['level'] + 1}: ещё {remaining} XP.\n\nПрогресс целей достижений:\n{achievement_progress_text(points,summary)}")
    elif command in ("/top", "/рейтинг"):
        with db() as conn:
            student = conn.execute("SELECT class_id FROM students WHERE user_id=?", (str(user_id),)).fetchone()
            rows = conn.execute("SELECT name,points FROM students WHERE class_id=? ORDER BY points DESC,name LIMIT 10", (student["class_id"],)).fetchall() if student and student["class_id"] else []
        max_send(user_id, "🏆 Рейтинг класса:\n" + ("\n".join(f"{i}. {r['name']} — {r['points']}" for i, r in enumerate(rows, 1)) or "В вашем классе пока нет участников"))
    elif command in ("/submit", "/ответ"):
        parts = rest.split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            max_send(user_id, "Формат: /submit НОМЕР_ЗАДАНИЯ ваш ответ")
            return
        task_id, answer = int(parts[0]), parts[1].strip()
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT t.id,t.title,t.points,t.correct_answer,t.due_at,s.name AS subject FROM tasks t JOIN subjects s ON s.id=t.subject_id WHERE t.class_number=? AND t.active=1 AND s.class_id=(SELECT class_id FROM students WHERE user_id=?)", (task_id, str(user_id))).fetchone()
            if not task:
                max_send(user_id, "Не нашёл активное задание с таким номером. Напишите /tasks.")
                return
            if task_is_closed(task["due_at"]):
                max_send(user_id,f"Срок сдачи задания «{task['title']}» истёк. Ответ больше принять нельзя.")
                return
            existing = conn.execute("SELECT id,status FROM submissions WHERE task_id=? AND user_id=? AND status IN ('pending','approved') ORDER BY id DESC LIMIT 1", (task_id, str(user_id))).fetchone()
            if existing:
                if existing["status"] == "approved":
                    max_send(user_id, "Это задание уже зачтено. Повторно получить за него баллы нельзя.")
                else:
                    max_send(user_id, f"Ответ уже ожидает проверки (заявка #{existing['id']}).")
                return
            if task["correct_answer"]:
                normalize = lambda value: " ".join(value.casefold().split())
                if normalize(answer) != normalize(task["correct_answer"]):
                    max_send(user_id, "❌ Ответ пока неверный. Проверьте решение и отправьте ещё раз.")
                    return
                cur = conn.execute("INSERT INTO submissions(task_id,user_id,answer,status,reviewed_at) VALUES(?,?,?,'approved',CURRENT_TIMESTAMP)", (task_id, str(user_id), answer))
                conn.execute("UPDATE students SET points=points+? WHERE user_id=?", (task["points"], str(user_id)))
                accepted_count=conn.execute("SELECT COUNT(*) FROM submissions WHERE user_id=? AND status='approved'",(str(user_id),)).fetchone()[0]
                claimed_count=conn.execute("SELECT COUNT(*) FROM achievement_claims WHERE user_id=?",(str(user_id),)).fetchone()[0]
                bonus=award_threshold_points(user_id,accepted_count,claimed_count,conn=conn)
                updated_points = conn.execute("SELECT points FROM students WHERE user_id=?", (str(user_id),)).fetchone()["points"]
                progress = level_for_points(updated_points)
                max_send(user_id, f"✅ Верно! За «{task['title']}» начислено {task['points']} баллов; бонус достижений: {bonus}. ⭐\n✨ Уровень {progress['level']} · {progress['progress_xp']}/{progress['level_xp']} XP до следующего уровня.\n{progress_meter(progress['progress_xp'],progress['level_xp'])}")
                return
            cur = conn.execute("INSERT INTO submissions(task_id,user_id,answer) VALUES(?,?,?)", (task_id, str(user_id), answer))
            submission_id = cur.lastrowid
        max_send(user_id, f"📝 Ответ на «{task['title']}» отправлен учителю на проверку. Заявка #{submission_id}.")
        with db() as conn:
            teachers = conn.execute("SELECT DISTINCT s.teacher_user_id FROM subjects s JOIN tasks t ON t.subject_id=s.id WHERE t.id=?", (task_id,)).fetchall()
        for teacher in teachers:
            max_send(teacher["teacher_user_id"], f"📝 Новая работа #{submission_id}\nУченик: {name} (ID {user_id})\nПредмет: {task['subject']}\nЗадание: {task['title']}\nОтвет: {answer}\nПроверьте работу в панели учителя.")
    elif command in ("/newtask", "/teacher_task"):
        fields = [x.strip() for x in rest.split("|", 4)]
        if len(fields) != 5 or not fields[0].isdigit() or not fields[3].isdigit() or not 1 <= int(fields[3]) <= 1000:
            max_send(user_id, "Формат: /newtask ПРЕДМЕТ_ID | Название | Описание | Баллы | Ответ (или ручная)\nПосмотрите ID предметов командой /myclasses.")
            return
        subject_id = int(fields[0])
        if not is_teacher_for_subject(user_id, subject_id):
            max_send(user_id, "⛔ Только назначенный учитель или системный администратор может создавать в нём задания.")
            return
        if not fields[1] or not fields[2] or len(fields[1]) > 120 or len(fields[2]) > 1000 or len(fields[4]) > 500:
            max_send(user_id, "Название и описание обязательны; ограничения: название 120, описание 1000, ответ 500 символов.")
            return
        correct_answer = "" if fields[4].casefold() in ("ручная", "вручную", "нет") else fields[4]
        task_id = create_task(fields[1], fields[2], int(fields[3]), correct_answer, subject_id)
        mode = "учитель проверит вручную" if not correct_answer else "ответ будет проверяться автоматически"
        max_send(user_id, f"✅ Задание #{task_id} добавлено в предмет #{subject_id}: {fields[1]}; {mode}.",keyboard([("➕ Создать ещё","teacher:create"),("📥 Мои работы","teacher:pending")],[("📚 Мои предметы","teacher:subjects"),("❔ Помощь","help")],[("↩ Меню","menu")]))
    elif command in ("/pending", "/works") and (is_teacher or is_lead_teacher):
        with db() as conn:
            if is_lead_teacher:
                rows=conn.execute("SELECT s.id,s.answer,s.user_id,t.class_number,t.title,sub.name AS subject FROM submissions s JOIN tasks t ON t.id=s.task_id JOIN subjects sub ON sub.id=t.subject_id WHERE s.status='pending' ORDER BY s.id").fetchall()
            else:
                rows=conn.execute("SELECT s.id,s.answer,s.user_id,t.class_number,t.title,sub.name AS subject FROM submissions s JOIN tasks t ON t.id=s.task_id JOIN subjects sub ON sub.id=t.subject_id JOIN classes c ON c.id=sub.class_id JOIN school_teachers st ON st.school_id=c.school_id WHERE s.status='pending' AND st.teacher_user_id=? AND sub.teacher_user_id=? ORDER BY s.id",(str(user_id),str(user_id))).fetchall()
        if not rows:
            max_send(user_id,"\u041d\u0435\u0442 \u0440\u0430\u0431\u043e\u0442 \u043d\u0430 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0443.",keyboard([("\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c","teacher:pending"),("\u041c\u043e\u0438 \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u044b","teacher:subjects")]))
        else:
            for row in rows[:20]:
                buttons=keyboard([("\u2705 \u041f\u0440\u0438\u043d\u044f\u0442\u044c",f"review:{row['id']}:approve"),("\u21a9 \u041d\u0430 \u0434\u043e\u0440\u0430\u0431\u043e\u0442\u043a\u0443",f"review:{row['id']}:reject")],[("\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c","teacher:pending"),("\u041c\u0435\u043d\u044e","menu")])
                max_send(user_id,f"\u0420\u0430\u0431\u043e\u0442\u0430 #{row['id']} \u00b7 \u0437\u0430\u0434\u0430\u043d\u0438\u0435 \u2116{row['class_number']} \u00b7 {row['subject']}\\n{row['title']}\\n\u0423\u0447\u0435\u043d\u0438\u043a ID {row['user_id']}\\n\u041e\u0442\u0432\u0435\u0442: {row['answer']}",buttons)
    elif command in ("/approve", "/reject"):
        submission_id = rest.strip().split()[0] if rest.strip() else ""
        if not submission_id.isdigit():
            max_send(user_id, f"Формат: {command} НОМЕР_ЗАЯВКИ")
            return
        if command == "/approve":
            with db() as conn:
                task=conn.execute("SELECT t.points FROM submissions s JOIN tasks t ON t.id=s.task_id JOIN subjects sub ON sub.id=t.subject_id WHERE s.id=? AND s.status='pending' AND (? OR sub.teacher_user_id=?)",(int(submission_id),int(is_lead_teacher),str(user_id))).fetchone()
            if not task:
                max_send(user_id,"Работа не найдена или у вас нет прав проверки.")
                return
            set_bot_state(user_id,"review_points",{"submission_id":int(submission_id),"max_points":task["points"]})
            max_send(user_id,f"Введите баллы за ответ от 0 до {task['points']}.")
            return
        if not approve_submission(int(submission_id), False, user_id):
            max_send(user_id, "⛔ Заявка не найдена или у вас нет прав проверки.")
            return
        max_send(user_id, "Результат сохранён, ученику отправлено уведомление.")
    else:
        max_send(user_id, "Не понял команду. Напишите /help для списка возможностей.")


def review_reward_request(request_id, approved, reviewer_id):
    reviewer_id=str(reviewer_id)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        query="SELECT rr.id,rr.user_id,rr.reward,rr.cost,st.points,c.school_id FROM reward_requests rr JOIN students st ON st.user_id=rr.user_id JOIN classes c ON c.id=st.class_id WHERE rr.id=? AND rr.status='pending'"
        params=[request_id]
        if not is_system_admin(reviewer_id):
            query+=" AND EXISTS (SELECT 1 FROM school_teachers assigned WHERE assigned.school_id=c.school_id AND assigned.teacher_user_id=?)"
            params.append(reviewer_id)
        row=conn.execute(query,params).fetchone()
        if not row:
            return False,"Запрос не найден или ученик не из вашей школы."
        if approved and row["points"]<row["cost"]:
            conn.execute("UPDATE reward_requests SET status='rejected',reviewed_at=CURRENT_TIMESTAMP,reviewed_by=? WHERE id=? AND status='pending'",(reviewer_id,request_id))
            return False,"У ученика больше недостаточно баллов; запрос отклонён без списания."
        status="approved" if approved else "rejected"
        if approved:
            updated=conn.execute("UPDATE students SET points=points-? WHERE user_id=? AND points>=?",(row["cost"],row["user_id"],row["cost"]))
            if not updated.rowcount:
                return False,"Баланс изменился; запрос не одобрен и баллы не списаны."
        conn.execute("UPDATE reward_requests SET status=?,reviewed_at=CURRENT_TIMESTAMP,reviewed_by=? WHERE id=? AND status='pending'",(status,reviewer_id,request_id))
    if approved:
        max_send(row["user_id"],f"🎁 Запрос награды «{row['reward']}» одобрен! Списано {row['cost']} баллов.")
        return True,"Запрос одобрен, баллы списаны."
    max_send(row["user_id"],f"Запрос награды «{row['reward']}» отклонён. Баллы не списаны.")
    return True,"Запрос отклонён. Баллы не списаны."


def approve_submission(submission_id, approved, reviewer_id=None, awarded_points=None):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        query = "SELECT s.*,t.title,t.points,st.name,st.points AS current_points,sub.teacher_user_id FROM submissions s JOIN tasks t ON t.id=s.task_id JOIN subjects sub ON sub.id=t.subject_id JOIN students st ON st.user_id=s.user_id WHERE s.id=? AND s.status='pending'"
        params = [submission_id]
        if reviewer_id is not None and not is_system_admin(reviewer_id):
            query += " AND sub.teacher_user_id=? AND EXISTS (SELECT 1 FROM classes c JOIN school_teachers st ON st.school_id=c.school_id WHERE c.id=sub.class_id AND st.teacher_user_id=?)"
            params.extend([str(reviewer_id),str(reviewer_id)])
        row = conn.execute(query, params).fetchone()
        if not row:
            return False
        awarded=row["points"] if awarded_points is None else int(awarded_points)
        if approved and not 0 <= awarded <= row["points"]:
            raise ValueError(f"Можно начислить от 0 до {row['points']} баллов.")
        status = "approved" if approved else "rejected"
        conn.execute("UPDATE submissions SET status=?,awarded_points=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=?", (status,awarded if approved else None,submission_id))
        if approved and awarded:
            conn.execute("UPDATE students SET points=points+? WHERE user_id=?", (awarded,row["user_id"]))
        accepted_count=conn.execute("SELECT COUNT(*) FROM submissions WHERE user_id=? AND status='approved'",(row["user_id"],)).fetchone()[0]
        claimed_count=conn.execute("SELECT COUNT(*) FROM achievement_claims WHERE user_id=?",(row["user_id"],)).fetchone()[0]
        custom_titles=[r["title"] for r in conn.execute("SELECT a.title FROM achievement_claims ac JOIN achievements a ON a.id=ac.achievement_id WHERE ac.user_id=?",(row["user_id"],))] if approved else []
    if approved:
        previous_level=level_for_points(row["current_points"] or 0)["level"]
        progress = level_for_points((row["current_points"] or 0) + awarded)
        summary=achievement_summary((row["current_points"] or 0)+awarded,accepted_count,custom_titles)
        result = f"Задание «{row['title']}» принято! Начислено {awarded} из {row['points']} баллов. ⭐\n✨ Уровень {progress['level']} · {progress['progress_xp']}/{progress['level_xp']} XP до следующего уровня.\n\nПрогресс целей достижений:\n{achievement_progress_text((row['current_points'] or 0)+awarded,summary)}"
        if accepted_count in TASK_ACHIEVEMENT_THRESHOLDS:
            result += f"\n🏆 Выполнено заданий: {accepted_count}!"
        if progress["level"] > previous_level:
            result += "\n"+"\n".join(f"🏆 Достижение за уровень {level}!" for level in range(((previous_level//5)+1)*5,progress["level"]+1,5))
        newly_count=[n for n in META_ACHIEVEMENT_THRESHOLDS if summary["base_achievement_count"]==n]
        if newly_count:
            result += "\n"+"\n".join(f"🏆 Коллекционер: {n} достижений!" for n in newly_count)
    else:
        result = f"Задание «{row['title']}» пока не принято. Уточните у учителя и попробуйте ещё раз."
    if approved:
        bonus=award_threshold_points(row["user_id"],accepted_count,claimed_count)
        if bonus: result+=f"\nБонус за пороги достижений: {bonus} баллов."
    max_send(row["user_id"], result)
    return True


def handle_update(update):
    if update.get("update_type") == "message_callback":
        handle_callback(update)
        return
    if update.get("update_type") != "message_created":
        return
    message = update.get("message") or {}
    sender = message.get("sender") or {}
    user_id = sender.get("user_id")
    if user_id is None or sender.get("is_bot"):
        return
    body = message.get("body") or {}
    process_bot_text(user_id, sender.get("first_name", "Ученик"), body.get("text", ""))


def load_marker():
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='max_update_marker'").fetchone()
        return row["value"] if row else None


def save_marker(marker):
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES('max_update_marker',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(marker),))


def poll_max():
    if not MAX_TOKEN:
        log.warning("MAX_BOT_TOKEN is empty; MAX polling is disabled")
        return
    marker = load_marker()
    consecutive_errors = 0
    while True:
        try:
            params = {"timeout": 30, "limit": 100}
            if marker is not None:
                params["marker"] = marker
            response = requests.get(f"{MAX_API}/updates", params=params, headers={"Authorization": MAX_TOKEN}, timeout=40)
            response.raise_for_status()
            result = response.json()
            for update in result.get("updates", []):
                try:
                    handle_update(update)
                except Exception:
                    log.exception("Failed to process MAX update")
            next_marker = result.get("marker")
            if next_marker is not None:
                marker = next_marker
                save_marker(marker)
            consecutive_errors = 0
        except requests.RequestException as exc:
            consecutive_errors += 1
            delay = min(5 * (2 ** min(consecutive_errors, 6)), 300)
            log.warning("MAX polling failed (%s); retrying in %ss", type(exc).__name__, delay)
            time.sleep(delay)
        except Exception as exc:
            consecutive_errors += 1
            delay = min(5 * (2 ** min(consecutive_errors, 6)), 300)
            log.warning("Unexpected MAX polling error (%s); retrying in %ss", type(exc).__name__, delay)
            time.sleep(delay)





















def main():
    init_db()
    if MAX_TOKEN:
        poll_max()
    else:
        log.warning("MAX_BOT_TOKEN is empty; MAX polling is disabled")
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
