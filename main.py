import os
import logging
import math
import json
import traceback
import re
import io
import pandas as pd
from datetime import datetime
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ConversationHandler, filters, ContextTypes

# ======== НАСТРОЙКИ ========
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DATABASE_ID = "3278c4d1fb0e80c4b6e5f261d0631ed2"
PACKAGES_DATABASE_ID = "32a8c4d1fb0e806ebb98f5995704d0e5"
CARGO_NOTION_DATABASE_ID = os.getenv('CARGO_NOTION_DB_ID', "СЮДА_ID_БАЗЫ_CARGO") 

BOX_PRICE_CNY = 7.77
MAX_BOX_WEIGHT = 30.0

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None
orders = {}
cargo_drafts = {}

# ======== УТИЛИТЫ ========
def normalize_client_name(name):
    return re.sub(r'\s+', '', name).strip().capitalize()

def get_code(client):
    return f"{client.upper()}-{datetime.now().strftime('%y%m%d')}"

def generate_cargo_id():
    import random
    return f"CARGO-{random.randint(100, 999)}"

def optimize_boxes_with_weight(items):
    MAX_L, MAX_W, MAX_H = 60, 40, 40
    boxes = []
    all_units = []
    for item in items:
        for _ in range(item.get('qty', 0)):
            l, w, h = item.get('dims', (1,1,1))
            all_units.append({'name': item['name'], 'dims': (l, w, h), 'weight': item.get('weight', 0.0), 'vol': l * w * h})

    for unit in all_units:
        placed = False
        for box in boxes:
            if box['rem_vol'] >= unit['vol'] and (box['cur_weight'] + unit['weight']) <= MAX_BOX_WEIGHT:
                box['items'].append(unit)
                box['rem_vol'] -= unit['vol']
                box['cur_weight'] += unit['weight']
                placed = True
                break
        if not placed:
            boxes.append({'items': [unit], 'rem_vol': (MAX_L * MAX_W * MAX_H) - unit['vol'], 'cur_weight': unit['weight']})
    return boxes

# ======== ГЛАВНОЕ МЕНЮ И РУКОВОДСТВО ========
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("🚫 Действие отменено. Вы вернулись в главное меню.\nНапишите /menu чтобы увидеть все команды.")
    elif update.callback_query:
        await update.callback_query.message.reply_text("🚫 Действие отменено.")
    return ConversationHandler.END

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu_text = """🎛 <b>ГЛАВНОЕ МЕНЮ БОТА</b>

📦 <b>ЛОГИСТИКА И КАРГО</b>
• /cargo — Управление Карго (расчет упаковки, тарифы).
• /ff — Фулфилмент в Китае (коробки до 30 кг).
• /dostavka_new — Доставка РФ (независимый расчет с нуля).
• /dostavka — Доставка РФ (привязка к теку выкупу).

🛒 <b>ВЫКУП ТОВАРОВ</b>
• /zakaz [Имя] — Ручной пошаговый ввод нового заказа.
• /paste [Текст] — Создание заказа из текста поставщика.
• /calc [Текст] — Быстрый калькулятор инвойса.
• /audit-gs [Текст] — Аудит и генерация чека от Kimi.

⚙️ <b>СИСТЕМА</b>
• /cancel — Прервать любое действие и сбросить бота.

📊 В конце каждого расчета вам доступны кнопки Airtable, Excel и Notion."""

    kb = [[InlineKeyboardButton("📖 Открыть подробное руководство", callback_data='guide_open')]]
    
    if update.message:
        await update.message.reply_text(menu_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    elif update.callback_query:
        await update.callback_query.edit_message_text(menu_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

async def guide_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    guide_text = """📖 <b>РУКОВОДСТВО ПО ИСПОЛЬЗОВАНИЮ БОТА</b>

<b>1. Выкуп из текста (/paste или /calc)</b>
Скопируйте текст от поставщика и отправьте боту.

<b>2. Интерактивный выкуп (/zakaz)</b>
Напишите <code>/zakaz Zaven</code> и бот сам спросит название, количество, цены и размеры.

<b>3. Фулфилмент (/ff)</b>
Запускается после выкупа. Распределяет товары по коробкам (до 30 кг в одной).

<b>4. Логистика Карго (/cargo)</b>
Напишите <code>/cargo</code>. Считает прибыль между тарифами, накидывает вес упаковки (+1 кг картон, +10 кг дерево).

<b>5. Доставка по РФ (/dostavka и /dostavka_new)</b>
• <code>/dostavka</code> — продолжает работу с текущим просчитанным клиентом (после /zakaz или /paste). Дает кнопки для обновления Notion и общего инвойса.
• <code>/dostavka_new</code> — начинает расчет с нуля для нового клиента (внешний груз).
<i>Как бот считает РФ:</i> Автоматически делит коробки на паллеты (от 11 шт — это 1 паллет). Применяет оптовые грейды (от 6 паллет тариф ниже). Прибавляет услуги FILLX: 100₽/приемка, 50₽/разбор, 9000₽/забор груза."""
    
    kb = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data='menu_back')]]
    await query.edit_message_text(guide_text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

# ======== СУПЕР-АУДИТОР ОТ KIMI (/AUDIT-GS) ========
async def cmd_audit_gs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    # Удаляем саму команду (как бы она ни была написана: /audit-gs или /audit_gs)
    text = re.sub(r'^/audit[-_]gs\s*', '', text, flags=re.IGNORECASE).strip()
    
    if not text: 
        return await update.message.reply_text("❌ Пустой запрос. Пришли текст от Kimi.")
        
    if '❌ Найдены ошибки' not in text or '✅ Исправленный расчет' not in text:
        return await update.message.reply_text("❌ Ошибка формата: не найдены обязательные маркеры Kimi (❌ Найдены ошибки / ✅ Исправленный расчет).")
        
    try:
        # Разрезаем текст на 3 логические части
        part1, rest = text.split('❌ Найдены ошибки', 1)
        original_text = part1.strip()
        
        part2, part3 = rest.split('✅ Исправленный расчет', 1)
        error_log = '❌ Найдены ошибки' + part2.strip()
        corrected_text = part3.strip().lstrip(':').strip()
        
        # Парсим исправленный расчет построчно
        lines = [line.strip() for line in corrected_text.split('\n') if line.strip()]
        client_name = "БЕЗ ИМЕНИ"
        
        items = []
        for line in lines:
            # Убираем квадратные скобки на случай, если они есть
            line = line.replace('[', '').replace(']', '').strip()
            
            # Ищем формулу товара: 7.5×200+144=1644 vase
            m = re.match(r'^([\d\.]+)\s*[×x*]\s*(\d+)(?:\s*\+\s*([\d\.]+))?\s*=\s*[\d\.]+\s+(.+)$', line, re.IGNORECASE)
            if m:
                price = float(m.group(1))
                qty = int(m.group(2))
                delivery = float(m.group(3)) if m.group(3) else 0.0
                name = m.group(4).strip().title()
                items.append({'name': name, 'qty': qty, 'price': price, 'delivery_factory': delivery})
            elif not items and not re.search(r'[×x*]', line):
                # Если формулы еще не начались, значит это имя клиента
                if len(line) < 30:
                    client_name = line
                
        # Ищем курс клиента (например 2474×58=)
        client_rate = 58.0
        rate_match = re.search(r'[×x*]\s*([\d\.]+)\s*=(?:\s*|\n|=)', corrected_text)
        if rate_match:
            client_rate = float(rate_match.group(1))
            
        if not items:
            return await update.message.reply_text("❌ Не удалось распознать товары из исправленного расчета. Проверь формулы.")
            
        # Внутренняя математика (чтобы всё сошлось 1 в 1)
        subtotal_cny = sum((i['price'] * i['qty']) + i['delivery_factory'] for i in items)
        comm_amd_3pct = (subtotal_cny * 0.03) * client_rate
        rule_applied = comm_amd_3pct < 10000
        actual_comm_cny = 10000 / client_rate if rule_applied else subtotal_cny * 0.03
        actual_comm_amd = 10000 if rule_applied else int(comm_amd_3pct)
        final_total_amd = int((subtotal_cny * client_rate) + actual_comm_amd)
        
        # Собираем красивый чек клиента
        inv_lines = ""
        for i in items:
            inv_lines += f"• {i['name']} — {i['qty']} шт\n"
            inv_lines += f"{i['qty']} × {i['price']} + {i['delivery_factory']} = {(i['price'] * i['qty']) + i['delivery_factory']:.1f}¥\n"
            
        msg = f"<b>COMMERCIAL INVOICE: {client_name.upper()}</b>\n"
        msg += f"📅 Date: {datetime.now().strftime('%d.%m.%Y')}\n\n"
        msg += f"<b>ОРИГИНАЛЬНЫЙ РАСЧЕТ:</b>\n{original_text}\n\n"
        msg += f"{error_log}\n\n"
        msg += f"✅ <b>ИСПРАВЛЕННАЯ ТОВАРНАЯ ВЕДОМОСТЬ</b>\n"
        msg += f"{inv_lines}<code>────────────────────────</code>\n"
        msg += f"<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥\n\n"
        msg += f"<b>КОМИССИЯ И СЕРВИС (Service Fee)</b>\n"
        msg += f"({'Минимальная 10000 AMD' if rule_applied else '3%'}): {actual_comm_cny:.1f}¥\n\n"
        msg += f"<b>ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>\n"
        msg += f"• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥\n"
        msg += f"• Курс: {client_rate}\n\n"
        msg += f"✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"
        
        # Отправляем единое сообщение без кнопок
        await update.message.reply_text(msg, parse_mode='HTML')
        
    except Exception as e:
        await update.message.reply_text(f"❌ Произошла ошибка при обработке: {e}")

# ======== ЕДИНЫЙ ЦЕНТР РАСЧЕТОВ ТОВАРОВ ========
async def finalize_order(uid, message_obj):
    data = orders[uid]
    subtotal_cny = sum((i['price'] * i['qty']) + i.get('delivery_factory', 0) for i in data['items'])
    comm_amd_3pct = (subtotal_cny * 0.03) * data['client_rate']
    
    rule_applied = comm_amd_3pct < 10000
    actual_comm_amd = 10000 if rule_applied else int(comm_amd_3pct)
    actual_comm_cny = 10000 / data['client_rate'] if rule_applied else subtotal_cny * 0.03
    final_total_amd = int((subtotal_cny * data['client_rate']) + actual_comm_amd)
    
    data.update({'final_total_amd': final_total_amd, 'total_cny_netto': subtotal_cny, 'rule_applied': rule_applied, 'actual_comm_cny': actual_comm_cny, 'ff_boxes_qty': data.get('ff_boxes_qty', 0)})

    audit = [f"• Применено правило 10 000 AMD (3% было {int(comm_amd_3pct)} AMD)"] if rule_applied else []
    missing = [i['name'] for i in data['items'] if i.get('dims', (0,0,0)) == (0,0,0)]
    if missing: 
        audit.append(f"• У <b>{', '.join(missing)}</b> нет размеров. Спрошу в /ff.")
        
    if audit: 
        await message_obj.reply_text("⚠️ <b>Аудит расчета:</b>\n" + "\n".join(audit), parse_mode='HTML')

    inv_lines = "".join([f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {i.get('delivery_factory', 0)} = {(i['price'] * i['qty']) + i.get('delivery_factory', 0):.1f}¥\n" for i in data['items']])
    msg_client = f"""<b>COMMERCIAL INVOICE: {data['client'].upper()}</b>
📅 Date: {datetime.now().strftime('%d.%m.%Y')}

<b>1. ТОВАРНАЯ ВЕДОМОСТЬ (Logistics Included)</b>
{inv_lines}<code>────────────────────────</code>
<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥

<b>2. КОМИССИЯ И СЕРВИС (Service Fee)</b>
({'Минимальная 10000 AMD' if rule_applied else '3%'}): {actual_comm_cny:.1f}¥

<b>3. ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>
• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥
• Курс: {data['client_rate']}

✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"""

    await message_obj.reply_text(msg_client, parse_mode='HTML')

    purchase_cny = sum(i.get('purchase', 0) * i['qty'] for i in data['items'])
    total_delivery_cny = sum(i.get('delivery_factory', 0) for i in data['items'])
    real_expenses_amd = int((purchase_cny + total_delivery_cny) * data['real_rate'])
    profit_amd = final_total_amd - real_expenses_amd

    msg_admin = f"""💼 <b>ВНУТРЕННИЙ РАСЧЕТ: {data['client'].upper()}</b>

<b>РАСХОДЫ (Курс закупа: {data['real_rate']}):</b>
• Закупка товара: {purchase_cny:.1f}¥
• Доставка по Китаю: {total_delivery_cny:.1f}¥
Итого расход: <b>{real_expenses_amd:,} AMD</b>

<b>ДОХОДЫ:</b>
• Взяли с клиента: <b>{final_total_amd:,} AMD</b>
• Комиссия в чеке: {actual_comm_amd:,} AMD

💰 <b>ЧИСТАЯ ПРИБЫЛЬ: {profit_amd:,} AMD</b>"""

    client_orders, _ = await get_client_orders_from_notion(data['client'])
    keyboard = [[InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')], [InlineKeyboardButton("📑 Export Airtable", callback_data='export_airtable')]]
    
    if client_orders:
        orders[uid]['existing_notion_page_id'] = client_orders[0]['id']
        msg_admin += f"\n\n⚠️ <b>Клиент найден в базе!</b> (Заказ от: {client_orders[0].get('date')})"
        keyboard.append([InlineKeyboardButton("🔄 Обновить старый", callback_data='paste_update'), InlineKeyboardButton("➕ Создать НОВЫЙ", callback_data='paste_new')])
    else:
        keyboard.append([InlineKeyboardButton("💾 Сохранить в Notion", callback_data='paste_new')])

    await message_obj.reply_text(msg_admin, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def create_excel_invoice(uid):
    data = orders[uid]
    items_data = []
    for i, item in enumerate(data['items'], 1):
        delivery = item.get('delivery_factory', 0)
        items_data.append({"№": i, "Название товара": item['name'], "Кол-во (шт)": item['qty'], "Цена (¥)": item['price'], "Логистика (¥)": delivery, "Итого (¥)": (item['price'] * item['qty']) + delivery})
        
    items_data.extend([
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "", "Итого (¥)": ""},
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "SUBTOTAL:", "Итого (¥)": f"{data.get('total_cny_netto', 0):.1f} ¥"},
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "Комиссия:", "Итого (¥)": f"{data.get('actual_comm_cny', 0):.1f} ¥"},
        {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "ИТОГО К ОПЛАТЕ:", "Итого (¥)": f"{data.get('final_total_amd', 0):,} AMD"}
    ])
    df = pd.DataFrame(items_data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Invoice')
    output.seek(0)
    return output

# ======== NOTION API ========
async def get_packages_from_notion():
    if not notion or not PACKAGES_DATABASE_ID: 
        return []
    try:
        res = notion.databases.query(database_id=PACKAGES_DATABASE_ID)
        return [{'name': p['properties'].get('Название', {}).get('title', [{}])[0].get('text', {}).get('content', ''), 'price': p['properties'].get('Цена', {}).get('number', 0)} for p in res.get('results', []) if p['properties'].get('Название', {}).get('title') and p['properties'].get('Цена', {}).get('number', 0) > 0]
    except: 
        return []

async def get_client_orders_from_notion(client_name):
    if not notion or not NOTION_DATABASE_ID: 
        return None, "Notion не настроен"
    try:
        res = notion.databases.query(database_id=NOTION_DATABASE_ID, sorts=[{"timestamp": "created_time", "direction": "descending"}], page_size=100)
        client_norm = normalize_client_name(client_name).lower()
        filtered = [p for p in res.get('results', []) if p['properties'].get('Клиент', {}).get('select', {}) and normalize_client_name(p['properties']['Клиент']['select'].get('name', '')).lower() == client_norm]
        if not filtered: 
            return [], None
        return [{'id': p['id'], 'date': p.get('created_time', '')[:10]} for p in filtered[:5]], None
    except Exception as e: 
        return None, str(e)

async def save_to_notion(uid):
    if not notion: 
        return None
    try:
        data = orders[uid]
        properties = {
            "Код заказа": {"title": [{"text": {"content": get_code(data['client'])}}]},
            "Клиент": {"select": {"name": data['client']}},
            "Количество": {"number": float(sum(i['qty'] for i in data['items']))},
            " К ОПЛАТЕ (AMD)": {"number": float(data['final_total_amd'])},
            "Статус": {"select": {"name": "Новый"}},
            "Date": {"date": {"start": datetime.now().strftime('%Y-%m-%d')}}
        }
        if 'ff_total_yuan' in data: 
            properties["ИТОГО (CNY)"] = {"number": float(data['ff_total_yuan'])}

        page_id = data.get('notion_page_id')
        
        if page_id: 
            res = notion.pages.update(page_id=page_id, properties=properties)
        else:
            res = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
            orders[uid]['notion_page_id'] = res['id']
            
        return f"https://notion.so/{res['id'].replace('-', '')}"
    except: 
        return None

# ======== СУПЕР-ПАРСЕР /PASTE & /CALC ========
def parse_paste_text(text):
    keywords = ['Количество:', 'Цена клиенту:', 'Закупка:', 'Доставка:', 'Размеры:', 'Курс клиенту:', 'Мой курс:']
    for kw in keywords: 
        text = re.sub(f"(?i)({kw})", r"\n\1", text)
        
    data = {'client': 'Unknown', 'items': [], 'client_rate': 58.0, 'real_rate': 55.0}
    current_item = None
    for line in text.split('\n'):
        l = line.strip().lower()
        if not l: continue
        
        if 'клиент:' in l: 
            data['client'] = normalize_client_name(l.split('клиент:')[-1])
        elif 'товар' in l:
            if current_item: 
                data['items'].append(current_item)
            current_item = {'name': 'Товар', 'qty': 0, 'price': 0.0, 'purchase': 0.0, 'delivery_factory': 0.0, 'dims': (0,0,0), 'weight': 0.0}
        elif 'название:' in l and current_item is not None: 
            current_item['name'] = line[line.lower().find('название:') + 9:].strip().title()
        elif 'количество:' in l and current_item is not None: 
            nums = re.findall(r'\d+', l.split('количество:')[-1])
            if nums: 
                current_item['qty'] = int(nums[0])
        elif 'цена клиенту:' in l and current_item is not None: 
            nums = re.findall(r'\d+\.?\d*', l.split('цена клиенту:')[-1].replace(',', '.'))
            if nums: 
                current_item['price'] = float(nums[0])
        elif 'закупка:' in l and current_item is not None: 
            nums = re.findall(r'\d+\.?\d*', l.split('закупка:')[-1].replace(',', '.'))
            if nums: 
                current_item['purchase'] = float(nums[0])
        elif 'доставка:' in l and current_item is not None: 
            nums = re.findall(r'\d+\.?\d*', l.split('доставка:')[-1].replace(',', '.'))
            if nums: 
                current_item['delivery_factory'] = float(nums[0])
        elif 'размеры:' in l and current_item is not None:
            nums = re.findall(r'\d+\.?\d*', l.split('размеры:')[-1].replace(',', '.'))
            if len(nums) >= 3:
                current_item['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: 
                    current_item['weight'] = float(nums[3])
        elif 'курс клиенту:' in l: 
            nums = re.findall(r'\d+\.?\d*', l.split('курс клиенту:')[-1].replace(',', '.'))
            if nums: 
                data['client_rate'] = float(nums[0])
        elif 'мой курс:' in l: 
            nums = re.findall(r'\d+\.?\d*', l.split('мой курс:')[-1].replace(',', '.'))
            if nums: 
                data['real_rate'] = float(nums[0])
            
    if current_item: 
        data['items'].append(current_item)
    return data

async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/paste', '').strip()
    if not text: 
        return
    data = parse_paste_text(text)
    if not data['items']: 
        return await update.message.reply_text("❌ Ошибка: товары не найдены.")
    orders[uid] = data
    await finalize_order(uid, update.message)
