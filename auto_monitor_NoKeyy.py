import requests
import json
import time
from datetime import datetime
import pytz
import re
import csv
import os
import gc
import schedule


try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False
    print("⚠️  gspread belum terinstall. Jalankan: pip install gspread google-auth")


API_KEY = "----"
PRIMARY_MODEL = "deepseek/deepseek-v4-flash"
BASE_URL = "http://192.168.1.118:5000"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CSV_LOG_FILE = "llm_experiments_log.csv"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "http://localhost:5000",
    "X-Title": "SmartGreenhouse"
}

WIB = pytz.timezone('Asia/Jakarta')

# =============================================================================
# KONFIGURASI GOOGLE SHEETS
# =============================================================================

GSHEET_CREDENTIALS_FILE = "/home/garlic/greenhouse_cileles.json"
GSHEET_SPREADSHEET_ID = "1Eav1CZifNXL5FUEJh29MvR_gHf-wxu6hyf6Qan94WW4"
GSHEET_WORKSHEET_NAME = "LLM_Experiments_Log"
_gsheet_ws = None


def init_google_sheets():
    global _gsheet_ws

    if not GSPREAD_AVAILABLE:
        return None

    if not os.path.isfile(GSHEET_CREDENTIALS_FILE):
        print(f"   ⚠️  Credentials tidak ditemukan: {GSHEET_CREDENTIALS_FILE}")
        print("   ↳  Sistem tetap berjalan dengan CSV lokal saja.")
        return None

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_file(
            GSHEET_CREDENTIALS_FILE, scopes=scopes
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GSHEET_SPREADSHEET_ID)

        try:
            ws = sheet.worksheet(GSHEET_WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            # Header baru dengan UTC
            ws = sheet.add_worksheet(
                title=GSHEET_WORKSHEET_NAME, rows=10000, cols=13
            )
            print(
                f"   ✅ Tab '{GSHEET_WORKSHEET_NAME}' dibuat baru di Google Sheets.")

        if not ws.row_values(1):
            ws.append_row([
                "Timestamp_WIB", "Timestamp_UTC", "Suhu", "Hum", "Lux", "Soil",
                "Prompt_Type", "Recommendation",
                "P_Tokens", "C_Tokens", "Total_Tokens",
                "Status_Log", "Cache_Hit"
            ])
            print("   ✅ Header Google Sheets dibuat.")

        _gsheet_ws = ws
        print(f"   ✅ Google Sheets terhubung → tab: '{GSHEET_WORKSHEET_NAME}'")
        return ws

    except Exception as e:
        print(f"   ⚠️  Gagal koneksi ke Google Sheets: {e}")
        print("   ↳  Sistem tetap berjalan dengan CSV lokal saja.")
        return None


def save_to_gsheet(row_data):
    global _gsheet_ws

    if _gsheet_ws is None:
        return

    try:
        _gsheet_ws.append_row(row_data, value_input_option="USER_ENTERED")
        print("   ☁️  Tersimpan ke Google Sheets.")

    except Exception as e:
        print(f"   ⚠️  Gagal ke Google Sheets: {e} — mencoba reconnect...")
        _gsheet_ws = init_google_sheets()
        if _gsheet_ws:
            try:
                _gsheet_ws.append_row(
                    row_data, value_input_option="USER_ENTERED")
                print("   ☁️  Reconnect berhasil. Tersimpan ke Google Sheets.")
            except Exception as e2:
                print(
                    f"   ❌  Gagal setelah reconnect: {e2}. Data hanya di CSV lokal.")



CACHE_THRESHOLD = {
    "suhu":  0.3,   
    "hum":   2.0,   
    "lux":   10.0,  
    "soil":  2.0,   
}

CACHE_MAX_AGE_DAYS = None

PROMPTS = {
    "Zero-Shot": """Berdasarkan data sensor greenhouse berikut, berikan rekomendasi pengelolaan tanaman bawang putih tunggal.

Data sensor saat ini:
- Suhu udara      : {suhu} °C
- Kelembapan udara : {hum} %
- Intensitas cahaya: {lux} lux (Estimasi Sensor Analog)
- Kelembapan tanah : {soil} %

Catatan Sistem: Angka Lux di atas adalah hasil estimasi dari sensor LDR yang belum dikalibrasi presisi. Gunakan data tersebut sebagai indikator tren cuaca relatif (misal: > 1000 lux berarti terik/terang, < 300 lux berarti mendung/gelap) untuk menentukan keputusan, bukan sebagai rujukan angka mutlak.

Berikan rekomendasi tindakan perawatan yang perlu dilakukan.
Output HARUS dalam format JSON valid berikut, tanpa teks tambahan di luar JSON:
{{
    "status": "Aman/Waspada/Bahaya",
    "analisis": "Penjelasan kondisi lingkungan saat ini",
    "rekomendasi": ["Tindakan 1", "Tindakan 2", "Tindakan 3"]
}}""",

    "Role-Based": """Anda berperan sebagai seorang agronom spesialis budidaya bawang putih tunggal yang berpengalaman.

Data sensor greenhouse saat ini:
- Suhu udara      : {suhu} °C
- Kelembapan udara : {hum} %
- Intensitas cahaya: {lux} lux (Estimasi)
- Kelembapan tanah : {soil} %

Rentang kondisi optimal bawang putih tunggal:
- Suhu            : 18–26 °C (> 30°C berbahaya, < 15°C terlalu dingin)
- Kelembapan udara : 60–80 %
- Kelembapan tanah : 50–70 % (< 40% kering, > 80% risiko busuk akar)

*Catatan Khusus Intensitas Cahaya:* Data Lux yang diterima adalah hasil estimasi sensor perangkat keras. Abaikan angka optimal literatur yang kaku. Gunakan pedoman toleransi estimasi ini:
- Gelap / Malam   : 0 - 50 lux
- Remang / Mendung: 51 - 400 lux (Pertimbangkan lampu UV)
- Cerah Normal    : 401 - 1500 lux (Kondisi aman fotosintesis)
- Sangat Terik    : > 1500 lux (Waspada suhu naik, pertimbangkan peneduh)

Berdasarkan data di atas, jelaskan kondisi tanaman saat ini dan berikan saran tindakan yang sebaiknya segera dilakukan oleh petani.
Output HARUS dalam format JSON valid berikut, tanpa teks tambahan di luar JSON:
{{
    "status": "Aman/Waspada/Bahaya",
    "analisis": "Penjelasan kondisi lingkungan berdasarkan perspektif agronomi",
    "rekomendasi": ["Tindakan 1", "Tindakan 2", "Tindakan 3"]
}}""",

    "CoT": """Anda berperan sebagai seorang agronom spesialis budidaya bawang putih tunggal yang berpengalaman.

Data sensor greenhouse saat ini:
- Suhu udara      : {suhu} °C
- Kelembapan udara : {hum} %
- Intensitas cahaya: {lux} lux (Estimasi)
- Kelembapan tanah : {soil} %

Analisis data sensor di atas secara bertahap menggunakan langkah berikut:
1. Evaluasi suhu: apakah berada dalam rentang optimal (18–26 °C), waspada, atau berbahaya?
2. Evaluasi kelembapan udara: apakah berada dalam rentang optimal (60–80 %)?
3. Evaluasi intensitas cahaya: Ingat bahwa nilai ini adalah estimasi kalibrasi sensor. Gunakan asumsi bahwa rentang 400-1500 lux adalah siang cerah yang wajar. Apakah kondisi saat ini tergolong gelap, normal, atau terik berlebihan?
4. Evaluasi kelembapan tanah: apakah berada dalam rentang optimal (50–70 %)?
5. Berdasarkan seluruh evaluasi di atas, tentukan status keseluruhan dan rekomendasi tindakan prioritas.

Output HARUS dalam format JSON valid berikut, tanpa teks tambahan di luar JSON:
{{
    "status": "Aman/Waspada/Bahaya",
    "analisis": "Ringkasan hasil evaluasi bertahap seluruh parameter",
    "rekomendasi": ["Tindakan prioritas 1", "Tindakan prioritas 2", "Tindakan prioritas 3"]
}}"""
}


def convert_ldr_to_lux(ldr_adc_value, constant_a=500000, gamma=0.75, vcc=5.0, r_fixed=10000.0):
    try:
        adc_val = float(ldr_adc_value)
        adc_val = max(1.0, min(1022.0, adc_val))
        v_out = adc_val * (vcc / 1023.0)
        resistance_ohm = (v_out * r_fixed) / (vcc - v_out)
        if resistance_ohm <= 0:
            return 0.0
        lux = (constant_a / resistance_ohm) ** (1.0 / gamma)
        return round(lux, 1)
    except (ValueError, TypeError, ZeroDivisionError) as e:
        print(f"   ⚠️ Gagal konversi LDR ke Lux: {e}")
        return 0.0


def check_cache_csv(suhu, hum, lux, soil, prompt_type):
    if not os.path.isfile(CSV_LOG_FILE):
        return None

    matched_rows = []

    try:
        with open(CSV_LOG_FILE, mode='r', encoding='utf-8') as file:
            reader = csv.DictReader(file)

            required_cols = {"Suhu", "Hum", "Lux", "Soil",
                             "Prompt_Type", "Recommendation", "Status_Log"}
            if not required_cols.issubset(set(reader.fieldnames or [])):
                return None

            for row in reader:
                if row.get("Status_Log", "") != "Success":
                    continue
                if row.get("Prompt_Type", "") != prompt_type:
                    continue
                if not row.get("Recommendation", "").strip():
                    continue

                
                ts_col = "Timestamp_WIB" if "Timestamp_WIB" in row else "Timestamp"
                ts_val = row.get(ts_col, "")

                if CACHE_MAX_AGE_DAYS is not None and ts_val:
                    try:
                        row_time = datetime.strptime(
                            ts_val, "%Y-%m-%d %H:%M").replace(tzinfo=WIB)
                        age_days = (datetime.now(WIB) - row_time).days
                        if age_days > CACHE_MAX_AGE_DAYS:
                            continue
                    except ValueError:
                        continue

                try:
                    diff_suhu = abs(float(row["Suhu"]) - float(suhu))
                    diff_hum = abs(float(row["Hum"]) - float(hum))
                    diff_lux = abs(float(row["Lux"]) - float(lux))
                    diff_soil = abs(float(row["Soil"]) - float(soil))
                except (ValueError, KeyError):
                    continue

                if (diff_suhu <= CACHE_THRESHOLD["suhu"] and
                    diff_hum <= CACHE_THRESHOLD["hum"] and
                    diff_lux <= CACHE_THRESHOLD["lux"] and
                        diff_soil <= CACHE_THRESHOLD["soil"]):
                    matched_rows.append(row)

    except Exception as e:
        print(f"   ⚠️ Gagal baca CSV untuk cache: {e}")
        return None

    if not matched_rows:
        return None

    # Mengembalikan sembarang data valid sebagai penanda True
    return matched_rows[0]


def save_experiment_log(suhu, hum, lux, soil, prompt_type,
                        recommendation, p_tok, c_tok, t_tok,
                        status_log, cache_hit=False, sensor_time=None):

    waktu_wib = sensor_time if sensor_time else datetime.now(
        WIB).strftime("%Y-%m-%d %H:%M")
    waktu_utc = datetime.now(pytz.utc).strftime(
        "%Y-%m-%d %H:%M")  

    file_exists = os.path.isfile(CSV_LOG_FILE)

    try:
        with open(CSV_LOG_FILE, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow([
                    "Timestamp_WIB", "Timestamp_UTC", "Suhu", "Hum", "Lux", "Soil",
                    "Prompt_Type", "Recommendation",
                    "P_Tokens", "C_Tokens", "Total_Tokens",
                    "Status_Log", "Cache_Hit"
                ])
            writer.writerow([
                waktu_wib, waktu_utc, suhu, hum, lux, soil,
                prompt_type, recommendation,
                p_tok, c_tok, t_tok,
                status_log, cache_hit
            ])
    except Exception as e:
        print(f"   ⚠️ Gagal catat ke CSV: {e}")

    # Simpan ke Google Sheets 
    save_to_gsheet([
        waktu_wib, waktu_utc, suhu, hum, lux, soil,
        prompt_type, recommendation,
        p_tok, c_tok, t_tok,
        status_log, cache_hit
    ])

    try:
        url = f"{BASE_URL}/api/log_llm"
        payload = {
            "suhu": suhu, "hum": hum, "lux": lux, "soil": soil,
            "prompt_type": prompt_type,
            "recommendation": recommendation,
            "p_tok": p_tok, "c_tok": c_tok, "t_tok": t_tok,
            "status_log": status_log,
            "cache_hit": cache_hit,
            "timestamp": waktu_wib,  
            "timestamp_utc": waktu_utc
        }
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code == 200:
            print(f"   ✅ Log tersimpan (API Call): {prompt_type}")
        else:
            print(f"   ⚠️ Gagal simpan ke DB. Status HTTP: {res.status_code}")
    except Exception as e:
        print(f"   ⚠️ Gagal koneksi ke API Flask: {e}")


def get_combined_data():
    print("   ↳ Sedang mengambil data sensor dari server...")
    try:
        response = requests.get(f"{BASE_URL}/api/data_dashboard", timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('sensor') and len(data['sensor']) > 0:
                return data['sensor'][-1]
    except Exception as e:
        print(f"   ❌ Error connection ke Flask: {e}")
    return None


def consult_ai(suhu, hum, lux, soil, prompt_type, prompt_template):
    prompt_system = prompt_template.format(
        suhu=suhu, hum=hum, lux=lux, soil=soil)
    user_message_content = "Berikan analisis dan rekomendasi berdasarkan data sensor di atas."

    print(f"   🚀 Panggil API: {prompt_type}...")

    payload = {
        "model": PRIMARY_MODEL,
        "messages": [
            {"role": "system", "content": prompt_system},
            {"role": "user",   "content": user_message_content}
        ],
        "temperature": 0.1
    }

    try:
        response = requests.post(
            OPENROUTER_URL, headers=HEADERS, json=payload, timeout=60)

        if response.status_code == 200:
            data = response.json()
            content = data['choices'][0]['message']['content']

            p_tok = data.get('usage', {}).get('prompt_tokens',     0)
            c_tok = data.get('usage', {}).get('completion_tokens', 0)
            t_tok = data.get('usage', {}).get('total_tokens',      0)

            recommendation_text = content
            try:
                parsed_result = json.loads(content.strip())
            except json.JSONDecodeError:
                match = re.search(r"\{.*\}", content, re.DOTALL)
                parsed_result = json.loads(match.group(0)) if match else None

            if parsed_result:
                try:
                    rek_list = parsed_result.get('rekomendasi', [])
                    rek_str = ", ".join(rek_list) if isinstance(
                        rek_list, list) else str(rek_list)
                    recommendation_text = (
                        f"[{parsed_result.get('status', 'Info')}] "
                        f"{parsed_result.get('analisis', '-')} "
                        f"| Saran: {rek_str}"
                    )
                except Exception:
                    pass

            return recommendation_text, p_tok, c_tok, t_tok, "Success"

        elif response.status_code == 401:
            return "-", 0, 0, 0, "API Error 401"
        elif response.status_code == 429:
            return "-", 0, 0, 0, "Rate Limit 429"
        else:
            return "-", 0, 0, 0, f"Error {response.status_code}"

    except requests.exceptions.Timeout:
        return "-", 0, 0, 0, "Connection Timeout"
    except Exception:
        return "-", 0, 0, 0, "Exception Error"


def job_ai_monitoring():
    print(
        f"\n[{datetime.now(WIB).strftime('%H:%M:%S')}] Memulai eksperimen 3 prompt...")
    data = get_combined_data()

    if not data:
        print("   ⚠️ Gagal mendapat data sensor.")
        gc.collect()
        return

    suhu = float(data.get('suhu',   0))
    hum = float(data.get('udara',  0))
    soil = float(data.get('tanah',  0))
    raw_ldr = float(data.get('cahaya', 0))
    lux = convert_ldr_to_lux(raw_ldr)

    raw_time = (data.get('timestamp') or data.get(
        'waktu') or data.get('created_at') or None)

    if raw_time:
        try:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
                try:
                    sensor_time = datetime.strptime(
                        str(raw_time), fmt).strftime("%Y-%m-%d %H:%M")
                    break
                except ValueError:
                    continue
            else:
                sensor_time = datetime.now(WIB).strftime("%Y-%m-%d %H:%M")
        except Exception:
            sensor_time = datetime.now(WIB).strftime("%Y-%m-%d %H:%M")
    else:
        sensor_time = datetime.now(WIB).strftime("%Y-%m-%d %H:%M")

    print(f"   🕐 Timestamp sensor: {sensor_time}")
    print(f"   💡 Cahaya Mentah (ADC): {raw_ldr} -> Estimasi: {lux} Lux")

    for prompt_name, prompt_content in PROMPTS.items():
        # Cek history hanya untuk mendapatkan status True/False
        past_record_exists = check_cache_csv(suhu, hum, lux, soil, prompt_name)
        is_cache_hit = True if past_record_exists is not None else False

        if is_cache_hit:
            print(
                f"   ♻️  Kondisi serupa historis terdeteksi (Cache Flag = True). Tetap memanggil API...")

        
        rekomendasi, p_tok, c_tok, t_tok, status_log = consult_ai(
            suhu, hum, lux, soil, prompt_name, prompt_content
        )

        save_experiment_log(
            suhu, hum, lux, soil,
            prompt_name, rekomendasi,
            p_tok=p_tok, c_tok=c_tok, t_tok=t_tok,
            status_log=status_log,
            cache_hit=is_cache_hit,
            sensor_time=sensor_time
        )

        time.sleep(5)

    gc.collect()
    print("Selesai. Memori dibersihkan.")


# =============================================================================
# JADWAL HARIAN
# =============================================================================
SCHEDULED_HOURS = ["06:00", "12:00", "16:00"]


def get_logged_hours_today():
    logged = set()
    today = datetime.now(WIB).strftime("%Y-%m-%d")

    if not os.path.isfile(CSV_LOG_FILE):
        return logged

    try:
        with open(CSV_LOG_FILE, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = row.get("Timestamp_WIB") or row.get("Timestamp") or ""
                if ts.startswith(today):
                    try:
                        jam = ts.split(" ")[1][:5]
                        logged.add(jam)
                    except IndexError:
                        continue
    except Exception as e:
        print(f"   ⚠️ Gagal baca CSV untuk recovery check: {e}")

    return logged


def log_stb_offline(missed_time_str):
    print(f"   📝 Mencatat STB Offline untuk: {missed_time_str}")
    waktu_utc = datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M")

    for prompt_name in PROMPTS.keys():
        save_experiment_log(
            suhu=0, hum=0, lux=0, soil=0,
            prompt_type=prompt_name,
            recommendation="-",
            p_tok=0, c_tok=0, t_tok=0,
            status_log="STB Offline",
            cache_hit=False,
            sensor_time=missed_time_str
        )
        try:
            requests.post(
                f"{BASE_URL}/api/log_llm",
                json={
                    "suhu": 0, "hum": 0, "lux": 0, "soil": 0,
                    "prompt_type": prompt_name,
                    "recommendation": "-",
                    "p_tok": 0, "c_tok": 0, "t_tok": 0,
                    "status_log": "STB Offline",
                    "cache_hit": False,
                    "timestamp": missed_time_str,
                    "timestamp_utc": waktu_utc
                },
                timeout=5
            )
        except Exception:
            pass


def recover_missed_jobs():
    now_wib = datetime.now(WIB)
    today_str = now_wib.strftime("%Y-%m-%d")
    now_hhmm = now_wib.strftime("%H:%M")

    passed_hours = [
        h for h in SCHEDULED_HOURS
        if h <= now_hhmm
    ]

    if not passed_hours:
        print("   ℹ️  Belum ada jadwal yang seharusnya berjalan hari ini.")
        return

    logged_hours = get_logged_hours_today()
    missed_hours = [h for h in passed_hours if h not in logged_hours]

    if not missed_hours:
        print("   ✅ Semua jadwal hari ini sudah tercatat. Tidak ada yang terlewat.")
        return

    print(
        f"\n   ⚠️  Terdeteksi {len(missed_hours)} jadwal terlewat: {missed_hours}")
    print("   ↳ Mencatat sebagai 'STB Offline' di CSV...")

    for jam in missed_hours:
        missed_time_str = f"{today_str} {jam}"
        log_stb_offline(missed_time_str)

    print(
        f"   ✅ Recovery selesai. {len(missed_hours)} baris STB Offline dicatat.")


def start_monitoring(force_run=False):
    print("=" * 55)
    print("🤖 AUTO MONITOR AI - PROMPT EXPERIMENT MODE")
    print(f"   Model     : {PRIMARY_MODEL}")
    print(f"   Via       : OpenRouter API")
    print(f"   Log       : {CSV_LOG_FILE}")
    print(f"   Jadwal    : {' | '.join(SCHEDULED_HOURS)} WIB")
    print(f"   Threshold : suhu ±{CACHE_THRESHOLD['suhu']}°C | "
          f"hum ±{CACHE_THRESHOLD['hum']}% | "
          f"lux ±{CACHE_THRESHOLD['lux']} | "
          f"soil ±{CACHE_THRESHOLD['soil']}%")
    print("=" * 55)

    print("\n☁️  Menghubungkan ke Google Sheets...")
    init_google_sheets()

    print("\n🔍 Mengecek jadwal yang terlewat...")
    recover_missed_jobs()

    for jam in SCHEDULED_HOURS:
        schedule.every().day.at(jam).do(job_ai_monitoring)

    print(f"\n⏰ Scheduler aktif. Jadwal: {SCHEDULED_HOURS} WIB")

    if force_run:
        print("\n▶️  Force run aktif — menjalankan job sekali sekarang...")
        job_ai_monitoring()

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            print("\nBerhenti.")
            break
        finally:
            gc.collect()


if __name__ == "__main__":
    start_monitoring(force_run=False)
