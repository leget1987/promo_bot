import ast
import logging
import os
import random
import string
import sqlite3
import qrcode
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# ===== CONFIG =====
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_USERNAME = os.getenv('CHANNEL_USERNAME')
ADMIN_USER_NAMES = ast.literal_eval(os.getenv('ADMIN_USER_NAMES'))
DATABASE_NAME = os.getenv('DATABASE_NAME')


# Настройки генерации кодов
CODE_LENGTH = 8
CODE_PREFIX = "DC"
DISCOUNT_TEMPLATE = "10%"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)


# ===== DATABASE FUNCTIONS =====
def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect(DATABASE_NAME)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            discount_value TEXT NOT NULL,
            is_used BOOLEAN NOT NULL DEFAULT FALSE,
            created_at DATETIME NOT NULL,
            used_at DATETIME,
            issued_to TEXT NOT NULL,
            issued_at DATETIME,
            used_by TEXT DEFAULT NULL
        )
    ''')
    conn.commit()
    conn.close()


def generate_unique_code():
    """Генерирует уникальный промо-код"""
    while True:
        random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=CODE_LENGTH))
        code = f"{CODE_PREFIX}{random_part}" if CODE_PREFIX else random_part

        conn = sqlite3.connect(DATABASE_NAME)
        cur = conn.cursor()
        cur.execute('SELECT id FROM promo_codes WHERE code = ?', (code,))
        exists = cur.fetchone()
        conn.close()

        if not exists:
            return code


def create_promo_code_for_user(user_name):
    """Создает и выдает промо-код пользователю с обработкой ошибок"""
    MAX_ATTEMPTS = 10
    attempts = 0

    while attempts < MAX_ATTEMPTS:
        code = generate_unique_code()
        conn = None

        try:
            conn = sqlite3.connect(DATABASE_NAME)
            cur = conn.cursor()

            # Проверяем, не существует ли код (дополнительная проверка)
            cur.execute('SELECT id FROM promo_codes WHERE code = ?', (code,))
            if cur.fetchone():
                logging.warning(f"Код {code} уже существует в БД (коллизия)")
                attempts += 1
                continue

            # Вставляем новый код
            cur.execute('''
                INSERT INTO promo_codes (code, discount_value, created_at, issued_to, issued_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (code, DISCOUNT_TEMPLATE, datetime.now(), user_name, datetime.now()))
            conn.commit()

            logging.info(f"Успешно создан промо-код {code} для пользователя {user_name}")
            return code, DISCOUNT_TEMPLATE

        except sqlite3.IntegrityError as e:
            if conn:
                conn.rollback()
            logging.error(f"IntegrityError: {e}")

            # Анализируем тип ошибки
            error_msg = str(e)
            if "UNIQUE constraint failed: promo_codes.code" in error_msg:
                logging.warning(f"Коллизия кода {code} - пробуем снова")
                attempts += 1
            elif "NOT NULL constraint failed" in error_msg:
                logging.error("Ошибка NOT NULL constraint")
                return None, None
            else:
                logging.error(f"Неизвестная ошибка целостности: {e}")
                return None, None

        except Exception as e:
            if conn:
                conn.rollback()
            logging.error(f"Неожиданная ошибка при создании кода: {e}")
            return None, None

        finally:
            if conn:
                conn.close()

    logging.error(f"Не удалось создать уникальный код после {MAX_ATTEMPTS} попыток")
    return None, None


def has_user_received_code(user_name):
    """Проверяет, получал ли пользователь код ранее"""
    conn = sqlite3.connect(DATABASE_NAME)
    cur = conn.cursor()
    cur.execute('SELECT code FROM promo_codes WHERE issued_to = ?', (user_name,))
    result = cur.fetchone()
    conn.close()
    return result is not None


def apply_promo_code(code, applied_by_user_id=None):
    """Применяет промо-код (отмечает как использованный)"""
    conn = sqlite3.connect(DATABASE_NAME)
    cur = conn.cursor()

    cur.execute('''
        SELECT id, is_used, discount_value, issued_to 
        FROM promo_codes WHERE code = ?
    ''', (code,))
    result = cur.fetchone()

    if not result:
        conn.close()
        return False, "Код не найден"

    code_id, is_used, discount_value, issued_to = result

    if not issued_to:
        conn.close()
        return False, "Код не был выдан пользователю"

    if is_used:
        conn.close()
        return False, "Код уже использован"

    # Помечаем код как использованный
    cur.execute('''
        UPDATE promo_codes 
        SET is_used = TRUE, used_at = ?, used_by = ?
        WHERE code = ?
    ''', (datetime.now(), applied_by_user_id, code))
    conn.commit()
    conn.close()

    return True, f"✅ Код '{code}' на скидку {discount_value} успешно применен!"


def get_code_info(code):
    """Возвращает информацию о коде"""
    conn = sqlite3.connect(DATABASE_NAME)
    cur = conn.cursor()
    cur.execute('''
        SELECT code, discount_value, is_used, created_at, issued_to, used_at, used_by
        FROM promo_codes WHERE code = ?
    ''', (code,))
    result = cur.fetchone()
    conn.close()
    return result


# ===== QR CODE GENERATION =====
def generate_qr_code(data, filename="qrcode.png"):
    """Универсальная функция генерации QR-кода"""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(filename)
    return filename


# ===== TELEGRAM BOT FUNCTIONS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    user_name = user.name

    # Создаем клавиатуру с основными кнопками
    keyboard = [
        [InlineKeyboardButton("🎁 Получить промо-код", callback_data="get_promo")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")]
    ]
    if user_name in ADMIN_USER_NAMES:
        keyboard.append([InlineKeyboardButton("📱 Сканировать QR-код", callback_data="scan_qr")],)
        keyboard.append([InlineKeyboardButton("👑 Админ-статистика", callback_data="admin_stats")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n\n"
        "Я бот для управления промо-кодами.\n\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик фотографий (для сканирования QR-кодов)"""
    user = update.effective_user
    photo = update.message.photo[-1]  # Берем самую большую версию фото

    # Скачиваем фото
    photo_file = await photo.get_file()
    photo_path = f"temp_photo_{user.id}.jpg"
    await photo_file.download_to_drive(photo_path)

    # Пытаемся прочитать QR-код
    qr_text = read_qr_code_from_image(photo_path)

    if qr_text:
        # Проверяем и применяем код
        success, message = apply_promo_code(qr_text, user.name)
        await update.message.reply_text(message)

        # Если это админ, показываем дополнительную информацию
        if user.name in ADMIN_USER_NAMES:
            code_info = get_code_info(qr_text)
            if code_info:
                code, discount, is_used, created, issued_to, used_at, used_by = code_info
                info_text = f"""
📋 Детали кода:
• Код: {code}
• Скидка: {discount}
• Статус: {'Использован' if is_used else 'Активен'}
• Создан: {created}
• Выдан пользователю: {issued_to}
• Использован: {used_at if used_at else 'Еще нет'}
                """
                await update.message.reply_text(info_text)
    else:
        await update.message.reply_text(
            "❌ QR-код не найден на изображении. Убедитесь, что фото четкое и код хорошо виден.")


def read_qr_code_from_image(image_path):
    """Читает QR-код из изображения"""
    try:
        import cv2
        from pyzbar.pyzbar import decode

        img = cv2.imread(image_path)
        decoded_objects = decode(img)

        if decoded_objects:
            return decoded_objects[0].data.decode('utf-8')
        return None
    except Exception as e:
        logging.error(f"Ошибка при чтении QR-кода: {e}")
        return None


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений (для ручного ввода кодов)"""
    user = update.effective_user
    if user is None:
        return
    text = update.effective_message.text.strip()
    if user.name in ADMIN_USER_NAMES:
        # Если сообщение похоже на промо-код (содержит префикс или нужную длину)
        if (CODE_PREFIX and text.startswith(CODE_PREFIX)) or len(text) == (len(CODE_PREFIX) + CODE_LENGTH):
            success, message = apply_promo_code(text, user.name)
            await update.message.reply_text(message)

            # Дополнительная информация для админа
            if user.name in ADMIN_USER_NAMES and success:
                code_info = get_code_info(text)
                if code_info:
                    code, discount, is_used, created, issued_to, used_at, used_by = code_info
                    info_text = f"""
    📋 Детали кода:
    • Код: {code}
    • Скидка: {discount}
    • Статус: {'Использован' if is_used else 'Активен'}
    • Создан: {created}
    • Выдан пользователю: {issued_to}
                    """
                    await update.message.reply_text(info_text)
        else:
            await update.message.reply_text("Отправьте мне QR-код или промо-код для активации")
    else:
        await update.message.reply_text("Только администраторы могут применить промокод")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    data = query.data

    if data == "get_promo":
        # Проверяем подписку и выдаем код
        try:
            chat_member = await context.bot.get_chat_member(CHANNEL_USERNAME, user.id)
            if chat_member.status in ['member', 'administrator', 'creator']:
                if has_user_received_code(user.name):
                    await query.edit_message_text("Вы уже получали свой промо-код! Спасибо за подписку!")
                else:
                    code, discount_value = create_promo_code_for_user(user.name)
                    if code is None:
                        await query.edit_message_text('Произошла ошибка при получение кода \U0001F612')
                    else:
                        qr_filename = generate_qr_code(code)
                        caption = f"🎉 Спасибо за подписку! Ваш промо-код на скидку {discount_value}.\nПокажите этот QR-код на кассе."

                        with open(qr_filename, 'rb') as qr_photo:
                            await context.bot.send_photo(
                                chat_id=user.id,
                                photo=qr_photo,
                                caption=caption
                            )
                        await query.edit_message_text(
                            f"*Ваш код:* `{code}`\nСохраните его на случай, если QR-код не отсканируется.",
                            parse_mode='Markdown'
                        )
            else:
                keyboard = [[InlineKeyboardButton("Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME[1:]}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    "Чтобы получить промо-код, подпишитесь на наш канал!",
                    reply_markup=reply_markup
                )
        except Exception as e:
            logging.error(f"Ошибка при проверке подписки: {e}")
            await query.edit_message_text("Произошла ошибка. Попробуйте позже.")

    elif data == "scan_qr":
        await query.edit_message_text(
            "📱 Отправьте мне фото QR-кода для сканирования.\n\n"
            "Убедитесь, что:\n"
            "• QR-код хорошо освещен\n"
            "• Занимает большую часть фото\n"
            "• Не размыт и не перекошен"
        )

    elif data == "help" and user.name in ADMIN_USER_NAMES:
        help_text = """
🤖 **Помощь по боту**

**Для клиентов:**
• Нажмите «Получить промо-код» для получения скидки
• Подпишитесь на канал, если еще не подписаны
• Покажите QR-код на кассе для активации скидки

**Для персонала:**
• Отправьте фото QR-кода или сам код для активации
• Код будет автоматически отмечен как использованный

**Формат кода:** {CODE_PREFIX} + {CODE_LENGTH} символов
        """.format(CODE_PREFIX=CODE_PREFIX, CODE_LENGTH=CODE_LENGTH)
        await query.edit_message_text(help_text, parse_mode='Markdown')
    elif data == "help":
        help_text = """
        🤖 **Помощь по боту**
        • Подпишитесь на канал, если еще не подписаны
        • Нажмите «Получить промо-код» для получения скидки
        • Покажите QR-код на кассе для активации скидки
        """
        await query.edit_message_text(help_text, parse_mode='Markdown')
    elif user.name in ADMIN_USER_NAMES:
        if data == "admin_stats":
            await admin_stats(update, context)


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда статистики для админов"""
    user_name = update.effective_user.name
    if user_name not in ADMIN_USER_NAMES:
        await update.message.reply_text("У вас нет прав для этой команды.")
        return

    conn = sqlite3.connect(DATABASE_NAME)
    cur = conn.cursor()

    cur.execute('SELECT COUNT(*) FROM promo_codes')
    total = cur.fetchone()[0]

    cur.execute('SELECT COUNT(*) FROM promo_codes WHERE issued_to IS NOT NULL')
    issued = cur.fetchone()[0]

    cur.execute('SELECT COUNT(*) FROM promo_codes WHERE is_used = TRUE')
    used = cur.fetchone()[0]

    conn.close()

    stats_text = f"""
📊 Статистика промо-кодов:

• Всего сгенерировано: {total}
• Выдано пользователям: {issued}
• Использовано: {used}
• Активных: {issued - used}

Шаблон скидки: {DISCOUNT_TEMPLATE}
    """

    await update.effective_message.reply_text(stats_text)

# ===== MAIN =====
def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", admin_stats))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling()


if __name__ == '__main__':
    main()