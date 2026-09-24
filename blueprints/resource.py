import os
import io
import uuid
import socket
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    jsonify, current_app, session, send_file, send_from_directory, abort
)
import qrcode
import qrcode.constants

from database import (
    db, CourseResource, CourseModule, CourseSection, Class, Institution
)

resource_bp = Blueprint('resource', __name__, url_prefix='/resources')

ALLOWED_EXTENSIONS = {
    # Documents
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'rtf', 'odt', 'ods', 'odp', 'csv',
    # Images
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg',
    # Archives & Code
    'zip', 'rar', '7z', 'tar', 'gz', 'py', 'c', 'cpp', 'java', 'html', 'css', 'js', 'json', 'sql'
}

IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_local_ip():
    """Détecte l'IP locale de la machine hôte pour les QR codes sur le réseau WiFi/LAN."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


# ==============================================================================
# ESPACE ENSEIGNANT (AUTHENTIFIÉ)
# ==============================================================================

@resource_bp.route('/')
def index():
    """Tableau de bord de gestion des ressources pédagogiques avec filtres avancés."""
    module_id = request.args.get('module_id', type=int)
    section_id = request.args.get('section_id', type=int)
    resource_type = request.args.get('type', '').strip()
    status_filter = request.args.get('status', 'all').strip()
    search_query = request.args.get('q', '').strip()

    query = CourseResource.query

    if module_id:
        query = query.filter(CourseResource.module_id == module_id)
    if section_id:
        query = query.filter(CourseResource.section_id == section_id)
    if resource_type:
        query = query.filter(CourseResource.resource_type == resource_type)
    if search_query:
        query = query.filter(
            db.or_(
                CourseResource.title.ilike(f"%{search_query}%"),
                CourseResource.description.ilike(f"%{search_query}%"),
                CourseResource.original_filename.ilike(f"%{search_query}%")
            )
        )

    all_resources = query.order_by(CourseResource.created_at.desc()).all()

    # Filtrage en mémoire sur le statut calculé (Actif, Masqué, Expiré)
    now = datetime.now()
    if status_filter == 'active':
        filtered_resources = [r for r in all_resources if r.is_public and not r.is_expired]
    elif status_filter == 'inactive':
        filtered_resources = [r for r in all_resources if not r.is_public]
    elif status_filter == 'expired':
        filtered_resources = [r for r in all_resources if r.is_expired]
    else:
        filtered_resources = all_resources

    # Statistiques globales
    total_resources = CourseResource.query.count()
    active_count = sum(1 for r in CourseResource.query.all() if r.is_public and not r.is_expired)
    total_downloads = db.session.query(db.func.sum(CourseResource.download_count)).scalar() or 0
    expired_count = sum(1 for r in CourseResource.query.all() if r.is_expired)

    # Données pour les filtres et les formulaires
    modules = CourseModule.query.order_by(CourseModule.name).all()
    classes = Class.query.order_by(Class.name).all()

    # Chapitres du module actuellement filtré
    sections_for_filter = []
    if module_id:
        sections_for_filter = CourseSection.query.filter_by(module_id=module_id).order_by(CourseSection.name).all()

    return render_template(
        'resources.html',
        resources=filtered_resources,
        modules=modules,
        classes=classes,
        sections_for_filter=sections_for_filter,
        selected_module_id=module_id,
        selected_section_id=section_id,
        selected_type=resource_type,
        selected_status=status_filter,
        search_query=search_query,
        total_resources=total_resources,
        active_count=active_count,
        total_downloads=total_downloads,
        expired_count=expired_count
    )


@resource_bp.route('/add', methods=['POST'])
def add_resource():
    """Enregistre une nouvelle ressource (fichier ou lien externe)."""
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    resource_type = request.form.get('resource_type', 'Cours').strip()
    module_id = request.form.get('module_id', type=int)
    section_id = request.form.get('section_id', type=int)
    target_class_id = request.form.get('target_class_id', type=int)
    is_public = 'is_public' in request.form
    pin_code = request.form.get('pin_code', '').strip() or None
    expires_at_str = request.form.get('expires_at', '').strip()
    media_source = request.form.get('media_source', 'file') # 'file' ou 'link'

    if not title:
        flash("Le titre de la ressource est obligatoire.", "danger")
        return redirect(url_for('resource.index'))

    if not module_id:
        flash("Veuillez sélectionner un Module de rattachement.", "danger")
        return redirect(url_for('resource.index'))

    # Traitement de la date d'expiration
    expires_at = None
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
        except Exception:
            try:
                expires_at = datetime.strptime(expires_at_str, '%Y-%m-%d %H:%M')
            except Exception:
                pass

    file_path = None
    original_filename = None
    file_size = None
    file_type = 'document'
    external_url = None

    if media_source == 'file':
        uploaded_file = request.files.get('resource_file')
        if not uploaded_file or uploaded_file.filename == '':
            flash("Veuillez sélectionner un fichier à téléverser.", "warning")
            return redirect(url_for('resource.index'))

        if not allowed_file(uploaded_file.filename):
            flash("Format de fichier non autorisé. Formats acceptés : PDF, Word, Excel, PowerPoint, Images, Zip/Rar, Code.", "danger")
            return redirect(url_for('resource.index'))

        original_filename = secure_filename(uploaded_file.filename)
        ext = original_filename.rsplit('.', 1)[1].lower() if '.' in original_filename else ''
        unique_name = f"res_{uuid.uuid4().hex[:10]}_{original_filename}"
        
        resources_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'resources')
        if not os.path.exists(resources_dir):
            os.makedirs(resources_dir, exist_ok=True)

        full_path = os.path.join(resources_dir, unique_name)
        uploaded_file.save(full_path)

        file_path = f"resources/{unique_name}"
        try:
            file_size = os.path.getsize(full_path)
        except Exception:
            file_size = None

        file_type = 'image' if ext in IMAGE_EXTENSIONS else 'document'

    elif media_source == 'link':
        external_url = request.form.get('external_url', '').strip()
        if not external_url:
            flash("Veuillez saisir l'adresse URL du lien externe.", "warning")
            return redirect(url_for('resource.index'))
        file_type = 'link'

    # Génération du jeton secret de partage unique
    share_token = uuid.uuid4().hex[:16]

    resource = CourseResource(
        title=title,
        description=description,
        resource_type=resource_type,
        file_type=file_type,
        file_path=file_path,
        external_url=external_url,
        original_filename=original_filename,
        file_size=file_size,
        module_id=module_id,
        section_id=section_id if section_id else None,
        target_class_id=target_class_id if target_class_id else None,
        is_public=is_public,
        share_token=share_token,
        pin_code=pin_code,
        expires_at=expires_at
    )

    db.session.add(resource)
    db.session.commit()

    flash(f"La ressource « {title} » a été ajoutée avec succès.", "success")
    return redirect(url_for('resource.index', module_id=module_id))


@resource_bp.route('/<int:resource_id>/edit', methods=['POST'])
def edit_resource(resource_id):
    """Met à jour les informations et les paramètres de partage d'une ressource."""
    resource = CourseResource.query.get_or_404(resource_id)

    title = request.form.get('title', '').strip()
    if not title:
        flash("Le titre ne peut pas être vide.", "danger")
        return redirect(url_for('resource.index'))

    resource.title = title
    resource.description = request.form.get('description', '').strip()
    resource.resource_type = request.form.get('resource_type', resource.resource_type).strip()
    resource.module_id = request.form.get('module_id', type=int) or resource.module_id
    
    sec_id = request.form.get('section_id', type=int)
    resource.section_id = sec_id if sec_id else None

    class_id = request.form.get('target_class_id', type=int)
    resource.target_class_id = class_id if class_id else None

    resource.is_public = 'is_public' in request.form
    resource.pin_code = request.form.get('pin_code', '').strip() or None

    expires_at_str = request.form.get('expires_at', '').strip()
    if expires_at_str:
        try:
            resource.expires_at = datetime.fromisoformat(expires_at_str)
        except Exception:
            pass
    else:
        resource.expires_at = None

    # Remplacement éventuel de fichier ou d'URL
    if resource.file_type == 'link':
        ext_url = request.form.get('external_url', '').strip()
        if ext_url:
            resource.external_url = ext_url
    else:
        new_file = request.files.get('resource_file')
        if new_file and new_file.filename != '':
            if allowed_file(new_file.filename):
                # Supprimer l'ancien fichier
                if resource.file_path:
                    old_path = os.path.join(current_app.config['UPLOAD_FOLDER'], resource.file_path)
                    if os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except Exception:
                            pass

                orig_name = secure_filename(new_file.filename)
                ext = orig_name.rsplit('.', 1)[1].lower() if '.' in orig_name else ''
                unique_name = f"res_{uuid.uuid4().hex[:10]}_{orig_name}"
                resources_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'resources')
                full_path = os.path.join(resources_dir, unique_name)
                new_file.save(full_path)

                resource.original_filename = orig_name
                resource.file_path = f"resources/{unique_name}"
                resource.file_size = os.path.getsize(full_path)
                resource.file_type = 'image' if ext in IMAGE_EXTENSIONS else 'document'

    db.session.commit()
    flash(f"La ressource « {resource.title} » a été modifiée.", "success")
    return redirect(url_for('resource.index'))


@resource_bp.route('/<int:resource_id>/delete', methods=['POST'])
def delete_resource(resource_id):
    """Supprime définitivement une ressource et son fichier associé."""
    resource = CourseResource.query.get_or_404(resource_id)

    # Suppression du fichier disque
    if resource.file_path:
        file_full_path = os.path.join(current_app.config['UPLOAD_FOLDER'], resource.file_path)
        if os.path.exists(file_full_path):
            try:
                os.remove(file_full_path)
            except Exception:
                pass

    title = resource.title
    db.session.delete(resource)
    db.session.commit()

    flash(f"La ressource « {title} » a été supprimée.", "info")
    return redirect(url_for('resource.index'))


@resource_bp.route('/<int:resource_id>/toggle-status', methods=['POST'])
def toggle_status(resource_id):
    """Active ou désactive instantanément l'accès public à une ressource (AJAX)."""
    resource = CourseResource.query.get_or_404(resource_id)
    resource.is_public = not resource.is_public
    db.session.commit()

    return jsonify({
        'success': True,
        'is_public': resource.is_public,
        'message': "Ressource activée (visible)" if resource.is_public else "Ressource désactivée (masquée)"
    })


@resource_bp.route('/<int:resource_id>/qr')
def resource_qr(resource_id):
    """Génère l'image PNG du QR Code pointant vers le lien de partage officiel."""
    resource = CourseResource.query.get_or_404(resource_id)
    
    local_ip = get_local_ip()
    port = request.host.split(':')[1] if ':' in request.host else '8080'
    url = f"http://{local_ip}:{port}/resources/partage/{resource.share_token}"

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
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


@resource_bp.route('/api/sections')
def api_sections():
    """Renvoie la liste des chapitres/sections d'un module pour les sélecteurs dynamiques."""
    module_id = request.args.get('module_id', type=int)
    if not module_id:
        return jsonify([])
    
    sections = CourseSection.query.filter_by(module_id=module_id).order_by(CourseSection.name).all()
    return jsonify([{'id': s.id, 'name': s.name} for s in sections])


# ==============================================================================
# ESPACE ÉTUDIANT PUBLIC (SANS AUTHENTIFICATION REQUISE)
# ==============================================================================

@resource_bp.route('/partage/<token>')
def shared_view(token):
    """Portail public d'accès à la ressource partagée avec contrôle de visibilité."""
    resource = CourseResource.query.filter_by(share_token=token).first_or_404()

    # 1. Vérifier si l'enseignant a activé la visibilité
    if not resource.is_public:
        return render_template(
            'resource_shared_disabled.html',
            title="Ressource indisponible",
            reason="Ce support pédagogique est actuellement masqué ou en cours d'actualisation par l'enseignant.",
            resource=resource
        ), 403

    # 2. Vérifier si la ressource est expirée
    if resource.is_expired:
        return render_template(
            'resource_shared_disabled.html',
            title="Accès expiré",
            reason=f"La date limite de consultation fixée au {resource.expires_at.strftime('%d/%m/%Y à %H:%M')} est dépassée.",
            resource=resource
        ), 403

    # 3. Vérifier le code PIN si configuré
    needs_pin = False
    unlocked_tokens = session.get('unlocked_resources', [])
    if resource.pin_code and token not in unlocked_tokens:
        needs_pin = True

    institution = Institution.query.first() or Institution(name="Établissement Scolaire & Supérieur")

    return render_template(
        'resource_shared_view.html',
        resource=resource,
        institution=institution,
        needs_pin=needs_pin,
        token=token
    )


@resource_bp.route('/partage/<token>/verify-pin', methods=['POST'])
def verify_pin(token):
    """Vérifie le code PIN saisi par l'étudiant pour déverrouiller la ressource."""
    resource = CourseResource.query.filter_by(share_token=token).first_or_404()

    entered_pin = request.form.get('pin_code', '').strip()
    if resource.pin_code and entered_pin == resource.pin_code:
        unlocked = session.get('unlocked_resources', [])
        if token not in unlocked:
            unlocked.append(token)
            session['unlocked_resources'] = unlocked
        flash("Accès déverrouillé avec succès.", "success")
    else:
        flash("Code PIN incorrect. Veuillez vérifier auprès de votre enseignant.", "danger")

    return redirect(url_for('resource.shared_view', token=token))


@resource_bp.route('/telecharger/<token>')
def download_file(token):
    """Permet le téléchargement direct ou la prévisualisation inline du document."""
    resource = CourseResource.query.filter_by(share_token=token).first_or_404()

    if not resource.is_accessible:
        abort(403)

    # Vérifier le code PIN
    unlocked_tokens = session.get('unlocked_resources', [])
    if resource.pin_code and token not in unlocked_tokens:
        flash("Veuillez d'abord déverrouiller la ressource avec le code PIN.", "warning")
        return redirect(url_for('resource.shared_view', token=token))

    # Redirection pour les liens externes
    if resource.file_type == 'link' and resource.external_url:
        resource.download_count += 1
        db.session.commit()
        return redirect(resource.external_url)

    if not resource.file_path:
        abort(404)

    file_full_path = os.path.join(current_app.config['UPLOAD_FOLDER'], resource.file_path)
    if not os.path.exists(file_full_path):
        abort(404)

    # Incrémentation du compteur de consultations/téléchargements
    resource.download_count += 1
    db.session.commit()

    view_mode = request.args.get('view', '0') == '1'
    directory = os.path.dirname(file_full_path)
    filename = os.path.basename(file_full_path)

    return send_from_directory(
        directory,
        filename,
        as_attachment=not view_mode,
        download_name=resource.original_filename or filename
    )
