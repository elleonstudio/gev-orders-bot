import os
import logging
import math
import json
from datetime import datetime
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = os.getenv('NOTION_DATABASE_ID')
PACKAGES_DATABASE_ID = "32a8c4d1fb0e806ebb98f5995704d0e5"

notion = Client(auth=NOTION_TOKEN)

TARIFFS = {
    "Москва": 350, "Электросталь": 350, "Коледино": 350, "Тула": 500,
    "Рязань": 700, "Котовск": 750, "Казань": 800, "Казань (8-10)": 650,
    "Краснодар": 1100, "Невинномысск": 1100, "Новосемейкино": 1000,
    "Воронеж": 800, "Пенза": 800, "Владимир": 700, "Сарапул": 1200,
    "Екатеринбург": 1400, "Екатеринбург (6-10)": 1200,
}

orders = {}
sessions_file = "/tmp/gs_sessions.json"

def get_code(name):
    return f"{name.upper()}-{datetime.now().strftime('%d%m%y')}"

def fmt(n):
    if n is None:
        return "0"
    if n == int(n):
        return str(int(n))
    return f"{n:.1f}"

def calculate_boxes(length, width, height, total_qty):
    box_volume = 60 * 40 * 40
    item_volume = length * width * height
    if item_volume == 0:
        return 0, 0
    items_per_box = box_volume // item_volume
    boxes_needed = math.ceil(total_qty / items_per_box) if items_per_box > 0 else 1
    return items_per_box, boxes_needed

def save_session():
    try:
        with open(sessions_file, 'w') as f:
            json.dump(orders, f, default=str)
    except:
        pass

def load_session():
    try:
        with open(sessions_file, 'r') as f:
            return json.load(f)
    except:
        return {}

async def get_packages_from_notion():
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
        return packages
    except Exception as e:
        logger.error(f"Error fetching packages: {e}")
        return []

def find_best_package(packages, item_length, item_width, item_height):
    item_dims = sorted([item_length, item_width, item_height], reverse=True)
    best_pkg = None
    min_volume_diff = float('inf')
    for pkg in packages:
        pkg_dims = sorted([pkg['length'], pkg['width'], pkg['height']], reverse=True)
        if (pkg_dims[0] >= item_dims[0] and pkg_dims[1] >= item_dims[1] and pkg_dims[2] >= item_dims[2]):
            item_vol = item_length * item_width * item_height
            pkg_vol = pkg['length'] * pkg['width'] * pkg['height']
            volume_diff = pkg_vol - item_vol
            if volume_diff < min_volume_diff:
                min_volume_diff = volume_diff
                best_pkg = pkg
    return best_pkg

async def get_client_from_notion(client_name):
    """Получаем последний заказ клиента из Notion"""
    try:
        res = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={"property": "Клиент", "select": {"equals": client_name}},
            sorts=[{"timestamp": "created_time", "direction": "descending"}],
            page_size=1
        )
        results = res.get('results', [])
        if results:
            props = results[0]['properties']
            return {
                'client_rate': props.get('Курс клиенту', {}).get('number', 58),
                'real_rate': props.get('Курс реальный', {}).get('number', 55),
                'rub_rate': props.get('Курс ₽→драм', {}).get('number', 5.8),
            }
    except Exception as e:
        logger.error(f"Error fetching client: {e}")
    return None

# ======== МЕНЮ ========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu = (
        "📋 <b>GS Orders Bot</b>\n\n"
        "<b>Команды:</b>\n"
        "/zakaz [имя] - Расчёт продукта (закупка + комиссия)\n"
        "/ff - FF в Китае (пакеты, забор, коробки, работа)\n"
        "/dostavka - Доставка РФ (FILLX, склады)\n"
        "/nayti - Найти заказ\n"
        "/debug - Проверить Notion\n"
        "/cancel - Отменить\n\n"
        "Начни с /zakaz [имя клиента]"
    )
    await update.message.reply_text(menu, parse_mode='HTML')

# ======== /ZAKAZ ========

Z_NAME, Z_QTY, Z_PRICE, Z_PURCHASE, Z_DELIVERY, Z_DIMS, Z_MORE, Z_CLIENT_RATE, Z_REAL_RATE, Z_COMMISSION = range(10)

async def cmd_zakaz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/zakaz [имя] — ввод товаров + комиссия"""
    uid = str(update.effective_user.id)
    text = update.message.text
    parts = text.split(maxsplit=1)
    
    if len(parts) < 2:
        await update.message.reply_text('Укажи имя клиента: /zakaz Армен')
        return ConversationHandler.END
    
    client_name = parts[1].strip()
    
    # Проверяем есть ли клиент в базе
    client_data = await get_client_from_notion(client_name)
    
    orders[uid] = {
        'client': client_name,
        'items': [],
        'type': 'zakaz'
    }
    
    if client_data:
        orders[uid]['client_rate'] = client_data['client_rate']
        orders[uid]['real_rate'] = client_data['real_rate']
        orders[uid]['rub_rate'] = client_data['rub_rate']
        await update.message.reply_text(
            f'Клиент: <b>{client_name}</b>\n'
            f'Курсы из базы: клиент {client_data["client_rate"]}, '
            f'реальный {client_data["real_rate"]}, руб {client_data["rub_rate"]}\n\n'
            f'Название товара:',
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(f'Новый клиент: <b>{client_name}</b>\n\nНазвание товара:', parse_mode='HTML')
    
    return Z_NAME

async def z_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    orders[uid]['current'] = {'name': update.message.text.strip()}
    await update.message.reply_text('Количество:')
    return Z_QTY

async def z_get_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['qty'] = int(update.message.text)
        await update.message.reply_text('Цена клиенту за 1 шт (CNY):')
        return Z_PRICE
    except:
        await update.message.reply_text('Число! Количество:')
        return Z_QTY

async def z_get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['price'] = float(update.message.text)
        await update.message.reply_text('Закупка у фабрики за 1 шт (CNY):')
        return Z_PURCHASE
    except:
        await update.message.reply_text('Число!')
        return Z_PRICE

async def z_get_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['purchase'] = float(update.message.text)
        await update.message.reply_text('Доставка фабрика→твой склад (CNY):')
        return Z_DELIVERY
    except:
        await update.message.reply_text('Число!')
        return Z_PURCHASE

async def z_get_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['current']['delivery_factory'] = float(update.message.text)
        await update.message.reply_text('Введи размеры 1 шт (Д Ш В в см):\nНапример: 15 10 8')
        return Z_DIMS
    except:
        await update.message.reply_text('Число!')
        return Z_DELIVERY

async def z_get_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.strip()
    try:
        dims = [float(x) for x in text.split()]
        if len(dims) != 3:
            raise ValueError
        l, w, h = dims
        current = orders[uid]['current']
        current['dimensions'] = f"{int(l)}×{int(w)}×{int(h)}"
        current['dims'] = (l, w, h)
        qty = current['qty']
        items_per_box, boxes = calculate_boxes(l, w, h, qty)
        current['boxes'] = boxes
        current['items_per_box'] = items_per_box
        
        keyboard = [[InlineKeyboardButton("Да", callback_data='z_more_yes'), 
                     InlineKeyboardButton("Нет", callback_data='z_more_no')]]
        await update.message.reply_text(
            f'📐 Размеры: {int(l)}×{int(w)}×{int(h)} см\n'
            f'📦 В короб влезет: ~{items_per_box} шт\n'
            f'📦 Коробок: {boxes}\n\n'
            f'Ещё товар?',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return Z_MORE
    except:
        await update.message.reply_text('Неверный формат. Введи 3 числа:\n15 10 8')
        return Z_DIMS

async def z_more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data == 'z_more_yes':
        orders[uid]['items'].append(orders[uid]['current'])
        orders[uid]['current'] = {}
        await query.edit_message_text('Название товара:')
        return Z_NAME
    else:
        orders[uid]['items'].append(orders[uid]['current'])
        
        # Если курсы уже есть из базы, спрашиваем подтверждение
        if 'client_rate' in orders[uid]:
            await query.edit_message_text(
                f'Курс клиенту ¥→драм ({orders[uid]["client_rate"]}):'
            )
        else:
            await query.edit_message_text('Курс клиенту ¥→драм (например 58):')
        return Z_CLIENT_RATE

async def z_client_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        # Если ввели новое значение, обновляем
        text = update.message.text.strip()
        if text:
            orders[uid]['client_rate'] = float(text)
        
        if 'real_rate' in orders[uid]:
            await update.message.reply_text(f'Курс реальный ¥→драм ({orders[uid]["real_rate"]}):')
        else:
            await update.message.reply_text('Курс реальный ¥→драм (например 55):')
        return Z_REAL_RATE
    except:
        await update.message.reply_text('Число! Курс клиенту:')
        return Z_CLIENT_RATE

async def z_real_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        text = update.message.text.strip()
        if text:
            orders[uid]['real_rate'] = float(text)
        
        # Считаем комиссию от (закупка + доставка фабрика) × курс клиента
        items = orders[uid]['items']
        client_rate = orders[uid]['client_rate']
        total_purchase = sum(i['purchase'] * i['qty'] for i in items)
        delivery = sum(i.get('delivery_factory', 0) for i in items)
        
        base_amd = int((total_purchase + delivery) * client_rate)
        comm_3 = int(base_amd * 0.03)
        
        if comm_3 < 10000:
            keyboard = [
                [InlineKeyboardButton("10000 драм", callback_data='z_comm_10000')],
                [InlineKeyboardButton("15000 драм", callback_data='z_comm_15000')]
            ]
            await update.message.reply_text(
                f'Комиссия: 3% = {comm_3} драм (меньше 10000)\nВыбери фикс:',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            comm_5 = int(base_amd * 0.05)
            keyboard = [
                [InlineKeyboardButton(f"3% = {comm_3} драм", callback_data='z_comm_3')],
                [InlineKeyboardButton(f"5% = {comm_5} драм", callback_data='z_comm_5')]
            ]
            await update.message.reply_text('Выбери комиссию:', reply_markup=InlineKeyboardMarkup(keyboard))
        return Z_COMMISSION
    except:
        await update.message.reply_text('Число! Курс реальный:')
        return Z_REAL_RATE

async def z_commission_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    data = query.data
    
    items = orders[uid]['items']
    client_rate = orders[uid]['client_rate']
    total_purchase = sum(i['purchase'] * i['qty'] for i in items)
    delivery = sum(i.get('delivery_factory', 0) for i in items)
    
    base_amd = int((total_purchase + delivery) * client_rate)
    total_price = sum(i['price'] * i['qty'] for i in items)
    
    if data == 'z_comm_3':
        orders[uid]['commission'] = int(base_amd * 0.03)
        orders[uid]['commission_type'] = '3%'
    elif data == 'z_comm_5':
        orders[uid]['commission'] = int(base_amd * 0.05)
        orders[uid]['commission_type'] = '5%'
    elif data == 'z_comm_10000':
        orders[uid]['commission'] = 10000
        orders[uid]['commission_type'] = '10000'
    elif data == 'z_comm_15000':
        orders[uid]['commission'] = 15000
        orders[uid]['commission_type'] = '15000'
    
    commission = orders[uid]['commission']
    
    # Показываем результат /zakaz
    msg = f"📊 <b>Расчёт закупки</b>\n\n"
    for i in items:
        msg += f"• {i['name']} × {int(i['qty'])}\n"
    msg += f"\nТовар: {fmt(total_purchase)}¥\n"
    msg += f"Доставка: {fmt(delivery)}¥\n"
    msg += f"━━━━━━━━━━━━\n"
    msg += f"В закупку: {base_amd} AMD\n"
    msg += f"Комиссия ({orders[uid]['commission_type']}): {commission} AMD\n"
    msg += f"<b>Итого: {base_amd + commission} AMD</b>\n\n"
    msg += f"Для FF используй <b>/ff</b>\n"
    msg += f"Для доставки РФ используй <b>/dostavka</b>"
    
    await query.edit_message_text(msg, parse_mode='HTML')
    save_session()
    return ConversationHandler.END

# ======== /FF ========

F_PACKAGES, F_PACKAGE_PRICE, F_PICKUP, F_FF_BOXES, F_WORK, F_THERMAL = range(6)

async def cmd_ff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ff — FF в Китае: пакеты, забор, коробки, работа, термобумага"""
    uid = str(update.effective_user.id)
    if uid not in orders or not orders[uid].get('items'):
        await update.message.reply_text('Сначала выполни /zakaz [имя] для ввода товаров')
        return ConversationHandler.END
    
    orders[uid]['ff_packages'] = {}
    orders[uid]['ff_index'] = 0
    
    # Показываем первый товар для выбора пакета
    await show_ff_package(update, context, uid)
    return F_PACKAGES

async def show_ff_package(update_or_query, context, uid):
    idx = orders[uid]['ff_index']
    items = orders[uid]['items']
    
    if idx >= len(items):
        # Все пакеты выбраны
        await update_or_query.message.reply_text('FF — Забор груза (¥):')
        return F_PICKUP
    
    item = items[idx]
    l, w, h = item['dims']
    qty = item['qty']
    
    packages = await get_packages_from_notion()
    best_pkg = find_best_package(packages, l, w, h)
    
    msg = f"📦 Товар {idx+1}/{len(items)}: <b>{item['name']}</b>\n"
    msg += f"📐 Размеры: {int(l)}×{int(w)}×{int(h)} см\n"
    msg += f"📦 Коробок: {item['boxes']}\n\n"
    
    if best_pkg:
        pkg_total = best_pkg['price'] * qty
        orders[uid]['ff_packages'][idx] = {'pkg': best_pkg, 'total': pkg_total, 'qty': qty}
        msg += f"📦 Пакет: {best_pkg['name']}\n"
        msg += f"   {best_pkg['price']} ¥ × {qty} = {fmt(pkg_total)} ¥\n\n"
        keyboard = [
            [InlineKeyboardButton("✅ OK", callback_data='f_pkg_ok')],
            [InlineKeyboardButton("💬 Своя цена", callback_data='f_pkg_custom')]
        ]
    else:
        msg += "⚠️ Пакет не найден\n\n"
        keyboard = [[InlineKeyboardButton("💬 Ввести цену", callback_data='f_pkg_custom')]]
    
    if hasattr(update_or_query, 'edit_message_text'):
        await update_or_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    else:
        await update_or_query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')

async def f_package_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    data = query.data
    
    if data == 'f_pkg_ok':
        orders[uid]['ff_index'] += 1
        result = await show_ff_package(query, context, uid)
        if result == F_PICKUP:
            return F_PICKUP
        return F_PACKAGES
    elif data == 'f_pkg_custom':
        await query.edit_message_text('Введи цену пакетов (¥):')
        return F_PACKAGE_PRICE

async def f_package_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        price = float(update.message.text)
        idx = orders[uid]['ff_index']
        items = orders[uid]['items']
        qty = items[idx]['qty']
        
        orders[uid]['ff_packages'][idx] = {
            'pkg': {'name': 'Пакет (ручной)'},
            'total': price * qty,
            'qty': qty
        }
        orders[uid]['ff_index'] += 1
        
        if orders[uid]['ff_index'] >= len(items):
            await update.message.reply_text('FF — Забор груза (¥):')
            return F_PICKUP
        else:
            await show_ff_package(update, context, uid)
            return F_PACKAGES
    except:
        await update.message.reply_text('Число! Введи цену:')
        return F_PACKAGE_PRICE

async def f_pickup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['ff_pickup'] = float(update.message.text)
        boxes = sum(i.get('boxes', 1) for i in orders[uid]['items'])
        await update.message.reply_text(f'FF — Коробки (¥ за шт, всего {boxes} шт):')
        return F_FF_BOXES
    except:
        await update.message.reply_text('Число!')
        return F_PICKUP

async def f_ff_boxes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        price = float(update.message.text)
        boxes = sum(i.get('boxes', 1) for i in orders[uid]['items'])
        orders[uid]['ff_boxes_total'] = price * boxes
        total_qty = sum(i['qty'] for i in orders[uid]['items'])
        await update.message.reply_text(f'FF — Работа (¥ за 1 шт × {total_qty}):')
        return F_WORK
    except:
        await update.message.reply_text('Число!')
        return F_FF_BOXES

async def f_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        price = float(update.message.text)
        total_qty = sum(i['qty'] for i in orders[uid]['items'])
        orders[uid]['ff_work'] = price * total_qty
        await update.message.reply_text('FF — Термобумага (0.016¥)\nСколько на 1 товар? (шт):')
        return F_THERMAL
    except:
        await update.message.reply_text('Число!')
        return F_WORK

async def f_thermal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        qty_per = float(update.message.text)
        total_qty = sum(i['qty'] for i in orders[uid]['items'])
        orders[uid]['ff_thermal'] = 0.016 * qty_per * total_qty
        
        # Считаем FF итого
        packages_total = sum(p['total'] for p in orders[uid].get('ff_packages', {}).values())
        pickup = orders[uid].get('ff_pickup', 0)
        boxes = orders[uid].get('ff_boxes_total', 0)
        work = orders[uid]['ff_work']
        thermal = orders[uid]['ff_thermal']
        
        ff_total = packages_total + pickup + boxes + work + thermal
        orders[uid]['ff_total_yuan'] = ff_total
        
        real_rate = orders[uid].get('real_rate', 55)
        ff_amd = int(ff_total * real_rate)
        
        msg = f"📦 <b>FF Китай</b>\n\n"
        msg += f"Пакеты: {fmt(packages_total)}¥\n"
        msg += f"Забор: {fmt(pickup)}¥\n"
        msg += f"Коробки: {fmt(boxes)}¥\n"
        msg += f"Работа: {fmt(work)}¥\n"
        msg += f"Термобумага: {fmt(thermal)}¥\n"
        msg += f"━━━━━━━━━━━━\n"
        msg += f"<b>Итого FF: {fmt(ff_total)}¥ = {ff_amd} AMD</b>\n\n"
        msg += f"Для доставки РФ используй <b>/dostavka</b>"
        
        await update.message.reply_text(msg, parse_mode='HTML')
        save_session()
        return ConversationHandler.END
    except:
        await update.message.reply_text('Число!')
        return F_THERMAL

# ======== /DOSTAVKA ========

D_WAREHOUSE, D_BOXES, D_MORE_WH, D_RUB_RATE, D_CRATING = range(5)

async def cmd_dostavka(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dostavka — доставка РФ: склады, FILLX"""
    uid = str(update.effective_user.id)
    if uid not in orders or not orders[uid].get('items'):
        await update.message.reply_text('Сначала выполни /zakaz [имя]')
        return ConversationHandler.END
    
    orders[uid]['warehouses'] = []
    
    keyboard = []
    cities = [c for c in TARIFFS.keys() if '(' not in c]
    for i in range(0, len(cities), 2):
        row = [InlineKeyboardButton(cities[i], callback_data=f'd_wh_{cities[i]}')]
        if i + 1 < len(cities):
            row.append(InlineKeyboardButton(cities[i+1], callback_data=f'd_wh_{cities[i+1]}'))
        keyboard.append(row)
    
    await update.message.reply_text('Выбери склад РФ:', reply_markup=InlineKeyboardMarkup(keyboard))
    return D_WAREHOUSE

async def d_warehouse_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    city = query.data.replace('d_wh_', '')
    orders[uid]['current_wh'] = city
    await query.edit_message_text(f'Склад: {city}\n\nСколько коробок?')
    return D_BOXES

async def d_boxes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        boxes = int(update.message.text)
        city = orders[uid]['current_wh']
        
        if city == "Казань" and boxes >= 8:
            tariff = TARIFFS["Казань (8-10)"]
        elif city == "Екатеринбург" and boxes >= 6:
            tariff = TARIFFS["Екатеринбург (6-10)"]
        else:
            tariff = TARIFFS[city]
        
        cost = tariff * boxes
        orders[uid]['warehouses'].append({'city': city, 'boxes': boxes, 'tariff': tariff, 'cost': cost})
        
        keyboard = [
            [InlineKeyboardButton("Да", callback_data='d_more_yes')],
            [InlineKeyboardButton("Нет", callback_data='d_more_no')]
        ]
        await update.message.reply_text(
            f'📦 {city}: {tariff}₽ × {boxes} = {cost}₽\n\nДобавить ещё склад?',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return D_MORE_WH
    except:
        await update.message.reply_text('Число!')
        return D_BOXES

async def d_more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    if query.data == 'd_more_yes':
        keyboard = []
        cities = [c for c in TARIFFS.keys() if '(' not in c]
        for i in range(0, len(cities), 2):
            row = [InlineKeyboardButton(cities[i], callback_data=f'd_wh_{cities[i]}')]
            if i + 1 < len(cities):
                row.append(InlineKeyboardButton(cities[i+1], callback_data=f'd_wh_{cities[i+1]}'))
            keyboard.append(row)
        await query.edit_message_text('Выбери склад РФ:', reply_markup=InlineKeyboardMarkup(keyboard))
        return D_WAREHOUSE
    else:
        await query.edit_message_text('Курс ₽→драм (например 5.8):')
        return D_RUB_RATE

async def d_rub_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try:
        orders[uid]['rub_rate'] = float(update.message.text)
        keyboard = [[InlineKeyboardButton("Да", callback_data='d_crate_yes'), 
                     InlineKeyboardButton("Нет", callback_data='d_crate_no')]]
        await update.message.reply_text('FILLX — Снятие обрешётки (2000₽)?', reply_markup=InlineKeyboardMarkup(keyboard))
        return D_CRATING
    except:
        await update.message.reply_text('Число!')
        return D_RUB_RATE

async def d_crating_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = str(update.effective_user.id)
    
    crating = 2000 if query.data == 'd_crate_yes' else 0
    orders[uid]['crating'] = crating
    
    rub_rate = orders[uid].get('rub_rate', 5.8)
    total_boxes = sum(w['boxes'] for w in orders[uid]['warehouses'])
    
    fillx_pickup = 7000
    fillx_receiving = 1000 * total_boxes
    fillx_unpacking = 500 * total_boxes
    fillx_delivery = sum(w['cost'] for w in orders[uid]['warehouses'])
    
    fillx_total = fillx_pickup + crating + fillx_receiving + fillx_delivery + fillx_unpacking
    fillx_amd = int(fillx_total * rub_rate)
    
    wh_text = "\n".join([f"📦 {w['city']}: {w['tariff']}₽ × {w['boxes']} = {w['cost']}₽" 
                        for w in orders[uid]['warehouses']])
    
    msg = f"📦 <b>FILLX Доставка РФ</b>\n\n"
    msg += f"{wh_text}\n\n"
    msg += f"Забор IOB: 7000₽\n"
    msg += f"Обрешётка: {crating}₽\n"
    msg += f"Приёмка: {fillx_receiving}₽\n"
    msg += f"Доставка: {fillx_delivery}₽\n"
    msg += f"Разбор: {fillx_unpacking}₽\n"
    msg += f"━━━━━━━━━━━━\n"
    msg += f"<b>Итого FILLX: {fillx_total}₽ = {fillx_amd} AMD</b>"
    
    orders[uid]['fillx_total'] = fillx_total
    orders[uid]['fillx_amd'] = fillx_amd
    
    await query.edit_message_text(msg, parse_mode='HTML')
    
    # Сохраняем в Notion
    await save_to_notion(update, context, uid)
    
    save_session()
    return ConversationHandler.END

async def save_to_notion(update, context, uid):
    """Сохраняем полный заказ в Notion"""
    try:
        data = orders[uid]
        client = data['client']
        items = data['items']
        
        total_qty = sum(i['qty'] for i in items)
        total_purchase = sum(i['purchase'] * i['qty'] for i in items)
        total_price = sum(i['price'] * i['qty'] for i in items)
        delivery_factory = sum(i.get('delivery_factory', 0) for i in items)
        
        order_code = get_code(client)
        
        # FF данные
        ff_total = data.get('ff_total_yuan', 0)
        ff_packages = sum(p['total'] for p in data.get('ff_packages', {}).values())
        ff_pickup = data.get('ff_pickup', 0)
        ff_boxes = data.get('ff_boxes_total', 0)
        ff_work = data.get('ff_work', 0)
        ff_thermal = data.get('ff_thermal', 0)
        
        # Курсы
        client_rate = data.get('client_rate', 58)
        real_rate = data.get('real_rate', 55)
        rub_rate = data.get('rub_rate', 5.8)
        
        # Комиссия
        commission = data.get('commission', 0)
        commission_type = data.get('commission_type', '')
        
        # FILLX
        fillx_total = data.get('fillx_total', 0)
        fillx_amd = data.get('fillx_amd', 0)
        crating = data.get('crating', 0)
        warehouses = data.get('warehouses', [])
        
        # Итоги
        ff_amd = int(ff_total * real_rate)
        purchase_amd = int((total_purchase + delivery_factory) * real_rate)
        client_total_amd = int((total_price + ff_total) * client_rate)
        total_costs = purchase_amd + ff_amd + fillx_amd + commission
        profit = client_total_amd - total_costs
        
        properties = {
            "Код заказа": {"title": [{"text": {"content": order_code}}]},
            "Клиент": {"select": {"name": client}},
            "Описание товара": {"rich_text": [{"text": {"content": '; '.join([i['name'] for i in items])}}]},
            "Количество": {"number": int(total_qty)},
            "Цена клиенту (CNY)": {"number": float(total_price)},
            "Цена закупки (CNY)": {"number": float(total_purchase)},
            "Доставка (CNY)": {"number": float(delivery_factory)},
            "Курс клиенту": {"number": float(client_rate)},
            "Курс реальный": {"number": float(real_rate)},
            "Курс ₽→драм": {"number": float(rub_rate)},
            "Закупка реальная (AMD)": {"number": purchase_amd},
            "К ОПЛАТЕ (AMD)": {"number": client_total_amd},
            "Прибыль (AMD)": {"number": profit},
            "FF Итого (CNY)": {"number": ff_total},
            "FF Итого (AMD)": {"number": ff_amd},
            "FILLX Итого (₽)": {"number": fillx_total},
            "FILLX Итого (AMD)": {"number": fillx_amd},
            "Комиссия": {"number": commission},
            "Статус": {"select": {"name": "Новый"}},
        }
        
        notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
        
        await context.bot.send_message(
            chat_id=update.callback_query.message.chat_id,
            text=f"✅ Сохранено в Notion: {order_code}"
        )
    except Exception as e:
        logger.error(f"Notion error: {e}")
        await context.bot.send_message(
            chat_id=update.callback_query.message.chat_id,
            text=f"❌ Ошибка сохранения: {str(e)[:200]}"
        )

# ======== /NAYTI /DEBUG /CANCEL ========

async def cmd_nayti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Введи имя клиента:')

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        res = notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
        props = list(res['properties'].keys())
        await update.message.reply_text(f"✅ Notion OK\n\nПоля:\n" + "\n".join(props[:20]))
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:200]}")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid in orders:
        del orders[uid]
    await update.message.reply_text('Отменено')
    return ConversationHandler.END

# ======== MAIN ========

def main():
    global orders
    orders = load_session()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('debug', cmd_debug))
    app.add_handler(CommandHandler('nayti', cmd_nayti))
    app.add_handler(CommandHandler('cancel', cmd_cancel))
    
    # /zakaz
    zakaz_conv = ConversationHandler(
        entry_points=[CommandHandler('zakaz', cmd_zakaz)],
        states={
            Z_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_name)],
            Z_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_qty)],
            Z_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_price)],
            Z_PURCHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_purchase)],
            Z_DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_delivery)],
            Z_DIMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_get_dims)],
            Z_MORE: [CallbackQueryHandler(z_more_cb, pattern='^z_more_')],
            Z_CLIENT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_client_rate)],
            Z_REAL_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, z_real_rate)],
            Z_COMMISSION: [CallbackQueryHandler(z_commission_cb, pattern='^z_comm_')],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
    )
    app.add_handler(zakaz_conv)
    
    # /ff
    ff_conv = ConversationHandler(
        entry_points=[CommandHandler('ff', cmd_ff)],
        states={
            F_PACKAGES: [CallbackQueryHandler(f_package_cb, pattern='^f_')],
            F_PACKAGE_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, f_package_price)],
            F_PICKUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, f_pickup)],
            F_FF_BOXES: [MessageHandler(filters.TEXT & ~filters.COMMAND, f_ff_boxes)],
            F_WORK: [MessageHandler(filters.TEXT & ~filters.COMMAND, f_work)],
            F_THERMAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, f_thermal)],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
    )
    app.add_handler(ff_conv)
    
    # /dostavka
    dostavka_conv = ConversationHandler(
        entry_points=[CommandHandler('dostavka', cmd_dostavka)],
        states={
            D_WAREHOUSE: [CallbackQueryHandler(d_warehouse_cb, pattern='^d_wh_')],
            D_BOXES: [MessageHandler(filters.TEXT & ~filters.COMMAND, d_boxes)],
            D_MORE_WH: [CallbackQueryHandler(d_more_cb, pattern='^d_more_')],
            D_RUB_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, d_rub_rate)],
            D_CRATING: [CallbackQueryHandler(d_crating_cb, pattern='^d_crate_')],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
    )
    app.add_handler(dostavka_conv)
    
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == '__main__':
    main()
