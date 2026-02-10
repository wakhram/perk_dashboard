import streamlit as st
import pandas as pd
import plotly.express as px
import datetime
import os
import hashlib
from get_token import iiko_service
from dateutil.relativedelta import relativedelta
import calendar

# --- КОНФИГУРАЦИЯ ПОЛЕЙ OLAP ---
# Если имена полей на сервере отличаются, поменяйте их здесь
FIELDS = {
    "Date": "OpenDate.Typed",       # Дата открытия (Учетный день)
    "Time": "OpenTime",             # Время открытия
    "DishName": "DishName",         # Название блюда
    "Revenue": "DishDiscountSumInt",# Выручка (с учетом скидок)
    "Quantity": "DishAmountInt",    # Количество
    "CheckID": "OrderNum",          # Номер чека (вместо UniqOrderId)
    "IsDeleted": "OrderDeleted",    # Флаг удаления
    "PayTypes": "PayTypes",         # Тип оплаты
    "Comment": "OrderComment"       # Комментарий к заказу
}

st.set_page_config(page_title="Perk Dashboard", layout="wide")
st.title("📊 Дашборд продаж")

# --- АВТОРИЗАЦИЯ ---
# Получаем пароль из переменных окружения
APP_PASSWORD = os.getenv("APP_PASSWORD")
AUTH_QUERY_KEY = "auth"

def make_auth_token(password):
    salt = os.getenv("APP_AUTH_SALT", "perk_dashboard")
    token_src = f"{salt}:{password}"
    return hashlib.sha256(token_src.encode("utf-8")).hexdigest()

def get_auth_token_from_url():
    try:
        params = st.query_params()
        return params.get(AUTH_QUERY_KEY, [None])[0]
    except Exception:
        return None

def set_auth_token_in_url(token):
    try:
        if token:
            st.query_params(**{AUTH_QUERY_KEY: token})
        else:
            st.query_params()
    except Exception:
        pass

if not APP_PASSWORD:
    st.error("⚠️ Ошибка конфигурации: не задан пароль приложения (APP_PASSWORD).")
    st.stop()

if "auth_ok" not in st.session_state:
    st.session_state["auth_ok"] = False

expected_token = make_auth_token(APP_PASSWORD)
url_token = get_auth_token_from_url()
if url_token == expected_token:
    st.session_state["auth_ok"] = True

if not st.session_state["auth_ok"]:
    password = st.sidebar.text_input("🔒 Введите пароль администратора", type="password")
    remember_password = st.sidebar.checkbox("💾 Запомнить на этом устройстве", value=True)

    if password:
        if password == APP_PASSWORD:
            st.session_state["auth_ok"] = True
            if remember_password:
                set_auth_token_in_url(expected_token)
            else:
                set_auth_token_in_url(None)
        else:
            st.warning("Пожалуйста, введите верный пароль для доступа к данным.")
            st.stop()
    else:
        st.stop()

# --- ФУНКЦИЯ ЗАГРУЗКИ ДАННЫХ ---
@st.cache_data(ttl=300)
def fetch_olap_data(date_from, date_to, silent=False):
    """
    Запрашивает данные из iiko OLAP по дате.
    """
    endpoint = "/resto/api/v2/reports/olap"
    
    # Фильтр по дате
    filters = {
        FIELDS["Date"]: {
            "filterType": "DateRange",
            "periodType": "CUSTOM",
            "from": str(date_from),
            "to": str(date_to),
            "includeLow": "true",
            "includeHigh": "true"
        }
    }

    payload = {
        "reportType": "SALES",
        "buildSummary": "false",
        "groupByRowFields": [
            FIELDS["DishName"],
            FIELDS["Date"],
            FIELDS["Time"],
            FIELDS["CheckID"],
            FIELDS["IsDeleted"],
            FIELDS["PayTypes"],
            FIELDS["Comment"]
        ],
        "aggregateFields": [
            FIELDS["Revenue"],
            FIELDS["Quantity"]
        ],
        "filters": filters
    }

    try:
        response = iiko_service.request("POST", endpoint, json=payload)
        if response.status_code == 200:
            data = response.json()
            if "data" in data:
                return pd.DataFrame(data["data"])
            else:
                if not silent: st.error(f"Некорректный формат ответа: {data}")
                return pd.DataFrame()
        else:
            if not silent: st.error(f"Ошибка сервера: {response.status_code} {response.text}")
            return pd.DataFrame()
    except Exception as e:
        if not silent: st.error(f"Ошибка при запросе: {e}")
        return pd.DataFrame()

# --- ФУНКЦИЯ ЗАГРУЗКИ СЕБЕСТОИМОСТИ ---
def normalize_dish_name(series):
    return (
        series.astype(str)
        .str.replace("\u00A0", " ", regex=False)
        .str.strip()
        .str.replace("ё", "е", regex=False)
        .str.replace(",", ".", regex=False)
        .str.replace(r"\s+", " ", regex=True)
        .str.lower()
    )

def load_tech_map():
    """Загружает себестоимость из файла cost_map.tsv"""
    file_path = os.path.join(os.path.dirname(__file__), "cost_map.tsv")
    if not os.path.exists(file_path):
        return pd.DataFrame()
    
    try:
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if "\t" in line:
                    parts = line.split("\t", 1)
                else:
                    parts = line.rsplit(" ", 1)
                if len(parts) != 2:
                    continue
                rows.append(parts)

        df = pd.DataFrame(rows, columns=["DishName", "Cost"])

        df = df[df["DishName"].notna()].copy()
        df["DishName"] = df["DishName"].astype(str)
        df["Cost"] = (
            df["Cost"]
            .astype(str)
            .str.replace("\u00A0", " ", regex=False)
            .str.replace(" ", "", regex=False)
            .str.replace(",", ".", regex=False)
        )
        df["Cost"] = pd.to_numeric(df["Cost"], errors='coerce')
        df = df.dropna(subset=["Cost"])

        df["DishNameNormalized"] = normalize_dish_name(df["DishName"])

        # Убираем дубликаты, оставляя последнюю цену (удобно для обновлений)
        df = df.drop_duplicates(subset=["DishNameNormalized"], keep="last")
        return df[["DishNameNormalized", "Cost"]]
    except Exception:
        return pd.DataFrame()

def apply_costs_to_df(df, df_tech=None):
    if df.empty:
        return df
    df = df.copy()
    if df_tech is None:
        df_tech = load_tech_map()
    df[FIELDS["Revenue"]] = pd.to_numeric(df[FIELDS["Revenue"]], errors='coerce').fillna(0)
    df[FIELDS["Quantity"]] = pd.to_numeric(df[FIELDS["Quantity"]], errors='coerce').fillna(0)
    if not df_tech.empty:
        df[FIELDS["DishName"]] = df[FIELDS["DishName"]].astype(str)
        df["DishNameNormalized"] = normalize_dish_name(df[FIELDS["DishName"]])
        df = df.merge(
            df_tech,
            left_on="DishNameNormalized",
            right_on="DishNameNormalized",
            how="left"
        )
        df["Cost"] = pd.to_numeric(df["Cost"], errors='coerce').fillna(0)
    else:
        df["Cost"] = 0
    df["TotalCost"] = df["Cost"] * df[FIELDS["Quantity"]]
    df["GrossProfit"] = df[FIELDS["Revenue"]] - df["TotalCost"]
    return df

# --- SIDEBAR: НАСТРОЙКИ ПЕРИОДА ---
st.sidebar.header("Настройка периода")

today = datetime.date.today()

# 1. Выбор текущего периода
st.sidebar.subheader("Текущий период")
curr_start_date = st.sidebar.date_input("Начало", today, key="d1")
curr_end_date = st.sidebar.date_input("Конец", today, key="d2")

# Преобразуем в кортеж для корректной работы с st.date_input
if isinstance(curr_start_date, tuple):
    curr_start_date = curr_start_date[0]
if isinstance(curr_end_date, tuple):
    curr_end_date = curr_end_date[-1]
if curr_end_date is None:
    curr_end_date = curr_start_date

# 2. Выбор периода для сравнения
st.sidebar.subheader("Период для сравнения")
period_options = ["Автоматический", "Смежный", "Выбрать период"]
period_type = st.sidebar.selectbox("Режим", period_options)

prev_start_date = None
prev_end_date = None

if period_type == "Автоматический":
    delta_days = (curr_end_date - curr_start_date).days + 1
    
    # 1. Один день
    if delta_days == 1:
        # Ищем ближайший предыдущий рабочий день
        candidate = curr_start_date - datetime.timedelta(days=1)
        found_working = False
        # Проверяем до 14 дней назад
        for i in range(14):
            check_date = candidate - datetime.timedelta(days=i)
            # Проверяем выручку (silent=True чтобы не спамить ошибками)
            df_check = fetch_olap_data(check_date, check_date, silent=True)
            if not df_check.empty:
                # Фильтруем удаленные и считаем сумму
                valid_rows = df_check[df_check[FIELDS["IsDeleted"]] == 'NOT_DELETED']
                rev = pd.to_numeric(valid_rows[FIELDS["Revenue"]], errors='coerce').sum()
                if rev > 0:
                    prev_start_date = check_date
                    prev_end_date = check_date
                    found_working = True
                    break
        
        if not found_working:
            # Если не нашли, просто берем вчера
            prev_start_date = curr_start_date - datetime.timedelta(days=1)
            prev_end_date = prev_start_date

    # 2. Текущий месяц (частичный) -> Полный прошлый месяц
    # Условие: Начало - 1 число текущего месяца, Конец - Сегодня (или около того)
    elif (curr_start_date.day == 1 and 
          curr_start_date.month == today.month and 
          curr_start_date.year == today.year and 
          curr_end_date >= today - datetime.timedelta(days=1)):
        
        # Берем полный предыдущий месяц
        prev_start_date = (curr_start_date - relativedelta(months=1)).replace(day=1)
        prev_end_date = curr_start_date - datetime.timedelta(days=1) # Последний день прошлого месяца

    # 3. Неделя (7 дней) -> Прошлая неделя
    elif delta_days == 7:
        prev_start_date = curr_start_date - datetime.timedelta(days=7)
        prev_end_date = curr_end_date - datetime.timedelta(days=7)

    # 4. Полный месяц (примерно 28-31 день, начинается с 1-го) -> Прошлый месяц
    elif (curr_start_date.day == 1 and (curr_end_date + datetime.timedelta(days=1)).day == 1):
        prev_start_date = curr_start_date - relativedelta(months=1)
        prev_end_date = curr_end_date - relativedelta(months=1)

    # 5. Год -> Прошлый год
    elif delta_days >= 365:
        prev_start_date = curr_start_date - relativedelta(years=1)
        prev_end_date = curr_end_date - relativedelta(years=1)

    # 6. Нестандартный период (например 17 дней) -> Те же дни прошлого месяца
    else:
        prev_start_date = curr_start_date - relativedelta(months=1)
        prev_end_date = curr_end_date - relativedelta(months=1)

elif period_type == "Смежный":
    delta_days = (curr_end_date - curr_start_date).days + 1
    
    # День -> Тот же день недели на прошлой неделе
    if delta_days == 1:
        prev_start_date = curr_start_date - datetime.timedelta(days=7)
        prev_end_date = prev_start_date
    
    # Текущий месяц (частичный) -> Полный тот же месяц прошлого года
    elif (curr_start_date.day == 1 and 
          curr_start_date.month == today.month and 
          curr_start_date.year == today.year and 
          curr_end_date >= today - datetime.timedelta(days=1)):
        prev_start_date = curr_start_date - relativedelta(years=1)
        prev_end_date = prev_start_date + relativedelta(months=1) - datetime.timedelta(days=1)

    # Полный месяц -> Тот же месяц прошлого года
    elif (curr_start_date.day == 1 and (curr_end_date + datetime.timedelta(days=1)).day == 1):
        prev_start_date = curr_start_date - relativedelta(years=1)
        prev_end_date = curr_end_date - relativedelta(years=1)
        
    else:
        st.sidebar.warning("⚠️ Смежный режим работает корректно только для 1 дня или полного месяца.")
        # Fallback: Прошлый год
        prev_start_date = curr_start_date - relativedelta(years=1)
        prev_end_date = curr_end_date - relativedelta(years=1)

else: # Выбрать период
    default_prev = curr_start_date - datetime.timedelta(days=1)
    prev_start_date = st.sidebar.date_input("Начало сравнения", default_prev, key="d3")
    prev_end_date = st.sidebar.date_input("Конец сравнения", default_prev, key="d4")

# Отображение выбранных дат для информации
st.sidebar.info(f"""
**Текущий:** {curr_start_date.strftime('%d.%m.%Y')} - {curr_end_date.strftime('%d.%m.%Y')}  
**Сравнение:** {prev_start_date.strftime('%d.%m.%Y')} - {prev_end_date.strftime('%d.%m.%Y')}
""")

# --- ЗАГРУЗКА ДАННЫХ ---
with st.spinner("Получение данных из iiko..."):
    df_curr_raw = fetch_olap_data(curr_start_date, curr_end_date)
    df_prev_raw = fetch_olap_data(prev_start_date, prev_end_date)

# Разделяем данные на активные и удаленные уже в коде
# df_curr_raw может быть пустым, нужна проверка
if not df_curr_raw.empty:
    df_curr = df_curr_raw[df_curr_raw[FIELDS["IsDeleted"]] == 'NOT_DELETED'].copy()
    df_del = df_curr_raw[df_curr_raw[FIELDS["IsDeleted"]] == 'DELETED'].copy()
    
    # --- ИНТЕГРАЦИЯ С СЕБЕСТОИМОСТЬЮ ---
    df_curr = apply_costs_to_df(df_curr)
else:
    df_curr = pd.DataFrame()
    df_del = pd.DataFrame()

if not df_prev_raw.empty:
    df_prev = df_prev_raw[df_prev_raw[FIELDS["IsDeleted"]] == 'NOT_DELETED'].copy()
    df_prev = apply_costs_to_df(df_prev)
else:
    df_prev = pd.DataFrame()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def format_currency(number):
    """Форматирует число в вид 1 234 567,00"""
    try:
        # Форматируем с 2 знаками после запятой, пробелами как разделителями тысяч и запятой для дроби
        return "{:,.2f}".format(number).replace(",", " ").replace(".", ",")
    except (ValueError, TypeError):
        return "0,00"

def format_integer(number):
    """Форматирует целое число в вид 1 234 567"""
    try:
        # Форматируем с пробелами как разделителями тысяч
        return "{:,.0f}".format(number).replace(",", " ")
    except (ValueError, TypeError):
        return "0"

def calculate_metrics(df):
    if df.empty:
        return 0, 0, 0
    
    # Конвертация в числа
    if FIELDS["Revenue"] in df.columns:
        df[FIELDS["Revenue"]] = pd.to_numeric(df[FIELDS["Revenue"]], errors='coerce').fillna(0)
    
    revenue = df[FIELDS["Revenue"]].sum()
    # Кол-во чеков = уникальные ID заказов
    # OrderNum может повторяться, поэтому считаем уникальные связки (Дата, Время, Номер)
    cols_to_count = [FIELDS["Date"], FIELDS["Time"], FIELDS["CheckID"]]
    if all(c in df.columns for c in cols_to_count):
        checks = df[cols_to_count].drop_duplicates().shape[0]
    else:
        checks = 0

    avg_check = revenue / checks if checks > 0 else 0
    
    return revenue, checks, avg_check

rev_c, checks_c, avg_c = calculate_metrics(df_curr)
rev_p, checks_p, avg_p = calculate_metrics(df_prev)

# --- 1. БЛОКИ МЕТРИК ---
st.markdown("### 📈 Показатели")

# Расчет дельт
delta_rev = rev_c - rev_p
delta_checks = checks_c - checks_p
delta_avg = avg_c - avg_p

# --- Функция для цветного отображения дельты с процентами ---
def format_delta_html(delta_value, prev_value, is_currency=True):
    """Форматирует дельту в HTML со знаком, цветом, валютой и процентом."""
    color = "green" if delta_value > 0 else "red" if delta_value < 0 else "gray"
    
    # Абсолютное значение
    formatted_abs_value = format_currency(delta_value) if is_currency else format_integer(delta_value)
    abs_display = f"+{formatted_abs_value}" if delta_value > 0 else formatted_abs_value
    
    # Добавляем валюту, если нужно
    if is_currency:
        abs_display += " ₸"

    # Процентное изменение
    percent_display = ""
    # Избегаем деления на ноль
    if prev_value != 0:
        percentage = (delta_value / prev_value) * 100
        percent_sign = "+" if percentage > 0 else ""
        percent_display = f" ({percent_sign}{percentage:.1f}%)"

    full_display = f"{abs_display}{percent_display}"

    return f'<span style="color: {color}; font-weight: bold;">{full_display}</span>'

# CSS для адаптивных карточек (Grid Layout)
# Это позволяет автоматически определять ширину экрана:
# На ПК будет 3 колонки, на телефоне карточки станут друг под другом.
st.markdown("""
<style>
    .metric-container {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
        gap: 16px;
        margin-bottom: 20px;
    }
    .metric-card {
        background-color: var(--secondary-background-color);
        padding: 20px;
        border-radius: 10px;
        border: 1px solid rgba(128, 128, 128, 0.1);
        box-shadow: 0 2px 5px rgba(0,0,0,0.05);
    }
    .metric-title {
        font-size: 16px;
        font-weight: 600;
        color: var(--text-color);
        opacity: 0.9;
        margin-bottom: 8px;
    }
    .metric-value {
        font-size: 24px;
        font-weight: 700;
        color: var(--text-color);
        margin-bottom: 4px;
    }
    .metric-sub {
        font-size: 14px;
        color: var(--text-color);
        opacity: 0.7;
    }
</style>
""", unsafe_allow_html=True)

st.markdown(f"""
<div class="metric-container">
    <div class="metric-card">
        <div class="metric-title">💰 Выручка</div>
        <div class="metric-value">{format_currency(rev_c)} ₸</div>
        <div class="metric-sub">Предыдущий: {format_currency(rev_p)} ₸</div>
        <div class="metric-sub">Изменение: {format_delta_html(delta_rev, rev_p, is_currency=True)}</div>
    </div>
    <div class="metric-card">
        <div class="metric-title">🧾 Количество чеков</div>
        <div class="metric-value">{format_integer(checks_c)}</div>
        <div class="metric-sub">Предыдущий: {format_integer(checks_p)}</div>
        <div class="metric-sub">Изменение: {format_delta_html(delta_checks, checks_p, is_currency=False)}</div>
    </div>
    <div class="metric-card">
        <div class="metric-title">💳 Средний чек</div>
        <div class="metric-value">{format_currency(avg_c)} ₸</div>
        <div class="metric-sub">Предыдущий: {format_currency(avg_p)} ₸</div>
        <div class="metric-sub">Изменение: {format_delta_html(delta_avg, avg_p, is_currency=True)}</div>
    </div>
</div>
""", unsafe_allow_html=True)

st.divider()

# --- 1.1 ФИНАНСОВЫЙ РЕЗУЛЬТАТ ---
st.markdown("### 💰 Финансовый результат")

def calc_financials(df):
    if df.empty:
        return 0, 0, 0
    revenue = pd.to_numeric(df[FIELDS["Revenue"]], errors='coerce').fillna(0).sum()
    cogs = pd.to_numeric(df["TotalCost"], errors='coerce').fillna(0).sum() if "TotalCost" in df.columns else 0
    gross = revenue - cogs
    return revenue, cogs, gross

rev_curr, cogs_curr, gross_curr = calc_financials(df_curr)
rev_prev, cogs_prev, gross_prev = calc_financials(df_prev)

delta_rev = rev_curr - rev_prev
delta_cogs = cogs_curr - cogs_prev
delta_gross = gross_curr - gross_prev

col_f1, col_f2, col_f3 = st.columns(3)
col_f1.metric("Выручка", f"{format_integer(rev_curr)} ₸", delta=f"{format_integer(delta_rev)} ₸")
col_f2.metric("Себестоимость", f"{format_integer(cogs_curr)} ₸", delta=f"{format_integer(delta_cogs)} ₸")
col_f3.metric("Валовая прибыль", f"{format_integer(gross_curr)} ₸", delta=f"{format_integer(delta_gross)} ₸")

st.caption(f"Сравнение: выручка {format_integer(rev_prev)} ₸, себестоимость {format_integer(cogs_prev)} ₸, валовая прибыль {format_integer(gross_prev)} ₸.")

st.warning("⚠️ Обратите внимание: Себестоимость считается только по позициям из файла cost_map.tsv. Остальные позиции идут без себестоимости.")

# --- 2. ТАБЛИЦА ПРОДАЖ ---
st.markdown("### 🍔 Продажи по позициям (Текущий период)")
if not df_curr.empty:
    sales_table = df_curr.groupby(FIELDS["DishName"]).agg({
        FIELDS["Quantity"]: "sum",
        FIELDS["Revenue"]: "sum",
        "TotalCost": "sum",
        "GrossProfit": "sum"
    }).reset_index().sort_values(FIELDS["Revenue"], ascending=False)
    
    sales_table.columns = ["Позиция", "Кол-во", "Выручка", "Себестоимость", "Валовая прибыль"]
    
    # Форматирование колонки "Выручка" для лучшей читаемости
    sales_table["Выручка"] = sales_table["Выручка"].apply(format_currency)
    sales_table["Себестоимость"] = sales_table["Себестоимость"].apply(format_currency)
    sales_table["Валовая прибыль"] = sales_table["Валовая прибыль"].apply(format_currency)

    st.dataframe(sales_table, use_container_width=True, hide_index=True)
else:
    st.info("Нет данных за текущий период.")

# --- 3. УДАЛЕНИЯ ---
st.markdown("### 🗑️ Удаленные чеки")

st.info("""
**ℹ️ Справка:**
В этой таблице показаны **полностью удаленные чеки** (Случай 2: когда весь чек был удален/аннулирован).
Здесь отображаются сторнированные заказы и удаления с суммой. Технические удаления с суммой 0 скрыты.

*Примечание: Удаления отдельных позиций внутри открытого заказа (до печати чека) здесь не отображаются, так как они не фиксируются в отчете о продажах.*
""")

if not df_del.empty:
    # Заполняем пропуски в типе оплаты
    df_del[FIELDS["PayTypes"]] = df_del[FIELDS["PayTypes"]].fillna("Не указано")
    df_del[FIELDS["Comment"]] = df_del[FIELDS["Comment"]].fillna("")
    
    # Группируем по чеку
    del_table = df_del.groupby([FIELDS["CheckID"], FIELDS["Time"], FIELDS["PayTypes"], FIELDS["Comment"]]).agg({
        FIELDS["Revenue"]: "sum"
    }).reset_index()
    
    # Фильтруем технические удаления с нулевой суммой (например, чек 60848)
    del_table = del_table[del_table[FIELDS["Revenue"]] != 0]
    
    if not del_table.empty:
        del_table.columns = ["ID Чека", "Время", "Тип оплаты", "Комментарий", "Сумма"]
        del_table["Сумма"] = del_table["Сумма"].apply(format_currency)
        st.dataframe(del_table, use_container_width=True, hide_index=True)
    else:
        st.info("Нет удаленных чеков с суммой > 0.")
else:
    st.info("Нет удаленных чеков.")

# --- 4. ГРАФИК ПО ЧАСАМ ---
st.markdown("### 🕒 Динамика количества чеков по часам")
def get_hourly_data(df, label):
    """
    Подготавливает данные для графика по часам.
    Гарантирует наличие всех 24 часов в данных для непрерывного графика.
    """
    if df.empty:
        # Возвращаем пустой DataFrame с нужными колонками для корректного concat
        return pd.DataFrame(columns=['Hour', 'CheckCount', 'Period'])

    df = df.copy()

    # Более надежное извлечение часа из времени
    df["Hour"] = pd.to_datetime(df[FIELDS["Time"]], errors='coerce').dt.hour
    df.dropna(subset=["Hour"], inplace=True)
    df["Hour"] = df["Hour"].astype(int)

    # Считаем уникальные чеки (Date + Time + CheckID)
    unique_checks = df[[FIELDS["Date"], FIELDS["Time"], FIELDS["CheckID"], "Hour"]].drop_duplicates()
    hourly_sales = unique_checks.groupby("Hour").size().reset_index(name="CheckCount")

    # Создаем полный диапазон часов (0-23)
    all_hours = pd.DataFrame({"Hour": range(24)})

    # Объединяем с данными о продажах, заполняя пропуски нулями
    hourly_sales = pd.merge(all_hours, hourly_sales, on="Hour", how="left").fillna(0)
    hourly_sales["Period"] = label

    return hourly_sales

h_curr = get_hourly_data(df_curr, "Текущий")
h_prev = get_hourly_data(df_prev, "Прошлый")

if not h_curr.empty or not h_prev.empty:
    chart_df = pd.concat([h_curr, h_prev])

    # Убедимся, что данные для оси Y числовые
    chart_df["CheckCount"] = pd.to_numeric(chart_df["CheckCount"], errors='coerce').fillna(0)

    fig = px.line(chart_df, x="Hour", y="CheckCount", color="Period", markers=True,
                  color_discrete_map={"Текущий": "#2ecc71", "Прошлый": "#3498db"},
                  labels={"Hour": "Час", "CheckCount": "Кол-во чеков"},
                  template="plotly_white") # Базовая светлая тема

    # Делаем линии плавными
    fig.update_traces(
        line_shape='spline',
        mode='lines+markers+text',
        texttemplate='%{y}',
        textposition='top center'
    )

    fig.update_xaxes(tickvals=list(range(0, 24, 1)), gridcolor='rgba(0,0,0,0.1)') 
    fig.update_yaxes(gridcolor='rgba(0,0,0,0.1)')
    
    fig.update_layout(
        yaxis_title="Количество чеков",
        hovermode="x unified", # Единая подсказка для сравнения точек
        paper_bgcolor="white", # Принудительный белый фон
        plot_bgcolor="white",
        font=dict(color="black"), # Принудительный черный текст
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=20, r=20, t=30, b=20)
    )

    # theme=None отключает перекрашивание графика Streamlit-ом в темную тему
    st.plotly_chart(
        fig,
        use_container_width=True,
        theme=None,
        config={"staticPlot": True, "displayModeBar": False}
    )

    fig_bar = px.bar(
        chart_df,
        x="Hour",
        y="CheckCount",
        color="Period",
        barmode="group",
        color_discrete_map={"Текущий": "#2ecc71", "Прошлый": "#3498db"},
        labels={"Hour": "Час", "CheckCount": "Кол-во чеков"},
        template="plotly_white"
    )

    fig_bar.update_xaxes(tickvals=list(range(0, 24, 1)), gridcolor='rgba(0,0,0,0.1)')
    fig_bar.update_yaxes(gridcolor='rgba(0,0,0,0.1)')
    fig_bar.update_traces(
        texttemplate='%{y}',
        textposition='outside'
    )
    fig_bar.update_layout(
        yaxis_title="Количество чеков",
        paper_bgcolor="white",
        plot_bgcolor="white",
        font=dict(color="black"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=20, r=20, t=30, b=20)
    )

    st.plotly_chart(
        fig_bar,
        use_container_width=True,
        theme=None,
        config={"staticPlot": True, "displayModeBar": False}
    )
else:
    st.info("Недостаточно данных для графика.")
