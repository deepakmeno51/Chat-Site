from flask import Flask, render_template, request, redirect, url_for, session, send_file, abort
from flask_socketio import SocketIO, emit, join_room
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import mistune
import os
import uuid
import base64
from datetime import datetime
from dotenv import load_dotenv
from functools import wraps

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-prod')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///site.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=50 * 1024 * 1024)
markdown = mistune.create_markdown(plugins=['def_list', 'table'])

# ─── Models ────────────────────────────────────────────────────────────────────

class User(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    username   = db.Column(db.String(30), unique=True, nullable=False)
    password   = db.Column(db.String(256), nullable=False)
    sid        = db.Column(db.String(120), unique=True, nullable=True)
    status     = db.Column(db.String(20), default='online')

    def __repr__(self):
        return f'<User {self.username}>'


class Message(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    sender_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    room         = db.Column(db.String(120), nullable=True)   # null for DMs
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)  # set for DMs
    content      = db.Column(db.Text, nullable=False)
    is_private   = db.Column(db.Boolean, default=False)
    timestamp    = db.Column(db.DateTime, default=datetime.utcnow)

    sender    = db.relationship('User', foreign_keys=[sender_id])
    recipient = db.relationship('User', foreign_keys=[recipient_id])

    def __repr__(self):
        return f'<Message {self.content[:20]}>'

    def to_dict(self):
        return {
            'sender':    self.sender.username,
            'message':   self.content,
            'private':   self.is_private,
            'recipient': self.recipient.username if self.recipient else None,
            'timestamp': self.timestamp.strftime('%H:%M'),
        }


class MediaFile(db.Model):
    """One-time-view media. Deleted from disk + DB on first fetch."""
    id           = db.Column(db.Integer, primary_key=True)
    token        = db.Column(db.String(64), unique=True, nullable=False, default=lambda: uuid.uuid4().hex)
    filename     = db.Column(db.String(256), nullable=False)
    mimetype     = db.Column(db.String(64), nullable=False)
    sender_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)  # None = room media
    room         = db.Column(db.String(120), nullable=True)
    viewed       = db.Column(db.Boolean, default=False)
    timestamp    = db.Column(db.DateTime, default=datetime.utcnow)

    sender    = db.relationship('User', foreign_keys=[sender_id])
    recipient = db.relationship('User', foreign_keys=[recipient_id])


# ─── Auth helpers ───────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def current_user():
    if 'user_id' in session:
        return User.query.get(session['user_id'])
    return None


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('chat'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        action   = request.form.get('action')
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if not username or not password:
            error = 'Username and password are required.'
        elif len(username) > 30:
            error = 'Username must be 30 characters or fewer.'
        elif action == 'register':
            if User.query.filter_by(username=username).first():
                error = 'Username already taken.'
            else:
                user = User(username=username, password=generate_password_hash(password))
                db.session.add(user)
                db.session.commit()
                session['user_id']  = user.id
                session['username'] = user.username
                session.permanent   = True
                return redirect(url_for('chat'))
        elif action == 'login':
            user = User.query.filter_by(username=username).first()
            if not user or not check_password_hash(user.password, password):
                error = 'Invalid username or password.'
            else:
                session['user_id']  = user.id
                session['username'] = user.username
                session.permanent   = True
                return redirect(url_for('chat'))

    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    user = current_user()
    if user:
        user.sid = None
        user.status = 'offline'
        db.session.commit()
    session.clear()
    return redirect(url_for('login'))


@app.route('/chat')
@login_required
def chat():
    user = current_user()
    return render_template('chat.html', username=user.username)


@app.route('/media/<token>')
@login_required
def serve_media(token):
    """One-time media endpoint. File + record deleted after first view."""
    media = MediaFile.query.filter_by(token=token).first_or_404()

    # Security: only sender or intended recipient can view
    viewer = current_user()
    if media.recipient_id and viewer.id not in (media.sender_id, media.recipient_id):
        abort(403)

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
    if not os.path.exists(filepath):
        db.session.delete(media)
        db.session.commit()
        abort(404)

    # Read file into memory BEFORE deleting
    with open(filepath, 'rb') as f:
        data = f.read()

    # Delete file + record — disappearing media
    os.remove(filepath)
    db.session.delete(media)
    db.session.commit()

    # Notify sender that media was viewed via socket
    socketio.emit('media_viewed', {'token': token}, room=f'user_{media.sender_id}')

    from io import BytesIO
    return send_file(BytesIO(data), mimetype=media.mimetype)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def broadcast_user_list():
    with app.app_context():
        users     = User.query.filter(User.sid.isnot(None)).all()
        user_list = {u.username: u.status for u in users}
        socketio.emit('user_list_update', user_list)


def get_room_history(room, limit=5):
    msgs = (Message.query
            .filter_by(room=room, is_private=False)
            .order_by(Message.timestamp.desc())
            .limit(limit)
            .all())
    return [m.to_dict() for m in reversed(msgs)]


def get_dm_history(user_a_id, user_b_id, limit=5):
    msgs = (Message.query
            .filter(
                Message.is_private == True,
                db.or_(
                    db.and_(Message.sender_id == user_a_id, Message.recipient_id == user_b_id),
                    db.and_(Message.sender_id == user_b_id, Message.recipient_id == user_a_id),
                )
            )
            .order_by(Message.timestamp.desc())
            .limit(limit)
            .all())
    return [m.to_dict() for m in reversed(msgs)]


# ─── Socket events ──────────────────────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    if 'user_id' not in session:
        return False  # Reject unauthenticated socket connections

    user = User.query.get(session['user_id'])
    if not user:
        return False

    user.sid    = request.sid
    user.status = 'online'
    db.session.commit()

    # Join a personal room for targeted events (e.g. media_viewed)
    join_room(f'user_{user.id}')

    broadcast_user_list()
    print(f'{user.username} connected')


@socketio.on('disconnect')
def handle_disconnect():
    user = User.query.filter_by(sid=request.sid).first()
    if user:
        user.sid    = None
        user.status = 'offline'
        db.session.commit()
        broadcast_user_list()
        print(f'{user.username} disconnected')


@socketio.on('join_room')
def handle_join_room(data):
    room = data.get('room', '').strip()
    if not room or len(room) > 50:
        emit('room_error', {'error': 'Room name must be 1–50 characters.'})
        return

    user = User.query.get(session['user_id'])
    join_room(room)

    # Send last 5 room messages as history
    history = get_room_history(room, limit=5)
    if history:
        emit('message_history', {'messages': history, 'context': 'room'})

    emit('new_message', {
        'sender':    'System',
        'message':   f'{user.username} has joined the room.',
        'private':   False,
        'timestamp': datetime.utcnow().strftime('%H:%M'),
    }, room=room)


@socketio.on('request_dm_history')
def handle_dm_history(data):
    """Client requests last 5 DMs with a specific user."""
    other_username = data.get('with')
    other = User.query.filter_by(username=other_username).first()
    if not other:
        return

    me = User.query.get(session['user_id'])
    history = get_dm_history(me.id, other.id, limit=5)
    if history:
        emit('message_history', {'messages': history, 'context': 'dm', 'with': other_username})


@socketio.on('send_message')
def handle_message(data):
    text = data.get('message', '').strip()
    room = data.get('room', '')
    if not text or len(text) > 2000:
        return

    me = User.query.get(session['user_id'])
    if not me:
        return

    rendered    = markdown(text)
    is_private  = data.get('private', False)
    timestamp   = datetime.utcnow().strftime('%H:%M')

    if is_private:
        recipient_username = data.get('recipient')
        recipient = User.query.filter_by(username=recipient_username).first()
        if not recipient:
            return

        # Persist DM
        msg = Message(
            sender_id    = me.id,
            recipient_id = recipient.id,
            content      = rendered,
            is_private   = True,
            room         = None,
        )
        db.session.add(msg)
        db.session.commit()

        payload = {
            'sender':    me.username,
            'message':   rendered,
            'private':   True,
            'recipient': recipient_username,
            'timestamp': timestamp,
        }
        if recipient.sid:
            emit('new_message', payload, room=recipient.sid)
        emit('new_message', payload, room=request.sid)

    else:
        # Persist room message
        msg = Message(
            sender_id  = me.id,
            room       = room,
            content    = rendered,
            is_private = False,
        )
        db.session.add(msg)
        db.session.commit()

        emit('new_message', {
            'sender':    me.username,
            'message':   rendered,
            'private':   False,
            'timestamp': timestamp,
        }, room=room)


@socketio.on('send_media')
def handle_media(data):
    """
    Receives base64-encoded file from client.
    Stores it, creates a one-time token, notifies recipient.
    """
    me = User.query.get(session['user_id'])
    if not me:
        return

    b64_data  = data.get('data', '')
    mimetype  = data.get('mimetype', 'application/octet-stream')
    is_private = data.get('private', False)
    room       = data.get('room', '')

    # Validate mimetype
    allowed_types = {
        'image/jpeg', 'image/png', 'image/gif', 'image/webp',
        'video/mp4', 'video/webm', 'video/quicktime',
    }
    if mimetype not in allowed_types:
        emit('upload_error', {'error': 'Unsupported file type.'})
        return

    # Decode and size check (50 MB)
    try:
        file_bytes = base64.b64decode(b64_data)
    except Exception:
        emit('upload_error', {'error': 'Invalid file data.'})
        return

    if len(file_bytes) > 50 * 1024 * 1024:
        emit('upload_error', {'error': 'File exceeds 50 MB limit.'})
        return

    # Save file
    ext      = mimetype.split('/')[-1].replace('quicktime', 'mov')
    filename = f'{uuid.uuid4().hex}.{ext}'
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    with open(filepath, 'wb') as f:
        f.write(file_bytes)

    timestamp = datetime.utcnow().strftime('%H:%M')

    if is_private:
        recipient_username = data.get('recipient')
        recipient = User.query.filter_by(username=recipient_username).first()
        if not recipient:
            os.remove(filepath)
            return

        media = MediaFile(
            filename     = filename,
            mimetype     = mimetype,
            sender_id    = me.id,
            recipient_id = recipient.id,
        )
        db.session.add(media)
        db.session.commit()

        payload = {
            'sender':     me.username,
            'media_token': media.token,
            'mimetype':   mimetype,
            'private':    True,
            'recipient':  recipient_username,
            'timestamp':  timestamp,
        }
        if recipient.sid:
            emit('new_media', payload, room=recipient.sid)
        emit('new_media', payload, room=request.sid)

    else:
        media = MediaFile(
            filename  = filename,
            mimetype  = mimetype,
            sender_id = me.id,
            room      = room,
        )
        db.session.add(media)
        db.session.commit()

        emit('new_media', {
            'sender':      me.username,
            'media_token': media.token,
            'mimetype':    mimetype,
            'private':     False,
            'timestamp':   timestamp,
        }, room=room)


@socketio.on('typing')
def handle_typing(data):
    user = User.query.get(session.get('user_id'))
    if user:
        emit('typing', {'username': user.username}, room=data.get('room'), include_self=False)


@socketio.on('stopped_typing')
def handle_stopped_typing(data):
    user = User.query.get(session.get('user_id'))
    if user:
        emit('stopped_typing', {'username': user.username}, room=data.get('room'), include_self=False)


# ─── Init ───────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    socketio.run(app, debug=debug)
