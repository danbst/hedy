import sys
if (sys.version_info.major < 3 or sys.version_info.minor < 6):
    print ('Hedy requires Python 3.6 or newer to run. However, your version of Python is', '.'.join ([str (sys.version_info.major), str (sys.version_info.minor), str (sys.version_info.micro)]))
    quit ()

# coding=utf-8
import datetime
import collections
import hedy
import json
import logging
import os
import csv
from Levenshtein import distance as lev
from os import path
import re
import traceback
import uuid
from ruamel import yaml
from flask_commonmark import Commonmark
from werkzeug.urls import url_encode
from config import config
from website.auth import auth_templates, current_user, requires_login, is_admin, is_teacher
from utils import db_init, db_get, db_get_many, db_create, db_update, is_debug_mode, timems, type_check, object_check, db_del, load_yaml, load_yaml_rt, dump_yaml_rt, version
import utils

# app.py
from flask import Flask, request, jsonify, session, abort, g, redirect, Response, make_response
from flask_helpers import render_template
from flask_compress import Compress

# Hedy-specific modules
import courses
import hedyweb
from website import querylog, aws_helpers, jsonbin, translating, ab_proxying, cdn

# Set the current directory to the root Hedy folder
os.chdir(os.path.join (os.getcwd (), __file__.replace (os.path.basename (__file__), '')))

# Define and load all available language data
ALL_LANGUAGES = {
    'en': 'English',
    'nl': 'Nederlands',
    'es': 'Español',
    'fr': 'Français',
    'pt_br': 'Português',
    'de': 'Deutsch',
    'it': 'Italiano',
    'sw': 'Swahili',
    'hu': 'Magyar',
    'el': 'Ελληνικά',
    "zh": "简体中文"
}

LEVEL_DEFAULTS = collections.defaultdict(courses.NoSuchDefaults)
for lang in ALL_LANGUAGES.keys():
    LEVEL_DEFAULTS[lang] = courses.LevelDefaults(lang)

HEDY_COURSE = collections.defaultdict(courses.NoSuchCourse)
for lang in ALL_LANGUAGES.keys():
    HEDY_COURSE[lang] = courses.Course('hedy', lang, LEVEL_DEFAULTS[lang])

SPACE_EU_COURSE = {'nl': courses.Course('space_eu', 'nl', LEVEL_DEFAULTS['nl']),
                   'en': courses.Course('space_eu', 'en', LEVEL_DEFAULTS['en']),
                   'es': courses.Course('space_eu', 'es', LEVEL_DEFAULTS['es'])
                   }

ONLINE_MASTERS_COURSE = courses.Course('online_masters', 'nl', LEVEL_DEFAULTS['nl'])

TRANSLATIONS = hedyweb.Translations()

def load_adventures_in_all_languages():
    adventures = {}
    for lang in ALL_LANGUAGES.keys ():
        adventures[lang] = load_yaml(f'coursedata/adventures/{lang}.yaml')
    return adventures


def load_adventure_for_language(lang):
    adventures = load_adventures_in_all_languages()
    if not lang in adventures or len (adventures [lang]) == 0:
        return adventures ['en']
    return adventures [lang]


def load_adventure_assignments_per_level(lang, level):

    loaded_programs = {}
    # If user is logged in, we iterate their programs that belong to the current level. Out of these, we keep the latest created program for both the level mode (no adventure) and for each of the adventures.
    if current_user (request) ['username']:
        user_programs = db_get_many ('programs', {'username': current_user (request) ['username']}, True)
        for program in user_programs:
            if program ['level'] != level:
                continue
            program_key = 'level' if not program.get ('adventure_name') else program ['adventure_name']
            if not program_key in loaded_programs:
                loaded_programs [program_key] = program
            elif loaded_programs [program_key] ['date'] < program ['date']:
                loaded_programs [program_key] = program

    assignments = []
    adventures = load_adventure_for_language(lang)['adventures']
    for short_name, adventure in adventures.items ():
        if not level in adventure['levels']:
            continue
        assignments.append({
            'short_name': short_name,
            'name': adventure['name'],
            'image': adventure.get('image', None),
            'default_save_name': adventure['default_save_name'],
            'text': adventure['levels'][level].get('story_text', 'No Story Text'),
            'start_code': adventure['levels'][level].get ('start_code', ''),
            'loaded_program': '' if not loaded_programs.get (short_name) else {
                'name': loaded_programs.get (short_name) ['name'],
                'code': loaded_programs.get (short_name) ['code']
             }
        })
    # We create a 'level' pseudo assignment to store the loaded program for level mode, if any.
    assignments.append({
        'short_name': 'level',
        'loaded_program': '' if not loaded_programs.get ('level') else {
            'name': loaded_programs.get ('level') ['name'],
            'code': loaded_programs.get ('level') ['code']
         }
    })
    return assignments

# Load main menu (do it once, can be cached)
with open(f'main/menu.json', 'r', encoding='utf-8') as f:
    main_menu_json = json.load(f)

logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)-8s: %(message)s')


app = Flask(__name__, static_url_path='')
csv.field_size_limit(sys.maxsize)
# Ignore trailing slashes in URLs
app.url_map.strict_slashes = False

cdn.Cdn(app, os.getenv('CDN_PREFIX'), os.getenv('HEROKU_SLUG_COMMIT', 'dev'))

# Set session id if not already set. This must be done as one of the first things,
# so the function should be defined high up.
@app.before_request
def set_session_cookie():
    session_id()

if os.getenv('IS_PRODUCTION'):
    @app.before_request
    def reject_e2e_requests():
        if utils.is_testing_request (request):
            return 'No E2E tests are allowed in production', 400

@app.before_request
def before_request_proxy_testing():
    if utils.is_testing_request (request):
        if os.getenv ('IS_TEST_ENV'):
            session ['test_session'] = 'test'

# HTTP -> HTTPS redirect
# https://stackoverflow.com/questions/32237379/python-flask-redirect-to-https-from-http/32238093
if os.getenv ('REDIRECT_HTTP_TO_HTTPS'):
    @app.before_request
    def before_request_https():
        if request.url.startswith('http://'):
            url = request.url.replace('http://', 'https://', 1)
            # We use a 302 in case we need to revert the redirect.
            return redirect(url, code=302)

# Unique random key for sessions.
# For settings with multiple workers, an environment variable is required, otherwise cookies will be constantly removed and re-set by different workers.
if utils.is_production():
    if not os.getenv ('SECRET_KEY'):
        raise RuntimeError('The SECRET KEY must be provided for non-dev environments.')

    app.config['SECRET_KEY'] = os.getenv ('SECRET_KEY')

else:
    app.config['SECRET_KEY'] = os.getenv ('SECRET_KEY', uuid.uuid4().hex)

if utils.is_heroku():
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
    )

# Set security attributes for cookies in a central place - but not when running locally, so that session cookies work well without HTTPS

Compress(app)
Commonmark(app)
parse_logger = jsonbin.MultiParseLogger(
    jsonbin.JsonBinLogger.from_env_vars(),
    jsonbin.S3ParseLogger.from_env_vars())
querylog.LOG_QUEUE.set_transmitter(aws_helpers.s3_querylog_transmitter_from_env())

# Check that requested language is supported, otherwise return 404
@app.before_request
def check_language():
    if requested_lang() not in ALL_LANGUAGES.keys ():
        return "Language " + requested_lang () + " not supported", 404

if utils.is_heroku() and not os.getenv('HEROKU_RELEASE_CREATED_AT'):
    logging.warning('Cannot determine release; enable Dyno metadata by running "heroku labs:enable runtime-dyno-metadata -a <APP_NAME>"')


@app.before_request
def before_request_begin_logging():
    querylog.begin_global_log_record(path=request.path, method=request.method)

@app.after_request
def after_request_log_status(response):
    querylog.log_value(http_code=response.status_code)
    return response

@app.after_request
def set_security_headers(response):
    security_headers = {
        'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
        'X-Frame-Options': 'DENY',
        'X-XSS-Protection': '1; mode=block',
    }
    response.headers.update(security_headers)
    return response

@app.teardown_request
def teardown_request_finish_logging(exc):
    querylog.finish_global_log_record(exc)

# If present, PROXY_TO_TEST_HOST should be the 'http[s]://hostname[:port]' of the target environment
if os.getenv ('PROXY_TO_TEST_HOST') and not os.getenv ('IS_TEST_ENV'):
    ab_proxying.ABProxying(app, os.getenv ('PROXY_TO_TEST_HOST'), app.config['SECRET_KEY'])

@app.route('/session_test', methods=['GET'])
def echo_session_vars_test():
    if not utils.is_testing_request (request):
        return 'This endpoint is only meant for E2E tests', 400
    return jsonify({'session': dict(session)})

@app.route('/session_main', methods=['GET'])
def echo_session_vars_main():
    if not utils.is_testing_request (request):
        return 'This endpoint is only meant for E2E tests', 400
    return jsonify({'session': dict(session), 'proxy_enabled': bool (os.getenv ('PROXY_TO_TEST_HOST'))})

@app.route('/parse', methods=['POST'])
def parse():
    body = request.json
    if not body:
        return "body must be an object", 400
    if 'code' not in body:
        return "body.code must be a string", 400
    if 'level' not in body:
        return "body.level must be a string", 400
    if 'sublevel' in body and not type_check (body ['sublevel'], 'int'):
        return "If present, body.sublevel must be an integer", 400
    if 'adventure_name' in body and not type_check (body ['adventure_name'], 'str'):
        return "if present, body.adventure_name must be a string", 400

    code = body ['code']
    level = int(body ['level'])
    sublevel = body.get ('sublevel') or 0

    # Language should come principally from the request body,
    # but we'll fall back to browser default if it's missing for whatever
    # reason.
    lang = body.get('lang', requested_lang())
    session['lang'] = lang
    querylog.log_value(level=level, lang=lang)

    response = {}
    username = current_user(request) ['username'] or None

    querylog.log_value(level=level, lang=lang, session_id=session_id(), username=username)

    if not 'error_level' in session:
        initialize_gfi_session(lang)

    # Check if user sent code
    if not code:
        response["Error"] = "no code found, please send code."
    # is so, parse
    else:
        try:
            hedy_errors = TRANSLATIONS.get_translations(lang, 'HedyErrorMessages')
            gradual_feedback = TRANSLATIONS.get_translations(lang, 'GradualFeedback')
            with querylog.log_time('transpile'):
                result = hedy.transpile(code, level,sublevel)
            response["Code"] = "# coding=utf8\nimport random\n" + result
            if gfi_support(lang):
                if not (('error_level' in session) or ('similar_code' in session)):
                    return 'session cookie must have error_level & similar_code set', 400
                response['prev_feedback_level'] = session['error_level']
                response['prev_similar_code'] = session['similar_code']
                session ['error_level'] = 0  # Code is correct: reset error_level back to 0

        except hedy.HedyException as E:
            traceback.print_exc()
            # some 'errors' can be fixed, for these we throw an exception, but also
            # return fixed code, so it can be ran
            if E.args[0] == "Invalid Space":
                error_template = hedy_errors[E.error_code]
                response["Code"] = "# coding=utf8\n" + E.arguments['fixed_code']
                response["Warning"] = error_template.format(**E.arguments)
            elif E.args[0] == "Parse":
                error_template = hedy_errors[E.error_code]
                # Localize the names of characters. If we can't do that, just show the original
                # character.
                if 'character_found' in E.arguments.keys():
                    E.arguments['character_found'] = hedy_errors.get(E.arguments['character_found'], E.arguments['character_found'])
                elif 'keyword_found' in E.arguments.keys():
                    #if we find an invalid keyword, place it in the same location in the error message but without translating
                    E.arguments['character_found'] = E.arguments['keyword_found']

                response["Error"] = error_template.format(**E.arguments)
            elif E.args[0] == "Unquoted Text":
                error_template = hedy_errors[E.error_code]
                response["Error"] = error_template.format(**E.arguments)
            else:
                error_template = hedy_errors[E.error_code]
                response["Error"] = error_template.format(**E.arguments)
            if gfi_support(lang):
                response.update(gradual_feedback_model(code, level, gradual_feedback, lang, E, hedy_exception=True))

        except Exception as E:
            traceback.print_exc()
            print(f"error transpiling {code}")
            response["Error"] = str(E)
            if gfi_support(lang):
                response.update(gradual_feedback_model(code, level, gradual_feedback, lang, E, hedy_exception=False))
        if gfi_support(lang):
            session['code'] = code
            session['hedy_level'] = level

    querylog.log_value(server_error=response.get('Error'))
    parse_logger.log ({
        'session': session_id(),
        'date': str(datetime.datetime.now()),
        'level': level,
        'lang': lang,
        'code': code,
        'server_error': response.get('Error'),
        'version': version(),
        'username': username,
        'feedback_level': session['error_level'] if gfi_support (lang) else None,
        'GFM': True if gfi_support (lang) else False,
        'is_test': 1 if os.getenv('IS_TEST_ENV') else None,
        'adventure_name': body.get('adventure_name', None)
    })

    return jsonify(response)


def gradual_feedback_model(code, level, gradual_feedback, language, E, hedy_exception):
    response = {}
    response['prev_feedback_level'] = session['error_level']
    response['prev_similar_code'] = session['similar_code']
    response['GFM'] = True

    if session['code'] == code:
        response["Feedback"] = gradual_feedback["Identical_code"]  # Don't raise the feedback level!
        response["Duplicate"] = True
    else:
        response["Duplicate"] = False
        if session['error_level'] < 5:  # Raise feedback level if is it not 5 (yet)
            session ['error_level'] = session['error_level'] + 1
        if session['error_level'] == 2:  # Give a more explanatory error message
            if hedy_exception:
                print("The current error code is: ")
                print(E.error_code)
                response["Feedback"] = gradual_feedback["Expanded_" + E.error_code]
            else:
                response["Feedback"] = gradual_feedback["Expanded_Unknown"]
        elif session['error_level'] == 3:  # Give a reminder what is new in this specific level
            similar_code = get_similar_code(preprocess_code_similarity_measure(code, level), language, level)
            if similar_code is None:  # No similar code is found against a, to be defined, threshold
                response["Feedback"] = gradual_feedback["No_similar_code"]
            else:
                response["Feedback"] = similar_code
                session ['similar_code'] = similar_code
        elif session['error_level'] == 4:
            try:
                response["Feedback"] = gradual_feedback["New_level" + str(level)]
            except:
                response["Feedback"] = gradual_feedback["Expanded_Uknown"]
        elif session['error_level'] == 5:
            response["Feedback"] = gradual_feedback["Break"]  # Suggest a break
    response["feedback_level"] = session['error_level']
    return response


@app.route('/feedback', methods=['POST'])
def log_feedback():
    body = request.json
    general_answer = body['general_answer']
    level_answer = body['level_answers']
    feedback_level = int(body['feedback_level'])
    collapse = body['collapse']  # this is either true or false: The window was either expanded or not
    similar_code = body['similar_code']
    parse_logger.log({
        'session': session_id(),
        'date': str(datetime.datetime.now()),
        'is_test': 1 if os.getenv('IS_TEST_ENV') else None,
        'level': session['hedy_level'],
        'feedback_level': feedback_level,
        'usefulness': general_answer,
        'level_usefulness': level_answer,
        'similar_code': similar_code,
        'collapse': collapse,
        'GFM': True if gfi_support(session['lang']) else False
    })
    debug = True
    if debug:
        print("TESTING FEEDBACK LOG INFORMATION")
        print("Current level: " + str(session['hedy_level']))
        print("Model was useful: " + str(general_answer))
        print("Specific level was useful: " + str(level_answer))
        print("Feedback level of fix: " + str(feedback_level))
        print("Window was opened: " + str(collapse))
        print("Possible similar code: " + str(similar_code))

    # https://stackoverflow.com/questions/26079754/flask-how-to-return-a-success-status-code-for-ajax-call
    # We have to return something to the AJAX POST to show we are okay
    return json.dumps({'success': True}), 200, {'ContentType': 'application/json'}


def get_concepts(level):
    if level == 1:
        return ['print', 'ask', 'echo']
    elif level == 2:
        return ['print', 'ask', 'at', 'random']
    elif level == 3:
        return ['print', 'is', 'ask', 'at', 'random']
    elif level == 4:
        return ['print', 'is', 'ask', 'at', 'random', 'if', 'else']
    elif level == 5:
        return ['print', 'is', 'ask', 'at', 'random', 'if', 'else', 'repeat', 'times']
    elif level in [6, 7]:
        return ['print', 'is', 'ask', 'at', 'random', 'if', 'else', 'repeat', 'times', '+', '-', '*']
    elif level in [8, 9, 10]:
        return ['print', 'is', 'ask', 'at', 'random', 'if', 'else', 'for', 'in', 'range', '+', '-', '*']
    return []


def preprocess_code_similarity_measure(code, level):
    print(code)
    concepts = get_concepts(int(level))
    words = code.split()
    code = ""
    for word in words:
        if word not in concepts:
            code += re.sub(r"[a-z|A-Z|0-9|!?,''{}]", "%", word)
            code += " "
        else:
            code += word + " "
    code = code.split()
    temp = ""
    for processed in code:
        if "%" in processed:
            temp += "% "
        else:
            temp += processed + " "
    code = temp
    print(code)
    return code


def get_similar_code(processed_code, language, level):
    filename = "coursedata/similar-code-files/" + language + "/level" + str(level) + ".csv"
    shortest_distance = 75  # This is the threshold: when differ more than this value it's no longer similar code
    similar_code = None
    try:
        with open(filename, mode='r', encoding='utf-8') as file:
            csvFile = csv.reader(file, quoting=csv.QUOTE_MINIMAL)
            for lines in csvFile:
                distance = lev(processed_code, lines[1])
                if distance < 1:  # The code is identical, no need to search any further
                    similar_code = lines[0]
                    break
                else:
                    if distance < shortest_distance:
                        shortest_distance = distance
                        similar_code = lines[0]
        similar_code = similar_code.replace("    ", "\t")
    except:
        similar_code = None
    return similar_code

def gfi_support(language):
    return language in ["en", "nl"]

def initialize_gfi_session(language):
    if gfi_support(language):
        session ['error_level'] = 0 # When requesting a new level, always reset error_level to 0
        session ['similar_code'] = "-" # Make sure that the gathered similar code is also deleted when re-loading the page
        session ['code'] = None # Make sure that no code is stored in the session when re-loading the page

@app.route('/report_error', methods=['POST'])
def report_error():
    post_body = request.json

    parse_logger.log ({
        'session': session_id(),
        'date': str(datetime.datetime.now()),
        'level': post_body.get('level'),
        'code': post_body.get('code'),
        'client_error': post_body.get('client_error'),
        'version': version(),
        'username': current_user(request) ['username'] or None,
        'is_test': 1 if os.getenv ('IS_TEST_ENV') else None
    })

    return 'logged'

@app.route('/client_exception', methods=['POST'])
def report_client_exception():
    post_body = request.json

    querylog.log_value(
        session=session_id(),
        date=str(datetime.datetime.now()),
        client_error=post_body,
        version=version(),
        username=current_user(request) ['username'] or None,
        is_test=1 if os.getenv ('IS_TEST_ENV') else None
    )

    return 'logged', 500

@app.route('/version', methods=['GET'])
def version_page():
    """
    Generate a page with some diagnostic information and a useful GitHub URL on upcoming changes.

    This is an admin-only page, it does not need to be linked.
    (Also does not have any sensitive information so it's fine to be unauthenticated).
    """
    app_name = os.getenv('HEROKU_APP_NAME')

    vrz = os.getenv('HEROKU_RELEASE_CREATED_AT')
    the_date = datetime.date.fromisoformat(vrz[:10]) if vrz else datetime.date.today()

    commit = os.getenv('HEROKU_SLUG_COMMIT', '????')[0:6]

    return render_template('version-page.html',
        app_name=app_name,
        heroku_release_time=the_date,
        commit=commit)

def programs_page (request):
    username = current_user(request) ['username']
    if not username:
        # redirect users to /login if they are not logged in
        url = request.url.replace('/programs', '/login')
        return redirect(url, code=302)

    from_user = request.args.get('user') or None
    if from_user and not is_admin (request):
        return "unauthorized", 403

    texts=TRANSLATIONS.data [requested_lang ()] ['Programs']
    ui=TRANSLATIONS.data [requested_lang ()] ['ui']
    adventures = load_adventure_for_language(requested_lang ())['adventures']

    result = db_get_many ('programs', {'username': from_user or username}, True)
    programs = []
    now = timems ()
    for item in result:
        measure = texts ['minutes']
        date = round ((now - item ['date']) / 60000)
        if date > 90:
            measure = texts ['hours']
            date = round (date / 60)
        if date > 36:
            measure = texts ['days']

            date = round (date / 24)

        programs.append ({'id': item ['id'], 'code': item ['code'], 'date': texts ['ago-1'] + ' ' + str (date) + ' ' + measure + ' ' + texts ['ago-2'], 'level': item ['level'], 'name': item ['name'], 'adventure_name': item.get ('adventure_name'), 'public': item.get ('public')})

    return render_template('programs.html', lang=requested_lang(), menu=render_main_menu('programs'), texts=texts, ui=ui, auth=TRANSLATIONS.data [requested_lang ()] ['Auth'], programs=programs, username=username, current_page='programs', from_user=from_user, adventures=adventures)

# Adventure mode
@app.route('/hedy/adventures', methods=['GET'])
def adventures_list():
    return render_template('adventures.html', lang=lang, adventures=load_adventure_for_language (requested_lang ()), menu=render_main_menu('adventures'), username=current_user(request) ['username'], auth=TRANSLATIONS.data [lang] ['Auth'])

@app.route('/hedy/adventures/<adventure_name>', methods=['GET'], defaults={'level': 1})
@app.route('/hedy/adventures/<adventure_name>/<level>', methods=['GET'])
def adventure_page(adventure_name, level):

    user = current_user (request)
    level = int (level)
    adventures = load_adventure_for_language (requested_lang ())

    # If requested adventure does not exist, return 404
    if not adventure_name in adventures ['adventures']:
        return 'No such Hedy adventure!', 404

    adventure = adventures ['adventures'] [adventure_name]

    # If no level is specified (this will happen if the last element of the path (minus the query parameter) is the same as the adventure_name)
    if re.sub (r'\?.+', '', request.url.split ('/') [len (request.url.split ('/')) - 1]) == adventure_name:
        # If user is logged in, check if they have a program for this adventure
        # If there are many, note the highest level for which there is a saved program
        desired_level = 0
        if user ['username']:
            existing_programs = db_get_many ('programs', {'username': user ['username']}, True)
            for program in existing_programs:
                if 'adventure_name' in program and program ['adventure_name'] == adventure_name and program ['level'] > desired_level:
                    desired_level = program ['level']
            # If the user has a saved program for this adventure, redirect them to the level with the highest adventure
            if desired_level != 0:
                return redirect(request.url.replace ('/' + adventure_name, '/' + adventure_name + '/' + str (desired_level)), code=302)
        # If user is not logged in, or has no saved programs for this adventure, default to the lowest level available for the adventure
        if desired_level == 0:
            for key in adventure ['levels'].keys ():
                if type_check (key, 'int') and (desired_level == 0 or desired_level > key):
                    desired_level = key
        level = desired_level

    # If requested level is not in adventure, return 404
    if not level in adventure ['levels']:
        abort(404)

    initialize_gfi_session(requested_lang())

    adventure_assignments = load_adventure_assignments_per_level(requested_lang(), level)
    g.prefix = '/hedy'
    return hedyweb.render_assignment_editor(
        request=request,
        course=HEDY_COURSE[requested_lang()],
        level_number=level,
        assignment_number=1,
        menu=render_main_menu('hedy'),
        translations=TRANSLATIONS,
        version=version(),
        adventure_assignments=adventure_assignments,
        # The relevant loaded program will be available to client-side js and it will be loaded by js.
        loaded_program='',
        adventure_name=adventure_name)

# routing to index.html
@app.route('/hedy', methods=['GET'], defaults={'level': '1', 'step': 1})
@app.route('/hedy/<level>', methods=['GET'], defaults={'step': 1})
@app.route('/hedy/<level>/<step>', methods=['GET'])
def index(level, step):
    # Sublevel requested
    if re.match ('\d+-\d+', level):
        pass
        # If level has a dash, we keep it as a string
    # Normal level requested
    elif re.match ('\d', level):
        try:
            g.level = level = int(level)
        except:
            return 'No such Hedy level!', 404
    else:
        return 'No such Hedy level!', 404

    g.lang = requested_lang()
    g.prefix = '/hedy'

    initialize_gfi_session(g.lang)

    loaded_program = ''
    adventure_name = ''

    # If step is a string that has more than two characters, it must be an id of a program
    if step and type_check (step, 'str') and len (step) > 2:
        result = db_get ('programs', {'id': step})
        if not result:
            return 'No such program', 404
        # If the program is not public, allow only the owner of the program, the admin user and the teacher users to access the program
        user = current_user (request)
        public_program = 'public' in result and result ['public']
        if not public_program and user ['username'] != result ['username'] and not is_admin (request) and not is_teacher (request):
            return 'No such program!', 404
        loaded_program = {'code': result ['code'], 'name': result ['name'], 'adventure_name': result.get ('adventure_name')}
        if 'adventure_name' in result:
            adventure_name = result ['adventure_name']
        # We default to step 1 to provide a meaningful default assignment
        step = 1

    adventure_assignments = load_adventure_assignments_per_level(g.lang, level)
    return hedyweb.render_assignment_editor(
        request=request,
        course=HEDY_COURSE[g.lang],
        level_number=level,
        assignment_number=step,
        menu=render_main_menu('hedy'),
        translations=TRANSLATIONS,
        version=version(),
        adventure_assignments=adventure_assignments,
        loaded_program=loaded_program,
        adventure_name=adventure_name)

@app.route('/hedy/<id>/view', methods=['GET'])
def view_program(id):
    g.lang = requested_lang()
    g.prefix = '/hedy'

    result = db_get ('programs', {'id': id})
    if not result:
        return 'No such program', 404

    # Default to the language of the program's author (but still respect)
    # the switch if given.
    lang = request.args.get("lang")
    if not lang:
        lang = result['lang']

    arguments_dict = {}
    arguments_dict['program_id'] = id
    arguments_dict['page_title'] = f'{result["name"]} – Hedy'
    arguments_dict['level'] = result['level']  # Necessary for running
    arguments_dict['loaded_program'] = result
    arguments_dict['editor_readonly'] = True
    arguments_dict['show_edit_button'] = True

    # Everything below this line has nothing to do with this page and it's silly
    # that every page needs to put in so much effort to re-set it
    arguments_dict['lang'] = lang
    arguments_dict['menu'] = render_main_menu('view')
    arguments_dict['auth'] = TRANSLATIONS.data [lang] ['Auth']
    arguments_dict['username'] = current_user(request) ['username'] or None
    arguments_dict.update(**TRANSLATIONS.get_translations(lang, 'ui'))

    return render_template("view-program-page.html", **arguments_dict)


@app.route('/onlinemasters', methods=['GET'], defaults={'level': 1, 'step': 1})
@app.route('/onlinemasters/<int:level>', methods=['GET'], defaults={'step': 1})
@app.route('/onlinemasters/<int:level>/<int:step>', methods=['GET'])
def onlinemasters(level, step):
    g.level = level = int(level)
    g.lang = lang = requested_lang()
    g.prefix = '/onlinemasters'

    adventure_assignments = load_adventure_assignments_per_level(g.lang, level)

    return hedyweb.render_assignment_editor(
        request=request,
        course=ONLINE_MASTERS_COURSE,
        level_number=level,
        assignment_number=step,
        translations=TRANSLATIONS,
        version=version(),
        menu=None,
        adventure_assignments=adventure_assignments,
        loaded_program='',
        adventure_name='')

@app.route('/space_eu', methods=['GET'], defaults={'level': 1, 'step': 1})
@app.route('/space_eu/<int:level>', methods=['GET'], defaults={'step': 1})
@app.route('/space_eu/<int:level>/<int:step>', methods=['GET'])
def space_eu(level, step):
    g.level = level = int(level)
    g.lang = requested_lang()
    g.prefix = '/space_eu'

    adventure_assignments = load_adventure_assignments_per_level(g.lang, level)

    return hedyweb.render_assignment_editor(
        request=request,
        course=SPACE_EU_COURSE[g.lang],
        level_number=level,
        assignment_number=step,
        translations=TRANSLATIONS,
        version=version(),
        menu=None,
        adventure_assignments=adventure_assignments,
        loaded_program='',
        adventure_name='')

@app.route('/error_messages.js', methods=['GET'])
def error():
    error_messages = TRANSLATIONS.get_translations(requested_lang(), "ClientErrorMessages")
    response = make_response(render_template("error_messages.js", error_messages=json.dumps(error_messages)))

    if not is_debug_mode():
        # Cache for longer when not devving
        response.cache_control.max_age = 60 * 60  # Seconds

    return response

@app.route('/gradual_messages.js', methods=['GET'])
def gradual_error():
    error_messages = TRANSLATIONS.get_translations(requested_lang(), "GradualFeedback")
    return render_template("gradual_messages.js", error_messages=json.dumps(error_messages))

@app.errorhandler(500)
def internal_error(exception):
    import traceback
    print(traceback.format_exc())
    return "<h1>500 Internal Server Error</h1>", 500

@app.route('/index.html')
@app.route('/')
def default_landing_page():
    return main_page('start')

@app.route('/<page>')
def main_page(page):
    if page == 'favicon.ico':
        abort(404)

    lang = requested_lang()
    effective_lang = lang

    if page in ['signup', 'login', 'my-profile', 'recover', 'reset', 'admin']:
        return auth_templates(page, lang, render_main_menu(page), request)

    if page == 'programs':
        return programs_page(request)

    # Default to English if requested language is not available
    if not path.isfile(f'main/{page}-{effective_lang}.md'):
        effective_lang = 'en'

    try:
        with open(f'main/{page}-{effective_lang}.md', 'r', encoding='utf-8') as f:
            contents = f.read()
    except IOError:
        abort(404)

    front_matter, markdown = split_markdown_front_matter(contents)

    menu = render_main_menu(page)
    return render_template('main-page.html', mkd=markdown, lang=lang, menu=menu, username=current_user(request) ['username'], auth=TRANSLATIONS.data [lang] ['Auth'], **front_matter)


def session_id():
    """Returns or sets the current session ID."""
    if 'session_id' not in session:
        if os.getenv ('IS_TEST_ENV') and 'X-session_id' in request.headers:
            session['session_id'] = request.headers ['X-session_id']
        else:
            session['session_id'] = uuid.uuid4().hex
    return session['session_id']

def requested_lang():
    """Return the user's requested language code.

    If not in the request parameters, use the browser's accept-languages
    header to do language negotiation.
    """
    lang = request.args.get("lang")
    if lang: return lang

    return request.accept_languages.best_match(ALL_LANGUAGES.keys(), 'en')

@app.template_global()
def current_language():
    return make_lang_obj(requested_lang())

@app.template_global()
def hedy_link(level_nr, assignment_nr, subpage=None, lang=None):
    """Make a link to a Hedy page."""
    parts = [g.prefix]
    parts.append('/' + str(level_nr))
    if str(assignment_nr) != '1' or subpage:
        parts.append('/' + str(assignment_nr if assignment_nr else '1'))
    if subpage and subpage != 'code':
        parts.append('/' + subpage)
    parts.append('?')
    parts.append('lang=' + (lang if lang else requested_lang()))
    return ''.join(parts)

@app.template_global()
def other_languages():
    cl = requested_lang()
    return [make_lang_obj(l) for l in ALL_LANGUAGES.keys() if l != cl]

@app.template_global()
def localize_link(url):
    lang = requested_lang()
    if not lang:
        return url
    return url + '?lang=' + lang

def make_lang_obj(lang):
    """Make a language object for a given language."""
    return {
        'sym': ALL_LANGUAGES[lang],
        'lang': lang
    }


@app.template_global()
def modify_query(**new_values):
    args = request.args.copy()

    for key, value in new_values.items():
        args[key] = value

    return '{}?{}'.format(request.path, url_encode(args))


def no_none_sense(d):
    """Remove all None values from a dict."""
    return {k: v for k, v in d.items() if v is not None}


def split_markdown_front_matter(md):
    parts = re.split('^---', md, 1, re.M)
    if len(parts) == 1:
        return {}, md
    # safe_load returns 'None' if the string is empty
    front_matter = yaml.safe_load(parts[0]) or {}
    if not isinstance(front_matter, dict):
      # There was some kind of parsing error
      return {}, md

    return front_matter, parts[1]


def render_main_menu(current_page):
    """Render a list of (caption, href, selected, color) from the main menu."""
    return [dict(
        caption=item.get(requested_lang(), item.get('en', '???')),
        href='/' + item['_'],
        selected=(current_page == item['_']),
        accent_color=item.get('accent_color', 'white')
    ) for item in main_menu_json['nav']]

# *** PROGRAMS ***

@app.route('/programs_list', methods=['GET'])
@requires_login
def list_programs (user):
    return {'programs': db_get_many ('programs', {'username': user ['username']}, True)}

# Not very restful to use a GET to delete something, but indeed convenient; we can do it with a single link and avoiding AJAX.
@app.route('/programs/delete/<program_id>', methods=['GET'])
@requires_login
def delete_program (user, program_id):
    result = db_get ('programs', {'id': program_id})
    if not result or result ['username'] != user ['username']:
        return "", 404
    db_del ('programs', {'id': program_id})
    program_count = 0
    if 'program_count' in user:
        program_count = user ['program_count']
    db_update ('users', {'username': user ['username'], 'program_count': program_count - 1})
    return redirect ('/programs')

@app.route('/programs', methods=['POST'])
@requires_login
def save_program (user):

    body = request.json
    if not type_check (body, 'dict'):
        return 'body must be an object', 400
    if not object_check (body, 'code', 'str'):
        return 'code must be a string', 400
    if not object_check (body, 'name', 'str'):
        return 'name must be a string', 400
    if not object_check (body, 'level', 'int'):
        return 'level must be an integer', 400
    if 'adventure_name' in body:
        if not object_check (body, 'adventure_name', 'str'):
            return 'if present, adventure_name must be a string', 400

    # We check if a program with a name `xyz` exists in the database for the username.
    # It'd be ideal to search by username & program name, but since DynamoDB doesn't allow searching for two indexes at the same time, this would require to create a special index to that effect, which is cumbersome.
    # For now, we bring all existing programs for the user and then search within them for repeated names.
    programs = db_get_many ('programs', {'username': user ['username']}, True)
    program_id = uuid.uuid4().hex
    overwrite = False
    for program in programs:
        if program ['name'] == body ['name']:
            overwrite = True
            program_id = program ['id']
            break

    stored_program = {
        'id': program_id,
        'session': session_id(),
        'date': timems (),
        'lang': requested_lang(),
        'version': version(),
        'level': body ['level'],
        'code': body ['code'],
        'name': body ['name'],
        'username': user ['username']
    }

    if 'adventure_name' in body:
        stored_program ['adventure_name'] = body ['adventure_name']

    if overwrite:
        db_update('programs', stored_program)
    else:
        db_create('programs', stored_program)
        db_update('users', {'username': user ['username'], 'program_count': len (programs) + 1})

    return jsonify({'name': body ['name'], 'id': program_id})

@app.route('/programs/share', methods=['POST'])
@requires_login
def share_unshare_program(user):
    body = request.json
    if not type_check (body, 'dict'):
        return 'body must be an object', 400
    if not object_check (body, 'id', 'str'):
        return 'id must be a string', 400
    if not object_check (body, 'public', 'bool'):
        return 'public must be a boolean', 400

    result = db_get ('programs', {'id': body ['id']})
    if not result or result ['username'] != user ['username']:
        return 'No such program!', 404

    db_update ('programs', {'id': body ['id'], 'public': 1 if body ['public'] else None})
    return jsonify({'id': body ['id']})

@app.route('/translate/<source>/<target>')
def translate_fromto(source, target):
    # FIXME: right now loading source file on demand. We might need to cache this...
    source_adventures = load_yaml(f'coursedata/adventures/{source}.yaml')
    source_levels = load_yaml(f'coursedata/level-defaults/{source}.yaml')
    source_texts = load_yaml(f'coursedata/texts/{source}.yaml')

    target_adventures = load_yaml(f'coursedata/adventures/{target}.yaml')
    target_levels = load_yaml(f'coursedata/level-defaults/{target}.yaml')
    target_texts = load_yaml(f'coursedata/texts/{target}.yaml')

    files = []

    files.append(translating.TranslatableFile(
      'Levels',
      f'level-defaults/{target}.yaml',
      translating.struct_to_sections(source_levels, target_levels)))

    files.append(translating.TranslatableFile(
      'Messages',
      f'texts/{target}.yaml',
      translating.struct_to_sections(source_texts, target_texts)))

    files.append(translating.TranslatableFile(
      'Adventures',
      f'adventures/{target}.yaml',
      translating.struct_to_sections(source_adventures, target_adventures)))

    return render_template('translate-fromto.html',
        source_lang=source,
        target_lang=target,
        files=files)

@app.route('/update_yaml', methods=['POST'])
def update_yaml():
    filename = path.join('coursedata', request.form['file'])
    # The file MUST point to something inside our 'coursedata' directory
    # (no exploiting bullshit here)
    filepath = path.abspath(filename)
    expected_path = path.abspath('coursedata')
    if not filepath.startswith(expected_path):
        raise RuntimeError('Are you trying to trick me?')

    data = load_yaml_rt(filepath)
    for key, value in request.form.items():
        if key.startswith('c:'):
            translating.apply_form_change(data, key[2:], translating.normalize_newlines(value))

    data = translating.normalize_yaml_blocks(data)

    return Response(dump_yaml_rt(data),
        mimetype='application/x-yaml',
        headers={'Content-disposition': 'attachment; filename=' + request.form['file'].replace('/', '-')})


# *** AUTH ***

from website import auth
auth.routes (app, requested_lang)

# *** START SERVER ***

def on_server_start():
    """Called just before the server is started, both in developer mode and on Heroku.

    Use this to initialize objects, dependencies and connections.
    """
    db_init()


if __name__ == '__main__':
    # Start the server on a developer machine. Flask is initialized in DEBUG mode, so it
    # hot-reloads files. We also flip our own internal "debug mode" flag to True, so our
    # own file loading routines also hot-reload.
    utils.set_debug_mode(True)

    on_server_start()

    # Threaded option enables multiple instances for multiple user access support
    app.run(threaded=True, debug=True, port=config ['port'], host="0.0.0.0")

    # See `Procfile` for how the server is started on Heroku.
