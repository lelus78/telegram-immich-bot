import os
import logging
import requests
import asyncio
import re
import shutil
import patoolib
import time
import subprocess
import myjdapi
import json
import hashlib
from datetime import datetime
from collections import defaultdict
from io import BytesIO
from PIL import Image, ExifTags
from exif import Image as ExifImage
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import RetryAfter, TelegramError
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler, CallbackQueryHandler

# --- CONFIGURAZIONE ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("PIL").setLevel(logging.WARNING)

TOKEN = os.getenv("TELEGRAM_TOKEN")
IMMICH_URL = os.getenv("IMMICH_URL").rstrip("/")
API_KEY = os.getenv("IMMICH_API_KEY")
ALBUM_ID = os.getenv("ALBUM_ID")
IMPORT_DIR = os.getenv("IMPORT_DIR", "/import")
HISTORY_FILE = os.path.join(IMPORT_DIR, "bot_history.json")
ALLOWED_USER_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
ADMIN_ID = ALLOWED_USER_IDS[0] if ALLOWED_USER_IDS else None

# JDownloader Config
MYJD_USER = os.getenv("MYJD_USER")
MYJD_PASSWORD = os.getenv("MYJD_PASSWORD")
MYJD_DEVICE = os.getenv("MYJD_DEVICE", "jdownloader")

TEMP_DIR = "/tmp/bot_downloads"
if os.path.exists(TEMP_DIR): shutil.rmtree(TEMP_DIR)
os.makedirs(TEMP_DIR, exist_ok=True)

ALBUM_CACHE = {}
USER_TAGS_MEM = {}

if not all([TOKEN, IMMICH_URL, API_KEY]):
    logging.error("Variabili d\'ambiente mancanti!"); exit(1)

# --- UTILS ---
def fix_perms(path):
    try:
        if os.path.isdir(path): os.chmod(path, 0o777)
        else: os.chmod(path, 0o666)
    except: pass

RETRY_FILE = os.path.join(IMPORT_DIR, "retry_counts.json")
LAST_IMPORT_FILE = os.path.join(IMPORT_DIR, "last_import.json")
CANCEL_REQUESTED = False

def load_retry_counts():
    try:
        with open(RETRY_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_retry_counts(data):
    try:
        with open(RETRY_FILE, 'w') as f: json.dump(data, f)
    except Exception as e: logging.error(f"Retry counts save error: {e}")

def save_last_import(stats, albums, source, failed_details):
    try:
        data = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "source": "Automatico (cartella)" if str(source).startswith("watch") else "Telegram",
            "success": stats.get("success", 0),
            "duplicate": stats.get("duplicate", 0),
            "error": stats.get("error", 0),
            "unsupported": stats.get("unsupported", 0),
            "albums": albums,
            "failed_details": failed_details[-20:],
        }
        with open(LAST_IMPORT_FILE, 'w') as f: json.dump(data, f)
    except Exception as e: logging.error(f"Last import save error: {e}")

def load_last_import():
    try:
        with open(LAST_IMPORT_FILE, 'r') as f: return json.load(f)
    except: return None

def load_history():
    if not os.path.exists(HISTORY_FILE): return {"albums": [], "photographers": [], "last_chat_id": ADMIN_ID}
    try:
        with open(HISTORY_FILE, 'r') as f: 
            data = json.load(f)
            if "last_chat_id" not in data: data["last_chat_id"] = ADMIN_ID
            return data
    except: return {"albums": [], "photographers": [], "last_chat_id": ADMIN_ID}

def save_history(data):
    try:
        with open(HISTORY_FILE, 'w') as f: json.dump(data, f)
    except Exception as e: logging.error(f"History save error: {e}")

def update_history(album, photographer, chat_id=None):
    data = load_history()
    changed = False
    if album:
        data["albums"] = [a for a in data["albums"] if a.lower() != album.lower()]
        data["albums"].insert(0, album)
        changed = True
    if photographer:
        data["photographers"] = [p for p in data["photographers"] if p.lower() != photographer.lower()]
        data["photographers"].insert(0, photographer)
        changed = True
    if chat_id:
        data["last_chat_id"] = chat_id
        changed = True
    if changed: save_history(data)

def get_or_create_album(album_name):
    if not album_name: return None
    album_name = album_name.strip().strip('"').strip("'").strip()
    album_name = re.sub(r"\.(zip|rar|7z|tar|gz|jpg|jpeg|png|heic|webp)$", "", album_name, flags=re.IGNORECASE)
    if not album_name or len(album_name) < 2: return None
    cache_key = album_name.lower()
    if cache_key in ALBUM_CACHE: return ALBUM_CACHE[cache_key]
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
    try:
        resp = requests.get(f"{IMMICH_URL}/api/albums", headers=headers)
        if resp.status_code == 200:
            for album in resp.json():
                if album["albumName"].lower() == cache_key:
                    ALBUM_CACHE[cache_key] = album["id"]; return album["id"]
        cr = requests.post(f"{IMMICH_URL}/api/albums", headers=headers, json={"albumName": album_name})
        if cr.status_code in [200, 201]:
            nid = cr.json()["id"]; ALBUM_CACHE[cache_key] = nid; return nid
    except: pass
    return None

def parse_tags(text):
    if not text: return None, None
    album, fotografo = None, None
    alb_m = re.search(r"#album\s+([^#\n\/]+)", text, re.IGNORECASE)
    if alb_m: album = re.sub(r"\.(zip|rar|7z|tar|gz|jpg|jpeg|png|heic|webp)$", "", alb_m.group(1).strip(), flags=re.IGNORECASE).strip()
    foto_m = re.search(r"#fotografo\s+([^#\n\/]+)", text, re.IGNORECASE)
    if foto_m: fotografo = re.sub(r"\.(zip|rar|7z|tar|gz|jpg|jpeg|png|heic|webp)$", "", foto_m.group(1).strip(), flags=re.IGNORECASE).strip()
    return album, fotografo

def get_effective_tags(user_id, current_text):
    ta, tf = parse_tags(current_text)
    if ta or tf:
        USER_TAGS_MEM[user_id] = {"tags": [ta, tf], "time": time.time()}
        return ta, tf
    if user_id in USER_TAGS_MEM:
        memo = USER_TAGS_MEM[user_id]
        if time.time() - memo["time"] < 1800: return memo["tags"]
    return None, None

def send_to_jdownloader(url, tags=(None, None)):
    if not MYJD_USER or not MYJD_PASSWORD:
        return False, "Credenziali JDownloader mancanti."
    dest_folder = "/output"
    tag_parts = []
    if tags and len(tags) > 0 and tags[0]: tag_parts.append(f"#album {tags[0]}")
    if tags and len(tags) > 1 and tags[1]: tag_parts.append(f"#fotografo {tags[1]}")
    if tag_parts:
        tag_str = " ".join(tag_parts)
        safe_tag_str = re.sub(r'[\\*?:\"<>|]', "", tag_str).strip()
        dest_folder = f"/output/{safe_tag_str}"
    try:
        jd = myjdapi.Myjdapi()
        jd.connect(MYJD_USER, MYJD_PASSWORD)
        jd.update_devices()
        device = jd.get_device(MYJD_DEVICE)
        if not device: return False, f"Device '{MYJD_DEVICE}' non trovato."
        device.linkgrabber.add_links([{"autostart": True, "links": url, "destinationFolder": dest_folder}])
        return True, "Link inviato a JDownloader!"
    except Exception as e: 
        logging.error(f"JD Error: {e}"); return False, f"Errore: {e}"

def is_file_stable(filepath, wait_time=5):
    if filepath.endswith(".part"): return False
    if os.path.exists(filepath + ".part"): return False
    try:
        size1 = os.path.getsize(filepath)
        time.sleep(wait_time)
        size2 = os.path.getsize(filepath)
        return size1 == size2
    except: return False

def calculate_checksum(file_path):
    sha1 = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while True:
            data = f.read(65536)
            if not data: break
            sha1.update(data)
    return sha1.hexdigest()

async def find_asset_by_checksum(checksum):
    headers = {"x-api-key": API_KEY, "Accept": "application/json"}
    try:
        resp = requests.post(f"{IMMICH_URL}/api/assets/check", headers=headers, json={"checksums": [checksum]})
        if resp.status_code == 200:
            data = resp.json()
            if data.get("results") and len(data["results"]) > 0:
                existing = data["results"][0]
                if existing.get("action") == "reject":
                    return existing.get("assetId")
    except: pass
    return None

# --- UPLOAD ---
async def upload_file_path_to_immich(file_path, original_name, telegram_date, message_id, override_date=None, extra_album=None, fotografo=None):
    try:
        if fotografo: logging.info(f"Uploading {original_name} with Photographer: {fotografo}")
        
        real_date = override_date or telegram_date
        if real_date.tzinfo is None:
            real_date = real_date.astimezone()
        headers = {"x-api-key": API_KEY, "Accept": "application/json"}
        
        with open(file_path, 'rb') as f:
            files = {"assetData": (original_name, f)}
            data = {
                "deviceAssetId": f"tg-{original_name}-{message_id}",
                "deviceId": "telegram-bot",
                "fileCreatedAt": real_date.isoformat(),
                "fileModifiedAt": real_date.isoformat(),
                "isFavorite": "false",
            }
            resp = requests.post(f"{IMMICH_URL}/api/assets", headers=headers, files=files, data=data)
        
        asset_id = None
        status = "error"
        reason = None
        
        if resp.status_code in [200, 201]:
            res_json = resp.json()
            asset_id = res_json.get("id")
            if res_json.get("status") == "duplicate":
                status = "duplicate"
                if not asset_id:
                    chk = calculate_checksum(file_path)
                    asset_id = await find_asset_by_checksum(chk)
            else:
                status = "success"
        elif resp.status_code == 409: 
             status = "duplicate"
             chk = calculate_checksum(file_path)
             asset_id = await find_asset_by_checksum(chk)
        else:
            reason = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logging.error(f"Immich upload rejected {original_name}: HTTP {resp.status_code} - {resp.text[:300]}")
        
        if not asset_id: return status, None, reason

        # 1. Aggiornamento Descrizione (Fotografo)
        if fotografo:
            try:
                requests.put(f"{IMMICH_URL}/api/assets/{asset_id}", headers=headers, json={"description": f"Fotografo: {fotografo}"})
            except Exception as e: logging.error(f"Desc upd error: {e}")

        # 2. Gestione Album Multipli (Album Principale + Album Specifiico + Album Fotografo)
        target_ids = []
        
        # A. Album Generale (Importate Bot)
        if ALBUM_ID: target_ids.append(ALBUM_ID)
        
        # B. Album Specifico (#album)
        if extra_album:
            aid = get_or_create_album(extra_album)
            if aid: target_ids.append(aid)
            
        # C. Album Fotografo (#fotografo)
        if fotografo:
            fid = get_or_create_album(fotografo)
            if fid: target_ids.append(fid)
        
        for alb_id in list(set(target_ids)):
            try:
                requests.put(f"{IMMICH_URL}/api/albums/{alb_id}/assets", headers=headers, json={"ids": [asset_id]})
            except Exception as e: logging.error(f"Alb upd error: {e}")

        return status, asset_id, None
    except Exception as e: 
        logging.error(f"Upload error: {e}"); return "error", None, str(e)[:200]

# --- PROCESSORS ---
async def process_directory_content(directory, msg_date, msg_id, manual_tags=None, bot=None, chat_id=None):
    total_stats = {"success": 0, "duplicate": 0, "error": 0, "unsupported": 0}
    current_job_albums = set()
    failed_details = []
    
    has_archives = True
    while has_archives:
        has_archives = False
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in ["corrupted", "failed_extraction", "unsupported_files", "failed_upload"]]
            for file in files:
                if file.lower().endswith((".zip", ".rar", ".7z", ".tar", ".gz")):
                    full_p = os.path.join(root, file)
                    if not is_file_stable(full_p, wait_time=2): continue
                    ext_to = os.path.join(root, "extracted_" + file)
                    os.makedirs(ext_to, exist_ok=True); fix_perms(ext_to)
                    try: patoolib.extract_archive(full_p, outdir=ext_to); os.remove(full_p); has_archives = True; await asyncio.sleep(5)
                    except: total_stats["error"] += 1
    
    all_items = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in ["corrupted", "failed_extraction", "unsupported_files", "failed_upload"]]
        for file in files: all_items.append((root, file))
    
    total_count = len(all_items); processed_count = 0; progress_msg = None
    if total_count > 5 and bot and chat_id:
        try: progress_msg = await bot.send_message(chat_id=chat_id, text=f"⏳ <b>Avanzamento:</b> 0/{total_count}...", parse_mode="HTML")
        except: pass

    global CANCEL_REQUESTED
    for root, file in all_items:
        if CANCEL_REQUESTED:
            CANCEL_REQUESTED = False
            if bot and chat_id:
                try: await bot.send_message(chat_id=chat_id, text="🛑 Importazione interrotta su richiesta.")
                except: pass
            break
        processed_count += 1; full_p = os.path.join(root, file)
        if not is_file_stable(full_p, wait_time=5): continue
        if file.lower().endswith(('.jpg', '.jpeg', '.png', '.heic', '.mp4', '.mov', '.avi', '.webp',
                                   '.raf', '.cr2', '.cr3', '.nef', '.arw', '.dng', '.rw2', '.orf', '.raw')):
            final_alb = manual_tags[0] if manual_tags and manual_tags[0] else None
            final_foto = manual_tags[1] if manual_tags and len(manual_tags) > 1 and manual_tags[1] else None
            
            rel_p = os.path.relpath(full_p, directory)
            p_alb, p_foto = parse_tags(rel_p)
            
            if p_foto: logging.info(f"Found photographer in path: {p_foto}")

            if p_alb and not final_alb: final_alb = p_alb
            if p_foto and not final_foto: final_foto = p_foto
            
            if final_alb: current_job_albums.add(final_alb)
            
            fb_date = msg_date
            if str(msg_id).startswith("watch"):
                try: fb_date = datetime.fromtimestamp(os.path.getmtime(full_p)).astimezone()
                except: pass
            
            status, _, reason = await upload_file_path_to_immich(full_p, file, fb_date, f"{msg_id}-{file}", extra_album=final_alb, fotografo=final_foto)
            if status in ["success", "duplicate"]:
                try: os.remove(full_p)
                except: pass
                if status == "success": total_stats["success"] += 1
                else: total_stats["duplicate"] += 1
            else:
                failed_details.append({"name": file, "reason": reason or "Motivo sconosciuto"})
                retry_counts = load_retry_counts()
                retry_counts[full_p] = retry_counts.get(full_p, 0) + 1
                if retry_counts[full_p] >= 3:
                    failed_dir = os.path.join(IMPORT_DIR, "failed_upload")
                    os.makedirs(failed_dir, exist_ok=True); fix_perms(failed_dir)
                    try:
                        shutil.move(full_p, os.path.join(failed_dir, file))
                        retry_counts.pop(full_p, None)
                    except: pass
                    if bot and chat_id:
                        try: await bot.send_message(chat_id=chat_id, text=f"❌ <b>Import fallito:</b> {file}\n{reason or 'Motivo sconosciuto'}\n(spostato in failed_upload dopo 3 tentativi, vedi /errori)", parse_mode="HTML")
                        except: pass
                save_retry_counts(retry_counts)
                total_stats["error"] += 1
            
            if progress_msg and processed_count % 5 == 0:
                perc = int((processed_count / total_count) * 100)
                try: await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=f"⏳ <b>Avanzamento:</b> {processed_count}/{total_count} ({perc}%)", parse_mode="HTML")
                except: pass
            await asyncio.sleep(1)
        else:
            if root == IMPORT_DIR and (file == "put_files_here.txt" or file == "bot_history.json"): continue
            unsupported_dir = os.path.join(IMPORT_DIR, "unsupported_files")
            os.makedirs(unsupported_dir, exist_ok=True)
            try: shutil.move(full_p, os.path.join(unsupported_dir, file)); total_stats["unsupported"] += 1
            except: pass

    save_last_import(total_stats, list(current_job_albums), msg_id, failed_details)

    if progress_msg:
        try: await bot.delete_message(chat_id=chat_id, message_id=progress_msg.message_id)
        except: pass
    
    for root, dirs, files in os.walk(directory, topdown=False):
        for name in dirs:
            d_path = os.path.join(root, name)
            if name in ["corrupted", "failed_extraction", "unsupported_files", "failed_upload", "jdownloader", "tg-immich-bot"] or d_path == directory: continue
            try: os.rmdir(d_path)
            except: pass
    return total_stats, list(current_job_albums)

# --- WATCH FOLDER TASK ---
async def watch_folder_task(app):
    special_dirs = ["corrupted", "failed_extraction", "unsupported_files", "failed_upload", "tg-immich-bot"]
    while True:
        await asyncio.sleep(30)
        if not os.path.exists(IMPORT_DIR): continue
        items = os.listdir(IMPORT_DIR)
        relevant = [i for i in items if i not in special_dirs and i != "put_files_here.txt" and i != "bot_history.json"]
        if not relevant: continue
        has_valid_content = False
        for root, dirs, files in os.walk(IMPORT_DIR):
            dirs[:] = [d for d in dirs if d not in special_dirs]
            for file in files:
                if file.lower().endswith((".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov", ".avi", ".zip", ".rar", ".7z", ".tar", ".webp",
                                          ".raf", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".rw2", ".orf", ".raw")):
                    if is_file_stable(os.path.join(root, file), wait_time=2):
                        has_valid_content = True; break
            if has_valid_content: break
        if not has_valid_content: continue
        hist = load_history(); target_chat = hist.get("last_chat_id", ADMIN_ID)
        if target_chat:
            try: await app.bot.send_message(chat_id=target_chat, text=f"📂 <b>Importazione Automatica Iniziata!</b>", parse_mode="HTML")
            except: pass
        stats, albums = await process_directory_content(IMPORT_DIR, datetime.now(), "watchdog", bot=app.bot, chat_id=target_chat)
        if target_chat and (sum(stats.values()) > 0):
            report = f"📂 <b>Importazione Completata!</b>\n✅ {stats['success']} Caricati\n♻️ {stats['duplicate']} Duplicati"
            if albums: report += f"\n📂 <b>Album:</b> {', '.join(albums)}"
            if stats['unsupported'] > 0: report += f"\n⚠️ {stats['unsupported']} Non supportati"
            if stats['error'] > 0: report += f"\n❌ {stats['error']} Errori"
            try: await app.bot.send_message(chat_id=target_chat, text=report, parse_mode="HTML")
            except: pass

# --- HANDLERS ---
async def get_tags_markup(user_id):
    data = load_history()
    curr = USER_TAGS_MEM.get(user_id, {}).get("tags", [None, None])
    keyboard = []
    if data.get("albums"):
        keyboard.append([InlineKeyboardButton("📂 ALBUM RECENTI:", callback_data="none")])
        row = []
        for i, alb in enumerate(data["albums"][:6]):
            label = f"✅ {alb}" if curr[0] == alb else alb
            row.append(InlineKeyboardButton(label, callback_data=f"alb_{i}"))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)
    if data.get("photographers"):
        keyboard.append([InlineKeyboardButton("👤 FOTOGRAFI:", callback_data="none")])
        row = []
        for i, foto in enumerate(data["photographers"][:4]):
            label = f"✅ {foto}" if curr[1] == foto else foto
            row.append(InlineKeyboardButton(label, callback_data=f"foto_{i}"))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🗑 Cancella Tag", callback_data="reset_tags")])
    return InlineKeyboardMarkup(keyboard)

async def send_tags_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    update_history(None, None, update.effective_chat.id)
    curr = USER_TAGS_MEM.get(update.effective_user.id, {}).get("tags", [None, None])
    msg_text = "🗂 <b>Gestione Tag</b>\nSeleziona album o fotografo dai tasti sotto."
    if curr[0] or curr[1]:
        msg_text += f"\n\n🔹 <b>Attivi:</b>\n📂 Album: {curr[0] or '--'}\n👤 Foto: {curr[1] or '--'}"
    await update.message.reply_text(msg_text, reply_markup=await get_tags_markup(update.effective_user.id), parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USER_IDS: return
    data = query.data; hist = load_history()
    curr = USER_TAGS_MEM.get(user_id, {"tags": [None, None], "time": time.time()})
    if data == "reset_tags": curr["tags"] = [None, None]
    elif data.startswith("alb_"): 
        idx = int(data.split("_")[1])
        if idx < len(hist["albums"]):
            val = hist["albums"][idx]; curr["tags"][0] = None if curr["tags"][0] == val else val
    elif data.startswith("foto_"):
        idx = int(data.split("_")[1])
        if idx < len(hist["photographers"]):
            val = hist["photographers"][idx]; curr["tags"][1] = None if curr["tags"][1] == val else val
    elif data.startswith("salb_"):
        idx = int(data.split("_")[1]); opts = curr.get("search_albums", [])
        if idx < len(opts):
            val = opts[idx]; curr["tags"][0] = None if curr["tags"][0] == val else val
    elif data.startswith("sfoto_"):
        idx = int(data.split("_")[1]); opts = curr.get("search_photographers", [])
        if idx < len(opts):
            val = opts[idx]; curr["tags"][1] = None if curr["tags"][1] == val else val
    curr["time"] = time.time(); USER_TAGS_MEM[user_id] = curr
    msg_text = "🗂 <b>Gestione Tag</b>\nSeleziona album o fotografo dai tasti sotto."
    if curr["tags"][0] or curr["tags"][1]:
        msg_text += f"\n\n🔹 <b>Attivi:</b>\n📂 Album: {curr['tags'][0] or '--'}\n👤 Foto: {curr['tags'][1] or '--'}"
    try: await query.edit_message_text(msg_text, reply_markup=await get_tags_markup(user_id), parse_mode="HTML")
    except: pass

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    text = update.message.text
    if len(text) > 200: return
    ta, tf = parse_tags(text)
    if ta or tf:
        curr_mem = USER_TAGS_MEM.get(update.effective_user.id, {"tags": [None, None], "time": time.time()})
        if ta: curr_mem["tags"][0] = ta
        if tf: curr_mem["tags"][1] = tf
        curr_mem["time"] = time.time(); USER_TAGS_MEM[update.effective_user.id] = curr_mem
        update_history(ta, tf, update.effective_chat.id)
        await update.message.reply_text(f"✅ Tag aggiornati!\n📂 {curr_mem['tags'][0]}\n👤 {curr_mem['tags'][1]}")

async def handle_any_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    update_history(None, None, update.effective_chat.id)
    msg = update.message; tags = get_effective_tags(update.effective_user.id, msg.caption or "")
    used_albums = [t for t in tags if t] if tags else []
    file_obj = None; fname = "file.jpg"
    if msg.document:
        if "image" in (msg.document.mime_type or "") or "video" in (msg.document.mime_type or ""):
            file_obj = await msg.document.get_file(); fname = msg.document.file_name or "file"
    elif msg.photo:
        file_obj = await msg.photo[-1].get_file(); fname = f"photo_{file_obj.file_unique_id}.jpg"
    elif msg.video:
        file_obj = await msg.video.get_file(); fname = msg.video.file_name or f"video_{file_obj.file_unique_id}.mp4"
    if file_obj:
        if fname.lower().endswith((".zip", ".rar", ".7z", ".tar")):
            job_dir = os.path.join(TEMP_DIR, f"zip_{msg.message_id}"); os.makedirs(job_dir, exist_ok=True)
            sm = await msg.reply_text("⏳ <b>Archivio ricevuto...</b>", parse_mode="HTML")
            await file_obj.download_to_drive(os.path.join(job_dir, fname))
            stats, albums = await process_directory_content(job_dir, msg.date, msg.message_id, manual_tags=tags, bot=context.bot, chat_id=msg.chat_id)
            shutil.rmtree(job_dir)
            rep = f"📦 <b>Elaborato!</b>\n✅ {stats['success']} Foto\n♻️ {stats['duplicate']} Duplicati"
            if albums: rep += f"\n📂 <b>Album:</b> {', '.join(albums)}"
            await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=sm.message_id, text=rep, parse_mode="HTML")
        else:
            tmp = os.path.join(TEMP_DIR, fname); await file_obj.download_to_drive(tmp)
            status, _, reason = await upload_file_path_to_immich(tmp, fname, msg.date, msg.message_id, extra_album=tags[0] if tags and tags[0] else None, fotografo=tags[1] if tags and len(tags)>1 else None)
            os.remove(tmp)
            if status == "success": report = "✅ Caricata!"
            elif status == "duplicate": report = "♻️ Duplicata (Tag Aggiornati)."
            else: report = f"❌ <b>Caricamento fallito:</b>\n{reason or 'Motivo sconosciuto'}"
            if tags and tags[0]: report += f"\n📂 Album: {tags[0]}"
            if tags and len(tags)>1 and tags[1]: report += f"\n👤 Foto: {tags[1]}"
            await msg.reply_text(report, parse_mode="HTML")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    update_history(None, None, update.effective_chat.id)
    url_m = re.search(r'(https?://\S+)', update.message.text)
    if not url_m: return
    url = url_m.group(1); tags = get_effective_tags(update.effective_user.id, update.message.text)
    JD_DOMAINS = ["mobidrive.com", "fromsmash.com", "swisstransfer.com", "mega.nz", "drive.google.com", "dropbox.com", "1fichier.com", "filecrypt.cc", "mediafire.com", "instagram.com", "facebook.com", "fb.watch"]
    if any(d in url for d in JD_DOMAINS) or "/jd" in update.message.text:
        ok, msg = send_to_jdownloader(url, tags=tags)
        if ok: await update.message.reply_text(f"🦅 <b>JDownloader:</b> Inviato!", parse_mode="HTML")
        else: await update.message.reply_text(f"🦅 <b>JDownloader Errore:</b> {msg}", parse_mode="HTML")
        return
    job_dir = os.path.join(TEMP_DIR, f"job_{update.message.message_id}"); os.makedirs(job_dir, exist_ok=True)
    msg = await update.message.reply_text("⏳ <b>Link ricevuto...</b>", parse_mode="HTML")
    success = False
    try:
        if "wetransfer.com" in url or "we.tl" in url:
            cmd = ["python3", "/opt/transferwee/transferwee.py", "download", url]
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=job_dir); await proc.wait(); success = (proc.returncode == 0)
        else:
            cmd = ["yt-dlp", "-o", f"{job_dir}/%(title)s.%(ext)s", url] if any(x in url for x in ["youtube", "youtu.be", "instagram", "tiktok"]) else ["wget", "-P", job_dir, url]
            proc = await asyncio.create_subprocess_exec(*cmd); await proc.wait(); success = (proc.returncode == 0)
    except: success = False
    downloaded_files = [f for f in os.listdir(job_dir) if os.path.isfile(os.path.join(job_dir, f))]
    if not success or not downloaded_files:
        if MYJD_USER:
            await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text="⚠️ Download diretto fallito. Provo con JD...")
            ok, jd_msg = send_to_jdownloader(url, tags=tags)
            if ok: await context.bot.send_message(chat_id=msg.chat_id, text=f"🦅 <b>JDownloader:</b> Inviato!", parse_mode="HTML")
            else: await context.bot.send_message(chat_id=msg.chat_id, text=f"❌ Fallito: {jd_msg}", parse_mode="HTML")
            shutil.rmtree(job_dir); return
        else:
            await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text="❌ Fallito."); shutil.rmtree(job_dir); return
    stats, albums = await process_directory_content(job_dir, update.message.date, update.message.message_id, manual_tags=tags, bot=context.bot, chat_id=update.message.chat_id)
    shutil.rmtree(job_dir)
    rep = f"✅ <b>Completato!</b>\n{stats['success']} Caricati\n{stats['duplicate']} Duplicati (Aggiornati)"
    if albums: rep += f"\n📂 Album: {', '.join(albums)}"
    await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text=rep, parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    msg = (
        "🤖 <b>Comandi disponibili</b>\n\n"
        "📤 Manda foto/video/archivi/link direttamente in chat per caricarli.\n\n"
        "🏷 /tags — Gestisci album e fotografo predefiniti\n"
        "🔍 /cerca &lt;testo&gt; — Cerca un album/fotografo già usato in passato\n"
        "📊 /ultimo — Riepilogo dell'ultima importazione\n"
        "📂 /coda — File in attesa di importazione\n"
        "⚠️ /errori — File falliti permanentemente (dopo 3 tentativi)\n"
        "🛑 /stop — Interrompe l'importazione in corso\n"
        "❓ /help — Questo messaggio"
    )
    await update.effective_message.reply_text(msg, parse_mode="HTML")

async def ultimo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    data = load_last_import()
    if not data:
        await update.effective_message.reply_text("ℹ️ Nessuna importazione registrata finora.")
        return
    msg = (f"📊 <b>Ultima importazione</b>\n"
           f"🕐 {data.get('timestamp','?')}\n"
           f"📥 Origine: {data.get('source','?')}\n\n"
           f"✅ {data.get('success',0)} Caricati\n"
           f"♻️ {data.get('duplicate',0)} Duplicati\n"
           f"❌ {data.get('error',0)} Errori\n"
           f"⚠️ {data.get('unsupported',0)} Non supportati")
    if data.get("albums"): msg += f"\n📂 Album: {', '.join(data['albums'])}"
    if data.get("failed_details"):
        msg += "\n\n<b>Ultimi errori:</b>"
        for fd in data["failed_details"][-5:]:
            msg += f"\n• {fd['name']}: {fd['reason'][:100]}"
    await update.effective_message.reply_text(msg, parse_mode="HTML")

async def coda_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    special_dirs = ["corrupted", "failed_extraction", "unsupported_files", "failed_upload", "tg-immich-bot"]
    skip_files = ("put_files_here.txt", "bot_history.json", "retry_counts.json", "last_import.json")
    count = 0
    for root, dirs, files in os.walk(IMPORT_DIR):
        dirs[:] = [d for d in dirs if d not in special_dirs]
        count += len([f for f in files if f not in skip_files])
    await update.effective_message.reply_text(f"📂 File in attesa di importazione: {count}")

async def errori_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    failed_dir = os.path.join(IMPORT_DIR, "failed_upload")
    files = os.listdir(failed_dir) if os.path.isdir(failed_dir) else []
    if not files:
        await update.effective_message.reply_text("✅ Nessun file in errore permanente.")
        return
    msg = f"⚠️ <b>{len(files)} file in errore permanente</b> (falliti 3 volte):\n" + "\n".join(f"• {f}" for f in files[:20])
    if len(files) > 20: msg += f"\n... e altri {len(files)-20}"
    await update.effective_message.reply_text(msg, parse_mode="HTML")

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    global CANCEL_REQUESTED
    CANCEL_REQUESTED = True
    await update.effective_message.reply_text("🛑 Richiesta di stop inviata. L'elaborazione in corso si fermerà entro pochi secondi.")

def _fuzzy_match(query, names, cutoff=0.7, limit=8):
    import difflib
    q = query.lower()
    scored = []
    for n in names:
        words = n.lower().split()
        best = max((difflib.SequenceMatcher(None, q, w).ratio() for w in words), default=0.0)
        if best >= cutoff:
            scored.append((best, n))
    scored.sort(key=lambda x: -x[0])
    return [n for _, n in scored[:limit]]

async def cerca_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.effective_message.reply_text("Uso: /cerca <parte del nome>\nEsempio: /cerca vecchi")
        return
    hist = load_history(); q = query.lower()
    album_matches = [a for a in hist.get("albums", []) if q in a.lower()]
    foto_matches = [f for f in hist.get("photographers", []) if q in f.lower()]
    approx = False
    if not album_matches and not foto_matches:
        approx = True
        album_matches = _fuzzy_match(query, hist.get("albums", []))
        foto_matches = _fuzzy_match(query, hist.get("photographers", []))
    if not album_matches and not foto_matches:
        await update.effective_message.reply_text(f"Nessun risultato per '{query}'.")
        return
    user_id = update.effective_user.id
    curr = USER_TAGS_MEM.get(user_id, {"tags": [None, None], "time": time.time()})
    curr["search_albums"] = album_matches[:10]; curr["search_photographers"] = foto_matches[:10]
    curr["time"] = time.time(); USER_TAGS_MEM[user_id] = curr
    keyboard = []
    if album_matches:
        keyboard.append([InlineKeyboardButton("📂 ALBUM TROVATI:", callback_data="none")])
        row = []
        for i, alb in enumerate(album_matches[:10]):
            row.append(InlineKeyboardButton(alb, callback_data=f"salb_{i}"))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)
    if foto_matches:
        keyboard.append([InlineKeyboardButton("👤 FOTOGRAFI TROVATI:", callback_data="none")])
        row = []
        for i, foto in enumerate(foto_matches[:10]):
            row.append(InlineKeyboardButton(foto, callback_data=f"sfoto_{i}"))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)
    header = f"🔍 Nessuna corrispondenza esatta per '{query}', forse cercavi (tocca per selezionare):" if approx else f"🔍 Risultati per '{query}' (tocca per selezionare):"
    await update.effective_message.reply_text(header, reply_markup=InlineKeyboardMarkup(keyboard))

COMMAND_MAP = {
    "tags": send_tags_menu, "start": send_tags_menu, "help": help_cmd,
    "ultimo": ultimo_cmd, "coda": coda_cmd, "errori": errori_cmd,
    "stop": stop_cmd, "cerca": cerca_cmd,
}

async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_message
    if not msg or not msg.text: return
    if update.effective_user.id not in ALLOWED_USER_IDS: return
    if msg.text.startswith("/"):
        parts = msg.text.split()
        cmd = parts[0][1:].split("@")[0].lower()
        context.args = parts[1:]
        handler = COMMAND_MAP.get(cmd)
        if handler: await handler(update, context)
    else:
        await handle_text(update, context)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("tags", send_tags_menu))
    app.add_handler(CommandHandler("start", send_tags_menu))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ultimo", ultimo_cmd))
    app.add_handler(CommandHandler("coda", coda_cmd))
    app.add_handler(CommandHandler("errori", errori_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("cerca", cerca_cmd))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, handle_edited_message))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Entity("url") | filters.Entity("text_link"), handle_url))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL, handle_any_media))
    loop = asyncio.get_event_loop(); loop.create_task(watch_folder_task(app))
    print("Bot avviato (v43 - Case-insensitive history dedup)...")
    app.run_polling()
