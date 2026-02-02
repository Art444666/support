from flask import Flask, render_template, render_template_string, session, request, redirect, url_for
from flask_socketio import SocketIO, join_room, emit, send
import random

# ---------- Инициализация ----------
app = Flask(__name__, template_folder='')
app.secret_key = "anonchat"
socketio = SocketIO(app)

# ---------- Глобальное состояние ----------
rooms = {}         # room_name → {'owner': username, 'private': bool, 'password': str}
participants = {}  # room_name → set of usernames
bans = {}          # room_name → set of banned usernames
sid_to_name = {}   # sid → username

users = {}         # ip → nickname

ADMIN_PASS = "1234"
blacklist_ips = set()
global_block = False
block_reason = "Глобальная блокировка"

# ---------- Встроенный шаблон регистрации ----------
REGISTER_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Регистрация</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: Arial, sans-serif; background:#0b0f14; color:#e6edf3;
           display:flex; align-items:center; justify-content:center; height:100vh; }
    .card { background:#111720; padding:24px; border-radius:12px; max-width:420px; width:92%; }
    h1 { margin:0 0 12px; font-size:22px; }
    label { display:block; margin:12px 0 6px; color:#9aa4ad; }
    input[type=text]{ width:100%; padding:10px; border-radius:8px; border:1px solid #30363d; background:#0d1117; color:#e6edf3; }
    button{ margin-top:12px; width:100%; padding:10px; border-radius:8px; border:none; background:linear-gradient(135deg,#238636,#2ea043); color:#fff; font-weight:bold; cursor:pointer;}
    .ip { color:#3da9fc; font-weight:bold; }
    .err { color:#ff7b72; margin-top:8px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Регистрация</h1>
    <p>Ваш IP: <span class="ip">{{ ip }}</span></p>
    <form method="post" action="{{ url_for('register') }}">
      <label>Введите ник</label>
      <input type="text" name="nickname" maxlength="24" placeholder="Например: Artem" required />
      {% if error %}<div class="err">{{ error }}</div>{% endif %}
      <button type="submit">Продолжить</button>
    </form>
  </div>
</body>
</html>
"""

# ---------- Роуты ----------
@app.route('/')
def index():
    ip = request.remote_addr or '0.0.0.0'

    # Блокировка (глобальная или по IP)
    if global_block or ip in blacklist_ips:
        return render_template('block.html', company="AnonChat", ip=ip, reason=block_reason)

    # Админ уже в сессии → панель
    if session.get('is_admin'):
        return render_template('admin.html', username=session.get('username', 'Админ'))

    # Авторизация по IP
    if ip in users:
        username = users[ip]
        session['username'] = username
        session['room'] = None
        session['is_admin'] = (username == "Administrator")
        return render_template('chat.html', username=username, rooms=rooms)

    # Регистрация
    return render_template_string(REGISTER_TEMPLATE, ip=ip, error=None)

@app.route('/register', methods=['POST'])
def register():
    ip = request.remote_addr or '0.0.0.0'
    nickname = (request.form.get('nickname') or '').strip()

    # Валидация
    if len(nickname) < 2:
        return render_template_string(REGISTER_TEMPLATE, ip=ip, error="Ник слишком короткий.")
    if len(nickname) > 24:
        return render_template_string(REGISTER_TEMPLATE, ip=ip, error="Ник превышает 24 символа.")
    if nickname in users.values():
        return render_template_string(REGISTER_TEMPLATE, ip=ip, error="Ник уже используется.")

    # сохраняем ник
    users[ip] = nickname
    session['username'] = nickname
    session['room'] = None
    session['is_admin'] = (nickname == "Administrator")

    return redirect(url_for('index'))

@app.route('/admin')
def admin_panel():
    ip = request.remote_addr or '0.0.0.0'
    if not session.get('is_admin'):
        return render_template('block.html', company="AnonChat", ip=ip, reason="Нет прав администратора")
    return render_template('admin.html', username=session.get('username', 'Админ'))

# ---------- Socket.IO события ----------
@socketio.on('connect')
def on_connect():
    # Гарантируем ник и статус админа в сессии
    if not session.get('username'):
        ip = request.remote_addr or '0.0.0.0'
        nickname = users.get(ip, f"Гость#{random.randint(1000,9999)}")
        session['username'] = nickname
        session['room'] = None
        session['is_admin'] = (nickname == "Administrator")

    sid_to_name[request.sid] = session['username']
    # Список комнат сразу при подключении
    emit('room_list', format_room_list())

@socketio.on('disconnect')
def on_disconnect():
    sid_to_name.pop(request.sid, None)

@socketio.on('admin_login')
def admin_login(data):
    password = (data or {}).get('password', '')
    if password == ADMIN_PASS:
        session['is_admin'] = True
        emit('admin_success', '✅ Вход в режим админа выполнен.')
        emit('redirect_admin', '/admin', to=request.sid)
    else:
        emit('admin_error', '❌ Неверный пароль.')

# --- Админ: бан IP ---
@socketio.on('admin_ban')
def admin_ban(data):
    if not session.get('is_admin'):
        emit('admin_error', '⚠️ Нет прав администратора.')
        return

    target_ip = (data or {}).get('ip', '').strip()
    reason = (data or {}).get('reason', 'Нарушение правил')

    if not target_ip:
        emit('admin_error', '❌ Не указан IP.')
        return

    blacklist_ips.add(target_ip)
    emit('admin_success', f'⛔ IP {target_ip} добавлен в чёрный список.')

# --- Админ: глобальная блокировка сайта ---
@socketio.on('admin_global_block')
def admin_global_block_evt(data):
    if not session.get('is_admin'):
        emit('admin_error', '⚠️ Нет прав администратора.')
        return

    enabled = bool((data or {}).get('enabled', False))
    reason = (data or {}).get('reason', 'Глобальная блокировка')

    global global_block, block_reason
    global_block = enabled
    block_reason = reason

    emit('admin_success', '🌐 Глобальная блокировка включена.' if enabled else '🌐 Глобальная блокировка отключена.')

# --- Админ: удалить комнату ---
@socketio.on('admin_ban_room')
def admin_ban_room(data):
    if not session.get('is_admin'):
        emit('admin_error', '⚠️ Нет прав администратора.')
        return

    room = (data or {}).get('room', '').strip()
    if not room or room not in rooms:
        emit('admin_error', '❌ Комната не найдена.')
        return

    # удаляем комнату и связанные структуры
    participants.pop(room, None)
    bans.pop(room, None)
    rooms.pop(room, None)

    emit('admin_success', f'⛔ Комната "{room}" удалена администратором.', broadcast=True)
    emit('room_list', format_room_list(), broadcast=True)

# --- Админ: получить список всех пользователей с IP ---
@socketio.on('get_all_users')
def get_all_users():
    if not session.get('is_admin'):
        emit('admin_error', '⚠️ Нет прав администратора.')
        return
    data = [{"ip": ip, "nickname": nick} for ip, nick in users.items()]
    emit('all_users', data, to=request.sid)

# --- Пользовательские события: комнаты и сообщения ---
@socketio.on('create_room')
def create_room(data):
    room = (data or {}).get('room', '').strip()
    password = (data or {}).get('password', '').strip()
    username = session.get('username', 'Гость')

    if not room:
        emit('room_error', '❌ Укажите название комнаты.')
        return
    if room in rooms:
        emit('room_error', '❌ Комната уже существует.')
        return

    rooms[room] = {
        'owner': username,
        'private': bool(password),
        'password': password
    }
    participants[room] = set()
    bans[room] = set()

    emit('room_list', format_room_list(), broadcast=True)

@socketio.on('join_room')
def join_room_event(data):
    room = (data or {}).get('room', '').strip()
    password = (data or {}).get('password', '').strip()
    username = session.get('username', 'Гость')

    if room not in rooms:
        emit('room_error', '❌ Комната не найдена.')
        return

    info = rooms[room]
    if info['private'] and info['password'] != password:
        emit('room_error', '🔐 Неверный пароль.')
        return

    session['room'] = room
    join_room(room)
    participants[room].add(username)
    send(f"🚪 {username} вошёл в комнату {room}.", to=room)

    update_userlist(room)
    emit('room_joined', room)

@socketio.on('message')
def handle_message(msg):
    username = session.get('username', 'Гость')
    room = session.get('room')

    if not room:
        emit('room_error', '⚠️ Вы не в комнате.')
        return

    if username in bans.get(room, set()):
        send("⛔ Вы забанены в этой комнате.", to=request.sid)
        return

    # Команды владельца комнаты
    if isinstance(msg, str) and msg.startswith("/ban "):
        target = msg.split("/ban ", 1)[1].strip()
        if rooms.get(room, {}).get('owner') == username:
            bans[room].add(target)
            send(f"🔒 {target} забанен владельцем {username}.", to=room)
        else:
            send("⚠️ Только владелец может банить.", to=request.sid)

    elif isinstance(msg, str) and msg.startswith("/unban "):
        target = msg.split("/unban ", 1)[1].strip()
        if rooms.get(room, {}).get('owner') == username:
            bans[room].discard(target)
            send(f"🔓 {target} разбанен владельцем {username}.", to=room)
        else:
            send("⚠️ Только владелец может разбанивать.", to=request.sid)

    else:
        send(f"{username}: {msg}", to=room)

    update_userlist(room)

# ---------- Утилиты ----------
def update_userlist(room):
    users_in_room = list(participants.get(room, []))
    owner = rooms.get(room, {}).get('owner', '')
    emit('userlist', {'users': users_in_room, 'owner': owner}, to=room)

def format_room_list():
    return [
        f"{name} {'[приват]' if info.get('private') else ''}".strip()
        for name, info in rooms.items()
    ]

# ---------- Запуск ----------
if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=10000)











