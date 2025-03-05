from flask import Flask, render_template, request, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
import secrets
import mistune  # Import the mistune library
from flask_sqlalchemy import SQLAlchemy  # Import Flask-SQLAlchemy
import os
from datetime import datetime
from dotenv import load_dotenv  # Import load_dotenv

load_dotenv()  # Load environment variables from .env

app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(16)

# Database Configuration
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///site.db')  # SQLite database
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False  # Disable tracking modifications

db = SQLAlchemy(app)  # Initialize SQLAlchemy

socketio = SocketIO(app, cors_allowed_origins="*")

# In-memory storage for active users (REMOVE THIS)
users = {}
room_users = {}

# Initialize Mistune Markdown parser
markdown = mistune.create_markdown(plugins=['def_list', 'table'])

# Define database models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    status = db.Column(db.String(20), default='online')  # Add status field
    private_room = db.Column(db.String(80), nullable=True)  # Store private room ID

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
    private = db.Column(db.Boolean, default=False) # Check if is private
    # private_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) # Which user to be private

    sender = db.relationship('User', backref=db.backref('messages', lazy=True))
    room = db.relationship('ChatRoom', backref=db.backref('messages', lazy=True))


    def __repr__(self):
        return f'<Message {self.content[:20]}>'

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
    user = User.query.filter_by(id=sid).first()

    if user:
        username = user.username
        # Delete the user from the database
        db.session.delete(user)
        db.session.commit()
        print(f"User {username} deleted from the database.")
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
    else:
        print(f"User {username} not found.")


@socketio.on('register')
def handle_register(data):
    username = data['username']
    sid = request.sid

    # Check if the user already exists
    existing_user = User.query.filter_by(username=username).first()
    if existing_user:
        # Handle the case where the user already exists
        print(f"User {username} already exists.")
        # You might want to emit an error message to the client here
        return

    # Create a new user object
    new_user = User(username=username, status='online', private_room=None)

    # Add the user to the database
    db.session.add(new_user)
    db.session.commit()

    # Associate session ID with the new user
    #users[sid] = {'username': username, 'status': 'online', 'private_room': None}

    emit('login_success', {'username': username}, room=sid)

@socketio.on('join_room')
def handle_join_room(data):
    username = data['username']
    room = data['room']
    sid = request.sid

    # Retrieve User
    user = User.query.filter_by(username=username).first()

    join_room(room)

    if user:
        emit('new_message', {'sender': 'System', 'message': f'{user.username} has joined the room.', 'private': False}, room=room)
    else:
        emit('new_message', {'sender': 'System', 'message': f'{username} has joined the room.', 'private': False}, room=room)

@socketio.on('send_message')
def handle_message(data):
    message = data['message']
    room = data['room']
    sid = request.sid

    # Retrieve user from the database using the session ID
    user = User.query.filter_by(id=sid).first()
    if user:
        sender = user.username  # Get username or default
    else:
        sender = "User"
        
    is_private = data.get('private', False)

    # Parse the message to html text
    message = markdown(message)

    if is_private:
        recipient = data['recipient']
        emit('new_message', {'sender': sender, 'message': message, 'private': True}, room=recipient)
        emit('new_message', {'sender': sender, 'message': message, 'private': True}, room=sid)
    else:
        emit('new_message', {'sender': sender, 'message': message, 'private': False}, room=room)


@socketio.on('typing')
def handle_typing(data):
    username = data['username']
    room = data['room']
    emit('typing', {'username': username}, room=room, include_self=False)  # Broadcast

@socketio.on('stopped_typing')
def handle_stopped_typing(data):
    username = data['username']
    room = data['room']
    emit('stopped_typing', {'username': username}, room=room, include_self=False)  # Broadcast

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0')