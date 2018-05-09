#!/usr/bin/env python

import dataset
import json
import random
import time
import hashlib
import datetime
import os
import re
import dateutil.parser
import bleach

from base64 import b64decode
from functools import wraps

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlite3 import Connection as SQLite3Connection
from werkzeug.contrib.fixers import ProxyFix

from flask import Flask
from flask import jsonify
from flask import make_response
from flask import redirect
from flask import render_template
from flask import request
from flask import session
from flask import url_for
from flask import Response
from flask import abort

app = Flask(__name__, static_folder='static', static_url_path='')

db = None
lang = None
config = None

descAllowedTags = bleach.ALLOWED_TAGS + ['br', 'pre']

config_str = open('config.json', 'rb').read()
config = json.loads(config_str)

lang_str = open(config['language_file'], 'rb').read()
lang = json.loads(lang_str)

lang = lang[config['language']]

db = dataset.connect(config['db'])

if config['isProxied']:
    app.wsgi_app = ProxyFix(app.wsgi_app)

username_regex = re.compile(config['username_regex'])

app.secret_key = config['secret_key']

# Rip off from https://github.com/internetwache/tinyctf-platform
def is_valid_username(u):
    """Ensures that the username matches username_regex"""

    if (username_regex.match(u)):
        return True
    return False

def before_end(f):
    """Ensures that actions can only be done before the CTF is over"""

    # TODO: Fix redirect message
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_user()
        cur_time = datetime.datetime.now()
        config['stopTime'] = dateutil.parser.parse(str(config['stopTime']))
        if cur_time >= config['stopTime'] and user['isAdmin'] == False:
            return redirect('/error/game_over')
        return f(*args, **kwargs)
    return decorated_function

def after_start(f):
    """Ensures that actions can only be done after the CTF has started"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_user()
        cur_time = datetime.datetime.now()
        config['startTime'] = dateutil.parser.parse(str(config['stopTime']))
        if cur_time < config['startTime'] and user['isAdmin'] == False:
            return redirect('/error/not_started')
        return f(*args, **kwargs)
    return decorated_function

# TODO: CSRF checks only on user_id?
@app.before_request
def csrf_protect():
    """Checks CSRF token before every request unless csrf_enabled is false"""

    if not config['csrf_enabled']:
        return
    if request.method == "POST":
        token = session.pop('_csrf_token', None)
        if not token or token != request.form.get('_csrf_token'):
            abort(400)

def generate_random_token():
    """Generates a random CSRF token"""

    return hashlib.sha256(os.random(16)).hexdigest()

def generate_csrf_token():
    """Generates a CSRF token and saves it in the session variable"""

    if '_csrf_token' not in session:
        session['_csrf_token'] = generate_random_token()
    return session['_csrf_token']
###

def stop_scoreboard(f):
    """Turn off the scoreboard"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_user()
        cur_time = datetime.datetime.now()
        config['stopScoreboard'] = dateutil.parser.parse(str(config['stopScoreboard']))
        if cur_time >= config['stopScoreboard'] and user['isAdmin'] == False:
            return redirect('/error/stop_scoreboard')
        return f(*args, **kwargs)
    return decorated_function

def login_required(f):
    """Ensures that an user is logged in"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/error/not_started')
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Ensures that an user is logged in"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('error', msg='login_required'))
        user = get_user()
        if user["isAdmin"] == False:
            return redirect(url_for('error', msg='admin_required'))
        return f(*args, **kwargs)
    return decorated_function

def get_user():
    """Looks up the current user in the database"""

    login = 'user_id' in session
    if login:
        return db['users'].find_one(id=session['user_id'])

    return None

def get_task(tid):
    """Finds a task with a given category and score"""

    task = db.query("SELECT t.*, c.name cat_name FROM tasks t JOIN categories c on c.id = t.category WHERE t.id = :tid",
            tid=tid)

    return task.next()

def get_pwn_flags():
    """Returns the pwn flags of the current user"""

    pwn_flags = db.query('''select pf.service_id from pwn_flags pf
            where pf.user_id = :user_id''',
            user_id=session['user_id'])
    return [pf['service_id'] for pf in list(pwn_flags)]

def get_flags():
    """Returns the flags of the current user"""

    flags = db.query('''select f.task_id from flags f
        where f.user_id = :user_id''',
        user_id=session['user_id'])
    return [f['task_id'] for f in list(flags)]

def get_total_completion_count():
    """Returns dictionary where key is task id and value is the number of users who have submitted the flag"""

    c = db.query("select t.id, count(t.id) count from tasks t join flags f on t.id = f.task_id group by t.id;")

    res = {}
    for r in c:
        res.update({r['id']: r['count']})

    return res

@app.route('/error/<msg>')
def error(msg):
    """Displays an error message"""

    if msg in lang['error']:
        message = lang['error'][msg]
    else:
        message = lang['error']['unknown']

    user = get_user()

    # TODO: Fix
    render = render_template('frame.html', lang=lang, page='error.html',
        message=message, user=user)
    return make_response(render)

# TODO: Initializes with something else
def session_login(username):
    """Initializes the session with the current user's id"""
    user = db['users'].find_one(username=username)
    session['user_id'] = user['id']

@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """ Enforces sqlite foreign key constrains """
    if isinstance(dbapi_connection, SQLite3Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

@app.route('/login', methods = ['POST'])
def login():
    """Attempts to log the user in"""

    from werkzeug.security import check_password_hash

    username = request.form['user']
    password = request.form['password']

    user = db['users'].find_one(username=username)
    if user is None:
        return redirect('/error/invalid_credentials')

    if check_password_hash(user['password'], password):
        session_login(username)
        return redirect('/tasks')

    return redirect('/error/invalid_credentials')

@app.route('/register')
@before_end
def register():
    """Displays the register form"""

    # NOTE: Already wrapped this as a function
    # userCount = db['users'].count()
    # if datetime.datetime.today() < config['startTime'] and userCount != 0:
    #     return redirect('/error/not_started')

    # Render template
    render = render_template('frame.html', lang=lang,
        page='register.html', login=False)
    return make_response(render)

@app.route('/register/submit', methods = ['POST'])
def register_submit():
    """Attempts to register a new user"""

    from werkzeug.security import generate_password_hash

    # NOTE: Double the protection lol
    username = bleach.clean(request.form['user'], tags=[])
    email = bleach.clean(request.form['email'], tags=[])
    password = bleach.clean(request.form['password'], tags=[])

    if not is_valid_username(username):
        return redirect('/error/invalid_credentials')

    if not username:
        return redirect('/error/empty_user')

    user_found = db['users'].find_one(username=username)
    if user_found:
        return redirect('/error/already_registered')

    isAdmin = False
    isHidden = False
    userCount = db['users'].count()

    #if no users, make first user admin
    if userCount == 0:
        isAdmin = True
        isHidden = True
    elif datetime.datetime.today() < dateutil.parser.parse(config['startTime']):
        return redirect('/error/not_started')


    new_user = dict(username=username, email=email,
        password=generate_password_hash(password), isAdmin=isAdmin,
        isHidden=isHidden)
    db['users'].insert(new_user)

    # Set up the user id for this session
    session_login(username)

    return redirect('/tasks')



@app.route('/attack')
@login_required
@before_end
def attack():
    """Handles the submission of flags for Attack and Defense style"""

    user = get_user()
    userCount = db['users'].count(isHidden=0)
    isAdmin = user['isAdmin']

    flags = get_pwn_flags()

    render = render_template('frame.html', lang=lang, page='attack.html',
        user=user)
    return make_response(render)

@app.route('/tasks')
@login_required
@before_end
def tasks():
    """Displays all the tasks in a grid"""

    user = get_user()
    userCount = db['users'].count(isHidden=0)
    isAdmin = user['isAdmin']

    categories = db['categories']
    catCount = categories.count()

    flags = get_flags()

    tasks = db.query("SELECT * FROM tasks ORDER BY category, score")
    tasks = list(tasks)
    taskCompletedCount = get_total_completion_count()

    grid = []

    for cat in categories:
        cTasks = [x for x in tasks if x['category'] == cat['id']]
        gTasks = []

        gTasks.append(cat)
        for task in cTasks:
            tid = task['id']
            if tid in taskCompletedCount and userCount != 0:
                percentComplete = (float(taskCompletedCount[tid]) / userCount) * 100
            else:
                percentComplete = 0

            #hax for bad css (if 100, nothing will show)
            if percentComplete == 100:
                percentComplete = 99.99

            task['percentComplete'] = percentComplete

            task['isComplete'] = tid in flags
            gTasks.append(task)

        if isAdmin:
            gTasks.append({'add': True, 'category': cat['id']})

        grid.append(gTasks)

    # Render template
    render = render_template('frame.html', lang=lang, page='tasks.html',
        user=user, categories=categories, grid=grid)
    return make_response(render)

@app.route('/addcat/', methods=['GET'])
@admin_required
def addcat():
    user = get_user()
    render = render_template('frame.html', lang=lang, user=user, page='addcat.html')
    return make_response(render)

@app.route('/addcat/', methods=['POST'])
@admin_required
def addcatsubmit():
    try:
        name = bleach.clean(request.form['name'], tags=[])
    except KeyError:
        return redirect('/error/form')
    else:
        categories = db['categories']
        categories.insert(dict(name=name))
        return redirect('/tasks')

@app.route('/editcat/<id>/', methods=['GET'])
@admin_required
def editcat(id):
    user = get_user()
    category = db['categories'].find_one(id=id)
    render = render_template('frame.html', lang=lang, user=user, category=category, page='editcat.html')
    return make_response(render)

@app.route('/editcat/<catId>/', methods=['POST'])
@admin_required
def editcatsubmit(catId):
    try:
        name = bleach.clean(request.form['name'], tags=[])
    except KeyError:
        return redirect('/error/form')
    else:
        categories = db['categories']
        categories.update(dict(name=name, id=catId), ['id'])
        return redirect('/tasks')

@app.route('/editcat/<catId>/delete', methods=['GET'])
@admin_required
def deletecat(catId):
    category = db['categories'].find_one(id=catId)

    user = get_user()
    render = render_template('frame.html', lang=lang, user=user, page='deletecat.html', category=category)
    return make_response(render)

@app.route('/editcat/<catId>/delete', methods=['POST'])
@admin_required
def deletecatsubmit(catId):
    db['categories'].delete(id=catId)
    return redirect('/tasks')

@app.route('/addtask/<cat>/', methods=['GET'])
@admin_required
def addtask(cat):
    category = db['categories'].find_one(id=cat)

    user = get_user()

    render = render_template('frame.html', lang=lang, user=user,
            cat_name=category['name'], cat_id=category['id'], page='addtask.html')
    return make_response(render)

@app.route('/addtask/<cat>/', methods=['POST'])
@admin_required
def addtasksubmit(cat):
    try:
        name = bleach.clean(request.form['name'], tags=[])
        desc = bleach.clean(request.form['desc'], tags=descAllowedTags)
        category = int(request.form['category'])
        score = int(request.form['score'])
        flag = request.form['flag']
    except KeyError:
        return redirect('/error/form')

    else:
        tasks = db['tasks']
        task = dict(
                name=name,
                desc=desc,
                category=category,
                score=score,
                flag=flag)

        try:
            file = request.files['file']
        except:
            file = None

        if file:
            filename, ext = os.path.splitext(file.filename)
            #hash current time for file name
            filename = hashlib.md5(str(datetime.datetime.utcnow())).hexdigest()
            #if upload has extension, append to filename
            if ext:
                filename = filename + ext
            file.save(os.path.join("static/files/", filename))
            task["file"] = filename

        tasks.insert(task)
        return redirect('/tasks')

@app.route('/tasks/<tid>/edit', methods=['GET'])
@admin_required
def edittask(tid):
    user = get_user()

    task = db["tasks"].find_one(id=tid);
    category = db["categories"].find_one(id=task['category'])

    render = render_template('frame.html', lang=lang, user=user,
            cat_name=category['name'], cat_id=category['id'],
            page='edittask.html', task=task)
    return make_response(render)

@app.route('/tasks/<tid>/edit', methods=['POST'])
@admin_required
def edittasksubmit(tid):
    try:
        name = bleach.clean(request.form['name'], tags=[])
        desc = bleach.clean(request.form['desc'], tags=descAllowedTags)
        category = int(request.form['category'])
        score = int(request.form['score'])
        flag = request.form['flag']
    except KeyError:
        return redirect('/error/form')

    else:
        tasks = db['tasks']
        task = tasks.find_one(id=tid)
        task['id']=tid
        task['name']=name
        task['desc']=desc
        task['category']=category
        task['score']=score

        #only replace flag if value specified
        if flag:
            task['flag']=flag

        try:
            file = request.files['file']
        except:
            file = None

        if file:
            filename, ext = os.path.splitext(file.filename)
            #hash current time for file name
            filename = hashlib.md5(str(datetime.datetime.utcnow())).hexdigest()
            #if upload has extension, append to filename
            if ext:
                filename = filename + ext
            file.save(os.path.join("static/files/", filename))

            #remove old file
            if task['file']:
                os.remove(os.path.join("static/files/", task['file']))

            task["file"] = filename

        tasks.update(task, ['id'])
        return redirect('/tasks')

@app.route('/tasks/<tid>/delete', methods=['GET'])
@admin_required
def deletetask(tid):
    tasks = db['tasks']
    task = tasks.find_one(id=tid)

    user = get_user()
    render = render_template('frame.html', lang=lang, user=user, page='deletetask.html', task=task)
    return make_response(render)

@app.route('/tasks/<tid>/delete', methods=['POST'])
@admin_required
def deletetasksubmit(tid):
    db['tasks'].delete(id=tid)
    return redirect('/tasks')

@app.route('/tasks/<tid>/')
@login_required
@before_end
def task(tid):
    """Displays a task with a given category and score"""

    user = get_user()

    task = get_task(tid)
    if not task:
        return redirect('/error/task_not_found')

    flags = get_flags()
    task_done = task['id'] in flags

    solutions = db['flags'].find(task_id=task['id'])
    solutions = len(list(solutions))

    # Render template
    render = render_template('frame.html', lang=lang, page='task.html',
        task_done=task_done, login=login, solutions=solutions,
        user=user, category=task["cat_name"], task=task, score=task["score"])
    return make_response(render)

@app.route('/submit/<tid>/<flag>')
@login_required
@before_end
def submit(tid, flag):
    """Handles the submission of flags"""

    user = get_user()

    task = get_task(tid)
    flags = get_flags()
    task_done = task['id'] in flags

    result = {'success': False}
    if not task_done and task['flag'] == b64decode(flag):

        timestamp = int(time.time() * 1000)
        ip = request.remote_addr
        print "flag submitter ip: {}".format(ip)

        # Insert flag
        new_flag = dict(task_id=task['id'], user_id=session['user_id'],
            score=task["score"], timestamp=timestamp, ip=ip)
        db['flags'].insert(new_flag)

        result['success'] = True

    return jsonify(result)

@app.route('/scoreboard')
@login_required
@stop_scoreboard
def scoreboard():
    """Displays the scoreboard"""

    user = get_user()
    scores = db.query('''select u.username, ifnull(sum(f.score), 0) as score,
        max(timestamp) as last_submit from users u left join flags f
        on u.id = f.user_id where u.isHidden = 0 group by u.username
        order by score desc, last_submit asc''')

    scores = list(scores)

    # Render template
    render = render_template('frame.html', lang=lang, page='scoreboard.html',
        user=user, scores=scores)
    return make_response(render)

@app.route('/scoreboard.json')
@stop_scoreboard
def scoreboard_json():
    scores = db.query('''select u.username, ifnull(sum(f.score), 0) as score,
        max(timestamp) as last_submit from users u left join flags f
        on u.id = f.user_id where u.isHidden = 0 group by u.username
        order by score desc, last_submit asc''')

    scores = list(scores)

    return Response(json.dumps(scores), mimetype='application/json')

@app.route('/about')
@login_required
def about():
    """Displays the about menu"""

    user = get_user()

    # Render template
    render = render_template('frame.html', lang=lang, page='about.html',
        user=user)
    return make_response(render)

@app.route('/settings')
@login_required
def settings():
    user = get_user()
    render = render_template('frame.html', lang=lang, page='settings.html',
        user=user)
    return make_response(render)

@app.route('/settings', methods = ['POST'])
@login_required
def settings_submit():
    from werkzeug.security import check_password_hash
    from werkzeug.security import generate_password_hash

    user = get_user()
    try:
        old_pw = request.form['old_pw']
        new_pw = request.form['new_pw']
        email = request.form['email']
    except KeyError as e:
        return redirect('/error/form')

    if old_pw and check_password_hash(user['password'], old_pw):
        if new_pw:
            user['password'] = generate_password_hash(new_pw)
        if email:
            user['email'] = email
    else:
        return redirect('/error/invalid_password')

    db["users"].update(user, ['id'])
    return redirect('/tasks')

@app.route('/logout')
@login_required
def logout():
    """Logs the current user out"""

    del session['user_id']
    return redirect('/')

@app.route('/')
def index():
    """Displays the main page"""

    user = get_user()

    # Render template
    render = render_template('frame.html', lang=lang,
        page='main.html', user=user)
    return make_response(render)

if __name__ == '__main__':
    app.jinja_env.globals['csrf_token'] = generate_csrf_token
    app.run(host=config['host'], port=config['port'],
        debug=config['debug'], threaded=False)