import os
import io
import json
import zipfile
import shutil
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, current_app, session
from database import db, Class, CourseModule, CourseSection, ActivityType, Objective, Competency, GeneralCompetency, AppSetting, SchoolYear, Institution, TimeSlot, ScheduleEntry, User, LoginLog

configuration_bp = Blueprint('configuration', __name__)

@configuration_bp.route('/pedagogy')
def pedagogy_page():
    return redirect(url_for('configuration.settings') + '#tab-pedagogy')

from extensions import mail
from flask_mail import Message  # type: ignore[import-untyped]
from database import Student, Absence

DEFAULT_MAIL_TEMPLATES = {
    '1': {
        'name': 'Formel & Réglementaire',
        'subject': "Avertissement d'absence — {classe}",
        'body': """Bonjour **{prenom} {nom}**,

Nous constatons votre absence aux séances suivantes :
{liste_dates}

Total d'absences : **{total_absences}** (Note d'assiduité actuelle : **{note_assiduite}/20**).

Rappel du règlement pédagogique : toute absence non justifiée sous 48h auprès de l'administration entraîne une déduction sur la note d'assiduité. Trois absences consécutives non justifiées entraîneront une note d'activité égale à **0/20**.

Veuillez régulariser votre situation au plus vite auprès du secrétariat.

Cordialement,
**{nom_enseignant}**"""
    },
    '2': {
        'name': 'Pédagogique & Bienveillant',
        'subject': "Suivi pédagogique / Absences constatées — {classe}",
        'body': """Bonjour **{prenom}**,

Nous avons remarqué votre absence récente lors de nos séances en classe de **{classe}** :
{liste_dates}

Bilan : **{total_absences} absence(s)** enregistrée(s) avec une note d'assiduité de **{note_assiduite}/20**.

Si tu rencontres des difficultés particulières, n'hésite surtout pas à venir m'en parler afin que nous puissions trouver des solutions et rattraper les cours manqués. Pense également à transmettre ton justificatif médical ou administratif.

Bon courage et à très bientôt en classe !

Bien cordialement,
**{nom_enseignant}**"""
    },
    '3': {
        'name': 'Alerte Décrochage / Urgence',
        'subject': "URGENT : Alerte absences répétées — Risque de défaillance — {classe}",
        'body': """Bonjour **{prenom} {nom}** (CNE: **{cne}**),

Ce message constitue une **mise en demeure formelle** concernant vos absences répétées en classe de **{classe}**.

Bilan académique :
- Total des absences : **{total_absences} séance(s)**
- Note d'assiduité : **{note_assiduite}/20**
{liste_dates}

Votre assiduité est désormais **critique** et compromet gravement la validation de votre semestre. Vous êtes prié(e) de vous présenter dès la prochaine séance muni(e) de vos justificatifs officiels.

L'enseignant responsable,
**{nom_enseignant}**"""
    },
    '4': {
        'name': 'Synthétique & Direct',
        'subject': "Relevé des absences — {prenom} {nom}",
        'body': """Bonjour **{prenom}**,

Voici le récapitulatif officiel de vos absences enregistrées pour la classe **{classe}** :
{liste_dates}

Total : **{total_absences} séance(s)** • Note assiduité : **{note_assiduite}/20**. Justificatifs requis sous 48h.

**{nom_enseignant}**"""
    }
}

MAIL_THEMES = {
    'campus': {
        'id': 'campus',
        'name': 'Campus Bleu (Lycée & Étudiant)',
        'subtitle': 'Moderne, dynamique, inspirant confiance',
        'primary': '#4361ee',
        'accent': '#3f37c9',
        'header_bg': 'linear-gradient(135deg, #4361ee 0%, #3a0ca3 100%)',
        'bg_page': '#f4f6fb',
        'badge_bg': '#eef2ff',
        'badge_text': '#4361ee',
        'border': '#dbeafe',
        'card_bg': '#ffffff',
        'card_text': '#2b2d42',
        'table_header': '#eef2ff',
        'icon': '🎓'
    },
    'mint': {
        'id': 'mint',
        'name': 'Fresh Menthe (Écoute & Motivation)',
        'subtitle': 'Apaisant, bienveillant, orienté progrès',
        'primary': '#059669',
        'accent': '#10b981',
        'header_bg': 'linear-gradient(135deg, #059669 0%, #10b981 100%)',
        'bg_page': '#f0fdf4',
        'badge_bg': '#d1fae5',
        'badge_text': '#065f46',
        'border': '#a7f3d0',
        'card_bg': '#ffffff',
        'card_text': '#1f2937',
        'table_header': '#ecfdf5',
        'icon': '🌱'
    },
    'neon': {
        'id': 'neon',
        'name': 'Cyber Focus (Dark & Énergique)',
        'subtitle': 'Style sombre néo-gaming très apprécié des 14-19 ans',
        'primary': '#6366f1',
        'accent': '#8b5cf6',
        'header_bg': 'linear-gradient(135deg, #1e1b4b 0%, #312e81 100%)',
        'bg_page': '#0f172a',
        'card_bg': '#1e293b',
        'card_text': '#f1f5f9',
        'badge_bg': '#3730a3',
        'badge_text': '#c7d2fe',
        'border': '#334155',
        'table_header': '#312e81',
        'icon': '⚡'
    },
    'sunset': {
        'id': 'sunset',
        'name': 'Sunset Orange (Vigilance & Positif)',
        'subtitle': 'Attire immédiatement l\'œil sans stresser',
        'primary': '#ea580c',
        'accent': '#f97316',
        'header_bg': 'linear-gradient(135deg, #ea580c 0%, #f97316 100%)',
        'bg_page': '#fff7ed',
        'badge_bg': '#ffedd5',
        'badge_text': '#9a3412',
        'border': '#fed7aa',
        'card_bg': '#ffffff',
        'card_text': '#2b2d42',
        'table_header': '#ffedd5',
        'icon': '🔔'
    }
}

def format_dates_table_html(raw_dates_str, is_dark=False):
    """Transforme la chaîne des dates avec tirets en un mini-tableau stylisé avec badges."""
    lines = [l.strip() for l in raw_dates_str.strip().split('\n') if l.strip()]
    if not lines or (len(lines) == 1 and 'aucune' in lines[0].lower()):
        return '<div style="padding: 12px; background: #f8fafc; border-radius: 8px; color: #64748b; font-size: 13px;">Aucune absence à signaler.</div>'

    rows_html = ""
    for line in lines:
        cleaned = line.lstrip('- ').strip()
        is_justified = "(Justifiée)" in cleaned
        date_part = cleaned.replace("(Justifiée)", "").replace("(Non justifiée)", "").strip()
        
        if is_justified:
            status_badge = '<span style="display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; background-color: #dcfce7; color: #15803d; border: 1px solid #bbf7d0;">✔ Justifiée</span>'
        else:
            status_badge = '<span style="display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; background-color: #fee2e2; color: #b91c1c; border: 1px solid #fecaca;">✖ Non justifiée</span>'

        td_bg = '#1e293b' if is_dark else '#ffffff'
        td_border = '#334155' if is_dark else '#e2e8f0'
        td_text = '#f8fafc' if is_dark else '#1e293b'

        rows_html += f"""<tr style="border-bottom: 1px solid {td_border};">
            <td style="padding: 8px 12px; color: {td_text}; font-size: 13px; font-weight: 600; background-color: {td_bg}; vertical-align: middle;">
                📅 {date_part}
            </td>
            <td style="padding: 8px 12px; text-align: right; background-color: {td_bg}; vertical-align: middle;">
                {status_badge}
            </td>
        </tr>"""

    th_bg = '#312e81' if is_dark else '#f1f5f9'
    th_text = '#c7d2fe' if is_dark else '#475569'
    table_border = '#334155' if is_dark else '#cbd5e1'

    return f"""<div style="margin: 10px 0; border-radius: 8px; overflow: hidden; border: 1px solid {table_border};">
        <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="border-collapse: collapse; margin: 0;">
            <thead>
                <tr style="background-color: {th_bg}; border-bottom: 2px solid {table_border};">
                    <th style="padding: 7px 12px; text-align: left; font-size: 11px; font-weight: 700; text-transform: uppercase; color: {th_text}; letter-spacing: 0.5px;">Séance manquée</th>
                    <th style="padding: 7px 12px; text-align: right; font-size: 11px; font-weight: 700; text-transform: uppercase; color: {th_text}; letter-spacing: 0.5px;">Statut</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>"""

def render_html_mail(subject, body_text, theme_key='campus', placeholders=None):
    import re
    theme = MAIL_THEMES.get(theme_key, MAIL_THEMES['campus'])
    is_dark = (theme_key == 'neon')
    card_bg = theme.get('card_bg', '#ffffff')
    card_text = theme.get('card_text', '#2b2d42')

    classe = placeholders.get('{classe}', '') if placeholders else ''
    prenom = placeholders.get('{prenom}', '') if placeholders else 'Élève'
    nom = placeholders.get('{nom}', '') if placeholders else ''
    cne = placeholders.get('{cne}', '') if placeholders else ''
    raw_dates = placeholders.get('{liste_dates}', '') if placeholders else ''
    total_abs = placeholders.get('{total_absences}', '0') if placeholders else '0'
    score_str = placeholders.get('{note_assiduite}', '20.0') if placeholders else '20.0'

    try:
        score_val = float(score_str)
    except:
        score_val = 20.0

    # Calcul pourcentage de la jauge (0 à 100%)
    score_pct = max(0, min(100, int((score_val / 20.0) * 100)))
    if score_val >= 16:
        bar_color = '#10b981' # Vert
        score_status = 'Excellente / Régulière'
    elif score_val >= 12:
        bar_color = '#f59e0b' # Orange
        score_status = 'Attention requise'
    else:
        bar_color = '#ef4444' # Rouge alerte
        score_status = 'Critique / Décrochage'

    # Génération du mini-tableau stylisé des dates
    table_dates_html = format_dates_table_html(raw_dates, is_dark=is_dark)

    # Remplacement des **texte** en <strong>texte</strong> pour le corps
    body_formatted = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', body_text)
    
    # Remplacer les sauts de lignes d'abord
    body_formatted = body_formatted.replace('\n', '<br>')

    # Remplacement précis du placeholder ou de la chaîne de dates pour éviter les double <br>
    raw_dates_br = raw_dates.replace('\n', '<br>')
    if raw_dates_br and raw_dates_br in body_formatted:
        body_formatted = body_formatted.replace(raw_dates_br, table_dates_html)
    elif raw_dates and raw_dates in body_formatted:
        body_formatted = body_formatted.replace(raw_dates, table_dates_html)
    else:
        body_formatted = body_formatted.replace('{liste_dates}', table_dates_html)

    # Nettoyage des <br> superflus autour du tableau
    body_formatted = body_formatted.replace('<br>' + table_dates_html, table_dates_html)
    body_formatted = body_formatted.replace(table_dates_html + '<br>', table_dates_html)

    # Option B : Carte d'identité étudiant stylisée en haut
    student_card_bg = '#1e293b' if is_dark else '#f8fafc'
    student_card_border = '#334155' if is_dark else '#e2e8f0'
    student_title_color = '#94a3b8' if is_dark else '#64748b'

    student_id_box = f"""
    <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color: {student_card_bg}; border: 1px solid {student_card_border}; border-radius: 12px; margin-bottom: 20px; overflow: hidden;">
        <tr>
            <td style="padding: 14px 18px; border-right: 1px solid {student_card_border}; width: 50%;">
                <div style="font-size: 11px; text-transform: uppercase; font-weight: 700; color: {student_title_color}; letter-spacing: 0.5px;">👤 Étudiant</div>
                <div style="font-size: 15px; font-weight: 800; color: {card_text}; margin-top: 2px;">{prenom} {nom}</div>
                <div style="font-size: 12px; color: {student_title_color}; margin-top: 1px;">CNE : <strong style="color: {theme['primary']};">{cne}</strong></div>
            </td>
            <td style="padding: 14px 18px; width: 50%;">
                <div style="font-size: 11px; text-transform: uppercase; font-weight: 700; color: {student_title_color}; letter-spacing: 0.5px;">🏫 Classe & Promotion</div>
                <div style="font-size: 15px; font-weight: 800; color: {card_text}; margin-top: 2px;">{classe or 'Non assignée'}</div>
                <div style="font-size: 12px; color: {student_title_color}; margin-top: 1px;">Absences cumulées : <strong style="color: #ef4444;">{total_abs}</strong></div>
            </td>
        </tr>
    </table>
    """

    # Option C : Barre de Jauge de progression d'assiduité
    gauge_box = f"""
    <div style="background-color: {student_card_bg}; border: 1px solid {student_card_border}; border-radius: 12px; padding: 14px 18px; margin-bottom: 22px;">
        <div style="display: table; width: 100%;">
            <div style="display: table-cell; vertical-align: middle;">
                <span style="font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: {student_title_color};">Note d'assiduité</span>
                <span style="font-size: 11px; font-weight: 600; color: {bar_color}; margin-left: 6px;">({score_status})</span>
            </div>
            <div style="display: table-cell; text-align: right; vertical-align: middle;">
                <span style="font-size: 18px; font-weight: 800; color: {bar_color};">{score_val} <span style="font-size: 13px; color: {student_title_color};">/ 20</span></span>
            </div>
        </div>
        <!-- Barre de progression -->
        <div style="height: 8px; background-color: {'#334155' if is_dark else '#e2e8f0'}; border-radius: 4px; overflow: hidden; margin-top: 10px;">
            <div style="height: 100%; width: {score_pct}%; background-color: {bar_color}; border-radius: 4px;"></div>
        </div>
    </div>
    """

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{subject}</title>
</head>
<body style="margin: 0; padding: 25px 15px; background-color: {theme['bg_page']}; font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
    <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 600px; margin: 0 auto;">
        <tr>
            <td>
                <!-- Carte Principale -->
                <table role="presentation" border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color: {card_bg}; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 25px rgba(0,0,0,0.08); border: 1px solid {theme['border']};">
                    
                    <!-- En-tête Dynamique -->
                    <tr>
                        <td style="background: {theme['header_bg']}; padding: 26px 24px; text-align: left; color: #ffffff;">
                            <div style="font-size: 26px; margin-bottom: 6px;">{theme['icon']}</div>
                            <h1 style="margin: 0; font-size: 20px; font-weight: 700; color: #ffffff; letter-spacing: -0.3px;">Suivi Pédagogique & Présences</h1>
                            <div style="font-size: 13px; color: rgba(255,255,255,0.85); margin-top: 4px;">
                                Espace Scolaire & Assiduité {('— ' + classe) if classe else ''}
                            </div>
                        </td>
                    </tr>

                    <!-- Corps de l'email -->
                    <tr>
                        <td style="padding: 24px; color: {card_text}; font-size: 14.5px; line-height: 1.65;">
                            
                            <!-- Option B : Fiche Identité Étudiant -->
                            {student_id_box}

                            <!-- Option C : Jauge de progression d'Assiduité -->
                            {gauge_box}

                            <!-- Texte du message (avec noms en gras et dates en mini-tableau) -->
                            <div style="margin-bottom: 20px;">
                                {body_formatted}
                            </div>

                            <!-- Bloc Conseil / Bon réflexe pour les ados -->
                            <div style="background-color: {theme['bg_page']}; border-left: 4px solid {theme['primary']}; border-radius: 6px; padding: 12px 16px; margin-top: 24px; font-size: 12.5px; color: {card_text}; opacity: 0.95;">
                                💡 <strong>Bon réflexe :</strong> En cas d'imprévu ou de motif médical, déposez votre justificatif auprès de l'administration sous 48 heures pour régulariser votre dossier.
                            </div>
                        </td>
                    </tr>

                    <!-- Pied de page -->
                    <tr>
                        <td style="background-color: {theme['bg_page']}; padding: 16px 24px; border-top: 1px solid {theme['border']}; text-align: center; font-size: 12px; color: #8d99ae;">
                            Ce message officiel vous est adressé dans le cadre du suivi de scolarité
                        </td>
                    </tr>

                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""
    return html

def get_sqlite_db_path():
    """Résout le chemin absolu du fichier SQLite absence.db."""
    db_uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', 'sqlite:///absence.db')
    if db_uri.startswith('sqlite:///'):
        rel_path = db_uri.replace('sqlite:///', '')
        db_path = os.path.join(current_app.instance_path, rel_path) if not os.path.isabs(rel_path) else rel_path
        if not os.path.exists(db_path):
            root_db = os.path.join(current_app.root_path, rel_path)
            if os.path.exists(root_db):
                db_path = root_db
    else:
        db_path = os.path.join(current_app.instance_path, 'absence.db')
    return db_path

def get_current_school_year():
    """Récupère l'année scolaire active pour la session courante."""
    year_id = session.get('active_school_year_id')
    if year_id:
        y = SchoolYear.query.get(year_id)
        if y: return y
    active = SchoolYear.query.filter_by(is_active=True).first()
    if active: return active
    return SchoolYear.query.order_by(SchoolYear.start_date.desc()).first()

@configuration_bp.route('/settings', methods=['GET'])
def settings():
    modules = CourseModule.query.order_by(CourseModule.name).all()
    sections = CourseSection.query.order_by(CourseSection.name).all()
    types = ActivityType.query.order_by(ActivityType.name).all()
    objectives = Objective.query.all()
    competencies = Competency.query.all()
    general_competencies = GeneralCompetency.query.all()

    current_year = get_current_school_year()
    if current_year:
        classes = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
    else:
        classes = []

    absence_penalty = float(AppSetting.get_value('absence_penalty', '0.5'))
    absence_alert_threshold = int(AppSetting.get_value('absence_alert_threshold', '3'))

    
    # Paramètres Email
    mail_frequency = AppSetting.get_value('mail_frequency', 'daily') # daily, weekly, monthly, semester, disabled
    active_mail_template = AppSetting.get_value('mail_active_template', '1') # '1', '2', '3', '4'
    mail_theme = AppSetting.get_value('mail_theme', 'campus') # campus, mint, neon, sunset
    mail_teacher_copy = AppSetting.get_value('mail_teacher_copy', '0') == '1' # Bcc teacher
    mail_only_threshold = AppSetting.get_value('mail_only_threshold', '0') == '1' # Only alert if >= threshold
    mail_antispam_days = int(AppSetting.get_value('mail_antispam_days', '0')) # min days between emails (0 = no limit)
    teacher_name = AppSetting.get_value('teacher_name', 'Prof ZYANI Azzeddine')

    # Récupération des 4 templates (avec valeurs personnalisées ou défauts)
    mail_templates = {}
    for tid, default_tmpl in DEFAULT_MAIL_TEMPLATES.items():
        mail_templates[tid] = {
            'name': default_tmpl['name'],
            'subject': AppSetting.get_value(f'mail_template_{tid}_subject', default_tmpl['subject']),
            'body': AppSetting.get_value(f'mail_template_{tid}_body', default_tmpl['body'])
        }

    # Thème UI de l'application
    app_ui_theme = AppSetting.get_value('app_ui_theme', 'royal_blue')

    time_slots = TimeSlot.query.order_by(TimeSlot.order_num, TimeSlot.start_time).all()
    schedule_entries = []
    if current_year:
        schedule_entries = ScheduleEntry.query.filter_by(school_year_id=current_year.id).all()

    total_absences_count = Absence.query.count()
    current_year_absences_count = 0
    if current_year:
        current_year_absences_count = Absence.query.join(Student).join(Class).filter(Class.school_year_id == current_year.id).count()

    db_path = get_sqlite_db_path()
    db_size_kb = round(os.path.getsize(db_path) / 1024, 1) if db_path and os.path.exists(db_path) else 0

    user_obj = db.session.get(User, session.get('user_id')) if 'user_id' in session else None
    login_logs = LoginLog.query.order_by(LoginLog.created_at.desc()).limit(10).all()

    return render_template('settings.html', 
                           modules=modules, 
                           sections=sections, 
                           types=types, 
                           classes=classes, 
                           objectives=objectives, 
                           competencies=competencies, 
                           general_competencies=general_competencies, 
                           time_slots=time_slots,
                           schedule_entries=schedule_entries,
                           current_school_year=current_year,
                           absence_penalty=absence_penalty, 
                           absence_alert_threshold=absence_alert_threshold,
                           mail_frequency=mail_frequency,
                           active_mail_template=active_mail_template,
                           mail_theme=mail_theme,
                           mail_themes=MAIL_THEMES,
                           mail_teacher_copy=mail_teacher_copy,
                           mail_only_threshold=mail_only_threshold,
                           mail_antispam_days=mail_antispam_days,
                           teacher_name=teacher_name,
                           mail_templates=mail_templates,
                           app_ui_theme=app_ui_theme,
                           total_absences_count=total_absences_count,
                           current_year_absences_count=current_year_absences_count,
                           db_size_kb=db_size_kb,
                           user_obj=user_obj,
                           login_logs=login_logs)

# =========================================================================
# GESTION DES ANNÉES SCOLAIRES (HISTORISATION & ARCHIVAGE)
# =========================================================================

@configuration_bp.route('/settings/years/add', methods=['POST'])
def add_school_year():
    """Crée une nouvelle année scolaire."""
    name = request.form.get('name', '').strip()
    start_date_raw = request.form.get('start_date', '').strip()
    end_date_raw = request.form.get('end_date', '').strip()
    set_as_active = request.form.get('set_as_active') == '1'

    if not name or not start_date_raw or not end_date_raw:
        flash("Veuillez renseigner le libellé et les dates de l'année scolaire.", 'error')
        return redirect(url_for('configuration.settings') + '#tab-years')

    existing = SchoolYear.query.filter_by(name=name).first()
    if existing:
        flash(f"L'année scolaire '{name}' existe déjà.", 'warning')
        return redirect(url_for('configuration.settings') + '#tab-years')

    try:
        start_date = datetime.strptime(start_date_raw, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_raw, '%Y-%m-%d').date()
        if end_date <= start_date:
            flash("La date de fin doit être postérieure à la date de début.", 'error')
            return redirect(url_for('configuration.settings') + '#tab-years')
    except ValueError:
        flash("Format de date invalide.", 'error')
        return redirect(url_for('configuration.settings') + '#tab-years')

    inst = Institution.query.first()
    if not inst:
        inst = Institution(name="Mon Établissement")
        db.session.add(inst)
        db.session.flush()

    # Si marquée active, désactiver les autres
    if set_as_active:
        SchoolYear.query.update({SchoolYear.is_active: False})

    new_year = SchoolYear(
        name=name,
        start_date=start_date,
        end_date=end_date,
        institution_id=inst.id,
        is_active=set_as_active,
        is_archived=False
    )
    db.session.add(new_year)
    db.session.commit()

    if set_as_active:
        session['active_school_year_id'] = new_year.id

    flash(f"Année scolaire '{name}' créée avec succès.", 'success')
    return redirect(url_for('configuration.settings') + '#tab-years')

@configuration_bp.route('/settings/years/set-active/<int:year_id>', methods=['POST'])
def set_active_year(year_id):
    """Définit une année comme l'année par défaut en cours."""
    target_year = SchoolYear.query.get_or_404(year_id)
    SchoolYear.query.update({SchoolYear.is_active: False})
    target_year.is_active = True
    target_year.is_archived = False # Une année active ne peut pas être archivée
    db.session.commit()

    session['active_school_year_id'] = target_year.id
    flash(f"L'année scolaire '{target_year.name}' est désormais l'année active par défaut.", 'success')
    return redirect(url_for('configuration.settings') + '#tab-years')

@configuration_bp.route('/settings/years/toggle-archive/<int:year_id>', methods=['POST'])
def toggle_archive_year(year_id):
    """Verrouille ou déverrouille une année scolaire en lecture seule."""
    year = SchoolYear.query.get_or_404(year_id)
    if year.is_active:
        flash("Impossible d'archiver l'année scolaire actuellement active par défaut.", 'warning')
        return redirect(url_for('configuration.settings') + '#tab-years')

    year.is_archived = not year.is_archived
    db.session.commit()

    state_str = "verrouillée en lecture seule (archivée)" if year.is_archived else "déverrouillée"
    flash(f"L'année scolaire '{year.name}' a été {state_str}.", 'info')
    return redirect(url_for('configuration.settings') + '#tab-years')

@configuration_bp.route('/settings/years/delete/<int:year_id>', methods=['POST'])
def delete_year(year_id):
    """Supprime une année scolaire si elle ne contient pas de données."""
    year = SchoolYear.query.get_or_404(year_id)
    if year.is_active:
        flash("Impossible de supprimer l'année scolaire active.", 'error')
        return redirect(url_for('configuration.settings') + '#tab-years')
    
    if len(year.sessions) > 0:
        flash(f"Impossible de supprimer l'année '{year.name}' car elle contient {len(year.sessions)} séance(s) de cours.", 'error')
        return redirect(url_for('configuration.settings') + '#tab-years')

    db.session.delete(year)
    db.session.commit()
    flash(f"Année scolaire '{year.name}' supprimée.", 'success')
    return redirect(url_for('configuration.settings') + '#tab-years')

@configuration_bp.route('/settings/classes/copy-from-year', methods=['POST'])
def copy_classes_from_year():
    """Copie la structure des classes d'une autre année vers l'année scolaire active."""
    current_year = get_current_school_year()
    if not current_year:
        flash("Veuillez sélectionner ou créer une année scolaire cible.", 'error')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    if current_year.is_read_only():
        flash("Impossible d'ajouter des classes à une année scolaire verrouillée en lecture seule.", 'error')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    source_year_id = request.form.get('source_year_id')
    if not source_year_id:
        flash("Veuillez sélectionner une année scolaire source.", 'warning')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    source_year = SchoolYear.query.get_or_404(int(source_year_id))
    if source_year.id == current_year.id:
        flash("L'année source et l'année cible sont identiques.", 'warning')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    source_classes = Class.query.filter_by(school_year_id=source_year.id).all()
    if not source_classes:
        flash(f"L'année '{source_year.name}' ne contient aucune classe à dupliquer.", 'info')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    existing_names = {c.name for c in Class.query.filter_by(school_year_id=current_year.id).all()}
    copied_count = 0

    for sc in source_classes:
        if sc.name not in existing_names:
            new_cls = Class(name=sc.name, school_year_id=current_year.id)
            db.session.add(new_cls)
            copied_count += 1

    db.session.commit()
    flash(f"{copied_count} classe(s) reconduite(s) avec succès depuis '{source_year.name}' vers '{current_year.name}'.", 'success')
    return redirect(url_for('configuration.settings') + '#tab-classes')


@configuration_bp.route('/settings/save_general', methods=['POST'])
def save_general():
    penalty_str = request.form.get('absence_penalty', '0.5').strip().replace(',', '.')
    threshold_str = request.form.get('absence_alert_threshold', '3').strip()
    try:
        penalty = float(penalty_str)
        if penalty < 0:
            penalty = 0.0
        AppSetting.set_value('absence_penalty', penalty, description='Sanction note déduite par absence non justifiée')
        
        threshold = int(threshold_str)
        if threshold < 1:
            threshold = 1
        AppSetting.set_value('absence_alert_threshold', threshold, description='Seuil d\'absences pour déclencher une alerte de décrochage')
        
        flash(f'Paramètres enregistrés : Sanction = {penalty} pt(s), Seuil d\'alerte = {threshold} absence(s).', 'success')
    except ValueError:
        flash('Valeurs invalides. Veuillez entrer des nombres valides.', 'error')
    return redirect(url_for('configuration.settings') + '#tab-activities')

@configuration_bp.route('/settings/add', methods=['POST'])
def add_setting():
    category = request.form.get('category')
    name = request.form.get('name')
    module_id = request.form.get('module_id')
    section_id = request.form.get('section_id')
    competency_id = request.form.get('competency_id')
    
    if category != 'schedule_entry' and not name:
        flash('Le nom est requis.', 'error')
        return redirect(url_for('configuration.settings'))
    
    try:
        model = None
        if category == 'module':
            model = CourseModule(name=name)
        elif category == 'section':
            if not module_id:
                flash('Le module est requis pour une section.', 'error')
                return redirect(url_for('configuration.settings'))
            model = CourseSection(name=name, module_id=module_id)
        elif category == 'type':
            model = ActivityType(name=name)
        elif category == 'objective':
            # Support direct link to section (Chapitre)
            if section_id:
                model = Objective(description=name, section_id=int(section_id), competency_id=int(competency_id) if competency_id else None)
            elif competency_id:
                model = Objective(description=name, competency_id=int(competency_id))
            else:
                flash('Le chapitre ou la compétence est requis pour un objectif.', 'error')
                return redirect(url_for('configuration.settings') + '#tab-pedagogy')
        elif category == 'competency':
            if not module_id:
                flash('Le module est requis pour une compétence.', 'error')
                return redirect(url_for('configuration.settings') + '#tab-pedagogy')
            model = Competency(description=name, module_id=module_id)
        elif category == 'general_competency':
            model = GeneralCompetency(description=name)
        elif category == 'time_slot':
            start_t = request.form.get('start_time', '08:30')
            end_t = request.form.get('end_time', '10:30')
            try:
                t1 = datetime.strptime(start_t, '%H:%M')
                t2 = datetime.strptime(end_t, '%H:%M')
                diff_h = (t2 - t1).total_seconds() / 3600.0
                dur_h = round(diff_h, 2) if diff_h > 0 else 2.0
            except Exception:
                dur_h = 2.0
            dur_form = request.form.get('duration_hours')
            if dur_form:
                try:
                    dur_h = float(dur_form)
                except Exception:
                    pass
            model = TimeSlot(name=name, start_time=start_t, end_time=end_t, duration_hours=dur_h)
            
        elif category == 'schedule_entry':
            day_of_week = int(request.form.get('day_of_week', 0))
            time_slot_id = int(request.form.get('time_slot_id'))
            class_id = int(request.form.get('class_id'))
            current_year = get_current_school_year()
            if not current_year:
                flash("Veuillez sélectionner ou créer une année scolaire d'abord.", 'error')
                return redirect(url_for('configuration.settings') + '#tab-schedule')
            room = request.form.get('room', 'Salle Info 1').strip()
            subject_title = request.form.get('subject_title', 'Informatique').strip()
            color = request.form.get('color', '#4e73df')
            
            # Vérifier si un créneau existe déjà au même moment
            existing = ScheduleEntry.query.filter_by(
                day_of_week=day_of_week, 
                time_slot_id=time_slot_id, 
                school_year_id=current_year.id
            ).first()
            if existing:
                existing.class_id = class_id
                existing.room = room
                existing.subject_title = subject_title
                existing.color = color
                model = existing
            else:
                model = ScheduleEntry(
                    day_of_week=day_of_week,
                    time_slot_id=time_slot_id,
                    class_id=class_id,
                    school_year_id=current_year.id,
                    room=room,
                    subject_title=subject_title,
                    color=color
                )
            
        if model:
            db.session.add(model)
            db.session.commit()
            flash('Élément enregistré avec succès.', 'success')
    except Exception as e:
        flash(f'Erreur: {str(e)}', 'error')
        
    redirect_hash = '#tab-pedagogy' if category in ['module', 'section', 'objective', 'competency', 'general_competency'] else ('#tab-timeslots' if category == 'time_slot' else ('#tab-schedule' if category == 'schedule_entry' else ''))
    return redirect(url_for('configuration.settings') + redirect_hash)

@configuration_bp.route('/settings/delete', methods=['POST'])
def delete_setting():
    category = request.form.get('category')
    item_id = request.form.get('id')
    
    if not item_id:
        flash('ID manquant.', 'error')
        return redirect(url_for('configuration.settings'))
    
    try:
        item_id_int = int(item_id)
        model = None
        if category == 'module':
            model = db.session.get(CourseModule, item_id_int)
        elif category == 'section':
            model = db.session.get(CourseSection, item_id_int)
        elif category == 'type':
            model = db.session.get(ActivityType, item_id_int)
        elif category == 'objective':
            model = db.session.get(Objective, item_id_int)
        elif category == 'competency':
            model = db.session.get(Competency, item_id_int)
        elif category == 'general_competency':
            model = db.session.get(GeneralCompetency, item_id_int)
        elif category == 'time_slot':
            model = db.session.get(TimeSlot, item_id_int)
        elif category == 'schedule_entry':
            model = db.session.get(ScheduleEntry, item_id_int)
            
        if model:
            db.session.delete(model)
            db.session.commit()
            flash('Élément supprimé avec succès.', 'success')
    except Exception as e:
        flash(f'Erreur: {str(e)}', 'error')
        
    redirect_hash = '#tab-timeslots' if category == 'time_slot' else ('#tab-schedule' if category == 'schedule_entry' else ('#tab-pedagogy' if category in ['module', 'section', 'objective', 'competency', 'general_competency'] else ''))
    return redirect(url_for('configuration.settings') + redirect_hash)

@configuration_bp.route('/settings/upload', methods=['POST'])
def upload_settings():
    category = request.form.get('category')
    module_id = request.form.get('module_id')
    section_id = request.form.get('section_id')
    competency_id = request.form.get('competency_id')
    file = request.files.get('file')
    
    if file and file.filename and file.filename.endswith('.txt'):
        try:
            content = file.read().decode('utf-8')
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            count = 0
            for name in lines:
                try:
                    model = None
                    if category == 'module':
                        if not CourseModule.query.filter_by(name=name).first():
                            model = CourseModule(name=name)
                    elif category == 'section':
                        if not module_id: continue
                        if not CourseSection.query.filter_by(name=name, module_id=module_id).first():
                            model = CourseSection(name=name, module_id=module_id)
                    elif category == 'type':
                        if not ActivityType.query.filter_by(name=name).first():
                            model = ActivityType(name=name)
                    elif category == 'objective':
                        if not competency_id: continue
                        if not Objective.query.filter_by(description=name, competency_id=competency_id).first():
                            model = Objective(description=name, competency_id=competency_id)
                    elif category == 'competency':
                        if not module_id: continue
                        if not Competency.query.filter_by(description=name, module_id=module_id).first():
                            model = Competency(description=name, module_id=module_id)
                    
                    if model:
                        db.session.add(model)
                        count += 1
                except:
                    pass
            
            db.session.commit()
            flash(f'{count} éléments importés.', 'success')
        except Exception as e:
            flash(f'Erreur lors de l\'import: {str(e)}', 'error')
    else:
        flash('Fichier invalide (.txt requis).', 'error')
        
    return redirect(url_for('configuration.settings'))

@configuration_bp.route('/configuration/photos/upload', methods=['POST'])
def upload_photos():
    files = request.files.getlist('photos')
    if not files or files[0].filename == '':
        flash('Aucun fichier sélectionné.', 'error')
        return redirect(url_for('configuration.settings'))

    import os
    from app import app
    from database import Student
    
    # Ensure photos directory exists
    photos_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'photos')
    if not os.path.exists(photos_dir):
        os.makedirs(photos_dir)
        
    success_count = 0
    errors = []
    
    for file in files:
        if file and file.filename:
            # Filename is CNE (e.g., R13002020.jpg)
            filename = file.filename
            cne = os.path.splitext(filename)[0] # Remove extension
            
            # Find student by CNE (assuming CNE is unique or we take first match)
            # We assume CNE is stored in `massar_number` or similar unique field. 
            # Ideally Student model has a 'cne' field. Using 'cne' attribute if exists.
            student = Student.query.filter_by(cne=cne).first()
            
            if student:
                # Save file
                safe_name = f"{cne}{os.path.splitext(filename)[1]}"
                path = os.path.join(photos_dir, safe_name)
                file.save(path)
                
                # Update student record
                student.photo_path = f"photos/{safe_name}"
                success_count += 1
            else:
                errors.append(filename)
    
    db.session.commit()
    
    if success_count > 0:
        flash(f'{success_count} photos importées avec succès.', 'success')
        
    if errors:
        flash(f'Échec pour {len(errors)} fichiers (CNE introuvable) : {", ".join(errors[:5])}...', 'warning')
        
    return redirect(url_for('configuration.photos_page'))

@configuration_bp.route('/configuration/photos', methods=['GET'])
def photos_page():
    return render_template('photos_upload.html')

@configuration_bp.route('/backup_db', methods=['GET'])
def backup_db():
    try:
        instance_dir = current_app.instance_path
        db_path = os.path.join(instance_dir, 'absence.db')
        if not os.path.exists(db_path):
            # Check root workspace dir
            db_path = os.path.abspath('absence.db')
            
        if not os.path.exists(db_path):
            flash('Fichier de base de données non trouvé pour la sauvegarde.', 'error')
            return redirect(url_for('configuration.settings') + '#tab-account')

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f"absence_backup_{timestamp}.db"
        return send_file(db_path, as_attachment=True, download_name=backup_filename)
    except Exception as e:
        flash(f'Erreur lors du téléchargement de la sauvegarde : {str(e)}', 'error')
        return redirect(url_for('configuration.settings') + '#tab-account')

@configuration_bp.route('/restore_db', methods=['POST'])
def restore_db():
    if 'backup_file' not in request.files:
        flash('Aucun fichier sélectionné.', 'error')
        return redirect(url_for('configuration.settings') + '#tab-account')

    file = request.files['backup_file']
    if not file or file.filename == '':
        flash('Aucun fichier sélectionné.', 'error')
        return redirect(url_for('configuration.settings') + '#tab-account')

    fname = file.filename or ''
    if not (fname.endswith('.db') or fname.endswith('.sqlite') or fname.endswith('.sqlite3')):
        flash('Veuillez fournir un fichier de base de données valide (.db ou .sqlite).', 'error')
        return redirect(url_for('configuration.settings') + '#tab-account')

    try:
        # Determine target db location
        instance_dir = current_app.instance_path
        db_path = os.path.join(instance_dir, 'absence.db')
        if not os.path.exists(db_path):
            db_path = os.path.abspath('absence.db')

        # Create a safety copy of current db before replacing
        if os.path.exists(db_path):
            shutil.copy2(db_path, db_path + '.pre_restore.bak')

        # Dispose active connections so file can be overwritten on Windows
        db.session.remove()
        db.engine.dispose()

        file.save(db_path)
        flash('Base de données restaurée avec succès ! Les modifications sont actives.', 'success')
    except Exception as e:
        flash(f'Erreur lors de la restauration : {str(e)}', 'error')

    return redirect(url_for('configuration.settings') + '#tab-account')

@configuration_bp.route('/settings/save_mail', methods=['POST'])
def save_mail_settings():
    mail_frequency = request.form.get('mail_frequency', 'daily')
    active_mail_template = request.form.get('active_mail_template', '1')
    mail_theme = request.form.get('mail_theme', 'campus')
    mail_teacher_copy = '1' if request.form.get('mail_teacher_copy') == '1' else '0'
    mail_only_threshold = '1' if request.form.get('mail_only_threshold') == '1' else '0'
    mail_antispam_days = request.form.get('mail_antispam_days', '0').strip()
    teacher_name = request.form.get('teacher_name', '').strip()

    AppSetting.set_value('mail_frequency', mail_frequency, description="Fréquence d'envoi des emails d'absence")
    AppSetting.set_value('mail_active_template', active_mail_template, description="Template d'email sélectionné")
    AppSetting.set_value('mail_theme', mail_theme, description="Thème graphique de l'email")
    AppSetting.set_value('mail_teacher_copy', mail_teacher_copy, description="Copie cachée de l'email pour l'enseignant")
    AppSetting.set_value('mail_only_threshold', mail_only_threshold, description="Alerter seulement si le seuil d'alerte est atteint")
    
    try:
        antispam_int = max(0, int(mail_antispam_days))
    except ValueError:
        antispam_int = 0
    AppSetting.set_value('mail_antispam_days', antispam_int, description="Délai minimum en jours entre deux emails pour un même étudiant")

    if teacher_name:
        AppSetting.set_value('teacher_name', teacher_name, description="Nom affiché de l'enseignant")

    # Sauvegarder les 4 templates
    for tid in ['1', '2', '3', '4']:
        subj = request.form.get(f'template_{tid}_subject', '').strip()
        body = request.form.get(f'template_{tid}_body', '').strip()
        if subj:
            AppSetting.set_value(f'mail_template_{tid}_subject', subj, description=f"Sujet du template email {tid}")
        if body:
            AppSetting.set_value(f'mail_template_{tid}_body', body, description=f"Corps du template email {tid}")

    flash("Paramètres et thème des notifications par email enregistrés avec succès.", "success")
    return redirect(url_for('configuration.settings') + '#tab-mail')

@configuration_bp.route('/settings/save_app_theme', methods=['POST'])
def save_app_theme():
    """Enregistre le thème UI de l'application sélectionné."""
    valid_themes = ['royal_blue', 'emerald_fresh', 'obsidian_pro', 'amber_warmth']
    chosen = request.form.get('app_ui_theme', 'royal_blue').strip()
    if chosen not in valid_themes:
        chosen = 'royal_blue'
    AppSetting.set_value('app_ui_theme', chosen, description="Thème graphique de l'interface de l'application")
    flash("Thème appliqué avec succès !", "success")
    return redirect(url_for('configuration.settings') + '#tab-theme')

@configuration_bp.route('/settings/test_mail', methods=['POST'])
def test_mail():
    test_email = request.form.get('test_email', '').strip()
    if not test_email:
        test_email = current_app.config.get('MAIL_USERNAME', 'azzeddine.zyani@gmail.com')

    tid = AppSetting.get_value('mail_active_template', '1')
    theme_key = AppSetting.get_value('mail_theme', 'campus')
    default_tmpl = DEFAULT_MAIL_TEMPLATES.get(tid, DEFAULT_MAIL_TEMPLATES['1'])
    subj_tmpl = AppSetting.get_value(f'mail_template_{tid}_subject', default_tmpl['subject'])
    body_tmpl = AppSetting.get_value(f'mail_template_{tid}_body', default_tmpl['body'])
    teacher_name = AppSetting.get_value('teacher_name', 'Prof ZYANI Azzeddine')

    # Valeurs de simulation pour l'aperçu/test
    sample_data = {
        '{prenom}': 'Karim',
        '{nom}': 'EL IDRISSI',
        '{classe}': '2BAC-SM',
        '{cne}': 'R130123456',
        '{liste_dates}': "- Lundi 08 Septembre 2026 (Non justifiée)\n- Mercredi 10 Septembre 2026 (Non justifiée)",
        '{total_absences}': '2',
        '{note_assiduite}': '19.0',
        '{nom_enseignant}': teacher_name
    }

    subject = subj_tmpl
    body = body_tmpl
    for k, v in sample_data.items():
        subject = subject.replace(k, v)
        body = body.replace(k, v)

    html_content = render_html_mail(subject, body, theme_key=theme_key, placeholders=sample_data)

    try:
        msg = Message(
            subject=f"[TEST] {subject}",
            sender=current_app.config.get('MAIL_USERNAME') or 'noreply@school.com',
            recipients=[test_email]
        )
        msg.body = body + "\n\n---\n*Ceci est un email de test généré depuis votre espace de configuration.*"
        msg.html = html_content
        mail.send(msg)
        flash(f"Email de test envoyé avec succès à {test_email} (Thème appliqué : {MAIL_THEMES.get(theme_key, {}).get('name', theme_key)}) !", "success")
    except Exception as e:
        flash(f"Échec de l'envoi de test : {str(e)}", "error")

    return redirect(url_for('configuration.settings') + '#tab-mail')

@configuration_bp.route('/optimize_database', methods=['POST'])
def optimize_database():
    """Défragmente, compacte et réindexe la base SQLite (VACUUM, ANALYZE, PRAGMA optimize)."""
    db_path = get_sqlite_db_path()
    size_before = os.path.getsize(db_path) if db_path and os.path.exists(db_path) else 0

    try:
        from sqlalchemy import text
        start_time = datetime.now()

        # 1. Exécuter ANALYZE et PRAGMA optimize
        with db.engine.connect() as conn:
            conn.execute(text("ANALYZE"))
            conn.execute(text("PRAGMA optimize"))
            conn.commit()

        # 2. Exécuter VACUUM via connection bas-niveau SQLite
        raw_conn = db.engine.raw_connection()
        try:
            raw_cursor = raw_conn.cursor()
            raw_cursor.execute("VACUUM")
            raw_cursor.close()
            raw_conn.commit()
        finally:
            raw_conn.close()

        size_after = os.path.getsize(db_path) if db_path and os.path.exists(db_path) else 0
        duration = (datetime.now() - start_time).total_seconds()

        saved_bytes = max(0, size_before - size_after)
        saved_str = f" ({saved_bytes / 1024:.1f} Ko libérés)" if saved_bytes > 0 else " (Base déjà compacte)"

        flash(
            f"Optimisation réussie en {duration:.2f}s ! Taille actuelle : {size_after / 1024:.1f} Ko{saved_str}. "
            "Les index et statistiques de requêtes ont été entièrement actualisés pour un fonctionnement ultra-rapide.",
            "success"
        )
    except Exception as e:
        flash(f"Erreur lors de l'optimisation : {str(e)}", "error")

    return redirect(url_for('configuration.settings') + '#tab-backup')

@configuration_bp.route('/backup_database', methods=['GET'])
def backup_database():
    """Télécharge une sauvegarde complète (ZIP avec base et photos) ou la base SQLite seule (.db)."""
    backup_type = request.args.get('type', 'full')  # 'full' ou 'db'
    db_path = get_sqlite_db_path()

    if not db_path or not os.path.exists(db_path):
        flash("Fichier de base de données introuvable sur le serveur.", "error")
        return redirect(url_for('configuration.settings') + '#tab-backup')

    now_str = datetime.now().strftime('%Y%m%d_%H%M%S')

    if backup_type == 'db':
        filename = f"backup_absence_{now_str}.db"
        return send_file(db_path, as_attachment=True, download_name=filename, mimetype='application/x-sqlite3')

    # Sauvegarde Complète en archive ZIP (Base + Photos + Manifeste)
    try:
        mem_zip = io.BytesIO()
        with zipfile.ZipFile(mem_zip, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
            # 1. Base de données à la racine
            zf.write(db_path, arcname='absence.db')

            # 2. Dossier des uploads (photos des étudiants, logos, documents)
            upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
            total_photos_count = 0
            if os.path.exists(upload_folder):
                for root, _, files in os.walk(upload_folder):
                    for f in files:
                        if f.startswith('.'):
                            continue
                        full_file_path = os.path.join(root, f)
                        rel_in_uploads = os.path.relpath(full_file_path, start=upload_folder)
                        arcname = os.path.join('uploads', rel_in_uploads)
                        zf.write(full_file_path, arcname=arcname)
                        total_photos_count += 1

            # 3. Métadonnées de l'archive
            current_year = get_current_school_year()
            manifest = {
                'application': 'App_Absence',
                'version': '1.0',
                'backup_date': datetime.now().isoformat(),
                'school_year': current_year.name if current_year else 'Non définie',
                'total_students': Student.query.count(),
                'total_absences': Absence.query.count(),
                'total_files': total_photos_count + 1
            }
            zf.writestr('metadata.json', json.dumps(manifest, indent=2, ensure_ascii=False))

        mem_zip.seek(0)
        filename = f"sauvegarde_complete_absence_{now_str}.zip"
        return send_file(mem_zip, as_attachment=True, download_name=filename, mimetype='application/zip')

    except Exception as e:
        flash(f"Erreur lors de la création de l'archive de sauvegarde : {str(e)}", "error")
        return redirect(url_for('configuration.settings') + '#tab-backup')

@configuration_bp.route('/restore_database', methods=['POST'])
def restore_database():
    """Restaure l'application à partir d'une archive complète (.zip) ou d'un fichier base (.db)."""
    uploaded_file = request.files.get('backup_file')
    if not uploaded_file or not uploaded_file.filename:
        flash("Veuillez sélectionner un fichier de sauvegarde (.zip ou .db).", "warning")
        return redirect(url_for('configuration.settings') + '#tab-backup')

    filename = uploaded_file.filename.lower()
    if not (filename.endswith('.zip') or filename.endswith(('.db', '.sqlite', '.sqlite3'))):
        flash("Format invalide. Seules les archives ZIP (.zip) ou bases SQLite (.db, .sqlite) sont autorisées.", "error")
        return redirect(url_for('configuration.settings') + '#tab-backup')

    db_path = get_sqlite_db_path()
    now_str = datetime.now().strftime('%Y%m%d_%H%M%S')

    # 1. Sauvegarde préventive automatique de la base actuelle
    try:
        if os.path.exists(db_path):
            safety_backup = f"{db_path}.auto_safety_{now_str}.bak"
            shutil.copy2(db_path, safety_backup)
    except Exception as e:
        current_app.logger.warning(f"Avertissement sauvegarde de sécurité : {e}")

    # 2. Traitement selon le format
    if filename.endswith('.zip'):
        try:
            upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
            os.makedirs(upload_folder, exist_ok=True)
            upload_abs_dir = os.path.abspath(upload_folder)

            photos_restored = 0
            db_restored = False

            with zipfile.ZipFile(uploaded_file, 'r') as zf:
                file_list = zf.namelist()

                # Recherche du fichier absence.db dans le zip
                db_member = next((m for m in file_list if m.lower() in ('absence.db', 'database.db') or m.lower().endswith('/absence.db')), None)
                if not db_member:
                    flash("L'archive ZIP ne contient aucun fichier de base de données valide (absence.db manquant).", "error")
                    return redirect(url_for('configuration.settings') + '#tab-backup')

                # Extraction de la base de données
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                with zf.open(db_member) as source_db, open(db_path, 'wb') as target_db:
                    shutil.copyfileobj(source_db, target_db)
                db_restored = True

                # Extraction sécurisée des fichiers uploads (anti Zip Slip)
                for member in file_list:
                    if member.startswith('uploads/') and not member.endswith('/'):
                        rel_name = member[len('uploads/'):]
                        target_file_path = os.path.abspath(os.path.join(upload_abs_dir, rel_name))
                        
                        # Vérification de sécurité contre la traversée de répertoires
                        if not target_file_path.startswith(upload_abs_dir):
                            continue

                        os.makedirs(os.path.dirname(target_file_path), exist_ok=True)
                        with zf.open(member) as source_f, open(target_file_path, 'wb') as target_f:
                            shutil.copyfileobj(source_f, target_f)
                        photos_restored += 1

            # Réinitialiser les caches en mémoire (AppSetting, etc.)
            AppSetting.clear_cache()

            flash(
                f"Restauration complète réussie ! Base de données et {photos_restored} fichier(s)/photo(s) réintégrés avec succès.",
                "success"
            )
        except Exception as e:
            flash(f"Erreur lors de la restauration de l'archive ZIP : {str(e)}", "error")

    else:
        # Fichier .db seul
        try:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            uploaded_file.save(db_path)
            AppSetting.clear_cache()
            flash("Base de données restaurée avec succès ! L'application utilise maintenant les données sauvegardées.", "success")
        except Exception as e:
            flash(f"Erreur lors de la restauration de la base : {str(e)}", "error")

    return redirect(url_for('configuration.settings') + '#tab-backup')

@configuration_bp.route('/reset_absences', methods=['POST'])
def reset_absences():
    """Réinitialise les absences (soit pour l'année scolaire active, soit pour l'ensemble de la base)."""
    scope = request.form.get('scope', 'current')  # 'current' or 'all'
    confirmation_word = request.form.get('confirmation_word', '').strip().upper()

    if confirmation_word != 'EFFACER':
        flash("Action annulée : vous devez saisir exactement 'EFFACER' pour confirmer la réinitialisation.", "warning")
        return redirect(url_for('configuration.settings') + '#tab-backup')

    # Sauvegarde automatique de sécurité avant suppression
    db_path = get_sqlite_db_path()

    try:
        if db_path and os.path.exists(db_path):
            safety_backup = f"{db_path}.auto_safety_pre_reset_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
            shutil.copy2(db_path, safety_backup)
    except Exception as e:
        current_app.logger.warning(f"Impossible de créer la sauvegarde préventive : {e}")

    current_year = get_current_school_year()

    try:
        if scope == 'current' and current_year:
            student_ids = [s.id for s in Student.query.join(Class).filter(Class.school_year_id == current_year.id).all()]
            if student_ids:
                deleted_count = Absence.query.filter(Absence.student_id.in_(student_ids)).delete(synchronize_session=False)
            else:
                deleted_count = 0
            db.session.commit()
            flash(f"Succès : {deleted_count} enregistrement(s) d'absence ont été effacés pour l'année scolaire '{current_year.name}'. Les classes et étudiants restent intacts.", "success")
        else:
            deleted_count = Absence.query.delete(synchronize_session=False)
            db.session.commit()
            flash(f"Succès : {deleted_count} enregistrement(s) d'absence ont été intégralement effacés de la base de données. Les classes et étudiants restent intacts.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Erreur lors de la réinitialisation des absences : {str(e)}", "error")

    return redirect(url_for('configuration.settings') + '#tab-backup')

