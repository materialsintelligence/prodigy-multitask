import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlparse, parse_qs, urlencode

import psutil
import requests
from flask import (
    Flask, render_template, make_response, request,
    redirect, url_for, abort, Response)
from flask_login import current_user
from flask_mongoengine import MongoEngine
from flask_security import MongoEngineUserDatastore, Security, UserMixin, RoleMixin, roles_required
from mongoengine import StringField, BooleanField, ListField, ReferenceField
from psutil import NoSuchProcess
from werkzeug.utils import secure_filename

import settings

app = Flask(__name__)
app.config['MONGODB_HOST'] = settings.MONGO_HOSTNAME
app.config['MONGODB_DB'] = settings.MONGO_DB
app.config['MONGODB_USERNAME'] = settings.MONGO_USERNAME
app.config['MONGODB_PASSWORD'] = settings.MONGO_PASSWORD
app.config['MONGODB_AUTHENTICATION_SOURCE'] = settings.MONGO_AUTHENTICATION_DB
app.config['SECRET_KEY'] = settings.SECURITY_KEY
app.config['SECURITY_PASSWORD_SALT'] = settings.SECURITY_PASSWORD_SALT
app.config['SECURITY_UNAUTHORIZED_VIEW'] = 'unauthorized'

db = MongoEngine(app)


class Role(db.Document, RoleMixin):
    name = StringField(max_length=80, unique=True)
    description = StringField(max_length=255)

    meta = {'indexes': ['name'], 'collection': 'prodigy_roles'}


class User(db.Document, UserMixin):
    email = StringField(max_length=255, unique=True, required=True)
    name = StringField(required=True)

    password = StringField(max_length=255)
    active = BooleanField(default=True)

    roles = ListField(ReferenceField(Role), default=[])

    meta = {'indexes': ['email'], 'collection': 'prodigy_users'}

    def add_role(self, role_name):
        for i in self.roles:
            if i.name == role_name:
                return

        role = Role.objects.get(name=role_name)
        self.roles.append(role)


user_database = MongoEngineUserDatastore(db, User, Role)
security = Security(app, user_database)

# Shared state variables
prodigy_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'prodigy_dir')
temp_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'temp_file_storage')
if not os.path.exists(prodigy_dir):
    os.mkdir(prodigy_dir)
if not os.path.exists(temp_dir):
    os.mkdir(temp_dir)
prodigy_services_lock = threading.Lock()


# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#             Prodigy PID control
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

def port_used(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', port))
        used = False
    except OSError:
        used = True
    s.close()
    return used


def get_next_available_port(start=8080):
    port = start
    while True:
        if port_used(port):
            port += 1
        else:
            break

    return port


def start_prodigy(working_dir, arguments=None):
    """Start a prodigy service at a new port"""

    port = get_next_available_port()
    work_dir = os.path.realpath(working_dir)

    # Write config
    with open(os.path.join(working_dir, 'prodigy.json'), 'w') as f:
        json.dump({
            "port": port,
            "host": "127.0.0.1",
        }, f)

    new_env = os.environ.copy()
    new_env['PRODIGY_HOME'] = work_dir
    script = os.path.realpath(os.path.join(os.path.dirname(__file__), 'prodigy_entrypoint.py'))
    process = subprocess.Popen(
        ['python', script, work_dir],
        shell=False,
        cwd=working_dir,
        env=new_env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    with open(os.path.join(work_dir, 'prodigy.pid'), 'w') as f:
        f.write(f'{process.pid}')

    return {
        'pid': process.pid,
        'process': process,
        'port': port,
        'work_dir': work_dir,
    }


def kill_pid_and_children(
        pid,
        sig=signal.SIGINT if sys.platform != 'win32' else signal.SIGTERM):
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    for process in parent.children():
        kill_pid_and_children(process.pid)
    parent.send_signal(sig)
    parent.wait()


def stop_prodigy(pid):
    kill_pid_and_children(pid)


def iter_prodigy_services():
    for i in os.listdir(prodigy_dir):
        work_dir = os.path.join(prodigy_dir, i)
        config_fn = os.path.join(work_dir, 'config.json')
        if not os.path.exists(config_fn):
            continue

        pid_fn = os.path.join(work_dir, 'prodigy.pid')

        alive = False
        listening = False
        pid = 0
        if os.path.exists(pid_fn):
            with open(pid_fn) as f:
                pid = int(f.read())
            try:
                process = psutil.Process(pid)
                if process.status() == psutil.STATUS_ZOMBIE:
                    process.wait()
                    raise NoSuchProcess('Zombie')
                alive = True
            except NoSuchProcess:
                os.unlink(pid_fn)

        if alive:
            try:
                with open(os.path.join(work_dir, 'prodigy.json')) as f:
                    port = int(json.load(f)['port'])
                    listening = port_used(port)
            except FileNotFoundError:
                pass

        yield i, work_dir, alive, listening, pid


def get_prodigy_pid(work_dir):
    pid_fn = os.path.join(work_dir, 'prodigy.pid')
    if os.path.exists(pid_fn):
        with open(pid_fn) as f:
            try:
                # Already started
                pid = int(f.read())
                psutil.Process(pid)
                return pid
            except NoSuchProcess:
                pass
    return None


def stop_all_prodigy(*_, **__):
    with prodigy_services_lock:
        for prodigy_id, _, alive, listening, pid in iter_prodigy_services():
            if alive:
                print('Stopping prodigy PID', pid)
                stop_prodigy(pid)


# Stop all services at exit
if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, stop_all_prodigy)
    signal.signal(signal.SIGABRT, stop_all_prodigy)
    signal.signal(signal.SIGINT, stop_all_prodigy)


def cleanup_temp_dir():
    with prodigy_services_lock:
        for i in os.listdir(temp_dir):
            filename = os.path.join(temp_dir, i)
            mtime = os.stat(filename).st_mtime
            if mtime < time.time() - 3600:
                # Remove files older than 1 hour
                os.unlink(filename)


# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#             Main Flask app
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

@security._state.unauthorized_handler
def unauthorized():
    return "You don't have permission to visit this page. " \
           "If you have questions, contact the manager of this site", 403


@app.route('/')
@roles_required('admin')
def list_services():
    with prodigy_services_lock:
        all_services = []

        for prodigy_id, work_dir, alive, listening, pid in iter_prodigy_services():
            config_filename = os.path.join(work_dir, 'config.json')
            with open(config_filename) as f:
                service_config = json.load(f)
                sharing = []
                for i in service_config.get('share', []):
                    try:
                        sharing.append({
                            'to': i['to'],
                            'id': i['id'],
                        })
                    except KeyError:
                        pass
                try:
                    all_services.append({
                        'id': prodigy_id,
                        'name': str(service_config['name']),
                        'arguments': str(service_config['arguments']),
                        'active': alive,
                        'listening': listening,
                        'sharing': sharing,
                    })
                except KeyError:
                    app.logger.warning(f'Config file {config_filename} corrupted.')

    return render_template(
        'services/list_services.html', all_services=all_services)


@app.route('/share/<service_id>/add', methods=['POST'])
@roles_required('admin')
def add_share(service_id):
    true_path = os.path.join(prodigy_dir, service_id)
    if not os.path.exists(true_path):
        abort(404)
        return

    share_to = request.form.get('sharewith')

    config_filename = os.path.join(true_path, 'config.json')
    with open(config_filename) as f:
        service_config = json.load(f)

    sharing = service_config.get('share', [])
    sharing.append({
        'to': share_to,
        'id': str(uuid.uuid1()),
    })
    service_config['share'] = sharing

    with open(config_filename, 'w') as f:
        json.dump(service_config, f)

    return redirect(url_for('list_services', viewsharing=service_id), code=302)


@app.route('/share/<service_id>/remove/<share_id>')
@roles_required('admin')
def remove_share(service_id, share_id):
    true_path = os.path.join(prodigy_dir, service_id)
    if not os.path.exists(true_path):
        abort(404)
        return

    config_filename = os.path.join(true_path, 'config.json')
    with open(config_filename) as f:
        service_config = json.load(f)

    new_sharing = []
    for i in service_config['share']:
        try:
            if i['id'] != share_id:
                new_sharing.append(i)
        except KeyError:
            pass
    service_config['share'] = new_sharing

    with open(config_filename, 'w') as f:
        json.dump(service_config, f)

    return redirect(url_for('list_services', viewsharing=service_id), code=302)


@app.route('/start/<service_id>')
@roles_required('admin')
def start_service(service_id):
    true_path = os.path.join(prodigy_dir, service_id)
    if not os.path.exists(true_path):
        abort(404)
        return

    with prodigy_services_lock:
        pid = get_prodigy_pid(true_path)
        if pid is not None:
            return redirect(url_for('list_services'))

    start_prodigy(true_path)

    return redirect(url_for('list_services'), code=302)


@app.route('/stop/<service_id>')
@roles_required('admin')
def stop_service(service_id):
    true_path = os.path.join(prodigy_dir, service_id)

    with prodigy_services_lock:
        pid = get_prodigy_pid(true_path)

    if pid is None:
        return abort(404)

    stop_prodigy(pid)
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass

    return redirect(url_for('list_services'), code=302)


@app.route('/remove/<service_id>')
@roles_required('admin')
def remove_service(service_id):
    true_path = os.path.join(prodigy_dir, service_id)

    with prodigy_services_lock:
        pid = get_prodigy_pid(true_path)
        if pid is not None:
            stop_prodigy(pid)

    if not os.path.exists(true_path):
        return abort(404)

    shutil.rmtree(true_path)

    return redirect(url_for('list_services'), code=302)


@app.route('/console/<service_id>')
@roles_required('admin')
def view_console(service_id):
    true_path = os.path.join(prodigy_dir, service_id)
    if not os.path.exists(true_path):
        return abort(404)

    try:
        with open(os.path.join(true_path, 'stdout.txt')) as f:
            stdout = f.read()
    except FileNotFoundError:
        stdout = ""
    except OSError:
        stdout = "Error, this Prodigy service did not write to stdout.txt"
    try:
        with open(os.path.join(true_path, 'stderr.txt')) as f:
            stderr = f.read()
    except FileNotFoundError:
        stderr = ""
    except OSError:
        stderr = "Error, this Prodigy service did not write to stdout.txt"

    return render_template("services/console_output.html",
                           prodigy_id=service_id,
                           stdout=stdout, stderr=stderr)


@app.route('/new_service')
@roles_required('admin')
def new_service_desc():
    random_id = str(uuid.uuid1())
    return render_template(
        'services/new_service.html',
        random_id=random_id)


@app.route('/new_service/<random_id>', methods=['POST'])
@roles_required('admin')
def create_new_service(random_id):
    form = request.form
    name = re.sub(r'[^a-zA-Z0-9_-]+', '', form.get('name', ''))
    if os.path.exists(os.path.join(prodigy_dir, name)):
        return 'The service with name "%s" already exists.' % name, 400

    arguments = form.get('arguments', '')
    files = list(map(secure_filename, form.getlist('files')))
    for i in files:
        if not os.path.exists(os.path.join(temp_dir, '%s--%s' % (random_id, i))):
            return 'File upload "%s" does not exist.' % name, 400

    new_service_dir = os.path.join(prodigy_dir, name)
    os.mkdir(new_service_dir)

    with open(os.path.join(new_service_dir, 'config.json'), 'w') as f:
        json.dump({
            'name': name,
            'arguments': arguments,
            'work_dir': new_service_dir
        }, f)
    for i in files:
        src = os.path.join(temp_dir, '%s--%s' % (random_id, i))
        os.rename(src, os.path.join(new_service_dir, i))

    try:
        cleanup_temp_dir()
    except OSError as e:
        app.logger.exception(f'Exception during cleaning up temp dir {e}')

    return redirect(url_for('list_services'), code=302)


@app.route('/upload_file/<random_id>', methods=['POST'])
@roles_required('admin')
def upload(random_id):
    # https://stackoverflow.com/questions/44727052/handling-large-file-uploads-with-flask
    file = request.files['file']

    save_path = os.path.join(temp_dir, random_id + '--' + secure_filename(file.filename))
    current_chunk = int(request.form['dzchunkindex'])

    # If the file already exists it's ok if we are appending to it,
    # but not if it's new file that would overwrite the existing one
    if os.path.exists(save_path) and current_chunk == 0:
        # 400 and 500s will tell dropzone that an error occurred and show an error
        return make_response(('File already exists', 400))

    try:
        with open(save_path, 'ab') as f:
            f.seek(int(request.form['dzchunkbyteoffset']))
            f.write(file.stream.read())
    except OSError:
        # log.exception will include the traceback so we can see what's wrong
        app.logger.exception('Could not write to file')
        return make_response(("Not sure why,"
                              " but we couldn't write the file to disk", 500))

    total_chunks = int(request.form['dztotalchunkcount'])

    if current_chunk + 1 == total_chunks:
        # This was the last chunk, the file should be complete and the size we expect
        if os.path.getsize(save_path) != int(request.form['dztotalfilesize']):
            app.logger.error(f"File {file.filename} was completed, "
                             f"but has a size mismatch."
                             f"Was {os.path.getsize(save_path)} but we"
                             f" expected {request.form['dztotalfilesize']} ")
            return make_response(('Size mismatch', 500))
        else:
            app.logger.info(f'File {file.filename} has been uploaded successfully')
    else:
        app.logger.debug(f'Chunk {current_chunk + 1} of {total_chunks} '
                         f'for file {file.filename} complete')

    return make_response(("Chunk upload successful", 200))


def _proxy_response(service_id, request_path, additional_query=None):
    true_path = os.path.join(prodigy_dir, service_id)
    with prodigy_services_lock:
        pid = get_prodigy_pid(true_path)
        if pid is None:
            return 'The page requested is not found', 404
        with open(os.path.join(true_path, 'prodigy.json')) as f:
            port = int(json.load(f)['port'])

    query = additional_query or {}
    query.update(parse_qs(request.query_string))
    new_qs = urlencode(query)
    query_ending = ''
    if new_qs:
        query_ending = '?' + new_qs
    if request_path.startswith('/'):
        request_path = request_path[1:]
    url = 'http://localhost:%d/%s%s' % (port, request_path, query_ending)

    app.logger.info(f'Forwarding request to prodigy instance {service_id}: {request.method} {url}')
    resp = requests.request(
        method=request.method,
        url=url,
        headers={key: value for (key, value) in request.headers if key != 'Host'},
        data=request.get_data(),
        cookies=request.cookies,
        allow_redirects=False)

    excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
    headers = [(name, value) for (name, value) in resp.raw.headers.items()
               if name.lower() not in excluded_headers]

    response = Response(resp.content, resp.status_code, headers)
    return response


def share_id_valid(prodigy_id, share_id):
    config_fn = os.path.join(prodigy_dir, prodigy_id, 'config.json')
    try:
        with open(config_fn) as f:
            config = json.load(f)
            return share_id in (x['id'] for x in config.get('share', []))
    except (KeyError, OSError):
        return False


@app.errorhandler(404)
def redirect_proxy(_):
    if 'referer' not in request.headers:
        return 'The page requested is not found', 404

    o = urlparse(request.headers.get('referer'))

    m = re.match(r'^/prodigy/([^/]+)/?$', o.path)
    if m:
        # This type of annotation requires auth
        if not current_user.has_role('admin'):
            return app.login_manager.unauthorized()

        service_id = m.group(1)
        path = urlparse(request.url).path
        return _proxy_response(service_id, path)

    m = re.match(r'^/prodigy/([^/]+)/share/([^/]+)/?$', o.path)
    if m:
        service_id = m.group(1)
        share_id = m.group(2)
        if share_id_valid(service_id, share_id):
            path = urlparse(request.url).path
            return _proxy_response(service_id, path)

    return 'The page requested is not found', 404


@app.route('/prodigy/<service_id>/',
           defaults={'path': ''},
           methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
@app.route('/prodigy/<service_id>/<path:path>',
           methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
@roles_required('admin')
def proxy_service(service_id, path):
    return _proxy_response(service_id, path)


@app.route('/prodigy/<service_id>/share/<share_id>/',
           defaults={'path': '/'},
           methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
@app.route('/prodigy/<service_id>/share/<share_id>/<path:path>',
           methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
def share_proxy_service(service_id, share_id, path):
    session_id = request.args.get('session', None)
    if session_id is None:
        return redirect(url_for(
            'share_proxy_service',
            service_id=service_id, share_id=share_id, path=path,
            session=share_id))

    if share_id_valid(service_id, share_id):
        return _proxy_response(service_id, path)

    return abort(404)
