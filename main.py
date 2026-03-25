async def handle_airtable_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Парсит блок AIRTABLE_EXPORT и отправляет данные в ERAZ ERP v5.0"""
    text = update.message.text
    try:
        # 1. Извлекаем сырые данные
        raw_data = {}
        for line in text.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                raw_data[key.strip()] = value.strip()

        # 2. ОЧИСТКА Invoice_ID (удаляем дату после дефиса)
        raw_invoice = raw_data.get("Invoice_ID", "UNKNOWN")
        # Берем только первую часть до дефиса
        clean_invoice = raw_invoice.split('-')[0]

        # 3. Маппинг данных под твою документацию ERAZ ERP v5.0
        fields = {
            "Заказ/Инвойс": clean_invoice,
            "Сумма закупа (¥)": float(raw_data.get("Real_Purchase_CNY", 0)),
            "Курс Клиент": float(raw_data.get("Client_Rate", 0)),
            "Курс Реал": float(raw_data.get("Real_Rate", 0)),
            "Кол-во пакетов": int(raw_data.get("Total_Qty", 0)),
            "Расход материалов (¥)": float(raw_data.get("China_Logistics_CNY", 0)),
            "Комиссия (%)": 0.1  # Твои стандартные 10% для расчетов
        }

        # 4. Отправка в Airtable
        api = Api(AIRTABLE_TOKEN)
        table = api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME)
        table.create(fields)

        await update.message.reply_text(
            f"✅ Данные очищены и записаны!\n"
            f"📦 Заказ: {clean_invoice}\n"
            f"💰 Прибыль будет рассчитана в Airtable (налог 10% учтен)."
        )

    except Exception as e:
        logger.error(f"Airtable mapping error: {e}")
        await update.message.reply_text(f"❌ Ошибка парсинга или записи: {str(e)}")
