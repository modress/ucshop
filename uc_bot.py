import asyncio, json, math, re
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (Message, PreCheckoutQuery, LabeledPrice, WebAppInfo,
                           ReplyKeyboardMarkup, KeyboardButton)

# ===== НАСТРОЙКИ =====
BOT_TOKEN = "ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER"
WEBAPP_URL = "https://ТВОЙ-САЙТ.github.io/webapp/index.html"  # https-ссылка на index.html
ADMIN_ID = 123456789          # твой Telegram ID (куда падают заказы)
RUB_PER_STAR = 1.51           # курс: сколько ₽ стоит 1 звезда
# id: (UC, цена в ₽)  — те же цены должны быть в webapp/index.html
PACKS = {
    "uc60":   (60,   95),
    "uc325":  (325,  480),
    "uc660":  (660,  950),
    "uc1800": (1800, 2400),
    "uc3850": (3850, 4800),
    "uc8100": (8100, 9500),
}
# ======================

router = Router()

def stars(rub): return math.ceil(rub / RUB_PER_STAR)

@router.message(CommandStart())
async def start(m: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛒 Купить UC", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True)
    await m.answer("🎮 <b>UC для PUBG Mobile</b>\n\nНажми кнопку ниже, выбери пак, "
                   "введи свой ID — и оплати звёздами.", reply_markup=kb, parse_mode="HTML")

# Мини-апп присылает JSON через tg.sendData -> приходит сюда
@router.message(F.web_app_data)
async def from_webapp(m: Message, bot: Bot):
    try:
        d = json.loads(m.web_app_data.data)
        pack = PACKS[d["pack"]]
        pid = re.sub(r"\D", "", str(d["pubg_id"]))
        assert 8 <= len(pid) <= 12
    except Exception:
        return await m.answer("❌ Ошибка данных. Попробуй ещё раз.")
    uc, rub = pack  # цена берётся ТОЛЬКО с сервера, не из мини-аппа
    await bot.send_invoice(
        chat_id=m.chat.id, title=f"{uc} UC PUBG Mobile",
        description=f"Пополнение на ID {pid}",
        payload=f"{d['pack']}:{pid}", currency="XTR",
        prices=[LabeledPrice(label=f"{uc} UC", amount=stars(rub))])

@router.pre_checkout_query()
async def pre(q: PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def paid(m: Message, bot: Bot):
    pack_id, pid = m.successful_payment.invoice_payload.split(":")
    uc, rub = PACKS[pack_id]
    await m.answer(f"✅ Оплачено! <b>{uc} UC</b> будут зачислены на ID <code>{pid}</code> "
                   f"в ближайшее время.", parse_mode="HTML")
    await bot.send_message(
        ADMIN_ID, f"💰 Новый заказ\nUser: @{m.from_user.username} ({m.from_user.id})\n"
                  f"Пак: {uc} UC\nPUBG ID: {pid}\nЗвёзд: {m.successful_payment.total_amount}")

async def main():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(); dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
