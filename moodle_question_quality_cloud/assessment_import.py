"""Plan an approved Moodle assessment import without mutating Notion."""

import csv
import hashlib
import io
import math
import re
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote

from cloud_backend import normalize, quality
from notion_bridge import DS_ID, field_equal, normalized_type, property_values, title, type_name, TYPE_MAP
from workflow import plain


ASSIGNMENTS = '1fa9e758-9a23-8069-a7b7-000bf1e13b81'
STUDENTS = '2179e758-9a23-804f-9a97-000bf189d159'
RESULTS = 'f80af089-ff2a-4c82-81cd-17ac65ec6228'


def text_key(value):
    return normalize(unicodedata.normalize('NFC', value or ''))


def id_key(value):
    return (value or '').replace('-', '').lower()


def relation_ids(page, name):
    prop = page.get('properties', {}).get(name, {})
    if 'relation' not in prop:
        raise ValueError(f'Notion relation {name} is unavailable. Check the integration has access to the linked databases.')
    if prop.get('has_more'):
        raise ValueError(f'Notion relation {name} has more entries than can be safely previewed')
    return [item['id'] for item in prop.get('relation', [])]


def relation(values):
    return {'relation': [{'id': value} for value in values]}


def text_property(value, kind='rich_text'):
    return {kind: [{'type': 'text', 'text': {'content': value}}]}


def page_name(page, field='Name'):
    return ''.join(
        part.get('plain_text', part.get('text', {}).get('content', ''))
        for part in page['properties'].get(field, {}).get('title', [])
    )


def csv_records(raw):
    if len(raw) > 8_000_000:
        raise ValueError('CSV exceeds the 8 MB limit')
    return csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline=''))


def quiz_title(raw):
    records = csv_records(raw)
    header = next(records, [])
    row = next(records, [])
    if header and text_key(header[0]) == 'quiz name' and row:
        return row[0].strip()
    return ''


def quiz_code(name):
    match = re.match(r'^([a-z]+\d+-\d+)(?![\w-])', text_key(name))
    return match[1] if match else ''


def assignment_matches(name, pages):
    exact = [p for p in pages if text_key(page_name(p, 'Title')) == text_key(name)]
    if exact or not quiz_code(name):
        return exact
    return [p for p in pages if quiz_code(page_name(p, 'Title')) == quiz_code(name)]


def parse_grades(raw):
    records = csv_records(raw)
    headers = [text_key(h) for h in next(records, [])]
    names = (
        ('first name', 'firstname', 'eesnimi'),
        ('surname', 'last name', 'lastname', 'perekonnanimi'),
        ('full name', 'fullname', 'täisnimi', 'nimi'),
        ('email address', 'email', 'e-post', 'e-posti aadress', 'e-posti aadressi'),
        ('state', 'status', 'olek', 'staatus'),
    )
    first, last, full, email, state = [
        next((i for i, header in enumerate(headers) if header in aliases), None)
        for aliases in names
    ]
    grades = [(i, re.fullmatch(r'(?:grade|hinne|punktid)\s*/\s*(\d+(?:[.,]\d+)?)', header))
              for i, header in enumerate(headers)]
    grade_columns = [(i, float(match[1].replace(',', '.'))) for i, match in grades if match]
    if (full is None and (first is None or last is None)) or len(grade_columns) != 1:
        raise ValueError('Upload the Moodle Results → Grades CSV with student names and a Grade/<maximum> column')
    grade_index, maximum = grade_columns[0]
    if not math.isfinite(maximum) or maximum <= 0:
        raise ValueError('The grade maximum must be a positive number')

    rows = []
    for line, cells in enumerate(records, 2):
        if not any(cells):
            continue

        def cell(index):
            return cells[index].strip() if index is not None and index < len(cells) else ''

        name = cell(full) if full is not None else f'{cell(first)} {cell(last)}'.strip()
        if text_key(name) in {'overall average', 'group average', 'üldine keskmine', 'rühma keskmine'}:
            continue
        raw_score = cell(grade_index).replace(',', '.')
        points = float(raw_score) if re.fullmatch(r'[+-]?\d+(?:\.\d+)?', raw_score) else None
        reason = ''
        if not name or (full is None and not (cell(first) and cell(last))):
            reason = 'Missing student name'
        elif cell(state) and text_key(cell(state)) not in {'finished', 'lõpetatud', 'lõpetatud katse', 'lõpetanud'}:
            reason = 'Attempt is not finished'
        elif points is None or not 0 <= points <= maximum:
            reason = 'Missing or invalid grade'
        rows.append({'name': name or f'CSV row {line}', 'email': cell(email), 'points': points,
                     'maximum': maximum, 'reason': reason})
    return rows


def entry(kind, name, status, reason='', page_id=None, properties=None):
    return {'kind': kind, 'name': name, 'status': status, 'reason': reason,
            'page_id': page_id, 'properties': properties or {}}


def plan_grades(rows, students, results, assignment):
    assignment_id = assignment['id']
    by_name = defaultdict(list)
    student_pages = {id_key(page['id']): page for page in students}
    for page in students:
        by_name[text_key(page_name(page))].append(id_key(page['id']))
    by_email = defaultdict(set)
    existing = defaultdict(list)
    import_keys = Counter()
    for page in results:
        people = relation_ids(page, 'Õpilane')
        for person in people:
            email = text_key(page['properties'].get('Moodle e-post', {}).get('email'))
            if email and len(people) == 1:
                by_email[email].add(id_key(person))
            if id_key(assignment_id) in map(id_key, relation_ids(page, 'Ülesanne')):
                existing[id_key(person)].append(page)
        import_keys[text_key(plain(page, 'Impordi võti'))] += 1

    plans = []
    matched = defaultdict(list)
    for row in rows:
        name = row['name']
        if row['reason']:
            plans.append(entry('Grade', name, 'refused', row['reason']))
            continue
        names = set(by_name[text_key(name)])
        emails = by_email[text_key(row['email'])]
        candidates = names & emails if emails else names
        if len(candidates) != 1:
            reason = 'Ambiguous student match' if len(candidates) > 1 else 'No matching student name or email/name conflict'
            plans.append(entry('Grade', name, 'refused', reason))
            continue
        matched[next(iter(candidates))].append(row)

    topics = relation_ids(assignment, 'Teema')
    for student_id, attempts in matched.items():
        row = attempts[0]
        name, score, maximum = row['name'], row['points'], row['maximum']
        if len({(a['points'], text_key(a['email'])) for a in attempts}) > 1:
            plans.append(entry('Grade', name, 'refused', 'Multiple attempts with different grades or identities; export one grading attempt per student'))
            continue
        prior = existing[student_id]
        import_key = f'moodle:{id_key(assignment_id)}:{student_id}'
        if len(prior) > 1 or (not prior and import_keys[import_key]):
            plans.append(entry('Grade', name, 'refused', 'Multiple existing results or an import-key conflict'))
            continue
        values = {'Punktid': score, 'Maksimum': maximum, 'Protsent': round(100 * score / maximum, 2)}
        props = {key: {'number': value} for key, value in values.items()}
        if prior:
            page = prior[0]
            if len(relation_ids(page, 'Õpilane')) != 1 or len(relation_ids(page, 'Ülesanne')) != 1:
                plans.append(entry('Grade', name, 'refused', 'Existing result links multiple students or assignments'))
                continue
            prior_props = page['properties']
            recorded = {k: prior_props.get(k, {}).get('number') for k in values}
            if recorded['Punktid'] is not None:
                same = recorded['Punktid'] == score and recorded['Maksimum'] in (None, maximum)
                plans.append(entry('Grade', name, 'skipped' if same else 'refused',
                                   'Grade already recorded' if same else 'Recorded grade differs'))
                continue
            if any(value is not None and value != values[k] for k, value in recorded.items()):
                plans.append(entry('Grade', name, 'refused', 'Existing result fields conflict'))
                continue
            props = {k: v for k, v in props.items() if recorded[k] is None}
            state = prior_props.get('Olek', {}).get('select') or {}
            if state.get('name') in (None, 'In progress'):
                props['Olek'] = {'select': {'name': 'Finished'}}
            plans.append(entry('Grade', name, 'fill', page_id=page['id'], properties=props))
        else:
            student = student_pages[student_id]
            assignment_name = page_name(assignment, 'Title')
            props.update({
                'Tulemus': text_property(f'{page_name(student)} — {assignment_name}', 'title'),
                'Õpilane': relation([student['id']]),
                'Ülesanne': relation([assignment_id]),
                'Impordi võti': text_property(import_key),
                'Olek': {'select': {'name': 'Finished'}},
            })
            if topics:
                props['Tunniteema'] = relation(topics)
            if row['email']:
                props['Moodle e-post'] = {'email': row['email']}
            plans.append(entry('Grade', name, 'create', properties=props))
    return plans


XML_TYPES = {'multichoice': 'multiple choice', 'truefalse': 'true/false',
             'shortanswer': 'short answer', 'ddwtos': 'drag and drop into text',
             'ddimageortext': 'drag and drop onto image', 'ddmarker': 'drag markers',
             'multianswer': 'cloze', 'calculatedmulti': 'calculated multichoice'}


def question_key(name, kind):
    return text_key(name), normalized_type(XML_TYPES.get(text_key(kind), kind))


def projected_metrics(rows):
    total = sum(r['attempts'] for r in rows)
    facilities = [r for r in rows if r['facility'] is not None]
    weight = sum(r['attempts'] for r in facilities)
    facility = None
    if weight:
        mean = sum(Decimal(str(r['facility'])) * r['attempts'] for r in facilities) / weight
        facility = float(mean.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    discriminations = [r['discrimination'] for r in rows
                       if r['attempts'] >= 10 and r['discrimination'] is not None]
    negative = sum(d < 0 for d in discriminations)
    low = sum(0 <= d < 20 for d in discriminations)
    return {'attempts': total, 'facility_percent': facility, 'negative_assessments': negative,
            'assessment_count': len(rows), 'quality': quality(total, facility, negative, low)}


def plan_questions(rows, pages, bank, assignment, history, local_questions):
    by_id, by_item, by_name, bank_by_key = (defaultdict(list) for _ in range(4))
    local = {q['signature']: q for q in local_questions}
    linked_locally = {q['moodle_bank_id']: q['signature'] for q in local_questions if q.get('moodle_bank_id')}
    for page in pages:
        by_id[plain(page, 'Moodle ID').strip()].append(page)
        by_item[plain(page, 'Moodle item key').strip()].append(page)
        by_name[text_key(title(page))].append(page)
    for item in bank:
        bank_by_key[question_key(item['name'], item['question_type'])].append(item)
    counts = Counter(question_key(row['name'], row['question_type']) for row in rows)
    titles = Counter(text_key(row['name']) for row in rows)
    plans = []
    for row in rows:
        name = row['name']
        identity = question_key(name, row['question_type'])
        item_hash = hashlib.sha256('|'.join(identity).encode()).hexdigest()
        item_key = f"assessment:{id_key(assignment['id'])}:{item_hash}"
        candidates = by_name[identity[0]]
        bank_matches = bank_by_key[identity]
        known = local.get(row['signature'], {})
        exact_ids = {plain(p, 'Moodle ID').strip() for p in candidates
                     if normalized_type(type_name(p)) == identity[1]} - {''}
        ids = exact_ids | {str(item['moodle_id']) for item in bank_matches}
        if known.get('moodle_bank_id'):
            ids.add(known['moodle_bank_id'])
        ids.update(plain(p, 'Moodle ID').strip() for p in by_item[item_key] if plain(p, 'Moodle ID').strip())
        if counts[identity] != 1 or len(ids) > 1 or (not ids and titles[identity[0]] > 1):
            reason = 'Duplicate question title in export' if counts[identity] != 1 or not ids else 'Ambiguous stable Moodle ID'
            plans.append(entry('Question', name, 'refused', reason))
            continue
        moodle_id = next(iter(ids), '')
        if linked_locally.get(moodle_id, row['signature']) != row['signature']:
            plans.append(entry('Question', name, 'refused', 'Moodle ID is already linked to a different historical question'))
            continue
        linked = {p['id']: p for p in [*(by_id[moodle_id] if moodle_id else []), *by_item[item_key]]}
        if len(linked) > 1:
            plans.append(entry('Question', name, 'refused', 'Moodle ID or assignment question key matches multiple pages'))
            continue
        page = next(iter(linked.values()), None)
        if not page and candidates:
            candidate = candidates[0]
            candidate_type = normalized_type(type_name(candidate))
            allowed_types = {identity[1], ''} if moodle_id else {identity[1]}
            if (len(candidates) != 1 or plain(candidate, 'Moodle ID').strip()
                    or plain(candidate, 'Moodle item key').strip() or candidate_type not in allowed_types):
                plans.append(entry('Question', name, 'refused', 'Conflicting question title, type or Moodle ID'))
                continue
            page = candidate
            if not moodle_id and any(id_key(value) != id_key(assignment['id']) for value in relation_ids(page, 'Ülesanded')):
                plans.append(entry('Question', name, 'refused', 'Same title belongs to a different assignment; verify Moodle ID'))
                continue
        saved_links = {id_key(p['notion_page_id']) for p in [known, *bank_matches] if p.get('notion_page_id')}
        if page and ((type_name(page) and normalized_type(type_name(page)) != identity[1]) or
                     (saved_links and saved_links != {id_key(page['id'])})):
            plans.append(entry('Question', name, 'refused', 'Question type or verified page conflicts'))
            continue
        if not page and saved_links:
            plans.append(entry('Question', name, 'refused', 'Previously linked Notion page is missing or inaccessible'))
            continue

        metrics = projected_metrics([*history.get(known.get('id'), []), row] if moodle_id else [row])
        desired = property_values(metrics)
        checked = desired.pop('Moodle checked')
        if page:
            props = {k: value for k, value in desired.items() if not field_equal(page, k, value)}
        else:
            props = desired
            props['Name'] = text_property(name, 'title')
            mapped_type = TYPE_MAP.get(identity[1], TYPE_MAP.get(text_key(row['question_type']), row['question_type']))
            props['Type'] = {'select': {'name': mapped_type}}
        if moodle_id and (not page or not plain(page, 'Moodle ID').strip()):
            props['Moodle ID'] = text_property(moodle_id)
        if not moodle_id and (not page or not plain(page, 'Moodle item key').strip()):
            props['Moodle item key'] = text_property(item_key)
        for key, incoming in [('Ülesanded', [assignment['id']]), ('Category', relation_ids(assignment, 'Teema'))]:
            prior = relation_ids(page, key) if page else []
            new = [value for value in incoming if id_key(value) not in map(id_key, prior)]
            if new:
                props[key] = relation(prior + new)
        workflow = (page['properties'].get('Workflow', {}).get('select') or {}).get('name') if page else None
        if metrics['quality'] == 'At risk' and workflow in (None, 'Active'):
            props['Workflow'] = {'select': {'name': 'Fix'}}
        if props:
            props['Moodle checked'] = checked
        reason = 'Assignment question matched by title and type' if not moodle_id else ''
        plan = entry('Question', name, ('update' if page else 'create') if props else 'skipped',
                     reason if props else 'Question already up to date', page['id'] if page else None, props)
        plan.update(moodle_id=moodle_id, signature=row['signature'])
        plans.append(plan)
    ids = Counter(p.get('moodle_id') for p in plans if p.get('moodle_id'))
    targets = Counter(p['page_id'] for p in plans if p['page_id'])
    return [entry('Question', p['name'], 'refused', 'More than one CSV row targets this question')
            if ids[p.get('moodle_id')] > 1 or targets[p['page_id']] > 1 else p for p in plans]


def select_snapshot(assessments, assignment, stats_hash, legacy_key=None):
    label = page_name(assignment, 'Title')
    key = 'notion:' + id_key(assignment['id'])
    known = [a for a in assessments
             if a['assessment_key'] == key or text_key(a['quiz']) == text_key(label)]
    identical = [a for a in assessments if a['stats_hash'] == stats_hash]
    if len(known) > 1:
        raise ValueError('Several snapshots match this assignment. Resolve the duplicate history before importing.')
    if legacy_key:
        chosen = [a for a in assessments if a['assessment_key'] == legacy_key]
        if len(chosen) != 1:
            raise ValueError('The selected snapshot is no longer available.')
        if known and chosen != known:
            raise ValueError('The selected snapshot differs from the one already linked to this assignment.')
    else:
        chosen = known or identical
    if chosen and identical and identical != chosen:
        raise ValueError('These statistics already belong to a different snapshot.')
    prior = chosen[0] if chosen else None
    if prior:
        if prior['assessment_key'].startswith('notion:') and prior['assessment_key'] != key:
            raise ValueError('These statistics already belong to another Notion assignment.')
        key = prior['assessment_key']
    return key, prior


def preview_import(notion, cloud, assignment, rows, stats_hash, grades, legacy_key=None):
    key, prior = select_snapshot(cloud.assessments(), assignment, stats_hash, legacy_key)
    if grades is None:
        result = []
    else:
        result = plan_grades(grades, notion.all_pages(STUDENTS), notion.all_pages(RESULTS), assignment)
    history = defaultdict(list)
    for observation in cloud.select(
        'observations',
        'select=assessment_key,item_key,question_id,attempts,facility,discrimination,version_label'
        '&order=assessment_key.asc,item_key.asc',
    ):
        if observation['assessment_key'] != key and observation['version_label'] == 'unverified':
            history[observation['question_id']].append(observation)
    plans = result + plan_questions(rows, notion.all_pages(), cloud.bank_items(), assignment,
                                    history, cloud.questions())
    return key, prior, plans


def save_snapshot(cloud, assignment, rows, stats_hash, key, prior):
    label = page_name(assignment, 'Title')
    if prior and prior['stats_hash'] == stats_hash:
        if prior['quiz'] != label:
            encoded_key = quote(key, safe='')
            cloud.update('assessments', 'assessment_key=eq.' + encoded_key, {'quiz': label})
        return 'Statistics already stored'
    outcome = cloud.rpc('import_assessment', {
        'p_assessment_key': key, 'p_quiz': label,
        'p_cohort': prior['cohort'] if prior else 'Unknown',
        'p_academic_year': prior['academic_year'] if prior else 'Unknown',
        'p_stats_hash': stats_hash, 'p_rows': rows,
    })
    if outcome.get('result') != 'imported':
        raise ValueError('Statistics changed during import. Rebuild the preview.')
    return f"Statistics saved · {outcome['rows']} question rows"


def apply_entries(notion, cloud, plans):
    receipt = []
    questions = {q['signature']: q for q in cloud.questions()}
    bank = {b['moodle_id']: b for b in cloud.bank_items()}
    for row in plans:
        record = {key: row[key] for key in ('kind', 'name', 'status', 'reason')}
        receipt.append(record)
        if row['status'] == 'refused' or (row['status'] == 'skipped' and row['kind'] == 'Grade'):
            continue
        page_id = row['page_id']
        try:
            if row['status'] == 'create':
                destination = RESULTS if row['kind'] == 'Grade' else DS_ID
                page_id = notion.create_page(destination, row['properties'])['id']
            elif row['status'] in {'fill', 'update'}:
                notion.patch(page_id, row['properties'])
        except Exception as error:
            record.update(status='failed', reason=str(error))
            continue
        if row['kind'] == 'Question' and row.get('moodle_id'):
            try:
                question = questions.get(row['signature'])
                if question and (question.get('moodle_bank_id'), question.get('notion_page_id')) != (row['moodle_id'], page_id):
                    cloud.update('questions', 'id=eq.' + question['id'],
                                 {'moodle_bank_id': row['moodle_id'], 'notion_page_id': page_id})
                item = bank.get(row['moodle_id'])
                if item and item.get('notion_page_id') != page_id:
                    cloud.update('bank_items', 'moodle_id=eq.' + row['moodle_id'], {'notion_page_id': page_id})
            except Exception as error:
                record['reason'] = f'Notion is up to date; saving the local link failed: {error}'
    return receipt
