import asyncio, base64, gzip, hashlib, hmac, html, json, logging, math, os, re, sqlite3, time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, Message, CallbackQuery, PreCheckoutQuery, LabeledPrice,
                           InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo)
 
# ================= НАСТРОЙКИ =================
BOT_TOKEN    = "8893630888:AAGKijLThtwB0K5EVvELPSzQAxgtsks4JUs"
PUBLIC_URL   = os.getenv("PUBLIC_URL", "")    # Автоматически подхватит адрес от Bothost (или укажите вручную)
PORT         = int(os.environ.get("PORT", 8080)) # Порт для Bothost / сервера
WEBAPP_URL   = ""                                  # Пусто = Mini App раздает сам бот (исправляет проблему с профилем и оплатой)
ADMIN_IDS    = [123456789]                         # Твой Telegram ID
SUPPORT_URL  = "https://t.me/neleron"
CRYPTO_TOKEN = "658058:AAJ5O3dJUQDUZNWklPGo4TnDzCaJnBMWALa"  # Токен Crypto Pay из @CryptoBot (исправлена опечатка)
RUB_PER_STAR = 1.51          # курс для оплаты товаров звёздами
REF_PERCENT  = 5             # % с покупок рефералов на баланс пригласившего
# --- автопополнение UC/Plus у поставщика ---
SUPPLIER_API = ""            # адрес API поставщика
SUPPLIER_KEY = ""            # ключ API поставщика
CARD_DETAILS = ""            # реквизиты для оплаты переводом (карта/СБП)
SUPPLIER_MAP = {}            # наш товар -> товар поставщика
# =============================================
 
VERSION = "6.2"
HERE = Path(__file__).resolve().parent

# Жестко указываем текущую папку, где лежит uc_bot.py и index.html
WEBDIR = HERE
if not (WEBDIR / "index.html").exists() and (Path.cwd() / "index.html").exists():
    WEBDIR = Path.cwd()

PHOTO = {}
API_URL = PUBLIC_URL.rstrip("/")
WEBAPP = (WEBAPP_URL or PUBLIC_URL).rstrip("/") + "/"

# Автоматический поиск папки с файлами Mini App (index.html, banner.jpg, catalog.json)
WEBDIR = next((d for d in (HERE, HERE / "webapp", Path.cwd(), Path.cwd() / "webapp") if (d / "index.html").exists()), HERE)
PHOTO = {}
API_URL = PUBLIC_URL.rstrip("/")
WEBAPP = (WEBAPP_URL or PUBLIC_URL).rstrip("/") + "/"
BOT_USERNAME = ""
logging.basicConfig(level=logging.INFO)
router, admin = Router(), Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))
 
# ---------- БАЗА ----------
db = sqlite3.connect("kaban.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, name TEXT, reg TEXT, discount INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, percent INTEGER, uses INTEGER);
CREATE TABLE IF NOT EXISTS promo_used(uid INTEGER, code TEXT);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, sec TEXT, item TEXT, title TEXT,
  target TEXT, rub INTEGER, disc INTEGER, method TEXT, inv TEXT, status TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS codes(id INTEGER PRIMARY KEY AUTOINCREMENT, item TEXT, code TEXT, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, name TEXT, rating INTEGER, text TEXT, ts TEXT);
""")
for t, c in [("users", "ref_by INTEGER"), ("users", "balance INTEGER DEFAULT 0"),
             ("users", "ref_earned INTEGER DEFAULT 0"), ("orders", "result TEXT")]:
    try: db.execute(f"ALTER TABLE {t} ADD COLUMN {c}")
    except sqlite3.OperationalError: pass
db.commit() 
def q(sql, args=(), one=False):
    cur = db.execute(sql, args)
    r = cur.fetchone() if one else cur.fetchall()
    db.commit()
    return r
 
def ensure_user(uid, username, name, ref=None):
    if ref and (ref == uid or not q("SELECT 1 FROM users WHERE id=?", (ref,), True)): ref = None
    cur = db.execute("INSERT OR IGNORE INTO users(id,username,name,reg,ref_by) VALUES(?,?,?,?,?)",
                     (uid, username, name, datetime.now().strftime("%d.%m.%Y %H:%M"), ref))
    new = cur.rowcount == 1
    db.execute("UPDATE users SET username=?, name=? WHERE id=?", (username, name, uid)); db.commit()
    return new
 
def eu(u, ref=None): return ensure_user(u.id, u.username, u.full_name, ref)
def stars(rub): return max(1, math.ceil(rub / RUB_PER_STAR))
 
# ---------- КАТАЛОГ (webapp/catalog.json) ----------
def catalog():
    try: return json.loads((WEBDIR / "catalog.json").read_text(encoding="utf-8"))
    except Exception: return json.loads(DEFAULT_CATALOG)
 
def banner_bytes():
    for f in (WEBDIR / "banner.jpg", HERE / "banner.jpg"):
        if f.exists(): return f.read_bytes()
    return base64.b64decode(BANNER_B64)
 
def find(cat, iid):
    for sec, items in cat.items():
        for it in items:
            if it["id"] == iid: return sec, it
    return None, None
 
# ---------- ЗАКАЗ (общая логика для бота и мини-аппа) ----------
async def make_order(uid, uname, iid, target):
    sec, it = find(catalog(), iid)
    if not it: raise ValueError("Товар не найден")
    if sec in ("uc", "plus"):
        if not re.fullmatch(r"\d{8,12}", target): raise ValueError("Неверный PUBG ID")
        label = f"PUBG ID: <code>{target}</code>"
    elif sec in ("stars", "premium"):
        if target == "me": target = f"@{uname}" if uname else f"id{uid}"
        elif re.fullmatch(r"[A-Za-z0-9_]{4,32}", target): target = "@" + target
        else: raise ValueError("Неверный username")
        label = f"Получатель: {html.escape(target)}"
    else:
        target, label = "-", "Код придёт сюда после оплаты"
    disc = q("SELECT discount FROM users WHERE id=?", (uid,), True)["discount"]
    rub = max(1, round(it["rub"] * (100 - disc) / 100))
    cur = db.execute("INSERT INTO orders(uid,sec,item,title,target,rub,disc,status,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                     (uid, sec, iid, it["title"], target, rub, disc, "new", datetime.now().strftime("%d.%m %H:%M")))
    db.commit()
    return dict(id=cur.lastrowid, sec=sec, title=it["title"], target=target, rub=rub, disc=disc, label=label)
 
async def crypto(method, **params):
    async with aiohttp.ClientSession(headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN}) as s:
        async with s.post("https://pay.crypt.bot/api/" + method, json=params) as r:
            return await r.json()
 
async def create_crypto(oid, rub):
    if not CRYPTO_TOKEN: return None
    try:
        j = await crypto("createInvoice", currency_type="fiat", fiat="RUB", amount=str(rub),
                         description=f"Заказ #{oid}", payload=str(oid), expires_in=3600)
    except Exception: return None
    if not j.get("ok"): return None
    inv = j["result"]
    q("UPDATE orders SET method='crypto', inv=?, status='wait' WHERE id=?", (str(inv["invoice_id"]), oid))
    return inv["bot_invoice_url"]
 
async def pay_balance(bot, oid, uid):
    o = q("SELECT * FROM orders WHERE id=?", (oid,), True)
    if not o or o["uid"] != uid or o["status"] != "new": return False
    cur = db.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (o["rub"], uid, o["rub"])); db.commit()
    if cur.rowcount == 0: return False
    q("UPDATE orders SET method='balance' WHERE id=?", (oid,))
    await finish(bot, oid)
    return True
 
async def check_crypto(bot, oid):
    o = q("SELECT * FROM orders WHERE id=?", (oid,), True)
    if not o or o["status"] != "wait": return o and o["status"]
    j = await crypto("getInvoices", invoice_ids=o["inv"])
    items = (j.get("result") or {}).get("items") or []
    st = items[0]["status"] if items else "active"
    if st == "paid": await finish(bot, oid)
    elif st == "expired": q("UPDATE orders SET status='cancel' WHERE id=?", (oid,))
    return st
 
async def crypto_loop(bot):
    while True:
        await asyncio.sleep(20)
        if not CRYPTO_TOKEN: continue
        for o in q("SELECT id FROM orders WHERE status='wait'"):
            try: await check_crypto(bot, o["id"])
            except Exception as e: logging.warning("crypto check: %s", e)
 
async def supplier_topup(o):
    """Автопополнение у поставщика. ЗАГОТОВКА: формат запроса зависит от API конкретного поставщика —
    пришли мне его документацию, и я впишу точный запрос. Верни (True, "ок") при успехе или (False, "причина")."""
    if o["item"] not in SUPPLIER_MAP: return None          # None = для этого товара выдача вручную
    async with aiohttp.ClientSession() as ses:
        async with ses.post(SUPPLIER_API, json={"key": SUPPLIER_KEY, "product": SUPPLIER_MAP[o["item"]],
                            "player_id": o["target"], "order_id": o["id"]}, timeout=aiohttp.ClientTimeout(total=30)) as r:
            j = await r.json(content_type=None)
    return bool(j.get("success") or j.get("ok")), str(j)[:200]
 
async def notify_admins(bot, text, kb=None):
    for a in ADMIN_IDS:
        try: await bot.send_message(a, text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logging.error("НЕ МОГУ НАПИСАТЬ АДМИНУ %s: %s — проверь ADMIN_IDS и нажми /start у бота с этого аккаунта", a, e)
 
def order_info(o):
    u = q("SELECT * FROM users WHERE id=?", (o["uid"],), True)
    nick = f"@{u['username']}" if u["username"] else "без юзернейма"
    return (f"🧾 <b>Заказ #{o['id']}</b> ({o['method']})\n"
            f"👤 <a href=\"tg://user?id={u['id']}\">{html.escape(u['name'])}</a> {html.escape(nick)} · <code>{u['id']}</code>\n"
            f"📦 {html.escape(o['title'])}\n🎯 <code>{html.escape(o['target'])}</code>\n💰 {o['rub']} ₽")
 
async def finish(bot: Bot, oid: int):
    cur = db.execute("UPDATE orders SET status='paid' WHERE id=? AND status IN ('new','wait','review')", (oid,)); db.commit()
    if cur.rowcount == 0: return
    o = q("SELECT * FROM orders WHERE id=?", (oid,), True)
    u = q("SELECT * FROM users WHERE id=?", (o["uid"],), True)
    if o["disc"]: q("UPDATE users SET discount=0 WHERE id=?", (o["uid"],))
    if u["ref_by"] and o["method"] != "balance":          # реферальный бонус
        bonus = round(o["rub"] * REF_PERCENT / 100)
        if bonus > 0:
            q("UPDATE users SET balance=balance+?, ref_earned=ref_earned+? WHERE id=?", (bonus, bonus, u["ref_by"]))
            try: await bot.send_message(u["ref_by"], f"👥 Твой реферал совершил покупку! +{bonus} ₽ на баланс.")
            except Exception: pass
    info = order_info(o)
    rv = InlineKeyboardMarkup(inline_keyboard=[[btn("⭐ Оставить отзыв", "rv_new")]])
    async def tell(text, **kw):
        try: await bot.send_message(o["uid"], text, **kw)
        except Exception as e: logging.warning("не смог написать клиенту %s: %s", o["uid"], e)
    if o["sec"] == "codes":
        c = q("SELECT id, code FROM codes WHERE item=? AND used=0 LIMIT 1", (o["item"],), True)
        if c:
            q("UPDATE codes SET used=1 WHERE id=?", (c["id"],))
            q("UPDATE orders SET status='done', result=? WHERE id=?", (c["code"], oid))
            await notify_admins(bot, "🤖 Код выдан автоматически\n" + info)
            return await tell(f"✅ Заказ #{oid} оплачен!\n\nТвой код:\n<code>{c['code']}</code>\n\nАктивируй его в игре / на сайте пополнения.",
                              parse_mode="HTML", reply_markup=rv)
        info += "\n⚠️ <b>КОДЫ ЗАКОНЧИЛИСЬ — выдай вручную!</b>"
    if o["sec"] in ("uc", "plus") and SUPPLIER_API:
        try: res = await supplier_topup(o)
        except Exception as e: res = (False, str(e))
        if res and res[0]:
            q("UPDATE orders SET status='done' WHERE id=?", (oid,))
            await notify_admins(bot, "🤖 Выдано автоматически\n" + info)
            return await tell(f"✅ Заказ #{oid} выполнен! {o['title']} зачислены на ID {o['target']}.", reply_markup=rv)
        info += f"\n⚠️ Автопополнение не удалось ({html.escape(str(res[1]) if res else 'нет данных')}) — выдай вручную!"
    await notify_admins(bot, "💰 <b>Новый оплаченный заказ</b>\n" + info + "\n\nПополни и нажми «Выдано» — клиент получит уведомление.",
        InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Выдано", f"done:{oid}", "success")]]))
    await tell(f"✅ Оплата получена! Заказ #{oid} в обработке — обычно до 15 минут. Мы напишем, когда всё будет готово.")
 
# ---------- ПРОМО / ОТЗЫВЫ ----------
def apply_promo(uid, code):
    code = code.strip().upper()
    p = q("SELECT * FROM promos WHERE code=?", (code,), True)
    if not p or p["uses"] <= 0: return False, "❌ Промокод не найден или закончился."
    if q("SELECT 1 FROM promo_used WHERE uid=? AND code=?", (uid, code), True): return False, "⚠️ Ты уже использовал этот промокод."
    q("INSERT INTO promo_used VALUES(?,?)", (uid, code))
    q("UPDATE promos SET uses=uses-1 WHERE code=?", (code,))
    q("UPDATE users SET discount=? WHERE id=?", (p["percent"], uid))
    return True, f"✅ Промокод активирован! Скидка {p['percent']}% на следующий заказ."
 
def has_bought(uid):
    return bool(q("SELECT 1 FROM orders WHERE uid=? AND status IN ('paid','done') LIMIT 1", (uid,), True))
 
def add_review(uid, name, rating, text):
    q("DELETE FROM reviews WHERE uid=?", (uid,))
    cur = db.execute("INSERT INTO reviews(uid,name,rating,text,ts) VALUES(?,?,?,?,?)",
                     (uid, name.split()[0] if name.split() else "Клиент", rating, text, datetime.now().strftime("%d.%m.%Y")))
    db.commit(); return cur.lastrowid
 
def reviews_data(n=20):
    st = q("SELECT COUNT(*) c, AVG(rating) a FROM reviews", one=True)
    rows = q("SELECT name,rating,text,ts FROM reviews ORDER BY id DESC LIMIT ?", (n,))
    return {"count": st["c"], "avg": round(st["a"] or 0, 1), "items": [dict(r) for r in rows]}
 
async def notify_review(bot, rid, name, rating, text):
    await notify_admins(bot, f"📝 Новый отзыв #{rid} от {html.escape(name)}: {'⭐' * rating}\n{html.escape(text)}",
        InlineKeyboardMarkup(inline_keyboard=[[btn("🗑 Удалить", f"rdel:{rid}", "danger")]]))
 
# ---------- КЛАВИАТУРЫ / ТЕКСТЫ ----------
EMO = {"wave": ("5409228133528252069", "👋"), "gem": ("5408896647952348680", "💎"), "id": ("5408909562919007848", "🆔"),
       "card": ("5409014699423446355", "💳"), "bolt": ("5409119256107297715", "⚡️")}
 
def welcome(name, premium=True):
    e = lambda k: f'<tg-emoji emoji-id="{EMO[k][0]}">{EMO[k][1]}</tg-emoji>' if premium else EMO[k][1]
    return (f"<b>{e('wave')} Добро пожаловать, {name}!</b>\n\n"
            f"<b>Kaban UC — быстрая и удобная покупка UC для PUBG Mobile.</b>\n\n"
            f"<blockquote><b>{e('gem')} Выберите нужный пакет\n{e('id')} Укажите PUBG ID\n"
            f"{e('card')} Оплатите заказ\n{e('bolt')} Получите UC прямо на свой аккаунт</b></blockquote>\n\n"
            f"<b>🔒 Пополнение происходит исключительно по игровому ID. Логин и пароль не требуются.</b>")
 
async def send_welcome(m: Message, bot: Bot):
    u = m.from_user
    name = f"@{u.username}" if u.username else html.escape(u.full_name)
    kb = await menu_kb(bot)
    for prem in (True, False):          # сначала с премиум-эмодзи, если Telegram не разрешит — с обычными
        try:
            msg = await m.answer_photo(PHOTO.get("id") or BufferedInputFile(banner_bytes(), filename="banner.jpg"),
                                       caption=welcome(name, prem), parse_mode="HTML", reply_markup=kb)
            PHOTO["id"] = msg.photo[-1].file_id
            return
        except Exception as e:
            logging.warning("welcome (premium=%s): %s", prem, e)
    await m.answer("👋 Добро пожаловать в KABAN UC!", reply_markup=kb)
 
def btn(text, data, style="primary"):
    return InlineKeyboardButton(text=text, callback_data=data, style=style)
 
async def menu_kb(bot):
    me = await bot.me()
    base = f"{WEBAPP}?bot={me.username}" + (f"&api={quote(API_URL, safe='')}" if API_URL else "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Пополнить UC", web_app=WebAppInfo(url=base + "&s=uc"), style="success")],
        [InlineKeyboardButton(text="⭐ Пополнить Telegram", web_app=WebAppInfo(url=base + "&s=tg"), style="primary")],
        [btn("👤 Профиль", "profile", "primary"), btn("👥 Рефералы", "ref", "success")],
        [btn("⭐ Отзывы", "reviews", "success"), btn("🎟 Промокод", "promo", "primary")],
        [InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL, style="primary")]])
 
def profile_text(uid, tgu=None):
    u = q("SELECT * FROM users WHERE id=?", (uid,), True)
    s = q("SELECT COUNT(*) n, COALESCE(SUM(rub),0) t FROM orders WHERE uid=? AND status IN ('paid','done')", (uid,), True)
    nick = f"@{u['username']}" if u["username"] else "нет"
    refs = q("SELECT COUNT(*) n FROM users WHERE ref_by=?", (uid,), True)["n"]
    extra = (f"\n🌐 Язык: {tgu.language_code or '—'}\n💎 Telegram Premium: {'да' if tgu.is_premium else 'нет'}") if tgu else ""
    return (f"👤 <b>Профиль</b>\n\nИмя: {html.escape(u['name'])}\nЮзернейм: {html.escape(nick)}\nID: <code>{uid}</code>\n"
            f"{extra}\n📅 Регистрация в боте: {u['reg']}\n🛒 Заказов: {s['n']} на {s['t']} ₽\n💳 Баланс: {u['balance']} ₽\n"
            f"👥 Приглашено: {refs} (заработано {u['ref_earned']} ₽)\n🎟 Скидка по промокоду: {u['discount']}%")
 
def ref_text(uid):
    u = q("SELECT * FROM users WHERE id=?", (uid,), True)
    n = q("SELECT COUNT(*) n FROM users WHERE ref_by=?", (uid,), True)["n"]
    return (f"👥 <b>Реферальная программа</b>\n\nПриглашай друзей и получай <b>{REF_PERCENT}%</b> с каждой их покупки на баланс.\n\n"
            f"🔗 Твоя ссылка:\n<code>https://t.me/{BOT_USERNAME}?start=ref_{uid}</code>\n\n"
            f"👤 Приглашено: {n}\n💰 Заработано: {u['ref_earned']} ₽\n💳 Баланс: {u['balance']} ₽\n\nБалансом можно оплачивать заказы.")
 
def reviews_text():
    d = reviews_data(5)
    if not d["count"]: return "⭐ <b>Отзывы</b>\n\nПока отзывов нет — стань первым!"
    lines = [f"{'⭐' * r['rating']} <b>{html.escape(r['name'])}</b> · {r['ts']}\n{html.escape(r['text'])}" for r in d["items"]]
    return f"⭐ <b>Отзывы</b> — {d['avg']} из 5 ({d['count']})\n\n" + "\n\n".join(lines)
 
# ---------- СТАРТ / ПРОФИЛЬ / РЕФЕРАЛЫ ----------
class U(StatesGroup):
    promo = State(); review = State()
 
@router.message(CommandStart())
async def start(m: Message, command: CommandObject, bot: Bot, state: FSMContext):
    await state.clear()
    arg = command.args or ""
    ref = int(arg[4:]) if arg.startswith("ref_") and arg[4:].isdigit() else None
    if eu(m.from_user, ref) and ref and q("SELECT ref_by FROM users WHERE id=?", (m.from_user.id,), True)["ref_by"]:
        try: await bot.send_message(ref, f"👥 По твоей ссылке зарегистрировался {html.escape(m.from_user.full_name)}!")
        except Exception: pass
    if arg == "profile": return await m.answer(profile_text(m.from_user.id, m.from_user), parse_mode="HTML")
    if arg == "refs": return await m.answer(ref_text(m.from_user.id), parse_mode="HTML")
    if arg == "reviews":
        return await m.answer(reviews_text(), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("✍️ Оставить отзыв", "rv_new", "success")]]))
    if arg == "promo":
        await state.set_state(U.promo)
        return await m.answer("🎟 Отправь промокод одним сообщением:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Отмена", "cancel_state", "danger")]]))
    if arg.startswith("b_"): return await begin_order(m, arg)
    await send_welcome(m, bot)
 
@router.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    eu(c.from_user); await c.message.answer(profile_text(c.from_user.id, c.from_user), parse_mode="HTML"); await c.answer()
 
@router.callback_query(F.data == "ref")
async def ref(c: CallbackQuery):
    eu(c.from_user); await c.message.answer(ref_text(c.from_user.id), parse_mode="HTML"); await c.answer()
 
# ---------- ПРОМОКОД ----------
@router.callback_query(F.data == "promo")
async def promo_ask(c: CallbackQuery, state: FSMContext):
    await state.set_state(U.promo)
    await c.message.answer("🎟 Отправь промокод одним сообщением:",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("✖️ Отмена", "cancel_state", "danger")]]))
    await c.answer()
 
@router.callback_query(F.data == "cancel_state")
async def cancel_state(c: CallbackQuery, state: FSMContext):
    await state.clear(); await c.message.delete(); await c.answer("Отменено")
 
@router.message(U.promo, F.text)
async def promo_apply(m: Message, state: FSMContext):
    eu(m.from_user)
    ok, msg = apply_promo(m.from_user.id, m.text)
    if ok: await state.clear()
    await m.answer(msg)
 
# ---------- ОТЗЫВЫ ----------
@router.callback_query(F.data == "reviews")
async def reviews(c: CallbackQuery):
    await c.message.answer(reviews_text(), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("✍️ Оставить отзыв", "rv_new", "success")]]))
    await c.answer()
 
@router.callback_query(F.data == "rv_new")
async def rv_new(c: CallbackQuery):
    eu(c.from_user)
    if not has_bought(c.from_user.id):
        return await c.answer("Отзыв можно оставить после первой покупки 🙂", show_alert=True)
    await c.message.answer("Поставь оценку:", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[btn("⭐" * n, f"rv_r:{n}") for n in (1, 2, 3, 4, 5)]]))
    await c.answer()
 
@router.callback_query(F.data.startswith("rv_r:"))
async def rv_rate(c: CallbackQuery, state: FSMContext):
    await state.set_state(U.review); await state.update_data(rating=int(c.data.split(":")[1]))
    await c.message.edit_text("✍️ Напиши текст отзыва (5–300 символов):")
    await c.answer()
 
@router.message(U.review, F.text)
async def rv_text(m: Message, state: FSMContext, bot: Bot):
    t = m.text.strip()
    if not 5 <= len(t) <= 300: return await m.answer("Нужно от 5 до 300 символов, попробуй ещё раз.")
    rating = (await state.get_data())["rating"]; await state.clear()
    rid = add_review(m.from_user.id, m.from_user.full_name, rating, t)
    await m.answer("💖 Спасибо за отзыв!")
    await notify_review(bot, rid, m.from_user.full_name, rating, t)
 
# ---------- ЗАКАЗ В БОТЕ (по ссылке b_ или из мини-аппа) ----------
async def begin_order(m: Message, arg: str):
    try:
        _, iid, target = arg.split("_", 2)
        o = await make_order(m.from_user.id, m.from_user.username, iid, target)
    except ValueError as e:
        return await m.answer(f"❌ {e}")
    except Exception:
        logging.exception("order failed")
        return await m.answer("⚠️ Не удалось оформить заказ, попробуй ещё раз или напиши в поддержку.")
    bal = q("SELECT balance FROM users WHERE id=?", (m.from_user.id,), True)["balance"]
    rows = []
    if o["sec"] not in ("stars", "premium"):
        rows.append([btn(f"⭐ Оплатить Stars ({stars(o['rub'])}⭐)", f"pay_s:{o['id']}", "success")])
    if CRYPTO_TOKEN: rows.append([btn("🪙 Оплатить CryptoBot", f"pay_c:{o['id']}", "primary")])
    if CARD_DETAILS: rows.append([btn("💳 Оплатить переводом (карта/СБП)", f"pay_m:{o['id']}", "primary")])
    if bal >= o["rub"]: rows.append([btn(f"💰 Оплатить балансом ({bal} ₽)", f"pay_b:{o['id']}", "success")])
    if not rows: rows.append([InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL, style="primary")])
    rows.append([btn("✖️ Отмена", f"cancel:{o['id']}", "danger")])
    off = f" (скидка {o['disc']}%)" if o["disc"] else ""
    await m.answer(f"🧾 <b>Заказ #{o['id']}</b>\n\n{html.escape(o['title'])}\n{o['label']}\n💰 К оплате: <b>{o['rub']} ₽</b>{off}\n\nВыбери способ оплаты:",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
 
def own_order(c: CallbackQuery, statuses=("new",)):
    o = q("SELECT * FROM orders WHERE id=?", (int(c.data.split(":")[1]),), True)
    return o if o and o["uid"] == c.from_user.id and o["status"] in statuses else None
 
@router.callback_query(F.data.startswith("cancel:"))
async def cancel(c: CallbackQuery):
    o = own_order(c)
    if o: q("UPDATE orders SET status='cancel' WHERE id=?", (o["id"],))
    await c.message.delete(); await c.answer("Заказ отменён")
 
@router.callback_query(F.data.startswith("pay_s:"))
async def pay_stars(c: CallbackQuery, bot: Bot):
    o = own_order(c)
    if not o: return await c.answer("Заказ недоступен", show_alert=True)
    q("UPDATE orders SET method='stars' WHERE id=?", (o["id"],))
    await bot.send_invoice(c.from_user.id, title=o["title"], description=f"Заказ #{o['id']} · {o['target']}",
                           payload=f"o{o['id']}", currency="XTR",
                           prices=[LabeledPrice(label=o["title"], amount=stars(o["rub"]))])
    await c.answer()
 
@router.callback_query(F.data.startswith("pay_c:"))
async def pay_crypto(c: CallbackQuery):
    o = own_order(c)
    if not o: return await c.answer("Заказ недоступен", show_alert=True)
    url = await create_crypto(o["id"], o["rub"])
    if not url: return await c.answer("CryptoBot недоступен, выбери другой способ", show_alert=True)
    await c.message.edit_text(f"🪙 Счёт на <b>{o['rub']} ₽</b> создан (действует 1 час).\nПосле оплаты товар придёт автоматически.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=url, style="success")],
            [btn("🔄 Проверить оплату", f"chk:{o['id']}", "primary")]]))
    await c.answer()
 
@router.callback_query(F.data.startswith("pay_m:"))
async def pay_manual(c: CallbackQuery):
    o = own_order(c)
    if not o: return await c.answer("Заказ недоступен", show_alert=True)
    q("UPDATE orders SET method='manual' WHERE id=?", (o["id"],))
    await c.message.edit_text(f"💳 <b>Оплата переводом</b>\n\nЗаказ #{o['id']} · к оплате <b>{o['rub']} ₽</b>\n\n{html.escape(CARD_DETAILS)}\n\n"
        "После перевода нажми «Я оплатил» — мы проверим оплату и выдадим заказ.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Я оплатил", f"paid_m:{o['id']}", "success")],
                                                           [btn("✖️ Отмена", f"cancel:{o['id']}", "danger")]]))
    await c.answer()
 
@router.callback_query(F.data.startswith("paid_m:"))
async def paid_manual(c: CallbackQuery, bot: Bot):
    o = own_order(c)
    if not o: return await c.answer("Заказ недоступен", show_alert=True)
    q("UPDATE orders SET status='review' WHERE id=?", (o["id"],))
    await c.message.edit_text("⏳ Спасибо! Проверяем оплату — обычно до 15 минут. Мы напишем, когда заказ будет выдан.")
    await notify_admins(bot, "💳 <b>Проверь перевод</b>\n" + order_info(o),
        InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Оплата пришла", f"mok:{o['id']}", "success"), btn("❌ Не пришла", f"mno:{o['id']}", "danger")]]))
    await c.answer()
 
@router.callback_query(F.data.startswith("pay_b:"))
async def pay_bal(c: CallbackQuery, bot: Bot):
    o = own_order(c)
    if not o or not await pay_balance(bot, o["id"], c.from_user.id):
        return await c.answer("Недостаточно средств или заказ недоступен", show_alert=True)
    await c.message.delete(); await c.answer("Оплачено с баланса")
 
@router.callback_query(F.data.startswith("chk:"))
async def chk(c: CallbackQuery, bot: Bot):
    o = own_order(c, ("wait", "paid", "done"))
    if not o: return await c.answer("Заказ недоступен", show_alert=True)
    st = await check_crypto(bot, o["id"])
    ok = st in ("paid", "done")
    await c.answer("✅ Оплачено!" if ok else "Оплата пока не найдена", show_alert=not ok)
 
@router.pre_checkout_query()
async def pre(p: PreCheckoutQuery):
    o = q("SELECT status FROM orders WHERE id=?", (int(p.invoice_payload[1:]),), True)
    await p.answer(ok=bool(o and o["status"] == "new"), error_message="Заказ уже недоступен")
 
@router.message(F.successful_payment)
async def paid(m: Message, bot: Bot):
    await finish(bot, int(m.successful_payment.invoice_payload[1:]))
 
# ---------- API ДЛЯ МИНИ-АППА ----------
EMOJI_IDS = {v[0] for v in EMO.values()}
EMOJI_CACHE = {}
 
def auth(init):
    """Проверка подписи initData от Telegram."""
    try:
        d = dict(parse_qsl(init, strict_parsing=True)); h = d.pop("hash")
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h): return None
        if time.time() - int(d["auth_date"]) > 86400: return None
        return json.loads(d["user"])
    except Exception:
        return None
 
def J(d, status=200): return web.json_response(d, status=status)
def _int(v, default=0):
    try: return int(v)
    except Exception: return default
 
async def authed(r):
    try: d = await r.json()
    except Exception: d = {}
    u = auth(d.get("init", ""))
    if not u: raise web.HTTPUnauthorized(text='{"error":"Открой магазин кнопкой из бота (не по ссылке в браузере)"}', content_type="application/json")
    name = (u.get("first_name", "") + " " + u.get("last_name", "")).strip() or "Без имени"
    ensure_user(u["id"], u.get("username"), name)
    return d, u, name
 
async def api_ping(r): return J({"ok": True, "bot": BOT_USERNAME})
 
async def api_me(r):
    d, u, name = await authed(r)
    uid = u["id"]
    row = q("SELECT * FROM users WHERE id=?", (uid,), True)
    s = q("SELECT COUNT(*) n, COALESCE(SUM(rub),0) t FROM orders WHERE uid=? AND status IN ('paid','done')", (uid,), True)
    refs = q("SELECT COUNT(*) n FROM users WHERE ref_by=?", (uid,), True)["n"]
    hist = [dict(id=o["id"], title=o["title"], rub=o["rub"], status=o["status"], ts=o["ts"]) for o in
            q("SELECT * FROM orders WHERE uid=? AND status IN ('wait','paid','done') ORDER BY id DESC LIMIT 5", (uid,))]
    return J(dict(reg=row["reg"], discount=row["discount"], balance=row["balance"], orders=s["n"], spent=s["t"],
                  refs=refs, earned=row["ref_earned"], percent=REF_PERCENT, crypto=bool(CRYPTO_TOKEN), rate=RUB_PER_STAR,
                  link=f"https://t.me/{BOT_USERNAME}?start=ref_{uid}", bought=has_bought(uid), history=hist))
 
async def api_order(r):
    d, u, name = await authed(r)
    bot, method = r.app["bot"], d.get("method")
    try: o = await make_order(u["id"], u.get("username"), str(d.get("item")), str(d.get("target", "x")))
    except ValueError as e: return J({"error": str(e)})
    info = dict(id=o["id"], title=o["title"], target=o["target"], rub=o["rub"], disc=o["disc"])
    def fail(msg):
        q("UPDATE orders SET status='cancel' WHERE id=?", (o["id"],)); return J({"error": msg})
    if method == "stars":
        if o["sec"] in ("stars", "premium"): return fail("Этот товар нельзя оплатить звёздами")
        q("UPDATE orders SET method='stars' WHERE id=?", (o["id"],))
        try:
            link = await bot.create_invoice_link(title=o["title"], description=f"Заказ #{o['id']} · {o['target']}",
                payload=f"o{o['id']}", currency="XTR", prices=[LabeledPrice(label=o["title"], amount=stars(o["rub"]))])
        except Exception as e:
            logging.warning("invoice link: %s", e); return fail("Не удалось создать счёт, попробуй ещё раз")
        return J({**info, "link": link, "stars": stars(o["rub"])})
    if method == "crypto":
        url = await create_crypto(o["id"], o["rub"])
        return J({**info, "url": url}) if url else fail("CryptoBot недоступен")
    if method == "balance":
        return J(info) if await pay_balance(bot, o["id"], u["id"]) else fail("Недостаточно средств на балансе")
    return fail("Неверный способ оплаты")
 
async def api_cancel(r):
    d, u, name = await authed(r)
    q("UPDATE orders SET status='cancel' WHERE id=? AND uid=? AND status IN ('new','wait')", (_int(d.get("id")), u["id"]))
    return J({"ok": True})
 
async def api_status(r):
    d, u, name = await authed(r)
    o = q("SELECT * FROM orders WHERE id=?", (_int(d.get("id")),), True)
    if not o or o["uid"] != u["id"]: return J({"error": "not found"})
    if o["status"] == "wait":
        try: await check_crypto(r.app["bot"], o["id"])
        except Exception: pass
        o = q("SELECT * FROM orders WHERE id=?", (o["id"],), True)
    return J({"status": o["status"], "result": o["result"], "sec": o["sec"]})
 
async def api_promo(r):
    d, u, name = await authed(r)
    ok, msg = apply_promo(u["id"], str(d.get("code", "")))
    return J({"ok": ok, "msg": msg})
 
async def api_reviews(r): return J(reviews_data())
 
async def api_review(r):
    d, u, name = await authed(r)
    try: rating, text = int(d.get("rating")), str(d.get("text", "")).strip()
    except Exception: return J({"error": "Неверные данные"})
    if not has_bought(u["id"]): return J({"error": "Отзыв можно оставить после первой покупки"})
    if not (1 <= rating <= 5 and 5 <= len(text) <= 300): return J({"error": "Оценка 1–5 и текст 5–300 символов"})
    rid = add_review(u["id"], name, rating, text)
    asyncio.create_task(notify_review(r.app["bot"], rid, name, rating, text))
    return J({"ok": True})
 
async def api_emoji(r):
    """Премиум-эмодзи для мини-аппа: берём стикер у Telegram и отдаём браузеру."""
    eid = r.query.get("id", "")
    if eid not in EMOJI_IDS: return J({"error": "no"}, 404)
    try:
        if eid not in EMOJI_CACHE:
            bot = r.app["bot"]
            st = (await bot.get_custom_emoji_stickers([eid]))[0]
            f = await bot.get_file(st.file_id)
            data = (await bot.download_file(f.file_path)).read()
            if f.file_path.endswith(".tgs"): EMOJI_CACHE[eid] = (gzip.decompress(data), "application/json")
            elif f.file_path.endswith(".webm"): EMOJI_CACHE[eid] = (data, "video/webm")
            else: EMOJI_CACHE[eid] = (data, "image/webp")
        body, ct = EMOJI_CACHE[eid]
        return web.Response(body=body, content_type=ct, headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        logging.warning("emoji %s: %s", eid, e); return J({"error": "emoji"}, 404)
 
async def index(r):
    f = WEBDIR / "index.html"
    return web.FileResponse(f) if f.exists() else web.Response(status=404, text="index.html не найден рядом с ботом")
async def f_catalog(r): return J(catalog())
async def f_banner(r): return web.Response(body=banner_bytes(), content_type="image/jpeg")
 
@web.middleware
async def cors(req, handler):
    if req.method == "OPTIONS": resp = web.Response(status=204)
    else:
        try: resp = await handler(req)
        except web.HTTPException as e: resp = e
    resp.headers.update({"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                         "Access-Control-Allow-Headers": "Content-Type, ngrok-skip-browser-warning"})
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp
 
def build_app(bot):
    app = web.Application(middlewares=[cors]); app["bot"] = bot
    app.add_routes([web.get("/api/ping", api_ping), web.post("/api/me", api_me), web.post("/api/order", api_order),
                    web.post("/api/cancel", api_cancel), web.post("/api/status", api_status), web.post("/api/promo", api_promo),
                    web.get("/api/reviews", api_reviews), web.post("/api/review", api_review),
                    web.get("/api/emoji", api_emoji), web.get("/", index),
                    web.get("/catalog.json", f_catalog), web.get("/banner.jpg", f_banner)])
    return app
 
# ---------- АДМИН-ПАНЕЛЬ ----------
class A(StatesGroup):
    bc = State(); promo = State(); codes = State()
 
def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("📊 Статистика", "adm:stats", "primary"), btn("📋 Невыданные заказы", "adm:orders", "primary")],
        [btn("📣 Рассылка", "adm:bc"), btn("🎟 Создать промокод", "adm:promo")],
        [btn("📦 Добавить коды на UC", "adm:codes", "success")]])
 
@admin.callback_query(F.data.startswith("done:"))
async def done(c: CallbackQuery, bot: Bot):
    oid = int(c.data.split(":")[1])
    o = q("SELECT * FROM orders WHERE id=?", (oid,), True)
    if o and o["status"] == "paid":
        q("UPDATE orders SET status='done' WHERE id=?", (oid,))
        await bot.send_message(o["uid"], f"🎉 Заказ #{oid} выполнен! Спасибо, что выбрал KABAN UC.",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("⭐ Оставить отзыв", "rv_new")]]))
    await c.answer("Готово"); await c.message.edit_reply_markup(reply_markup=None)
 
@admin.callback_query(F.data.startswith("rdel:"))
async def rdel(c: CallbackQuery):
    q("DELETE FROM reviews WHERE id=?", (int(c.data.split(":")[1]),))
    await c.answer("Отзыв удалён"); await c.message.edit_reply_markup(reply_markup=None)
 
@admin.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear(); await m.answer("🛠 <b>Админ-панель KABAN UC</b>", reply_markup=admin_kb(), parse_mode="HTML")
 
@admin.callback_query(F.data == "adm:stats")
async def adm_stats(c: CallbackQuery):
    n = q("SELECT COUNT(*) n FROM users", one=True)["n"]
    s = q("SELECT COUNT(*) n, COALESCE(SUM(rub),0) t FROM orders WHERE status IN ('paid','done')", one=True)
    p = q("SELECT COUNT(*) n FROM orders WHERE status='paid'", one=True)["n"]
    rv = reviews_data(0)
    stock = "\n".join(f"  {r['item']}: {r['k']} шт" for r in q("SELECT item, COUNT(*) k FROM codes WHERE used=0 GROUP BY item")) or "  пусто"
    await c.message.answer(f"📊 Пользователей: {n}\n🛒 Оплачено заказов: {s['n']} на {s['t']} ₽\n⏳ Ждут выдачи: {p}\n"
                           f"⭐ Отзывов: {rv['count']} (средняя {rv['avg']})\n📦 Коды в наличии:\n{stock}")
    await c.answer()
 
@admin.callback_query(F.data.startswith("mok:"))
async def mok(c: CallbackQuery, bot: Bot):
    oid = int(c.data.split(":")[1]); o = q("SELECT * FROM orders WHERE id=?", (oid,), True)
    if o and o["status"] == "review": await finish(bot, oid)
    await c.answer("Оплата подтверждена"); await c.message.edit_reply_markup(reply_markup=None)
 
@admin.callback_query(F.data.startswith("mno:"))
async def mno(c: CallbackQuery, bot: Bot):
    oid = int(c.data.split(":")[1]); o = q("SELECT * FROM orders WHERE id=?", (oid,), True)
    if o and o["status"] == "review":
        q("UPDATE orders SET status='cancel' WHERE id=?", (oid,))
        try: await bot.send_message(o["uid"], f"❌ Оплата по заказу #{oid} не найдена. Если ты платил — напиши в поддержку.",
                                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL, style="primary")]]))
        except Exception: pass
    await c.answer("Отклонено"); await c.message.edit_reply_markup(reply_markup=None)
 
@admin.callback_query(F.data == "adm:orders")
async def adm_orders(c: CallbackQuery):
    paid = q("SELECT * FROM orders WHERE status='paid' ORDER BY id DESC LIMIT 10")
    rev = q("SELECT * FROM orders WHERE status='review' ORDER BY id DESC LIMIT 10")
    if not paid and not rev: await c.message.answer("Невыданных заказов нет 🎉")
    for o in rev:
        await c.message.answer("💳 <b>Ждёт проверки перевода</b>\n" + order_info(o), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Оплата пришла", f"mok:{o['id']}", "success"), btn("❌ Не пришла", f"mno:{o['id']}", "danger")]]))
    for o in paid:
        await c.message.answer("⏳ <b>Оплачен, ждёт выдачи</b>\n" + order_info(o), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Выдано", f"done:{o['id']}", "success")]]))
    await c.answer()
 
@admin.callback_query(F.data == "adm:bc")
async def adm_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.bc); await c.message.answer("📣 Отправь текст рассылки (или /admin для отмены):"); await c.answer()
 
@admin.message(A.bc, F.text)
async def bc_send(m: Message, state: FSMContext, bot: Bot):
    await state.clear(); ok = 0
    for u in q("SELECT id FROM users"):
        try: await bot.send_message(u["id"], m.text); ok += 1
        except Exception: pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Доставлено: {ok}")
 
@admin.callback_query(F.data == "adm:promo")
async def adm_promo(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.promo)
    await c.message.answer("🎟 Формат: <code>КОД ПРОЦЕНТ КОЛ-ВО</code>\nНапример: <code>KABAN10 10 50</code>", parse_mode="HTML"); await c.answer()
 
@admin.message(A.promo, F.text)
async def promo_new(m: Message, state: FSMContext):
    try:
        code, pc, n = m.text.split(); pc, n = int(pc), int(n); assert 1 <= pc <= 90
    except Exception:
        return await m.answer("❌ Неверный формат. Пример: KABAN10 10 50")
    q("INSERT OR REPLACE INTO promos VALUES(?,?,?)", (code.upper(), pc, n)); await state.clear()
    await m.answer(f"✅ Промокод {code.upper()}: −{pc}%, использований: {n}")
 
@admin.callback_query(F.data == "adm:codes")
async def adm_codes(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.codes)
    await c.message.answer("📦 Первой строкой — id товара из catalog.json (например <code>cd60</code>), дальше по одному коду на строку.", parse_mode="HTML"); await c.answer()
 
@admin.message(A.codes, F.text)
async def codes_add(m: Message, state: FSMContext):
    lines = [x.strip() for x in m.text.splitlines() if x.strip()]
    if len(lines) < 2: return await m.answer("❌ Нужна строка с id товара и минимум один код.")
    for code in lines[1:]: q("INSERT INTO codes(item,code) VALUES(?,?)", (lines[0], code))
    await state.clear(); await m.answer(f"✅ Добавлено кодов: {len(lines) - 1} для {lines[0]}")
 
# ---------- ЗАПУСК ----------
async def main():
    global BOT_USERNAME
    bot = Bot(BOT_TOKEN)
    BOT_USERNAME = (await bot.me()).username
    await bot.delete_webhook(drop_pending_updates=True)
    await notify_admins(bot, f"✅ <b>Бот запущен</b> (v{VERSION}). Оплаченные заказы будут приходить сюда.")
    runner = web.AppRunner(build_app(bot)); await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    logging.info("KABAN UC bot v%s | папка мини-аппа: %s | catalog.json: %s", VERSION, WEBDIR,
                 "найден" if (WEBDIR / "catalog.json").exists() else "НЕ найден, использую встроенный")
    logging.info("Мини-апп: %s | проверка сервера: %s/api/ping | локально http://127.0.0.1:%s", WEBAPP, API_URL, PORT)
    dp = Dispatcher(); dp.include_routers(admin, router)
    asyncio.create_task(crypto_loop(bot))
    await dp.start_polling(bot)
 
# ---------- ВСТРОЕННЫЕ КОПИИ (запасные, если рядом нет файлов webapp/) ----------
DEFAULT_CATALOG = r'''{
  "uc": [
    {
      "id": "uc60",
      "title": "60 UC",
      "rub": 91
    },
    {
      "id": "uc325",
      "title": "325 UC",
      "rub": 459
    },
    {
      "id": "uc660",
      "title": "660 UC",
      "rub": 919,
      "tag": "ХИТ"
    },
    {
      "id": "uc1800",
      "title": "1800 UC",
      "rub": 2296
    },
    {
      "id": "uc3850",
      "title": "3850 UC",
      "rub": 4593,
      "tag": "ВЫГОДНО"
    },
    {
      "id": "uc8100",
      "title": "8100 UC",
      "rub": 9186
    }
  ],
  "plus": [
    {
      "id": "pl1",
      "title": "Plus · 1 месяц",
      "rub": 450
    },
    {
      "id": "pl3",
      "title": "Plus · 3 месяца",
      "rub": 1250,
      "tag": "ВЫГОДНО"
    }
  ],
  "codes": [
    {
      "id": "cd60",
      "title": "Код 60 UC",
      "rub": 91
    },
    {
      "id": "cd325",
      "title": "Код 325 UC",
      "rub": 459
    },
    {
      "id": "cd660",
      "title": "Код 660 UC",
      "rub": 919
    },
    {
      "id": "cd1800",
      "title": "Код 1800 UC",
      "rub": 2296
    }
  ],
  "stars": [
    {
      "id": "st50",
      "title": "50 ⭐",
      "rub": 76
    },
    {
      "id": "st100",
      "title": "100 ⭐",
      "rub": 151
    },
    {
      "id": "st250",
      "title": "250 ⭐",
      "rub": 378
    },
    {
      "id": "st500",
      "title": "500 ⭐",
      "rub": 755
    },
    {
      "id": "st1000",
      "title": "1000 ⭐",
      "rub": 1510,
      "tag": "ХИТ"
    }
  ],
  "premium": [
    {
      "id": "pr3",
      "title": "Premium · 3 мес",
      "rub": 1149
    },
    {
      "id": "pr6",
      "title": "Premium · 6 мес",
      "rub": 1549
    },
    {
      "id": "pr12",
      "title": "Premium · 12 мес",
      "rub": 2649,
      "tag": "ВЫГОДНО"
    }
  ]
}'''
BANNER_B64 = "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"
 
if __name__ == "__main__":
    asyncio.run(main())
 
