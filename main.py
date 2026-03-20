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
INVOICE, PRODUCT_NAME, QUANTITY, PRICE, DELIVERY, PURCHASE, PHOTO, MORE, CLIENT_RATE, REAL_RATE, PERCENT = range(11)

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
        await update.message.reply_text('📷 Пришли фото:')
        return PHOTO
    except:
        await update.message.reply_text('❌ Число! Закупка:')
        return PURCHASE

async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    
    if update.message.photo:
        orders[uid]['current']['photo'] = update.message.photo[-1].file_id
        
        keyboard = [[InlineKeyboardButton("✅ Да", callback_data='more_yes'), 
                     InlineKeyboardButton("❌ Нет", callback_data='more_no')]]
        await update.message.reply_text('Ещё товар?', reply_markup=InlineKeyboardMarkup(keyboard))
        return MORE
    else:
        await update.message.reply_text('📷 Нужно фото!')
        return PHOTO

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
            # Фикс 10000
            final_dram = 10000
            commission = 0
            await show_result(update, context, total_yuan, final_dram, "Фикс 10000", commission)
            return ConversationHandler.END
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

async def percent_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    
    pct = 3 if query.data == 'pct_3' else 5
    orders[uid]['commission'] = pct
    
    # Пересчёт
    items = orders[uid]['items']
    client_rate = orders[uid]['client_rate']
    total_yuan = sum(i['price'] * i['qty'] + i['delivery'] for i in items)
    base_dram = total_yuan * client_rate
    final_dram = int(base_dram * (1 + pct / 100))
    
    await show_result(update, context, total_yuan, final_dram, f"+{pct}%", pct)
    return ConversationHandler.END

async def show_result(update, context, total_yuan, final_dram, commission_text, commission_pct):
    uid = update.effective_user.id
    items = orders[uid]['items']
    client = orders[uid]['client']
    client_rate = orders[uid]['client_rate']
    real_rate = orders[uid]['real_rate']
    invoice = orders[uid]['invoice']
    
    # Расчёт для меня
    total_purchase_yuan = sum(i['purchase'] * i['qty'] for i in items)
    on_purchase_dram = int(total_purchase_yuan * real_rate)
    margin_dram = final_dram - on_purchase_dram
    profit_dram = int(margin_dram * 0.9) if invoice else margin_dram
    
    # Сообщение КЛИЕНТУ
    client_msg = "📋 ВАШ ЗАКАЗ:\n\n"
    for i in items:
        line_total = i['price'] * i['qty'] + i['delivery']
        client_msg += f"• {i['name']}:\n{i['price']}×{i['qty']}+{i['delivery']} = {line_total} ¥\n\n"
    
    client_msg += f"━━━━━━━━━━━━\n\n"
    client_msg += f"💰 ИТОГО (¥): {total_yuan}\n"
    if commission_pct > 0:
        client_msg += f"📈 С комиссией ({commission_text}): {int(final_dram / client_rate)} ¥\n"
    client_msg += f"💳 К ОПЛАТЕ: {final_dram} ֏"
    
    # Сообщение МНЕ
    my_msg = f"\n\n💼 МОЙ РАСЧЁТ:\n"
    my_msg += f"На закупку: {on_purchase_dram} ֏\n"
    my_msg += f"Счёт клиенту: {final_dram} ֏\n"
    my_msg += f"Маржа: {margin_dram} ֏\n"
    my_msg += f"Инвойс: {'Да' if invoice else 'Нет'}\n"
    my_msg += f"💵 Прибыль: {profit_dram} ֏"
    
    # Отправляем
    if hasattr(update, 'callback_query'):
        await update.callback_query.edit_message_text(client_msg + my_msg)
    else:
        await update.message.reply_text(client_msg + my_msg)
    
    # Сохраняем в Notion
    try:
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                "Название товара": {"title": [{"text": {"content": "; ".join([i['name'] for i in items])}}]},
                "Количество": {"number": sum(i['qty'] for i in items)},
                "Цена клиенту (¥)": {"number": int(items[0]['price'])},
                "Цена закупки (¥)": {"number": int(items[0]['purchase'])},
                "Доставка (¥)": {"number": sum(i['delivery'] for i in items)},
                "ИТОГО (¥)": {"number": total_yuan},
                "Комиссия %": {"number": commission_pct},
                "С комиссией (¥)": {"number": int(final_dram / client_rate)},
                "К ОПЛАТЕ (֏)": {"number": final_dram},
                "Курс клиенту": {"number": client_rate},
                "Курс реальный": {"number": real_rate},
                "На закупку (֏)": {"number": on_purchase_dram},
                "Маржа (֏)": {"number": margin_dram},
                "Инвойс": {"select": {"name": "Да" if invoice else "Нет"}},
                "Прибыль (֏)": {"number": profit_dram},
            }
        )
    except Exception as e:
        logging.error(f"Notion error: {e}")
        if hasattr(update, 'callback_query'):
            await update.callback_query.message.reply_text(f"⚠️ Notion: {e}")
        else:
            await update.message.reply_text(f"⚠️ Notion: {e}")
    
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
                {"property": "Название товара", "title": {"contains": q}},
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
            name = p['Название товара']['title'][0]['text']['content'] if p['Название товара']['title'] else '-'
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
            PHOTO: [MessageHandler(filters.PHOTO, get_photo)],
            MORE: [CallbackQueryHandler(more_cb, pattern='^more_')],
            CLIENT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_rate)],
            REAL_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_real_rate)],
            PERCENT: [CallbackQueryHandler(percent_cb, pattern='^pct_')],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    app.add_handler(conv)
    app.add_handler(CommandHandler(['nayti', 'find'], nayti))
    
    app.run_polling()

if __name__ == '__main__':
    main()
