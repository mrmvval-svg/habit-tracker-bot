import os
import asyncio
from datetime import datetime, timedelta, date, time as dtime
from typing import Optional

import pytz
import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext


BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "habits.db")
TZ_DEFAULT = "Europe/Kyiv"

scheduler = AsyncIOScheduler()
dp = Dispatcher(storage=MemoryStorage())


# -------------------- Helpers --------------------
def utc_now() -> datetime:
    return datetime.utcnow().replace(tzinfo=pytz.UTC)

def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    return dt.astimezone(pytz.UTC).isoformat()

def parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    h = int(h); m = int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("bad time")
    return dtime(h, m)

def localize(tz: pytz.BaseTzInfo, d: date, t: dtime) -> datetime:
    return tz.localize(datetime.combine(d, t))

def in_quiet(local_t: dtime, q_from: str, q_to: str) -> bool:
    qf, qt = parse_hhmm(q_from), parse_hhmm(q_to)
    if qf <= qt:
        return qf <= local_t < qt
    return local_t >= qf or local_t < qt

def next_quiet_end(tz: pytz.BaseTzInfo, now_local: datetime, quiet_to: str) -> datetime:
    qt = parse_hhmm(quiet_to)
    cand = tz.localize(datetime.combine(now_local.date(), qt))
    if cand <= now_local:
        cand = tz.localize(datetime.combine(now_local.date() + timedelta(days=1), qt))
    return cand


# -------------------- DB (with simple migrations) --------------------
async def _ensure_column(db: aiosqlite.Connection, table: str, col: str, ddl: str):
    cur = await db.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in await cur.fetchall()]
    if col not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY,
          timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv',
          quiet_from TEXT NOT NULL DEFAULT '23:00',
          quiet_to   TEXT NOT NULL DEFAULT '08:00',
          digest_time TEXT NOT NULL DEFAULT '09:00',
          mute_until_utc TEXT,
          last_digest_date TEXT
        );

        CREATE TABLE IF NOT EXISTS habits(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          title TEXT NOT NULL,
          start_date TEXT NOT NULL,
          end_date TEXT,
          time_of_day TEXT NOT NULL,
          days_of_week TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at_utc TEXT NOT NULL,
          FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS habit_logs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          habit_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL,
          log_date TEXT NOT NULL,
          status TEXT NOT NULL, -- done/skipped
          done_at_utc TEXT,
          UNIQUE(habit_id, log_date),
          FOREIGN KEY(habit_id) REFERENCES habits(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reminders(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          habit_id INTEGER NOT NULL,
          fire_at_utc TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'scheduled', -- scheduled/sent/canceled
          snoozed_until_utc TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(user_id, status, fire_at_utc);
        """)

        await _ensure_column(db, "users", "mute_until_utc", "mute_until_utc TEXT")
        await _ensure_column(db, "users", "last_digest_date", "last_digest_date TEXT")
        await db.commit()


async def ensure_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT timezone, quiet_from, quiet_to, digest_time, mute_until_utc, last_digest_date
            FROM users WHERE user_id=?
        """, (user_id,))
        row = await cur.fetchone()
        if not row:
            return (TZ_DEFAULT, "23:00", "08:00", "09:00", None, None)
        return row

async def set_digest_time(user_id: int, hhmm: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET digest_time=? WHERE user_id=?", (hhmm, user_id))
        await db.commit()

async def set_quiet_hours(user_id: int, quiet_from: str, quiet_to: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET quiet_from=?, quiet_to=? WHERE user_id=?",
                         (quiet_from, quiet_to, user_id))
        await db.commit()

async def set_mute(user_id: int, mute_until_utc: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET mute_until_utc=? WHERE user_id=?", (mute_until_utc, user_id))
        await db.commit()

async def set_last_digest(user_id: int, d_iso: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_digest_date=? WHERE user_id=?", (d_iso, user_id))
        await db.commit()

async def all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

async def add_habit(user_id: int, title: str, start: date, end: Optional[date], time_of_day: str, dow: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO habits(user_id,title,start_date,end_date,time_of_day,days_of_week,is_active,created_at_utc)
            VALUES(?,?,?,?,?,?,1,?)
        """, (user_id, title, start.isoformat(), end.isoformat() if end else None,
              time_of_day, dow, to_utc_iso(utc_now())))
        await db.commit()
        return cur.lastrowid

async def get_habit(user_id: int, habit_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,title,start_date,end_date,time_of_day,days_of_week,is_active
            FROM habits WHERE user_id=? AND id=?
        """, (user_id, habit_id))
        return await cur.fetchone()

async def list_habits(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,title,start_date,end_date,time_of_day,days_of_week,is_active
            FROM habits WHERE user_id=? ORDER BY id DESC
        """, (user_id,))
        return await cur.fetchall()

async def log_habit(user_id: int, habit_id: int, d_iso: str, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO habit_logs(habit_id,user_id,log_date,status,done_at_utc)
            VALUES(?,?,?,?,?)
            ON CONFLICT(habit_id, log_date) DO UPDATE SET
              status=excluded.status,
              done_at_utc=excluded.done_at_utc
        """, (habit_id, user_id, d_iso, status, to_utc_iso(utc_now()) if status == "done" else None))
        await db.commit()

async def streak_habit(user_id: int, habit_id: int, today_iso: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT log_date FROM habit_logs
            WHERE user_id=? AND habit_id=? AND status='done'
            ORDER BY log_date DESC
        """, (user_id, habit_id))
        rows = await cur.fetchall()

    done = set(r[0] for r in rows)
    d = date.fromisoformat(today_iso)
    streak = 0
    while d.isoformat() in done:
        streak += 1
        d -= timedelta(days=1)
    return streak

async def add_reminder(user_id: int, habit_id: int, fire_at_utc: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO reminders(user_id,habit_id,fire_at_utc,status)
            VALUES(?,?,?,'scheduled')
        """, (user_id, habit_id, fire_at_utc))
        await db.commit()

async def snooze_reminder(rem_id: int, new_utc_iso: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE reminders SET snoozed_until_utc=?, status='scheduled'
            WHERE rowid=?
        """, (new_utc_iso, rem_id))
        await db.commit()

async def cancel_habit_reminders(user_id: int, habit_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE reminders SET status='canceled'
            WHERE user_id=? AND habit_id=? AND status='scheduled'
        """, (user_id, habit_id))
        await db.commit()

async def mark_rem_sent(rem_rowid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET status='sent' WHERE rowid=?", (rem_rowid,))
        await db.commit()

async def get_due_reminders(limit: int = 50):
    now = to_utc_iso(utc_now())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT rowid, user_id, habit_id, fire_at_utc, snoozed_until_utc
            FROM reminders
            WHERE status='scheduled'
              AND COALESCE(snoozed_until_utc, fire_at_utc) <= ?
            ORDER BY COALESCE(snoozed_until_utc, fire_at_utc) ASC
            LIMIT ?
        """, (now, limit))
        return await cur.fetchall()


# -------------------- Keyboards --------------------
def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить привычку", callback_data="menu:add")
    kb.button(text="🔁 Список привычек", callback_data="menu:list")
    kb.button(text="📅 Сегодня", callback_data="menu:today")
    kb.button(text="🏋 Тренировка 60 минут", callback_data="menu:mute60")
    kb.button(text="⚙ Настройки", callback_data="menu:settings")
    kb.adjust(2, 1, 1, 1)
    return kb.as_markup()

def habit_rem_kb(habit_id: int, rem_rowid: int, d_iso: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выполнено", callback_data=f"habit_done:{habit_id}:{rem_rowid}:{d_iso}")
    kb.button(text="❌ Пропуск", callback_data=f"habit_skip:{habit_id}:{rem_rowid}:{d_iso}")
    kb.button(text="⏭ +10 мин", callback_data=f"snooze:{rem_rowid}:10")
    kb.button(text="⏭ +30 мин", callback_data=f"snooze:{rem_rowid}:30")
    kb.button(text="⏭ +60 мин", callback_data=f"snooze:{rem_rowid}:60")
    kb.adjust(2, 3)
    return kb.as_markup()

def settings_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="☀ Время утреннего плана", callback_data="settings:digest")
    kb.button(text="🌙 Тихие часы", callback_data="settings:quiet")
    kb.button(text="⬅ Назад", callback_data="menu:back")
    kb.adjust(1, 1, 1)
    return kb.as_markup()


# -------------------- FSM --------------------
class AddHabit(StatesGroup):
    title = State()
    start = State()
    end = State()
    time_of_day = State()
    dow = State()

class SettingsFSM(StatesGroup):
    digest_time = State()
    quiet_from = State()
    quiet_to = State()


# -------------------- Scheduling logic --------------------
async def schedule_habit_today(user_id: int, habit_id: int):
    tzname, *_ = await get_user(user_id)
    tz = pytz.timezone(tzname)
    h = await get_habit(user_id, habit_id)
    if not h:
        return
    hid, title, start_iso, end_iso, time_of_day, dow, active = h
    if not active:
        return

    today = datetime.now(tz).date()
    if today < date.fromisoformat(start_iso):
        return
    if end_iso and today > date.fromisoformat(end_iso):
        return

    days = set(int(x) for x in dow.split(",") if x.strip() != "")
    if today.weekday() not in days:
        return

    fire_local = localize(tz, today, parse_hhmm(time_of_day))
    if fire_local <= datetime.now(tz):
        return

    await add_reminder(user_id, habit_id, to_utc_iso(fire_local.astimezone(pytz.UTC)))

async def daily_refresh():
    # create today's reminders for all users/habits
    for uid in await all_user_ids():
        for h in await list_habits(uid):
            hid = h[0]
            if h[6] == 1:
                await schedule_habit_today(uid, hid)

async def send_digest(bot: Bot, user_id: int):
    tzname, *_ = await get_user(user_id)
    tz = pytz.timezone(tzname)
    today = datetime.now(tz).date()
    today_iso = today.isoformat()

    habits = await list_habits(user_id)

    lines = ["☀ Доброе утро!", "", f"📅 Сегодня: {today_iso}", ""]
    planned = []
    for (hid, title, start_iso, end_iso, tm, dow, active) in habits:
        if not active:
            continue
        if today < date.fromisoformat(start_iso):
            continue
        if end_iso and today > date.fromisoformat(end_iso):
            continue
        days = set(int(x) for x in dow.split(",") if x.strip() != "")
        if today.weekday() in days:
            planned.append((title, tm))
    if planned:
        lines.append("🔁 Привычки по плану:")
        for t, tm in planned[:15]:
            lines.append(f"• {t} — {tm}")
    else:
        lines.append("🔁 Привычек по плану нет.")

    await bot.send_message(user_id, "\n".join(lines), reply_markup=main_menu_kb())

async def due_worker(bot: Bot):
    due = await get_due_reminders(limit=50)
    for (rem_rowid, user_id, habit_id, fire_at, snoozed_until) in due:
        tzname, qf, qt, digest_time, mute_until, last_digest = await get_user(user_id)
        tz = pytz.timezone(tzname)

        # mute?
        if mute_until:
            mu = datetime.fromisoformat(mute_until)
            if mu.tzinfo is None:
                mu = pytz.UTC.localize(mu)
            if utc_now() < mu:
                await snooze_reminder(rem_rowid, to_utc_iso(mu))
                continue

        now_local = datetime.now(tz)
        if in_quiet(now_local.time(), qf, qt):
            new_local = next_quiet_end(tz, now_local, qt)
            await snooze_reminder(rem_rowid, to_utc_iso(new_local.astimezone(pytz.UTC)))
            continue

        h = await get_habit(user_id, habit_id)
        if not h or h[6] != 1:
            await mark_rem_sent(rem_rowid)
            continue

        hid, title, *_ = h
        today_iso = datetime.now(tz).date().isoformat()

        await bot.send_message(
            user_id,
            f"🔁 Привычка: {title}\n📅 Сегодня: {today_iso}\n\nСделано?",
            reply_markup=habit_rem_kb(hid, rem_rowid, today_iso)
        )
        await mark_rem_sent(rem_rowid)

async def digest_worker(bot: Bot):
    user_ids = await all_user_ids()
    for uid in user_ids:
        tzname, qf, qt, digest_time, mute_until, last_digest = await get_user(uid)
        tz = pytz.timezone(tzname)
        now = datetime.now(tz)
        today_iso = now.date().isoformat()

        if last_digest == today_iso:
            continue
        if in_quiet(now.time(), qf, qt):
            continue
        if mute_until:
            mu = datetime.fromisoformat(mute_until)
            if mu.tzinfo is None:
                mu = pytz.UTC.localize(mu)
            if utc_now() < mu:
                continue

        dt = parse_hhmm(digest_time)
        if now.hour == dt.hour and abs(now.minute - dt.minute) <= 1:
            await send_digest(bot, uid)
            await set_last_digest(uid, today_iso)


# -------------------- Handlers --------------------
@dp.message(Command("start"))
async def start(m: Message):
    await ensure_user(m.from_user.id)
    await m.answer("Привет! Я трекер привычек.\nУправление кнопками 👇", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "menu:back")
async def back_menu(c: CallbackQuery):
    await c.message.edit_text("Меню 👇", reply_markup=main_menu_kb())
    await c.answer()

@dp.callback_query(F.data == "menu:add")
async def add_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddHabit.title)
    await c.message.edit_text("Напиши название привычки одним сообщением:")
    await c.answer()

@dp.callback_query(F.data == "menu:list")
async def menu_list(c: CallbackQuery):
    habits = await list_habits(c.from_user.id)
    if not habits:
        text = "🔁 Привычек пока нет."
    else:
        lines = ["🔁 Привычки:"]
        for (hid, title, start_iso, end_iso, tm, dow, active) in habits[:30]:
            status = "✅" if active else "⛔"
            lines.append(f"{status} #{hid} {title} — {tm} (c {start_iso}" + (f" до {end_iso})" if end_iso else ")"))
        text = "\n".join(lines)
    await c.message.edit_text(text, reply_markup=main_menu_kb())
    await c.answer()

@dp.callback_query(F.data == "menu:today")
async def menu_today(c: CallbackQuery):
    tzname, *_ = await get_user(c.from_user.id)
    tz = pytz.timezone(tzname)
    today = datetime.now(tz).date()
    today_iso = today.isoformat()

    habits = await list_habits(c.from_user.id)
    lines = [f"📅 Сегодня: {today_iso}", ""]
    planned = []
    for (hid, title, start_iso, end_iso, tm, dow, active) in habits:
        if not active:
            continue
        if today < date.fromisoformat(start_iso):
            continue
        if end_iso and today > date.fromisoformat(end_iso):
            continue
        days = set(int(x) for x in dow.split(",") if x.strip() != "")
        if today.weekday() in days:
            planned.append((title, tm))
    if planned:
        lines.append("🔁 Привычки по плану:")
        for t, tm in planned[:20]:
            lines.append(f"• {t} — {tm}")
    else:
        lines.append("🔁 Привычек по плану нет.")
    await c.message.edit_text("\n".join(lines), reply_markup=main_menu_kb())
    await c.answer()

@dp.callback_query(F.data == "menu:mute60")
async def menu_mute60(c: CallbackQuery):
    mu = utc_now() + timedelta(minutes=60)
    await set_mute(c.from_user.id, to_utc_iso(mu))
    await c.answer("Ок, 60 минут не беспокою 👍", show_alert=True)

@dp.callback_query(F.data == "menu:settings")
async def menu_settings(c: CallbackQuery):
    tzname, qf, qt, digest_time, mute_until, last_digest = await get_user(c.from_user.id)
    text = (
        "⚙ Настройки:\n\n"
        f"☀ Утренний план: {digest_time}\n"
        f"🌙 Тихие часы: {qf}–{qt}\n"
    )
    await c.message.edit_text(text, reply_markup=settings_kb())
    await c.answer()


# ---- Settings flow ----
@dp.callback_query(F.data == "settings:digest")
async def set_digest_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(SettingsFSM.digest_time)
    await c.message.edit_text("Введи время утреннего плана ЧЧ:ММ (например 09:00):")
    await c.answer()

@dp.message(SettingsFSM.digest_time)
async def set_digest_done(m: Message, state: FSMContext):
    hhmm = m.text.strip()
    parse_hhmm(hhmm)
    await set_digest_time(m.from_user.id, hhmm)
    await state.clear()
    await m.answer(f"✅ Готово! Утренний план будет приходить в {hhmm}.", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "settings:quiet")
async def set_quiet_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(SettingsFSM.quiet_from)
    await c.message.edit_text("Введи начало тихих часов ЧЧ:ММ (например 23:00):")
    await c.answer()

@dp.message(SettingsFSM.quiet_from)
async def set_quiet_from(m: Message, state: FSMContext):
    qf = m.text.strip()
    parse_hhmm(qf)
    await state.update_data(qf=qf)
    await state.set_state(SettingsFSM.quiet_to)
    await m.answer("Введи конец тихих часов ЧЧ:ММ (например 08:00):")

@dp.message(SettingsFSM.quiet_to)
async def set_quiet_to(m: Message, state: FSMContext):
    qt = m.text.strip()
    parse_hhmm(qt)
    data = await state.get_data()
    qf = data["qf"]
    await set_quiet_hours(m.from_user.id, qf, qt)
    await state.clear()
    await m.answer(f"✅ Готово! Тихие часы: {qf}–{qt}", reply_markup=main_menu_kb())


# ---- Add Habit wizard ----
@dp.message(AddHabit.title)
async def habit_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AddHabit.start)
    kb = InlineKeyboardBuilder()
    kb.button(text="Сегодня", callback_data="habit_start:today")
    kb.adjust(1)
    await m.answer("Дата начала:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("habit_start:"), AddHabit.start)
async def habit_start(c: CallbackQuery, state: FSMContext):
    tzname, *_ = await get_user(c.from_user.id)
    tz = pytz.timezone(tzname)
    d = datetime.now(tz).date()
    await state.update_data(start=d.isoformat())
    await state.set_state(AddHabit.end)
    await c.message.edit_text("Дата конца? Напиши:\n• `нет` — если без конца\n• или дату YYYY-MM-DD (например 2026-03-30)")
    await c.answer()

@dp.message(AddHabit.end)
async def habit_end(m: Message, state: FSMContext):
    txt = m.text.strip().lower()
    end = None if txt == "нет" else date.fromisoformat(m.text.strip())
    await state.update_data(end=end.isoformat() if end else None)
    await state.set_state(AddHabit.time_of_day)
    await m.answer("Введи время напоминания ЧЧ:ММ (например 20:00):")

@dp.message(AddHabit.time_of_day)
async def habit_time(m: Message, state: FSMContext):
    tm = m.text.strip()
    parse_hhmm(tm)
    await state.update_data(time_of_day=tm)
    await state.set_state(AddHabit.dow)
    await m.answer("Дни недели:\n• `каждый день`\n• или числа через запятую (0=Пн ... 6=Вс), например: `0,2,4`")

@dp.message(AddHabit.dow)
async def habit_dow(m: Message, state: FSMContext):
    txt = m.text.strip().lower()
    if txt == "каждый день":
        dow = "0,1,2,3,4,5,6"
    else:
        parts = [p.strip() for p in txt.split(",")]
        _ = [int(p) for p in parts]  # validate
        dow = ",".join(parts)

    data = await state.get_data()
    title = data["title"]
    start = date.fromisoformat(data["start"])
    end_iso = data.get("end")
    end = date.fromisoformat(end_iso) if end_iso else None
    time_of_day = data["time_of_day"]

    hid = await add_habit(m.from_user.id, title, start, end, time_of_day, dow)
    await schedule_habit_today(m.from_user.id, hid)
    await state.clear()

    await m.answer(f"✅ Привычка добавлена: {title} — {time_of_day}", reply_markup=main_menu_kb())


# ---- Reminder actions ----
@dp.callback_query(F.data.startswith("snooze:"))
async def cb_snooze(c: CallbackQuery):
    _, rid_s, minutes_s = c.data.split(":")
    rid = int(rid_s)
    minutes = int(minutes_s)
    new_time = utc_now() + timedelta(minutes=minutes)
    await snooze_reminder(rid, to_utc_iso(new_time))
    await c.answer(f"Ок, перенёс на +{minutes} мин 👍", show_alert=True)

@dp.callback_query(F.data.startswith("habit_done:"))
async def cb_habit_done(c: CallbackQuery):
    _, hid_s, rem_rowid_s, d_iso = c.data.split(":")
    hid = int(hid_s)
    rem_rowid = int(rem_rowid_s)
    await log_habit(c.from_user.id, hid, d_iso, "done")
    await cancel_habit_reminders(c.from_user.id, hid)
    st = await streak_habit(c.from_user.id, hid, d_iso)
    await c.answer(f"✅ Супер! Серия: {st} 🔥", show_alert=True)

@dp.callback_query(F.data.startswith("habit_skip:"))
async def cb_habit_skip(c: CallbackQuery):
    _, hid_s, rem_rowid_s, d_iso = c.data.split(":")
    hid = int(hid_s)
    await log_habit(c.from_user.id, hid, d_iso, "skipped")
    await cancel_habit_reminders(c.from_user.id, hid)
    await c.answer("Ок, пропуск отмечен", show_alert=True)


# -------------------- Background jobs --------------------
async def job_tick(bot: Bot):
    await due_worker(bot)
    await digest_worker(bot)

async def job_daily():
    await daily_refresh()


# -------------------- Main --------------------
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var is required")

    await db_init()
    bot = Bot(BOT_TOKEN)

    scheduler.start()
    # every 20s: send due reminders + digest
    scheduler.add_job(job_tick, "interval", seconds=20, args=[bot], id="tick", replace_existing=True)

    # daily refresh at 00:10 Kyiv time
    kyiv = pytz.timezone(TZ_DEFAULT)
    scheduler.add_job(job_daily, "cron", hour=0, minute=10, timezone=kyiv, id="daily", replace_existing=True)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
