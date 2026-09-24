import os
import io
import re
import json
import locale
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime, timezone
import pandas as pd
import csv
from werkzeug.utils import secure_filename
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, session, send_file, make_response
from flask_mail import Message  # type: ignore[import-untyped]
from database import db, Class, Student, Absence, Activity, Grade, AppSetting, SchoolYear, ScheduleEntry, StudentBadge, BADGE_DEFINITIONS, ExceptionalEvent, CourseModule, CourseSection, TextbookSession
from extensions import mail
from blueprints.configuration import DEFAULT_MAIL_TEMPLATES

# Correctif de compatibilité global openpyxl pour styles avec extLst
try:
    import openpyxl
    from openpyxl.styles.fills import PatternFill, localname, Color
    if not getattr(PatternFill, '_extlst_patched', False):
        _orig_from_tree = PatternFill._from_tree
        @classmethod
        def _safe_from_tree(cls, el):
            try:
                return _orig_from_tree(el)
            except TypeError:
                attrib = dict(el.attrib)
                for child in el:
                    desc = localname(child)
                    if desc in ['fgColor', 'bgColor']:
                        attrib[desc] = Color.from_tree(child)
                valid_keys = {'patternType', 'fgColor', 'bgColor', 'fill_type', 'start_color', 'end_color'}
                return cls(**{k: v for k, v in attrib.items() if k in valid_keys})
        PatternFill._from_tree = _safe_from_tree
        PatternFill._extlst_patched = True
except Exception:
    pass

# Try to set locale to French for date formatting
try:
    locale.setlocale(locale.LC_TIME, 'fr_FR.UTF-8')
except Exception:
    try:
        locale.setlocale(locale.LC_TIME, 'fr_FR')
    except Exception:
        pass

absence_bp = Blueprint('absence', __name__)

def get_current_school_year():
    """Récupère l'année scolaire active pour la session courante."""
    year_id = session.get('active_school_year_id')
    if year_id:
        y = SchoolYear.query.get(year_id)
        if y: return y
    active = SchoolYear.query.filter_by(is_active=True).first()
    if active: return active
    return SchoolYear.query.order_by(SchoolYear.start_date.desc()).first()

@absence_bp.route('/')
def index():
    # Get view mode from query param or session, default to 'card'
    view_mode = request.args.get('view', session.get('view_mode', 'card'))
    session['view_mode'] = view_mode
    
    current_year = get_current_school_year()
    if current_year:
        classes = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
    else:
        classes = []

    # Cours du jour d'après l'emploi du temps
    today = datetime.now().date()
    today_weekday = today.weekday()
    today_schedule = []
    if current_year and today_weekday < 6:
        entries = ScheduleEntry.query.filter_by(
            school_year_id=current_year.id,
            day_of_week=today_weekday
        ).all()
        for e in entries:
            # Vérifier si l'appel a été fait aujourd'hui
            has_called = Absence.query.join(Student).filter(
                Student.class_id == e.class_id,
                Absence.date == today
            ).first() is not None

            today_schedule.append({
                'entry': e,
                'class_id': e.class_id,
                'class_name': e.classe.name if e.classe else '',
                'slot': e.time_slot,
                'room': e.room,
                'has_called': has_called
            })

    return render_template('index.html', 
                           classes=classes, 
                           view_mode=view_mode,
                           today_schedule=today_schedule,
                           today_date_str=today.strftime('%d/%m/%Y'))

# ==============================================================================
# HELPERS POUR L'IMPORTATION ROBUSTE DES ÉLÈVES (XLSX, CSV, ARABE & FRANÇAIS)
# ==============================================================================

def normalize_text(s):
    """Normalise un texte de manière universelle (Arabe sans diacritiques, Français sans accents)."""
    if s is None:
        return ""
    txt = str(s).strip()
    # Normalisation Arabe
    txt = re.sub(r'[\u064B-\u065F\u0670ـ]', '', txt)
    txt = re.sub(r'[إأآا]', 'ا', txt)
    txt = re.sub(r'ى', 'ي', txt)
    txt = re.sub(r'ة', 'ه', txt)
    # Normalisation accents français (retrait des marques Mn)
    txt = ''.join(c for c in unicodedata.normalize('NFD', txt) if unicodedata.category(c) != 'Mn')
    txt = re.sub(r'[^\w\s\u0600-\u06FF]', ' ', txt.lower())
    return " ".join(txt.split())

def names_similarity(name1, name2):
    """Calcule la ressemblance entre deux noms (en Arabe ou en Français) indépendamment de l'ordre."""
    c1 = normalize_text(name1)
    c2 = normalize_text(name2)
    if not c1 or not c2:
        return 0.0
    if c1 == c2:
        return 1.0
    words1 = sorted(c1.split())
    words2 = sorted(c2.split())
    if words1 == words2:
        return 0.99
    r1 = SequenceMatcher(None, c1, c2).ratio()
    r2 = SequenceMatcher(None, " ".join(words1), " ".join(words2)).ratio()
    return max(r1, r2)

def parse_birth_date(bd_val):
    """Parse une date de naissance sous divers formats courants."""
    if bd_val is None or pd.isna(bd_val):
        return None
    if isinstance(bd_val, (datetime, pd.Timestamp)):
        return bd_val.date() if hasattr(bd_val, 'date') else bd_val
    val_str = str(bd_val).strip()
    if not val_str or val_str.lower() in ('', 'none', 'nan', 'nat'):
        return None
    for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(val_str, fmt).date()
        except ValueError:
            pass
    try:
        dt = pd.to_datetime(val_str, dayfirst=True)
        if pd.isna(dt):
            return None
        return dt.date()
    except Exception:
        return None

def extract_rows_from_file_bytes(file_bytes, filename):
    """Extrait la liste des lignes d'un fichier .xlsx, .csv ou .txt sous forme de liste de tuples/listes."""
    lower = filename.lower()
    if lower.endswith('.csv') or lower.endswith('.txt'):
        raw = file_bytes.read()
        for enc in ['utf-8-sig', 'utf-8', 'cp1256', 'windows-1256', 'iso-8859-1', 'windows-1252']:
            try:
                decoded = raw.decode(enc)
                sample = decoded[:2048]
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
                    delimiter = dialect.delimiter
                except Exception:
                    delimiter = ';' if ';' in sample else (',' if ',' in sample else '\t')
                reader = csv.reader(decoded.splitlines(), delimiter=delimiter)
                return [row for row in reader if any(cell.strip() for cell in row)]
            except Exception:
                continue
        return []
    else:
        # Excel (.xlsx)
        try:
            from openpyxl import load_workbook
            wb_temp = load_workbook(file_bytes, data_only=True, keep_links=False)
            try:
                sheet = wb_temp.active
                if sheet is not None:
                    return list(sheet.values)
            finally:
                wb_temp.close()
        except Exception:
            file_bytes.seek(0)
            df_fallback = pd.read_excel(file_bytes, header=None)
            return df_fallback.values.tolist()
    return []

def analyze_sheet_structure(rows, filename):
    """Analyse la structure des lignes d'un classeur ou CSV pour détecter la classe, l'en-tête et les colonnes."""
    # 1. Extraction du nom de la classe dans les métadonnées de l'en-tête
    extracted_class_name = None
    for r_idx, row in enumerate(rows[:25]):
        if not row:
            continue
        for c_idx, cell in enumerate(row):
            if not cell:
                continue
            cell_raw = str(cell).strip()
            clean_k = re.sub(r'[:：\s]', '', cell_raw)
            if clean_k in ['القسم', 'classe']:
                for next_c in range(c_idx + 1, min(c_idx + 5, len(row))):
                    val = row[next_c]
                    if val is not None and str(val).strip():
                        extracted_class_name = str(val).strip()
                        break
            elif 'القسم' in cell_raw or 'classe' in cell_raw.lower():
                parts = re.split(r'[:：]', cell_raw)
                if len(parts) > 1 and parts[1].strip():
                    extracted_class_name = parts[1].strip()
            if extracted_class_name:
                break
        if extracted_class_name:
            break

    cne_keywords = [
        'الرمز', 'رمز', 'رقم التلميذ', 'رقم تلميذ', 'كود مسار', 'رمز مسار', 'مسار',
        'cne', 'massar', 'code eleve', 'code etudiant', 'matricule', 'identifiant'
    ]
    nom_keywords = [
        'النسب', 'اسم العائلي', 'الاسم العائلي', 'nom', 'nom de famille'
    ]
    prenom_keywords = [
        'الاسم الشخصي', 'اسم الشخصي', 'prenom'
    ]
    fullname_keywords = [
        'اسم التلميذ', 'الاسم الكامل', 'اسم الكامل', 'اسم و نسب', 'الاسم و النسب',
        'nom complet', 'nom et prenom', 'nom prenom', 'nom & prenom'
    ]
    dob_keywords = [
        'تاريخ الازدياد', 'تاريخ الميلاد', 'تاريخ ازدياد', 'تاريخ ميلاد', 'ازدياد', 'ميلاد',
        'naissance', 'date de naissance', 'date naissance', 'date naiss', 'ddn'
    ]

    header_row_idx = None
    cne_col = None
    nom_col = None
    prenom_col = None
    fullname_col = None
    dob_col = None

    for r_idx, row in enumerate(rows[:40]):
        if not row:
            continue
        row_norm = [normalize_text(c) for c in row if c is not None]
        for txt in row_norm:
            if any(kw in txt for kw in cne_keywords):
                header_row_idx = r_idx
                break
        if header_row_idx is not None:
            break

    headers_list = []
    if header_row_idx is not None:
        header_row = rows[header_row_idx]
        headers_list = [str(c or '').strip() for c in header_row]
        for c_idx, cell in enumerate(header_row):
            txt = normalize_text(cell)
            if not txt:
                continue
            if any(kw in txt for kw in cne_keywords) and cne_col is None:
                cne_col = c_idx
            elif any(kw in txt for kw in nom_keywords) and nom_col is None:
                nom_col = c_idx
            elif any(kw in txt for kw in fullname_keywords) and fullname_col is None:
                fullname_col = c_idx
            elif any(kw in txt for kw in prenom_keywords) and prenom_col is None:
                prenom_col = c_idx
            elif any(kw in txt for kw in dob_keywords) and dob_col is None:
                dob_col = c_idx

        for c_idx, cell in enumerate(header_row):
            txt = normalize_text(cell)
            if txt in ['الاسم', 'اسم', 'prenom'] and prenom_col is None and fullname_col is None and c_idx != nom_col:
                prenom_col = c_idx

    start_row = header_row_idx + 1 if header_row_idx is not None else 0
    if cne_col is None:
        cne_col = 0
    if fullname_col is None and nom_col is None:
        fullname_col = 1
    if dob_col is None:
        dob_col = 2

    return {
        'extracted_class_name': extracted_class_name or os.path.splitext(filename)[0],
        'header_row_idx': header_row_idx,
        'start_row': start_row,
        'headers': headers_list,
        'cne_col': cne_col,
        'nom_col': nom_col,
        'prenom_col': prenom_col,
        'fullname_col': fullname_col,
        'dob_col': dob_col
    }

@absence_bp.route('/import/template')
def download_import_template():
    """Télécharger le modèle type Excel vierge pour l'import d'élèves."""
    template_path = os.path.join(current_app.root_path, 'static', 'modele_import_etudiants.xlsx')
    if not os.path.exists(template_path):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Modèle Import'
        ws.append(['الرمز / CNE', 'النسب / Nom', 'الإسم / Prénom', 'تاريخ الإزدياد / Date Naissance (JJ/MM/AAAA)'])
        ws.append(['D153007647', 'ايت منصور', 'ابتسام', '29/07/2009'])
        ws.append(['M123456789', 'BENANI', 'Karim', '15/03/2008'])
        wb.save(template_path)
    return send_file(template_path, as_attachment=True, download_name='modele_import_etudiants.xlsx')

@absence_bp.route('/import/preview', methods=['POST'])
def preview_import():
    """Endpoint AJAX pour prévisualiser les données avant confirmation de l'import."""
    files = request.files.getlist('file')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'success': False, 'message': 'Aucun fichier reçu.'}), 400

    results = []
    current_year = get_current_school_year()
    cne_keywords = [
        'الرمز', 'رمز', 'رقم التلميذ', 'رقم تلميذ', 'كود مسار', 'رمز مسار', 'مسار',
        'cne', 'massar', 'code eleve', 'code etudiant', 'matricule', 'identifiant'
    ]

    for file in files:
        if not file or file.filename == '':
            continue
        filename = secure_filename(file.filename or '')
        file_bytes = io.BytesIO(file.read())
        rows = extract_rows_from_file_bytes(file_bytes, filename)
        if not rows:
            continue

        info = analyze_sheet_structure(rows, filename)
        cne_col = info['cne_col']
        nom_col = info['nom_col']
        prenom_col = info['prenom_col']
        fullname_col = info['fullname_col']
        dob_col = info['dob_col']

        sample_students = []
        total_detected = 0

        for r_idx, row in enumerate(rows[info['start_row']:]):
            if not row:
                continue
            raw_cne = row[cne_col] if (cne_col is not None and cne_col < len(row)) else None
            bd_val = row[dob_col] if (dob_col is not None and dob_col < len(row)) else None

            if fullname_col is not None and fullname_col < len(row) and row[fullname_col]:
                full_name_str = str(row[fullname_col]).strip()
                parts = full_name_str.split(' ', 1)
                last_name = parts[0].strip() if len(parts) >= 1 else ''
                first_name = parts[1].strip() if len(parts) == 2 else ''
            elif nom_col is not None and nom_col < len(row) and row[nom_col]:
                last_name = str(row[nom_col]).strip()
                first_name = str(row[prenom_col]).strip() if (prenom_col is not None and prenom_col < len(row) and row[prenom_col]) else ''
            else:
                continue

            if not raw_cne:
                continue

            cne = str(raw_cne).strip()
            if not cne or cne.lower() in ('none', 'nan', '') or normalize_text(cne) in cne_keywords:
                continue

            full_name = f"{last_name} {first_name}".strip()
            if not full_name or full_name.lower() in ('none', 'nan', ''):
                continue

            bdate = parse_birth_date(bd_val)
            total_detected += 1

            if len(sample_students) < 5:
                # Vérifier si l'étudiant existe déjà
                exists = Student.query.filter_by(cne=cne).first() is not None if cne else False
                sample_students.append({
                    'cne': cne,
                    'last_name': last_name,
                    'first_name': first_name,
                    'birth_date': bdate.strftime('%d/%m/%Y') if bdate else '-',
                    'is_existing': exists
                })

        results.append({
            'filename': filename,
            'class_name': info['extracted_class_name'],
            'total_students': total_detected,
            'sample_students': sample_students,
            'columns_detected': {
                'cne': cne_col,
                'nom': nom_col,
                'prenom': prenom_col,
                'fullname': fullname_col,
                'dob': dob_col
            }
        })

    return jsonify({'success': True, 'previews': results})

@absence_bp.route('/import', methods=['GET', 'POST'])
def import_class():
    current_year = get_current_school_year()
    if not current_year:
        flash("Veuillez d'abord configurer une année scolaire.", 'warning')
        return redirect(url_for('configuration.settings') + '#tab-years')

    if request.method == 'POST':
        if current_year.is_read_only():
            flash("Impossible d'importer dans une année scolaire archivée / verrouillée.", 'error')
            return redirect(request.url)

        files = request.files.getlist('file')
        form_class_name = request.form.get('class_name', '').strip()
        
        if not files or all(f.filename == '' for f in files):
            flash('Aucun fichier sélectionné', 'error')
            return redirect(request.url)

        total_added = 0
        total_updated = 0
        classes_processed = 0
        errors = []
        cne_keywords = [
            'الرمز', 'رمز', 'رقم التلميذ', 'رقم تلميذ', 'كود مسار', 'رمز مسار', 'مسار',
            'cne', 'massar', 'code eleve', 'code etudiant', 'matricule', 'identifiant'
        ]

        for file in files:
            if not file or file.filename == '':
                continue
                
            filename = secure_filename(file.filename or '')
            file_bytes = io.BytesIO(file.read())

            try:
                rows = extract_rows_from_file_bytes(file_bytes, filename)
                if not rows:
                    errors.append(f"Le fichier '{filename}' est vide ou illisible.")
                    continue

                info = analyze_sheet_structure(rows, filename)

                # Priorité pour le nom de la classe :
                if form_class_name and len(files) == 1:
                    class_name = form_class_name
                elif info['extracted_class_name']:
                    class_name = info['extracted_class_name']
                else:
                    class_name = os.path.splitext(filename)[0]

                # Récupérer ou créer la classe
                classe = Class.query.filter_by(name=class_name, school_year_id=current_year.id).first()
                if not classe:
                    classe = Class(name=class_name, school_year_id=current_year.id)
                    db.session.add(classe)
                    db.session.commit()

                cne_col = info['cne_col']
                nom_col = info['nom_col']
                prenom_col = info['prenom_col']
                fullname_col = info['fullname_col']
                dob_col = info['dob_col']

                class_students = Student.query.filter_by(class_id=classe.id).all()

                def find_matching_student(cand_cne, cand_full_name, cand_bdate):
                    if cand_cne:
                        by_cne = Student.query.filter_by(cne=cand_cne).first()
                        if by_cne:
                            return by_cne, "cne"

                    best_match = None
                    best_score = 0.0

                    for st in class_students:
                        st_full = f"{st.last_name} {st.first_name}"
                        score = names_similarity(cand_full_name, st_full)

                        bdate_match = (cand_bdate and st.birth_date and cand_bdate == st.birth_date)
                        if bdate_match and score >= 0.70:
                            return st, "date_and_name"

                        if score > best_score:
                            best_score = score
                            best_match = st

                    if best_match and best_score >= 0.85:
                        return best_match, f"fuzzy_{int(best_score*100)}%"

                    return None, None

                added_count = 0
                updated_count = 0
                excel_row_order = 0

                for row in rows[info['start_row']:]:
                    if not row:
                        continue

                    raw_cne = row[cne_col] if (cne_col is not None and cne_col < len(row)) else None
                    bd_val = row[dob_col] if (dob_col is not None and dob_col < len(row)) else None

                    if fullname_col is not None and fullname_col < len(row) and row[fullname_col]:
                        full_name_str = str(row[fullname_col]).strip()
                        parts = full_name_str.split(' ', 1)
                        last_name = parts[0].strip() if len(parts) >= 1 else ''
                        first_name = parts[1].strip() if len(parts) == 2 else ''
                    elif nom_col is not None and nom_col < len(row) and row[nom_col]:
                        last_name = str(row[nom_col]).strip()
                        first_name = str(row[prenom_col]).strip() if (prenom_col is not None and prenom_col < len(row) and row[prenom_col]) else ''
                    else:
                        continue

                    if not raw_cne:
                        continue

                    cne = str(raw_cne).strip()
                    if not cne or cne.lower() in ('none', 'nan', '') or normalize_text(cne) in cne_keywords:
                        continue

                    full_name = f"{last_name} {first_name}".strip()
                    if not full_name or full_name.lower() in ('none', 'nan', ''):
                        continue

                    excel_row_order += 1
                    birth_date = parse_birth_date(bd_val)
                    existing, match_type = find_matching_student(cne, full_name, birth_date)

                    if not existing:
                        # Vérifier si le CNE existe déjà pour un autre étudiant
                        cne_existing = Student.query.filter_by(cne=cne).first()
                        if cne_existing:
                            cne_existing.class_id = classe.id
                            cne_existing.order_num = excel_row_order
                            if first_name and not cne_existing.first_name:
                                cne_existing.first_name = first_name
                            if last_name and not cne_existing.last_name:
                                cne_existing.last_name = last_name
                            if birth_date:
                                cne_existing.birth_date = birth_date
                            updated_count += 1
                        else:
                            student = Student(
                                cne=cne,
                                first_name=first_name,
                                last_name=last_name,
                                birth_date=birth_date,
                                class_id=classe.id,
                                order_num=excel_row_order
                            )
                            db.session.add(student)
                            class_students.append(student)
                            added_count += 1
                    else:
                        existing.class_id = classe.id
                        existing.order_num = excel_row_order
                        if first_name and not existing.first_name:
                            existing.first_name = first_name
                        if last_name and not existing.last_name:
                            existing.last_name = last_name

                        if cne and existing.cne != cne:
                            cne_taken = Student.query.filter(Student.cne == cne, Student.id != existing.id).first()
                            if not cne_taken:
                                existing.cne = cne

                        if birth_date:
                            existing.birth_date = birth_date

                        updated_count += 1

                db.session.commit()
                total_added += added_count
                total_updated += updated_count
                classes_processed += 1
                
            except Exception as e:
                errors.append(f"Erreur sur '{filename}': {str(e)}")

        if errors:
            for err in errors:
                flash(err, 'error')
        
        if classes_processed > 0:
            flash(
                f"Import terminé avec succès : {total_added} nouvel(s) étudiant(s) ajouté(s), "
                f"{total_updated} étudiant(s) existant(s) actualisé(s) dans {classes_processed} classe(s).", 
                'success'
            )
            return redirect(url_for('absence.index'))
            
    return render_template('import.html')

@absence_bp.route('/class/<int:class_id>')
def view_class(class_id):
    classe = Class.query.get_or_404(class_id)
    
    # Pagination parameters
    try:
        page = int(request.args.get('page', 1))
        per_page = request.args.get('per_page', '15')
        if per_page == 'all':
            per_page_int = 1000000 
        else:
            per_page_int = int(per_page)
    except ValueError:
        page = 1
        per_page_int = 15
        per_page = '15'

    pagination = Student.query.filter_by(class_id=class_id).order_by(*Student.default_order()).paginate(page=page, per_page=per_page_int, error_out=False)
    students = pagination.items
    
    # Date parameter (default today)
    date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        selected_date = datetime.now().date()

    all_class_students = Student.query.filter_by(class_id=class_id).order_by(*Student.default_order()).all()
    absences = Absence.query.filter(
        Absence.date == selected_date,
        Absence.student_id.in_([s.id for s in all_class_students])
    ).all()
    
    absent_student_ids = {a.student_id for a in absences}

    # Récupérer la sanction par absence et seuil configurés
    absence_penalty = float(AppSetting.get_value('absence_penalty', '0.5'))
    absence_alert_threshold = int(AppSetting.get_value('absence_alert_threshold', '3'))

    # Année scolaire active pour filtrer les statistiques d'assiduité
    current_year = get_current_school_year()

    # Optimisation Performance : Récupérer en UNE SEULE requête groupée toutes les absences de l'année
    student_ids = [s.id for s in all_class_students]
    abs_year_query = Absence.query.filter(Absence.student_id.in_(student_ids))
    if current_year:
        abs_year_query = abs_year_query.filter(Absence.date >= current_year.start_date, Absence.date <= current_year.end_date)
    all_year_absences = abs_year_query.all()

    # Indexation en mémoire par student_id (évite N requêtes SQL individuelles)
    absences_by_student = {}
    for a in all_year_absences:
        absences_by_student.setdefault(a.student_id, []).append(a)

    # Calculer les statistiques d'assiduité instantanément en mémoire
    student_stats = {}
    for s in all_class_students:
        year_absences = absences_by_student.get(s.id, [])
        total_s_abs = len(year_absences)
        unjustified_s_abs = sum(1 for a in year_absences if not a.justified)
        justified_s_abs = total_s_abs - unjustified_s_abs
        score = max(0.0, 20.0 - (unjustified_s_abs * absence_penalty))
        student_stats[s.id] = {
            'total': total_s_abs,
            'unjustified': unjustified_s_abs,
            'justified': justified_s_abs,
            'score': round(score, 2)
        }

    # Récupérer les autres classes de la même année scolaire pour le transfert groupé
    other_classes = Class.query.filter(Class.school_year_id == classe.school_year_id, Class.id != classe.id).order_by(Class.name).all()

    # Récupérer les groupes actifs pour cette classe si configurés
    active_groups = None
    group_setting = AppSetting.get_value(f'class_groups_{classe.id}', '')
    if group_setting:
        try:
            active_groups = json.loads(group_setting)
        except Exception:
            active_groups = None

    # Récupérer les événements exceptionnels (Grève, Événement) pour cette date et classe
    exceptional_events = ExceptionalEvent.query.filter(
        ExceptionalEvent.date == selected_date,
        (ExceptionalEvent.class_id == classe.id) | (ExceptionalEvent.class_id.is_(None))
    ).all()
    current_exceptional_event = exceptional_events[0] if exceptional_events else None

    # Tous les événements exceptionnels de l'année pour la classe (registre / historique)
    all_class_events_query = ExceptionalEvent.query.filter(
        (ExceptionalEvent.class_id == classe.id) | (ExceptionalEvent.class_id.is_(None))
    )
    if current_year:
        all_class_events_query = all_class_events_query.filter(ExceptionalEvent.school_year_id == current_year.id)
    all_class_events = all_class_events_query.order_by(ExceptionalEvent.date.desc()).all()

    return render_template('view_class.html', 
                           classe=classe, 
                           students=all_class_students, 
                           all_students_count=len(all_class_students),
                           absent_student_ids=absent_student_ids,
                           selected_date=selected_date,
                           per_page=per_page,
                           student_stats=student_stats,
                           absence_penalty=absence_penalty,
                           absence_alert_threshold=absence_alert_threshold,
                           other_classes=other_classes,
                           active_groups=active_groups,
                           exceptional_events=exceptional_events,
                           current_exceptional_event=current_exceptional_event,
                           all_class_events=all_class_events)

@absence_bp.route('/check_cne', methods=['GET'])
def check_cne():
    cne = request.args.get('cne', '').strip()
    if not cne:
        return {'available': True}
    student = Student.query.filter_by(cne=cne).first()
    return {'available': student is None}

@absence_bp.route('/class/<int:class_id>/add_student', methods=['POST'])
def add_student(class_id):
    classe = Class.query.get_or_404(class_id)
    
    cne = request.form.get('cne', '').strip()
    first_name = request.form.get('first_name', '').strip()
    last_name = request.form.get('last_name', '').strip()
    email = request.form.get('email', '').strip() or None
    phone = request.form.get('phone', '').strip() or None
    birth_date_str = request.form.get('birth_date')
    add_another = request.form.get('add_another') == '1'
    
    # Validation des champs requis
    if not cne or not first_name or not last_name:
        flash('Le CNE, le prénom et le nom sont requis.', 'error')
        return redirect(url_for('absence.view_class', class_id=class_id))
    
    # Vérification de l'unicité du CNE
    existing = Student.query.filter_by(cne=cne).first()
    if existing:
        flash(f'Ce CNE ({cne}) est déjà utilisé par un autre étudiant.', 'error')
        return redirect(url_for('absence.view_class', class_id=class_id))
    
    try:
        # Création du nouvel étudiant
        new_student = Student(
            cne=cne,
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            class_id=class_id
        )
        
        # Gestion de la date de naissance (optionnelle)
        if birth_date_str:
            try:
                new_student.birth_date = datetime.strptime(birth_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Format de date invalide.', 'error')
                return redirect(url_for('absence.view_class', class_id=class_id))
        
        # Gestion de l'upload de photo (optionnelle)
        if 'photo' in request.files:
            photo = request.files['photo']
            if photo and photo.filename:
                # Validation de l'extension
                allowed_extensions = {'jpg', 'jpeg', 'png'}
                filename = photo.filename.lower()
                if '.' in filename and filename.rsplit('.', 1)[1] in allowed_extensions:
                    # Génération d'un nom de fichier sécurisé
                    ext = filename.rsplit('.', 1)[1]
                    secure_name = f"{secure_filename(cne)}_{datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
                    
                    # Création du dossier photos si nécessaire
                    photos_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'photos')
                    if not os.path.exists(photos_dir):
                        os.makedirs(photos_dir)
                    
                    # Sauvegarde du fichier
                    photo_path = os.path.join(photos_dir, secure_name)
                    photo.save(photo_path)
                    
                    # Mise à jour du chemin relatif dans la base de données
                    new_student.photo_path = f"photos/{secure_name}"
                else:
                    flash('Format de photo invalide. Utilisez JPG, JPEG ou PNG.', 'warning')
        
        db.session.add(new_student)
        db.session.commit()
        flash(f'Étudiant "{first_name} {last_name}" ajouté avec succès.', 'success')
        
        # Si "Enregistrer et ajouter un autre", on réouvre automatiquement le modal
        if add_another:
            return redirect(url_for('absence.view_class', class_id=class_id, open_add_modal=1))
        return redirect(url_for('absence.view_class', class_id=class_id))
    except Exception as e:
        db.session.rollback()
        flash(f'Erreur lors de l\'ajout de l\'étudiant: {str(e)}', 'error')
        return redirect(url_for('absence.view_class', class_id=class_id))

def send_absence_email(student, force=False, template_id=None, custom_subject=None, custom_body=None):
    if not student.email:
        return False, "Aucune adresse email renseignée pour cet étudiant."

    mail_frequency = AppSetting.get_value('mail_frequency', 'daily')
    if mail_frequency == 'disabled' and not force:
        return False, "L'envoi automatique d'email est désactivé dans la configuration."

    # Contrôle du seuil d'alerte si l'option est activée
    total_abs = len(student.absences)
    mail_only_threshold = AppSetting.get_value('mail_only_threshold', '0') == '1'
    absence_alert_threshold = int(AppSetting.get_value('absence_alert_threshold', '3'))
    if mail_only_threshold and not force and total_abs < absence_alert_threshold:
        return False, f"Seuil minimum d'absences ({absence_alert_threshold}) non atteint ({total_abs})."

    # Contrôle Anti-Spam (délai en jours)
    antispam_days = int(AppSetting.get_value('mail_antispam_days', '0'))
    if antispam_days > 0 and not force and student.last_email_sent_at:
        delta = datetime.now(timezone.utc).replace(tzinfo=None) - student.last_email_sent_at
        if delta.days < antispam_days:
            return False, f"Délai anti-spam actif ({delta.days}/{antispam_days} jours écoulés)."

    # Récupération du template sélectionné
    tid = str(template_id) if template_id else AppSetting.get_value('mail_active_template', '1')
    default_tmpl = DEFAULT_MAIL_TEMPLATES.get(tid, DEFAULT_MAIL_TEMPLATES.get('1', {}))
    subj_tmpl = custom_subject if custom_subject is not None else AppSetting.get_value(f'mail_template_{tid}_subject', default_tmpl.get('subject', ''))
    body_tmpl = custom_body if custom_body is not None else AppSetting.get_value(f'mail_template_{tid}_body', default_tmpl.get('body', ''))
    teacher_name = AppSetting.get_value('teacher_name', 'Prof ZYANI Azzeddine')

    # Calcul de la note d'assiduité
    absence_penalty = float(AppSetting.get_value('absence_penalty', '0.5'))
    unjustified_abs = sum(1 for a in student.absences if not a.justified)
    score = round(max(0.0, 20.0 - (unjustified_abs * absence_penalty)), 2)

    # Fetch and format all absences
    absences = Absence.query.filter_by(student_id=student.id).order_by(Absence.date).all()
    dates_list = ""
    for a in absences:
        try:
            date_fmt = a.date.strftime('%A %d %B %Y')
        except:
            date_fmt = a.date.strftime('%d/%m/%Y')
        status_txt = " (Justifiée)" if a.justified else " (Non justifiée)"
        dates_list += f"- {date_fmt}{status_txt}\n"

    placeholders = {
        '{prenom}': student.first_name,
        '{nom}': student.last_name,
        '{classe}': student.student_class.name if student.student_class else '',
        '{cne}': student.cne,
        '{liste_dates}': dates_list or "- Aucune date enregistrée",
        '{total_absences}': str(len(absences)),
        '{note_assiduite}': str(score),
        '{nom_enseignant}': teacher_name
    }

    subject = subj_tmpl
    body = body_tmpl
    for k, v in placeholders.items():
        subject = subject.replace(k, v)
        body = body.replace(k, v)

    # Récupération du thème graphique sélectionné
    theme_key = AppSetting.get_value('mail_theme', 'campus')
    from blueprints.configuration import render_html_mail
    html_content = render_html_mail(subject, body, theme_key=theme_key, placeholders=placeholders)

    try:
        sender = current_app.config.get('MAIL_USERNAME') or 'noreply@school.com'
        extra_kwargs = {}
        if AppSetting.get_value('mail_teacher_copy', '0') == '1' and sender:
            extra_kwargs['bcc'] = [sender]

        msg = Message(
            subject=subject,
            sender=sender,
            recipients=[student.email],
            **extra_kwargs
        )
        msg.body = body
        msg.html = html_content
        mail.send(msg)

        # Enregistrer la date du dernier envoi
        student.last_email_sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
        return True, f"Email envoyé avec succès à {student.email}."
    except Exception as e:
        db.session.rollback()
        return False, f"Échec de l'envoi : {str(e)}"

@absence_bp.route('/student/<int:student_id>/send_mail', methods=['POST'])
def send_student_mail(student_id):
    student = Student.query.get_or_404(student_id)
    success, message = send_absence_email(student, force=True)
    if success:
        flash(message, 'success')
    else:
        flash(message, 'error')
    return redirect(url_for('absence.student_detail', student_id=student_id))


@absence_bp.route('/api/save_absence', methods=['POST'])
def save_absence():
    data = request.json
    class_id = data.get('class_id')
    date_str = data.get('date')
    student_ids = set(data.get('student_ids', [])) # Liste des IDs marqués absents
    scope_student_ids = data.get('scope_student_ids', None) # IDs de la page courante si pagination

    if not date_str or not class_id:
        return jsonify({'error': 'Données manquantes'}), 400

    # Vérification du verrouillage lecture seule de l'année scolaire
    current_year = get_current_school_year()
    if current_year and current_year.is_read_only():
        return jsonify({'error': f"L'année scolaire {current_year.name} est archivée en lecture seule. Modifications interdites."}), 403

    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        
        # Si un scope_student_ids est fourni (ex: page 1 seulement), on ne met à jour QUE ces étudiants
        if scope_student_ids is not None:
            target_ids = [int(sid) for sid in scope_student_ids]
        else:
            students = Student.query.filter_by(class_id=class_id).all()
            target_ids = [s.id for s in students]

        for sid in target_ids:
            absence = Absence.query.filter_by(student_id=sid, date=selected_date).first()
            if sid in student_ids:
                if not absence:
                    new_absence = Absence(student_id=sid, date=selected_date)
                    db.session.add(new_absence)
            else:
                if absence:
                    db.session.delete(absence)
        
        db.session.commit()

        # Send emails for newly absent students? 
        # User said "après chaque fois ils sont marqués absent".
        # Does this mean ONLY for the new absence? Or whenever the list is saved?
        # "chaque fois ils sont marqués absent" implies when the state is Active.
        # But if I edit the list and they were ALREADY absent, should I send it again?
        # Probably best to send it only if they are in the list of current absents for this date, 
        # OR only if it's a new toggle.
        # Given "Suivi régulier", it sounds like a status update.
        # I will send it for everyone who is absent ON THIS DATE (whether new or existing), 
        # because the user might be confirming the list.
        # Envoi asynchrone des emails pour les élèves absents en arrière-plan
        if student_ids:
            import threading
            app_obj = current_app._get_current_object()  # type: ignore[attr-defined]
            
            def send_async_emails(app_context, ids):
                with app_context.app_context():
                    from database import db as thread_db
                    for sid in ids:
                        try:
                            s = thread_db.session.get(Student, sid)
                            if s:
                                send_absence_email(s)
                        except Exception:
                            pass

            email_thread = threading.Thread(target=send_async_emails, args=(app_obj, list(student_ids)), daemon=True)
            email_thread.start()

        return jsonify({'success': 'Absences enregistrées'})
            
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@absence_bp.route('/update_justification/<int:absence_id>', methods=['POST'])
def update_justification(absence_id):
    absence = Absence.query.get_or_404(absence_id)
    data = request.json
    
    try:
        justified = data.get('justified', False)
        reason = data.get('reason', '')
        
        absence.justified = justified
        # Only save reason if justified is true, or keep it? 
        # User might want to keep reason even if unrelated to justification logic, 
        # but typically reason explains the justification.
        absence.reason = reason if justified else None
        
        db.session.commit()
        return jsonify({
            'success': True, 
            'message': 'Statut mis à jour',
            'justified': absence.justified,
            'reason': absence.reason
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@absence_bp.route('/student/<int:student_id>')
def student_detail(student_id):
    student = Student.query.get_or_404(student_id)
    absences = Absence.query.filter_by(student_id=student_id).order_by(Absence.date.desc()).all()
    # Fetch grades (related to activities)
    grades = Grade.query.filter_by(student_id=student_id).join(Activity).order_by(Activity.date.desc()).all()

    # Calcul de la Note d'Assiduité
    absence_penalty = float(AppSetting.get_value('absence_penalty', '0.5'))
    total_abs = len(absences)
    unjustified_abs = sum(1 for a in absences if not a.justified)
    justified_abs = total_abs - unjustified_abs
    absence_score = round(max(0.0, 20.0 - (unjustified_abs * absence_penalty)), 2)

    return render_template(
        'student_detail.html',
        student=student,
        absences=absences,
        grades=grades,
        total_abs=total_abs,
        unjustified_abs=unjustified_abs,
        justified_abs=justified_abs,
        absence_score=absence_score,
        absence_penalty=absence_penalty,
        badge_definitions=BADGE_DEFINITIONS
    )

@absence_bp.route('/class/<int:class_id>/grades_report')
def class_grades_report(class_id):
    classe = Class.query.get_or_404(class_id)
    absence_penalty = float(AppSetting.get_value('absence_penalty', '0.5'))
    students = Student.query.filter_by(class_id=class_id).order_by(*Student.default_order()).all()

    students_data = []
    total_class_unjustified = 0
    total_class_justified = 0

    for s in students:
        t_abs = len(s.absences)
        unj_abs = sum(1 for a in s.absences if not a.justified)
        j_abs = t_abs - unj_abs
        total_class_unjustified += unj_abs
        total_class_justified += j_abs
        score = round(max(0.0, 20.0 - (unj_abs * absence_penalty)), 2)
        students_data.append({
            'student': s,
            'total_abs': t_abs,
            'unjustified_abs': unj_abs,
            'justified_abs': j_abs,
            'score': score
        })

    class_avg = round(sum(item['score'] for item in students_data) / len(students_data), 2) if students_data else 20.0

    return render_template(
        'class_grades_report.html',
        classe=classe,
        students_data=students_data,
        absence_penalty=absence_penalty,
        class_avg=class_avg,
        total_class_unjustified=total_class_unjustified,
        total_class_justified=total_class_justified
    )

@absence_bp.route('/class/<int:class_id>/export_grades_excel')
def export_class_grades_excel(class_id):
    classe = Class.query.get_or_404(class_id)
    absence_penalty = float(AppSetting.get_value('absence_penalty', '0.5'))
    
    # Même ordre d'affichage que dans l'application (ordre d'importation Excel / default_order)
    students = Student.query.filter_by(class_id=class_id).order_by(*Student.default_order()).all()

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    assert ws is not None  # wb.active is always set on a new Workbook
    ws.title = f"Notes Assiduité {classe.name[:20]}"

    # Styles
    title_font = Font(name='Segoe UI', size=14, bold=True, color='1F4E79')
    sub_font = Font(name='Segoe UI', size=10, italic=True, color='595959')
    header_font = Font(name='Segoe UI', size=11, bold=True, color='FFFFFF')
    header_fill = PatternFill(start_color='2F5597', end_color='2F5597', fill_type='solid')
    zebra_fill = PatternFill(start_color='F2F4F8', end_color='F2F4F8', fill_type='solid')
    data_font = Font(name='Segoe UI', size=10)
    bold_font = Font(name='Segoe UI', size=10, bold=True)
    center_align = Alignment(horizontal='center', vertical='center')
    left_align = Alignment(horizontal='left', vertical='center')
    thin_border = Border(
        left=Side(style='thin', color='D9D9D9'),
        right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'),
        bottom=Side(style='thin', color='D9D9D9')
    )

    # Titre du fichier
    ws.merge_cells('A1:G1')
    ws['A1'] = f"Bilan des Notes d'Assiduité - Classe : {classe.name}"
    ws['A1'].font = title_font
    ws['A1'].alignment = Alignment(horizontal='center', vertical='center')

    ws.merge_cells('A2:G2')
    ws['A2'] = f"Date d'export : {datetime.now().strftime('%d/%m/%Y %H:%M')} | Règle : 20 - (Absences Non Justifiées × {absence_penalty})"
    ws['A2'].font = sub_font
    ws['A2'].alignment = Alignment(horizontal='center', vertical='center')

    # Ligne d'en-tête
    headers = [
        "N°",
        "CNE / Code Massar",
        "Nom Complet",
        "Date de Naissance",
        "Total Absences",
        "Non Justifiées (NJ)",
        "Justifiées (J)",
        "Note Assiduité (/20)"
    ]

    header_row = 4
    ws.row_dimensions[header_row].height = 28
    for col_idx, text in enumerate(headers, 1):
        cell = ws.cell(row=header_row, column=col_idx, value=text)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border

    # Remplissage des données des étudiants
    for idx, s in enumerate(students, 1):
        row_num = header_row + idx
        ws.row_dimensions[row_num].height = 20
        
        t_abs = len(s.absences)
        unj_abs = sum(1 for a in s.absences if not a.justified)
        j_abs = t_abs - unj_abs
        score = round(max(0.0, 20.0 - (unj_abs * absence_penalty)), 2)
        dob_str = s.birth_date.strftime('%d/%m/%Y') if s.birth_date else '-'

        row_values = [
            idx,
            s.cne,
            f"{s.last_name} {s.first_name}",
            dob_str,
            t_abs,
            unj_abs,
            j_abs,
            score
        ]

        is_zebra = (idx % 2 == 0)
        for col_idx, val in enumerate(row_values, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=val)
            cell.font = bold_font if col_idx in (3, 8) else data_font
            cell.alignment = left_align if col_idx in (2, 3) else center_align
            cell.border = thin_border
            if is_zebra:
                cell.fill = zebra_fill
            
            # Mise en valeur de la note
            if col_idx == 8:
                if score >= 16:
                    cell.font = Font(name='Segoe UI', size=10, bold=True, color='276A3C')
                elif score >= 10:
                    cell.font = Font(name='Segoe UI', size=10, bold=True, color='B25E00')
                else:
                    cell.font = Font(name='Segoe UI', size=10, bold=True, color='A61C1C')

    # Ajustement automatique de la largeur des colonnes
    col_widths = {1: 6, 2: 20, 3: 28, 4: 18, 5: 16, 6: 20, 7: 16, 8: 22}
    for col_idx, width in col_widths.items():
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = width

    # Sauvegarde en mémoire
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"Notes_Assiduite_{secure_filename(classe.name)}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )

@absence_bp.route('/class/<int:class_id>/export_attendance_excel')
def export_class_attendance_excel(class_id):
    """Exporte la grille d'assiduité complète de la classe (élèves x dates d'absence) en format Excel."""
    classe = Class.query.get_or_404(class_id)
    students = Student.query.filter_by(class_id=class_id).order_by(*Student.default_order()).all()
    current_year = get_current_school_year()

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = f"Grille {classe.name[:25]}"

    # Récupérer toutes les dates distinctes d'absences pour cette classe
    student_ids = [s.id for s in students]
    query = Absence.query.filter(Absence.student_id.in_(student_ids))
    if current_year:
        query = query.filter(Absence.date >= current_year.start_date, Absence.date <= current_year.end_date)
    all_absences = query.all()

    # Dictionnaire (student_id, date_str) -> absence_obj
    absence_map = {(a.student_id, a.date.strftime('%Y-%m-%d')): a for a in all_absences}
    distinct_dates = sorted(list({a.date.strftime('%Y-%m-%d') for a in all_absences}))

    # Styles
    title_font = Font(name='Segoe UI', size=13, bold=True, color='1E293B')
    sub_font = Font(name='Segoe UI', size=9, italic=True, color='64748B')
    header_font = Font(name='Segoe UI', size=10, bold=True, color='FFFFFF')
    header_fill = PatternFill(start_color='1E293B', end_color='1E293B', fill_type='solid')
    header_date_fill = PatternFill(start_color='2563EB', end_color='2563EB', fill_type='solid')
    zebra_fill = PatternFill(start_color='F8FAFC', end_color='F8FAFC', fill_type='solid')
    data_font = Font(name='Segoe UI', size=9)
    bold_font = Font(name='Segoe UI', size=9, bold=True)
    center_align = Alignment(horizontal='center', vertical='center')
    left_align = Alignment(horizontal='left', vertical='center')
    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    abs_nj_fill = PatternFill(start_color='FEE2E2', end_color='FEE2E2', fill_type='solid')
    abs_nj_font = Font(name='Segoe UI', size=9, bold=True, color='B91C1C')
    abs_j_fill = PatternFill(start_color='FEF3C7', end_color='FEF3C7', fill_type='solid')
    abs_j_font = Font(name='Segoe UI', size=9, bold=True, color='B45309')
    pres_font = Font(name='Segoe UI', size=9, color='94A3B8')

    total_cols = max(5 + len(distinct_dates), 7)

    # Titre
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=total_cols)
    ws.cell(row=1, column=1, value=f"Grille Complète d'Assiduité — Classe : {classe.name}").font = title_font
    ws.cell(row=1, column=1).alignment = center_align

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=total_cols)
    ws.cell(row=2, column=1, value=f"Généré le {datetime.now().strftime('%d/%m/%Y à %H:%M')} | Légende : A (Absent non justifié), J (Justifié), • (Présent)").font = sub_font
    ws.cell(row=2, column=1).alignment = center_align

    # En-têtes fixes
    fixed_headers = ["N°", "CNE / Massar", "Nom & Prénom", "Total Abs", "NJ", "J"]
    header_row = 4
    ws.row_dimensions[header_row].height = 26

    for c_idx, h in enumerate(fixed_headers, 1):
        c = ws.cell(row=header_row, column=c_idx, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center_align
        c.border = thin_border

    # En-têtes des dates
    for d_idx, d_str in enumerate(distinct_dates, len(fixed_headers) + 1):
        try:
            d_formatted = datetime.strptime(d_str, '%Y-%m-%d').strftime('%d/%m')
        except Exception:
            d_formatted = d_str
        c = ws.cell(row=header_row, column=d_idx, value=d_formatted)
        c.font = header_font
        c.fill = header_date_fill
        c.alignment = center_align
        c.border = thin_border

    # Lignes étudiants
    for r_idx, s in enumerate(students, 1):
        curr_row = header_row + r_idx
        ws.row_dimensions[curr_row].height = 19
        is_zebra = (r_idx % 2 == 0)

        # Calcul totaux
        s_abs = [a for a in all_absences if a.student_id == s.id]
        tot = len(s_abs)
        unj = sum(1 for a in s_abs if not a.justified)
        just = tot - unj

        fixed_vals = [r_idx, s.cne, f"{s.last_name} {s.first_name}", tot, unj, just]
        for col_idx, val in enumerate(fixed_vals, 1):
            cell = ws.cell(row=curr_row, column=col_idx, value=val)
            cell.font = bold_font if col_idx in (3, 4) else data_font
            cell.alignment = left_align if col_idx in (2, 3) else center_align
            cell.border = thin_border
            if is_zebra:
                cell.fill = zebra_fill

        # Colonnes par date
        for d_idx, d_str in enumerate(distinct_dates, len(fixed_headers) + 1):
            key = (s.id, d_str)
            if key in absence_map:
                abs_obj = absence_map[key]
                if abs_obj.justified:
                    val, f_font, f_fill = "J", abs_j_font, abs_j_fill
                else:
                    val, f_font, f_fill = "A", abs_nj_font, abs_nj_fill
            else:
                val, f_font, f_fill = "•", pres_font, (zebra_fill if is_zebra else None)

            c = ws.cell(row=curr_row, column=d_idx, value=val)
            c.border = thin_border
            c.font = f_font
            if f_fill:
                c.fill = f_fill
            c.alignment = center_align

    # Largeurs de colonnes
    ws.column_dimensions['A'].width = 5
    ws.column_dimensions['B'].width = 16
    ws.column_dimensions['C'].width = 25
    ws.column_dimensions['D'].width = 11
    ws.column_dimensions['E'].width = 8
    ws.column_dimensions['F'].width = 8
    for d_idx in range(len(fixed_headers) + 1, len(fixed_headers) + len(distinct_dates) + 1):
        col_letter = get_column_letter(d_idx)
        ws.column_dimensions[col_letter].width = 8

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"Grille_Assiduite_{secure_filename(classe.name)}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )

@absence_bp.route('/class/<int:class_id>/print_summary')
def print_class_summary(class_id):
    """Affiche une fiche récapitulative complète optimisée pour impression / enregistrement PDF."""
    classe = Class.query.get_or_404(class_id)
    students = Student.query.filter_by(class_id=class_id).order_by(*Student.default_order()).all()
    current_year = get_current_school_year()
    absence_penalty = float(AppSetting.get_value('absence_penalty', '0.5'))
    absence_alert_threshold = int(AppSetting.get_value('absence_alert_threshold', '3'))
    teacher_name = AppSetting.get_value('teacher_name', 'Prof ZYANI Azzeddine')

    student_stats = []
    total_unjustified_all = 0
    total_justified_all = 0
    total_score_sum = 0.0

    for s in students:
        if current_year:
            year_abs = [a for a in s.absences if current_year.start_date <= a.date <= current_year.end_date]
        else:
            year_abs = s.absences
        tot = len(year_abs)
        unj = sum(1 for a in year_abs if not a.justified)
        just = tot - unj
        score = round(max(0.0, 20.0 - (unj * absence_penalty)), 2)
        total_unjustified_all += unj
        total_justified_all += just
        total_score_sum += score

        student_stats.append({
            'student': s,
            'total': tot,
            'unjustified': unj,
            'justified': just,
            'score': score,
            'is_alert': unj >= absence_alert_threshold
        })

    class_avg = round(total_score_sum / len(students), 2) if students else 20.0

    return render_template(
        'class_summary_print.html',
        classe=classe,
        student_stats=student_stats,
        current_year=current_year,
        teacher_name=teacher_name,
        absence_penalty=absence_penalty,
        class_avg=class_avg,
        total_unjustified_all=total_unjustified_all,
        total_justified_all=total_justified_all,
        generated_at=datetime.now()
    )

@absence_bp.route('/student/<int:student_id>/edit', methods=['GET', 'POST'])
def edit_student(student_id):
    student = Student.query.get_or_404(student_id)
    
    if request.method == 'POST':
        cne = request.form.get('cne')
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        class_id = request.form.get('class_id')
        birth_date_str = request.form.get('birth_date')
        
        if not cne or not full_name or not class_id:
            flash('Tous les champs obligatoires sont requis.', 'error')
        else:
            # Check for unique CNE (if changed)
            existing = Student.query.filter_by(cne=cne).first()
            if existing and existing.id != student.id:
                flash('Ce CNE est déjà utilisé par un autre étudiant.', 'error')
            else:
                try:
                    # Parse full name
                    # Assumption: Last word is First Name, rest is Last Name (e.g. "Ait Maryam Taha")
                    parts = full_name.strip().rsplit(' ', 1)
                    if len(parts) > 1:
                        last_name = parts[0]
                        first_name = parts[1]
                    else:
                        last_name = parts[0] # Fallback if single name
                        first_name = "" 
                    
                    student.cne = cne
                    student.first_name = first_name
                    student.last_name = last_name
                    student.email = email.strip() if email else None
                    student.phone = phone.strip() if phone else None
                    student.class_id = class_id
                    
                    if birth_date_str:
                        student.birth_date = datetime.strptime(birth_date_str, '%Y-%m-%d').date()
                    else:
                        student.birth_date = None
                    
                    # Handle photo upload
                    if 'photo' in request.files:
                        photo = request.files['photo']
                        if photo and photo.filename:
                            # Validate file extension
                            allowed_extensions = {'jpg', 'jpeg', 'png'}
                            filename = photo.filename.lower()
                            if '.' in filename and filename.rsplit('.', 1)[1] in allowed_extensions:
                                # Generate secure filename with student CNE
                                ext = filename.rsplit('.', 1)[1]
                                secure_name = f"{secure_filename(student.cne)}_{datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
                                
                                # Create photos directory if it doesn't exist
                                photos_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'photos')
                                if not os.path.exists(photos_dir):
                                    os.makedirs(photos_dir)
                                
                                # Save the file
                                photo_path = os.path.join(photos_dir, secure_name)
                                photo.save(photo_path)
                                
                                # Delete old photo if exists
                                if student.photo_path:
                                    old_photo = os.path.join(current_app.config['UPLOAD_FOLDER'], student.photo_path)
                                    if os.path.exists(old_photo):
                                        os.remove(old_photo)
                                
                                # Update database with relative path
                                student.photo_path = f"photos/{secure_name}"
                            else:
                                flash('Format de photo invalide. Utilisez JPG, JPEG ou PNG.', 'error')
                        
                    db.session.commit()
                    flash('Informations mises à jour avec succès.', 'success')
                    return redirect(url_for('absence.student_detail', student_id=student.id))
                except Exception as e:
                    flash(f'Erreur lors de la mise à jour: {str(e)}', 'error')
    
    current_year = get_current_school_year()
    if current_year:
        classes = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
    else:
        classes = Class.query.order_by(Class.name).all()
    return render_template('edit_student.html', student=student, classes=classes)

@absence_bp.route('/delete_class/<int:class_id>', methods=['POST'])
def delete_class(class_id):
    try:
        classe = Class.query.get_or_404(class_id)
        db.session.delete(classe)
        db.session.commit()
        flash(f'Classe "{classe.name}" supprimée avec succès.', 'success')
    except Exception as e:
        flash(f'Erreur lors de la suppression de la classe: {str(e)}', 'error')
    return redirect(request.referrer or url_for('absence.index'))

@absence_bp.route('/delete_multiple_classes', methods=['POST'])
def delete_multiple_classes():
    class_ids = request.form.getlist('class_ids[]')
    
    if not class_ids:
        flash('Aucune classe sélectionnée.', 'warning')
        return redirect(url_for('absence.index'))
    
    try:
        deleted_count = 0
        deleted_names = []
        
        for class_id in class_ids:
            classe = db.session.get(Class, int(class_id))
            if classe:
                deleted_names.append(classe.name)
                db.session.delete(classe)
                deleted_count += 1
        
        db.session.commit()
        
        if deleted_count > 0:
            if deleted_count == 1:
                flash(f'Classe "{deleted_names[0]}" supprimée avec succès.', 'success')
            else:
                flash(f'{deleted_count} classes supprimées avec succès: {", ".join(deleted_names)}', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erreur lors de la suppression: {str(e)}', 'error')
    
    return redirect(request.referrer or url_for('absence.index'))

def delete_student_photo_file(photo_path):
    """Supprime proprement le fichier photo d'un étudiant sur le disque s'il existe et n'est pas la photo par défaut."""
    if not photo_path or 'default' in photo_path.lower():
        return
    try:
        upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
        full_path = os.path.join(upload_folder, photo_path)
        if os.path.exists(full_path) and os.path.isfile(full_path):
            os.remove(full_path)
    except Exception:
        pass

@absence_bp.route('/delete_student/<int:student_id>', methods=['POST'])
def delete_student(student_id):
    try:
        student = Student.query.get_or_404(student_id)
        class_id = student.class_id
        photo_path = student.photo_path
        db.session.delete(student)
        db.session.commit()
        delete_student_photo_file(photo_path)
        flash(f'Étudiant "{student.first_name} {student.last_name}" supprimé avec succès.', 'success')
        return redirect(url_for('absence.view_class', class_id=class_id))
    except Exception as e:
        flash(f'Erreur lors de la suppression de l\'étudiant: {str(e)}', 'error')
        return redirect(request.referrer or url_for('absence.index'))

@absence_bp.route('/class/<int:class_id>/bulk_students_action', methods=['POST'])
def bulk_students_action(class_id):
    """Effectue une action groupée sur une sélection d'étudiants."""
    classe = Class.query.get_or_404(class_id)
    action = request.form.get('bulk_action', '').strip()
    student_ids_raw = request.form.getlist('selected_student_ids')

    if not student_ids_raw:
        flash("Aucun étudiant sélectionné.", "warning")
        return redirect(request.referrer or url_for('absence.view_class', class_id=class_id))

    try:
        student_ids = [int(sid) for sid in student_ids_raw if sid.isdigit()]
    except Exception:
        flash("Identifiants d'étudiants invalides.", "error")
        return redirect(request.referrer or url_for('absence.view_class', class_id=class_id))

    students = Student.query.filter(Student.id.in_(student_ids), Student.class_id == class_id).all()
    if not students:
        flash("Aucun étudiant correspondant trouvé dans cette classe.", "warning")
        return redirect(request.referrer or url_for('absence.view_class', class_id=class_id))

    count = len(students)

    try:
        if action == 'delete':
            photos_to_delete = [s.photo_path for s in students if s.photo_path]
            for s in students:
                db.session.delete(s)
            db.session.commit()
            for p in photos_to_delete:
                delete_student_photo_file(p)
            flash(f"{count} étudiant(s) supprimé(s) avec succès.", "success")

        elif action == 'change_class':
            target_class_id = request.form.get('target_class_id')
            if not target_class_id or not target_class_id.isdigit():
                flash("Veuillez choisir une classe de destination valide.", "warning")
                return redirect(request.referrer or url_for('absence.view_class', class_id=class_id))
            
            target_class = Class.query.get_or_404(int(target_class_id))
            for s in students:
                s.class_id = target_class.id
            db.session.commit()
            flash(f"{count} étudiant(s) déplacé(s) vers la classe '{target_class.name}'.", "success")

        elif action == 'mark_absent':
            date_str = request.form.get('action_date')
            try:
                action_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.now().date()
            except ValueError:
                action_date = datetime.now().date()

            added = 0
            for s in students:
                existing = Absence.query.filter_by(student_id=s.id, date=action_date).first()
                if not existing:
                    abs_obj = Absence(student_id=s.id, date=action_date, justified=False)
                    db.session.add(abs_obj)
                    added += 1
            db.session.commit()
            flash(f"{count} étudiant(s) marqué(s) absent(s) pour le {action_date.strftime('%d/%m/%Y')} ({added} nouvelle(s) absence(s)).", "success")

        elif action == 'mark_present':
            date_str = request.form.get('action_date')
            try:
                action_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.now().date()
            except ValueError:
                action_date = datetime.now().date()

            deleted = Absence.query.filter(Absence.student_id.in_([s.id for s in students]), Absence.date == action_date).delete(synchronize_session=False)
            db.session.commit()
            flash(f"{count} étudiant(s) marqué(s) présent(s) pour le {action_date.strftime('%d/%m/%Y')} ({deleted} absence(s) retirée(s)).", "success")

        else:
            flash("Action groupée non reconnue.", "danger")

    except Exception as e:
        db.session.rollback()
        flash(f"Erreur lors de l'opération groupée : {str(e)}", "error")

    return redirect(request.referrer or url_for('absence.view_class', class_id=class_id))


def normalize_search_text(text):
    """Harmonise le texte pour la recherche (arabe avec/sans hamza/harakat, français sans accents, minuscules)."""
    if not text:
        return ''
    t = str(text).strip().lower()
    # Variantes de Alif arabe (أ, إ, آ, ٱ -> ا)
    t = re.sub(r'[أإآٱ]', 'ا', t)
    # Variantes de Taa Marbouta (ة -> ه)
    t = re.sub(r'ة', 'ه', t)
    # Variantes de Alif Maqsoura (ى -> ي)
    t = re.sub(r'ى', 'ي', t)
    # Voyelles et diacritiques arabes (harakat : fatha, damma, kasra, sukun, shadda, tanwin)
    t = re.sub(r'[\u064B-\u065F\u0670]', '', t)
    # Accents latins (é, è, ê, à, ç, ...)
    t = ''.join(c for c in unicodedata.normalize('NFD', t) if unicodedata.category(c) != 'Mn')
    return t

@absence_bp.route('/search')
def search():
    query_class_id = request.args.get('class_id')
    query_cne = request.args.get('cne', '').strip()
    query_full_name = request.args.get('full_name', '').strip()
    query_date_seance = request.args.get('date_seance', '').strip()

    current_year = get_current_school_year()
    absences = []
    students_absences = []

    if current_year:
        classes = Class.query.filter_by(school_year_id=current_year.id).order_by(Class.name).all()
    else:
        classes = Class.query.order_by(Class.name).all()

    # Needs at least one filter to run
    if query_class_id or query_cne or query_full_name or query_date_seance:
        base_query = Student.query.join(Class)
        if current_year:
            base_query = base_query.filter(Class.school_year_id == current_year.id)
        if query_class_id:
            base_query = base_query.filter(Student.class_id == query_class_id)

        all_candidates = base_query.order_by(Student.last_name, Student.first_name).all()

        norm_name = normalize_search_text(query_full_name)
        norm_cne = normalize_search_text(query_cne)

        matched_students = []
        for s in all_candidates:
            # Si un filtre CNE explicite est renseigné
            if norm_cne:
                s_cne_norm = normalize_search_text(s.cne)
                if norm_cne not in s_cne_norm:
                    continue

            # Si le champ universel (full_name) est renseigné
            if norm_name:
                s_cne_norm = normalize_search_text(s.cne)
                s_last_norm = normalize_search_text(s.last_name)
                s_first_norm = normalize_search_text(s.first_name)
                full1 = f"{s_last_norm} {s_first_norm}"
                full2 = f"{s_first_norm} {s_last_norm}"

                # Correspondance tolérante (Nom, Prénom, CNE, Nom+Prénom, Prénom+Nom, mots individuels)
                words = norm_name.split()
                matches_words = all(w in full1 or w in full2 or w in s_cne_norm for w in words)
                matches_substr = (norm_name in s_cne_norm or 
                                  norm_name in s_last_norm or 
                                  norm_name in s_first_norm or 
                                  norm_name in full1 or 
                                  norm_name in full2)

                if not (matches_words or matches_substr):
                    continue

            matched_students.append(s)

        matched_student_ids = [s.id for s in matched_students]

        # Récupérer les absences de ces étudiants (ou selon date_seance)
        if matched_student_ids:
            absences_query = Absence.query.filter(Absence.student_id.in_(matched_student_ids))
            if query_date_seance:
                try:
                    date_obj = datetime.strptime(query_date_seance, '%Y-%m-%d').date()
                    absences_query = absences_query.filter(Absence.date == date_obj)
                except ValueError:
                    pass
            raw_absences = absences_query.order_by(Absence.date.desc()).all()
        elif query_date_seance and not (query_cne or query_full_name or query_class_id):
            absences_query = Absence.query.join(Student).join(Class)
            if current_year:
                absences_query = absences_query.filter(Class.school_year_id == current_year.id)
            try:
                date_obj = datetime.strptime(query_date_seance, '%Y-%m-%d').date()
                absences_query = absences_query.filter(Absence.date == date_obj)
            except ValueError:
                pass
            raw_absences = absences_query.order_by(Absence.date.desc(), Student.last_name).all()
            matched_students = [a.student for a in raw_absences if a.student not in matched_students]
        else:
            raw_absences = []

        absences_by_student = {}
        for a in raw_absences:
            absences_by_student.setdefault(a.student_id, []).append(a)

        grouped_students = {}
        for s in matched_students:
            s_abs = absences_by_student.get(s.id, [])
            # Si un filtre de date est spécifié et que l'élève n'a pas d'absence à cette date, on ne l'affiche pas
            if query_date_seance and not s_abs:
                continue
            justified_c = sum(1 for a in s_abs if a.justified)
            unjustified_c = len(s_abs) - justified_c
            grouped_students[s.id] = {
                'student': s,
                'absences': s_abs,
                'total': len(s_abs),
                'justified_count': justified_c,
                'unjustified_count': unjustified_c
            }

        students_absences = list(grouped_students.values())
        absences = raw_absences

    return render_template(
        'search.html',
        absences=absences,
        students_absences=students_absences,
        classes=classes
    )


@absence_bp.route('/api/get_students')
def api_get_students():
    class_id = request.args.get('class_id')
    if not class_id:
        return jsonify([])
    students = Student.query.filter_by(class_id=class_id).order_by(*Student.default_order()).all()
    return jsonify([{'id': s.id, 'name': f"{s.last_name} {s.first_name}"} for s in students])

@absence_bp.route('/settings/upload_classes', methods=['POST'])
def upload_classes():
    current_year = get_current_school_year()
    if not current_year:
        flash("Veuillez configurer une année scolaire active.", 'warning')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    if current_year.is_read_only():
        flash("Impossible d'ajouter des classes dans une année scolaire archivée.", 'error')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    file = request.files.get('file')
    
    if not file or not file.filename:
        flash('Aucun fichier sélectionné.', 'error')
        return redirect(url_for('configuration.settings') + '#tab-classes')
    
    # Accept both CSV and TXT files
    if not (file.filename.endswith('.csv') or file.filename.endswith('.txt')):
        flash('Fichier invalide. Formats acceptés: .csv ou .txt', 'error')
        return redirect(url_for('configuration.settings') + '#tab-classes')
    
    try:
        content = file.read().decode('utf-8')
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        
        # Skip header if it looks like "Nom de la classe" or similar
        if lines and ('nom' in lines[0].lower() or 'classe' in lines[0].lower()):
            lines = lines[1:]
        
        count = 0
        skipped = 0
        
        for class_name in lines:
            if class_name:
                # Check if class already exists in the current school year
                existing = Class.query.filter_by(name=class_name, school_year_id=current_year.id).first()
                if not existing:
                    new_class = Class(name=class_name, school_year_id=current_year.id)
                    db.session.add(new_class)
                    count += 1
                else:
                    skipped += 1
        
        db.session.commit()
        
        if count > 0:
            flash(f'{count} classe(s) créée(s) avec succès pour {current_year.name}.', 'success')
        if skipped > 0:
            flash(f'{skipped} classe(s) ignorée(s) (déjà existante(s) dans {current_year.name}).', 'info')
        if count == 0 and skipped == 0:
            flash('Aucune classe à importer.', 'warning')
            
    except Exception as e:
        flash(f'Erreur lors de l\'import: {str(e)}', 'error')
    
    return redirect(url_for('configuration.settings') + '#tab-classes')

@absence_bp.route('/settings/add_class', methods=['POST'])
def add_single_class():
    """Ajouter une nouvelle classe vide en saisissant son libellé pour l'année active."""
    current_year = get_current_school_year()
    if not current_year:
        flash("Veuillez sélectionner ou créer une année scolaire.", 'warning')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    if current_year.is_read_only():
        flash("Impossible d'ajouter une classe : cette année scolaire est archivée en lecture seule.", 'error')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    class_name = request.form.get('class_name', '').strip()
    if not class_name:
        flash('Veuillez saisir un nom ou libellé de classe.', 'error')
        return redirect(url_for('configuration.settings') + '#tab-classes')
    
    existing = Class.query.filter_by(name=class_name, school_year_id=current_year.id).first()
    if existing:
        flash(f'La classe "{class_name}" existe déjà pour l\'année {current_year.name}.', 'warning')
    else:
        new_class = Class(name=class_name, school_year_id=current_year.id)
        db.session.add(new_class)
        db.session.commit()
        flash(f'Classe "{class_name}" créée avec succès dans l\'année {current_year.name}.', 'success')
        
    return redirect(url_for('configuration.settings') + '#tab-classes')

@absence_bp.route('/class/<int:class_id>/edit_name', methods=['POST'])
def edit_class_name(class_id):
    """Permet de renommer / modifier le titre d'une classe."""
    classe = Class.query.get_or_404(class_id)
    current_year = get_current_school_year()
    if (current_year and current_year.is_read_only()) or (classe.school_year and classe.school_year.is_read_only()):
        flash("Impossible de modifier une classe appartenant à une année scolaire archivée.", 'error')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    new_name = request.form.get('class_name', '').strip()
    if not new_name:
        flash("Le nom de la classe ne peut pas être vide.", 'error')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    # Vérifier l'unicité du nom dans la même année scolaire
    existing = Class.query.filter(
        Class.name == new_name,
        Class.school_year_id == classe.school_year_id,
        Class.id != classe.id
    ).first()
    if existing:
        flash(f'Une classe nommée "{new_name}" existe déjà pour cette année scolaire.', 'warning')
        return redirect(url_for('configuration.settings') + '#tab-classes')

    old_name = classe.name
    classe.name = new_name
    db.session.commit()
    flash(f'Classe renommée avec succès : "{old_name}" ➜ "{new_name}".', 'success')
    return redirect(url_for('configuration.settings') + '#tab-classes')

@absence_bp.route('/api/class/<int:class_id>/save_groups', methods=['POST'])
def save_class_groups(class_id):
    """Enregistre ou met à jour la répartition des groupes d'une classe."""
    classe = Class.query.get_or_404(class_id)
    data = request.get_json() or {}
    groups = data.get('groups', {}) # { "1": [sid, sid...], "2": [sid, sid...] }
    criterion = data.get('criterion', 'manual')
    created_at = datetime.now().strftime('%d/%m/%Y %H:%M')

    payload = {
        'criterion': criterion,
        'created_at': created_at,
        'groups': groups
    }
    AppSetting.set_value(f'class_groups_{class_id}', json.dumps(payload), f"Groupes pour classe {classe.name}")
    return jsonify({'success': True, 'message': 'Groupes enregistrés avec succès.', 'payload': payload})

@absence_bp.route('/api/class/<int:class_id>/reset_groups', methods=['POST'])
def reset_class_groups(class_id):
    """Supprime la répartition des groupes d'une classe."""
    setting = AppSetting.query.filter_by(key=f'class_groups_{class_id}').first()
    if setting:
        db.session.delete(setting)
        db.session.commit()
    return jsonify({'success': True, 'message': 'Répartition des groupes réinitialisée.'})

@absence_bp.route('/class/<int:class_id>/export_groups_pdf')
def export_groups_pdf(class_id):
    """Génère une fiche PDF claire avec les deux groupes de la classe."""
    classe = Class.query.get_or_404(class_id)
    raw = AppSetting.get_value(f'class_groups_{class_id}', '')
    group_data = json.loads(raw) if raw else {'groups': {'1': [], '2': []}}
    group1_ids = [int(i) for i in group_data.get('groups', {}).get('1', [])]
    group2_ids = [int(i) for i in group_data.get('groups', {}).get('2', [])]

    students_g1 = Student.query.filter(Student.id.in_(group1_ids)).order_by(Student.last_name).all() if group1_ids else []
    students_g2 = Student.query.filter(Student.id.in_(group2_ids)).order_by(Student.last_name).all() if group2_ids else []

    html = render_template('groups_pdf.html',
                           classe=classe,
                           students_g1=students_g1,
                           students_g2=students_g2,
                           criterion=group_data.get('criterion', 'Manuel'),
                           created_at=group_data.get('created_at', ''))
    
    # Tentative d'export via Weasyprint ou rendu HTML imprimable
    try:
        from weasyprint import HTML
        pdf = HTML(string=html).write_pdf()
        response = make_response(pdf)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'inline; filename=Groupes_{classe.name}.pdf'
        return response
    except Exception:
        return html


@absence_bp.route('/student/<int:student_id>/export_bilan_pdf')
def export_student_bilan_pdf(student_id):
    """Génère la fiche bilan individuelle d'assiduité en PDF / format officiel imprimable."""
    student = Student.query.get_or_404(student_id)
    current_year = get_current_school_year()

    # Absences ordonnées par date
    absences = Absence.query.filter_by(student_id=student.id).order_by(Absence.date.desc()).all()
    total_abs = len(absences)
    justified_abs = sum(1 for a in absences if a.justified)
    unjustified_abs = total_abs - justified_abs

    # Calcul note d'assiduité
    absence_penalty = float(AppSetting.get_value('absence_penalty', '0.5'))
    score = round(max(0.0, 20.0 - (unjustified_abs * absence_penalty)), 2)

    # Groupe de l'étudiant si configuré
    group_name = "Classe entière"
    if student.class_id:
        raw_grp = AppSetting.get_value(f'class_groups_{student.class_id}', '')
        if raw_grp:
            try:
                gdata = json.loads(raw_grp)
                if str(student.id) in [str(x) for x in gdata.get('groups', {}).get('1', [])]:
                    group_name = "Groupe 1"
                elif str(student.id) in [str(x) for x in gdata.get('groups', {}).get('2', [])]:
                    group_name = "Groupe 2"
            except Exception:
                pass

    photo_url = url_for('main.uploaded_file', filename=student.photo_path if student.photo_path else 'photos/Default.jpg')
    school_name = AppSetting.get_value('school_name', 'Établissement Scolaire & Supérieur')

    html = render_template(
        'student_bilan_pdf.html',
        student=student,
        absences=absences,
        total_abs=total_abs,
        justified_abs=justified_abs,
        unjustified_abs=unjustified_abs,
        score=score,
        group_name=group_name,
        photo_url=photo_url,
        school_name=school_name,
        current_school_year=current_year,
        now=datetime.now()
    )

    try:
        from weasyprint import HTML
        pdf = HTML(string=html).write_pdf()
        response = make_response(pdf)
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'inline; filename=Bilan_Assiduite_{student.cne}.pdf'
        return response
    except Exception:
        return html


# ─── API BADGES D'ATTITUDE & COMPÉTENCES (OPTION C) ──────────────────────────

@absence_bp.route('/api/student/<int:student_id>/award_badge', methods=['POST'])
def api_award_badge(student_id):
    """Attribue un badge d'attitude ou compétence à un élève."""
    student = Student.query.get_or_404(student_id)
    data = request.get_json() or {}
    badge_key = data.get('badge_key')
    custom_desc = data.get('description', '')

    if badge_key not in BADGE_DEFINITIONS:
        return jsonify({'success': False, 'error': 'Badge non reconnu'}), 400

    b_def = BADGE_DEFINITIONS[badge_key]
    current_year = get_current_school_year()

    badge = StudentBadge(
        student_id=student.id,
        class_id=student.class_id,
        school_year_id=current_year.id if current_year else None,
        badge_key=badge_key,
        title=b_def['title'],
        description=custom_desc or b_def['desc'],
        icon=b_def['icon'],
        color=b_def['color']
    )
    db.session.add(badge)
    db.session.commit()

    return jsonify({
        'success': True,
        'message': f"Badge « {badge.title} » décerné à {student.first_name} {student.last_name} !",
        'badge': badge.to_dict()
    })


@absence_bp.route('/api/student/badge/<int:badge_id>/delete', methods=['POST'])
def api_delete_badge(badge_id):
    """Supprime un badge décerné."""
    badge = StudentBadge.query.get_or_404(badge_id)
    db.session.delete(badge)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Badge supprimé avec succès.'})


@absence_bp.route('/api/student/<int:student_id>/badges', methods=['GET'])
def api_get_student_badges(student_id):
    """Retourne la liste des badges d'un élève."""
    badges = StudentBadge.query.filter_by(student_id=student_id).order_by(StudentBadge.awarded_at.desc()).all()
    return jsonify({
        'success': True,
        'badges': [b.to_dict() for b in badges]
    })


# ─── PLAN DE SALLE & GESTION DES POSTES DU LABO (OPTION A) ───────────────────

@absence_bp.route('/class/<int:class_id>/seating-plan')
def seating_plan(class_id):
    """Affiche le plan interactif du laboratoire d'informatique pour la classe."""
    classe = Class.query.get_or_404(class_id)
    current_year = get_current_school_year()
    students = Student.query.filter_by(class_id=classe.id).order_by(*Student.default_order()).all()

    # Absences du jour
    today = datetime.now().date()
    today_absences = {a.student_id for a in Absence.query.filter_by(date=today).all()}

    # Groupes TP si configurés
    raw_grp = AppSetting.get_value(f'class_groups_{class_id}', '')
    groups_data = json.loads(raw_grp) if raw_grp else {'groups': {'1': [], '2': []}}
    g1_ids = {int(x) for x in groups_data.get('groups', {}).get('1', [])}
    g2_ids = {int(x) for x in groups_data.get('groups', {}).get('2', [])}

    # Configuration du plan de salle enregistrée
    raw_plan = AppSetting.get_value(f'class_lab_plan_{class_id}', '')
    plan_config = json.loads(raw_plan) if raw_plan else None

    # Initialisation ou migration vers la disposition en 'U' avec Groupes de Travail
    if not plan_config or plan_config.get('layout_type') != 'u_shape' or not plan_config.get('u_config'):
        # Conserver les assignations déjà existantes si une ancienne grille était présente
        existing_assignments = {}
        if plan_config and plan_config.get('stations'):
            for s in plan_config['stations']:
                if s.get('assigned_student_ids'):
                    existing_assignments[s.get('station_number', 0)] = s['assigned_student_ids']

        num_students = len(students)
        total_stations = max(20, (num_students + 1) // 2) if num_students > 0 else 20
        bottom_count = max(4, round(total_stations / 3.5))
        rem = total_stations - bottom_count
        left_count = rem // 2
        right_count = rem - left_count

        stations = []
        num = 1
        # Aile Gauche
        for i in range(left_count):
            stations.append({
                'id': f'pc_{num}',
                'station_number': num,
                'name': f'Poste {num:02d}',
                'group_name': f'Groupe {num:02d}',
                'zone': 'left',
                'status': 'ok',
                'note': '',
                'assigned_student_ids': existing_assignments.get(num, [])
            })
            num += 1

        # Fond de Salle
        for i in range(bottom_count):
            stations.append({
                'id': f'pc_{num}',
                'station_number': num,
                'name': f'Poste {num:02d}',
                'group_name': f'Groupe {num:02d}',
                'zone': 'bottom',
                'status': 'ok',
                'note': '',
                'assigned_student_ids': existing_assignments.get(num, [])
            })
            num += 1

        # Aile Droite
        for i in range(right_count):
            stations.append({
                'id': f'pc_{num}',
                'station_number': num,
                'name': f'Poste {num:02d}',
                'group_name': f'Groupe {num:02d}',
                'zone': 'right',
                'status': 'ok',
                'note': '',
                'assigned_student_ids': existing_assignments.get(num, [])
            })
            num += 1

        plan_config = {
            'layout_type': 'u_shape',
            'u_config': {
                'total_stations': total_stations,
                'wing_left': left_count,
                'bottom': bottom_count,
                'wing_right': right_count,
                'max_per_station': 2
            },
            'stations': stations,
            'updated_at': ''
        }

    return render_template(
        'seating_plan.html',
        classe=classe,
        students=[s.to_dict() for s in students],
        plan_config=plan_config,
        today_absences=list(today_absences),
        g1_ids=list(g1_ids),
        g2_ids=list(g2_ids),
        today_date=today.strftime('%Y-%m-%d'),
        today_date_fr=today.strftime('%d/%m/%Y'),
        current_year=current_year,
        badge_definitions=BADGE_DEFINITIONS
    )


@absence_bp.route('/api/class/<int:class_id>/save_seating_plan', methods=['POST'])
def save_seating_plan(class_id):
    """Enregistre la disposition des postes en U, l'état du matériel et les élèves assignés."""
    classe = Class.query.get_or_404(class_id)
    data = request.get_json() or {}
    data['updated_at'] = datetime.now().strftime('%d/%m/%Y %H:%M')

    AppSetting.set_value(f'class_lab_plan_{class_id}', json.dumps(data), f"Plan de salle labo pour classe {classe.name}")
    return jsonify({'success': True, 'message': 'Plan de salle du laboratoire enregistré avec succès.'})


@absence_bp.route('/api/class/<int:class_id>/quick_seating_assign', methods=['POST'])
def quick_seating_assign(class_id):
    """Répartition automatique rapide des élèves sur les postes du laboratoire en binômes/groupes."""
    classe = Class.query.get_or_404(class_id)
    data = request.get_json() or {}
    mode = data.get('mode', 'alpha') # 'alpha', 'groups', 'random', 'clear'

    students = Student.query.filter_by(class_id=classe.id).order_by(*Student.default_order()).all()
    raw_plan = AppSetting.get_value(f'class_lab_plan_{class_id}', '')
    plan = json.loads(raw_plan) if raw_plan else None

    if not plan or not plan.get('stations'):
        num_students = len(students)
        total_stations = max(20, (num_students + 1) // 2) if num_students > 0 else 20
        bottom_count = max(4, round(total_stations / 3.5))
        rem = total_stations - bottom_count
        left_count = rem // 2
        right_count = rem - left_count
        stations = []
        num = 1
        for z, count in [('left', left_count), ('bottom', bottom_count), ('right', right_count)]:
            for _ in range(count):
                stations.append({
                    'id': f'pc_{num}',
                    'station_number': num,
                    'name': f'Poste {num:02d}',
                    'group_name': f'Groupe {num:02d}',
                    'zone': z,
                    'status': 'ok',
                    'note': '',
                    'assigned_student_ids': []
                })
                num += 1
        plan = {
            'layout_type': 'u_shape',
            'u_config': {
                'total_stations': total_stations,
                'wing_left': left_count,
                'bottom': bottom_count,
                'wing_right': right_count,
                'max_per_station': 2
            },
            'stations': stations
        }

    stations = plan['stations']
    max_per = plan.get('u_config', {}).get('max_per_station', 2)

    if mode == 'clear':
        for s in stations:
            s['assigned_student_ids'] = []
    elif mode == 'alpha':
        for i, s in enumerate(stations):
            start = i * max_per
            chunk = students[start:start + max_per]
            s['assigned_student_ids'] = [st.id for st in chunk]
    elif mode == 'random':
        import random
        shuffled = list(students)
        random.shuffle(shuffled)
        for i, s in enumerate(stations):
            start = i * max_per
            chunk = shuffled[start:start + max_per]
            s['assigned_student_ids'] = [st.id for st in chunk]
    elif mode == 'groups':
        raw_grp = AppSetting.get_value(f'class_groups_{class_id}', '')
        grp_data = json.loads(raw_grp) if raw_grp else {'groups': {'1': [], '2': []}}
        g1 = [s for s in students if str(s.id) in [str(x) for x in grp_data.get('groups', {}).get('1', [])]]
        g2 = [s for s in students if str(s.id) in [str(x) for x in grp_data.get('groups', {}).get('2', [])]]
        ordered = g1 + g2
        for i, s in enumerate(stations):
            start = i * max_per
            chunk = ordered[start:start + max_per]
            s['assigned_student_ids'] = [st.id for st in chunk]

    plan['updated_at'] = datetime.now().strftime('%d/%m/%Y %H:%M')
    AppSetting.set_value(f'class_lab_plan_{class_id}', json.dumps(plan), f"Plan de salle labo pour classe {classe.name}")

    return jsonify({'success': True, 'plan': plan, 'message': 'Assignation automatique en groupes effectuée avec succès !'})


@absence_bp.route('/api/toggle_student_absence', methods=['POST'])
def toggle_student_absence():
    """Bascule le statut présent/absent d'un étudiant pour une date donnée en temps réel."""
    data = request.get_json() or {}
    student_id = data.get('student_id')
    date_str = data.get('date') or datetime.now().strftime('%Y-%m-%d')

    if not student_id:
        return jsonify({'success': False, 'error': 'ID étudiant manquant'}), 400

    target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    existing = Absence.query.filter_by(student_id=student_id, date=target_date).first()

    if existing:
        db.session.delete(existing)
        db.session.commit()
        is_absent = False
    else:
        new_abs = Absence(student_id=student_id, date=target_date, justified=False)
        db.session.add(new_abs)
        db.session.commit()
        is_absent = True

    return jsonify({
        'success': True,
        'student_id': student_id,
        'is_absent': is_absent,
        'date': date_str
    })


# =========================================================
# API ÉVÉNEMENTS EXCEPTIONNELS (GRÈVES, ÉVÉNEMENTS, SESSIONS SUSPENDUES)
# =========================================================

@absence_bp.route('/api/exceptional_event/create', methods=['POST'])
def create_exceptional_event():
    """Déclare une journée ou demi-journée exceptionnelle (Grève, Événement établissement) et synchronise avec le cahier de texte."""
    data = request.get_json() or {}
    title = (data.get('title') or '').strip()
    event_type = data.get('event_type', 'greve')
    date_str = data.get('date')
    period = data.get('period', 'all_day')
    start_time = data.get('start_time', '08:00')
    end_time = data.get('end_time', '18:00')
    description = (data.get('description') or '').strip()
    class_id = data.get('class_id') # None ou int
    scope_all_classes = data.get('scope_all_classes', False)
    sync_textbook = data.get('sync_textbook', True)

    if not title:
        title = "Mouvement de Grève" if event_type == 'greve' else "Événement de l'Établissement"

    if not date_str:
        return jsonify({'success': False, 'error': 'Date manquante'}), 400

    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'success': False, 'error': 'Format de date invalide (YYYY-MM-DD)'}), 400

    current_year = get_current_school_year()
    if not current_year:
        return jsonify({'success': False, 'error': 'Aucune année scolaire active trouvée'}), 400

    target_class_id = None if scope_all_classes else (int(class_id) if class_id else None)

    # Création de l'événement
    event = ExceptionalEvent(
        title=title,
        event_type=event_type,
        date=target_date,
        period=period,
        start_time=start_time if period in ['morning', 'afternoon', 'custom'] else '08:00',
        end_time=end_time if period in ['morning', 'afternoon', 'custom'] else '18:00',
        description=description,
        class_id=target_class_id,
        school_year_id=current_year.id,
        sync_textbook=bool(sync_textbook)
    )
    db.session.add(event)
    db.session.commit()

    # Synchronisation optionnelle avec le Cahier de Texte
    created_sessions_count = 0
    if sync_textbook:
        # Déterminer les classes concernées
        if target_class_id:
            classes_to_sync = [Class.query.get(target_class_id)]
        else:
            classes_to_sync = Class.query.filter_by(school_year_id=current_year.id).all()

        default_mod = CourseModule.query.first()
        default_sec = CourseSection.query.first() if default_mod else None

        for cls in classes_to_sync:
            if not cls or not default_mod or not default_sec:
                continue

            # Vérifier si une session n'existe pas déjà pour ce motif à cette date
            existing_sess = TextbookSession.query.filter_by(
                class_id=cls.id,
                date=target_date,
                session_type="Séance Neutralisée"
            ).first()

            if not existing_sess:
                period_label = event.period_display
                sess_title = f"[SUSPENSION COURS] {title} — {period_label}"
                sess_remark = f"Séance neutralisée ({event.type_display}). {description}".strip()

                new_sess = TextbookSession(
                    module_id=default_mod.id,
                    section_id=default_sec.id,
                    class_id=cls.id,
                    school_year_id=current_year.id,
                    date=target_date,
                    course_titles=sess_title,
                    remark=sess_remark,
                    session_type="Séance Neutralisée",
                    duration_hours=2.0 if period in ['morning', 'afternoon'] else 4.0,
                    start_time=start_time if period in ['morning', 'afternoon', 'custom'] else '08:30',
                    end_time=end_time if period in ['morning', 'afternoon', 'custom'] else '12:30',
                    teacher_name=AppSetting.get_value('teacher_name', 'Enseignant')
                )
                db.session.add(new_sess)
                created_sessions_count += 1

        if created_sessions_count > 0:
            db.session.commit()

    return jsonify({
        'success': True,
        'message': f"Événement exceptionnel enregistré avec succès ({event.period_display}).",
        'event': event.to_dict(),
        'textbook_sessions_created': created_sessions_count
    })


@absence_bp.route('/api/exceptional_event/<int:event_id>/delete', methods=['POST'])
def delete_exceptional_event(event_id):
    """Supprime un événement exceptionnel."""
    event = ExceptionalEvent.query.get_or_404(event_id)
    db.session.delete(event)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Événement exceptionnel supprimé avec succès.'})


@absence_bp.route('/api/class/<int:class_id>/exceptional_events', methods=['GET'])
def get_class_exceptional_events(class_id):
    """Retourne la liste des événements exceptionnels enregistrés pour cette classe ou globaux."""
    classe = Class.query.get_or_404(class_id)
    current_year = get_current_school_year()
    query = ExceptionalEvent.query.filter(
        (ExceptionalEvent.class_id == classe.id) | (ExceptionalEvent.class_id.is_(None))
    )
    if current_year:
        query = query.filter(ExceptionalEvent.school_year_id == current_year.id)
    events = query.order_by(ExceptionalEvent.date.desc()).all()
    return jsonify({
        'success': True,
        'events': [e.to_dict() for e in events]
    })




