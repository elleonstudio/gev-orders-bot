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

# ======== УТИЛИТЫ ========
def normalize_client_name(name):
    return re.sub(r'\s+', '', name).strip().capitalize()

def get_code(client):
    return f"{client.upper()}-{datetime.now().strftime('%y%m%d')}"

# ======== EXCEL ========
async def create_excel_invoice(uid):
    data = orders[uid]
    items_data = []
    for i, item in enumerate(data['items'], 1):
        delivery = item.get('delivery_factory', 0)
        total_item_cny = (item['price'] * item['qty']) + delivery
        items_data.append({
            "№": i,
            "Название": item['name'],
            "Кол-во": item['qty'],
            "Цена ¥": item['price'],
            "Логистика ¥": delivery,
            "Итого ¥": total_item_cny
        })
    df = pd.DataFrame(items_data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Invoice')
    output.seek(0)
    return output

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
            current_item = {'name': 'Без названия', 'qty': 0, 'price': 0.0, 'purchase': 0.0, 'delivery_factory': 0.0, 'dims': (0,0,0), 'weight': 0.0}
        elif 'название:' in l:
            if current_item is not None: current_item['name'] = line.split(':', 1)[1].strip().title()
        elif 'количество:' in l:
            val = re.findall(r'\d+', l)
            if val and current_item is not None: current_item['qty'] = int(val[0])
        elif 'цена клиенту:' in l:
            val = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if val and current_item is not None: current_item['price'] = float(val[0])
        elif 'закупка:' in l:
            val = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if val and current_item is not None: current_item['purchase'] = float(val[0])
        elif 'доставка:' in l:
            val = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if val and current_item is not None: current_item['delivery_factory'] = float(val[0])
        elif 'размеры:' in l:
            nums = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if len(nums) >= 3 and current_item is not None:
                current_item['dims'] = (float(nums[0]), float(nums[1]), float(nums[2]))
                if len(nums) >= 4: current_item['weight'] = float(nums[3])
        elif 'курс клиенту:' in l:
            val = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if val: data['client_rate'] = float(val[0])
        elif 'мой курс:' in l:
            val = re.findall(r'\d+\.?\d*', l.replace(',', '.'))
            if val: data['real_rate'] = float(val[0])

    if current_item: data['items'].append(current_item)
    return data

# ======== COMMANDS ========
async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.replace('/paste', '').strip()
    if not text:
        await update.message.reply_text("Вставь текст после команды /paste")
        return

    try:
        data = parse_paste_text(text)
        if not data['items']:
            await update.message.reply_text("❌ Не нашел товаров в тексте. Проверь формат.")
            return

        # Расчеты
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
        
        # Сохранение в сессию
        orders[uid] = data
        orders[uid].update({
            'final_total_amd': final_total_amd,
            'total_cny_netto': subtotal_cny,
            'actual_comm_cny': actual_comm_cny,
            'rule_applied': rule_applied
        })

        # Аудит
        audit = []
        if rule_applied: audit.append(f"• Применено правило 10 000 AMD (3% было {int(comm_amd_3pct)} AMD)")
        missing = [i['name'] for i in data['items'] if i['dims'] == (0,0,0)]
        if missing: audit.append(f"• У <b>{', '.join(missing)}</b> нет размеров. Спрошу в /ff.")
        if audit: await update.message.reply_text("⚠️ <b>Аудит расчета:</b>\n" + "\n".join(audit), parse_mode='HTML')

        # Инвойс
        inv_lines = ""
        for i in data['items']:
            line_total = (i['price'] * i['qty']) + i['delivery_factory']
            inv_lines += f"• {i['name']} — {i['qty']} шт\n{i['qty']} × {i['price']} + {i['delivery_factory']} = {line_total:.1f}¥\n"

        fee_label = "Минимальная 10000 AMD" if rule_applied else "3%"
        msg = f"""<b>COMMERCIAL INVOICE: {data['client'].upper()}</b>
📅 Date: {datetime.now().strftime('%d.%m.%Y')}

<b>1. ТОВАРНАЯ ВЕДОМОСТЬ (Logistics Included)</b>
{inv_lines}<code>────────────────────────</code>
<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥

<b>2. КОМИССИЯ И СЕРВИС (Service Fee)</b>
({fee_label}): {actual_comm_cny:.1f}¥

<b>3. ИТОГОВЫЙ РАСЧЕТ (Convertation)</b>
• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥
• Курс обмена (Exchange): {data['client_rate']}

✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"""

        keyboard = [[InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')],
                    [InlineKeyboardButton("💾 Сохранить в Notion", callback_data='paste_save')]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logger.error(traceback.format_exc())
        await update.message.reply_text(f"❌ Произошла ошибка при парсинге. Проверь данные.")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); uid = str(update.effective_user.id)
    if query.data == 'gen_excel':
        try:
            file_stream = await create_excel_invoice(uid)
            await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(file_stream, filename=f"Invoice_{orders[uid]['client']}.xlsx"))
        except: await query.message.reply_text("Ошибка создания Excel. Проверь requirements.txt")
    elif query.data == 'paste_save':
        await query.message.reply_text("💾 Функция сохранения в Notion вызвана. (Добавь свой save_to_notion)")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('paste', cmd_paste))
    app.add_handler(CallbackQueryHandler(callback_handler))
    logger.info("Бот запущен v50 (Robust Parser)")
    app.run_polling()

if __name__ == '__main__': main()
