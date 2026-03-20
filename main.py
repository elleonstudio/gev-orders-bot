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
    """Формат: NAME-DDMMYY"""
    return f"{name.upper()}-{datetime.now().strftime('%d%m%y')}"

def fmt(n):
    """Формат числа: 1 знак после точки если дробное, иначе целое"""
    if n == int(n):
        return str(int(n))
    return f"{n:.1f}"

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
        await update.message.reply_text('💰 Цена клиенту за 1 шт (CNY):')
        return PRICE
    except:
        await update.message.reply_text('❌ Число! Количество:')
        return QUANTITY

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['current']['price'] = float(update.message.text)
        await update.message.reply_text('🚚 Доставка (CNY):')
        return DELIVERY
    except:
        await update.message.reply_text('❌ Число! Цена:')
        return PRICE

async def get_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['current']['delivery'] = float(update.message.text)
        await update.message.reply_text('🏭 Закупка у фабрики за 1 шт (CNY):')
        return PURCHASE
    except:
        await update.message.reply_text('❌ Число! Доставка:')
        return DELIVERY

async def get_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['current']['purchase'] = float(update.message.text)
        
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
    
    orders[uid]['items'].append(orders[uid]['current'])
    
    if query.data == 'more_yes':
        orders[uid]['current'] = {}
        await query.message.reply_text('📝 Название товара:')
        return PRODUCT_NAME
    else:
        await query.message.reply_text('💱 Курс клиенту (например 58):')
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
        
        items = orders[uid]['items']
        client_rate = orders[uid]['client_rate']
        
        total_yuan = sum(i['price'] * i['qty'] + i['delivery'] for i in items)
        
        commission_yuan = total_yuan * 0.03
        commission_dram = commission_yuan * client_rate
        
        if commission_dram < 10000:
            keyboard = [
                [InlineKeyboardButton("10000 AMD", callback_data='fix_10000')], 
                [InlineKeyboardButton("15000 AMD", callback_data='fix_15000')]
            ]
            msg = f"📊 Итого: {fmt(total_yuan)} CNY\nКомиссия 3% = {int(commission_dram)} AMD (мало)\n\nВыбери фиксированную комиссию:"
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
            return FIXED_COMMISSION
        else:
            keyboard = [[InlineKeyboardButton("+3%", callback_data='pct_3'), 
                         InlineKeyboardButton("+5%", callback_data='pct_5')]]
            msg = f"📊 Итого: {fmt(total_yuan)} CNY\nКомиссия 3% = {int(commission_dram)} AMD\n\nВыбери комиссию:"
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
    orders[uid]['commission'] = 0
    
    items = orders[uid]['items']
    client_rate = orders[uid]['client_rate']
    total_yuan = sum(i['price'] * i['qty'] + i['delivery'] for i in items)
    
    base_dram = int(total_yuan * client_rate)
    final_dram = base_dram + fixed_amount
    
    await show_result(update, context, total_yuan, final_dram, f"Фикс {fixed_amount}", 0, fixed_amount)
    return ConversationHandler.END

async def percent_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    
    pct = 3 if query.data == 'pct_3' else 5
    orders[uid]['commission'] = pct
    orders[uid]['fixed_commission'] = 0
    
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
    
    order_code = get_code(client)
    
    total_purchase_yuan = sum(i['purchase'] * i['qty'] + i['delivery'] for i in items)
    total_qty = sum(i['qty'] for i in items)
    on_purchase_dram = int(total_purchase_yuan * real_rate)
    
    if fixed_amount > 0:
        margin_dram = fixed_amount
        profit_dram = fixed_amount
    else:
        margin_dram = final_dram - on_purchase_dram
        profit_dram = int(margin_dram * 0.9) if invoice else margin_dram
    
    client_bill_dram = int(final_dram)
    
    # === СООБЩЕНИЕ КЛИЕНТУ ===
    client_msg = f"{order_code}\n\n"
    
    lines_yuan = []
    for i in items:
        line_total = i['price'] * i['qty'] + i['delivery']
        lines_yuan.append(line_total)
        client_msg += f"• {i['name']}:\n{fmt(i['price'])}×{int(i['qty'])}+{fmt(i['delivery'])} = {fmt(line_total)} CNY\n\n"
    
    if len(lines_yuan) > 1:
        formula = "+".join([fmt(l) for l in lines_yuan])
        formula += f"={fmt(total_yuan)}"
    else:
        formula = fmt(total_yuan)
    
    if commission_pct > 0:
        with_commission_yuan = final_dram / client_rate
        formula += f"+{commission_pct}%={fmt(with_commission_yuan)}x{int(client_rate)}={int(final_dram)}AMD"
    elif fixed_amount > 0:
        base_dram = int(total_yuan * client_rate)
        formula += f"x{int(client_rate)}={base_dram}+{fixed_amount}={int(final_dram)}"
    else:
        formula += f"x{int(client_rate)}={int(final_dram)}AMD"
    
    client_msg += f"{formula}\n"
    client_msg += f"━━━━━━━━━━━━\n\n"
    client_msg += f"💰 ИТОГО (CNY): {fmt(total_yuan)}\n"
    
    if commission_pct > 0:
        with_commission_yuan = final_dram / client_rate
        client_msg += f"📈 С комиссией ({commission_text}): {fmt(with_commission_yuan)} CNY\n"
    elif fixed_amount > 0:
        client_msg += f"📈 Фиксированная комиссия: {fixed_amount} AMD\n"
    
    client_msg += f"💳 К ОПЛАТЕ: {int(final_dram)} AMD"
    
    if hasattr(update, 'callback_query'):
        chat_id = update.callback_query.message.chat_id
        await context.bot.send_message(chat_id=chat_id, text=client_msg)
    else:
        await update.message.reply_text(client_msg)
    
    # === СООБЩЕНИЕ МНЕ ===
    my_msg = f"💼 МОЙ РАСЧЁТ:\n"
    my_msg += f"{order_code}\n\n"
    my_msg += f"На закупку(CNY): {fmt(total_purchase_yuan)}\n"
    my_msg += f"На закупку(AMD): {on_purchase_dram} AMD\n"
    my_msg += f"Маржа: {margin_dram} AMD\n"
    my_msg += f"Инвойс: {'Да' if invoice else 'Нет'}\n"
    my_msg += f"💵 Прибыль: {profit_dram} AMD"
    
    if hasattr(update, 'callback_query'):
        chat_id = update.callback_query.message.chat_id
        await context.bot.send_message(chat_id=chat_id, text=my_msg)
    else:
        await update.message.reply_text(my_msg)
    
    # === NOTION ===
    try:
        items_description = "; ".join([f"{i['name']} (×{int(i['qty'])})" for i in items])
        
        notion_properties = {
            "Описание товара": {"rich_text": [{"text": {"content": items_description}}]},
            "Количество": {"number": int(total_qty)},
            "Цена клиенту (CNY)": {"number": float(items[0]['price'])},
            "Цена закупки (CNY)": {"number": float(items[0]['purchase'])},
            "Доставка (CNY)": {"number": float(sum(i['delivery'] for i in items))},
            "ИТОГО (CNY)": {"number": float(total_yuan)},
            "На закупку (CNY)": {"number": float(total_purchase_yuan)},
            "Комиссия": {"number": float(commission_pct)},
            "С комиссией (CNY)": {"number": float(final_dram / client_rate) if client_rate > 0 else 0},
            "К ОПЛАТЕ (AMD)": {"number": int(final_dram)},
            "Курс клиенту": {"number": float(client_rate)},
            "Курс реальный": {"number": float(real_rate)},
            "Закупка реальная (AMD)": {"number": int(on_purchase_dram)},
            "На закупку (AMD)": {"number": int(on_purchase_dram)},
            "Маржа (AMD)": {"number": int(margin_dram)},
            "Инвойс": {"select": {"name": "Да" if invoice else "Нет"}},
            "Прибыль (AMD)": {"number": int(profit_dram)},
            "Счёт клиенту (AMD)": {"number": int(client_bill_dram)},
            "Клиент": {"select": {"name": client}},
            "Статус": {"select": {"name": "Поиск — жду цену"}},
        }
        
        if fixed_amount > 0:
            notion_properties["Фиксированная комиссия (AMD)"] = {"number": int(fixed_amount)}
        
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=notion_properties
        )
        
        if hasattr(update, 'callback_query'):
            chat_id = update.callback_query.message.chat_id
            await context.bot.send_message(chat_id=chat_id, text="✅ Сохранено в Notion")
        else:
            await update.message.reply_text("✅ Сохранено в Notion")
            
    except Exception as e:
        logging.error(f"Notion error: {e}")
        error_msg = f"⚠️ Ошибка Notion: {str(e)[:400]}"
        if hasattr(update, 'callback_query'):
            chat_id = update.callback_query.message.chat_id
            await context.bot.send_message(chat_id=chat_id, text=error_msg)
        else:
            await update.message.reply_text(error_msg)
    
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
                {"property": "Описание товара", "rich_text": {"contains": q}},
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
            name = p['Описание товара']['rich_text'][0]['text']['content'] if p['Описание товара']['rich_text'] else '-'
            client = p.get('Клиент', {}).get('select', {}).get('name', '-')
            msg += f"{name}\nКлиент: {client}\n\n"
        
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# === ЗАПУСК ===

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
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
