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
MAX_BOX_WEIGHT = 30.0  # Лимит веса на одну коробку

notion = Client(auth=NOTION_TOKEN) if NOTION_TOKEN else None
orders = {}

# ======== УТИЛИТЫ ========
def normalize_client_name(name):
    return re.sub(r'\s+', '', name).strip().capitalize()

def get_code(client):
    return f"{client.upper()}-{datetime.now().strftime('%y%m%d')}"

def optimize_boxes_with_weight(items):
    """Алгоритм упаковки 3D + Вес (лимит 30кг)"""
    MAX_L, MAX_W, MAX_H = 60, 40, 40
    boxes = []
    
    all_units = []
    for item in items:
        for _ in range(item.get('qty', 0)):
            l, w, h = item.get('dims', (1,1,1))
            all_units.append({
                'name': item['name'],
                'dims': (l, w, h),
                'weight': item.get('weight', 0.0),
                'vol': l * w * h
            })

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
            boxes.append({
                'items': [unit],
                'rem_vol': (MAX_L * MAX_W * MAX_H) - unit['vol'],
                'cur_weight': unit['weight']
            })
    return boxes

# ======== EXCEL ГЕНЕРАТОР ========
async def create_excel_invoice(uid):
    data = orders[uid]
    items_data = []
    
    for i, item in enumerate(data['items'], 1):
        delivery = item.get('delivery_factory', 0)
        total_item_cny = (item['price'] * item['qty']) + delivery
        items_data.append({
            "№": i,
            "Название товара": item['name'],
            "Кол-во (шт)": item['qty'],
            "Цена (¥)": item['price'],
            "Логистика (¥)": delivery,
            "Итого (¥)": total_item_cny
        })
        
    items_data.append({"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "", "Итого (¥)": ""})
    items_data.append({"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "SUBTOTAL:", "Итого (¥)": f"{data.get('total_cny_netto', 0):.1f} ¥"})
    
    fee_label = "Комиссия (Мин. 10000 AMD):" if data.get('rule_applied') else "Комиссия (3%):"
    items_data.append({"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": fee_label, "Итого (¥)": f"{data.get('actual_comm_cny', 0):.1f} ¥"})
    items_data.append({"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "ИТОГО К ОПЛАТЕ:", "Итого (¥)": f"{data.get('final_total_amd', 0):,} AMD"})
    
    df = pd.DataFrame(items_data)
    output = io.BytesIO()
    
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Invoice')
        worksheet = writer.sheets['Invoice']
        worksheet.set_column('A:A', 5)
        worksheet.set_column('B:B', 35)
        worksheet.set_column('C:C', 12)
        worksheet.set_column('D:E', 15)
        worksheet.set_column('F:F', 20)
        
    output.seek(0)
    return output

# ======== NOTION АУДИТ ========
async def get_client_orders_from_notion(client_name):
    if not notion or not NOTION_DATABASE_ID: return None, "Notion не настроен"
    try:
        res = notion.databases.query(database_id=NOTION_DATABASE_ID, sorts=[{"timestamp": "created_time", "direction": "descending"}], page_size=100)
        client_norm = normalize_client_name(client_name).lower()
        filtered = []
        
        for p in res.get('results', []):
            tag = p['properties'].get('Клиент', {}).get('select', {})
            if tag and normalize_client_name(tag.get('name', '')).lower() == client_norm:
                filtered.append(p)
        
        if not filtered: return [], None
        return [{'id': p['id'], 'date': p.get('created_time', '')[:10]} for p in filtered[:5]], None
    except Exception as e: return None, str(e)

# ======== NOTION SAVE ========
async def save_to_notion(uid):
    if not notion: return None
    try:
        data = orders[uid]
        client = data['client']
        total_qty = sum(i['qty'] for i in data['items'])
        
        properties = {
            "Код заказа": {"title": [{"text": {"content": get_code(client)}}]},
            "Клиент": {"select": {"name": client}},
            "Количество": {"number": float(total_qty)},
            " К ОПЛАТЕ (AMD)": {"number": float(data['final_total_amd'])},
            "Статус": {"select": {"name": "Новый"}},
            "Date": {"date": {"start": datetime.now().strftime('%Y-%m-%d')}}
        }
        
        if 'ff_total_yuan' in data: properties["ИТОГО (CNY)"] = {"number": float(data['ff_total_yuan'])}

        page_id = data.get('notion_page_id')
        if page_id: 
            res = notion.pages.update(page_id=page_id, properties=properties)
        else:
            res = notion.pages.create(parent={"database_id": NOTION_DATABASE_ID}, properties=properties)
            orders[uid]['notion_page_id'] = res['id']
            
        return f"https://notion.so/{res['id'].replace('-', '')}"
    except Exception as e:
        logger.error(f"Notion Error: {e}")
        return None

# ======== ПАРСЕР /PASTE ========
def parse_paste_text(text):
    data = {'client': 'Unknown', 'items': [], 'client_rate': 58.0, 'real_rate': 55.0}
    current_item = None
    lines = text.split('\n')
    
    for line in lines:
        l = line.strip().lower()
        if not l: continue
        
        if 'клиент:' in l:
            data['client'] = normalize_client_name(line.split(':', 1)[1])
        elif 'товар' in l:
            if current_item: data['items'].append(current_item)
            current_item = {'name': 'Товар', 'qty': 0, 'price': 0.0, 'purchase': 0.0, 'delivery_factory': 0.0, 'dims': (0,0,0), 'weight': 0.0}
        elif 'название:' in l: current_item['name'] = line.split(':', 1)[1].strip().title()
        elif 'количество:' in l:
            nums = re.findall(r'\d+', l)
            if nums: current_item['qty'] = int(nums[0])
        elif 'цена клиенту:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if nums: current_item['price'] = float(nums[0])
        elif 'закупка:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if nums: current_item['purchase'] = float(nums[0])
        elif 'доставка:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if nums: current_item['delivery_factory'] = float(nums[0])
        elif 'размеры:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if len(nums) >= 3:
                current_item['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: current_item['weight'] = float(nums[3])
        elif 'курс клиенту:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if nums: data['client_rate'] = float(nums[0])
        elif 'мой курс:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if nums: data['real_rate'] = float(nums[0])

    if current_item: data['items'].append(current_item)
    return data

# ======== COMMANDS ========
async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/paste', '').strip()
    if not text: return

    data = parse_paste_text(text)
    if not data['items']:
        await update.message.reply_text("❌ Ошибка: товары не найдены.")
        return

    subtotal_cny = sum((i['price'] * i['qty']) + i['delivery_factory'] for i in data['items'])
    comm_amd_3pct = (subtotal_cny * 0.03) * data['client_rate']
    
    rule_applied = False
    if comm_amd_3pct < 10000:
        actual_comm_amd = 10000
        actual_comm_cny = 10000 / data['client_rate']
        rule_applied = True
    else:
        actual_comm_amd = int(comm_amd_3pct)
        actual_comm_cny = subtotal_cny * 0.03

    final_total_amd = int((subtotal_cny * data['client_rate']) + actual_comm_amd)
    
    orders[uid] = data
    orders[uid].update({'final_total_amd': final_total_amd, 'total_cny_netto': subtotal_cny, 'rule_applied': rule_applied, 'actual_comm_cny': actual_comm_cny})

    # === АУДИТ ===
    audit = []
    if rule_applied: audit.append(f"• Применено правило 10 000 AMD (3% было {int(comm_amd_3pct)} AMD)")
    missing = [i['name'] for i in data['items'] if i['dims'] == (0,0,0)]
    if missing: audit.append(f"• У <b>{', '.join(missing)}</b> нет размеров. Спрошу в /ff.")
    if audit: await update.message.reply_text("⚠️ <b>Аудит расчета:</b>\n" + "\n".join(audit), parse_mode='HTML')

    # === ИНВОЙС КЛИЕНТУ ===
    inv_lines = ""
    for i in data['items']:
        total_line = (i['price'] * i['qty']) + i['delivery_factory']
        inv_lines += f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {i['delivery_factory']} = {total_line:.1f}¥\n"

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

✅ <b>ИТОГО: {final_total_amd:,} AMD</b>"""

    await update.message.reply_text(msg_client, parse_mode='HTML')

    # === ВНУТРЕННИЙ ЧЕК (ДЛЯ ТЕБЯ) ===
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

    # Проверка Notion
    client_orders, _ = await get_client_orders_from_notion(data['client'])
    keyboard = [[InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')]]
    
    if client_orders:
        orders[uid]['existing_notion_page_id'] = client_orders[0]['id']
        last_date = client_orders[0].get('date', 'неизвестно')
        msg_admin += f"\n\n⚠️ <b>Клиент найден в базе!</b> (Заказ от: {last_date})"
        keyboard.append([InlineKeyboardButton("🔄 Обновить старый заказ", callback_data='paste_update')])
        keyboard.append([InlineKeyboardButton("➕ Сохранить как НОВЫЙ", callback_data='paste_new')])
    else:
        keyboard.append([InlineKeyboardButton("💾 Сохранить в Notion", callback_data='paste_new')])

    await update.message.reply_text(msg_admin, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'gen_excel':
        try:
            file_stream = await create_excel_invoice(uid)
            await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(file_stream, filename=f"Invoice_{orders[uid]['client']}.xlsx"))
        except Exception as e: 
            logger.error(e)
            await query.message.reply_text("❌ Ошибка Excel. Проверь требования.")
    elif query.data in ['paste_new', 'paste_update']:
        if query.data == 'paste_update':
            orders[uid]['notion_page_id'] = orders[uid].get('existing_notion_page_id')
            action = "Обновлен старый заказ"
        else:
            orders[uid]['notion_page_id'] = None
            action = "Создан новый заказ"
            
        url = await save_to_notion(uid)
        
        # Редактируем сообщение, чтобы убрать кнопки
        try:
            await query.edit_message_text(f"{query.message.text}\n\n✅ {action}:\n{url}" if url else f"{query.message.text}\n\n❌ Ошибка Notion")
        except:
            await query.message.reply_text(f"✅ {action}:\n{url}" if url else "❌ Ошибка Notion")

# ======== ФУНКЦИИ FF ========
async def cmd_ff_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in orders: return
    
    items_to_pack = orders[uid]['items']
    boxes = optimize_boxes_with_weight(items_to_pack)
    total_boxes = len(boxes)
    total_weight = sum(b['cur_weight'] for b in boxes)
    cost = total_boxes * BOX_PRICE_CNY
    
    orders[uid]['ff_total_yuan'] = cost
    await save_to_notion(uid)
    
    res = f"📦 <b>Результат FF (Лимит 30кг):</b>\n\nМест: {total_boxes} шт\nОбщий вес: {total_weight:.2f} кг\nСтоимость коробок: {cost:.2f}¥ (по {BOX_PRICE_CNY}¥)"
    await update.message.reply_text(res, parse_mode='HTML')

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('paste', cmd_paste))
    app.add_handler(CommandHandler('ff_done', cmd_ff_done))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.run_polling()

if __name__ == '__main__': main()
