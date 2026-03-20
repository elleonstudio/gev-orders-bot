import os
import logging
from datetime import datetime
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes

# Настройка
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Получаем токены из переменных окружения Railway
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = os.getenv('NOTION_DATABASE_ID')

notion = Client(auth=NOTION_TOKEN)

# Состояния диалога
INVOICE, PRODUCT_NAME, QUANTITY, PRICE, DELIVERY, PURCHASE, MORE, CLIENT_RATE, REAL_RATE, PERCENT, FIXED_COMMISSION = range(11)

# Хранилище заказов (в памяти)
orders = {}

def get_code(name):
    return f"{name.upper()}-{datetime.now().strftime('%d%m%y')}-1"

# === КОМАНДЫ ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '👋 Привет! Бот для заказов.\n\n'
        '📋 Команды:\n'
        '/zakaz [имя] — новый заказ\n'
        '/nayti [текст] — найти заказ\n'
        '/cancel — отменить'
    )

async def zakaz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        await update.message.reply_text('❌ Укажи имя: /zakaz Армен')
        return ConversationHandler.END

    name = parts[1].strip()
    uid = update.effective_user.id

    orders[uid] = {
        'client': name,
        'items': [],
        'current': {},
        'invoice': False
    }

    # Спрашиваем инвойс
    keyboard = [[InlineKeyboardButton("Да", callback_data='inv_yes'), 
                 InlineKeyboardButton("Нет", callback_data='inv_no')]]
    await update.message.reply_text(f'📦 Заказ для: {name}\n\nИнвойс?', reply_markup=InlineKeyboardMarkup(keyboard))
    return INVOICE

async def invoice_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id

    orders[uid]['invoice'] = (query.data == 'inv_yes')
    await query.edit_message_text('📝 Название товара:')
    return PRODUCT_NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    orders[uid]['current']['name'] = update.message.text
    await update.message.reply_text('🔢 Количество:')
    return QUANTITY

async def get_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['current']['qty'] = int(update.message.text)
        await update.message.reply_text('💰 Цена за 1 шт (¥):')
        return PRICE
    except:
        await update.message.reply_text('❌ Число! Количество:')
        return QUANTITY

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['current']['price'] = float(update.message.text)
        await update.message.reply_text('🚚 Доставка (¥):')
        return DELIVERY
    except:
        await update.message.reply_text('❌ Число! Цена:')
        return PRICE

async def get_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['current']['delivery'] = float(update.message.text)
        await update.message.reply_text('🏭 Закупка за 1 шт (¥):')
        return PURCHASE
    except:
        await update.message.reply_text('❌ Число! Доставка:')
        return DELIVERY

async def get_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['current']['purchase'] = float(update.message.text)

        # Сразу спрашиваем "Ещё товар?" без фото
        keyboard = [[InlineKeyboardButton("✅ Да", callback_data='more_yes'), 
                     InlineKeyboardButton("❌ Нет", callback_data='more_no')]]
        await update.message.reply_text('Ещё товар?', reply_markup=InlineKeyboardMarkup(keyboard))
        return MORE
    except:
        await update.message.reply_text('❌ Число! Закупка:')
        return PURCHASE

async def more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id

    # Сохраняем товар
    orders[uid]['items'].append(orders[uid]['current'])

    if query.data == 'more_yes':
        orders[uid]['current'] = {}
        await query.edit_message_text('📝 Название товара:')
        return PRODUCT_NAME
    else:
        await query.edit_message_text('💱 Курс клиенту (например 58):')
        return CLIENT_RATE

async def get_client_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['client_rate'] = float(update.message.text)
        await update.message.reply_text('💱 Курс реальный (например 55):')
        return REAL_RATE
    except:
        await update.message.reply_text('❌ Число! Курс:')
        return CLIENT_RATE

async def get_real_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['real_rate'] = float(update.message.text)

        # Расчёты
        items = orders[uid]['items']
        client_rate = orders[uid]['client_rate']

        # Считаем итог в юанях
        total_yuan = sum(i['price'] * i['qty'] + i['delivery'] for i in items)
        total_dram = total_yuan * client_rate

        # Проверяем комиссию
        if total_dram < 10000:
            # Спросить фиксированную сумму: 10000 или 15000
            keyboard = [
                [InlineKeyboardButton("10000 ֏", callback_data='fix_10000')], 
                [InlineKeyboardButton("15000 ֏", callback_data='fix_15000')]
            ]
            msg = f"📊 Итого: {total_yuan} ¥ = {int(total_dram)} ֏\n\nВыбери фиксированную комиссию:"
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
            return FIXED_COMMISSION
        else:
            # Спросить процент
            keyboard = [[InlineKeyboardButton("+3%", callback_data='pct_3'), 
                         InlineKeyboardButton("+5%", callback_data='pct_5')]]
            msg = f"📊 Итого: {total_yuan} ¥ = {int(total_dram)} ֏\n\nВыбери комиссию:"
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
            return PERCENT

    except:
        await update.message.reply_text('❌ Число! Курс:')
        return REAL_RATE

async def fixed_commission_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id

    fixed_amount = 10000 if query.data == 'fix_10000' else 15000
    orders[uid]['fixed_commission'] = fixed_amount
    orders[uid]['commission'] = 0  # Нет процентной комиссии

    # Пересчёт
    items = orders[uid]['items']
    client_rate = orders[uid]['client_rate']
    total_yuan = sum(i['price'] * i['qty'] + i['delivery'] for i in items)
    final_dram = fixed_amount

    await show_result(update, context, total_yuan, final_dram, f"Фикс {fixed_amount}", 0, fixed_amount)
    return ConversationHandler.END

async def percent_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id

    pct = 3 if query.data == 'pct_3' else 5
    orders[uid]['commission'] = pct
    orders[uid]['fixed_commission'] = 0

    # Пересчёт
    items = orders[uid]['items']
    client_rate = orders[uid]['client_rate']
    total_yuan = sum(i['price'] * i['qty'] + i['delivery'] for i in items)
    base_dram = total_yuan * client_rate
    final_dram = int(base_dram * (1 + pct / 100))

    await show_result(update, context, total_yuan, final_dram, f"+{pct}%", pct, 0)
    return ConversationHandler.END

async def show_result(update, context, total_yuan, final_dram, commission_text, commission_pct, fixed_amount):
    uid = update.effective_user.id
    items = orders[uid]['items']
    client = orders[uid]['client']
    client_rate = orders[uid]['client_rate']
    real_rate = orders[uid]['real_rate']
    invoice = orders[uid]['invoice']

    # Генерируем код заказа
    order_code = get_code(client)

    # === РАСЧЁТ ДЛЯ МЕНЯ ===
    # На закупку (¥) = закупочная цена × количество + доставка
    total_purchase_yuan = sum(i['purchase'] * i['qty'] + i['delivery'] for i in items)
    total_qty = sum(i['qty'] for i in items)
    on_purchase_dram = int(total_purchase_yuan * real_rate)
    margin_dram = final_dram - on_purchase_dram
    profit_dram = int(margin_dram * 0.9) if invoice else margin_dram

    # Счёт клиенту в драмах (то же что final_dram)
    client_bill_dram = int(final_dram)

    # === СООБЩЕНИЕ КЛИЕНТУ (ПЕРВОЕ СООБЩЕНИЕ) ===
    client_msg = f"{order_code}\n"
    client_msg += "📋 ВАШ ЗАКАЗ:\n\n"

    lines_yuan = []
    for i in items:
        line_total = i['price'] * i['qty'] + i['delivery']
        lines_yuan.append(line_total)
        client_msg += f"• {i['name']}:\n{i['price']}×{i['qty']}+{i['delivery']} = {line_total} ¥\n\n"

    # Формула расчёта
    if len(lines_yuan) > 1:
        formula = "+".join([str(int(l)) for l in lines_yuan])
        formula += f"={int(total_yuan)}"
    else:
        formula = str(int(total_yuan))

    if commission_pct > 0:
        with_commission_yuan = int(final_dram / client_rate)
        formula += f"+{commission_pct}%={with_commission_yuan}x{int(client_rate)}={int(final_dram)}֏"
    elif fixed_amount > 0:
        formula += f"x{int(client_rate)}={int(total_yuan * client_rate)} (фикс {fixed_amount})"
    else:
        formula += f"x{int(client_rate)}={int(final_dram)}֏"

    client_msg += f"{formula}\n"
    client_msg += f"━━━━━━━━━━━━\n\n"
    client_msg += f"💰 ИТОГО (¥): {total_yuan}\n"

    if commission_pct > 0:
        with_commission_yuan = int(final_dram / client_rate)
        client_msg += f"📈 С комиссией ({commission_text}): {with_commission_yuan} ¥\n"
    elif fixed_amount > 0:
        client_msg += f"📈 Фиксированная комиссия: {fixed_amount} ֏\n"

    client_msg += f"💳 К ОПЛАТЕ: {int(final_dram)} ֏"

    # Отправляем КЛИЕНТУ (первое сообщение)
    if hasattr(update, 'callback_query'):
        chat_id = update.callback_query.message.chat_id
        await context.bot.send_message(chat_id=chat_id, text=client_msg)
    else:
        await update.message.reply_text(client_msg)

    # === СООБЩЕНИЕ МНЕ (ВТОРОЕ СООБЩЕНИЕ) ===
    my_msg = f"💼 МОЙ РАСЧЁТ:\n"
    my_msg += f"{order_code}\n\n"
    my_msg += f"На закупку(¥): {int(total_purchase_yuan)}\n"
    my_msg += f"На закупку(֏): {on_purchase_dram} ֏\n"
    my_msg += f"Маржа: {margin_dram} ֏\n"
    my_msg += f"Инвойс: {'Да' if invoice else 'Нет'}\n"
    my_msg += f"💵 Прибыль: {profit_dram} ֏"

    # Отправляем МНЕ (второе сообщение)
    if hasattr(update, 'callback_query'):
        chat_id = update.callback_query.message.chat_id
        await context.bot.send_message(chat_id=chat_id, text=my_msg)
    else:
        await update.message.reply_text(my_msg)

    # === СОХРАНЕНИЕ В NOTION ===
    try:
        # Формируем описание товара для поля Title
        items_description = "; ".join([f"{i['name']} (×{i['qty']})" for i in items])

        notion_properties = {
            "Описание товара": {"title": [{"text": {"content": items_description}}]},
            "Количество": {"number": int(total_qty)},
            "Цена клиенту (¥)": {"number": float(items[0]['price'])},
            "Цена закупки (¥)": {"number": float(items[0]['purchase'])},
            "Доставка (¥)": {"number": float(sum(i['delivery'] for i in items))},
            "ИТОГО (¥)": {"number": float(total_yuan)},
            "На закупку (¥)": {"number": float(total_purchase_yuan)},
            "Комиссия %": {"number": float(commission_pct)},
            "С комиссией (¥)": {"number": float(final_dram / client_rate) if client_rate > 0 else 0},
            "К ОПЛАТЕ (֏)": {"number": int(final_dram)},
            "Курс клиенту": {"number": float(client_rate)},
            "Курс реальный": {"number": float(real_rate)},
            "Закупка реальная (¥)": {"number": float(total_purchase_yuan)},
            "На закупку (֏)": {"number": int(on_purchase_dram)},
            "Маржа (֏)": {"number": int(margin_dram)},
            "Инвойс": {"select": {"name": "Да" if invoice else "Нет"}},
            "Прибыль (֏)": {"number": int(profit_dram)},
            "Счёт клиенту (֏)": {"number": int(client_bill_dram)},
            "Клиент": {"select": {"name": client}},
            "Дата": {"date": {"start": datetime.now().isoformat()}},
            "Статус": {"select": {"name": "Поиск — жду цену"}},
        }

        # Добавляем фиксированную комиссию если есть
        if fixed_amount > 0:
            notion_properties["Фиксированная комиссия (֏)"] = {"number": int(fixed_amount)}

        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=notion_properties
        )

        # Подтверждение сохранения
        if hasattr(update, 'callback_query'):
            chat_id = update.callback_query.message.chat_id
            await context.bot.send_message(chat_id=chat_id, text="✅ Сохранено в Notion")
        else:
            await update.message.reply_text("✅ Сохранено в Notion")

    except Exception as e:
        logging.error(f"Notion error: {e}")
        error_msg = f"⚠️ Ошибка Notion: {str(e)[:300]}"
        if hasattr(update, 'callback_query'):
            chat_id = update.callback_query.message.chat_id
            await context.bot.send_message(chat_id=chat_id, text=error_msg)
        else:
            await update.message.reply_text(error_msg)

    # Чистим
    del orders[uid]

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in orders:
        del orders[uid]
    await update.message.reply_text('❌ Отменено')
    return ConversationHandler.END

async def nayti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.replace('/nayti', '').strip()
    if not q:
        await update.message.reply_text('🔍 /nayти текст')
        return

    try:
        res = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={"or": [
                {"property": "Описание товара", "title": {"contains": q}},
                {"property": "Клиент", "select": {"equals": q}}
            ]}
        )

        results = res.get('results', [])
        if not results:
            await update.message.reply_text('Ничего не найдено')
            return

        msg = f"🔍 Найдено: {len(results)}\n\n"
        for r in results[:5]:
            p = r['properties']
            name = p['Описание товара']['title'][0]['text']['content'] if p['Описание товара']['title'] else '-'
            client = p.get('Клиент', {}).get('select', {}).get('name', '-')
            msg += f"{name}\nКлиент: {client}\n\n"

        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# === ЗАПУСК ===

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Обработчики
    app.add_handler(CommandHandler('start', start))

    conv = ConversationHandler(
        entry_points=[CommandHandler('zakaz', zakaz)],
        states={
            INVOICE: [CallbackQueryHandler(invoice_cb, pattern='^inv_')],
            PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_qty)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_price)],
            DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_delivery)],
            PURCHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_purchase)],
            MORE: [CallbackQueryHandler(more_cb, pattern='^more_')],
            CLIENT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_rate)],
            REAL_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_real_rate)],
            PERCENT: [CallbackQueryHandler(percent_cb, pattern='^pct_')],
            FIXED_COMMISSION: [CallbackQueryHandler(fixed_commission_cb, pattern='^fix_')],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler(['nayti', 'find'], nayti))

    app.run_polling()

if __name__ == '__main__':
    main()
