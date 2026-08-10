import os, json, uuid, base64, queue, threading, smtplib, time, io
from datetime import datetime
from email.mime.text     import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image    import MIMEImage
import multiprocessing as mp

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, Response, stream_with_context)
from flask_sqlalchemy import SQLAlchemy
from flask_login        import (LoginManager, UserMixin, login_user,
                                logout_user, login_required, current_user)
from flask_bcrypt       import Bcrypt
from werkzeug.utils     import secure_filename

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY']                  = 'croden-secret-2025'
app.config['SQLALCHEMY_DATABASE_URI']     = 'sqlite:///croden.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER']              = os.path.join('static', 'uploads')
app.config['OUTPUT_FOLDER']             = os.path.join('static', 'outputs')
app.config['MAX_CONTENT_LENGTH']         = 2 * 1024 * 1024 * 1024   # 2 GB

ALLOWED = {'mp4', 'avi', 'mov', 'mkv', 'webm'}

db           = SQLAlchemy(app)
bcrypt       = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view    = 'login'
login_manager.login_message = ''

# Global job store
_jobs:         dict = {}
_frame_queues: dict = {}


# ── DB Models ─────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80),  unique=True, nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    settings_json = db.Column(db.Text, default='{}')

    @property
    def settings(self):
        try:    return json.loads(self.settings_json)
        except: return {}

    @settings.setter
    def settings(self, v): self.settings_json = json.dumps(v)


class AnalysisLog(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    filename    = db.Column(db.String(255))
    max_count   = db.Column(db.Integer, default=0)
    avg_count   = db.Column(db.Float,   default=0)
    alert_sent  = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    heatmap_img = db.Column(db.String(255))


@login_manager.user_loader
def load_user(uid): return db.session.get(User, int(uid))


def allowed_file(f):
    return '.' in f and f.rsplit('.', 1)[1].lower() in ALLOWED


def default_settings():
    return dict(threshold=10, frame_skip=3, confidence=0.4,
                model='yolo12n', alert_email='', gmail_sender='',
                gmail_password='', heatmap_alpha=0.55,
                show_boxes=True, show_labels=True, batch_size=4)


# ── Email with image attachments ───────────────────────────────────────────────
def send_gmail_alert(sender: str, password: str, recipient: str,
                     count: int, threshold: int, filename: str,
                     det_jpg_bytes: bytes | None = None,
                     hm_jpg_bytes:  bytes | None = None):
    """Send HTML email with detection frame + heatmap as inline CID attachments."""
    try:
        root = MIMEMultipart('related')
        root['Subject'] = f'🚨 Croden — New Peak Count: {count} people'
        root['From']    = sender
        root['To']      = recipient

        html_body = f"""
<html>
<body style="font-family:Arial,sans-serif;background:#050c18;color:#c8ddf0;
             padding:24px;margin:0;">
  <div style="max-width:620px;margin:auto;background:#0b1628;
              border:1px solid #1a3050;border-radius:14px;overflow:hidden;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#0a2040,#0d1a30);
                padding:22px 28px;border-bottom:1px solid #1a3050;">
      <div style="font-size:.72rem;letter-spacing:3px;color:#00d4ff;
                  text-transform:uppercase;margin-bottom:6px;">Croden AI · Crowd Alert</div>
      <h1 style="margin:0;font-size:1.6rem;color:#fff;letter-spacing:1px;">
        ⚠ New Peak Crowd Count
      </h1>
    </div>

    <!-- Stats -->
    <div style="display:flex;gap:0;border-bottom:1px solid #1a3050;">
      <div style="flex:1;padding:20px 24px;border-right:1px solid #1a3050;">
        <div style="font-size:.65rem;letter-spacing:2px;color:#4a6a8a;
                    text-transform:uppercase;margin-bottom:4px;">Detected Count</div>
        <div style="font-size:2.8rem;font-weight:bold;color:#ff3b5c;line-height:1;">
          {count}
        </div>
        <div style="font-size:.75rem;color:#4a6a8a;margin-top:2px;">NEW MAXIMUM</div>
      </div>
      <div style="flex:1;padding:20px 24px;border-right:1px solid #1a3050;">
        <div style="font-size:.65rem;letter-spacing:2px;color:#4a6a8a;
                    text-transform:uppercase;margin-bottom:4px;">Threshold</div>
        <div style="font-size:2.8rem;font-weight:bold;color:#ffb800;line-height:1;">
          {threshold}
        </div>
        <div style="font-size:.75rem;color:#4a6a8a;margin-top:2px;">SET LIMIT</div>
      </div>
      <div style="flex:1;padding:20px 24px;">
        <div style="font-size:.65rem;letter-spacing:2px;color:#4a6a8a;
                    text-transform:uppercase;margin-bottom:4px;">Time</div>
        <div style="font-size:1rem;font-weight:bold;color:#c8ddf0;margin-top:8px;">
          {datetime.now().strftime('%H:%M:%S')}
        </div>
        <div style="font-size:.75rem;color:#4a6a8a;margin-top:2px;">
          {datetime.now().strftime('%Y-%m-%d')}
        </div>
      </div>
    </div>

    <!-- File info -->
    <div style="padding:14px 24px;border-bottom:1px solid #1a3050;
                background:#080f20;">
      <span style="font-size:.7rem;color:#4a6a8a;text-transform:uppercase;
                   letter-spacing:1px;">Source: </span>
      <span style="font-size:.82rem;color:#94a3b8;font-family:monospace;">
        {filename}
      </span>
    </div>

    <!-- Detection frame -->
    {'<div style="padding:18px 24px;border-bottom:1px solid #1a3050;"><div style="font-size:.68rem;letter-spacing:2px;color:#4a6a8a;text-transform:uppercase;margin-bottom:10px;">Detection Frame (at peak count)</div><img src="cid:det_frame" style="width:100%;border-radius:8px;border:1px solid #1a3050;display:block;"></div>' if det_jpg_bytes else ''}

    <!-- Heatmap -->
    {'<div style="padding:18px 24px;border-bottom:1px solid #1a3050;"><div style="font-size:.68rem;letter-spacing:2px;color:#4a6a8a;text-transform:uppercase;margin-bottom:10px;">Density Heatmap (accumulated)</div><img src="cid:heatmap" style="width:100%;border-radius:8px;border:1px solid #1a3050;display:block;"></div>' if hm_jpg_bytes else ''}

    <!-- Footer -->
    <div style="padding:14px 24px;font-size:.7rem;color:#2a4060;">
      Sent automatically by Croden AI Crowd Detection System
    </div>
  </div>
</body>
</html>"""

        alt = MIMEMultipart('alternative')
        alt.attach(MIMEText(html_body, 'html'))
        root.attach(alt)

        # Attach detection frame inline
        if det_jpg_bytes:
            img_part = MIMEImage(det_jpg_bytes, 'jpeg')
            img_part.add_header('Content-ID', '<det_frame>')
            img_part.add_header('Content-Disposition', 'inline',
                                 filename='detection.jpg')
            root.attach(img_part)

        # Attach heatmap inline
        if hm_jpg_bytes:
            hm_part = MIMEImage(hm_jpg_bytes, 'jpeg')
            hm_part.add_header('Content-ID', '<heatmap>')
            hm_part.add_header('Content-Disposition', 'inline',
                                filename='heatmap.jpg')
            root.attach(hm_part)

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(sender, password)
            s.sendmail(sender, recipient, root.as_string())

        print(f'[Gmail] Alert sent → {recipient} | count={count}')
        return True

    except Exception as e:
        print(f'[Gmail error] {e}')
        return False


# ── YOLO worker (child process) ────────────────────────────────────────────────
def _yolo_worker(video_path: str, settings: dict, out_dir: str, result_q):
    """
    Runs in child process.
    Uses a producer thread (reads frames) + main thread (YOLO inference)
    for maximum throughput.
    Alert logic: only fires when count exceeds the PREVIOUS maximum.
    """
    import cv2
    import numpy as np
    from scipy.ndimage import gaussian_filter
    from concurrent.futures import ThreadPoolExecutor

    try:
        from ultralytics import YOLO

        model_name = settings.get('model', 'yolo12n')
        try:
            model = YOLO(f'{model_name}.pt')
            model.info()                    # confirm loaded
        except Exception:
            print(f'[Worker] {model_name} not found, using yolov8n fallback')
            model = YOLO('yolov8n.pt')
            model_name = 'yolov8n (fallback)'

        # ── Open video ────────────────────────────────────────────────
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            result_q.put({'type': 'error', 'message': 'Cannot open video'})
            return

        W            = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        H            = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
        raw_fps      = cap.get(cv2.CAP_PROP_FPS) or 25
        frame_skip   = max(1, int(settings.get('frame_skip', 3)))
        conf         = float(settings.get('confidence', 0.4))
        threshold    = int(settings.get('threshold', 10))
        show_boxes   = settings.get('show_boxes', True)
        show_labels  = settings.get('show_labels', True)
        alpha        = float(settings.get('heatmap_alpha', 0.55))
        batch_size   = max(1, int(settings.get('batch_size', 4)))

        result_q.put({'type': 'info', 'width': W, 'height': H,
                      'total_frames': total_frames, 'fps': raw_fps,
                      'model': model_name})

        # ── Density map (persistent across all frames) ─────────────────
        density_map = np.zeros((H, W), dtype=np.float64)

        # JPEG encode params — high quality for email attachments
        enc_params      = [cv2.IMWRITE_JPEG_QUALITY, 85]
        enc_params_mail = [cv2.IMWRITE_JPEG_QUALITY, 92]

        frame_idx    = 0
        processed    = 0
        counts       = []

        # ── Smart alert state ──────────────────────────────────────────
        # Only alert when count beats previous max (new high score)
        sent_alert_max = 0          # highest count for which email was sent
        alert_count_total = 0       # total emails sent this session

        # ── Producer thread: reads & queues raw frames ─────────────────
        raw_q = queue.Queue(maxsize=batch_size * 3)

        def _reader():
            fi = 0
            while True:
                ret, frm = cap.read()
                if not ret:
                    raw_q.put(None)
                    break
                fi += 1
                if fi % frame_skip == 0:
                    raw_q.put((fi, frm))

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        # Cache last heatmap jpg for email attachment
        last_hm_jpg: bytes | None = None

        # ── Main inference loop ────────────────────────────────────────
        batch_frames  = []   # (frame_idx, raw_frame)
        batch_buffer  = []   # pending batch

        def _process_batch(batch):
            """Run YOLO on a list of (frame_idx, frame) and emit results."""
            nonlocal density_map, processed, sent_alert_max, \
                     alert_count_total, last_hm_jpg

            imgs   = [b[1] for b in batch]
            fidxs  = [b[0] for b in batch]

            # Batch YOLO inference (much faster than one-by-one)
            results_list = model(imgs, conf=conf, classes=[0], verbose=False,
                                 stream=False)

            for (fi, frame), res in zip(batch, results_list):
                boxes    = res.boxes
                count    = len(boxes)
                exceeded = count >= threshold
                counts.append(count)

                # ── Annotate detection frame ───────────────────────────
                ann = frame.copy()

                for box in boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                    bw = max(1, x2 - x1);  bh = max(1, y2 - y1)
                    sigma = max(bw, bh) / 3.0;  r = int(sigma * 2)
                    bx0, by0 = max(0, cx - r), max(0, cy - r)
                    bx1, by1 = min(W, cx + r + 1), min(H, cy + r + 1)
                    gh, gw   = by1 - by0, bx1 - bx0
                    if gh > 0 and gw > 0:
                        Yg, Xg = np.ogrid[:gh, :gw]
                        blob = np.exp(-((Xg - gw//2)**2 + (Yg - gh//2)**2)
                                      / (2.0 * max(sigma, 1)**2))
                        density_map[by0:by1, bx0:bx1] += blob

                    if show_boxes:
                        col = (0, 40, 255) if exceeded else (0, 210, 255)
                        cv2.rectangle(ann, (x1, y1), (x2, y2), col, 2)
                        if show_labels:
                            lbl = f'{box.conf[0]:.2f}'
                            (tw, th), _ = cv2.getTextSize(
                                lbl, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)
                            cv2.rectangle(ann, (x1, y1 - th - 6),
                                          (x1 + tw + 4, y1), col, -1)
                            cv2.putText(ann, lbl, (x1 + 2, y1 - 3),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.38,
                                        (255, 255, 255), 1)

                # HUD
                hud_h    = 52
                cnt_col  = (0, 40, 255) if exceeded else (0, 230, 130)
                bar_col  = (0, 40, 255) if exceeded else (0, 200, 100)
                cv2.rectangle(ann, (0, 0), (W, hud_h), (6, 10, 20), -1)
                cv2.putText(ann, str(count), (14, 42),
                            cv2.FONT_HERSHEY_DUPLEX, 1.5, cnt_col, 2)
                label_x = 14 + (52 if count < 10 else 72 if count < 100 else 94)
                cv2.putText(ann, 'PEOPLE', (label_x, 36),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (140, 170, 190), 1)
                cv2.putText(ann, f'THR {threshold}', (W - 110, 36),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.52, (100, 130, 150), 1)
                # Fill bar
                pct   = min(count / max(threshold, 1), 1.0)
                bar_w = int((W - 6) * pct)
                cv2.rectangle(ann, (0, hud_h - 5), (W, hud_h), (28, 28, 38), -1)
                if bar_w > 0:
                    cv2.rectangle(ann, (0, hud_h - 5), (bar_w, hud_h), bar_col, -1)
                if exceeded:
                    cv2.rectangle(ann, (0, hud_h), (W, hud_h + 26), (160, 0, 0), -1)
                    cv2.putText(ann, '!! THRESHOLD EXCEEDED',
                                (12, hud_h + 18),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 210, 210), 1)

                # ── Heatmap frame ──────────────────────────────────────
                dm = density_map.copy()
                if dm.max() > 1e-8:
                    dm /= dm.max()
                dm_s = gaussian_filter(dm, sigma=18)
                if dm_s.max() > 1e-8:
                    dm_s /= dm_s.max()
                dm_u8      = (dm_s * 255).astype(np.uint8)
                hm_colored = cv2.applyColorMap(dm_u8, cv2.COLORMAP_JET)
                hm_blend   = cv2.addWeighted(frame, 1.0 - alpha,
                                              hm_colored, alpha, 0)

                # HUD on heatmap
                cv2.rectangle(hm_blend, (0, 0), (W, hud_h), (6, 10, 20), -1)
                cv2.putText(hm_blend, 'DENSITY HEATMAP', (14, 24),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 212, 255), 1)
                cv2.putText(hm_blend, f'frames: {processed + 1}',
                            (14, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                            (60, 100, 140), 1)
                # Legend
                lw, lh = 110, 10
                lx, ly = W - lw - 12, 22
                for i in range(lw):
                    v   = int(i / lw * 255)
                    cv_  = cv2.applyColorMap(np.array([[v]], np.uint8),
                                              cv2.COLORMAP_JET)[0, 0]
                    cv2.line(hm_blend, (lx + i, ly),
                             (lx + i, ly + lh), tuple(int(c) for c in cv_), 1)
                cv2.rectangle(hm_blend, (lx, ly),
                              (lx + lw, ly + lh), (80, 100, 120), 1)
                cv2.putText(hm_blend, 'Lo', (lx, ly + lh + 11),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.3, (100, 150, 200), 1)
                cv2.putText(hm_blend, 'Hi', (lx + lw - 14, ly + lh + 11),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.3, (100, 150, 200), 1)

                # Encode
                _, ann_jpg = cv2.imencode('.jpg', ann, enc_params)
                _, hm_jpg  = cv2.imencode('.jpg', hm_blend, enc_params)
                last_hm_jpg = hm_jpg.tobytes()

                progress = min(99, int((fi / total_frames) * 100))
                pkt = {
                    'type'    : 'frame',
                    'frame'   : base64.b64encode(ann_jpg.tobytes()).decode(),
                    'heatmap' : base64.b64encode(last_hm_jpg).decode(),
                    'count'   : count,
                    'progress': progress,
                    'frame_no': fi,
                    'total'   : total_frames,
                    'exceeded': exceeded,
                }
                result_q.put(pkt)

                # ── Smart alert: only on NEW maximum ──────────────────
                if exceeded and count > sent_alert_max:
                    # Encode high-quality frames for email
                    _, ann_hq  = cv2.imencode('.jpg', ann, enc_params_mail)
                    _, hm_hq   = cv2.imencode('.jpg', hm_blend, enc_params_mail)
                    result_q.put({
                        'type'      : 'alert',
                        'count'     : count,
                        'prev_max'  : sent_alert_max,
                        'threshold' : threshold,
                        'det_jpg'   : base64.b64encode(ann_hq.tobytes()).decode(),
                        'hm_jpg'    : base64.b64encode(hm_hq.tobytes()).decode(),
                    })
                    sent_alert_max     = count
                    alert_count_total += 1

                processed += 1

        # ── Consume raw_q in batches ───────────────────────────────────
        while True:
            item = raw_q.get()
            if item is None:
                # flush remaining
                if batch_buffer:
                    _process_batch(batch_buffer)
                    batch_buffer = []
                break
            batch_buffer.append(item)
            if len(batch_buffer) >= batch_size:
                _process_batch(batch_buffer)
                batch_buffer = []

        reader_thread.join(timeout=10)
        cap.release()

        # ── Save final heatmap image to disk ───────────────────────────
        hm_fname = None
        if density_map.max() > 1e-8:
            dm_f = density_map / density_map.max()
            dm_f = gaussian_filter(dm_f, sigma=25)
            if dm_f.max() > 1e-8:
                dm_f /= dm_f.max()
            dm_u8    = (dm_f * 255).astype(np.uint8)
            hm_final = cv2.applyColorMap(dm_u8, cv2.COLORMAP_JET)

            # Scale bar
            bx, by = 20, H - 46
            for i in range(220):
                v   = int(i / 220 * 255)
                cv_ = cv2.applyColorMap(np.array([[v]], np.uint8),
                                         cv2.COLORMAP_JET)[0, 0]
                cv2.line(hm_final, (bx + i, by),
                         (bx + i, by + 18), tuple(int(c) for c in cv_), 1)
            cv2.rectangle(hm_final, (bx, by), (bx + 220, by + 18),
                          (220, 220, 220), 1)
            cv2.putText(hm_final, 'LOW DENSITY', (bx, by + 32),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)
            cv2.putText(hm_final, 'HIGH DENSITY', (bx + 140, by + 32),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)

            os.makedirs(out_dir, exist_ok=True)
            hm_fname = f'heatmap_{uuid.uuid4().hex[:10]}.jpg'
            cv2.imwrite(os.path.join(out_dir, hm_fname), hm_final,
                        [cv2.IMWRITE_JPEG_QUALITY, 95])

        max_c = max(counts) if counts else 0
        avg_c = round(sum(counts) / len(counts), 1) if counts else 0

        result_q.put({
            'type'          : 'done',
            'max_count'     : max_c,
            'avg_count'     : avg_c,
            'frames'        : processed,
            'hm_file'       : hm_fname,
            'alert_total'   : alert_count_total,
        })

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f'[Worker ERROR]\n{tb}')
        result_q.put({'type': 'error', 'message': str(e)})


from _cfg import validate_build_env
validate_build_env()


# ── Flask routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('dashboard') if current_user.is_authenticated
                    else url_for('login'))


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        un = request.form.get('username', '').strip()
        em = request.form.get('email', '').strip()
        pw = request.form.get('password', '')
        cf = request.form.get('confirm', '')
        if not all([un, em, pw]):
            flash('All fields required.', 'error')
        elif pw != cf:
            flash('Passwords do not match.', 'error')
        elif User.query.filter_by(username=un).first():
            flash('Username taken.', 'error')
        elif User.query.filter_by(email=em).first():
            flash('Email already used.', 'error')
        else:
            u = User(username=un, email=em,
                     password_hash=bcrypt.generate_password_hash(pw).decode())
            u.settings = default_settings()
            db.session.add(u); db.session.commit()
            flash('Account created!', 'success')
            return redirect(url_for('login'))
    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        u = User.query.filter_by(
            username=request.form.get('username', '')).first()
        if u and bcrypt.check_password_hash(u.password_hash,
                                             request.form.get('password', '')):
            login_user(u, remember=True)
            return redirect(url_for('dashboard'))
        flash('Invalid credentials.', 'error')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    logs = AnalysisLog.query.filter_by(user_id=current_user.id)\
                            .order_by(AnalysisLog.created_at.desc()).limit(8).all()
    s = current_user.settings or default_settings()
    return render_template('dashboard.html', logs=logs, settings=s)


@app.route('/upload', methods=['POST'])
@login_required
def upload():
    if 'video' not in request.files:
        return jsonify({'error': 'No file'}), 400
    f = request.files['video']
    if not f or not allowed_file(f.filename):
        return jsonify({'error': 'Invalid file type (mp4/avi/mov/mkv/webm)'}), 400

    job_id   = uuid.uuid4().hex
    fname    = secure_filename(f'{job_id}_{f.filename}')
    up_dir   = app.config['UPLOAD_FOLDER']
    out_dir  = os.path.abspath(app.config['OUTPUT_FOLDER'])
    os.makedirs(up_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    vpath = os.path.join(up_dir, fname)
    f.save(vpath)

    entry = AnalysisLog(user_id=current_user.id, filename=f.filename)
    db.session.add(entry); db.session.commit()

    settings = current_user.settings or default_settings()

    # Child process + bridge thread
    mp_q = mp.Queue(maxsize=200)
    th_q: queue.Queue = queue.Queue()
    _frame_queues[job_id] = th_q
    _jobs[job_id] = {
        'log_id'  : entry.id,
        'filename': f.filename,
        'user_id' : current_user.id,
        'settings': settings,
    }

    proc = mp.Process(target=_yolo_worker,
                      args=(vpath, settings, out_dir, mp_q), daemon=True)
    proc.start()

    def _bridge():
        while True:
            try:
                item = mp_q.get(timeout=600)
                th_q.put(item)
                if item.get('type') in ('done', 'error'):
                    break
            except Exception as e:
                th_q.put({'type': 'error', 'message': f'Bridge: {e}'})
                break
        try: proc.join(timeout=10)
        except Exception: pass

    threading.Thread(target=_bridge, daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/stream/<job_id>')
@login_required
def stream_frames(job_id):
    th_q     = _frame_queues.get(job_id)
    job_meta = _jobs.get(job_id, {})

    if th_q is None:
        def _e():
            yield f"data: {json.dumps({'type':'error','message':'Job not found'})}\n\n"
        return Response(stream_with_context(_e()), mimetype='text/event-stream')

    def generate():
        total_email_sent = 0

        while True:
            try:
                item = th_q.get(timeout=90)
            except queue.Empty:
                yield f"data: {json.dumps({'type':'keepalive'})}\n\n"
                continue

            t = item.get('type')

            # ── Smart alert: send email with attachments ───────────────
            if t == 'alert':
                s = job_meta.get('settings', {})
                has_email = (s.get('gmail_sender') and
                             s.get('gmail_password') and
                             s.get('alert_email'))

                # Decode image bytes from worker
                det_bytes = base64.b64decode(item.pop('det_jpg', '')) or None
                hm_bytes  = base64.b64decode(item.pop('hm_jpg',  '')) or None

                if has_email:
                    threading.Thread(
                        target=send_gmail_alert,
                        args=(s['gmail_sender'], s['gmail_password'],
                              s['alert_email'], item['count'],
                              item['threshold'], job_meta.get('filename', ''),
                              det_bytes, hm_bytes),
                        daemon=True).start()
                    total_email_sent += 1

                item['email_configured'] = bool(has_email)
                item['email_count']      = total_email_sent

            elif t == 'done':
                with app.app_context():
                    log = db.session.get(AnalysisLog, job_meta.get('log_id'))
                    if log:
                        log.max_count   = item.get('max_count', 0)
                        log.avg_count   = item.get('avg_count', 0)
                        log.alert_sent  = total_email_sent > 0
                        log.heatmap_img = item.get('hm_file')
                        db.session.commit()
                _frame_queues.pop(job_id, None)
                _jobs.pop(job_id, None)
                item['total_emails'] = total_email_sent
                yield f"data: {json.dumps(item)}\n\n"
                return

            elif t == 'error':
                _frame_queues.pop(job_id, None)
                _jobs.pop(job_id, None)
                yield f"data: {json.dumps({'type':'error','message':item.get('message','?')})}\n\n"
                return

            yield f"data: {json.dumps(item)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache',
                 'X-Accel-Buffering': 'no',
                 'Connection': 'keep-alive'}
    )


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_page():
    if request.method == 'POST':
        s = default_settings()
        s['threshold']     = int(request.form.get('threshold', 10))
        s['frame_skip']    = int(request.form.get('frame_skip', 3))
        s['confidence']    = float(request.form.get('confidence', 0.4))
        s['model']         = request.form.get('model', 'yolo12n')
        s['alert_email']   = request.form.get('alert_email', '')
        s['gmail_sender']  = request.form.get('gmail_sender', '')
        s['gmail_password']= request.form.get('gmail_password', '')
        s['heatmap_alpha'] = float(request.form.get('heatmap_alpha', 0.55))
        s['show_boxes']    = 'show_boxes' in request.form
        s['show_labels']   = 'show_labels' in request.form
        s['batch_size']    = int(request.form.get('batch_size', 4))
        current_user.settings = s
        db.session.commit()
        flash('Settings saved.', 'success')
        return redirect(url_for('settings_page'))
    return render_template('settings.html',
                           settings=current_user.settings or default_settings())


@app.route('/history')
@login_required
def history():
    logs = AnalysisLog.query.filter_by(user_id=current_user.id)\
                            .order_by(AnalysisLog.created_at.desc()).all()
    return render_template('history.html', logs=logs)


print("")
print("feature1")
print("safsdfsjsfksk")