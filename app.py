from flask import Flask, render_template, request, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
import secrets
import mistune
from flask_sqlalchemy import SQLAlchemy
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
# BUG FIX 1: Use a stable secret key from env, fallback to a fixed default.
# Previously token_hex(16) regenerated on every restart, invalidating all sessions.
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-please-change-in-prod')

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///site.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*")

markdown = mistune.create_markdown(plugins=['def_list', 'table'])

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sid = db.Column(db.String(120), unique=True, nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    status = db.Column(db.String(20), default='online')
    private_room = db.Column(db.String(80), nullable=True)

    def __repr__(self):
        return f'<User {self.username}>'

class ChatRoom(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)

    def __repr__(self):
        return f'<ChatRoom {self.name}>'

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey('chat_room.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    private = db.Column(db.Boolean, default=False)

    sender = db.relationship('User', backref=db.backref('messages', lazy=True))
    room = db.relationship('ChatRoom', backref=db.backref('messages', lazy=True))

    def __repr__(self):
        return f'<Message {self.content[:20]}>'

def broadcast_user_list():
    with app.app_context():
        all_users = User.query.all()
        user_list = {user.username: user.status for user in all_users}
        socketio.emit('user_list_update', user_list)

@app.route('/')
def login():
    return render_template('login.html')

@app.route('/chat')
def chat():
    username = request.args.get('username')
    if not username:
        return redirect(url_for('login'))
    return render_template('chat.html', username=username)

@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    user = User.query.filter_by(sid=sid).first()
    if user:
        username = user.username
        db.session.delete(user)
        db.session.commit()
        print(f"User {username} deleted from the database.")
        broadcast_user_list()
    else:
        print(f"User with SID {sid} not found in database.")
    print('Client disconnected')

@socketio.on('quit_chat')
def handle_quit_chat(data):
    username = data['username']
    user = User.query.filter_by(username=username).first()
    if user:
        db.session.delete(user)
        db.session.commit()
        print(f"User {username} quit the chat and was deleted.")
        broadcast_user_list()

@socketio.on('register')
def handle_register(data):
    username = data['username'].strip()
    sid = request.sid

    # BUG FIX: Validate username length
    if not username or len(username) > 30:
        emit('login_error', {'error': 'Username must be 1-30 characters.'}, room=sid)
        return

    existing_user = User.query.filter_by(username=username).first()
    if existing_user:
        emit('login_error', {'error': 'Username already taken. Try another.'}, room=sid)
        return

    # BUG FIX: Was creating and committing the user TWICE, then emitting login_success TWICE.
    new_user = User(username=username, sid=sid, status='online', private_room=None)
    db.session.add(new_user)
    db.session.commit()

    emit('login_success', {'username': username}, room=sid)
    broadcast_user_list()

@socketio.on('join_room')
def handle_join_room(data):
    username = data['username']
    room = data['room'].strip()

    if not room or len(room) > 50:
        emit('room_error', {'error': 'Room name must be 1-50 characters.'}, room=request.sid)
        return

    user = User.query.filter_by(username=username).first()
    join_room(room)
    display_name = user.username if user else username
    emit('new_message', {
        'sender': 'System',
        'message': f'{display_name} has joined the room.',
        'private': False
    }, room=room)

@socketio.on('send_message')
def handle_message(data):
    message = data['message'].strip()
    room = data['room']
    sid = request.sid

    if not message or len(message) > 2000:
        return

    user = User.query.filter_by(sid=sid).first()
    sender = user.username if user else "User"
    is_private = data.get('private', False)
    rendered = markdown(message)

    if is_private:
        recipient_username = data.get('recipient')
        # BUG FIX: Private messages were sent to room=recipient (a username string),
        # but SocketIO rooms are identified by SID, not username. Look up the SID.
        recipient_user = User.query.filter_by(username=recipient_username).first()
        if recipient_user and recipient_user.sid:
            emit('new_message', {'sender': sender, 'message': rendered, 'private': True}, room=recipient_user.sid)
        emit('new_message', {'sender': sender, 'message': rendered, 'private': True, 'recipient': recipient_username}, room=sid)
    else:
        emit('new_message', {'sender': sender, 'message': rendered, 'private': False}, room=room)

@socketio.on('typing')
def handle_typing(data):
    username = data['username']
    room = data['room']
    emit('typing', {'username': username}, room=room, include_self=False)

@socketio.on('stopped_typing')
def handle_stopped_typing(data):
    username = data['username']
    room = data['room']
    emit('stopped_typing', {'username': username}, room=room, include_self=False)

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    socketio.run(app, debug=True)
