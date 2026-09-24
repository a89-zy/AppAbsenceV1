from flask import Blueprint, render_template, send_from_directory, current_app, session
from database import db, Student, Absence, Class, TextbookSession, AppSetting, SchoolYear, ScheduleEntry, TimeSlot
from datetime import datetime
from sqlalchemy import func

main_bp = Blueprint('main', __name__)

def get_current_school_year():
    """Récupère l'année scolaire active pour la session courante."""
    year_id = session.get('active_school_year_id')
    if year_id:
        y = SchoolYear.query.get(year_id)
        if y: return y
    active = SchoolYear.query.filter_by(is_active=True).first()
    if active: return active
    return SchoolYear.query.order_by(SchoolYear.start_date.desc()).first()

@main_bp.route('/')
def dashboard():
    current_year = get_current_school_year()

    # 1. KPIs globaux (filtrés par l'année scolaire active)
    if current_year:
        classes_list = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
        total_classes = len(classes_list)
        total_students = Student.query.join(Class).filter(Class.school_year_id == current_year.id).count()
    else:
        classes_list = []
        total_classes = 0
        total_students = 0


    abs_base = Absence.query
    if current_year:
        abs_base = abs_base.filter(Absence.date >= current_year.start_date, Absence.date <= current_year.end_date)

    total_absences = abs_base.count()
    unjustified_absences = abs_base.filter_by(justified=False).count()
    justified_absences = total_absences - unjustified_absences

    session_query = TextbookSession.query
    if current_year:
        session_query = session_query.filter_by(school_year_id=current_year.id)
    total_sessions = session_query.count()

    # Seuil configuré
    absence_alert_threshold = int(AppSetting.get_value('absence_alert_threshold', '3'))

    # 2. Élèves avec absences élevées durant l'année scolaire active
    student_abs_query = (
        db.session.query(
            Student,
            func.count(Absence.id).label('total_abs'),
            func.sum(db.case((Absence.justified == False, 1), else_=0)).label('unjustified_abs'),
            func.sum(db.case((Absence.justified == True, 1), else_=0)).label('justified_abs')
        )
        .join(Absence, Student.id == Absence.student_id)
    )
    if current_year:
        student_abs_query = student_abs_query.filter(Absence.date >= current_year.start_date, Absence.date <= current_year.end_date)

    student_abs_query = (
        student_abs_query
        .group_by(Student.id)
        .order_by(func.count(Absence.id).desc())
    )

    all_absent_students = student_abs_query.all()
    # Élèves en alerte (>= seuil configuré en absences non justifiées ou au total)
    alert_students_count = sum(1 for item in all_absent_students if (item.unjustified_abs or 0) >= absence_alert_threshold or item.total_abs >= absence_alert_threshold)
    
    # Top 10 des élèves les plus absents pour le tableau
    top_absent_students = all_absent_students[:10]

    # 3. Répartition des absences par classe durant l'année scolaire active
    class_stats_query = (
        db.session.query(
            Class.id,
            Class.name,
            func.count(Absence.id).label('abs_count')
        )
        .join(Student, Class.id == Student.class_id)
        .join(Absence, Student.id == Absence.student_id)
    )
    if current_year:
        class_stats_query = class_stats_query.filter(
            Class.school_year_id == current_year.id,
            Absence.date >= current_year.start_date,
            Absence.date <= current_year.end_date
        )

    class_stats = (
        class_stats_query
        .group_by(Class.id, Class.name)
        .order_by(func.count(Absence.id).desc())
        .all()
    )

    class_names = [c[1] for c in class_stats]
    class_abs_counts = [c[2] for c in class_stats]
    total_class_abs = sum(class_abs_counts) if class_abs_counts else 0
    class_stats_details = [
        {
            'id': c[0],
            'name': c[1],
            'count': c[2],
            'percentage': round((c[2] / total_class_abs) * 100, 1) if total_class_abs > 0 else 0
        }
        for c in class_stats
    ]

    # Détection du cours en cours & planning du jour
    today = datetime.now().date()
    now_time_str = datetime.now().strftime('%H:%M')
    today_weekday = today.weekday() # 0 = Lundi ... 5 = Samedi
    today_schedule = []
    active_course = None

    if current_year and today_weekday < 6:
        entries = ScheduleEntry.query.filter_by(
            school_year_id=current_year.id,
            day_of_week=today_weekday
        ).all()
        for e in entries:
            # Vérifier si l'appel a été fait aujourd'hui pour cette classe
            abs_today_count = Absence.query.join(Student).filter(
                Student.class_id == e.class_id,
                Absence.date == today
            ).count()
            
            # Vérifier si séance cahier de texte enregistrée
            tb_today = TextbookSession.query.filter_by(
                class_id=e.class_id,
                date=today,
                school_year_id=current_year.id
            ).first()

            slot = e.time_slot
            is_now = False
            if slot and slot.start_time <= now_time_str <= slot.end_time:
                is_now = True
                active_course = {
                    'entry': e,
                    'class_name': e.classe.name if e.classe else '',
                    'class_id': e.class_id,
                    'slot': slot,
                    'room': e.room,
                    'subject': e.subject_title,
                    'has_attendance': abs_today_count > 0,
                    'has_textbook': tb_today is not None
                }

            today_schedule.append({
                'entry': e,
                'class_name': e.classe.name if e.classe else '',
                'class_id': e.class_id,
                'slot': slot,
                'room': e.room,
                'subject': e.subject_title,
                'is_now': is_now,
                'has_attendance': abs_today_count > 0,
                'has_textbook': tb_today is not None
            })

    return render_template(
        'dashboard.html',
        total_students=total_students,
        total_classes=total_classes,
        total_absences=total_absences,
        unjustified_absences=unjustified_absences,
        justified_absences=justified_absences,
        alert_students_count=alert_students_count,
        absence_alert_threshold=absence_alert_threshold,
        total_sessions=total_sessions,
        top_absent_students=top_absent_students,
        total_absent_students_count=len(all_absent_students),
        class_names=class_names,
        class_abs_counts=class_abs_counts,
        class_stats_details=class_stats_details,
        classes_list=classes_list,
        today_schedule=today_schedule,
        active_course=active_course,
        today_date_str=today.strftime('%d/%m/%Y')
    )

@main_bp.route('/uploads/<path:filename>')
def uploaded_file(filename):
    response = send_from_directory(current_app.config['UPLOAD_FOLDER'], filename, max_age=604800)
    response.headers['Cache-Control'] = 'public, max-age=604800'
    return response
