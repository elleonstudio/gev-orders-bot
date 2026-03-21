import os
import logging
import math
from datetime import datetime
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = os.getenv('NOTION_DATABASE_ID')
PACKAGES_DATABASE_ID = "32a8c4d1fb0e806ebb98f5995704d0e5"  # База пакетов

notion = Client(auth=NOTION_TOKEN)

# Тарифы FILLX (₽ за короб)
TARIFFS = {
    "Москва": 350,
    "Электросталь": 350,
    "Коледино": 350,
    "Тула": 500,
    "Рязань": 700,
    "Котовск": 750,
    "Казань": 800,
    "Казань (8-10)": 650,
    "Краснодар": 1100,
    "Невинномысск": 1100,
    "Новосемейкино": 1000,
    "Воронеж": 800,
    "Пенза": 800,
    "Владимир": 700,
    "Сарапул": 1200,
    "Екатеринбург": 1400,
    "Екатеринбург (6-10)": 1200,
}

# Состояния
(INVOICE, PRODUCT_NAME, QUANTITY, PRICE, PURCHASE, DELIVERY_FACTORY,
 DIMENSIONS, PACKAGE_SELECT, MORE, NEED_FF,  # NEW: спрашиваем нужен ли FF
 CLIENT_RATE, REAL_RATE, RUB_RATE,
 FF_PICKUP, FF_BOX_PRICE, FF_STICKER_PRICE, THERMAL_PAPER_QTY, FF_WORK_PRICE,
 WAREHOUSE, BOX_COUNT, CRATING) = range(22)

orders = {}
packages_cache = None

def get_code(name):
    return f"{name.upper()}-{datetime.now().strftime('%d%m%y')}"

def fmt(n):
    if n == int(n):
        return str(int(n))
    return f"{n:.1f}"

def calculate_boxes(length, width, height, total_qty):
    """Расчёт сколько товаров влезет в короб 60×40×40"""
    box_volume = 60 * 40 * 40
    item_volume = length * width * height
    if item_volume == 0:
        return 0, 0
    items_per_box = box_volume // item_volume
    boxes_needed = math.ceil(total_qty / items_per_box) if items_per_box > 0 else 1
    return items_per_box, boxes_needed

async def get_packages_from_notion():
    """Получить список пакетов из Notion"""
    global packages_cache
    if packages_cache:
        return packages_cache
    
    try:
        res = notion.databases.query(database_id=PACKAGES_DATABASE_ID)
        packages = []
        for page in res.get('results', []):
            props = page['properties']
            pkg = {
                'id': page['id'],
                'name': props.get('Название', {}).get('title', [{}])[0].get('text', {}).get('content', ''),
                'length': props.get('Длина', {}).get('number', 0),
                'width': props.get('Ширина', {}).get('number', 0),
                'height': props.get('Высота', {}).get('number', 0),
                'price': props.get('Цена', {}).get('number', 0),
            }
            if pkg['name'] and pkg['length'] > 0:
                packages.append(pkg)
        
        packages.sort(key=lambda x: x['length'] * x['width'] * x['height'])
        packages_cache = packages
        return packages
    except Exception as e:
        logging.error(f"Error fetching packages: {e}")
        return []

def find_best_package(packages, item_length, item_width, item_height):
    """Найти лучший пакет для товара"""
    item_dims = sorted([item_length, item_width, item_height], reverse=True)
    
    best_pkg = None
    min_volume_diff = float('inf')
    
    for pkg in packages:
        pkg_dims = sorted([pkg['length'], pkg['width'], pkg['height']], reverse=True)
        
        if (pkg_dims[0] >= item_dims[0] and 
            pkg_dims[1] >= item_dims[1] and 
            pkg_dims[2] >= item_dims[2]):
            
            item_vol = item_length * item_width * item_height
            pkg_vol = pkg['length'] * pkg['width'] * pkg['height']
            volume_diff = pkg_vol - item_vol
            
            if volume_diff < min_volume_diff:
                min_volume_diff = volume_diff
                best_pkg = pkg
    
    return best_pkg

async def get_client_orders(client_name):
    """Получить последние заказы клиента из Notion"""
    try:
        res = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={"property": "Клиент", "select": {"equals": client_name}},
            sorts=[{"timestamp": "created_time", "direction": "descending"}],
            page_size=5
        )
        return res.get('results', [])
    except:
        return []

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Привет! Бот для заказов GS.\n\n'
        '/zakaz [имя] — новый заказ\n'
        '/nayti [текст] — найти заказ\n'
        '/cancel — отменить'
    )

async def zakaz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    parts = text.split(maxsplit=1)
    
    if len(parts) < 2:
        await update.message.reply_text('Укажи имя: /zakaz Армен')
        return ConversationHandler.END
    
    name = parts[1].strip()
    uid = update.effective_user.id
    
    history = await get_client_orders(name)
    
    if history:
        keyboard = []
        for order in history[:3]:
            props = order['properties']
            product = props.get('Описание товара', {}).get('rich_text', [{}])[0].get('text', {}).get('content', 'Товар')
            qty = props.get('Количество', {}).get('number', 0)
            date = order.get('created_time', '')[:10]
            keyboard.append([InlineKeyboardButton(f"🔄 {product} ×{int(qty)} ({date})", callback_data=f'repeat_{order["id"]}')])
        
        keyboard.append([InlineKeyboardButton("✏️ Новый товар", callback_data='new_product')])
        
        await update.message.reply_text(
            f'Заказ для: {name}\n\nНайдены предыдущие заказы:',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(f'Заказ для: {name}\n\nНазвание товара:')
    
    orders[uid] = {'client': name, 'items': [], 'current': {}, 'invoice': False, 'need_ff': True}
    return INVOICE if not history else PRODUCT_NAME

async def repeat_order_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    data = query.data
    
    if data == 'new_product':
        await query.edit_message_text('Название товара:')
        return PRODUCT_NAME
    
    order_id = data.replace('repeat_', '')
    try:
        order = notion.pages.retrieve(page_id=order_id)
        props = order['properties']
        
        orders[uid]['current'] = {
            'name': props.get('Описание товара', {}).get('rich_text', [{}])[0].get('text', {}).get('content', ''),
            'qty': props.get('Количество', {}).get('number', 0),
            'price': props.get('Цена клиенту (CNY)', {}).get('number', 0),
            'purchase': props.get('Цена закупки (CNY)', {}).get('number', 0),
            'delivery_factory': props.get('Доставка (CNY)', {}).get('number', 0),
            'dimensions': props.get('Размеры (Д×Ш×В)', {}).get('rich_text', [{}])[0].get('text', {}).get('content', ''),
        }
        
        c = orders[uid]['current']
        await query.edit_message_text(
            f'Повторить заказ:\n'
            f'📦 {c["name"]} × {int(c["qty"])}\n'
            f'💰 Цена клиенту: {fmt(c["price"])} ¥\n'
            f'🏭 Закупка: {fmt(c["purchase"])} ¥\n'
            f'📐 Размеры: {c["dimensions"]}\n\n'
            f'Всё верно?',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, продолжить", callback_data='confirm_repeat')],
                [InlineKeyboardButton("✏️ Изменить количество", callback_data='change_qty')],
                [InlineKeyboardButton("🔄 Сначала", callback_data='new_product')]
            ])
        )
        return INVOICE
    except:
        await query.edit_message_text('Ошибка загрузки заказа. Название товара:')
        return PRODUCT_NAME

async def confirm_repeat_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    
    keyboard = [[InlineKeyboardButton("Да", callback_data='inv_yes'), 
                 InlineKeyboardButton("Нет", callback_data='inv_no')]]
    await query.edit_message_text('Инвойс?', reply_markup=InlineKeyboardMarkup(keyboard))
    return INVOICE

async def change_qty_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text('Введи новое количество:')
    return QUANTITY

async def invoice_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    
    if query.data == 'inv_yes':
        orders[uid]['invoice'] = True
    else:
        orders[uid]['invoice'] = False
    
    if orders[uid]['current'].get('name'):
        await query.edit_message_text('Введи размеры 1 шт (Д Ш В в см, через пробел):\nНапример: 15 10 8')
        return DIMENSIONS
    
    await query.edit_message_text('Название товара:')
    return PRODUCT_NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    orders[uid]['current']['name'] = update.message.text
    await update.message.reply_text('Количество:')
    return QUANTITY

async def get_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['current']['qty'] = int(update.message.text)
        await update.message.reply_text('Цена клиенту за 1 шт (CNY):')
        return PRICE
    except:
        await update.message.reply_text('Число! Количество:')
        return QUANTITY

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['current']['price'] = float(update.message.text)
        await update.message.reply_text('Закупка у фабрики за 1 шт (CNY):')
        return PURCHASE
    except:
        await update.message.reply_text('Число! Цена:')
        return PRICE

async def get_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['current']['purchase'] = float(update.message.text)
        await update.message.reply_text('Доставка фабрика→твой склад (CNY):')
        return DELIVERY_FACTORY
    except:
        await update.message.reply_text('Число! Закупка:')
        return PURCHASE

async def get_delivery_factory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['current']['delivery_factory'] = float(update.message.text)
        await update.message.reply_text('Введи размеры 1 шт (Д Ш В в см, через пробел):\nНапример: 15 10 8')
        return DIMENSIONS
    except:
        await update.message.reply_text('Число! Доставка:')
        return DELIVERY_FACTORY

async def get_dimensions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    
    try:
        dims = [float(x) for x in text.split()]
        if len(dims) != 3:
            raise ValueError
        
        l, w, h = dims
        orders[uid]['current']['dimensions'] = f"{int(l)}×{int(w)}×{int(h)}"
        orders[uid]['current']['volume'] = l * w * h
        
        qty = orders[uid]['current']['qty']
        items_per_box, boxes = calculate_boxes(l, w, h, qty)
        orders[uid]['current']['boxes'] = boxes
        
        packages = await get_packages_from_notion()
        best_pkg = find_best_package(packages, l, w, h)
        
        if best_pkg:
            pkg_total = best_pkg['price'] * qty
            orders[uid]['current']['package'] = best_pkg
            orders[uid]['current']['package_price'] = best_pkg['price']
            orders[uid]['current']['package_total'] = pkg_total
            
            await update.message.reply_text(
                f'📐 Размеры товара: {int(l)}×{int(w)}×{int(h)} см\n'
                f'📦 В короб 60×40×40 влезет: ~{items_per_box} шт\n'
                f'📦 Нужно коробок: {boxes}\n\n'
                f'📦 Подобран пакет: {best_pkg["name"]}\n'
                f'   Размер: {int(best_pkg["length"])}×{int(best_pkg["width"])}×{int(best_pkg["height"])} см\n'
                f'   Цена: {best_pkg["price"]} ¥ × {qty} шт = {fmt(pkg_total)} ¥\n\n',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ OK", callback_data='pkg_ok')],
                    [InlineKeyboardButton("📋 Выбрать другой пакет", callback_data='pkg_select')],
                    [InlineKeyboardButton("💬 Своя цена", callback_data='pkg_custom')]
                ])
            )
            return PACKAGE_SELECT
        else:
            await update.message.reply_text(
                f'📐 Размеры: {int(l)}×{int(w)}×{int(h)} см\n'
                f'📦 В короб влезет: ~{items_per_box} шт\n'
                f'📦 Коробок: {boxes}\n\n'
                f'⚠️ Подходящий пакет не найден. Введи цену пакетов вручную (¥):'
            )
            orders[uid]['current']['package'] = None
            orders[uid]['current']['package_price'] = 0
            return FF_STICKER_PRICE
            
    except:
        await update.message.reply_text('Неверный формат. Введи 3 числа через пробел:\n15 10 8')
        return DIMENSIONS

async def package_select_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    data = query.data
    
    if data == 'pkg_ok':
        keyboard = [[InlineKeyboardButton("Да", callback_data='more_yes'), 
                     InlineKeyboardButton("Нет", callback_data='more_no')]]
        await query.edit_message_text(
            f'📦 Пакет: {orders[uid]["current"]["package"]["name"]}\n'
            f'💰 {orders[uid]["current"]["package_total"]} ¥\n\n'
            f'Ещё товар?',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return MORE
        
    elif data == 'pkg_select':
        packages = await get_packages_from_notion()
        keyboard = []
        for pkg in packages[:10]:
            keyboard.append([InlineKeyboardButton(
                f'{pkg["name"]} ({int(pkg["length"])}×{int(pkg["width"])}×{int(pkg["height"])}) — {pkg["price"]}¥',
                callback_data=f'pkg_{pkg["id"]}'
            )])
        keyboard.append([InlineKeyboardButton("💬 Своя цена", callback_data='pkg_custom')])
        
        await query.edit_message_text('Выбери пакет:', reply_markup=InlineKeyboardMarkup(keyboard))
        return PACKAGE_SELECT
        
    elif data == 'pkg_custom':
        await query.edit_message_text('Введи цену пакетов вручную (¥):')
        return FF_STICKER_PRICE
        
    elif data.startswith('pkg_'):
        pkg_id = data.replace('pkg_', '')
        packages = await get_packages_from_notion()
        selected_pkg = next((p for p in packages if p['id'] == pkg_id), None)
        
        if selected_pkg:
            qty = orders[uid]['current']['qty']
            pkg_total = selected_pkg['price'] * qty
            orders[uid]['current']['package'] = selected_pkg
            orders[uid]['current']['package_price'] = selected_pkg['price']
            orders[uid]['current']['package_total'] = pkg_total
            
            keyboard = [[InlineKeyboardButton("Да", callback_data='more_yes'), 
                         InlineKeyboardButton("Нет", callback_data='more_no')]]
            await query.edit_message_text(
                f'📦 Выбран: {selected_pkg["name"]}\n'
                f'💰 {selected_pkg["price"]} ¥ × {qty} = {fmt(pkg_total)} ¥\n\n'
                f'Ещё товар?',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return MORE

async def more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    
    if query.data == 'more_yes':
        orders[uid]['items'].append(orders[uid]['current'])
        orders[uid]['current'] = {}
        await query.message.reply_text('Название товара:')
        return PRODUCT_NAME
    else:
        orders[uid]['items'].append(orders[uid]['current'])
        # NEW: Спрашиваем нужен ли FF в Китае
        total_qty = sum(i['qty'] for i in orders[uid]['items'])
        keyboard = [
            [InlineKeyboardButton("✅ Да, нужен FF в Китае", callback_data='ff_yes')],
            [InlineKeyboardButton("❌ Нет, пропустить FF", callback_data='ff_no')]
        ]
        await query.message.reply_text(
            f'Всего товаров: {total_qty} шт\n\n'
            f'Нужен фулфилмент (FF) в Китае?',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return NEED_FF

async def need_ff_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    
    if query.data == 'ff_yes':
        orders[uid]['need_ff'] = True
        await query.edit_message_text('FF Китай — Забор груза (¥):')
        return FF_PICKUP
    else:
        orders[uid]['need_ff'] = False
        # Пропускаем все FF-вопросы, сразу к курсам
        await query.edit_message_text('Курс клиенту ¥→драм (например 58):')
        return CLIENT_RATE

async def get_ff_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['ff_pickup'] = float(update.message.text)
        boxes = sum(i.get('boxes', 1) for i in orders[uid]['items'])
        await update.message.reply_text(f'FF — Коробки 60×40×40 (¥ за шт, всего {boxes} шт):')
        return FF_BOX_PRICE
    except:
        await update.message.reply_text('Число!')
        return FF_PICKUP

async def get_ff_box(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        price = float(update.message.text)
        boxes = sum(i.get('boxes', 1) for i in orders[uid]['items'])
        orders[uid]['ff_boxes'] = price * boxes
        total_qty = sum(i['qty'] for i in orders[uid]['items'])
        await update.message.reply_text(f'FF — Термонаклейки (¥ за шт × {total_qty}):')
        return FF_STICKER_PRICE
    except:
        await update.message.reply_text('Число!')
        return FF_BOX_PRICE

async def get_ff_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        price = float(update.message.text)
        total_qty = sum(i['qty'] for i in orders[uid]['items'])
        orders[uid]['ff_stickers'] = price * total_qty
        # Термобумага: фиксированная цена 0.016¥, спрашиваем только количество
        await update.message.reply_text(
            f'FF — Термобумага (0.016¥ за шт)\n\n'
            f'Сколько термобумаги на 1 товар? (шт)\nНапример: 1'
        )
        return THERMAL_PAPER_QTY
    except:
        await update.message.reply_text('Число!')
        return FF_STICKER_PRICE

async def get_thermal_paper_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        qty_per_item = float(update.message.text)
        total_qty = sum(i['qty'] for i in orders[uid]['items'])
        # Фиксированная цена 0.016¥
        orders[uid]['ff_thermal_paper'] = 0.016 * qty_per_item * total_qty
        
        await update.message.reply_text(f'FF — Работа (¥ за 1 шт × {total_qty}):')
        return FF_WORK_PRICE
    except:
        await update.message.reply_text('Число!')
        return THERMAL_PAPER_QTY

async def get_ff_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        price = float(update.message.text)
        total_qty = sum(i['qty'] for i in orders[uid]['items'])
        orders[uid]['ff_work'] = price * total_qty
        
        # После FF вопросов — курс клиенту
        await update.message.reply_text('Курс клиенту ¥→драм (например 58):')
        return CLIENT_RATE
    except:
        await update.message.reply_text('Число!')
        return FF_WORK_PRICE

async def get_client_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['client_rate'] = float(update.message.text)
        await update.message.reply_text('Курс реальный ¥→драм (например 55):')
        return REAL_RATE
    except:
        await update.message.reply_text('Число! Курс:')
        return CLIENT_RATE

async def get_real_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['real_rate'] = float(update.message.text)
        await update.message.reply_text('Курс ₽→драм (например 5.8):')
        return RUB_RATE
    except:
        await update.message.reply_text('Число! Курс:')
        return REAL_RATE

async def get_rub_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        orders[uid]['rub_rate'] = float(update.message.text)
        
        # Показываем список складов
        keyboard = []
        cities = [c for c in TARIFFS.keys() if '(' not in c]  # Только основные города
        for i in range(0, len(cities), 2):
            row = [InlineKeyboardButton(cities[i], callback_data=f'wh_{cities[i]}')]
            if i + 1 < len(cities):
                row.append(InlineKeyboardButton(cities[i+1], callback_data=f'wh_{cities[i+1]}'))
            keyboard.append(row)
        
        await update.message.reply_text('Выбери склад РФ:', reply_markup=InlineKeyboardMarkup(keyboard))
        return WAREHOUSE
    except:
        await update.message.reply_text('Число! Курс:')
        return RUB_RATE

async def warehouse_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    
    city = query.data.replace('wh_', '')
    orders[uid]['warehouse'] = city
    
    boxes = sum(i.get('boxes', 1) for i in orders[uid]['items'])
    
    # Специальные случаи для диапазонов
    if city == "Казань" and boxes >= 8:
        city = "Казань (8-10)"
    elif city == "Екатеринбург":
        if boxes >= 6:
            city = "Екатеринбург (6-10)"
    
    price_per_box = TARIFFS.get(city, 1000)
    orders[uid]['fillx_delivery'] = price_per_box * boxes
    
    await query.edit_message_text(f'{city}: {price_per_box}₽ × {boxes} короб = {price_per_box * boxes}₽\n\n'
                                  f'Сколько коробок всего? (подтверди или исправь):')
    return BOX_COUNT

async def get_box_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        boxes = int(update.message.text)
        city = orders[uid]['warehouse']
        
        # Пересчёт с учётом диапазонов
        if city == "Казань":
            city = "Казань (8-10)" if boxes >= 8 else "Казань"
        elif city == "Екатеринбург":
            city = "Екатеринбург (6-10)" if boxes >= 6 else "Екатеринбург"
            
        price_per_box = TARIFFS.get(city, 1000)
        orders[uid]['fillx_delivery'] = price_per_box * boxes
        orders[uid]['total_boxes'] = boxes
        
        keyboard = [[InlineKeyboardButton("Да", callback_data='crate_yes'),
                     InlineKeyboardButton("Нет", callback_data='crate_no')]]
        await update.message.reply_text('FILLX — Снятие обрешётки (2000₽)?', reply_markup=InlineKeyboardMarkup(keyboard))
        return CRATING
    except:
        await update.message.reply_text('Число!')
        return BOX_COUNT

async def crating_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    
    orders[uid]['fillx_crating'] = 2000 if query.data == 'crate_yes' else 0
    
    # Расчёты
    await calculate_and_show_result(update, context)
    return ConversationHandler.END

async def calculate_and_show_result(update, context):
    uid = update.effective_user.id if hasattr(update, 'effective_user') else update.callback_query.from_user.id
    items = orders[uid]['items']
    client = orders[uid]['client']
    client_rate = orders[uid]['client_rate']
    real_rate = orders[uid]['real_rate']
    rub_rate = orders[uid]['rub_rate']
    invoice = orders[uid]['invoice']
    need_ff = orders[uid].get('need_ff', True)
    
    order_code = get_code(client)
    total_qty = sum(i['qty'] for i in items)
    total_boxes = orders[uid].get('total_boxes', sum(i.get('boxes', 1) for i in items))
    
    # Товары
    total_yuan = sum(i['price'] * i['qty'] for i in items)
    total_purchase_yuan = sum(i['purchase'] * i['qty'] for i in items)
    delivery_factory_yuan = sum(i.get('delivery_factory', 0) for i in items)
    
    # Пакеты
    total_packages_yuan = sum(i.get('package_total', 0) for i in items)
    
    # FF Китай (только если нужен)
    if need_ff:
        ff_pickup = orders[uid].get('ff_pickup', 0)
        ff_boxes = orders[uid].get('ff_boxes', 0)
        ff_stickers = orders[uid].get('ff_stickers', 0)
        ff_thermal_paper = orders[uid].get('ff_thermal_paper', 0)
        ff_work = orders[uid].get('ff_work', 0)
        ff_total_yuan = ff_pickup + ff_boxes + total_packages_yuan + ff_stickers + ff_thermal_paper + ff_work
    else:
        ff_pickup = ff_boxes = ff_stickers = ff_thermal_paper = ff_work = 0
        ff_total_yuan = 0
    
    ff_total_amd = int(ff_total_yuan * real_rate)
    
    # FILLX
    fillx_pickup = 7000
    fillx_crating = orders[uid].get('fillx_crating', 0)
    fillx_receiving = 1000 * total_boxes
    fillx_delivery = orders[uid].get('fillx_delivery', 0)
    fillx_unpacking = 500 * total_boxes
    fillx_total_rub = fillx_pickup + fillx_crating + fillx_receiving + fillx_delivery + fillx_unpacking
    fillx_total_amd = int(fillx_total_rub * rub_rate)
    
    # Итоги
    client_total_yuan = total_yuan + ff_total_yuan
    client_total_amd = int(client_total_yuan * client_rate)
    
    purchase_amd = int((total_purchase_yuan + delivery_factory_yuan) * real_rate)
    total_costs_amd = purchase_amd + ff_total_amd + fillx_total_amd
    profit_amd = client_total_amd - total_costs_amd
    
    # Сообщение для КЛИЕНТА
    client_msg = f"{order_code}\n\n"
    for i in items:
        client_msg += f"• {i['name']} × {int(i['qty'])}\n"
    client_msg += f"\nТовары: {fmt(total_yuan)} ¥\n"
    if need_ff:
        client_msg += f"Фулфилмент Китай: {fmt(ff_total_yuan)} ¥\n"
    client_msg += f"━━━━━━━━━━━━\n"
    client_msg += f"ИТОГО ¥: {fmt(client_total_yuan)}\n"
    client_msg += f"К ОПЛАТЕ: {client_total_amd} AMD"
    
    # Сообщение для ТЕБЯ
    my_msg = f"📊 {order_code}\n\n"
    my_msg += f"─── ЗАКУПКА ───\n"
    my_msg += f"Товар: {fmt(total_purchase_yuan)}¥ = {purchase_amd} AMD\n"
    my_msg += f"Доставка фабрика→FF: {fmt(delivery_factory_yuan)}¥\n\n"
    
    if need_ff:
        my_msg += f"─── FF КИТАЙ ({fmt(ff_total_yuan)}¥ × {real_rate}) ───\n"
        my_msg += f"Забор: {fmt(ff_pickup)}¥\n"
        my_msg += f"Коробки: {fmt(ff_boxes)}¥\n"
        my_msg += f"Пакеты: {fmt(total_packages_yuan)}¥\n"
        my_msg += f"Наклейки: {fmt(ff_stickers)}¥\n"
        my_msg += f"Термобумага: {fmt(ff_thermal_paper)}¥\n"
        my_msg += f"Работа: {fmt(ff_work)}¥\n"
        my_msg += f"Итого FF: {ff_total_amd} AMD\n\n"
    
    my_msg += f"─── FILLX ({fillx_total_rub}₽ × {rub_rate}) ───\n"
    my_msg += f"Забор IOB: 7000₽\n"
    my_msg += f"Обрешётка: {fillx_crating}₽\n"
    my_msg += f"Приёмка: {fillx_receiving}₽\n"
    my_msg += f"Доставка: {fillx_delivery}₽\n"
    my_msg += f"Разбор: {fillx_unpacking}₽\n"
    my_msg += f"Итого FILLX: {fillx_total_amd} AMD\n\n"
    
    my_msg += f"─── ИТОГО ───\n"
    my_msg += f"Выручка: {client_total_amd} AMD\n"
    my_msg += f"Расходы: {total_costs_amd} AMD\n"
    my_msg += f"💰 ПРИБЫЛЬ: {profit_amd} AMD"
    
    # Отправка
    if hasattr(update, 'callback_query'):
        chat_id = update.callback_query.message.chat_id
        await context.bot.send_message(chat_id=chat_id, text=client_msg)
        await context.bot.send_message(chat_id=chat_id, text=my_msg)
    else:
        await update.message.reply_text(client_msg)
        await update.message.reply_text(my_msg)
    
    # Сохранение в Notion
    try:
        items_description = "; ".join([f"{i['name']} (x{int(i['qty'])})" for i in items])
        dimensions_str = items[0].get('dimensions', '')
        
        notion_properties = {
            "Код заказа": {"title": [{"text": {"content": order_code}}]},
            "Описание товара": {"rich_text": [{"text": {"content": items_description}}]},
            "Количество": {"number": int(total_qty)},
            "Размеры (Д×Ш×В)": {"rich_text": [{"text": {"content": dimensions_str}}]},
            "Цена клиенту (CNY)": {"number": float(items[0]['price'])},
            "Цена закупки (CNY)": {"number": float(items[0]['purchase'])},
            "Доставка (CNY)": {"number": float(delivery_factory_yuan)},
            "ИТОГО (CNY)": {"number": float(total_yuan)},
            "На закупку (CNY)": {"number": float(total_purchase_yuan)},
            "Курс клиенту": {"number": float(client_rate)},
            "Курс реальный": {"number": float(real_rate)},
            "Курс ₽→драм": {"number": float(rub_rate)},
            "Закупка реальная (AMD)": {"number": purchase_amd},
            " К ОПЛАТЕ (AMD)": {"number": client_total_amd},
            "Прибыль (AMD)": {"number": profit_amd},
            "Клиент": {"select": {"name": client}},
            "Склад РФ": {"select": {"name": orders[uid].get('warehouse', 'Москва')}},
            # FF Китай
            "FF Забор груза": {"number": ff_pickup},
            "FF Коробки": {"number": ff_boxes},
            "FF Пакеты": {"number": total_packages_yuan},
            "FF Термонаклейки": {"number": ff_stickers},
            "FF Термобумага": {"number": ff_thermal_paper},
            "FF Работа": {"number": ff_work},
            "FF Итого (CNY)": {"number": ff_total_yuan},
            "FF Итого (AMD)": {"number": ff_total_amd},
            # FILLX
            "FILLX Забор IOB": {"number": 7000},
            "FILLX Обрешётка": {"number": fillx_crating},
            "FILLX Приёмка": {"number": fillx_receiving},
            "FILLX Доставка": {"number": fillx_delivery},
            "FILLX Разбор": {"number": fillx_unpacking},
            "FILLX Итого (₽)": {"number": fillx_total_rub},
            "FILLX Итого (AMD)": {"number": fillx_total_amd},
            # Остальное
            " Инвойс": {"select": {"name": "Да" if invoice else "Нет"}},
            "Статус": {"select": {"name": "Новый"}},
        }
        
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
        error_msg = f"❌ Ошибка Notion: {str(e)[:400]}"
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
    await update.message.reply_text('Отменено')
    return ConversationHandler.END

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler('start', start))
    
    conv = ConversationHandler(
        entry_points=[CommandHandler('zakaz', zakaz)],
        states={
            INVOICE: [
                CallbackQueryHandler(invoice_cb, pattern='^inv_'),
                CallbackQueryHandler(confirm_repeat_cb, pattern='^confirm_repeat$'),
                CallbackQueryHandler(change_qty_cb, pattern='^change_qty$'),
            ],
            PRODUCT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_name),
                CallbackQueryHandler(repeat_order_cb, pattern='^(repeat_|new_product)')
            ],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_qty)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_price)],
            PURCHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_purchase)],
            DELIVERY_FACTORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_delivery_factory)],
            DIMENSIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_dimensions)],
            PACKAGE_SELECT: [
                CallbackQueryHandler(package_select_cb, pattern='^(pkg_ok|pkg_select|pkg_custom|pkg_)')
            ],
            MORE: [CallbackQueryHandler(more_cb, pattern='^more_')],
            NEED_FF: [CallbackQueryHandler(need_ff_cb, pattern='^ff_')],  # NEW
            CLIENT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_client_rate)],
            REAL_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_real_rate)],
            RUB_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_rub_rate)],
            FF_PICKUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_ff_pickup)],
            FF_BOX_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_ff_box)],
            FF_STICKER_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_ff_sticker)],
            THERMAL_PAPER_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_thermal_paper_qty)],
            FF_WORK_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_ff_work)],
            WAREHOUSE: [CallbackQueryHandler(warehouse_cb, pattern='^wh_')],
            BOX_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_box_count)],
            CRATING: [CallbackQueryHandler(crating_cb, pattern='^crate_')],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    app.add_handler(conv)
    
    app.run_polling()

if __name__ == '__main__':
    main()
