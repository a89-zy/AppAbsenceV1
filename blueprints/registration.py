import os
import io
import socket
import qrcode
import qrcode.constants
from datetime import datetime
from flask import (
    Blueprint, render_template, request, redirect, url_for, 
    flash, jsonify, send_file, current_app, session
)
from werkzeug.utils import secure_filename
from database import db, Class, Student, AppSetting, PendingStudent, SchoolYear

registration_bp = Blueprint('registration', __name__)

def get_current_school_year():
    """Récupère l'année scolaire active pour la session courante."""
    year_id = session.get('active_school_year_id')
    if year_id:
        y = SchoolYear.query.get(year_id)
        if y: return y
    active = SchoolYear.query.filter_by(is_active=True).first()
    if active: return active
    return SchoolYear.query.order_by(SchoolYear.start_date.desc()).first()


def get_local_ip():
    """Détecte l'adresse IP locale de la machine sur le réseau local (WiFi / Ethernet)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        # Ne contacte pas réellement, mais permet au système d'exploitation de choisir l'interface active
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return '127.0.0.1'

def detect_device_type(user_agent_string):
    """Détecte si l'appareil est un mobile, tablette ou ordinateur."""
    ua = (user_agent_string or '').lower()
    if 'ipad' in ua or 'tablet' in ua or 'kindle' in ua:
        return 'Tablette'
    if any(m in ua for m in ['mobile', 'android', 'iphone', 'ipod', 'webos', 'blackberry']):
        return 'Smartphone'
    return 'Ordinateur'

# =========================================================================
# ROUTES PUBLIQUES (ACCÈS ÉLÈVES SANS CONNEXION)
# =========================================================================

@registration_bp.route('/inscription', methods=['GET'])
def student_register():
    """Page d'auto-inscription accessible aux élèves."""
    is_enabled = AppSetting.get_value('registration_enabled', 'false').lower() == 'true'
    if not is_enabled:
        return render_template('registration/closed.html')
    
    pin_required = AppSetting.get_value('registration_pin', '').strip()
    
    # Récupérer l'année scolaire active par défaut pour les élèves
    active_year = SchoolYear.query.filter_by(is_active=True).first() or SchoolYear.query.order_by(SchoolYear.start_date.desc()).first()
    if active_year:
        classes = Class.query.filter_by(school_year_id=active_year.id).order_by(Class.name).all()
    else:
        classes = Class.query.order_by(Class.name).all()

    user_agent = request.headers.get('User-Agent', '')
    device_type = detect_device_type(user_agent)

    return render_template(
        'registration/student_form.html',
        classes=classes,
        device_type=device_type,
        has_pin=bool(pin_required)
    )

@registration_bp.route('/inscription/submit', methods=['POST'])
def student_submit():
    """Traitement de la soumission de la fiche élève."""
    is_enabled = AppSetting.get_value('registration_enabled', 'false').lower() == 'true'
    if not is_enabled:
        return jsonify({'success': False, 'message': "Les inscriptions sont fermées."}), 403

    # Vérification du code PIN si configuré
    pin_required = AppSetting.get_value('registration_pin', '').strip()
    if pin_required:
        pin_entered = request.form.get('pin', '').strip()
        if pin_entered != pin_required:
            return jsonify({'success': False, 'message': "Code PIN de session invalide."}), 400

    cne = request.form.get('cne', '').strip()
    first_name = request.form.get('first_name', '').strip()
    last_name = request.form.get('last_name', '').strip()
    class_id = request.form.get('class_id', '').strip()
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    birth_date_raw = request.form.get('birth_date', '').strip()

    if not cne or not first_name or not last_name or not class_id:
        return jsonify({'success': False, 'message': "Veuillez renseigner tous les champs obligatoires (CNE, Nom, Prénom, Classe)."}), 400

    try:
        class_id_int = int(class_id)
    except ValueError:
        return jsonify({'success': False, 'message': "Classe invalide."}), 400

    birth_date = None
    if birth_date_raw:
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
            try:
                birth_date = datetime.strptime(birth_date_raw, fmt).date()
                break
            except ValueError:
                pass

    # Gestion de la photo
    photo_filename = None
    photo_file = request.files.get('photo')
    if photo_file and photo_file.filename:
        ext = os.path.splitext(photo_file.filename)[1].lower()
        if ext in ['.jpg', '.jpeg', '.png', '.webp']:
            # Nom sécurisé : cne_nom_timestamp.ext
            safe_cne = "".join(c for c in cne if c.isalnum() or c in ('-', '_'))
            timestamp = int(datetime.now().timestamp())
            photo_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'photos')
            os.makedirs(photo_dir, exist_ok=True)
            photo_filename = f"photos/{safe_cne}_{timestamp}{ext}"
            full_save_path = os.path.join(current_app.config['UPLOAD_FOLDER'], photo_filename)
            photo_file.save(full_save_path)

    # Détection de l'appareil
    device_type = detect_device_type(request.headers.get('User-Agent', ''))

    # Sauvegarde dans PendingStudent (mise à jour si déjà en attente avec le même CNE)
    existing_pending = PendingStudent.query.filter_by(cne=cne, status='pending').first()
    if existing_pending:
        existing_pending.first_name = first_name
        existing_pending.last_name = last_name
        existing_pending.class_id = class_id_int
        existing_pending.email = email or existing_pending.email
        existing_pending.phone = phone or existing_pending.phone
        if birth_date:
            existing_pending.birth_date = birth_date
        if photo_filename:
            existing_pending.photo_path = photo_filename
        existing_pending.device_type = device_type
        db.session.commit()
    else:
        pending = PendingStudent(
            cne=cne,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            birth_date=birth_date,
            photo_path=photo_filename,
            class_id=class_id_int,
            device_type=device_type,
            status='pending'
        )
        db.session.add(pending)
        db.session.commit()

    return jsonify({
        'success': True,
        'message': "Votre fiche a été envoyée avec succès ! L'enseignant va la valider.",
        'student': {
            'cne': cne,
            'name': f"{first_name} {last_name}"
        }
    })

# =========================================================================
# ROUTES ENSEIGNANT (GESTION DE LA SESSION & MODÉRATION)
# =========================================================================

@registration_bp.route('/registration/manage', methods=['GET'])
def manage():
    """Tableau de bord de gestion des inscriptions pour l'enseignant."""
    is_enabled = AppSetting.get_value('registration_enabled', 'false').lower() == 'true'
    pin = AppSetting.get_value('registration_pin', '')
    
    local_ip = get_local_ip()
    port = request.host.split(':')[1] if ':' in request.host else '5000'
    registration_url = f"http://{local_ip}:{port}/inscription"
    
    pending_list = PendingStudent.query.filter_by(status='pending').order_by(PendingStudent.created_at.desc()).all()
    approved_count = Student.query.count()
    current_year = get_current_school_year()
    if current_year:
        classes = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
    else:
        classes = Class.query.order_by(Class.name).all()


    # Détection des doublons existants déjà dans la base
    pending_data = []
    for p in pending_list:
        p_dict = p.to_dict()
        p_dict['photo_path'] = p.photo_path
        existing_student = Student.query.filter_by(cne=p.cne).first()
        p_dict['is_existing_student'] = bool(existing_student)
        p_dict['existing_class'] = existing_student.student_class.name if (existing_student and existing_student.student_class) else None
        pending_data.append(p_dict)

    # Configuration pour la session de mise à jour des profils
    student_update_enabled = AppSetting.get_value('student_update_enabled', 'false').lower() == 'true'
    student_update_pin = AppSetting.get_value('student_update_pin', '')
    student_update_target_class_id = AppSetting.get_value('student_update_target_class_id', 'all')
    update_allowed_str = AppSetting.get_value('student_update_allowed_fields', 'photo,name,birth_date,cne,contact')
    update_allowed_fields = [f.strip() for f in update_allowed_str.split(',') if f.strip()]
    update_url = f"http://{local_ip}:{port}/profil/maj"

    return render_template(
        'registration/teacher_dashboard.html',
        is_enabled=is_enabled,
        pin=pin,
        local_ip=local_ip,
        port=port,
        registration_url=registration_url,
        pending_students=pending_data,
        approved_count=approved_count,
        classes=classes,
        student_update_enabled=student_update_enabled,
        student_update_pin=student_update_pin,
        student_update_target_class_id=student_update_target_class_id,
        update_allowed_fields=update_allowed_fields,
        update_url=update_url
    )

@registration_bp.route('/registration/toggle', methods=['POST'])
def toggle_registration():
    """Active ou désactive la publication de la page d'inscription."""
    current_status = AppSetting.get_value('registration_enabled', 'false').lower() == 'true'
    new_status = not current_status
    AppSetting.set_value('registration_enabled', 'true' if new_status else 'false', "Statut du portail d'auto-inscription")
    
    # Enregistrer le code PIN si fourni
    pin = request.form.get('pin', '').strip()
    if 'pin' in request.form:
        AppSetting.set_value('registration_pin', pin, "Code PIN de session")

    return jsonify({
        'success': True,
        'enabled': new_status,
        'message': "Inscriptions OUVERTES" if new_status else "Inscriptions FERMÉES"
    })

@registration_bp.route('/registration/update-pin', methods=['POST'])
def update_pin():
    """Met à jour le code PIN de session."""
    data = request.get_json() or {}
    pin = data.get('pin', '').strip()
    AppSetting.set_value('registration_pin', pin, "Code PIN de session")
    return jsonify({'success': True, 'pin': pin})

@registration_bp.route('/registration/qr')
def generate_qr():
    """Génère l'image du QR Code pointant vers l'URL d'inscription locale."""
    local_ip = get_local_ip()
    port = request.host.split(':')[1] if ':' in request.host else '5000'
    url = f"http://{local_ip}:{port}/inscription"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1e293b", back_color="#ffffff")
    
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')

@registration_bp.route('/registration/live-data')
def live_data():
    """Endpoint JSON interrogé par le dashboard pour rafraîchissement temps réel."""
    pending = PendingStudent.query.filter_by(status='pending').order_by(PendingStudent.created_at.desc()).all()
    results = []
    for p in pending:
        d = p.to_dict()
        existing = Student.query.filter_by(cne=p.cne).first()
        d['is_existing_student'] = bool(existing)
        d['existing_class'] = existing.student_class.name if (existing and existing.student_class) else None
        results.append(d)

    is_enabled = AppSetting.get_value('registration_enabled', 'false').lower() == 'true'
    return jsonify({
        'enabled': is_enabled,
        'count': len(results),
        'pending': results
    })

@registration_bp.route('/registration/approve/<int:pending_id>', methods=['POST'])
def approve_student(pending_id):
    """Valide une fiche élève et l'ajoute/met à jour dans la table Student."""
    pending = PendingStudent.query.get_or_404(pending_id)
    
    # Vérifie si l'étudiant existe déjà
    student = Student.query.filter_by(cne=pending.cne).first()
    if student:
        # Mise à jour
        student.first_name = pending.first_name
        student.last_name = pending.last_name
        student.class_id = pending.class_id
        if pending.email:
            student.email = pending.email
        if pending.phone:
            student.phone = pending.phone
        if pending.birth_date:
            student.birth_date = pending.birth_date
        if pending.photo_path:
            student.photo_path = pending.photo_path
    else:
        # Création
        student = Student(
            cne=pending.cne,
            first_name=pending.first_name,
            last_name=pending.last_name,
            email=pending.email,
            phone=pending.phone,
            birth_date=pending.birth_date,
            photo_path=pending.photo_path,
            class_id=pending.class_id
        )
        db.session.add(student)

    pending.status = 'approved'
    db.session.commit()
    return jsonify({'success': True, 'message': f"Élève {pending.first_name} {pending.last_name} validé avec succès !"})

@registration_bp.route('/registration/approve-all', methods=['POST'])
def approve_all():
    """Valide toutes les fiches actuellement en attente."""
    pendings = PendingStudent.query.filter_by(status='pending').all()
    count = 0
    for p in pendings:
        student = Student.query.filter_by(cne=p.cne).first()
        if student:
            student.first_name = p.first_name
            student.last_name = p.last_name
            student.class_id = p.class_id
            if p.email:
                student.email = p.email
            if p.phone:
                student.phone = p.phone
            if p.birth_date:
                student.birth_date = p.birth_date
            if p.photo_path:
                student.photo_path = p.photo_path
        else:
            student = Student(
                cne=p.cne,
                first_name=p.first_name,
                last_name=p.last_name,
                email=p.email,
                phone=p.phone,
                birth_date=p.birth_date,
                photo_path=p.photo_path,
                class_id=p.class_id
            )
            db.session.add(student)
        p.status = 'approved'
        count += 1

    db.session.commit()
    return jsonify({'success': True, 'count': count, 'message': f"{count} élèves validés avec succès !"})

@registration_bp.route('/registration/reject/<int:pending_id>', methods=['POST'])
def reject_student(pending_id):
    """Rejette une fiche en attente."""
    pending = PendingStudent.query.get_or_404(pending_id)
    pending.status = 'rejected'
    db.session.commit()
    return jsonify({'success': True, 'message': "Fiche rejetée."})

@registration_bp.route('/registration/clear-history', methods=['POST'])
def clear_history():
    """Efface l'historique des inscriptions déjà traitées (approved/rejected)."""
    PendingStudent.query.filter(PendingStudent.status != 'pending').delete()
    db.session.commit()
    return jsonify({'success': True, 'message': "Historique nettoyé."})

# =========================================================================
# MODULE : MISE À JOUR AUTONOME DES PROFILS ÉTUDIANTS (SESSION & QR)
# =========================================================================

@registration_bp.route('/api/class/<int:class_id>/students', methods=['GET'])
def get_class_students(class_id):
    """Retourne la liste des élèves d'une classe (pour le sélecteur d'identification)."""
    students = Student.query.filter_by(class_id=class_id).order_by(*Student.default_order()).all()
    return jsonify({
        'success': True,
        'students': [{'id': s.id, 'name': f"{s.last_name} {s.first_name}"} for s in students]
    })

@registration_bp.route('/profil/maj', methods=['GET'])
def student_update_portal():
    """Page publique de mise à jour de profil pour les étudiants."""
    is_enabled = AppSetting.get_value('student_update_enabled', 'false').lower() == 'true'
    if not is_enabled:
        return render_template('registration/closed.html', custom_message="La session de mise à jour des profils est actuellement fermée par l'enseignant.")

    has_pin = bool(AppSetting.get_value('student_update_pin', '').strip())
    target_class_id = AppSetting.get_value('student_update_target_class_id', 'all')
    
    # Récupérer l'année active
    active_year = SchoolYear.query.filter_by(is_active=True).first() or SchoolYear.query.order_by(SchoolYear.start_date.desc()).first()
    
    if target_class_id != 'all' and target_class_id.isdigit():
        target_class = Class.query.get(int(target_class_id))
        classes = [target_class] if target_class else []
    elif active_year:
        classes = Class.query.filter_by(school_year_id=active_year.id).order_by(Class.name).all()
    else:
        classes = Class.query.order_by(Class.name).all()

    # Récupérer les permissions configurées par le prof
    allowed_str = AppSetting.get_value('student_update_allowed_fields', 'photo,name,birth_date,cne,contact')
    allowed_fields = [f.strip() for f in allowed_str.split(',') if f.strip()]

    user_agent = request.headers.get('User-Agent', '')
    device_type = detect_device_type(user_agent)

    return render_template(
        'registration/student_update_form.html',
        classes=classes,
        has_pin=has_pin,
        device_type=device_type,
        allowed_fields=allowed_fields
    )

@registration_bp.route('/profil/maj/auth', methods=['POST'])
def student_update_identify():
    """Vérifie l'identité de l'étudiant et retourne ses données pré-remplies."""
    is_enabled = AppSetting.get_value('student_update_enabled', 'false').lower() == 'true'
    if not is_enabled:
        return jsonify({'success': False, 'message': "La session de mise à jour est fermée."}), 403

    # Vérifier PIN si configuré
    pin_required = AppSetting.get_value('student_update_pin', '').strip()
    if pin_required:
        pin_entered = request.form.get('pin', '').strip()
        if pin_entered != pin_required:
            return jsonify({'success': False, 'message': "Code PIN de session incorrect."}), 400

    student_id = request.form.get('student_id')
    verification_code = request.form.get('verification_code', '').strip().lower()

    if not student_id or not verification_code:
        return jsonify({'success': False, 'message': "Veuillez sélectionner votre nom et saisir votre CNE ou Date de Naissance de confirmation."}), 400

    student = Student.query.get(student_id)
    if not student:
        return jsonify({'success': False, 'message': "Élève introuvable."}), 404

    # Vérification d'identité : le code saisi doit correspondre soit au CNE, soit à la date de naissance (YYYY-MM-DD ou DD/MM/YYYY)
    student_cne = (student.cne or '').strip().lower()
    student_bdate_iso = student.birth_date.strftime('%Y-%m-%d') if student.birth_date else ''
    student_bdate_fr = student.birth_date.strftime('%d/%m/%Y') if student.birth_date else ''

    is_verified = (
        (verification_code == student_cne) or
        (verification_code == student_bdate_iso) or
        (verification_code == student_bdate_fr) or
        (student.birth_date and verification_code == student.birth_date.strftime('%d-%m-%Y'))
    )

    if not is_verified:
        return jsonify({
            'success': False, 
            'message': "La confirmation a échoué. Veuillez saisir exactement votre CNE actuel ou votre date de naissance."
        }), 403

    # Permissions
    allowed_str = AppSetting.get_value('student_update_allowed_fields', 'photo,name,birth_date,cne,contact')
    allowed_fields = [f.strip() for f in allowed_str.split(',') if f.strip()]

    photo_url = url_for('main.uploaded_file', filename=student.photo_path if student.photo_path else 'photos/Default.jpg')

    return jsonify({
        'success': True,
        'student': {
            'id': student.id,
            'first_name': student.first_name,
            'last_name': student.last_name,
            'cne': student.cne or '',
            'birth_date': student.birth_date.strftime('%Y-%m-%d') if student.birth_date else '',
            'email': student.email or '',
            'phone': student.phone or '',
            'class_name': student.student_class.name if student.student_class else '',
            'photo_url': photo_url
        },
        'allowed_fields': allowed_fields
    })

@registration_bp.route('/profil/maj/submit', methods=['POST'])
def student_update_submit():
    """Enregistre les modifications apportées par l'étudiant à sa fiche."""
    is_enabled = AppSetting.get_value('student_update_enabled', 'false').lower() == 'true'
    if not is_enabled:
        return jsonify({'success': False, 'message': "La session de mise à jour est fermée."}), 403

    student_id = request.form.get('student_id')
    verification_code = request.form.get('verification_code', '').strip().lower()

    student = Student.query.get(student_id)
    if not student:
        return jsonify({'success': False, 'message': "Élève introuvable."}), 404

    # Re-vérification de sécurité
    student_cne = (student.cne or '').strip().lower()
    student_bdate_iso = student.birth_date.strftime('%Y-%m-%d') if student.birth_date else ''
    student_bdate_fr = student.birth_date.strftime('%d/%m/%Y') if student.birth_date else ''

    if not (verification_code == student_cne or verification_code == student_bdate_iso or verification_code == student_bdate_fr):
        return jsonify({'success': False, 'message': "Authentification invalide."}), 403

    # Récupérer les permissions configurées par l'enseignant
    allowed_str = AppSetting.get_value('student_update_allowed_fields', 'photo,name,birth_date,cne,contact')
    allowed_fields = [f.strip() for f in allowed_str.split(',') if f.strip()]

    # 1. Nom & Prénom
    if 'name' in allowed_fields:
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        if first_name and last_name:
            student.first_name = first_name
            student.last_name = last_name

    # 2. CNE (avec vérification d'unicité)
    if 'cne' in allowed_fields:
        new_cne = request.form.get('cne', '').strip()
        if new_cne and new_cne != student.cne:
            # Vérifier qu'aucun autre élève ne possède ce CNE
            duplicate = Student.query.filter(Student.cne == new_cne, Student.id != student.id).first()
            if duplicate:
                return jsonify({'success': False, 'message': f"Le CNE '{new_cne}' est déjà utilisé par un autre élève."}), 400
            student.cne = new_cne

    # 3. Date de naissance
    if 'birth_date' in allowed_fields:
        birth_date_raw = request.form.get('birth_date', '').strip()
        if birth_date_raw:
            for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
                try:
                    student.birth_date = datetime.strptime(birth_date_raw, fmt).date()
                    break
                except ValueError:
                    pass

    # 4. Contacts (email, téléphone)
    if 'contact' in allowed_fields:
        student.email = request.form.get('email', '').strip() or None
        student.phone = request.form.get('phone', '').strip() or None

    # 5. Photo de profil
    if 'photo' in allowed_fields:
        photo_file = request.files.get('photo')
        if photo_file and photo_file.filename:
            ext = os.path.splitext(photo_file.filename)[1].lower()
            if ext in ['.jpg', '.jpeg', '.png', '.webp']:
                safe_cne = "".join(c for c in (student.cne or f"student_{student.id}") if c.isalnum() or c in ('-', '_'))
                timestamp = int(datetime.now().timestamp())
                photo_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'photos')
                os.makedirs(photo_dir, exist_ok=True)
                photo_filename = f"photos/{safe_cne}_{timestamp}{ext}"
                full_save_path = os.path.join(current_app.config['UPLOAD_FOLDER'], photo_filename)
                photo_file.save(full_save_path)
                student.photo_path = photo_filename

    db.session.commit()

    return jsonify({
        'success': True,
        'message': f"Félicitations {student.first_name} ! Vos informations ont été mises à jour avec succès."
    })

# =========================================================================
# GESTION ENSEIGNANT : ACTIVATION / DÉSACTIVATION SESSION & QR
# =========================================================================

@registration_bp.route('/registration/student-update/toggle', methods=['POST'])
def toggle_student_update():
    """Active ou désactive la session de mise à jour des profils étudiants."""
    current_status = AppSetting.get_value('student_update_enabled', 'false').lower() == 'true'
    new_status = not current_status
    AppSetting.set_value('student_update_enabled', 'true' if new_status else 'false', "Session de mise à jour profil étudiant")

    # Mise à jour des options si fournies
    pin = request.form.get('pin', '').strip()
    target_class_id = request.form.get('target_class_id', 'all').strip()
    allowed_fields = request.form.getlist('allowed_fields')
    
    if allowed_fields:
        AppSetting.set_value('student_update_allowed_fields', ",".join(allowed_fields), "Champs modifiables par les étudiants")
    if 'pin' in request.form:
        AppSetting.set_value('student_update_pin', pin, "PIN session mise à jour profil")
    if target_class_id:
        AppSetting.set_value('student_update_target_class_id', target_class_id, "Classe cible session mise à jour")

    return jsonify({
        'success': True,
        'enabled': new_status,
        'message': "Session de mise à jour OUVERTE" if new_status else "Session de mise à jour FERMÉE"
    })

@registration_bp.route('/registration/update-qr')
def update_qr():
    """Génère le QR Code menant directement à la page de mise à jour de profil."""
    local_ip = get_local_ip()
    port = request.host.split(':')[1] if ':' in request.host else '5000'
    url = f"http://{local_ip}:{port}/profil/maj"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=3,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0f172a", back_color="#ffffff")

    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')

