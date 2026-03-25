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

BOX_PRICE_CNY = 7.77
MAX_BOX_WEIGHT = 30.0

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None
orders = {}

TARIFFS = {
    'Коледино': 350, 'Невинномысск': 1100, 'Электросталь': 400, 'Белые Столбы': 350,
    'Чашниково': 350, 'Санкт-Петербург': 450, 'Казань': 450, 'Екатеринбург': 700,
    'Новосибирск': 850, 'Владивосток': 1000, 'Краснодар': 550, 'Свой тариф': 0
}

# ======== УТИЛИТЫ ========
def normalize_client_name(name):
    return re.sub(r'\s+', '', name).strip().capitalize()

def get_code(client):
    return f"{client.upper()}-{datetime.now().strftime('%y%m%d')}"

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

# ======== ЕДИНЫЙ ЦЕНТР РАСЧЕТОВ ========
async def finalize_order(uid, message_obj):
    data = orders[uid]
    
    subtotal_cny = sum((i['price'] * i['qty']) + i.get('delivery_factory', 0) for i in data['items'])
    comm_amd_3pct = (subtotal_cny * 0.03) * data['client_rate']
    
    rule_applied = comm_amd_3pct < 10000
    actual_comm_amd = 10000 if rule_applied else int(comm_amd_3pct)
    actual_comm_cny = 10000 / data['client_rate'] if rule_applied else subtotal_cny * 0.03
    final_total_amd = int((subtotal_cny * data['client_rate']) + actual_comm_amd)
    
    data.update({'final_total_amd': final_total_amd, 'total_cny_netto': subtotal_cny, 'rule_applied': rule_applied, 'actual_comm_cny': actual_comm_cny, 'ff_boxes_qty': data.get('ff_boxes_qty', 0)})

    audit = []
    if rule_applied:
        audit.append(f"• Применено правило 10 000 AMD (3% было {int(comm_amd_3pct)} AMD)")
    else:
        audit.append(f"• Применена комиссия 3% ({int(comm_amd_3pct)} AMD), так как она больше 10 000.")
        
    missing = [i['name'] for i in data['items'] if i.get('dims', (0,0,0)) == (0,0,0)]
    if missing: audit.append(f"• У <b>{', '.join(missing)}</b> нет размеров. Спрошу в /ff.")
    
    await message_obj.reply_text("⚠️ <b>Аудит расчета:</b>\n" + "\n".join(audit), parse_mode='HTML')

    inv_lines = "".join([f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {i.get('delivery_factory', 0)} = {(i['price'] * i['qty']) + i.get('delivery_factory', 0):.1f}¥\n" for i in data['items']])
    msg_client = f"<b>COMMERCIAL INVOICE: {data['client'].upper()}</b>\n📅 Date: {datetime.now().strftime('%d.%m.%Y')}\n\n<b>1. ТОВАРНАЯ ВЕДОМОСТЬ (Logistics Included)</b>\n{inv_lines}<code>────────────────────────</code>\n<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥\n\n<b>2. КОМИССИЯ И СЕРВИС (Service Fee)</b>\n({'Минимальная 10000 AMD' if rule_applied else '3%'}): {actual_comm_cny:.1f}¥\n\n<b>3. ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>\n• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥\n• Курс: {data['client_rate']}\n\n✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"
    await message_obj.reply_text(msg_client, parse_mode='HTML')

    purchase_cny = sum(i.get('purchase', 0) * i['qty'] for i in data['items'])
    total_delivery_cny = sum(i.get('delivery_factory', 0) for i in data['items'])
    real_expenses_amd = int((purchase_cny + total_delivery_cny) * data['real_rate'])
    profit_amd = final_total_amd - real_expenses_amd

    msg_admin = f"💼 <b>ВНУТРЕННИЙ РАСЧЕТ: {data['client'].upper()}</b>\n\n<b>РАСХОДЫ (Курс закупа: {data['real_rate']}):</b>\n• Закупка товара: {purchase_cny:.1f}¥\n• Доставка по Китаю: {total_delivery_cny:.1f}¥\nИтого расход: <b>{real_expenses_amd:,} AMD</b>\n\n<b>ДОХОДЫ:</b>\n• Взяли с клиента: <b>{final_total_amd:,} AMD</b>\n• Комиссия в чеке: {actual_comm_amd:,} AMD\n\n💰 <b>ЧИСТАЯ ПРИБЫЛЬ: {profit_amd:,} AMD</b>"
    
    client_orders, _ = await get_client_orders_from_notion(data['client'])
    keyboard = [[InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')], [InlineKeyboardButton("📑 Export Airtable", callback_data='export_airtable')]]
    if client_orders:
        orders[uid]['existing_notion_page_id'] = client_orders[0]['id']
        msg_admin += f"\n\n⚠️ <b>Клиент найден в базе!</b> (Заказ от: {client_orders[0].get('date')})"
        keyboard.append([InlineKeyboardButton("🔄 Обновить старый", callback_data='paste_update'), InlineKeyboardButton("➕ Создать НОВЫЙ", callback_data='paste_new')])
    else: keyboard.append([InlineKeyboardButton("💾 Сохранить в Notion", callback_data='paste_new')])

    await message_obj.reply_text(msg_admin, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

# ======== EXCEL И NOTION ========
async def create_excel_invoice(uid):
    data = orders[uid]
    items_data = []
    for i, item in enumerate(data['items'], 1):
        items_data.append({"№": i, "Название товара": item['name'], "Кол-во (шт)": item['qty'], "Цена (¥)": item['price'], "Логистика (¥)": item.get('delivery_factory', 0), "Итого (¥)": (item['price'] * item['qty']) + item.get('delivery_factory', 0)})
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
        worksheet = writer.sheets['Invoice']
        worksheet.set_column('A:A', 5); worksheet.set_column('B:B', 35); worksheet.set_column('C:C', 12); worksheet.set_column('D:E', 15); worksheet.set_column('F:F', 20)
    output.seek(0)
    return output

async def get_packages_from_notion():
    if not notion or not PACKAGES_DATABASE_ID: return []
    try:
        res = notion.databases.query(database_id=PACKAGES_DATABASE_ID)
        return [{'name': p['properties'].get('Название', {}).get('title', [{}])[0].get('text', {}).get('content', ''), 'price': p['properties'].get('Цена', {}).get('number', 0)} for p in res.get('results', []) if p['properties'].get('Название', {}).get('title') and p['properties'].get('Цена', {}).get('number', 0) > 0]
    except: return []

async def get_client_orders_from_notion(client_name):
    if not notion or not NOTION_DATABASE_ID: return None, "Notion не настроен"
    try:
        res = notion.databases.query(database_id=NOTION_DATABASE_ID, sorts=[{"timestamp": "created_time", "direction": "descending"}], page_size=100)
        client_norm = normalize_client_name(client_name).lower()
        filtered = [p for p in res.get('results', []) if p['properties'].get('Клиент', {}).get('select', {}) and normalize_client_name(p['properties']['Клиент']['select'].get('name', '')).lower() == client_norm]
        if not filtered: return [], None
        return [{'id': p['id'], 'date': p.get('created_time', '')[:10]} for p in filtered[:5]], None
    except Exception as e: return None, str(e)

async def save_to_notion(uid):
    if not notion: return None
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
        if 'ff_total_yuan' in data: properties["ИТОГО (CNY)"] = {"number": float(data['ff_total_yuan'])}

        page_id = data.get('notion_page_id')
        if page_id: res = notion.pages.update(page_id=page_id, properties=properties)
        else:
            res = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
            orders[uid]['notion_page_id'] = res['id']
        return f"https://notion.so/{res['id'].replace('-', '')}"
    except: return None

# ======== ПАРСЕР /PASTE & /CALC (С ЗАЩИТОЙ ОТ СЛИПАНИЯ) ========
def parse_paste_text(text):
    # Принудительно "разрезаем" текст, если он слипся
    keywords = [r'Клиент:', r'Товар \d+:', r'Название:', r'Количество:', r'Цена клиенту:', r'Закупка:', r'Доставка:', r'Размеры:', r'Курс клиенту:', r'Мой курс:']
    for kw in keywords:
        text = re.sub(f"({kw})", r"\n\1", text, flags=re.IGNORECASE)
        
    data = {'client': 'Unknown', 'items': [], 'client_rate': 58.0, 'real_rate': 55.0}
    current_item = None
    
    for line in text.split('\n'):
        l = line.strip().lower()
        if not l: continue
        
        if 'клиент:' in l: 
            data['client'] = normalize_client_name(line.split(':', 1)[1])
        elif 'товар' in l:
            if current_item: data['items'].append(current_item)
            current_item = {'name': 'Товар', 'qty': 0, 'price': 0.0, 'purchase': 0.0, 'delivery_factory': 0.0, 'dims': (0,0,0), 'weight': 0.0}
        elif 'название:' in l and current_item: 
            current_item['name'] = line.split(':', 1)[1].strip().title()
        elif 'количество:' in l and current_item: 
            part = l.split('количество:')[1]
            nums = re.findall(r'\d+', part)
            if nums: current_item['qty'] = int(nums[0])
        elif 'цена клиенту:' in l and current_item: 
            part = l.split('цена клиенту:')[1]
            nums = re.findall(r'\d+\.?\d*', part.replace(',', '.'))
            if nums: current_item['price'] = float(nums[0])
        elif 'закупка:' in l and current_item: 
            part = l.split('закупка:')[1]
            nums = re.findall(r'\d+\.?\d*', part.replace(',', '.'))
            if nums: current_item['purchase'] = float(nums[0])
        elif 'доставка:' in l and current_item: 
            part = l.split('доставка:')[1]
            nums = re.findall(r'\d+\.?\d*', part.replace(',', '.'))
            if nums: current_item['delivery_factory'] = float(nums[0])
        elif 'размеры:' in l and current_item: 
            part = l.split('размеры:')[1]
            nums = re.findall(r'\d+\.?\d*', part.replace(',', '.'))
            if len(nums) >= 3:
                current_item['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: current_item['weight'] = float(nums[3])
        elif 'курс клиенту:' in l: 
            part = l.split('курс клиенту:')[1]
            nums = re.findall(r'\d+\.?\d*', part.replace(',', '.'))
            if nums: data['client_rate'] = float(nums[0])
        elif 'мой курс:' in l: 
            part = l.split('мой курс:')[1]
            nums = re.findall(r'\d+\.?\d*', part.replace(',', '.'))
            if nums: data['real_rate'] = float(nums[0])
            
    if current_item: data['items'].append(current_item)
    return data

async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/paste', '').strip()
    if not text: return
    data = parse_paste_text(text)
    if not data['items']: return await update.message.reply_text("❌ Ошибка: товары не найдены.")
    orders[uid] = data
    await finalize_order(uid, update.message)

# ======== КОМАНДА /CALC ========
async def cmd_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/calc', '').strip()
    if not text: return
    
    data = parse_paste_text(text)
    if not data['items']: return await update.message.reply_text("❌ Ошибка: товары не найдены.")
    
    subtotal_cny = sum((i['price'] * i['qty']) + i.get('delivery_factory', 0) for i in data['items'])
    comm_amd_3pct = (subtotal_cny * 0.03) * data['client_rate']
    
    rule_applied = comm_amd_3pct < 10000
    actual_comm_amd = 10000 if rule_applied else int(comm_amd_3pct)
    actual_comm_cny = 10000 / data['client_rate'] if rule_applied else subtotal_cny * 0.03
    final_total_amd = int((subtotal_cny * data['client_rate']) + actual_comm_amd)
    
    data.update({'final_total_amd': final_total_amd, 'total_cny_netto': subtotal_cny, 'rule_applied': rule_applied, 'actual_comm_cny': actual_comm_cny, 'ff_boxes_qty': 0})
    orders[uid] = data

    # АУДИТ В /CALC
    audit = []
    if rule_applied:
        audit.append(f"• Применено правило 10 000 AMD (3% было {int(comm_amd_3pct)} AMD)")
    else:
        audit.append(f"• Применена комиссия 3% ({int(comm_amd_3pct)} AMD), так как она больше 10 000.")
    await update.message.reply_text("⚠️ <b>Аудит расчета:</b>\n" + "\n".join(audit), parse_mode='HTML')

    inv_lines = "".join([f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {i.get('delivery_factory', 0)} = {(i['price'] * i['qty']) + i.get('delivery_factory', 0):.1f}¥\n" for i in data['items']])
    msg_client = f"<b>COMMERCIAL INVOICE: {data['client'].upper()}</b>\n📅 Date: {datetime.now().strftime('%d.%m.%Y')}\n\n<b>1. ТОВАРНАЯ ВЕДОМОСТЬ (Logistics Included)</b>\n{inv_lines}<code>────────────────────────</code>\n<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥\n\n<b>2. КОМИССИЯ И СЕРВИС (Service Fee)</b>\n({'Минимальная 10000 AMD' if rule_applied else '3%'}): {actual_comm_cny:.1f}¥\n\n<b>3. ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>\n• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥\n• Курс: {data['client_rate']}\n\n✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"
    await update.message.reply_text(msg_client, parse_mode='HTML')

    msg_warning = "⚠️ <b>Внимание:</b> Для финального (внутреннего) расчета не хватает цен закупки и размеров."
    kb = [[InlineKeyboardButton("✍️ Дополнить расчет", callback_data='calc_fill'), InlineKeyboardButton("📊 Export Excel", callback_data='gen_excel')]]
    await update.message.reply_text(msg_warning, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

# ======== ИНТЕРАКТИВНОЕ ДОПОЛНЕНИЕ /CALC ========
C_PURCHASE, C_DIMS = range(50, 52)

async def calc_fill_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    missing_idx = -1
    for idx, item in enumerate(orders[uid]['items']):
        if item.get('purchase', 0.0) == 0.0 or item.get('dims', (0,0,0)) == (0,0,0):
            missing_idx = idx
            break
            
    if missing_idx == -1:
        await query.message.reply_text("✅ Все данные уже заполнены! Вывожу расчет...")
        await finalize_order(uid, query.message)
        return ConversationHandler.END
        
    orders[uid]['calc_missing_idx'] = missing_idx
    item_name = orders[uid]['items'][missing_idx]['name']
    await query.message.reply_text(f"Введи цену закупки (CNY) для товара <b>{item_name}</b>:", parse_mode='HTML')
    return C_PURCHASE

async def c_get_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    try: val = float(update.message.text.replace(',', '.'))
    except: await update.message.reply_text("❌ Введи число:"); return C_PURCHASE
        
    idx = orders[uid]['calc_missing_idx']
    orders[uid]['items'][idx]['purchase'] = val
    item_name = orders[uid]['items'][idx]['name']
    await update.message.reply_text(f"Введи размеры и вес (Д Ш В Вес) для товара <b>{item_name}</b> (или '-'):", parse_mode='HTML')
    return C_DIMS

async def c_get_dims(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.strip()
    idx = orders[uid]['calc_missing_idx']

    if text != '-':
        try:
            nums = re.findall(r'\d+\.?\d*', text.replace(',', '.'))
            if len(nums) >= 3:
                orders[uid]['items'][idx]['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: orders[uid]['items'][idx]['weight'] = float(nums[3])
            else: raise ValueError
        except:
            await update.message.reply_text("❌ Введи 4 числа через пробел (или '-'):")
            return C_DIMS

    missing_idx = -1
    for i, item in enumerate(orders[uid]['items']):
        if item.get('purchase', 0.0) == 0.0 or item.get('dims', (0,0,0)) == (0,0,0):
            missing_idx = i
            break

    if missing_idx == -1:
        await update.message.reply_text("✅ Все данные собраны!")
        await finalize_order(uid, update.message)
        return ConversationHandler.END
    else:
        orders[uid]['calc_missing_idx'] = missing_idx
        item_name = orders[uid]['items'][missing_idx]['name']
        await update.message.reply_text(f"Введи цену закупки (CNY) для товара <b>{item_name}</b>:", parse_mode='HTML')
        return C_PURCHASE

# ======== ОБЩИЙ ОБРАБОТЧИК КНОПОК ========
async def export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'gen_excel':
        try:
            file_stream = await create_excel_invoice(uid)
            await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(file_stream, filename=f"Invoice_{orders[uid]['client']}.xlsx"))
        except: await query.message.reply_text("❌ Ошибка Excel.")
    elif query.data == 'export_airtable':
        data = orders.get(uid)
        export_text = f"AIRTABLE_EXPORT_START\nInvoice_ID: {data['client']}\nDate: {datetime.now().strftime('%d.%m.%Y')}\nSum_Client_CNY: {data.get('total_cny_netto', 0)}\nReal_Purchase_CNY: {sum(i.get('purchase', 0) * i.get('qty', 0) for i in data.get('items', []))}\nClient_Rate: {data.get('client_rate', 58.0)}\nReal_Rate: {data.get('real_rate', 55.0)}\nTotal_Qty: {sum(i.get('qty', 0) for i in data.get('items', []))}\nChina_Logistics_CNY: {sum(i.get('delivery_factory', 0) for i in data.get('items', []))}\nFF_Boxes_Qty: {data.get('ff_boxes_qty', 0)}\nAIRTABLE_EXPORT_END"
        await query.message.reply_text(f"<code>{export_text}</code>", parse_mode='HTML')
    elif query.data in ['paste_new', 'paste_update', 'paste_save_direct']:
        if query.data == 'paste_update': orders[uid]['notion_page_id'] = orders[uid].get('existing_notion_page_id')
        elif query.data == 'paste_new': orders[uid]['notion_page_id'] = None
        url = await save_to_notion(uid)
        try: await query.edit_message_text(f"{query.message.text}\n\n✅ Сохранено:\n{url}" if url else f"{query.message.text}\n\n❌ Ошибка Notion")
        except: await query.message.reply_text(f"✅ Сохранено:\n{url}" if url else "❌ Ошибка Notion")

# ======== РУЧНОЙ ВВОД ЗАКАЗА (/ZAKAZ) ========
Z_NAME, Z_QTY, Z_PRICE, Z_PURCHASE, Z_DELIVERY, Z_DIMS, Z_MORE, Z_CLIENT_RATE, Z_REAL_RATE = range(40, 49)

async def cmd_zakaz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args: 
        await update.message.reply_text("❌ Напиши имя клиента после команды. Например: /zakaz Zaven8291")
        return ConversationHandler.END
    client = normalize_client_name(' '.join(context.args))
    orders[uid] = {'client': client, 'items': [], 'client_rate': 58.0, 'real_rate': 55.0}
    await update.message.reply_text(f"Клиент: {client}\n\nНазвание первого товара:")
    return Z_NAME

async def z_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    orders[uid]['current'] = {'name': update.message.text.strip().title(), 'dims': (0,0,0), 'weight': 0.0}
    await update.message.reply_text("Количество:")
    return Z_QTY

async def z_get_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(
