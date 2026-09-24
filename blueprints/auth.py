from flask import Blueprint, render_template, request, redirect, url_for, flash, session, make_response
from database import db, User, LoginLog
from functools import wraps
from datetime import datetime, timedelta
import time
import pyotp
import qrcode
import io

auth_bp = Blueprint('auth', __name__)

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION_MINUTES = 10

def get_client_ip():
    """Récupère l'adresse IP réelle du client."""
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr or '127.0.0.1'

def log_login_event(username, status, details=None):
    """Enregistre un événement de sécurité dans le journal d'audit."""
    try:
        log_entry = LoginLog(
            username=username,
            ip_address=get_client_ip(),
            user_agent=request.user_agent.string[:250] if request.user_agent else '',
            status=status,
            details=details
        )
        db.session.add(log_entry)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Error logging login event: {e}")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Veuillez vous connecter pour accéder à cette page.', 'warning')
            return redirect(url_for('auth.login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        user = User.query.filter_by(username=username).first()

        # 1. Vérification du verrouillage de compte (Anti Brute-Force)
        if user and user.is_locked():
            remaining_mins = max(1, int((user.locked_until - datetime.now()).total_seconds() / 60) + 1)
            log_login_event(username, 'LOCKED', f"Tentative sur compte verrouillé (reste {remaining_mins} min)")
            flash(f"Compte temporairement verrouillé pour des raisons de sécurité. Réessayez dans {remaining_mins} minute(s).", "error")
            return render_template('login.html', is_locked=True, remaining_mins=remaining_mins)

        # 2. Vérification du mot de passe
        if user and user.check_password(password):
            # Réinitialiser les compteurs d'échecs
            user.failed_login_attempts = 0
            user.locked_until = None
            db.session.commit()

            # 3. Si le 2FA (TOTP) est activé, diriger vers la validation OTP
            if user.is_totp_enabled and user.totp_secret:
                session['2fa_user_id'] = user.id
                session['2fa_next'] = request.args.get('next') or ''
                return redirect(url_for('auth.verify_2fa'))

            # 4. Connexion réussie directe
            # Protection contre la fixation de session
            session.clear()
            session['user_id'] = user.id
            session['username'] = user.username
            session['user_full_name'] = user.full_name or user.username
            session.permanent = True

            user.last_login_at = datetime.now()
            user.last_login_ip = get_client_ip()
            db.session.commit()

            log_login_event(username, 'SUCCESS', 'Connexion réussie')
            flash(f'Bienvenue, {session["user_full_name"]} !', 'success')
            next_page = request.args.get('next')
            return redirect(next_page or url_for('main.dashboard'))

        else:
            # Échec d'authentification : Délai défensif (tarpitting anti-bruteforce)
            time.sleep(0.5)

            if user:
                user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
                if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
                    user.locked_until = datetime.now() + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
                    db.session.commit()
                    log_login_event(username, 'LOCKED', f'Verrouillé après {user.failed_login_attempts} échecs consécutifs')
                    flash(f'Trop de tentatives infructueuses ({MAX_FAILED_ATTEMPTS}). Compte verrouillé pendant {LOCKOUT_DURATION_MINUTES} minutes.', 'error')
                    return render_template('login.html', is_locked=True, remaining_mins=LOCKOUT_DURATION_MINUTES)
                else:
                    db.session.commit()
                    remaining = MAX_FAILED_ATTEMPTS - user.failed_login_attempts
                    log_login_event(username, 'FAILED', f'Tentative échouée ({user.failed_login_attempts}/{MAX_FAILED_ATTEMPTS})')
                    flash(f'Identifiant ou mot de passe incorrect. ({remaining} tentative(s) restante(s))', 'error')
            else:
                log_login_event(username, 'FAILED', 'Utilisateur inexistant')
                flash('Identifiant ou mot de passe incorrect.', 'error')

    return render_template('login.html')

@auth_bp.route('/login/2fa', methods=['GET', 'POST'])
def verify_2fa():
    """Page de saisie du code TOTP (Google/Microsoft Authenticator) lors du login."""
    user_id = session.get('2fa_user_id')
    if not user_id:
        return redirect(url_for('auth.login'))

    user = db.session.get(User, user_id)
    if not user or not user.is_totp_enabled or not user.totp_secret:
        session.pop('2fa_user_id', None)
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        otp_code = request.form.get('otp_code', '').strip().replace(' ', '')
        totp = pyotp.TOTP(user.totp_secret)

        if totp.verify(otp_code, valid_window=1): # tolérance +/- 30s
            next_url = session.get('2fa_next')
            session.clear()
            session['user_id'] = user.id
            session['username'] = user.username
            session['user_full_name'] = user.full_name or user.username
            session.permanent = True

            user.last_login_at = datetime.now()
            user.last_login_ip = get_client_ip()
            db.session.commit()

            log_login_event(user.username, 'SUCCESS', 'Connexion réussie avec 2FA validée')
            flash(f'Authentification à double facteur validée. Bienvenue, {session["user_full_name"]} !', 'success')
            return redirect(next_url or url_for('main.dashboard'))
        else:
            time.sleep(0.5)
            log_login_event(user.username, '2FA_FAILED', 'Code TOTP invalide')
            flash('Code à 6 chiffres incorrect ou expiré. Veuillez vérifier votre application d\'authentification.', 'error')

    return render_template('login_2fa.html', user=user)

@auth_bp.route('/logout')
def logout():
    username = session.get('username')
    if username:
        log_login_event(username, 'SUCCESS', 'Déconnexion manuelle')
    session.clear()
    flash('Vous avez été déconnecté avec succès.', 'info')
    return redirect(url_for('auth.login'))

@auth_bp.route('/change-password', methods=['POST'])
@login_required
def change_password():
    current_password = request.form.get('current_password', '')
    new_username = request.form.get('username', '').strip()
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    user = db.session.get(User, session['user_id'])
    if not user:
        flash('Utilisateur introuvable.', 'error')
        return redirect(url_for('auth.logout'))

    if not user.check_password(current_password):
        flash('Le mot de passe actuel est incorrect.', 'error')
        return redirect(url_for('configuration.settings') + '#tab-account')

    if new_username and new_username != user.username:
        existing = User.query.filter_by(username=new_username).first()
        if existing and existing.id != user.id:
            flash('Cet identifiant est déjà utilisé.', 'error')
            return redirect(url_for('configuration.settings') + '#tab-account')
        user.username = new_username
        session['username'] = new_username

    if new_password:
        # Règle de robustesse du mot de passe : 8 caractères minimum + chiffres & lettres
        if len(new_password) < 8:
            flash('Le nouveau mot de passe doit comporter au moins 8 caractères.', 'error')
            return redirect(url_for('configuration.settings') + '#tab-account')
        
        has_letter = any(c.isalpha() for c in new_password)
        has_digit = any(c.isdigit() for c in new_password)
        if not (has_letter and has_digit):
            flash('Le nouveau mot de passe doit contenir à la fois des lettres et des chiffres.', 'error')
            return redirect(url_for('configuration.settings') + '#tab-account')

        if new_password != confirm_password:
            flash('Les nouveaux mots de passe ne correspondent pas.', 'error')
            return redirect(url_for('configuration.settings') + '#tab-account')

        user.set_password(new_password)
        log_login_event(user.username, 'SUCCESS', 'Modification du mot de passe')

    db.session.commit()
    flash('Paramètres du compte mis à jour avec succès.', 'success')
    return redirect(url_for('configuration.settings') + '#tab-account')

# =========================================================================
# GESTION DU 2FA (DOUBLE FACTEUR TOTP)
# =========================================================================

@auth_bp.route('/2fa/setup', methods=['POST'])
@login_required
def setup_2fa():
    """Génère un nouveau secret TOTP pour l'utilisateur s'il n'est pas encore activé."""
    user = db.session.get(User, session['user_id'])
    if not user.totp_secret:
        user.totp_secret = pyotp.random_base32()
        db.session.commit()
    return redirect(url_for('configuration.settings') + '#tab-account')

@auth_bp.route('/2fa/qr')
@login_required
def get_2fa_qr():
    """Génère et sert l'image PNG du QR code pour Google Authenticator."""
    user = db.session.get(User, session['user_id'])
    if not user or not user.totp_secret:
        return '', 404

    totp_uri = pyotp.totp.TOTP(user.totp_secret).provisioning_uri(
        name=user.username,
        issuer_name='GestionScolaire-Absence'
    )
    qr_img = qrcode.make(totp_uri)
    buf = io.BytesIO()
    qr_img.save(buf, format='PNG')
    buf.seek(0)
    response = make_response(buf.getvalue())
    response.headers['Content-Type'] = 'image/png'
    return response

@auth_bp.route('/2fa/enable', methods=['POST'])
@login_required
def enable_2fa():
    """Valide le premier code saisi par l'enseignant pour activer le 2FA définitivement."""
    user = db.session.get(User, session['user_id'])
    code = request.form.get('code', '').strip().replace(' ', '')
    
    if not user.totp_secret:
        flash("Veuillez d'abord initialiser la configuration 2FA.", "error")
        return redirect(url_for('configuration.settings') + '#tab-account')

    totp = pyotp.TOTP(user.totp_secret)
    if totp.verify(code, valid_window=1):
        user.is_totp_enabled = True
        db.session.commit()
        log_login_event(user.username, 'SUCCESS', 'Activation du 2FA (TOTP)')
        flash("Double facteur (2FA) activé avec succès ! Votre compte est désormais protégé.", "success")
    else:
        flash("Code invalide. Vérifiez que l'heure de votre smartphone est bien synchronisée.", "error")

    return redirect(url_for('configuration.settings') + '#tab-account')

@auth_bp.route('/2fa/disable', methods=['POST'])
@login_required
def disable_2fa():
    """Désactive le 2FA après confirmation avec le mot de passe actuel."""
    user = db.session.get(User, session['user_id'])
    password = request.form.get('password', '')

    if not user.check_password(password):
        flash("Mot de passe incorrect pour désactiver le 2FA.", "error")
        return redirect(url_for('configuration.settings') + '#tab-account')

    user.is_totp_enabled = False
    user.totp_secret = None
    db.session.commit()
    log_login_event(user.username, 'SUCCESS', 'Désactivation du 2FA')
    flash("L'authentification à double facteur a été désactivée.", "info")
    return redirect(url_for('configuration.settings') + '#tab-account')
