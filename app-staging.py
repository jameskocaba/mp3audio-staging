import gevent.monkey
gevent.monkey.patch_all()

import os, uuid, logging, glob, zipfile, certifi, gc, shutil, time, subprocess, math, tempfile, hmac, hashlib
from flask import Flask, request, send_file, jsonify, session, redirect, url_for
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from yt_dlp import YoutubeDL
import json
import requests

from gevent.pool import Pool
from gevent.lock import BoundedSemaphore
from threading import Thread
from collections import deque

import resend
from openai import OpenAI

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
from xhtml2pdf import pisa

os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-prod')
app.config['SESSION_COOKIE_SAMESITE'] = 'None'
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///mp3audio.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app, supports_credentials=True, resources={
    r"/*": { "origins": "*", "methods": ["GET", "POST", "OPTIONS"], "allow_headers": ["Content-Type"] }
})

db = SQLAlchemy(app)
serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    free_conversions_used = db.Column(db.Integer, default=0)
    paid_track_credits = db.Column(db.Integer, default=0)

with app.app_context():
    db.create_all()

DOWNLOAD_FOLDER = os.path.join(os.getcwd(), 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
try: client = OpenAI()
except: client = None

MAX_SONGS = 50
AVG_TIME_PER_TRACK = 45  
PUBLIC_URL = os.environ.get('PUBLIC_URL', 'https://mp3aud.io')
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'https://mp3aud.io')

conversion_jobs = {} 
zip_locks = {}
conversion_queue = deque() 
current_processing_session = None 

def cleanup_memory(): gc.collect()

def cleanup_old_sessions():
    try:
        current_time = time.time()
        for session_id in list(conversion_jobs.keys()):
            job = conversion_jobs[session_id]
            if job['status'] not in ['processing', 'queued'] and current_time - job.get('last_update', 0) > 3600:
                session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
                if os.path.exists(session_dir): shutil.rmtree(session_dir, ignore_errors=True)
                del conversion_jobs[session_id]
                if session_id in zip_locks: del zip_locks[session_id]
    except: pass

def send_email_notification(recipient, subject, html_content):
    try:
        resend.api_key = os.environ.get('RESEND_API_KEY')
        resend.Emails.send({
            "from": f"MP3 Audio Tools <{os.environ.get('FROM_EMAIL')}>",
            "to": [recipient],
            "subject": subject,
            "html": html_content,
        })
    except: pass

def get_or_create_user():
    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        if user: return user
        
    fake_email = f"anon_{uuid.uuid4().hex[:12]}@guest.local"
    ghost_user = User(email=fake_email)
    db.session.add(ghost_user)
    db.session.commit()
    session['user_id'] = ghost_user.id
    return ghost_user

def refund_unused_credits(user_id, payment_method, unused_tracks):
    if unused_tracks > 0 and user_id and payment_method:
        try:
            with app.app_context():
                user = User.query.get(user_id)
                if user:
                    if payment_method == 'credits':
                        user.paid_track_credits += unused_tracks
                    elif payment_method == 'free':
                        user.free_conversions_used = max(0, user.free_conversions_used - unused_tracks)
                    db.session.commit()
        except Exception as e:
            logger.error(f"Failed to refund credits: {e}")

@app.route('/auth/login', methods=['POST'])
def send_magic_link():
    email = request.json.get('email', '').strip().lower()
    if not email: return jsonify({"error": "Email is required"}), 400
        
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(email=email)
        db.session.add(user)
        db.session.commit()
        
    token = serializer.dumps(email, salt='magic-link')
    magic_url = f"{FRONTEND_URL}?token={token}"
    html = f"""<div style="padding: 20px;"><h2>Login to mp3aud.io</h2><a href="{magic_url}" style="background-color: #007BFF; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; display: inline-block;">Log In Now</a></div>"""
    send_email_notification(email, "Your Login Link", html)
    return jsonify({"success": True, "message": "Magic link sent to your email."})

@app.route('/auth/verify', methods=['POST'])
def verify_magic_link():
    token = request.json.get('token')
    if not token: return jsonify({"error": "No token provided"}), 400
    try: 
        email = serializer.loads(token, salt='magic-link', max_age=3600)
    except: 
        return jsonify({"error": "Invalid or expired link"}), 400
    user = User.query.filter_by(email=email).first()
    if user:
        session['user_id'] = user.id
        return jsonify({"success": True})
    return jsonify({"error": "User not found"}), 404

@app.route('/auth/me', methods=['GET'])
def get_current_user():
    user = get_or_create_user()
    is_guest = user.email.startswith('anon_')
    return jsonify({
        "authenticated": not is_guest,
        "email": None if is_guest else user.email,
        "free_conversions_used": user.free_conversions_used,
        "paid_track_credits": user.paid_track_credits
    })

@app.route('/auth/logout', methods=['POST'])
def logout():
    session.pop('user_id', None)
    return jsonify({"success": True})

@app.route('/buy-credits', methods=['POST'])
def generate_invoice():
    user = get_or_create_user()
    if user.email.startswith('anon_'):
        return jsonify({"error": "Unauthorized. Please log in first."}), 401
    payload = {
        "price_amount": 5.00,
        "price_currency": "usd",
        "order_id": str(user.id), 
        "order_description": "250 Track Conversions",
        "ipn_callback_url": f"{PUBLIC_URL.rstrip('/')}/webhook/nowpayments"
    }
    try:
        headers = {'x-api-key': os.environ.get('NOWPAYMENTS_API_KEY'), 'Content-Type': 'application/json'}
        response = requests.post('https://api.nowpayments.io/v1/invoice', headers=headers, json=payload)
        if response.status_code == 200: return jsonify({"invoice_url": response.json().get('invoice_url')})
        return jsonify({"error": "Failed to connect to payment gateway."}), 500
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/webhook/nowpayments', methods=['POST'])
def nowpayments_webhook():
    secret_key = os.environ.get('NOWPAYMENTS_IPN_SECRET', '').encode('utf-8')
    if request.headers.get('x-nowpayments-sig') != hmac.new(secret_key, request.get_data(), hashlib.sha512).hexdigest():
        return jsonify({"error": "Invalid Signature"}), 403
    data = request.json
    if data and data.get('payment_status') == 'finished':
        user = User.query.get(int(data.get('order_id')))
        if user:
            user.paid_track_credits += 250
            db.session.commit()
    return jsonify({"status": "OK"}), 200

def notify_user_complete(session_id, user_email, track_count, html_summaries=""):
    if not user_email: return
    download_link = f"{PUBLIC_URL.rstrip('/')}/download/{session_id}/playlist_backup.zip"
    manuals_section = f"<div style='margin-top: 30px; padding: 20px; background-color: #f8fafc; border-radius: 8px; border: 1px solid #e2e8f0;'>{html_summaries}</div>" if html_summaries else ""
    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px;">
        <h2 style="color: #2980b9;">Your Files Are Ready</h2>
        <p>Your conversion of <strong>{track_count} media file(s)</strong> has finished processing.</p>
        {manuals_section}
        <div style="margin: 30px 0; text-align: center;">
            <a href="{download_link}" style="background-color: #ea580c; color: #ffffff; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold;">Download ZIP Archive</a>
        </div>
    </div>
    """
    send_email_notification(user_email, "Your Conversion is Ready 📦", html)

def transcribe_audio_file(mp3_file_path, job=None):
    if not client: return None, None
    try:
        temp_dir = tempfile.mkdtemp()
        chunk_pattern = os.path.join(temp_dir, "chunk_%03d.mp3")
        ffmpeg_exe = 'ffmpeg_bin/ffmpeg' if os.path.exists('ffmpeg_bin/ffmpeg') else 'ffmpeg'
        if job: job['current_status'] = 'Slicing audio for AI analysis...'
        subprocess.run([ffmpeg_exe, '-y', '-i', mp3_file_path, '-f', 'segment', '-segment_time', '900', '-c', 'copy', chunk_pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        chunks = sorted(glob.glob(os.path.join(temp_dir, "chunk_*.mp3")))
        total_chunks = len(chunks)
        full_transcript = ""
        
        for i, chunk_path in enumerate(chunks):
            if job:
                job['current_status'] = f'Transcribing audio (Part {i+1} of {total_chunks})...'
                job['sub_progress'] = int((i / total_chunks) * 100)
            try:
                with open(chunk_path, "rb") as audio_file:
                    transcript = client.audio.transcriptions.create(model="whisper-1", file=audio_file)
                full_transcript += transcript.text + " "
            except: full_transcript += f"\n[Warning: AI transcription failed for this segment.]\n"
        
        if job: job['sub_progress'] = 100
        shutil.rmtree(temp_dir, ignore_errors=True)
                
        text_file_path = mp3_file_path.replace('.mp3', '.txt')
        with open(text_file_path, "w", encoding="utf-8") as f: f.write(full_transcript.strip()) 
            
        pdf_file_path = mp3_file_path.replace('.mp3', '.pdf')
        try:
            doc = SimpleDocTemplate(pdf_file_path, pagesize=letter)
            story = [Paragraph(full_transcript.strip().replace('\n', '<br/>'), getSampleStyleSheet()["Normal"])]
            doc.build(story)
        except: pdf_file_path = None
        return text_file_path, pdf_file_path
    except: return None, None

def generate_diy_manual(transcript_text_path, job=None):
    if not client: return None, None, None
    try:
        if job: job['current_status'] = 'Formatting AI summary...'; job['sub_progress'] = 0
        with open(transcript_text_path, "r", encoding="utf-8") as file: transcript = file.read()[:100000] 
        system_prompt = "You are an expert technical writer. Format the provided text into a highly detailed, comprehensive document in HTML format."
        response = client.chat.completions.create(
            model="gpt-4o", 
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Here is the raw transcript:\n\n{transcript}"}],
            temperature=0.3 
        )
        if job: job['sub_progress'] = 100
        manual_html = response.choices[0].message.content
        manual_path = transcript_text_path.replace('.txt', '_summary.html')
        pdf_path = transcript_text_path.replace('.txt', '_summary.pdf')
        with open(manual_path, "w", encoding="utf-8") as f: f.write(manual_html)
        try:
            with open(pdf_path, "w+b") as result_file: pisa.CreatePDF(manual_html, dest=result_file)
        except: pdf_path = None
        return manual_path, pdf_path, manual_html
    except: return None, None, None

def process_track(url, session_dir, track_index, ffmpeg_exe, session_id, zip_path, lock, track_name, artist_name, thumbnail, start_time, end_time, transcribe_audio):
    job = conversion_jobs.get(session_id)
    if not job or job.get('cancelled'): return False

    temp_filename_base = f"track_{track_index}"
    
    def progress_hook(d):
        if job.get('cancelled'): 
            raise Exception("CancelledByUser")
            
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            if total and d.get('downloaded_bytes'):
                job['sub_progress'] = int((d['downloaded_bytes'] / total) * 100)
            job['current_status'] = 'Downloading audio...'
        elif d['status'] == 'finished':
            job['sub_progress'] = 100
            job['current_status'] = 'Extracting audio...'

    ydl_opts = {
        'format': 'http_mp3_128/bestaudio[ext=mp3]/bestaudio/best',
        'outtmpl': os.path.join(session_dir, f"{temp_filename_base}.%(ext)s"),
        'ffmpeg_location': ffmpeg_exe,
        'quiet': True, 'no_warnings': True, 'nocheckcertificate': True,
        'socket_timeout': 30, 'retries': 5,
        'hls_prefer_native': True, 
        'writethumbnail': False,
        'progress_hooks': [progress_hook], 'cookiefile': None,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        },
        'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '128'},
        ],
        'postprocessor_args': {
            'ffmpeg': [
                '-map_metadata', '-1', 
                '-threads', '1',
                '-err_detect', 'ignore_err'
            ]
        },
    }

    if start_time or end_time:
        ydl_opts['external_downloader'] = ffmpeg_exe
        ffmpeg_args = ['-y']
        if start_time:
            ffmpeg_args.extend(['-ss', str(start_time)])
        if end_time:
            ffmpeg_args.extend(['-to', str(end_time)])
        ydl_opts['external_downloader_args'] = {'ffmpeg_i': ffmpeg_args}

    try:
        job['current_track'] = track_index
        job['last_update'] = time.time()
        job['current_status'] = f'Initializing track {track_index}...'
        job['sub_progress'] = 0
        job['current_thumbnail'] = thumbnail 
        
        if job.get('cancelled'): return False

        try:
            with YoutubeDL({'quiet':True, 'no_warnings':True, 'socket_timeout':10}) as ydl:
                info = ydl.extract_info(url, download=False)
                if info.get('title'): track_name = info['title']
                if info.get('uploader'): artist_name = info['uploader']
                if info.get('thumbnail'): job['current_thumbnail'] = info['thumbnail']
        except: pass
        
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        mp3_files = glob.glob(os.path.join(session_dir, f"{temp_filename_base}*.mp3"))
        if mp3_files:
            file_to_zip = mp3_files[0]

            try:
                cmd = [
                    ffmpeg_exe, '-y', '-i', file_to_zip, 
                    '-map_metadata', '-1', 
                    '-metadata', f'title={track_name}', 
                    '-metadata', f'artist={artist_name}', 
                    '-c', 'copy', file_to_zip + '.tmp'
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                if os.path.exists(file_to_zip + '.tmp'): 
                    os.replace(file_to_zip + '.tmp', file_to_zip)
            except: pass

            clean_name = "".join([c for c in f"{artist_name} - {track_name}"[:100] if c.isalnum() or c in (' ', '-', '_')]).strip() or f"Track_{track_index}"
            
            with lock:
                with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                    z.write(file_to_zip, f"{clean_name}.mp3")
            
            if transcribe_audio:
                raw_txt_path, raw_pdf_path = transcribe_audio_file(file_to_zip, job)
                
                if raw_txt_path:
                    html_path, summary_pdf_path, manual_html = generate_diy_manual(raw_txt_path, job)
                    
                    with lock:
                        with zipfile.ZipFile(zip_path, 'a', zipfile.ZIP_STORED) as z:
                            if raw_pdf_path and os.path.exists(raw_pdf_path): z.write(raw_pdf_path, f"{clean_name}_raw_transcript.pdf")
                            if summary_pdf_path and os.path.exists(summary_pdf_path): z.write(summary_pdf_path, f"{clean_name}_summary.pdf")

                    if manual_html: job['email_summaries'] += f"<hr><h2>{clean_name}</h2>" + manual_html

            job['completed'] += 1
            job['sub_progress'] = 100
            job['completed_tracks'].append(clean_name)
            return True
        else:
            if not job.get('cancelled'):
                job['skipped'] += 1
                job['last_track_error'] = "Download finished, but no MP3 file was created."
                job['failed_track_details'].append({
                    "track": track_name or f"Track {track_index}",
                    "reason": "Corrupted stream or missing audio track."
                })
            return False

    except Exception as e:
        if not job.get('cancelled'): 
            job['skipped'] += 1
            error_string = str(e)
            job['last_track_error'] = error_string
            
            if "404" in error_string:
                friendly_reason = "Private, deleted, or invalid track link."
            elif "403" in error_string:
                friendly_reason = "Geo-blocked or access denied by platform."
            elif "ffmpeg" in error_string.lower():
                friendly_reason = "Server audio processor (FFmpeg) missing."
            else:
                friendly_reason = "Unsupported format or protected track."

            job['failed_track_details'].append({
                "track": track_name or f"Track {track_index}",
                "reason": friendly_reason
            })
        return False
        
    finally:
        try:
            for f in glob.glob(os.path.join(session_dir, f"{temp_filename_base}*")):
                try: os.remove(f)
                except: pass
        except: pass
        cleanup_memory()

def run_conversion_task(session_id, url, entries, user_email=None, start_time=None, end_time=None, transcribe_audio=False, user_id=None, payment_method=None):
    global current_processing_session
    current_processing_session = session_id
    job = conversion_jobs[session_id]
    session_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    os.makedirs(session_dir, exist_ok=True)
    zip_path = os.path.join(session_dir, "playlist_backup.zip")
    zip_locks[session_id] = BoundedSemaphore(1)
    ffmpeg_exe = 'ffmpeg_bin/ffmpeg' if os.path.exists('ffmpeg_bin/ffmpeg') else 'ffmpeg'

    try:
        job['status'] = 'processing'
        for idx, t_url, t_title, t_artist, t_thumb in entries:
            if job.get('cancelled'): break
            process_track(t_url, session_dir, idx, ffmpeg_exe, session_id, zip_path, zip_locks[session_id], t_title, t_artist, t_thumb, start_time, end_time, transcribe_audio)

        if not job.get('cancelled'):
            if job['completed'] == 0:
                job['status'] = 'error'
                hidden_error = job.get('last_track_error', 'Unknown internal error.')
                job['error'] = (
                    "Failed to extract audio. "
                    f"System Output: {hidden_error}"
                )
            else:
                job['status'] = 'completed'
                job['zip_ready'] = True
                job['zip_path'] = f"/download/{session_id}/playlist_backup.zip"
                if user_email: notify_user_complete(session_id, user_email, job['completed'], job.get('email_summaries', ''))
        else:
            job['status'] = 'cancelled'
    except Exception as e:
        job['status'] = 'error'
        job['error'] = str(e)
    finally:
        if session_id in zip_locks: del zip_locks[session_id]
        current_processing_session = None
        
        unused_tracks = job['total'] - job['completed']
        refund_unused_credits(user_id, payment_method, unused_tracks)
        
        cleanup_memory()

def worker_loop():
    while True:
        try:
            if conversion_queue:
                task_data = conversion_queue.popleft()
                sid = task_data['session_id']
                job = conversion_jobs.get(sid, {})
                
                if job.get('cancelled'):
                    job['status'] = 'cancelled'
                    unused_tracks = job.get('total', 0) - job.get('completed', 0)
                    refund_unused_credits(task_data.get('user_id'), task_data.get('payment_method'), unused_tracks)
                    continue
                    
                run_conversion_task(
                    sid, task_data['url'], task_data['entries'], task_data.get('email'), 
                    task_data.get('start_time'), task_data.get('end_time'), task_data.get('transcribe_audio'),
                    task_data.get('user_id'), task_data.get('payment_method')
                )
            else: time.sleep(1)
        except: time.sleep(1)

queue_worker = Thread(target=worker_loop, daemon=True)
queue_worker.start()

@app.route('/start_conversion', methods=['POST'])
def start_conversion():
    cleanup_old_sessions()
    user = get_or_create_user()
    data = request.json
    url = data.get('url', '').strip()
    session_id = data.get('session_id', str(uuid.uuid4()))
    if not url: return jsonify({"error": "No URL provided"}), 400
    try:
        with YoutubeDL({'extract_flat': True, 'quiet': True, 'playlistend': MAX_SONGS, 'nocheckcertificate': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get('entries', [info]) if info else []
            valid_entries = []
            for i, e in enumerate(entries[:MAX_SONGS]):
                if e:
                    track_url = e.get('url') or e.get('webpage_url') or e.get('id', '')
                    if not track_url.startswith('http') and 'soundcloud' in url: 
                        track_url = f"https://api.soundcloud.com/tracks/{e.get('id', i)}"
                    elif not track_url.startswith('http'): continue 
                    valid_entries.append((i+1, track_url, e.get('title', f"Track {i+1}"), e.get('uploader', 'Artist'), e.get('thumbnail', '')))
            total_tracks = len(valid_entries)

        if total_tracks == 0: return jsonify({"error": "No tracks found or supported."}), 400
        
        payment_method = None
        if user.paid_track_credits >= total_tracks:
            user.paid_track_credits -= total_tracks
            payment_method = 'credits'
            db.session.commit()
        elif user.free_conversions_used + total_tracks <= 5:
            user.free_conversions_used += total_tracks
            payment_method = 'free'
            db.session.commit()
        else:
            available_free = 5 - user.free_conversions_used
            return jsonify({
                "error": f"Limit reached. This playlist has {total_tracks} tracks, but you only have {available_free} free uses and {user.paid_track_credits} credits.", 
                "requires_payment": True
            }), 403

        conversion_jobs[session_id] = {
            'status': 'queued', 'total': total_tracks, 'completed': 0, 'skipped': 0, 'current_track': 0, 
            'completed_tracks': [], 'skipped_tracks': [], 'failed_track_details': [],
            'cancelled': False, 'zip_ready': False, 'current_thumbnail': '', 
            'last_update': time.time(), 'email_summaries': '', 'sub_progress': 0 
        }
        
        conversion_queue.append({
            'session_id': session_id, 'url': url, 'entries': valid_entries,
            'email': user.email if not user.email.startswith('anon_') else None,
            'user_id': user.id, 'payment_method': payment_method,
            'start_time': data.get('start_time'), 'end_time': data.get('end_time'),
            'transcribe_audio': data.get('transcribe_audio', False)
        })
        return jsonify({"session_id": session_id, "total_tracks": total_tracks, "status": "queued", "queue_position": len(conversion_queue)}), 200
    except Exception as e:
        return jsonify({"error": "This URL may be protected and unsupported."}), 400

@app.route('/status/<session_id>', methods=['GET'])
def get_status(session_id):
    job = conversion_jobs.get(session_id)
    if not job: return jsonify({"error": "Session not found"}), 404
    queue_pos, wait_seconds = 0, 0
    if job['status'] == 'queued':
        if current_processing_session and current_processing_session != session_id:
            curr_job = conversion_jobs.get(current_processing_session)
            if curr_job and curr_job['status'] == 'processing':
                wait_seconds += (max(0, curr_job['total'] - curr_job['completed']) * AVG_TIME_PER_TRACK)
        for idx, item in enumerate(conversion_queue):
            if item['session_id'] == session_id: queue_pos = idx + 1; break
            wait_seconds += (len(item['entries']) * AVG_TIME_PER_TRACK)
    
    return jsonify({
        "status": job['status'], 
        "total": job['total'], 
        "completed": job['completed'], 
        "skipped": job['skipped'], 
        "failed_details": job.get('failed_track_details', []),
        "current_track": job['current_track'], 
        "current_status": job.get('current_status', ''), 
        "current_thumbnail": job.get('current_thumbnail', ''), 
        "zip_ready": job.get('zip_ready', False),
        "zip_path": job.get('zip_path', ''), 
        "sub_progress": job.get('sub_progress', 0),
        "error": job.get('error', ''), 
        "queue_position": queue_pos, 
        "estimated_wait": math.ceil(wait_seconds / 60)
    }), 200

@app.route('/cancel', methods=['POST'])
def cancel_conversion():
    session_id = request.json.get('session_id')
    if session_id in conversion_jobs:
        job = conversion_jobs[session_id]
        job['cancelled'] = True
        if job['status'] == 'queued': job['status'] = 'cancelled'
        
        try:
            for item in list(conversion_queue):
                if item['session_id'] == session_id: 
                    conversion_queue.remove(item)
                    unused_tracks = job['total'] - job['completed']
                    refund_unused_credits(item.get('user_id'), item.get('payment_method'), unused_tracks)
                    break
        except: pass
        
        return jsonify({"status": "cancelling"}), 200
    return jsonify({"status": "not_found"}), 404

@app.route('/download/<session_id>/<filename>')
def download_file(session_id, filename):
    file_path = os.path.join(DOWNLOAD_FOLDER, session_id, filename)
    if os.path.exists(file_path): return send_file(file_path, as_attachment=True)
    return "File not found", 404

@app.route('/health')
def health(): return jsonify({"status": "ok"}), 200
@app.route('/')
def index(): return jsonify({"message": "Audio Processor API", "status": "active"}), 200

if __name__ == '__main__': app.run(debug=False, port=5000, threaded=True)