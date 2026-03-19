import os
import logging
from datetime import datetime
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = os.getenv('NOTION_DATABASE_ID')

notion = Client(auth=NOTION_TOKEN)

CLIENT_NAME, PRODUCT_NAME, QUANTITY, CLIENT_PRICE, DELIVERY_PRICE, PURCHASE_PRICE, PHOTO, MORE_PRODUCTS, CLIENT_RATE, REAL_RATE = range(10)

orders_data = {}

def generate_order_code(client_name):
    today = datetime.now()
    date_str = today.strftime("%d%m%y")
    return f"{client_name.upper()}-{date_str}-1"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Привет! Я бот для учёта заказов.\n\n'
        'Команды:\n'
        '/zakaz [имя клиента] - создать новый заказ\n'
        '/nayti [запрос] - найти заказы\n'
    )

async def start_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text
    parts = message_text.split(maxsplit=1)
    
    if len(parts) < 2:
        await update.message.reply_text('Укажи имя клиента: /zakaz Петя')
        return ConversationHandler.END
    
    client_name = parts[1].strip()
    user_id = update.effective_user.id
    
    orders_data[user_id] = {
        'client': client_name,
        'products': [],
        'current_product': {}
    }
    
    await update.message.reply_text(f'Создаём заказ для {client_name}.\n\nВведи название товара (подробно):')
    return PRODUCT_NAME

async def get_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    orders_data[user_id]['current_product']['name'] = update.message.text
    await update.message.reply_text('Количество:')
    return QUANTITY

async def get_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        quantity = int(update.message.text)
        if quantity <= 0:
            await update.message.reply_text('Введи число больше 0:')
            return QUANTITY
        orders_data[user_id]['current_product']['quantity'] = quantity
        await update.message.reply_text('Цена за 1 шт. клиенту (¥):')
        return CLIENT_PRICE
    except ValueError:
        await update.message.reply_text('Введи целое число:')
        return QUANTITY

async def get_client_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        price_per_unit = float(update.message.text)
        quantity = orders_data[user_id]['current_product'].get('quantity', 1)
        total_price = price_per_unit * quantity
        orders_data[user_id]['current_product']['client_price'] = total_price
        orders_data[user_id]['current_product']['price_per_unit'] = price_per_unit
        await update.message.reply_text(f'Цена за {quantity} шт: {total_price}¥\n\nДоставка (¥):')
        return DELIVERY_PRICE
    except ValueError:
        await update.message.reply_text('Введи число:')
        return CLIENT_PRICE

async def get_delivery_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        price = float(update.message.text)
        orders_data[user_id]['current_product']['delivery'] = price
        await update.message.reply_text('Закупка реальная (¥):')
        return PURCHASE_PRICE
    except ValueError:
        await update.message.reply_text('Введи число:')
        return DELIVERY_PRICE

async def get_purchase_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        price = float(update.message.text)
        orders_data[user_id]['current_product']['purchase'] = price
        await update.message.reply_text('Пришли фото товара (или напиши "нет"):')
        return PHOTO
    except ValueError:
        await update.message.reply_text('Введи число:')
        return PURCHASE_PRICE

async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
        orders_data[user_id]['current_product']['photo'] = photo_file_id
        keyboard = [
            [InlineKeyboardButton("Да", callback_data='yes')],
            [InlineKeyboardButton("Нет", callback_data='no')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text('Ещё товар?', reply_markup=reply_markup)
        return MORE_PRODUCTS
    elif update.message.text and update.message.text.lower() == 'нет':
        orders_data[user_id]['current_product']['photo'] = None
        keyboard = [
            [InlineKeyboardButton("Да", callback_data='yes')],
            [InlineKeyboardButton("Нет", callback_data='no')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text('Ещё товар?', reply_markup=reply_markup)
        return MORE_PRODUCTS
    else:
        await update.message.reply_text('Пришли фото или напиши "нет":')
        return PHOTO

async def more_products_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    orders_data[user_id]['products'].append(orders_data[user_id]['current_product'])
    
    if query.data == 'yes':
        orders_data[user_id]['current_product'] = {}
        await query.edit_message_text('Введи название товара (подробно):')
        return PRODUCT_NAME
    else:
        await query.edit_message_text('Курс клиенту? (например: 58)')
        return CLIENT_RATE

async def get_client_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        rate = float(update.message.text)
        orders_data[user_id]['client_rate'] = rate
        await update.message.reply_text('Курс реальный? (например: 55)')
        return REAL_RATE
    except ValueError:
        await update.message.reply_text('Введи число:')
        return CLIENT_RATE

async def get_real_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        rate = float(update.message.text)
        orders_data[user_id]['real_rate'] = rate
        order_code = generate_order_code(orders_data[user_id]['client'])
        orders_data[user_id]['order_code'] = order_code
        
        # Считаем итоговые суммы
        total_qty = 0
        total_client = 0
        total_purchase = 0
        products_text = []
        
        for i, prod in enumerate(orders_data[user_id]['products'], 1):
            qty = prod.get('quantity', 1)
            total_qty += qty
            total_client += prod['client_price'] + prod['delivery']
            total_purchase += prod['purchase']
            products_text.append(f"{i}. {prod['name']} (×{qty})")
        
        # Считаем маржу
        margin = total_client - total_purchase
        
        try:
            notion.pages.create(
                parent={"database_id": NOTION_DATABASE_ID},
                properties={
                    "Клиент": {"select": {"name": orders_data[user_id]['client']}},
                    "Описание товара": {"rich_text": [{"text": {"content": "; ".join(products_text)}}]},
                    "Количество": {"number": total_qty},
                    "Цена клиенту (¥)": {"number": int(prod['price_per_unit'])},
                    "Цена закупки (¥)": {"number": int(prod['purchase'] / qty)},
                    "Закупка реальная (֏)": {"number": total_purchase},
                    "Счёт клиенту (֏)": {"number": total_client},
                    "Курс клиенту": {"number": orders_data[user_id]['client_rate']},
                    "Курс реальный": {"number": rate},
                    "Маржа (֏)": {"number": margin},
                    "Статус": {"select": {"name": "Поиск — жду цену"}},
                }
            )
            
            summary = f"""✅ Заказ создан!

Клиент: {orders_data[user_id]['client']}
Товаров: {len(orders_data[user_id]['products'])}
Общее кол-во: {total_qty} шт
Счёт клиенту: {total_client}֏
Закупка: {total_purchase}¥
Маржа: {margin}֏

Сохранено в Notion!"""
            await update.message.reply_text(summary)
            
        except Exception as e:
            logging.error(f"Notion error: {e}")
            await update.message.reply_text(f'Ошибка сохранения в Notion: {e}')
        
        del orders_data[user_id]
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text('Введи число:')
        return REAL_RATE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in orders_data:
        del orders_data[user_id]
    await update.message.reply_text('Заказ отменён.')
    return ConversationHandler.END

async def search_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.replace('/nayti', '').replace('/find', '').strip()
    if not query:
        await update.message.reply_text('Укажи что искать: /nayti nike')
        return
    try:
        response = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={
                "or": [
                    {"property": "Описание товара", "rich_text": {"contains": query}},
                    {"property": "Клиент", "select": {"equals": query}}
                ]
            }
        )
        results = response.get('results', [])
        if not results:
            await update.message.reply_text(f'Ничего не найдено по запросу: {query}')
            return
        text = f'Найдено {len(results)} заказов:\n\n'
        for page in results[:5]:
            props = page['properties']
            desc = props['Описание товара']['rich_text'][0]['text']['content'] if props['Описание товара']['rich_text'] else 'Без описания'
            client = props['Клиент']['select']['name'] if props['Клиент']['select'] else 'Неизвестно'
            status = props['Статус']['select']['name'] if props['Статус']['select'] else '—'
            text += f'{desc}\nКлиент: {client}\nСтатус: {status}\n\n'
        await update.message.reply_text(text)
    except Exception as e:
        logging.error(f"Search error: {e}")
        await update.message.reply_text(f'Ошибка поиска: {e}')

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Простые команды для теста
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('test', lambda u, c: u.message.reply_text('Тест работает!')))
    
    # ConversationHandler для заказов
    order_conv = ConversationHandler(
        entry_points=[CommandHandler('zakaz', start_order)],
        states={
            PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_product_name)],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_quantity)],
            CLIENT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_price)],
            DELIVERY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_delivery_price)],
            PURCHASE_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_purchase_price)],
            PHOTO: [MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, get_photo)],
            MORE_PRODUCTS: [CallbackQueryHandler(more_products_callback)],
            CLIENT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_rate)],
            REAL_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_real_rate)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    application.add_handler(order_conv)
    application.add_handler(CommandHandler(['nayti', 'find'], search_orders))
    
    application.run_polling()

if __name__ == '__main__':
    main()
