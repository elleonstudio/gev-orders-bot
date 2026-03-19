import os
import logging
import re
from datetime import datetime
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Получаем токены из переменных окружения
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = os.getenv('NOTION_DATABASE_ID')

# Инициализация Notion клиента
notion = Client(auth=NOTION_TOKEN)

# Состояния для ConversationHandler
CLIENT_NAME, PRODUCT_NAME, CLIENT_PRICE, DELIVERY_PRICE, PURCHASE_PRICE, PHOTO, MORE_PRODUCTS, CLIENT_RATE, REAL_RATE = range(9)

# Временное хранилище заказов
orders_data = {}

def generate_order_code(client_name):
    """Генерирует код заказа: ИМЯ-ДДММГГ-НОМЕР"""
    today = datetime.now()
    date_str = today.strftime("%d%m%y")
    # Простой счётчик для демо (в реальном боте нужна база данных)
    return f"{client_name.upper()}-{date_str}-1"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    await update.message.reply_text(
        'Привет! Я бот для учёта заказов.\n\n'
        'Команды:\n'
        '/заказ [имя клиента] - создать новый заказ\n'
        '/найти [запрос] - найти заказы\n'
        '/[код] - найти по коду карго\n'
    )

async def start_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало создания заказа: /заказ Имя"""
    message_text = update.message.text
    parts = message_text.split(maxsplit=1)
    
    if len(parts) < 2:
        await update.message.reply_text('Укажи имя клиента: /заказ Петя')
        return ConversationHandler.END
    
    client_name = parts[1].strip()
    user_id = update.effective_user.id
    
    # Инициализируем заказ
    orders_data[user_id] = {
        'client': client_name,
        'products': [],
        'current_product': {}
    }
    
    await update.message.reply_text(f'Создаём заказ для {client_name}.\n\nВведи название товара (подробно):')
    return PRODUCT_NAME

async def get_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем название товара"""
    user_id = update.effective_user.id
    orders_data[user_id]['current_product']['name'] = update.message.text
    
    await update.message.reply_text('Цена клиенту (¥):')
    return CLIENT_PRICE

async def get_client_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем цену клиенту"""
    user_id = update.effective_user.id
    try:
        price = float(update.message.text)
        orders_data[user_id]['current_product']['client_price'] = price
        
        await update.message.reply_text('Доставка (¥):')
        return DELIVERY_PRICE
    except ValueError:
        await update.message.reply_text('Введи число:')
        return CLIENT_PRICE

async def get_delivery_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем цену доставки"""
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
    """Получаем цену закупки"""
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
    """Получаем фото"""
    user_id = update.effective_user.id
    
    if update.message.photo:
        # Сохраняем file_id фото
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
    """Обработка кнопки Ещё товар"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    # Сохраняем текущий товар
    orders_data[user_id]['products'].append(orders_data[user_id]['current_product'])
    
    if query.data == 'yes':
        # Новый товар
        orders_data[user_id]['current_product'] = {}
        await query.edit_message_text('Введи название товара (подробно):')
        return PRODUCT_NAME
    else:
        # Переходим к курсам
        await query.edit_message_text('Курс клиенту? (например: 58)')
        return CLIENT_RATE

async def get_client_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем курс клиенту"""
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
    """Получаем реальный курс и сохраняем в Notion"""
    user_id = update.effective_user.id
    try:
        rate = float(update.message.text)
        orders_data[user_id]['real_rate'] = rate
        
        # Генерируем код заказа
        order_code = generate_order_code(orders_data[user_id]['client'])
        orders_data[user_id]['order_code'] = order_code
        
        # Формируем данные для Notion
        products_text = []
        client_prices = []
        purchase_prices = []
        
        for i, prod in enumerate(orders_data[user_id]['products'], 1):
            products_text.append(f"{i}. {prod['name']}")
            client_prices.append(f"{prod['client_price']}+{prod['delivery']}")
            purchase_prices.append(str(prod['purchase']))
        
        # Создаём страницу в Notion
        try:
            new_page = notion.pages.create(
                parent={"database_id": NOTION_DATABASE_ID},
                properties={
                    "Код заказа": {"title": [{"text": {"content": order_code}}]},
                    "Клиент": {"select": {"name": orders_data[user_id]['client']}},
                    "Описание товаров": {"rich_text": [{"text": {"content": "; ".join(products_text)}}]},
                    "Цены клиенту": {"rich_text": [{"text": {"content": "; ".join(client_prices)}}]},
                    "Цены закупки": {"rich_text": [{"text": {"content": "; ".join(purchase_prices)}}]},
                    "Курсы": {"rich_text": [{"text": {"content": f"{orders_data[user_id]['client_rate']} / {rate}"}}]},
                    "Статус": {"select": {"name": "🔍 Поиск"}},
                    "Код карго": {"rich_text": [{"text": {"content": ""}}]},
                }
            )
            
            # Отправляем подтверждение
            summary = f"""✅ Заказ создан: {order_code}

Клиент: {orders_data[user_id]['client']}
Товаров: {len(orders_data[user_id]['products'])}
Курсы: {orders_data[user_id]['client_rate']} / {rate}

Сохранено в Notion!"""
            
            await update.message.reply_text(summary)
            
        except Exception as e:
            logging.error(f"Notion error: {e}")
            await update.message.reply_text(f'Ошибка сохранения в Notion: {e}')
        
        # Очищаем данные
        del orders_data[user_id]
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text('Введи число:')
        return REAL_RATE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена заказа"""
    user_id = update.effective_user.id
    if user_id in orders_data:
        del orders_data[user_id]
    await update.message.reply_text('Заказ отменён.')
    return ConversationHandler.END

async def search_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск заказов: /найти запрос"""
    query = update.message.text.replace('/найти', '').replace('/find', '').strip()
    
    if not query:
        await update.message.reply_text('Укажи что искать: /найти nike')
        return
    
    try:
        # Ищем в Notion
        response = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={
                "or": [
                    {
                        "property": "Описание товаров",
                        "rich_text": {"contains": query}
                    },
                    {
                        "property": "Клиент",
                        "select": {"equals": query}
                    },
                    {
                        "property": "Код заказа",
                        "title": {"contains": query}
                    }
                ]
            }
        )
        
        results = response.get('results', [])
        
        if not results:
            await update.message.reply_text(f'Ничего не найдено по запросу: {query}')
            return
        
        # Формируем ответ
        text = f'Найдено {len(results)} заказов:\n\n'
        for page in results[:5]:  # Показываем первые 5
            props = page['properties']
            code = props['Код заказа']['title'][0]['text']['content'] if props['Код заказа']['title'] else 'Без кода'
            client = props['Клиент']['select']['name'] if props['Клиент']['select'] else 'Неизвестно'
            status = props['Статус']['select']['name'] if props['Статус']['select'] else '—'
            
            text += f'📦 {code}\nКлиент: {client}\nСтатус: {status}\n\n'
        
        await update.message.reply_text(text)
        
    except Exception as e:
        logging.error(f"Search error: {e}")
        await update.message.reply_text(f'Ошибка поиска: {e}')

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Conversation handler для создания заказа
    order_conv = ConversationHandler(
        entry_points=[CommandHandler('заказ', start_order)],
        states={
            PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_product_name)],
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
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(order_conv)
    application.add_handler(CommandHandler(['найти', 'find'], search_orders))
    
    application.run_polling()

if __name__ == '__main__':
    main()
