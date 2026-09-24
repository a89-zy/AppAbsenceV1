import os
import gzip
from datetime import timedelta
from flask import Flask
from database import db
from blueprints.auth import auth_bp
from blueprints.absence import absence_bp
from blueprints.activity import activity_bp
from blueprints.configuration import configuration_bp
from blueprints.main import main_bp
from blueprints.textbook import textbook_bp
from blueprints.registration import registration_bp
from blueprints.resource import resource_bp
from blueprints.evaluation import evaluation_bp
from database import User
from flask import session, request, redirect, url_for, send_from_directory, make_response

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///absence.db'
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_secret_key_absence_app_2026')
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 2592000  # 30 jours de cache pour les ressources statiques
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=2)  # Déconnexion automatique après 2h d'inactivité

# Durcissement de sécurité des cookies de session (OWASP)
app.config['SESSION_COOKIE_HTTPONLY'] = True       # Empêche l'accès aux cookies par JavaScript (anti-XSS)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'      # Protection contre les attaques CSRF
app.config['SESSION_COOKIE_SECURE'] = False        # Mettre à True en environnement HTTPS strict

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'resources'), exist_ok=True)

db.init_app(app)
from extensions import mail
app.config['MAIL_SERVER'] = 'smtp.googlemail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USER', 'azzeddine.zyani@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASS', 'ovsq yuwd vxex wbam')
mail.init_app(app)

# Register Blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(main_bp)
app.register_blueprint(absence_bp, url_prefix='/absence')
app.register_blueprint(activity_bp, url_prefix='/activity')
app.register_blueprint(evaluation_bp, url_prefix='/evaluation')
app.register_blueprint(configuration_bp, url_prefix='/configuration')
app.register_blueprint(textbook_bp)
app.register_blueprint(registration_bp)
app.register_blueprint(resource_bp)

@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')

@app.route('/sw.js')
def service_worker():
    response = make_response(send_from_directory('static', 'sw.js', mimetype='application/javascript'))
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response

@app.before_request
def require_login():
    # Allow static assets, auth endpoints, public student registration, PWA files, shared textbook, and public resources without login
    exempt_endpoints = {
        'auth.login', 'auth.verify_2fa', 'static', 'main.uploaded_file',
        'manifest', 'service_worker',
        'registration.student_register', 'registration.student_submit',
        'registration.student_update_portal', 'registration.get_class_students',
        'registration.student_update_identify', 'registration.student_update_submit',
        'registration.generate_qr', 'registration.update_qr',
        'textbook.shared_view', 'textbook.shared_qr',
        'resource.shared_view', 'resource.verify_pin', 'resource.download_file', 'resource.resource_qr'
    }
    if request.endpoint and (request.endpoint in exempt_endpoints or request.endpoint.startswith('static')):
        return None
    # If not logged in, redirect to login page
    if 'user_id' not in session:
        return redirect(url_for('auth.login', next=request.url))
    session.permanent = True

@app.after_request
def compress_and_secure_response(response):
    """Injecte les en-têtes de sécurité OWASP et compresse les réponses volumineuses en Gzip."""
    # En-têtes de sécurité recommandés
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'

    # Désactiver le cache pour les pages HTML afin d'assurer l'actualisation immédiate des templates
    content_type = response.content_type or ''
    if 'text/html' in content_type:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'

    accept_encoding = request.headers.get('Accept-Encoding', '')
    if 'gzip' in accept_encoding.lower() and 200 <= response.status_code < 300:
        if not response.direct_passthrough and 'Content-Encoding' not in response.headers:
            compressible = ('application/json', 'text/html', 'text/css', 'application/javascript', 'text/plain')
            if any(content_type.startswith(t) for t in compressible):
                data = response.get_data()
                if len(data) >= 500:
                    compressed = gzip.compress(data, compresslevel=6)
                    response.set_data(compressed)
                    response.headers['Content-Encoding'] = 'gzip'
                    response.headers['Content-Length'] = len(compressed)
    return response

def detect_device(user_agent_string):
    """Détecte si l'appareil est un PC (desktop), une tablette ou un smartphone (mobile)."""
    if not user_agent_string:
        return 'desktop', False
    
    ua = user_agent_string.lower()
    
    # Tablettes explicites (iPad, Android sans 'mobile', Kindle, Silk, etc.)
    is_tablet = (
        'ipad' in ua or 
        ('android' in ua and 'mobile' not in ua) or 
        'tablet' in ua or 
        'silk' in ua or 
        'playbook' in ua
    )
    
    # iPadOS 13+ se présente parfois comme MacIntel avec multi-touch (Safari)
    # Côté serveur, un user-agent Safari sur Mac avec "version/.. safari" est standard,
    # mais si l'en-tête Sec-CH-UA-Mobile ou un hint spécifique existe on s'y adapte.
    
    # Smartphones explicites
    is_phone = (
        'mobile' in ua or 
        'iphone' in ua or 
        'ipod' in ua or 
        'webos' in ua or 
        'blackberry' in ua or 
        'windows phone' in ua
    )
    
    if is_tablet:
        return 'tablet', True
    elif is_phone:
        return 'mobile', True
    else:
        return 'desktop', False

@app.context_processor
def inject_ui_theme():
    """Injecte le thème UI, l'année scolaire active et le mode appareil (desktop/mobile/tablette) dans tous les templates."""
    from database import AppSetting, SchoolYear
    try:
        theme = AppSetting.get_value('app_ui_theme', 'royal_blue')
    except Exception:
        theme = 'royal_blue'

    # Récupération de toutes les années pour le sélecteur
    try:
        all_school_years = SchoolYear.query.order_by(SchoolYear.start_date.desc()).all()
    except Exception:
        all_school_years = []

    # Année active dans la session ou par défaut
    current_year = None
    selected_year_id = session.get('active_school_year_id')
    if selected_year_id:
        current_year = next((y for y in all_school_years if y.id == selected_year_id), None)
    
    # Si aucune année en session ou introuvable, prendre l'année marquée is_active ou la première
    if not current_year and all_school_years:
        current_year = next((y for y in all_school_years if y.is_active), all_school_years[0])
        session['active_school_year_id'] = current_year.id

    is_read_only = current_year.is_read_only() if current_year else False

    # Détection d'appareil (avec possibilité de forçage manuel en session ou via ?device_mode=)
    forced_mode = request.args.get('device_mode')
    if forced_mode in ['desktop', 'mobile', 'tablet', 'auto']:
        if forced_mode == 'auto':
            session.pop('device_mode_override', None)
        else:
            session['device_mode_override'] = forced_mode

    saved_mode = session.get('device_mode_override')
    if saved_mode:
        device_type = saved_mode
        is_mobile = (saved_mode in ['mobile', 'tablet'])
    else:
        ua_string = request.headers.get('User-Agent', '')
        device_type, is_mobile = detect_device(ua_string)

    return dict(
        app_ui_theme=theme,
        all_school_years=all_school_years,
        current_school_year=current_year,
        is_year_read_only=is_read_only,
        device_type=device_type,
        is_mobile=is_mobile,
        device_mode_override=saved_mode
    )

@app.route('/set-device-mode/<mode>', methods=['GET'])
def set_device_mode(mode):
    """Permet de basculer manuellement le mode d'affichage (auto, desktop, mobile, tablet)."""
    if mode in ['auto', 'desktop', 'mobile', 'tablet']:
        if mode == 'auto':
            session.pop('device_mode_override', None)
        else:
            session['device_mode_override'] = mode
    next_url = request.referrer or url_for('main.dashboard')
    return redirect(next_url)

@app.route('/switch-school-year/<int:year_id>', methods=['GET'])
def switch_school_year(year_id):
    """Bascule l'année scolaire active pour la session de l'enseignant."""
    from database import SchoolYear
    year = SchoolYear.query.get(year_id)
    if year:
        session['active_school_year_id'] = year.id
    next_url = request.referrer or url_for('main.dashboard')
    return redirect(next_url)

with app.app_context():
    db.create_all()
    # Migration légère : vérifier la présence de la colonne gender dans la table student
    try:
        from sqlalchemy import text
        with db.engine.connect() as conn:
            columns = [row[1] for row in conn.execute(text("PRAGMA table_info(student)")).fetchall()]
            if 'gender' not in columns:
                conn.execute(text("ALTER TABLE student ADD COLUMN gender VARCHAR(10)"))
                conn.commit()

            # Index composites pour accélérer les requêtes d'absences, notes et cahier de texte
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_student_class_name ON student (class_id, last_name, first_name)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_grade_student_activity ON grade (student_id, activity_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_textbook_class_date ON textbook_session (class_id, date)"))
            conn.commit()
    except Exception as e:
        print(f"Note migration SQLite: {e}")

    # Seed default teacher user if no user exists
    if not User.query.first():
        default_user = User(
            username='admin',
            full_name='Prof ZYANI Azzeddine'
        )
        default_user.set_password('admin123')
        db.session.add(default_user)
        db.session.commit()
        print("Compte enseignant par défaut créé : Identifiant 'admin', Mot de passe 'admin123'")

if __name__ == '__main__':
    # host='0.0.0.0' permet d'accéder à l'application via localhost:5000 ou l'IP locale de la machine (ex: 192.168.1.7:5000)
    app.run(host='0.0.0.0', port=8080, debug=True)
