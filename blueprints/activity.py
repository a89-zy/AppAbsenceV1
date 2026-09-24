import json
import io
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session, send_file
from database import db, Class, Student, Activity, Grade, CourseModule, CourseSection, CourseResource, ActivityType, Absence, SchoolYear, AppSetting, FlashParticipation, SemesterActivityEvaluation, SemesterExamGrade
from datetime import datetime, timedelta

activity_bp = Blueprint('activity', __name__)

def get_current_school_year():
    """Récupère l'année scolaire active pour la session courante."""
    year_id = session.get('active_school_year_id')
    if year_id:
        y = SchoolYear.query.get(year_id)
        if y: return y
    active = SchoolYear.query.filter_by(is_active=True).first()
    if active: return active
    return SchoolYear.query.order_by(SchoolYear.start_date.desc()).first()


# Helper to ensure settings exist (maybe better placed in configuration or run on app start)
def seed_settings():
    # Helper to populate defaults if empty
    if not CourseModule.query.first():
        for i in range(1, 5):
            m = CourseModule(name=f"Module {i}")
            db.session.add(m)
            db.session.flush() # Get ID
            # Add some default chapters for each module
            for j in range(1, 4):
                s = CourseSection(name=f"Chapitre {j}", module_id=m.id)
                db.session.add(s)

    if not ActivityType.query.first():
        for t in ["Présentation", "Recherche", "Projet"]:
            db.session.add(ActivityType(name=t))
    db.session.commit()

@activity_bp.route('/activities', methods=['GET'])
def activities():
    # Only seed if needed (simple check)
    if not CourseModule.query.first():
        seed_settings()

    current_year = get_current_school_year()
    if current_year:
        classes = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
        act_query = Activity.query.filter(Activity.date >= current_year.start_date, Activity.date <= current_year.end_date)
    else:
        classes = []
        act_query = Activity.query
    activities_list = act_query.order_by(Activity.date.desc(), Activity.id.desc()).all()
    
    modules = CourseModule.query.order_by(CourseModule.name).all()
    sections = [] 
    types = ActivityType.query.order_by(ActivityType.name).all()
    resources = CourseResource.query.order_by(CourseResource.title).all()
    
    return render_template('activities.html', classes=classes, activities=activities_list, Class=Class,
                           modules=modules, sections=sections, types=types, resources=resources)

@activity_bp.route('/activities/search', methods=['GET'])
def activity_search():
    # Filters
    class_id = request.args.get('class_id')
    module = request.args.get('module')
    section = request.args.get('section')
    activity_type = request.args.get('activity_type')
    
    current_year = get_current_school_year()
    query = Activity.query
    if current_year:
        query = query.filter(Activity.date >= current_year.start_date, Activity.date <= current_year.end_date)
    
    if class_id:
        query = query.filter_by(class_id=class_id)
    if module:
        query = query.filter_by(module=module)
    if section:
        query = query.filter_by(section=section)
    if activity_type:
        query = query.filter_by(activity_type=activity_type)
        
    activities = query.order_by(Activity.date.desc()).all()
    if current_year:
        classes = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
    else:
        classes = Class.query.order_by(Class.name).all()
    modules = CourseModule.query.order_by(CourseModule.name).all()
    types = ActivityType.query.order_by(ActivityType.name).all()
    
    # Logic to populate sections based on module if selected (for the filter dropdown)
    sections = []
    if module:
        mod_obj = CourseModule.query.filter_by(name=module).first()
        if mod_obj:
            sections = CourseSection.query.filter_by(module_id=mod_obj.id).all()

    return render_template('activity_search.html', 
                           activities=activities, 
                           classes=classes, 
                           modules=modules, 
                           sections=sections, 
                           types=types,
                           Class=Class)

@activity_bp.route('/activities/report')
def activities_report():
    current_year = get_current_school_year()
    if current_year:
        classes = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
    else:
        classes = Class.query.order_by(Class.name).all()
    student_id = request.args.get('student_id')
    student = None
    grades = []
    
    if student_id:
        student = Student.query.get_or_404(student_id)
        # Fetch grades joined with activity info, only for Fait/Assigné/Non fait (or all grades)
        grades = Grade.query.filter_by(student_id=student_id).join(Activity).order_by(Activity.date.desc()).all()
    
    return render_template('student_report.html', classes=classes, student=student, grades=grades)

# ==============================================================================
# APIS DYNAMIQUES POUR LE FORMULAIRE NOUVELLE ACTIVITÉ
# ==============================================================================

@activity_bp.route('/api/resources-by-module')
def api_resources_by_module():
    """Renvoie les ressources pédagogiques d'un module et/ou chapitre pour liaison directe."""
    module_name = request.args.get('module_name', '').strip()
    module_id = request.args.get('module_id', type=int)
    section_name = request.args.get('section_name', '').strip()

    if not module_id and module_name:
        mod = CourseModule.query.filter_by(name=module_name).first()
        if mod:
            module_id = mod.id

    if not module_id:
        return jsonify([])

    query = CourseResource.query.filter_by(module_id=module_id)
    if section_name:
        sec = CourseSection.query.filter_by(module_id=module_id, name=section_name).first()
        if sec:
            query = query.filter(db.or_(CourseResource.section_id == sec.id, CourseResource.section_id.is_(None)))

    resources = query.order_by(CourseResource.created_at.desc()).all()
    data = []
    for r in resources:
        data.append({
            'id': r.id,
            'title': r.title,
            'resource_type': r.resource_type,
            'file_type': r.file_type,
            'original_filename': r.original_filename or '',
            'icon_class': r.icon_class,
            'human_size': r.human_file_size,
            'share_token': r.share_token,
            'section_name': r.section.name if r.section else 'Module Général'
        })
    return jsonify(data)


@activity_bp.route('/api/class-students-groups')
def api_class_students_groups():
    """Renvoie la liste des élèves d'une classe avec répartition Groupe 1 / Groupe 2."""
    class_id = request.args.get('class_id', type=int)
    if not class_id:
        return jsonify({'total': 0, 'g1_count': 0, 'g2_count': 0, 'students': [], 'g1_ids': [], 'g2_ids': []})

    students = Student.query.filter_by(class_id=class_id).order_by(*Student.default_order()).all()

    raw_grp = AppSetting.get_value(f'class_groups_{class_id}', '')
    g1_ids = []
    g2_ids = []
    if raw_grp:
        try:
            gdata = json.loads(raw_grp)
            g1_ids = [int(x) for x in gdata.get('groups', {}).get('1', [])]
            g2_ids = [int(x) for x in gdata.get('groups', {}).get('2', [])]
        except Exception:
            pass

    st_list = []
    for s in students:
        grp = '1' if s.id in g1_ids else ('2' if s.id in g2_ids else '')
        st_list.append({
            'id': s.id,
            'name': f"{s.last_name} {s.first_name}",
            'cne': s.cne,
            'group': grp
        })

    return jsonify({
        'total': len(students),
        'g1_count': len(g1_ids),
        'g2_count': len(g2_ids),
        'students': st_list,
        'g1_ids': g1_ids,
        'g2_ids': g2_ids
    })


# ==============================================================================
# CRÉATION & CYCLE DE VIE DES ACTIVITÉS
# ==============================================================================

@activity_bp.route('/activities/create', methods=['POST'])
def create_activity():
    """Crée une nouvelle activité soit en mode planification (Assigné), soit avant saisie des notes."""
    current_year = get_current_school_year()
    if current_year and current_year.is_read_only():
        flash(f"L'année scolaire {current_year.name} est archivée en lecture seule.", 'error')
        return redirect(url_for('activity.activities'))

    class_id = request.form.get('class_id', type=int)
    module = request.form.get('module', '').strip()
    section = request.form.get('section', '').strip()
    activity_type = request.form.get('activity_type', '').strip()
    title = request.form.get('title', '').strip() or None
    description = request.form.get('description', '').strip() or None

    date_str = request.form.get('date', '').strip()
    act_date = datetime.now().date()
    if date_str:
        try:
            act_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except Exception:
            pass

    due_date_str = request.form.get('due_date', '').strip()
    due_date = None
    if due_date_str:
        try:
            due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
        except Exception:
            pass

    max_score = float(request.form.get('max_score', 20.0) or 20.0)
    coefficient = float(request.form.get('coefficient', 1.0) or 1.0)
    work_mode = request.form.get('work_mode', 'Individuel').strip()
    target_group = request.form.get('target_group', 'all').strip()
    resource_id = request.form.get('resource_id', type=int)
    action_type = request.form.get('action_type', 'plan')
    groups_json = request.form.get('groups_json', '').strip() or None

    if not all([class_id, module, section, activity_type]):
        flash("Veuillez renseigner au minimum la Classe, le Module, le Chapitre et le Type d'activité.", "danger")
        return redirect(url_for('activity.activities'))

    if not title:
        title = f"{activity_type} - {section} ({act_date.strftime('%d/%m/%Y')})"

    # Mapping élève -> groupe
    student_group_map = {}
    if groups_json:
        try:
            parsed_groups = json.loads(groups_json)
            for grp in parsed_groups:
                grp_name = grp.get('name', '').strip()
                for member_id in grp.get('members', []):
                    try:
                        student_group_map[int(member_id)] = grp_name
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    new_activity = Activity(
        class_id=class_id,
        title=title,
        description=description,
        module=module,
        section=section,
        activity_type=activity_type,
        date=act_date,
        due_date=due_date,
        max_score=max_score,
        coefficient=coefficient,
        work_mode=work_mode,
        target_group=target_group,
        status='Assigné',
        resource_id=resource_id if resource_id else None,
        groups_json=groups_json
    )
    db.session.add(new_activity)
    db.session.flush()

    # Déterminer les élèves ciblés
    students = Student.query.filter_by(class_id=class_id).all()
    targeted_ids = set()

    if target_group == 'all':
        targeted_ids = {s.id for s in students}
    elif target_group in ['g1', 'g2']:
        raw_grp = AppSetting.get_value(f'class_groups_{class_id}', '')
        if raw_grp:
            try:
                gdata = json.loads(raw_grp)
                key = '1' if target_group == 'g1' else '2'
                targeted_ids = {int(x) for x in gdata.get('groups', {}).get(key, [])}
            except Exception:
                targeted_ids = {s.id for s in students}
        else:
            targeted_ids = {s.id for s in students}
    elif target_group == 'custom':
        custom_ids = request.form.getlist('selected_students')
        targeted_ids = {int(x) for x in custom_ids if x.isdigit()}
        if not targeted_ids:
            targeted_ids = {s.id for s in students}

    # Initialisation des notes (Grade) avec rattachement au groupe
    for student in students:
        is_targeted = student.id in targeted_ids
        grp_name = student_group_map.get(student.id)
        grade = Grade(
            activity_id=new_activity.id,
            student_id=student.id,
            score=0.0,
            status='Assigné' if is_targeted else 'Non assigné',
            feedback=None,
            group_number=grp_name
        )
        db.session.add(grade)

    db.session.commit()

    if action_type == 'grade_now':
        return redirect(url_for('activity.grading_view', activity_id=new_activity.id))
    else:
        grp_info = f" ({new_activity.groups_summary})" if new_activity.groups_summary else ""
        flash(f"L'activité « {new_activity.title} » a été créée et assignée à {len(targeted_ids)} élève(s){grp_info}.", "success")
        return redirect(url_for('activity.activities'))


@activity_bp.route('/activities/grading', methods=['GET'])
def grading_view():
    """Vue de saisie des notes et d'évaluation avec support des activités existantes et ressources liées."""
    activity_id = request.args.get('activity_id', type=int)

    if activity_id:
        activity = Activity.query.get_or_404(activity_id)
        classe = Class.query.get_or_404(activity.class_id)
        students = Student.query.filter_by(class_id=activity.class_id).order_by(*Student.default_order()).all()
        existing_grades = {g.student_id: g for g in activity.grades}
        return render_template(
            'grading_view.html',
            activity=activity,
            classe=classe,
            students=students,
            existing_grades=existing_grades,
            module=activity.module,
            section=activity.section,
            activity_type=activity.activity_type,
            max_score=activity.max_score or 20.0,
            coefficient=activity.coefficient or 1.0
        )

    # Flux alternatif direct (paramètres dans l'URL)
    class_id = request.args.get('class_id')
    module = request.args.get('module')
    section = request.args.get('section')
    activity_type = request.args.get('activity_type')
    max_score = float(request.args.get('max_score', 20.0) or 20.0)
    coefficient = float(request.args.get('coefficient', 1.0) or 1.0)

    if not all([class_id, module, section, activity_type]):
        flash('Veuillez sélectionner tous les champs obligatoires.', 'error')
        return redirect(url_for('activity.activities'))

    classe = Class.query.get_or_404(class_id)
    students = Student.query.filter_by(class_id=class_id).order_by(*Student.default_order()).all()

    return render_template(
        'grading_view.html',
        activity=None,
        classe=classe,
        students=students,
        existing_grades={},
        module=module,
        section=section,
        activity_type=activity_type,
        max_score=max_score,
        coefficient=coefficient
    )


@activity_bp.route('/activities/save', methods=['POST'])
def save_activity():
    """Enregistre ou met à jour les notes et le statut d'une activité."""
    current_year = get_current_school_year()
    if current_year and current_year.is_read_only():
        flash(f"L'année scolaire {current_year.name} est archivée en lecture seule. Impossible d'ajouter des évaluations.", 'error')
        return redirect(url_for('activity.activities'))

    try:
        activity_id = request.form.get('activity_id', type=int)
        class_id = request.form.get('class_id', type=int)
        module = request.form.get('module', '').strip()
        section = request.form.get('section', '').strip()
        activity_type = request.form.get('activity_type', '').strip()
        title = request.form.get('title', '').strip() or None
        description = request.form.get('description', '').strip() or None
        max_score = float(request.form.get('max_score', 20.0) or 20.0)
        coefficient = float(request.form.get('coefficient', 1.0) or 1.0)
        work_mode = request.form.get('work_mode', 'Individuel').strip()
        target_group = request.form.get('target_group', 'all').strip()
        resource_id = request.form.get('resource_id', type=int)

        due_date_str = request.form.get('due_date', '').strip()
        due_date = None
        if due_date_str:
            try:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            except Exception:
                pass

        groups_json = request.form.get('groups_json', '').strip() or None

        if activity_id:
            activity = Activity.query.get_or_404(activity_id)
            if title: activity.title = title
            if description: activity.description = description
            if due_date is not None: activity.due_date = due_date
            if resource_id: activity.resource_id = resource_id
            if groups_json: activity.groups_json = groups_json
            if work_mode: activity.work_mode = work_mode
            activity.max_score = max_score
            activity.coefficient = coefficient
        else:
            today = datetime.now().date()
            if not title:
                title = f"{activity_type} - {section} ({today.strftime('%d/%m/%Y')})"
            activity = Activity(
                class_id=class_id,
                title=title,
                description=description,
                module=module,
                section=section,
                activity_type=activity_type,
                date=today,
                due_date=due_date,
                max_score=max_score,
                coefficient=coefficient,
                work_mode=work_mode,
                target_group=target_group,
                resource_id=resource_id if resource_id else None
            )
            db.session.add(activity)
            db.session.flush()

        # Enregistrement des notes par élève
        students = Student.query.filter_by(class_id=activity.class_id).all()
        count = 0
        done_count = 0
        for student in students:
            score_str = request.form.get(f'grade_{student.id}')
            status = request.form.get(f'status_{student.id}', 'Non assigné')
            feedback = request.form.get(f'feedback_{student.id}', '').strip() or None
            group_number = request.form.get(f'group_{student.id}', '').strip() or None

            grade = Grade.query.filter_by(activity_id=activity.id, student_id=student.id).first()
            score = 0.0
            if score_str and score_str.strip():
                try:
                    score = float(score_str)
                except ValueError:
                    score = 0.0

            if (score_str and score_str.strip()) or status != 'Non assigné' or feedback or group_number:
                if 0 <= score <= activity.max_score:
                    if grade:
                        grade.score = score
                        grade.status = status
                        grade.feedback = feedback
                        grade.group_number = group_number
                    else:
                        grade = Grade(
                            activity_id=activity.id,
                            student_id=student.id,
                            score=score,
                            status=status,
                            feedback=feedback,
                            group_number=group_number
                        )
                        db.session.add(grade)
                    count += 1
                    if status == 'Fait':
                        done_count += 1
            else:
                if grade:
                    db.session.delete(grade)

        # Mise à jour du statut global de l'activité
        if done_count > 0:
            assigned_grades = [g for g in activity.grades if g.status in ['Assigné', 'Fait', 'Non fait']]
            if assigned_grades and all(g.status == 'Fait' for g in assigned_grades):
                activity.status = 'Terminé'
            else:
                activity.status = 'En cours'
        else:
            activity.status = 'Assigné'

        db.session.commit()
        flash(f'Activité « {activity.title} » enregistrée avec succès ({count} élèves notés/suivis).', 'success')
        return redirect(url_for('activity.activities'))
    except Exception as e:
        db.session.rollback()
        flash(f'Erreur: {str(e)}', 'error')
        return redirect(url_for('activity.activities'))


@activity_bp.route('/activities/<int:activity_id>/edit', methods=['GET', 'POST'])
def edit_activity(activity_id):
    """Modification des détails d'une activité et de ses notes."""
    activity = Activity.query.get_or_404(activity_id)

    if request.method == 'POST':
        try:
            activity.title = request.form.get('title', activity.title).strip() or activity.title
            activity.description = request.form.get('description', activity.description).strip() or None
            activity.module = request.form.get('module', activity.module).strip()
            activity.section = request.form.get('section', activity.section).strip()
            activity.activity_type = request.form.get('activity_type', activity.activity_type).strip()
            activity.max_score = float(request.form.get('max_score', activity.max_score or 20.0) or 20.0)
            activity.coefficient = float(request.form.get('coefficient', activity.coefficient or 1.0) or 1.0)
            activity.work_mode = request.form.get('work_mode', activity.work_mode or 'Individuel').strip()

            due_date_str = request.form.get('due_date', '').strip()
            if due_date_str:
                try:
                    activity.due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
                except Exception:
                    pass
            else:
                activity.due_date = None

            res_id = request.form.get('resource_id', type=int)
            activity.resource_id = res_id if res_id else None

            groups_json = request.form.get('groups_json', '').strip() or None
            if groups_json:
                activity.groups_json = groups_json

            # Mise à jour des notes
            students = Student.query.filter_by(class_id=activity.class_id).all()
            for student in students:
                score_str = request.form.get(f'grade_{student.id}')
                status = request.form.get(f'status_{student.id}', 'Non assigné')
                feedback = request.form.get(f'feedback_{student.id}', '').strip() or None
                group_number = request.form.get(f'group_{student.id}', '').strip() or None

                grade = Grade.query.filter_by(activity_id=activity.id, student_id=student.id).first()

                if (score_str and score_str.strip()) or status != 'Non assigné' or feedback or group_number:
                    try:
                        score = float(score_str) if (score_str and score_str.strip()) else 0.0
                        if 0 <= score <= activity.max_score:
                            if grade:
                                grade.score = score
                                grade.status = status
                                grade.feedback = feedback
                                grade.group_number = group_number
                            else:
                                grade = Grade(
                                    activity_id=activity.id,
                                    student_id=student.id,
                                    score=score,
                                    status=status,
                                    feedback=feedback,
                                    group_number=group_number
                                )
                                db.session.add(grade)
                    except ValueError:
                        pass
                else:
                    if grade:
                        db.session.delete(grade)

            db.session.commit()
            flash(f"L'activité « {activity.title} » a été mise à jour.", 'success')
            return redirect(url_for('activity.activities'))
        except Exception as e:
            db.session.rollback()
            flash(f'Erreur lors de la modification: {str(e)}', 'error')

    # GET request preparation
    classe = db.session.get(Class, activity.class_id)
    students = Student.query.filter_by(class_id=activity.class_id).order_by(*Student.default_order()).all()
    grades_map = {g.student_id: {'score': g.score, 'status': g.status, 'feedback': g.feedback, 'group_number': g.group_number} for g in activity.grades}
    modules = CourseModule.query.order_by(CourseModule.name).all()
    module_obj = CourseModule.query.filter_by(name=activity.module).first()
    sections = CourseSection.query.filter_by(module_id=module_obj.id).all() if module_obj else []
    types = ActivityType.query.order_by(ActivityType.name).all()
    resources = CourseResource.query.order_by(CourseResource.title).all()

    return render_template('edit_activity.html',
                           activity=activity,
                           classe=classe,
                           students=students,
                           grades=grades_map,
                           modules=modules,
                           sections=sections,
                           types=types,
                           resources=resources)

    # GET request preparation
    classe = db.session.get(Class, activity.class_id)
    students = Student.query.filter_by(class_id=activity.class_id).order_by(Student.last_name).all()
    
    # Map grades for easier template access: {student_id: {'score': score, 'status': string, 'feedback': str, 'group_number': str}}
    grades_map = {g.student_id: {'score': g.score, 'status': g.status, 'feedback': g.feedback, 'group_number': g.group_number} for g in activity.grades}
    
    modules = CourseModule.query.order_by(CourseModule.name).all()
    
    # For edit, we need sections of the current activity's module
    module_obj = CourseModule.query.filter_by(name=activity.module).first()
    sections = CourseSection.query.filter_by(module_id=module_obj.id).all() if module_obj else []
    
    types = ActivityType.query.order_by(ActivityType.name).all()

    return render_template('edit_activity.html', 
                           activity=activity, 
                           classe=classe, 
                           students=students, 
                           grades=grades_map,
                           modules=modules,
                           sections=sections,
                           types=types)

@activity_bp.route('/activities/<int:activity_id>/delete', methods=['POST'])
def delete_activity(activity_id):
    try:
        activity = Activity.query.get_or_404(activity_id)
        db.session.delete(activity)
        db.session.commit()
        flash('Activité supprimée avec succès.', 'success')
    except Exception as e:
        flash(f'Erreur lors de la suppression: {str(e)}', 'error')
    return redirect(url_for('activity.activities'))


@activity_bp.route('/activities/<int:activity_id>/groups-pdf')
def activity_groups_pdf(activity_id):
    """Fiche imprimable / projetable de la composition des groupes de travail."""
    activity = Activity.query.get_or_404(activity_id)
    classe = Class.query.get_or_404(activity.class_id)

    students_map = {s.id: s for s in Student.query.filter_by(class_id=activity.class_id).all()}
    raw_groups = activity.groups_list
    structured_groups = []

    for idx, g in enumerate(raw_groups, 1):
        grp_name = g.get('name', f"Groupe {idx}")
        member_ids = g.get('members', [])
        member_objs = [students_map[m_id] for m_id in member_ids if m_id in students_map]

        m_count = len(member_objs)
        if m_count == 1:
            type_label = "Individuel"
            badge_color = "secondary"
        elif m_count == 2:
            type_label = "Binôme"
            badge_color = "primary"
        elif m_count == 3:
            type_label = "Trinôme"
            badge_color = "info"
        elif m_count == 4:
            type_label = "Quatuor"
            badge_color = "success"
        else:
            type_label = f"Groupe ({m_count})"
            badge_color = "dark"

        structured_groups.append({
            'name': grp_name,
            'type_label': type_label,
            'badge_color': badge_color,
            'members': member_objs,
            'count': m_count
        })

    return render_template(
        'activity_groups_pdf.html',
        activity=activity,
        classe=classe,
        groups=structured_groups
    )


@activity_bp.route('/api/activity-groups/<int:activity_id>')
def api_activity_groups(activity_id):
    """Renvoie la composition JSON des groupes d'une activité pour le modal de réajustement."""
    activity = Activity.query.get_or_404(activity_id)
    return jsonify({
        'activity_id': activity.id,
        'groups': activity.groups_list,
        'groups_summary': activity.groups_summary
    })


import json
from datetime import datetime, date, timedelta, timezone

@activity_bp.route('/stats')
def stats():
    current_year = get_current_school_year()
    if current_year:
        classes = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
    else:
        classes = Class.query.order_by(Class.name).all()
    return render_template('stats.html', classes=classes, current_year=current_year)


@activity_bp.route('/api/stats_data')
def stats_data():
    class_id_raw = request.args.get('class_id', '').strip()
    class_id = int(class_id_raw) if class_id_raw.isdigit() else None
    student_id_raw = request.args.get('student_id', '').strip()
    student_id = int(student_id_raw) if student_id_raw.isdigit() else None
    period = request.args.get('period', 'all').strip()

    current_year = get_current_school_year()
    today = datetime.now().date()

    # 1. Bornes temporelles selon la période choisie
    start_date = None
    end_date = None

    if current_year:
        start_date = current_year.start_date
        end_date = current_year.end_date
    else:
        start_date = today.replace(month=9, day=1) if today.month >= 9 else today.replace(year=today.year - 1, month=9, day=1)
        end_date = today

    if period == 'month':
        start_date = today.replace(day=1)
        end_date = today
    elif period == '30days':
        start_date = today - timedelta(days=30)
        end_date = today
    elif period == 't1' and current_year:
        start_date = current_year.start_date
        end_date = current_year.start_date.replace(year=current_year.start_date.year + 1, month=1, day=31) if current_year.start_date.month > 1 else current_year.start_date.replace(month=1, day=31)
    elif period == 't2' and current_year:
        start_date = current_year.start_date.replace(year=current_year.start_date.year + 1, month=2, day=1) if current_year.start_date.month > 1 else current_year.start_date.replace(month=2, day=1)
        end_date = current_year.end_date

    # 2. Récupérer les classes concernées et les élèves
    if class_id:
        classes_in_scope = Class.query.filter_by(id=class_id).all()
    elif current_year:
        classes_in_scope = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
    else:
        classes_in_scope = Class.query.order_by(Class.name).all()

    class_ids = [c.id for c in classes_in_scope]

    if student_id:
        students_query = Student.query.filter_by(id=student_id)
    elif class_ids:
        students_query = Student.query.filter(Student.class_id.in_(class_ids))
    else:
        students_query = Student.query.filter(db.false())

    students_in_scope = students_query.order_by(*Student.default_order()).all()
    student_ids = [s.id for s in students_in_scope]

    # 3. Récupérer les absences sur la période
    abs_query = Absence.query.filter(Absence.student_id.in_(student_ids))
    if start_date:
        abs_query = abs_query.filter(Absence.date >= start_date)
    if end_date:
        abs_query = abs_query.filter(Absence.date <= end_date)

    absences = abs_query.order_by(Absence.date).all()
    total_absences = len(absences)
    justified_count = sum(1 for a in absences if a.justified)
    unjustified_count = total_absences - justified_count
    justified_rate = round((justified_count / total_absences * 100), 1) if total_absences > 0 else 100.0

    # Taux d'assiduité global estimé
    unique_dates = len(set(a.date for a in absences))
    total_opportunities = len(student_ids) * max(unique_dates, 1)
    if total_opportunities > 0:
        attendance_rate = max(0.0, min(100.0, round(((total_opportunities - total_absences) / total_opportunities) * 100, 1)))
    else:
        attendance_rate = 100.0

    # 4. Répartition par Jour de la Semaine (0: Lundi ... 6: Dimanche)
    day_names = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    day_counts = [0] * 7
    for a in absences:
        w = a.date.weekday()
        if 0 <= w <= 6:
            day_counts[w] += 1

    max_day_val = max(day_counts) if day_counts else 0
    peak_day_name = day_names[day_counts.index(max_day_val)] if max_day_val > 0 else "Aucun"

    # 5. Tendance Chronologique (par semaine)
    weekly_map = {}
    for a in absences:
        iso_year, iso_week, _ = a.date.isocalendar()
        key = f"S{iso_week}"
        weekly_map[key] = weekly_map.get(key, 0) + 1

    weekly_labels = list(weekly_map.keys())
    weekly_data = [weekly_map[k] for k in weekly_labels]

    # 6. Profils d'élèves & Alerte Décrochage
    from database import AppSetting
    absence_penalty = float(AppSetting.get_value('absence_penalty', '0.5'))
    absence_alert_threshold = int(AppSetting.get_value('absence_alert_threshold', '3'))

    student_abs_map = {s.id: [] for s in students_in_scope}
    for a in absences:
        if a.student_id in student_abs_map:
            student_abs_map[a.student_id].append(a)

    assidus_count = 0
    vigilance_count = 0
    alerte_count = 0

    student_rows = []
    for s in students_in_scope:
        s_abs = student_abs_map.get(s.id, [])
        tot = len(s_abs)
        unjust = sum(1 for a in s_abs if not a.justified)
        just = tot - unjust
        score = max(0.0, 20.0 - (unjust * absence_penalty))

        if tot <= 1:
            profile = 'assidu'
            assidus_count += 1
        elif tot < absence_alert_threshold and unjust < 2:
            profile = 'vigilance'
            vigilance_count += 1
        else:
            profile = 'alerte'
            alerte_count += 1

        student_rows.append({
            'id': s.id,
            'name': f"{s.last_name} {s.first_name}",
            'cne': s.cne,
            'class_id': s.class_id,
            'class_name': s.student_class.name if s.student_class else '',
            'photo_url': url_for('main.uploaded_file', filename=s.photo_path if s.photo_path else 'photos/Default.jpg'),
            'total_abs': tot,
            'unjustified_abs': unjust,
            'justified_abs': just,
            'score': round(score, 2),
            'profile': profile,
            'has_email': bool(s.email)
        })

    # Trier la liste des élèves : les profils alerte d'abord, puis par total d'absences décroissant
    profile_weight = {'alerte': 3, 'vigilance': 2, 'assidu': 1}
    student_rows.sort(key=lambda r: (profile_weight[r['profile']], r['total_abs'], r['unjustified_abs']), reverse=True)

    # 7. Comparatif Inter-Classes OU Groupe 1 vs Groupe 2
    comparison_type = 'classes'
    comparison_labels = []
    comparison_data = []

    if class_id and len(classes_in_scope) == 1:
        c_obj = classes_in_scope[0]
        group_setting = AppSetting.get_value(f'class_groups_{c_obj.id}', '')
        if group_setting:
            try:
                g_data = json.loads(group_setting)
                g1_ids = [int(x) for x in g_data.get('groups', {}).get('1', [])]
                g2_ids = [int(x) for x in g_data.get('groups', {}).get('2', [])]
                if g1_ids or g2_ids:
                    comparison_type = 'groups'
                    comparison_labels = ['Groupe 1', 'Groupe 2']
                    g1_abs = sum(1 for a in absences if a.student_id in g1_ids)
                    g2_abs = sum(1 for a in absences if a.student_id in g2_ids)
                    comparison_data = [g1_abs, g2_abs]
            except Exception:
                pass

    if comparison_type == 'classes':
        for c in classes_in_scope:
            c_student_ids = [s.id for s in students_in_scope if s.class_id == c.id]
            c_abs_count = sum(1 for a in absences if a.student_id in c_student_ids)
            comparison_labels.append(c.name)
            comparison_data.append(c_abs_count)

    # 8. Corrélation Assiduité vs Notes d'Évaluations
    grades_query = Grade.query.filter(Grade.student_id.in_(student_ids))
    grades = grades_query.all()
    grades_by_student = {}
    for g in grades:
        if g.status == 'Fait':
            grades_by_student.setdefault(g.student_id, []).append(g.score)

    def avg_for_students(std_ids):
        all_scores = []
        for sid in std_ids:
            all_scores.extend(grades_by_student.get(sid, []))
        return round(sum(all_scores) / len(all_scores), 2) if all_scores else 0.0

    assidu_ids = [r['id'] for r in student_rows if r['profile'] == 'assidu']
    vigilance_ids = [r['id'] for r in student_rows if r['profile'] == 'vigilance']
    alerte_ids = [r['id'] for r in student_rows if r['profile'] == 'alerte']

    corr_labels = ['Assidus (0-1)', 'Vigilance (2-3)', 'Alerte (≥4)']
    corr_data = [
        avg_for_students(assidu_ids),
        avg_for_students(vigilance_ids),
        avg_for_students(alerte_ids)
    ]

    all_scores_general = [g.score for g in grades if g.status == 'Fait']
    overall_avg_grade = round(sum(all_scores_general) / len(all_scores_general), 2) if all_scores_general else 0.0

    return jsonify({
        'kpis': {
            'attendance_rate': attendance_rate,
            'total_absences': total_absences,
            'justified_count': justified_count,
            'unjustified_count': unjustified_count,
            'justified_rate': justified_rate,
            'alert_count': alerte_count,
            'vigilance_count': vigilance_count,
            'assidus_count': assidus_count,
            'total_students': len(students_in_scope),
            'avg_grade': overall_avg_grade,
            'peak_day': peak_day_name
        },
        'day_of_week': {
            'labels': day_names,
            'data': day_counts
        },
        'trend': {
            'labels': weekly_labels if weekly_labels else ['Aucune donnée'],
            'data': weekly_data if weekly_data else [0]
        },
        'profiles': {
            'labels': ['Assidus (0-1)', 'Vigilance (2-3)', 'Alerte Décrochage (≥4)'],
            'data': [assidus_count, vigilance_count, alerte_count]
        },
        'comparison': {
            'type': comparison_type,
            'labels': comparison_labels,
            'data': comparison_data
        },
        'correlation': {
            'labels': corr_labels,
            'data': corr_data
        },
        'students': student_rows
    })

@activity_bp.route('/api/send_alert_email', methods=['POST'])
def api_send_alert_email():
    student_id = request.form.get('student_id') or (request.json and request.json.get('student_id'))
    if not student_id:
        return jsonify({'success': False, 'message': 'Identifiant étudiant manquant.'}), 400

    student = Student.query.get(student_id)
    if not student:
        return jsonify({'success': False, 'message': 'Étudiant introuvable.'}), 404

    from blueprints.absence import send_absence_email
    success, msg = send_absence_email(student, force=True)
    return jsonify({'success': success, 'message': msg})


@activity_bp.route('/api/mail_templates', methods=['GET'])
def api_mail_templates():
    from blueprints.configuration import DEFAULT_MAIL_TEMPLATES
    from database import AppSetting

    active_tid = AppSetting.get_value('mail_active_template', '1')
    templates = []
    for tid, default_tmpl in DEFAULT_MAIL_TEMPLATES.items():
        subject = AppSetting.get_value(f'mail_template_{tid}_subject', default_tmpl.get('subject', ''))
        body = AppSetting.get_value(f'mail_template_{tid}_body', default_tmpl.get('body', ''))
        templates.append({
            'id': tid,
            'name': default_tmpl.get('name', f'Modèle {tid}'),
            'subject': subject,
            'body': body,
            'is_active': (tid == active_tid)
        })
    return jsonify({
        'templates': templates,
        'active_template_id': active_tid
    })


@activity_bp.route('/api/send_batch_alert_emails', methods=['POST'])
def api_send_batch_alert_emails():
    payload = request.get_json(silent=True) or request.form
    raw_student_ids = payload.get('student_ids')
    template_id = payload.get('template_id')
    custom_subject = payload.get('custom_subject')
    custom_body = payload.get('custom_body')

    if not raw_student_ids:
        return jsonify({'success': False, 'message': 'Aucun étudiant sélectionné.'}), 400

    import json
    from blueprints.absence import send_absence_email
    from database import Student, db

    # Handle string or list of IDs
    if isinstance(raw_student_ids, str):
        try:
            student_ids = json.loads(raw_student_ids)
        except Exception:
            student_ids = [int(x.strip()) for x in raw_student_ids.split(',') if x.strip().isdigit()]
    else:
        student_ids = [int(x) for x in raw_student_ids if str(x).isdigit()]

    if not student_ids:
        return jsonify({'success': False, 'message': 'Liste d\'étudiants invalide.'}), 400

    students = Student.query.filter(Student.id.in_(student_ids)).all()
    if not students:
        return jsonify({'success': False, 'message': 'Aucun étudiant trouvé.'}), 404

    sent_count = 0
    ignored_no_email = []
    failed_students = []

    for s in students:
        if not s.email:
            ignored_no_email.append(f"{s.first_name} {s.last_name}")
            continue

        try:
            success, msg = send_absence_email(
                s,
                force=True,
                template_id=template_id,
                custom_subject=custom_subject,
                custom_body=custom_body
            )
            if success:
                sent_count += 1
            else:
                failed_students.append(f"{s.first_name} {s.last_name} ({msg})")
        except Exception as e:
            failed_students.append(f"{s.first_name} {s.last_name} ({str(e)})")

    db.session.commit()

    parts = []
    if sent_count > 0:
        parts.append(f"{sent_count} email(s) envoyé(s) avec succès.")
    if ignored_no_email:
        parts.append(f"{len(ignored_no_email)} élève(s) ignoré(s) (adresse email manquante).")
    if failed_students:
        parts.append(f"{len(failed_students)} échec(s) d'envoi.")

    summary_msg = " ".join(parts) if parts else "Aucun email envoyé."

    return jsonify({
        'success': sent_count > 0 or (len(ignored_no_email) > 0 and len(failed_students) == 0),
        'sent_count': sent_count,
        'total_selected': len(students),
        'ignored_count': len(ignored_no_email),
        'failed_count': len(failed_students),
        'ignored_students': ignored_no_email,
        'failed_students': failed_students,
        'message': summary_msg
    })


# Route to get sections for a specific module
@activity_bp.route('/api/get_sections')
def api_get_sections():
    module_name = request.args.get('module_name')
    if not module_name:
        return jsonify([])
    module = CourseModule.query.filter_by(name=module_name).first()
    if not module:
        return jsonify([])
    sections = CourseSection.query.filter_by(module_id=module.id).order_by(CourseSection.name).all()
    return jsonify([s.name for s in sections])

@activity_bp.route('/api/get_activities')
def api_get_activities():
    class_id = request.args.get('class_id')
    module = request.args.get('module')
    section = request.args.get('section')
    
    query = Activity.query
    if class_id:
        query = query.filter_by(class_id=class_id)
    if module:
        query = query.filter_by(module=module)
    if section:
        query = query.filter_by(section=section)
        
    activities = query.order_by(Activity.date.desc()).all()
    # Return list of {id, type, date}
    return jsonify([{
        'id': a.id,
        'label': f"{a.activity_type} - {a.date.strftime('%d/%m/%Y')}"
    } for a in activities])

@activity_bp.route('/activities/monitoring')
def monitoring():
    # Filters
    class_id = request.args.get('class_id')
    module = request.args.get('module')
    section = request.args.get('section')
    activity_id = request.args.get('activity_id')
    status = request.args.get('status')
    
    students_data = []
    
    # If a specific activity is selected, we show students for that activity
    if activity_id:
        activity = Activity.query.get_or_404(activity_id)
        # Verify it matches other filters if provided ?? 
        # For simplicity, if activity_id is provided, it dictates the context.
        
        all_students = Student.query.filter_by(class_id=activity.class_id).order_by(Student.last_name).all()
        
        for student in all_students:
            grade = Grade.query.filter_by(activity_id=activity.id, student_id=student.id).first()
            s_status = grade.status if grade else 'Non assigné'
            s_score = grade.score if grade else None
            
            # Apply Status Filter
            if status and status != '' and s_status != status:
                continue
                
            students_data.append({
                'student': student,
                'activity': activity,
                'status': s_status,
                'score': s_score
            })
            
    current_year = get_current_school_year()
    if current_year:
        classes = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
    else:
        classes = Class.query.order_by(Class.name).all()

    modules = CourseModule.query.order_by(CourseModule.name).all()
    
    # Context data for dropdowns
    sections = []
    if module:
        mod_obj = CourseModule.query.filter_by(name=module).first()
        if mod_obj:
            sections = CourseSection.query.filter_by(module_id=mod_obj.id).all()
            
    # Activities for dropdown
    activities_list = []
    if class_id: # Only load activities if class is selected at minimum
        query = Activity.query.filter_by(class_id=class_id)
        if module:
            query = query.filter_by(module=module)
        if section:
            query = query.filter_by(section=section)
        activities_list = query.order_by(Activity.date.desc()).all()

    return render_template('activity_monitoring.html',
                           students_data=students_data,
                           classes=classes,
                           modules=modules,
                           sections=sections,
                           activities=activities_list)


@activity_bp.route('/export_stats_excel')
def export_stats_excel():
    """Exporte le récapitulatif administratif officiel des absences au format Excel (.xlsx)."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    class_id_raw = request.args.get('class_id', '').strip()
    class_id = int(class_id_raw) if class_id_raw.isdigit() else None
    student_id_raw = request.args.get('student_id', '').strip()
    student_id = int(student_id_raw) if student_id_raw.isdigit() else None
    period = request.args.get('period', 'all').strip()

    current_year = get_current_school_year()
    today = datetime.now().date()

    # Bornes temporelles
    start_date = None
    end_date = None

    if current_year:
        start_date = current_year.start_date
        end_date = current_year.end_date
    else:
        start_date = today.replace(month=9, day=1) if today.month >= 9 else today.replace(year=today.year - 1, month=9, day=1)
        end_date = today

    period_label = "Année Complète"
    if period == 'month':
        start_date = today.replace(day=1)
        end_date = today
        period_label = "Ce Mois"
    elif period == '30days':
        start_date = today - timedelta(days=30)
        end_date = today
        period_label = "30 Derniers Jours"
    elif period == 't1' and current_year:
        start_date = current_year.start_date
        end_date = current_year.start_date.replace(year=current_year.start_date.year + 1, month=1, day=31) if current_year.start_date.month > 1 else current_year.start_date.replace(month=1, day=31)
        period_label = "Semestre 1"
    elif period == 't2' and current_year:
        start_date = current_year.start_date.replace(year=current_year.start_date.year + 1, month=2, day=1) if current_year.start_date.month > 1 else current_year.start_date.replace(month=2, day=1)
        end_date = current_year.end_date
        period_label = "Semestre 2"

    # Récupérer classes & élèves
    selected_class = None
    if class_id:
        classes_in_scope = Class.query.filter_by(id=class_id).all()
        selected_class = classes_in_scope[0] if classes_in_scope else None
    elif current_year:
        classes_in_scope = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
    else:
        classes_in_scope = Class.query.order_by(Class.name).all()

    class_ids = [c.id for c in classes_in_scope]

    if student_id:
        students_query = Student.query.filter_by(id=student_id)
    elif class_ids:
        students_query = Student.query.filter(Student.class_id.in_(class_ids))
    else:
        students_query = Student.query.filter(db.false())

    students_in_scope = students_query.order_by(*Student.default_order()).all()

    # Paramètres d'assiduité
    absence_penalty = float(AppSetting.get_value('absence_penalty', '0.5'))
    absence_alert_threshold = int(AppSetting.get_value('absence_alert_threshold', '3'))
    school_name = AppSetting.get_value('school_name', 'Établissement Scolaire & Supérieur')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Synthèse Absences"
    ws.views.sheetView[0].showGridLines = True

    # Styles
    title_font = Font(name="Calibri", size=14, bold=True, color="1E3A8A")
    subtitle_font = Font(name="Calibri", size=10, italic=True, color="475569")
    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    bold_font = Font(name="Calibri", size=10, bold=True)
    regular_font = Font(name="Calibri", size=10)
    zebra_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

    alert_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
    alert_font = Font(name="Calibri", size=10, bold=True, color="991B1B")
    vigilance_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
    vigilance_font = Font(name="Calibri", size=10, bold=True, color="92400E")
    assidu_fill = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")
    assidu_font = Font(name="Calibri", size=10, bold=True, color="065F46")

    center_align = Alignment(horizontal="center", vertical="center")
    left_align = Alignment(horizontal="left", vertical="center")
    right_align = Alignment(horizontal="right", vertical="center")

    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )
    total_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='64748B'),
        bottom=Side(style='double', color='1E3A8A')
    )

    # Titre
    ws.merge_cells('A1:M1')
    ws['A1'] = f"{school_name.upper()} — RELEVÉ RÉCAPITULATIF DES ABSENCES & ASSIDUITÉ"
    ws['A1'].font = title_font
    ws['A1'].alignment = center_align

    classe_txt = selected_class.name if selected_class else "Toutes les classes"
    year_txt = current_year.name if current_year else "Année en cours"
    export_txt = datetime.now().strftime('%d/%m/%Y à %H:%M')
    ws.merge_cells('A2:M2')
    ws['A2'] = f"Périmètre : {classe_txt} | Période : {period_label} | Année : {year_txt} | Exporté le {export_txt}"
    ws['A2'].font = subtitle_font
    ws['A2'].alignment = center_align

    ws.row_dimensions[1].height = 25
    ws.row_dimensions[2].height = 18
    ws.row_dimensions[4].height = 24

    headers = [
        "#", "CNE", "Nom", "Prénom", "Classe",
        "Total Absences", "Injustifiées", "Justifiées",
        "Taux Régul. (%)", "Note Assiduité (/20)", "Profil",
        "Dernière Absence", "Email"
    ]

    for col_num, h_text in enumerate(headers, 1):
        cell = ws.cell(row=4, column=col_num)
        cell.value = h_text
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border

    # Données
    current_row = 5
    sum_total_abs = 0
    sum_unjustified = 0
    sum_justified = 0
    sum_scores = 0.0

    for idx, s in enumerate(students_in_scope, 1):
        abs_q = Absence.query.filter_by(student_id=s.id)
        if start_date:
            abs_q = abs_q.filter(Absence.date >= start_date)
        if end_date:
            abs_q = abs_q.filter(Absence.date <= end_date)
        s_absences = abs_q.order_by(Absence.date.desc()).all()

        tot = len(s_absences)
        just = sum(1 for a in s_absences if a.justified)
        unjust = tot - just
        score = round(max(0.0, 20.0 - (unjust * absence_penalty)), 2)

        rate = round((just / tot * 100.0), 1) if tot > 0 else 100.0

        if score < 12.0 or unjust >= absence_alert_threshold:
            profile_label = "Alerte Décrochage"
            p_fill = alert_fill
            p_font = alert_font
        elif score < 16.0 or tot >= 2:
            profile_label = "Vigilance"
            p_fill = vigilance_fill
            p_font = vigilance_font
        else:
            profile_label = "Assidu"
            p_fill = assidu_fill
            p_font = assidu_font

        last_date = s_absences[0].date.strftime('%d/%m/%Y') if s_absences else "-"
        class_name = s.student_class.name if s.student_class else "-"

        row_fill = zebra_fill if idx % 2 == 0 else None

        row_vals = [
            (idx, center_align, regular_font),
            (s.cne, center_align, regular_font),
            (s.last_name.upper(), left_align, bold_font),
            (s.first_name, left_align, regular_font),
            (class_name, center_align, regular_font),
            (tot, center_align, bold_font),
            (unjust, center_align, bold_font),
            (just, center_align, regular_font),
            (f"{rate}%", center_align, regular_font),
            (f"{score:.2f}", center_align, bold_font),
            (profile_label, center_align, p_font),
            (last_date, center_align, regular_font),
            (s.email or "-", left_align, regular_font)
        ]

        ws.row_dimensions[current_row].height = 20
        for col_num, (val, align, f_font) in enumerate(row_vals, 1):
            cell = ws.cell(row=current_row, column=col_num)
            cell.value = val
            cell.alignment = align
            cell.font = f_font
            cell.border = thin_border
            if col_num == 11:
                cell.fill = p_fill
            elif row_fill:
                cell.fill = row_fill

        sum_total_abs += tot
        sum_unjustified += unjust
        sum_justified += just
        sum_scores += score
        current_row += 1

    # Ligne de synthèse
    ws.row_dimensions[current_row].height = 22
    count_students = len(students_in_scope)
    avg_score = round(sum_scores / count_students, 2) if count_students > 0 else 20.0

    ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=5)
    total_cell = ws.cell(row=current_row, column=1)
    total_cell.value = f"TOTAL / MOYENNES ({count_students} élèves)"
    total_cell.font = bold_font
    total_cell.alignment = right_align

    totals_map = {
        6: sum_total_abs,
        7: sum_unjustified,
        8: sum_justified,
        9: f"{round((sum_justified/sum_total_abs*100.0), 1) if sum_total_abs > 0 else 100.0}%",
        10: f"{avg_score:.2f}/20",
        11: "-",
        12: "-",
        13: "-"
    }

    for col in range(1, 14):
        c = ws.cell(row=current_row, column=col)
        c.border = total_border
        if col in totals_map:
            c.value = totals_map[col]
            c.alignment = center_align
            c.font = bold_font

    col_widths = {
        1: 5, 2: 14, 3: 18, 4: 18, 5: 14,
        6: 15, 7: 15, 8: 14, 9: 16, 10: 18,
        11: 20, 12: 16, 13: 26
    }
    for col_idx, width in col_widths.items():
        col_letter = openpyxl.utils.get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = width

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename_classe = selected_class.name.replace(' ', '_') if selected_class else "Global"
    filename = f"Synthese_Absences_{filename_classe}_{period}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"

    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )


# ==============================================================================
# FORMULE B : ÉVALUATION SEMESTRIELLE & GESTION DES NOTES D'ACTIVITÉS
# ==============================================================================

def get_activity_config():
    """Récupère la configuration complète pour le calcul de la note d'activité semestrielle."""
    return {
        'bonus_max': float(AppSetting.get_value('activity_bonus_max', '3.0')),
        'malus_max': float(AppSetting.get_value('activity_malus_max', '4.0')),
        'absence_unjustified_penalty': float(AppSetting.get_value('activity_absence_unjustified_penalty', '0.5')),
        'absence_justified_penalty': float(AppSetting.get_value('activity_absence_justified_penalty', '0.0')),
        'late_penalty': float(AppSetting.get_value('activity_late_penalty', '0.25')),
        'late_tolerance': int(AppSetting.get_value('activity_late_tolerance', '2')),
        'rounding': AppSetting.get_value('activity_rounding', '0.25'), # 'none', '0.1', '0.25', '0.5', '1.0'
        's1_start': AppSetting.get_value('activity_s1_start', ''),
        's1_end': AppSetting.get_value('activity_s1_end', ''),
        's2_start': AppSetting.get_value('activity_s2_start', ''),
        's2_end': AppSetting.get_value('activity_s2_end', ''),
        'included_types': AppSetting.get_value('activity_included_types', 'all'), # 'all' or comma-separated
    }

def get_semester_dates(semester, current_year=None):
    """Calcule les dates de début et fin du semestre sélectionné (1 ou 2)."""
    cfg = get_activity_config()
    today = datetime.now().date()
    
    if semester == 1:
        if cfg['s1_start'] and cfg['s1_end']:
            try:
                return datetime.strptime(cfg['s1_start'], '%Y-%m-%d').date(), datetime.strptime(cfg['s1_end'], '%Y-%m-%d').date()
            except Exception:
                pass
        if current_year:
            s_date = current_year.start_date
            e_date = s_date.replace(year=s_date.year + 1, month=1, day=31) if s_date.month > 1 else s_date.replace(month=1, day=31)
            return s_date, e_date
        return today.replace(month=9, day=1) if today.month >= 9 else today.replace(year=today.year - 1, month=9, day=1), today.replace(month=1, day=31)
    else:
        if cfg['s2_start'] and cfg['s2_end']:
            try:
                return datetime.strptime(cfg['s2_start'], '%Y-%m-%d').date(), datetime.strptime(cfg['s2_end'], '%Y-%m-%d').date()
            except Exception:
                pass
        if current_year:
            s_date = current_year.start_date.replace(year=current_year.start_date.year + 1, month=2, day=1) if current_year.start_date.month > 1 else current_year.start_date.replace(month=2, day=1)
            e_date = current_year.end_date
            return s_date, e_date
        return today.replace(month=2, day=1), today.replace(month=6, day=30)

def apply_rounding(val, rule):
    if rule == 'none':
        return round(val, 2)
    elif rule == '0.1':
        return round(val, 1)
    elif rule == '0.25':
        return round(val * 4.0) / 4.0
    elif rule == '0.5':
        return round(val * 2.0) / 2.0
    elif rule == '1.0':
        return float(round(val))
    return round(val, 2)

def compute_student_activity_evaluation(student, semester, class_obj, current_year=None, saved_eval=None):
    cfg = get_activity_config()
    start_date, end_date = get_semester_dates(semester, current_year)

    # 1. Activités du semestre pour cette classe
    act_query = Activity.query.filter(
        Activity.class_id == class_obj.id,
        Activity.date >= start_date,
        Activity.date <= end_date
    )
    if cfg['included_types'] != 'all':
        types_list = [t.strip() for t in cfg['included_types'].split(',') if t.strip()]
        if types_list:
            act_query = act_query.filter(Activity.activity_type.in_(types_list))
    activities = act_query.order_by(Activity.date.asc()).all()

    total_weighted_score = 0.0
    total_coeffs = 0.0
    activities_details = []

    for act in activities:
        grade = Grade.query.filter_by(activity_id=act.id, student_id=student.id).first()
        coeff = float(act.coefficient or 1.0)
        max_s = float(act.max_score or 20.0)
        if grade and grade.status != 'Non assigné':
            score_on_20 = (float(grade.score) / max_s) * 20.0 if max_s > 0 else 0.0
            total_weighted_score += score_on_20 * coeff
            total_coeffs += coeff
            activities_details.append({
                'id': act.id,
                'title': act.display_title,
                'type': act.activity_type,
                'date': act.date.strftime('%d/%m/%Y'),
                'score': grade.score,
                'max_score': max_s,
                'score_on_20': round(score_on_20, 2),
                'coeff': coeff,
                'status': grade.status
            })

    base_score = round(total_weighted_score / total_coeffs, 2) if total_coeffs > 0 else 0.0

    # 2. Bonus Questions Flash & Participation
    flash_records = FlashParticipation.query.filter(
        FlashParticipation.student_id == student.id,
        FlashParticipation.semester == semester,
        FlashParticipation.date >= start_date,
        FlashParticipation.date <= end_date
    ).order_by(FlashParticipation.date.desc()).all()
    raw_bonus = sum(float(f.points) for f in flash_records)
    bonus_score = round(min(raw_bonus, cfg['bonus_max']), 2)

    # 3. Malus Assiduité & Absences
    absences = Absence.query.filter(
        Absence.student_id == student.id,
        Absence.date >= start_date,
        Absence.date <= end_date
    ).all()
    unjustified_count = sum(1 for a in absences if not a.justified)
    justified_count = sum(1 for a in absences if a.justified)

    raw_malus = (unjustified_count * cfg['absence_unjustified_penalty']) + (justified_count * cfg['absence_justified_penalty'])
    malus_score = round(min(raw_malus, cfg['malus_max']), 2)

    # 4. Ajustement manuel (si déjà enregistré ou fourni)
    manual_adj = float(saved_eval.manual_adjustment if saved_eval else 0.0)

    # 5. Note Finale
    raw_final = base_score + bonus_score - malus_score + manual_adj
    final_score = apply_rounding(min(20.0, max(0.0, raw_final)), cfg['rounding'])

    # 6. Appréciation suggérée ou enregistrée
    if saved_eval and saved_eval.appreciation and saved_eval.appreciation.strip():
        appreciation = saved_eval.appreciation.strip()
    else:
        if final_score >= 16:
            appreciation = "Excellent travail, grande assiduité et participation active remarquable."
        elif final_score >= 13:
            appreciation = "Bon travail d'ensemble, régularité et investissement appréciables."
        elif final_score >= 10:
            appreciation = "Travail convenable. Peut progresser avec plus d'assiduité et d'initiative."
        elif final_score >= 7:
            appreciation = "Résultats insuffisants. Manque de régularité dans la réalisation des activités."
        else:
            appreciation = "Niveau insuffisant. Difficultés majeures et absences pénalisantes."

    return {
        'student': student,
        'activities_count': len(activities_details),
        'activities_details': activities_details,
        'base_score': base_score,
        'flash_count': len(flash_records),
        'flash_records': [f.to_dict() for f in flash_records],
        'raw_bonus': round(raw_bonus, 2),
        'bonus_score': bonus_score,
        'unjustified_absences': unjustified_count,
        'justified_absences': justified_count,
        'raw_malus': round(raw_malus, 2),
        'malus_score': malus_score,
        'manual_adjustment': manual_adj,
        'final_score': final_score,
        'appreciation': appreciation,
        'is_locked': saved_eval.is_locked if saved_eval else False,
        'saved_id': saved_eval.id if saved_eval else None
    }


@activity_bp.route('/semester-evaluations', methods=['GET', 'POST'])
def semester_evaluations():
    """Redirige de façon transparente vers le nouveau module Évaluation."""
    class_id = request.args.get('class_id', type=int)
    semester = request.args.get('semester', default=1, type=int)
    return redirect(url_for('evaluation.grades_grid', class_id=class_id, semester=semester))


@activity_bp.route('/api/save-activity-config', methods=['POST'])
def save_activity_config():
    """Enregistre les pondérations et paramètres de calcul de la note d'activité."""
    try:
        bonus_max = request.form.get('bonus_max', '3.0')
        malus_max = request.form.get('malus_max', '4.0')
        unjustified_pen = request.form.get('absence_unjustified_penalty', '0.5')
        justified_pen = request.form.get('absence_justified_penalty', '0.0')
        late_pen = request.form.get('late_penalty', '0.25')
        late_tol = request.form.get('late_tolerance', '2')
        rounding = request.form.get('rounding', '0.25')
        s1_start = request.form.get('s1_start', '').strip()
        s1_end = request.form.get('s1_end', '').strip()
        s2_start = request.form.get('s2_start', '').strip()
        s2_end = request.form.get('s2_end', '').strip()
        included_types = request.form.get('included_types', 'all').strip()

        AppSetting.set_value('activity_bonus_max', bonus_max, "Plafond max du bonus questions flash/participation")
        AppSetting.set_value('activity_malus_max', malus_max, "Plafond max du malus assiduité/absences")
        AppSetting.set_value('activity_absence_unjustified_penalty', unjustified_pen, "Déduction par absence non justifiée")
        AppSetting.set_value('activity_absence_justified_penalty', justified_pen, "Déduction par absence justifiée")
        AppSetting.set_value('activity_late_penalty', late_pen, "Déduction par retard")
        AppSetting.set_value('activity_late_tolerance', late_tol, "Nombre de retards tolérés")
        AppSetting.set_value('activity_rounding', rounding, "Règle d'arrondi de la note finale")
        AppSetting.set_value('activity_s1_start', s1_start, "Date début Semestre 1")
        AppSetting.set_value('activity_s1_end', s1_end, "Date fin Semestre 1")
        AppSetting.set_value('activity_s2_start', s2_start, "Date début Semestre 2")
        AppSetting.set_value('activity_s2_end', s2_end, "Date fin Semestre 2")
        AppSetting.set_value('activity_included_types', included_types, "Types d'activités inclus dans la note de base")

        AppSetting.clear_cache()
        return jsonify({'success': True, 'message': 'Configuration enregistrée avec succès.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 400


@activity_bp.route('/api/flash-question', methods=['POST'])
def add_flash_question():
    """Enregistre un point bonus de question flash ou participation rapide."""
    try:
        student_id = request.form.get('student_id', type=int)
        class_id = request.form.get('class_id', type=int)
        points = float(request.form.get('points', 1.0))
        label = request.form.get('label', 'Question Flash début de séance').strip()
        semester = request.form.get('semester', default=1, type=int)

        if not student_id or not class_id:
            return jsonify({'success': False, 'message': 'Élève et classe requis.'}), 400

        record = FlashParticipation(
            student_id=student_id,
            class_id=class_id,
            points=points,
            label=label or 'Question Flash début de séance',
            semester=semester,
            date=datetime.now(timezone.utc).date()
        )
        db.session.add(record)
        db.session.commit()

        # Calculer le nouveau total du bonus pour cet élève
        cfg = get_activity_config()
        current_year = get_current_school_year()
        start_date, end_date = get_semester_dates(semester, current_year)
        total_pts = sum(f.points for f in FlashParticipation.query.filter(
            FlashParticipation.student_id == student_id,
            FlashParticipation.semester == semester,
            FlashParticipation.date >= start_date,
            FlashParticipation.date <= end_date
        ).all())

        return jsonify({
            'success': True,
            'record': record.to_dict(),
            'total_bonus': round(total_pts, 2),
            'capped_bonus': round(min(total_pts, cfg['bonus_max']), 2)
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 400


@activity_bp.route('/api/flash-history', methods=['GET'])
def get_flash_history():
    """Retourne l'historique des questions flash récentes pour une classe et un semestre."""
    class_id = request.args.get('class_id', type=int)
    semester = request.args.get('semester', default=1, type=int)
    if not class_id:
        return jsonify({'records': []})

    records = FlashParticipation.query.filter_by(
        class_id=class_id,
        semester=semester
    ).order_by(FlashParticipation.created_at.desc()).limit(30).all()

    return jsonify({'records': [r.to_dict() for r in records]})


@activity_bp.route('/api/class-flash-stats', methods=['GET'])
def get_class_flash_stats():
    """Retourne pour chaque élève de la classe son nombre de questions flash et son bonus actuel."""
    class_id = request.args.get('class_id', type=int)
    semester = request.args.get('semester', default=1, type=int)
    if not class_id:
        return jsonify({'stats': {}})

    cfg = get_activity_config()
    current_year = get_current_school_year()
    start_date, end_date = get_semester_dates(semester, current_year)

    records = FlashParticipation.query.filter(
        FlashParticipation.class_id == class_id,
        FlashParticipation.semester == semester,
        FlashParticipation.date >= start_date,
        FlashParticipation.date <= end_date
    ).all()

    stats = {}
    for r in records:
        if r.student_id not in stats:
            stats[r.student_id] = {'count': 0, 'raw_points': 0.0, 'capped_points': 0.0}
        stats[r.student_id]['count'] += 1
        stats[r.student_id]['raw_points'] += float(r.points)

    for sid, s in stats.items():
        s['capped_points'] = round(min(s['raw_points'], cfg['bonus_max']), 2)
        s['raw_points'] = round(s['raw_points'], 2)

    return jsonify({'stats': stats, 'bonus_max': cfg['bonus_max']})


@activity_bp.route('/api/delete-flash-question/<int:flash_id>', methods=['POST'])
def delete_flash_question(flash_id):
    """Supprime une question flash."""
    record = FlashParticipation.query.get_or_404(flash_id)
    student_id = record.student_id
    class_id = record.class_id
    semester = record.semester
    try:
        db.session.delete(record)
        db.session.commit()
        return jsonify({'success': True, 'student_id': student_id, 'class_id': class_id, 'semester': semester})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 400


@activity_bp.route('/semester-evaluations/export-excel', methods=['GET'])
def export_semester_evaluations_excel():
    """Exportation du Bilan Semestriel au format Excel prêt pour MASSAR."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    class_id = request.args.get('class_id', type=int)
    semester = request.args.get('semester', default=1, type=int)

    selected_class = Class.query.get_or_404(class_id)
    current_year = get_current_school_year()
    students = Student.query.filter_by(class_id=selected_class.id).order_by(*Student.default_order()).all()

    saved_map = {
        e.student_id: e 
        for e in SemesterActivityEvaluation.query.filter_by(
            class_id=selected_class.id, 
            semester=semester, 
            school_year_id=current_year.id if current_year else 1
        ).all()
    }

    evaluations = [
        compute_student_activity_evaluation(st, semester, selected_class, current_year, saved_map.get(st.id))
        for st in students
    ]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Notes Activités S{semester}"
    ws.views.sheetView[0].showGridLines = True

    # Styles
    navy_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    header_fill = PatternFill(start_color="2563EB", end_color="2563EB", fill_type="solid")
    score_fill = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
    title_font = Font(name="Arial", size=15, bold=True, color="1E3A8A")
    subtitle_font = Font(name="Arial", size=10, italic=True, color="475569")
    header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    data_font = Font(name="Arial", size=10)
    score_font = Font(name="Arial", size=11, bold=True, color="15803D")
    bold_font = Font(name="Arial", size=10, bold=True)

    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    # Titre & Métadonnées
    ws.merge_cells('A1:I1')
    title_cell = ws['A1']
    title_cell.value = f"BILAN DES NOTES D'ACTIVITÉS & ASSIDUITÉ — SEMESTRE {semester}"
    title_cell.font = title_font
    title_cell.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[1].height = 32

    ws.merge_cells('A2:I2')
    sub_cell = ws['A2']
    teacher_name = AppSetting.get_value('teacher_name', 'Prof ZYANI Azzeddine')
    school_name = AppSetting.get_value('school_name', 'Établissement Scolaire & Supérieur')
    sub_cell.value = f"Classe : {selected_class.name} | Année : {current_year.name if current_year else ''} | Enseignant : {teacher_name} | Établissement : {school_name}"
    sub_cell.font = subtitle_font
    sub_cell.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[2].height = 18

    # En-têtes du tableau
    headers = [
        ("N°", 5, 'center'),
        ("CNE / Code Massar", 16, 'center'),
        ("Nom & Prénom", 24, 'left'),
        ("Groupe TP", 12, 'center'),
        ("Note Base TP/TD (/20)", 18, 'center'),
        ("Bonus Flash (+pts)", 16, 'center'),
        ("Malus Absences (-pts)", 16, 'center'),
        ("NOTE FINALE (/20)", 18, 'center'),
        ("Appréciation / Remarques", 36, 'left')
    ]

    header_row = 4
    ws.row_dimensions[header_row].height = 28

    for col_idx, (h_title, width, align) in enumerate(headers, 1):
        cell = ws.cell(row=header_row, column=col_idx)
        cell.value = h_title
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal=align, vertical='center', wrap_text=True)
        cell.border = thin_border
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = width

    current_row = header_row + 1
    scores_list = []

    for idx, item in enumerate(evaluations, 1):
        st = item['student']
        fs = item['final_score']
        scores_list.append(fs)

        row_data = [
            (idx, 'center', data_font, None),
            (st.cne, 'center', data_font, None),
            (f"{st.last_name.upper()} {st.first_name}", 'left', bold_font, None),
            (f"G{st.group}" if hasattr(st, 'group') and st.group else "G1", 'center', data_font, None),
            (f"{item['base_score']:.2f}", 'center', data_font, None),
            (f"+{item['bonus_score']:.2f}" if item['bonus_score'] > 0 else "0.0", 'center', data_font, None),
            (f"-{item['malus_score']:.2f}" if item['malus_score'] > 0 else "0.0", 'center', data_font, None),
            (fs, 'center', score_font, score_fill),
            (item['appreciation'], 'left', data_font, None)
        ]

        ws.row_dimensions[current_row].height = 20
        for col_idx, (val, align, font_obj, fill_obj) in enumerate(row_data, 1):
            cell = ws.cell(row=current_row, column=col_idx)
            cell.value = val
            cell.font = font_obj
            cell.alignment = Alignment(horizontal=align, vertical='center')
            cell.border = thin_border
            if fill_obj:
                cell.fill = fill_obj

        current_row += 1

    # Ligne de synthèse / Moyenne
    current_row += 1
    ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=4)
    tot_label = ws.cell(row=current_row, column=1)
    tot_label.value = f"STATISTIQUES DE LA CLASSE ({len(scores_list)} élèves)"
    tot_label.font = bold_font
    tot_label.alignment = Alignment(horizontal='right', vertical='center')

    avg_score = round(sum(scores_list) / len(scores_list), 2) if scores_list else 0.0
    pass_count = sum(1 for s in scores_list if s >= 10.0)
    pass_pct = round((pass_count / len(scores_list)) * 100.0, 1) if scores_list else 0.0

    avg_cell = ws.cell(row=current_row, column=8)
    avg_cell.value = f"Moy : {avg_score:.2f}/20"
    avg_cell.font = bold_font
    avg_cell.alignment = Alignment(horizontal='center', vertical='center')
    avg_cell.fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")

    appr_stat = ws.cell(row=current_row, column=9)
    appr_stat.value = f"Taux de réussite : {pass_pct}% ({pass_count}/{len(scores_list)})"
    appr_stat.font = bold_font
    appr_stat.alignment = Alignment(horizontal='left', vertical='center')

    for c_idx in range(1, 10):
        ws.cell(row=current_row, column=c_idx).border = thin_border

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename_classe = selected_class.name.replace(' ', '_')
    filename = f"Notes_Activites_{filename_classe}_S{semester}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"

    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )


@activity_bp.route('/semester-evaluations/pdf', methods=['GET'])
def print_semester_evaluations_pdf():
    """Fiche imprimable et PDF officielle du Bilan Semestriel."""
    class_id = request.args.get('class_id', type=int)
    semester = request.args.get('semester', default=1, type=int)

    selected_class = Class.query.get_or_404(class_id)
    current_year = get_current_school_year()
    students = Student.query.filter_by(class_id=selected_class.id).order_by(*Student.default_order()).all()

    saved_map = {
        e.student_id: e 
        for e in SemesterActivityEvaluation.query.filter_by(
            class_id=selected_class.id, 
            semester=semester, 
            school_year_id=current_year.id if current_year else 1
        ).all()
    }

    evaluations = [
        compute_student_activity_evaluation(st, semester, selected_class, current_year, saved_map.get(st.id))
        for st in students
    ]

    scores = [e['final_score'] for e in evaluations]
    stats = {
        'count': len(scores),
        'avg': round(sum(scores) / len(scores), 2) if scores else 0.0,
        'min': min(scores) if scores else 0.0,
        'max': max(scores) if scores else 0.0,
        'pass_count': sum(1 for s in scores if s >= 10.0),
        'pass_rate': round((sum(1 for s in scores if s >= 10.0) / len(scores)) * 100.0, 1) if scores else 0.0
    }

    teacher_name = AppSetting.get_value('teacher_name', 'Prof ZYANI Azzeddine')
    school_name = AppSetting.get_value('school_name', 'Établissement Scolaire & Supérieur')

    return render_template(
        'semester_activity_pdf.html',
        selected_class=selected_class,
        semester=semester,
        current_year=current_year,
        evaluations=evaluations,
        stats=stats,
        teacher_name=teacher_name,
        school_name=school_name,
        now=datetime.now()
    )


# ─── API RAPIDE SAUVEGARDE CONTRÔLES CONTINUS (CC1 À CC4) ────────────────────

@activity_bp.route('/api/save-exam-grades', methods=['POST'])
def api_save_exam_grades():
    """Sauvegarde rapide en AJAX des notes de contrôles continus (CC1 à CC4) d'un étudiant."""
    data = request.get_json() or {}
    student_id = data.get('student_id')
    semester = int(data.get('semester', 1))
    class_id = data.get('class_id')

    if not student_id:
        return jsonify({'success': False, 'error': 'ID étudiant manquant'}), 400

    current_year = get_current_school_year()
    if not current_year:
        return jsonify({'success': False, 'error': 'Aucune année scolaire active'}), 400

    def clean_val(v):
        if v is None or v == '':
            return None
        try:
            return round(max(0.0, min(20.0, float(str(v).replace(',', '.')))), 2)
        except (ValueError, TypeError):
            return None

    cc1 = clean_val(data.get('cc1'))
    cc2 = clean_val(data.get('cc2'))
    cc3 = clean_val(data.get('cc3'))
    cc4 = clean_val(data.get('cc4'))

    rec = SemesterExamGrade.query.filter_by(
        student_id=student_id,
        semester=semester,
        school_year_id=current_year.id
    ).first()

    if not rec:
        st = Student.query.get(student_id)
        rec = SemesterExamGrade(
            student_id=student_id,
            class_id=class_id or (st.class_id if st else 1),
            school_year_id=current_year.id,
            semester=semester,
            cc1=cc1,
            cc2=cc2,
            cc3=cc3,
            cc4=cc4
        )
        db.session.add(rec)
    else:
        rec.cc1 = cc1
        rec.cc2 = cc2
        rec.cc3 = cc3
        rec.cc4 = cc4

    db.session.commit()

    return jsonify({
        'success': True,
        'student_id': student_id,
        'cc_average': rec.cc_average,
        'message': 'Notes de contrôles enregistrées avec succès !'
    })


# ─── INJECTEUR AUTOMATIQUE DANS LA GRILLE EXCEL OFFICIELLE MASSAR ────────────

@activity_bp.route('/semester-evaluations/inject-massar', methods=['POST'])
def inject_massar_excel():
    """Injecte automatiquement les 5 notes (CC1..CC4 + Activité Formule B) dans la grille officielle MASSAR."""
    import openpyxl

    class_id = request.form.get('class_id', type=int)
    semester = request.form.get('semester', default=1, type=int)

    if not class_id:
        flash("Classe non spécifiée.", "danger")
        return redirect(url_for('activity.semester_evaluations'))

    if 'massar_file' not in request.files:
        flash("Veuillez sélectionner un fichier Excel officiel exporté depuis MASSAR.", "danger")
        return redirect(url_for('activity.semester_evaluations', class_id=class_id, semester=semester))

    file = request.files['massar_file']
    if not file or not file.filename.lower().endswith(('.xlsx', '.xlsm')):
        flash("Le fichier doit être un classeur Excel valide (.xlsx).", "danger")
        return redirect(url_for('activity.semester_evaluations', class_id=class_id, semester=semester))

    selected_class = Class.query.get_or_404(class_id)
    current_year = get_current_school_year()

    # Récupérer les évaluations d'activités (Formule B) et les notes de contrôles (CC1..CC4)
    saved_evals = {
        e.student_id: e for e in SemesterActivityEvaluation.query.filter_by(
            class_id=selected_class.id, semester=semester, school_year_id=current_year.id if current_year else 1
        ).all()
    }
    exam_grades = {
        e.student_id: e for e in SemesterExamGrade.query.filter_by(
            class_id=selected_class.id, semester=semester, school_year_id=current_year.id if current_year else 1
        ).all()
    }

    students = Student.query.filter_by(class_id=selected_class.id).all()
    # Map CNE normalisé -> données de notes
    cne_map = {}
    for st in students:
        clean_cne = st.cne.strip().upper()
        eval_rec = saved_evals.get(st.id)
        final_act = eval_rec.final_score if eval_rec else compute_student_activity_evaluation(st, semester, selected_class, current_year, None)['final_score']
        ex_rec = exam_grades.get(st.id)

        cne_map[clean_cne] = {
            'activity': final_act,
            'cc1': ex_rec.cc1 if ex_rec else None,
            'cc2': ex_rec.cc2 if ex_rec else None,
            'cc3': ex_rec.cc3 if ex_rec else None,
            'cc4': ex_rec.cc4 if ex_rec else None
        }

    try:
        wb = openpyxl.load_workbook(file)
        ws = wb.active

        # Recherche de la ligne d'en-tête et des colonnes cibles
        header_row_idx = None
        col_cne = None
        col_act = None
        col_cc1 = None
        col_cc2 = None
        col_cc3 = None
        col_cc4 = None

        # Parcourir les 35 premières lignes pour trouver l'en-tête du tableau MASSAR
        for r in range(1, min(35, ws.max_row + 1)):
            row_vals = [str(ws.cell(r, c).value or '').strip() for c in range(1, ws.max_column + 1)]
            row_str = ' '.join(row_vals).lower()

            if any(k in row_str for k in ['الرمز', 'رمز', 'cne', 'massar', 'code élève', 'code eleve', 'matricule']):
                header_row_idx = r
                for c_idx, val in enumerate(row_vals, start=1):
                    v_clean = val.lower()
                    if any(k in v_clean for k in ['الرمز', 'رمز', 'cne', 'massar', 'code', 'matricule']):
                        if not col_cne: col_cne = c_idx
                    elif any(k in v_clean for k in ['أنشطة', 'انشطة', 'activité', 'activite', 'مندمجة', 'منذمجة']):
                        col_act = c_idx
                    elif any(k in v_clean for k in ['مراقبة 1', 'المراقبة 1', 'م.م 1', 'cc1', 'controle 1', 'contrôle 1']):
                        col_cc1 = c_idx
                    elif any(k in v_clean for k in ['مراقبة 2', 'المراقبة 2', 'م.م 2', 'cc2', 'controle 2', 'contrôle 2']):
                        col_cc2 = c_idx
                    elif any(k in v_clean for k in ['مراقبة 3', 'المراقبة 3', 'م.م 3', 'cc3', 'controle 3', 'contrôle 3']):
                        col_cc3 = c_idx
                    elif any(k in v_clean for k in ['مراقبة 4', 'المراقبة 4', 'م.م 4', 'cc4', 'controle 4', 'contrôle 4']):
                        col_cc4 = c_idx
                break

        if not col_cne:
            flash("La colonne d'identification des élèves (CNE / Code MASSAR) n'a pas pu être détectée dans le fichier.", "danger")
            return redirect(url_for('activity.semester_evaluations', class_id=class_id, semester=semester))

        injected_students = 0
        start_r = (header_row_idx + 1) if header_row_idx else 2

        for r in range(start_r, ws.max_row + 1):
            cell_cne = ws.cell(r, col_cne).value
            if not cell_cne:
                continue
            cne_val = str(cell_cne).strip().upper()
            if cne_val in cne_map:
                grades = cne_map[cne_val]
                # Injection Note d'Activité (Formule B)
                if col_act and grades['activity'] is not None:
                    ws.cell(r, col_act, value=grades['activity'])
                # Injection Contrôles Continus (si renseignés)
                if col_cc1 and grades['cc1'] is not None:
                    ws.cell(r, col_cc1, value=grades['cc1'])
                if col_cc2 and grades['cc2'] is not None:
                    ws.cell(r, col_cc2, value=grades['cc2'])
                if col_cc3 and grades['cc3'] is not None:
                    ws.cell(r, col_cc3, value=grades['cc3'])
                if col_cc4 and grades['cc4'] is not None:
                    ws.cell(r, col_cc4, value=grades['cc4'])

                injected_students += 1

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        filename = f"MASSAR_Complete_{selected_class.name}_S{semester}.xlsx"
        return send_file(
            output,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        flash(f"Erreur lors du traitement du fichier Excel : {str(e)}", "danger")
        return redirect(url_for('activity.semester_evaluations', class_id=class_id, semester=semester))



