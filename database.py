from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()

# Optimisations SQLite automatiques à chaque connexion (WAL, Cache mémoire 64Mo, Temp en RAM)
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA cache_size = -64000")  # 64 Mo de cache RAM
        cursor.execute("PRAGMA temp_store = MEMORY")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()
    except Exception:
        pass

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120), nullable=True, default='Enseignant')
    
    # Sécurité renforcée : 2FA (TOTP), verrouillage de compte et historique
    totp_secret = db.Column(db.String(64), nullable=True)
    is_totp_enabled = db.Column(db.Boolean, default=False)
    failed_login_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    last_login_ip = db.Column(db.String(45), nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_locked(self):
        if self.locked_until and self.locked_until > datetime.now():
            return True
        return False

class LoginLog(db.Model):
    __tablename__ = 'login_logs'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), nullable=False) # 'SUCCESS', 'FAILED', 'LOCKED', '2FA_FAILED'
    details = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)

class AppSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False, index=True)
    value = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(255), nullable=True)

    _cache = {}

    @classmethod
    def get_value(cls, key, default=''):
        # Lecture ultra-rapide depuis le cache RAM si disponible
        if key in cls._cache:
            return cls._cache[key]
        setting = cls.query.filter_by(key=key).first()
        val = setting.value if setting else default
        cls._cache[key] = val
        return val

    @classmethod
    def set_value(cls, key, value, description=None):
        setting = cls.query.filter_by(key=key).first()
        if not setting:
            setting = cls(key=key, value=str(value), description=description)
            db.session.add(setting)
        else:
            setting.value = str(value)
            if description:
                setting.description = description
        db.session.commit()
        cls._cache[key] = str(value)
        return setting

    @classmethod
    def clear_cache(cls):
        cls._cache.clear()


# Association tables for TextbookSession
session_objectives = db.Table('session_objectives',
    db.Column('session_id', db.Integer, db.ForeignKey('textbook_session.id'), primary_key=True),
    db.Column('objective_id', db.Integer, db.ForeignKey('objective.id'), primary_key=True)
)

session_competencies = db.Table('session_competencies',
    db.Column('session_id', db.Integer, db.ForeignKey('textbook_session.id'), primary_key=True),
    db.Column('competency_id', db.Integer, db.ForeignKey('competency.id'), primary_key=True)
)

class Class(db.Model):
    __table_args__ = (
        db.UniqueConstraint('name', 'school_year_id', name='_class_school_year_uc'),
    )
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    school_year_id = db.Column(db.Integer, db.ForeignKey('school_year.id'), nullable=False, index=True)
    students = db.relationship('Student', backref='student_class', lazy=True, cascade="all, delete-orphan")
    activities = db.relationship('Activity', backref='classe', lazy=True, cascade="all, delete-orphan")
    textbook_sessions = db.relationship('TextbookSession', backref='classe', lazy=True, cascade="all, delete-orphan")
    school_year = db.relationship('SchoolYear', backref=db.backref('classes', lazy=True, cascade="all, delete-orphan"))


class Student(db.Model):
    __table_args__ = (
        db.Index('idx_student_class_name', 'class_id', 'last_name', 'first_name'),
        db.Index('idx_student_class_order', 'class_id', 'order_num', 'last_name'),
    )
    id = db.Column(db.Integer, primary_key=True)
    cne = db.Column(db.String(20), unique=True, nullable=False, index=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    birth_date = db.Column(db.Date, nullable=True)
    gender = db.Column(db.String(10), nullable=True) # 'M', 'F'
    photo_path = db.Column(db.String(200), nullable=True)
    last_email_sent_at = db.Column(db.DateTime, nullable=True)
    order_num = db.Column(db.Integer, nullable=True, default=0, index=True) # Ordre d'origine du fichier Excel importé
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=False, index=True)
    absences = db.relationship('Absence', backref='student', lazy=True, cascade="all, delete-orphan")
    grades = db.relationship('Grade', backref='student_record', lazy=True, cascade="all, delete-orphan")

    @classmethod
    def default_order(cls):
        """Retourne les critères de tri : priorité à l'ordre d'origine Excel (order_num), puis nom et prénom."""
        return [
            db.case((cls.order_num > 0, cls.order_num), else_=999999),
            cls.last_name.asc(),
            cls.first_name.asc()
        ]

    def get_photo_url(self):
        if self.photo_path:
            return f"/uploads/{self.photo_path}"
        return "/uploads/photos/Default.jpg"

    def to_dict(self):
        return {
            'id': self.id,
            'cne': self.cne,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'email': self.email,
            'phone': self.phone,
            'gender': self.gender,
            'photo_url': self.get_photo_url(),
            'class_id': self.class_id,
        }

class Absence(db.Model):
    __table_args__ = (
        db.Index('idx_absence_student_date', 'student_id', 'date'),
    )
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    justified = db.Column(db.Boolean, default=False)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False, index=True)
    reason = db.Column(db.String(200), nullable=True)

    def to_dict(self):
        student = getattr(self, 'student', None)
        student_name = f"{student.first_name} {student.last_name}" if student else ""
        return {
            'id': self.id,
            'date': self.date.strftime('%Y-%m-%d'),
            'justified': self.justified,
            'student_id': self.student_id,
            'student_name': student_name
        }

class Activity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=True)                 # Titre / Sujet explicite
    description = db.Column(db.Text, nullable=True)                  # Consignes & directives
    module = db.Column(db.String(100), nullable=False)
    section = db.Column(db.String(100), nullable=False)
    activity_type = db.Column(db.String(50), nullable=False)         # TP, TD, Projet, Exercice, etc.
    date = db.Column(db.Date, nullable=False, default=lambda: datetime.now(timezone.utc).date()) # Date de séance / assignation
    due_date = db.Column(db.Date, nullable=True)                     # Date limite de rendu / réalisation
    max_score = db.Column(db.Float, default=20.0, nullable=True)     # Barème (ex: 10, 20, 40)
    coefficient = db.Column(db.Float, default=1.0, nullable=True)    # Coefficient de pondération
    status = db.Column(db.String(20), default='Assigné', nullable=True) # 'Assigné', 'En cours', 'Terminé'
    work_mode = db.Column(db.String(20), default='Individuel', nullable=True) # 'Individuel', 'Binôme', 'Groupe'
    target_group = db.Column(db.String(20), default='all', nullable=True) # 'all', 'g1', 'g2', 'custom'

    # Liaison avec le module Ressources
    resource_id = db.Column(db.Integer, db.ForeignKey('course_resource.id'), nullable=True, index=True)
    resource = db.relationship('CourseResource', backref=db.backref('linked_activities', lazy=True))

    groups_json = db.Column(db.Text, nullable=True) # Stocke la composition JSON des groupes de travail

    grades = db.relationship('Grade', backref='activity', lazy=True, cascade="all, delete-orphan")

    @property
    def display_title(self):
        if self.title and self.title.strip():
            return self.title.strip()
        return f"{self.activity_type} ({self.date.strftime('%d/%m/%Y')})"

    @property
    def groups_list(self):
        """Retourne la liste structurée des groupes depuis groups_json ou inférée depuis grades."""
        if self.groups_json:
            try:
                import json
                data = json.loads(self.groups_json)
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        
        # Fallback : regrouper depuis les notes existantes
        groups_map = {}
        for g in self.grades or []:
            if g.group_number and g.group_number.strip():
                grp_name = g.group_number.strip()
                if grp_name not in groups_map:
                    groups_map[grp_name] = {
                        'id': len(groups_map) + 1,
                        'name': grp_name,
                        'members': []
                    }
                groups_map[grp_name]['members'].append(g.student_id)
        return list(groups_map.values())

    @property
    def groups_summary(self):
        """Texte synthétique des types de groupes formés (ex: '8 Binômes, 1 Trinôme, 1 Individuel')."""
        grps = self.groups_list
        if not grps:
            return ""
        
        counts = {}
        for g in grps:
            m_count = len(g.get('members', []))
            if m_count == 1:
                t = "Individuel"
            elif m_count == 2:
                t = "Binôme"
            elif m_count == 3:
                t = "Trinôme"
            elif m_count == 4:
                t = "Quatuor"
            else:
                t = f"Groupe ({m_count})"
            counts[t] = counts.get(t, 0) + 1

        parts = []
        for t, c in counts.items():
            plural = f"{c} {t}s" if c > 1 and not t.endswith('s') else f"{c} {t}"
            parts.append(plural)
        return f"{len(grps)} groupe(s) : " + ", ".join(parts)

    @property
    def completion_stats(self):
        """Renvoie un résumé du suivi de réalisation : total assigné, fait, non fait, etc."""
        grades_list = self.grades or []
        assigned = [g for g in grades_list if g.status in ['Assigné', 'Fait', 'Non fait']]
        total_assigned = len(assigned)
        done_count = sum(1 for g in assigned if g.status == 'Fait' or (g.score is not None and g.score > 0 and g.status != 'Non fait'))
        not_done_count = sum(1 for g in assigned if g.status == 'Non fait')
        pending_count = total_assigned - done_count - not_done_count

        pct = round((done_count / total_assigned * 100), 1) if total_assigned > 0 else 0.0

        return {
            'total': total_assigned,
            'done': done_count,
            'not_done': not_done_count,
            'pending': pending_count,
            'assigned': pending_count,
            'percent': pct
        }

    @property
    def is_overdue(self):
        if not self.due_date:
            return False
        today = datetime.now().date()
        stats = self.completion_stats
        return today > self.due_date and stats['done'] < stats['total']

class Grade(db.Model):
    __table_args__ = (
        db.Index('idx_grade_student_activity', 'student_id', 'activity_id'),
    )
    id = db.Column(db.Integer, primary_key=True)
    activity_id = db.Column(db.Integer, db.ForeignKey('activity.id'), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False, index=True)
    score = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='Non assigné') # 'Non assigné', 'Assigné', 'Fait', 'Non fait'
    feedback = db.Column(db.Text, nullable=True)             # Appréciation / Remarque individualisée
    group_number = db.Column(db.String(50), nullable=True)   # Numéro de groupe/binôme (ex: "Groupe 1")


class FlashParticipation(db.Model):
    __table_args__ = (
        db.Index('idx_flash_student_date', 'student_id', 'date'),
        db.Index('idx_flash_class_sem', 'class_id', 'semester'),
    )
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False, index=True)
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, default=lambda: datetime.now(timezone.utc).date())
    points = db.Column(db.Float, default=1.0, nullable=False) # e.g. 0.5, 1.0, 2.0
    label = db.Column(db.String(200), nullable=True) # e.g. "Question flash intro C", "Participation orale"
    semester = db.Column(db.Integer, default=1, nullable=False) # 1 or 2
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    student = db.relationship('Student', backref=db.backref('flash_participations', lazy=True, cascade="all, delete-orphan"))
    classe = db.relationship('Class', backref=db.backref('flash_participations', lazy=True, cascade="all, delete-orphan"))

    def to_dict(self):
        st = self.student
        return {
            'id': self.id,
            'student_id': self.student_id,
            'student_name': f"{st.first_name} {st.last_name}" if st else "Élève inconnu",
            'class_id': self.class_id,
            'date': self.date.strftime('%d/%m/%Y'),
            'points': self.points,
            'label': self.label or "Question Flash",
            'semester': self.semester
        }


class SemesterActivityEvaluation(db.Model):
    __table_args__ = (
        db.UniqueConstraint('student_id', 'semester', 'school_year_id', name='_student_sem_year_uc'),
        db.Index('idx_sem_eval_class', 'class_id', 'semester'),
    )
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False, index=True)
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=False, index=True)
    semester = db.Column(db.Integer, default=1, nullable=False) # 1 or 2
    school_year_id = db.Column(db.Integer, db.ForeignKey('school_year.id'), nullable=False, index=True)

    base_score = db.Column(db.Float, default=0.0, nullable=False) # Moyenne pondérée des TP/TD (/20)
    bonus_score = db.Column(db.Float, default=0.0, nullable=False) # Bonus questions flash/participation
    malus_score = db.Column(db.Float, default=0.0, nullable=False) # Malus absences/assiduité
    manual_adjustment = db.Column(db.Float, default=0.0, nullable=False) # Ajustement prof (+/-)
    final_score = db.Column(db.Float, default=0.0, nullable=False) # Note finale bornée [0, 20]
    appreciation = db.Column(db.Text, nullable=True) # Remarque bulletin / MASSAR
    is_locked = db.Column(db.Boolean, default=False, nullable=False) # Si verrouillé/validé
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    student = db.relationship('Student', backref=db.backref('semester_evaluations', lazy=True, cascade="all, delete-orphan"))
    classe = db.relationship('Class', backref=db.backref('semester_evaluations', lazy=True, cascade="all, delete-orphan"))
    school_year = db.relationship('SchoolYear', backref=db.backref('semester_evaluations', lazy=True, cascade="all, delete-orphan"))


class CourseModule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    sections = db.relationship('CourseSection', backref='module', lazy=True, cascade="all, delete-orphan")

class CourseSection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False) # Name is no longer globally unique, only per module ideally. 
    module_id = db.Column(db.Integer, db.ForeignKey('course_module.id'), nullable=False)
    objectives = db.relationship('Objective', backref='section', lazy=True, cascade="all, delete-orphan")
    __table_args__ = (db.UniqueConstraint('name', 'module_id', name='_name_module_uc'),)

class Objective(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(500), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey('course_section.id'), nullable=True)
    competency_id = db.Column(db.Integer, db.ForeignKey('competency.id'), nullable=True)
    competency = db.relationship('Competency', backref=db.backref('objectives', lazy=True, cascade="all, delete-orphan"))

class Competency(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(500), nullable=False)
    module_id = db.Column(db.Integer, db.ForeignKey('course_module.id'), nullable=True)
    module = db.relationship('CourseModule', backref=db.backref('competencies', lazy=True, cascade="all, delete-orphan"))

class GeneralCompetency(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(500), nullable=False)

class ActivityType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

class Institution(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.String(200), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    logo_path = db.Column(db.String(200), nullable=True)
    years = db.relationship('SchoolYear', backref='institution', lazy=True, cascade="all, delete-orphan")

class SchoolYear(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False) # e.g. "2025-2026"
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    institution_id = db.Column(db.Integer, db.ForeignKey('institution.id'), nullable=False)
    is_active = db.Column(db.Boolean, default=False)
    is_archived = db.Column(db.Boolean, default=False)
    sessions = db.relationship('TextbookSession', backref='year', lazy=True, cascade="all, delete-orphan")

    def is_read_only(self):
        """Vérifie si l'année est verrouillée (soit archivée manuellement, soit passée et non active)."""
        if self.is_archived:
            return True
        today = datetime.now(timezone.utc).date()
        if self.end_date < today and not self.is_active:
            return True
        return False

class TextbookSession(db.Model):
    __table_args__ = (
        db.Index('idx_textbook_class_date', 'class_id', 'date'),
    )
    id = db.Column(db.Integer, primary_key=True)
    
    # Structured Linkage
    module_id = db.Column(db.Integer, db.ForeignKey('course_module.id'), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey('course_section.id'), nullable=False)
    
    # Relationships to content
    module = db.relationship('CourseModule')
    section = db.relationship('CourseSection')
    
    objectives = db.relationship('Objective', secondary=session_objectives, lazy='subquery',
        backref=db.backref('sessions', lazy=True))
    competencies = db.relationship('Competency', secondary=session_competencies, lazy='subquery',
        backref=db.backref('sessions', lazy=True))

    course_titles = db.Column(db.Text, nullable=False) # Only this remains as free text/summary
    remark = db.Column(db.Text, nullable=True)
    date = db.Column(db.Date, nullable=False, default=lambda: datetime.now(timezone.utc).date())
    
    # Relationships
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=False)
    school_year_id = db.Column(db.Integer, db.ForeignKey('school_year.id'), nullable=False)
    teacher_name = db.Column(db.String(100), nullable=True)

    # Nouveaux champs pédagogiques
    session_type = db.Column(db.String(50), default='Cours', nullable=True)  # 'Cours', 'TP', 'TD', 'Évaluation', 'Projet', 'Soutien'
    homework = db.Column(db.Text, nullable=True)  # Travail à faire / Devoirs
    homework_due_date = db.Column(db.Date, nullable=True)  # Date limite de remise / échéance
    start_time = db.Column(db.String(10), nullable=True, default='08:30')  # Heure de début (ex: "08:30")
    end_time = db.Column(db.String(10), nullable=True, default='10:30')    # Heure de fin (ex: "10:30")
    duration_hours = db.Column(db.Float, nullable=True, default=2.0)       # Volume horaire en heures (ex: 2.0)

class PendingStudent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cne = db.Column(db.String(20), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    birth_date = db.Column(db.Date, nullable=True)
    photo_path = db.Column(db.String(200), nullable=True)
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=False)
    device_type = db.Column(db.String(50), nullable=True)  # 'Mobile', 'Desktop', 'Tablet'
    status = db.Column(db.String(20), default='pending')  # 'pending', 'approved', 'rejected'
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    classe = db.relationship('Class', backref=db.backref('pending_students', lazy=True, cascade="all, delete-orphan"))

    def get_photo_url(self):
        if self.photo_path:
            return f"/uploads/{self.photo_path}"
        return "/uploads/photos/Default.jpg"

    def to_dict(self):
        return {
            'id': self.id,
            'cne': self.cne,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'email': self.email or '',
            'phone': self.phone or '',
            'birth_date': self.birth_date.strftime('%Y-%m-%d') if self.birth_date else '',
            'class_id': self.class_id,
            'class_name': self.classe.name if self.classe else '',
            'photo_url': self.get_photo_url(),
            'photo_path': self.photo_path or 'photos/Default.jpg',
            'device_type': self.device_type or 'Inconnu',
            'status': self.status,
            'created_at': self.created_at.strftime('%H:%M:%S') if self.created_at else ''
        }

class TimeSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False) # e.g. "M1 (Matin 1)" or "08:30 - 10:30"
    start_time = db.Column(db.String(10), nullable=False) # "08:30"
    end_time = db.Column(db.String(10), nullable=False)   # "10:30"
    duration_hours = db.Column(db.Float, nullable=False, default=2.0) # 2.0 or 1.0
    is_active = db.Column(db.Boolean, default=True)
    order_num = db.Column(db.Integer, default=1)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration_hours': self.duration_hours,
            'is_active': self.is_active,
            'order_num': self.order_num
        }

class ScheduleEntry(db.Model):
    """Représente un créneau dans l'emploi du temps hebdomadaire de l'enseignant."""
    id = db.Column(db.Integer, primary_key=True)
    day_of_week = db.Column(db.Integer, nullable=False) # 0=Lundi, 1=Mardi, 2=Mercredi, 3=Jeudi, 4=Vendredi, 5=Samedi
    time_slot_id = db.Column(db.Integer, db.ForeignKey('time_slot.id'), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=False)
    school_year_id = db.Column(db.Integer, db.ForeignKey('school_year.id'), nullable=False)
    room = db.Column(db.String(50), nullable=True, default='Salle Info 1') # Salle de cours / Labo
    subject_title = db.Column(db.String(100), nullable=True, default='Informatique') # Matière ou intitulé
    color = db.Column(db.String(20), nullable=True, default='#4e73df') # Couleur d'affichage dans le planning

    # Relations
    time_slot = db.relationship('TimeSlot', backref=db.backref('schedule_entries', lazy=True, cascade='all, delete-orphan'))
    classe = db.relationship('Class', backref=db.backref('schedule_entries', lazy=True, cascade='all, delete-orphan'))
    year = db.relationship('SchoolYear', backref=db.backref('schedule_entries', lazy=True, cascade='all, delete-orphan'))

    DAY_NAMES = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi']

    @property
    def day_name(self):
        if 0 <= self.day_of_week < len(self.DAY_NAMES):
            return self.DAY_NAMES[self.day_of_week]
        return 'Inconnu'

    def to_dict(self):
        return {
            'id': self.id,
            'day_of_week': self.day_of_week,
            'day_name': self.day_name,
            'time_slot_id': self.time_slot_id,
            'slot_name': self.time_slot.name if self.time_slot else '',
            'start_time': self.time_slot.start_time if self.time_slot else '',
            'end_time': self.time_slot.end_time if self.time_slot else '',
            'duration_hours': self.time_slot.duration_hours if self.time_slot else 2.0,
            'class_id': self.class_id,
            'class_name': self.classe.name if self.classe else '',
            'room': self.room or '',
            'subject_title': self.subject_title or 'Informatique',
            'color': self.color or '#4e73df'
        }


class CourseResource(db.Model):
    """Ressource pédagogique (Cours, Présentation, TP, TD, Projet) classée par Module et Chapitre."""
    __tablename__ = 'course_resource'
    __table_args__ = (
        db.Index('idx_resource_module_section', 'module_id', 'section_id'),
        db.Index('idx_resource_token', 'share_token', unique=True),
    )

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    resource_type = db.Column(db.String(50), nullable=False, default='Cours')  # 'Cours', 'Présentation', 'TP', 'TD', 'Projet'
    file_type = db.Column(db.String(20), nullable=False, default='document')   # 'document', 'image', 'link'
    file_path = db.Column(db.String(300), nullable=True)                       # Chemin relatif dans uploads/
    external_url = db.Column(db.String(500), nullable=True)                    # URL externe si lien
    original_filename = db.Column(db.String(255), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)                           # Taille en octets
    
    # Classification par Module et Chapitre (Section)
    module_id = db.Column(db.Integer, db.ForeignKey('course_module.id'), nullable=False, index=True)
    section_id = db.Column(db.Integer, db.ForeignKey('course_section.id'), nullable=True, index=True)
    target_class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=True, index=True)

    # Partage & Visibilité (Options A + B)
    is_public = db.Column(db.Boolean, default=True, nullable=False)            # Commutateur Actif/Inactif
    share_token = db.Column(db.String(64), unique=True, nullable=False, index=True) # Jeton unique
    pin_code = db.Column(db.String(20), nullable=True)                         # Code PIN d'accès optionnel
    expires_at = db.Column(db.DateTime, nullable=True)                         # Date/Heure d'expiration optionnelle
    download_count = db.Column(db.Integer, default=0, nullable=False)          # Compteur de téléchargements

    # Dates de suivi
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relations
    module = db.relationship('CourseModule', backref=db.backref('resources', lazy=True, cascade='all, delete-orphan'))
    section = db.relationship('CourseSection', backref=db.backref('resources', lazy=True))
    target_class = db.relationship('Class', backref=db.backref('assigned_resources', lazy=True))

    @property
    def is_expired(self):
        if not self.expires_at:
            return False
        now = datetime.now() if self.expires_at.tzinfo is None else datetime.now(timezone.utc)
        return now > self.expires_at

    @property
    def is_accessible(self):
        return self.is_public and not self.is_expired

    @property
    def file_extension(self):
        if self.original_filename and '.' in self.original_filename:
            return self.original_filename.rsplit('.', 1)[1].lower()
        elif self.file_path and '.' in self.file_path:
            return self.file_path.rsplit('.', 1)[1].lower()
        return ''

    @property
    def human_file_size(self):
        if not self.file_size:
            return ''
        bytes_val = self.file_size
        if bytes_val < 1024:
            return f"{bytes_val} o"
        elif bytes_val < 1024 * 1024:
            return f"{round(bytes_val / 1024, 1)} Ko"
        else:
            return f"{round(bytes_val / (1024 * 1024), 2)} Mo"

    @property
    def icon_class(self):
        ext = self.file_extension
        if self.file_type == 'link':
            return 'fas fa-link text-info'
        elif self.file_type == 'image' or ext in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg']:
            return 'fas fa-file-image text-success'
        elif ext == 'pdf':
            return 'fas fa-file-pdf text-danger'
        elif ext in ['doc', 'docx']:
            return 'fas fa-file-word text-primary'
        elif ext in ['xls', 'xlsx', 'csv']:
            return 'fas fa-file-excel text-success'
        elif ext in ['ppt', 'pptx']:
            return 'fas fa-file-powerpoint text-warning'
        elif ext in ['zip', 'rar', '7z', 'tar', 'gz']:
            return 'fas fa-file-archive text-secondary'
        elif ext in ['py', 'c', 'cpp', 'java', 'html', 'css', 'js', 'json', 'sql']:
            return 'fas fa-file-code text-dark'
        return 'fas fa-file-alt text-muted'

    @property
    def type_badge_color(self):
        types_map = {
            'Cours': 'primary',         # Bleu
            'Présentation': 'info',     # Cyan/Violet
            'TP': 'success',            # Émeraude
            'TD': 'warning',            # Ambre
            'Projet': 'danger'          # Rouge
        }
        return types_map.get(self.resource_type, 'secondary')


# ─── CATALOGUE DE BADGES PÉDAGOGIQUES (GAMIFICATION INFORMATIQUE) ────────────

BADGE_DEFINITIONS = {
    'bug_hunter': {
        'title': 'Bug Hunter',
        'desc': 'A débusqué et résolu un bogue ou une erreur algorithmique complexe.',
        'icon': 'fas fa-bug',
        'color': 'danger'
    },
    'mentor': {
        'title': 'Code Mentor',
        'desc': 'Entraide remarquable, patience et soutien technique apporté aux camarades.',
        'icon': 'fas fa-hands-helping',
        'color': 'info'
    },
    'fast_coder': {
        'title': 'Fast Coder',
        'desc': 'A achevé le TP ou défi algorithmique avec rapidité et grande précision.',
        'icon': 'fas fa-bolt',
        'color': 'warning'
    },
    'clean_code': {
        'title': 'Clean Code',
        'desc': 'Code impeccable : rigueur syntaxique, indentation parfaite et clarté.',
        'icon': 'fas fa-shield-alt',
        'color': 'success'
    },
    'active': {
        'title': 'Super Actif',
        'desc': 'Participation constante, réactivité et dynamisme exemplaire en séance.',
        'icon': 'fas fa-star',
        'color': 'primary'
    }
}


class StudentBadge(db.Model):
    """Badge d'encouragement et de compétence pédagogique attribué à un élève."""
    __tablename__ = 'student_badge'
    __table_args__ = (
        db.Index('idx_student_badge_student_key', 'student_id', 'badge_key'),
    )
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False, index=True)
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=False, index=True)
    school_year_id = db.Column(db.Integer, db.ForeignKey('school_year.id'), nullable=True)
    badge_key = db.Column(db.String(50), nullable=False) # 'bug_hunter', 'mentor', 'fast_coder', 'clean_code', 'active'
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255), nullable=True)
    icon = db.Column(db.String(50), default='fas fa-award', nullable=True)
    color = db.Column(db.String(50), default='warning', nullable=True)
    awarded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    student = db.relationship('Student', backref=db.backref('badges', lazy=True, cascade="all, delete-orphan"))
    classe = db.relationship('Class')
    school_year = db.relationship('SchoolYear')

    def to_dict(self):
        return {
            'id': self.id,
            'student_id': self.student_id,
            'class_id': self.class_id,
            'badge_key': self.badge_key,
            'title': self.title,
            'description': self.description,
            'icon': self.icon,
            'color': self.color,
            'awarded_at': self.awarded_at.strftime('%d/%m/%Y %H:%M') if self.awarded_at else ''
        }


class SemesterExamGrade(db.Model):
    """Stocke les 4 notes de Contrôles Continus (CC1, CC2, CC3, CC4) par élève et par semestre."""
    __tablename__ = 'semester_exam_grade'
    __table_args__ = (
        db.UniqueConstraint('student_id', 'semester', 'school_year_id', name='_student_exam_sem_uc'),
        db.Index('idx_exam_student_sem', 'student_id', 'semester'),
    )
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False, index=True)
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=False, index=True)
    school_year_id = db.Column(db.Integer, db.ForeignKey('school_year.id'), nullable=False, index=True)
    semester = db.Column(db.Integer, default=1, nullable=False) # 1 ou 2
    cc1 = db.Column(db.Float, nullable=True) # Contrôle Continu 1 /20
    cc2 = db.Column(db.Float, nullable=True) # Contrôle Continu 2 /20
    cc3 = db.Column(db.Float, nullable=True) # Contrôle Continu 3 /20
    cc4 = db.Column(db.Float, nullable=True) # Contrôle Continu 4 /20
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    student = db.relationship('Student', backref=db.backref('exam_grades', lazy=True, cascade="all, delete-orphan"))
    classe = db.relationship('Class')
    school_year = db.relationship('SchoolYear')

    @property
    def cc_average(self):
        """Moyenne des contrôles continus renseignés."""
        notes = [n for n in [self.cc1, self.cc2, self.cc3, self.cc4] if n is not None]
        if not notes:
            return None
        return round(sum(notes) / len(notes), 2)

    def to_dict(self):
        return {
            'id': self.id,
            'student_id': self.student_id,
            'class_id': self.class_id,
            'semester': self.semester,
            'cc1': self.cc1,
            'cc2': self.cc2,
            'cc3': self.cc3,
            'cc4': self.cc4,
            'cc_average': self.cc_average
        }


class ExceptionalEvent(db.Model):
    """Enregistre les jours ou périodes exceptionnelles (Grève, Événement établissement, Férié imprévu, etc.)."""
    __tablename__ = 'exceptional_event'
    __table_args__ = (
        db.Index('idx_event_date_period', 'date', 'period'),
        db.Index('idx_event_class_year', 'class_id', 'school_year_id'),
    )

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)                         # Ex: "Grève générale", "Journée Portes Ouvertes"
    event_type = db.Column(db.String(50), nullable=False, default='greve')    # 'greve', 'evenement', 'intemperies', 'formation', 'autre'
    date = db.Column(db.Date, nullable=False, index=True)
    period = db.Column(db.String(20), nullable=False, default='all_day')      # 'all_day', 'morning', 'afternoon', 'custom'
    start_time = db.Column(db.String(10), nullable=True, default='08:00')     # Optionnel si custom ou demi-journée
    end_time = db.Column(db.String(10), nullable=True, default='18:00')
    description = db.Column(db.Text, nullable=True)                           # Explications / Consignes administratives
    class_id = db.Column(db.Integer, db.ForeignKey('class.id'), nullable=True) # NULL si tout l'établissement / toutes les classes
    school_year_id = db.Column(db.Integer, db.ForeignKey('school_year.id'), nullable=False, index=True)
    sync_textbook = db.Column(db.Boolean, default=True, nullable=False)       # Traçabilité dans le cahier de texte
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relations
    classe = db.relationship('Class', backref=db.backref('exceptional_events', lazy=True, cascade='all, delete-orphan'))
    school_year = db.relationship('SchoolYear', backref=db.backref('exceptional_events', lazy=True, cascade='all, delete-orphan'))

    @property
    def period_display(self):
        labels = {
            'all_day': 'Journée entière',
            'morning': 'Matinée (Matin)',
            'afternoon': 'Après-midi',
            'custom': f'{self.start_time or "08:00"} - {self.end_time or "18:00"}'
        }
        return labels.get(self.period, self.period)

    @property
    def type_display(self):
        labels = {
            'greve': 'Mouvement de Grève',
            'evenement': 'Événement Établissement',
            'intemperies': 'Intempéries / Force Majeure',
            'formation': 'Formation Pédagogique',
            'autre': 'Autre Motif Exceptionnel'
        }
        return labels.get(self.event_type, self.event_type)

    @property
    def badge_color(self):
        colors = {
            'greve': 'danger',
            'evenement': 'warning',
            'intemperies': 'info',
            'formation': 'primary',
            'autre': 'secondary'
        }
        return colors.get(self.event_type, 'secondary')

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'event_type': self.event_type,
            'type_display': self.type_display,
            'badge_color': self.badge_color,
            'date': self.date.strftime('%Y-%m-%d'),
            'date_fr': self.date.strftime('%d/%m/%Y'),
            'period': self.period,
            'period_display': self.period_display,
            'start_time': self.start_time or '',
            'end_time': self.end_time or '',
            'description': self.description or '',
            'class_id': self.class_id,
            'class_name': self.classe.name if self.classe else 'Toutes les classes',
            'sync_textbook': self.sync_textbook,
            'created_at': self.created_at.strftime('%d/%m/%Y %H:%M') if self.created_at else ''
        }



