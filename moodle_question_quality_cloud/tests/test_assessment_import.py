import unittest

from assessment_import import (
    assignment_matches, parse_grades, plan_grades, plan_questions, select_snapshot, text_property, relation,
)
from cloud_backend import parse_csv


def page(identifier, **properties):
    return {'id': identifier, 'properties': properties}


ASSIGNMENT = page('a-1', Title=text_property('F01-03. Scientific method (graded)', 'title'),
                  Teema=relation(['topic-1']))
STUDENT = page('s-1', Name=text_property('Example Student', 'title'))
RESULT = page('r-1', Õpilane=relation(['s-1']), Ülesanne=relation(['a-1']),
              Punktid={'number': 0}, Maksimum={'number': 10})


class TestAssessmentImport(unittest.TestCase):
    def test_complete_ordering_report_stops_before_numbered_response_parts(self):
        basic = ('Quiz name,Course name\nExample,Course\n'
                 'Q#,Question type,Question name,Attempts,Facility index,Discrimination index\n'
                 '1,Ordering,Steps,25,80%,24%\n').encode()
        suffix = ('Part of question,Model response,Partial credit,Count,Frequency\n'
                  '1,First step,100%,25,100%\n1,Second step,0%,0,0%\n').encode()
        self.assertEqual(parse_csv(basic), parse_csv(basic + suffix))

    def test_grade_import_preserves_recorded_zero_and_reports_conflicts(self):
        raw = b'First name,Surname,Email address,State,Grade/10.00\nExample,Student,s@example.test,Finished,7.5\n'
        rows = parse_grades(raw)
        plan = plan_grades(rows, [STUDENT], [RESULT], ASSIGNMENT)
        self.assertEqual(plan[0]['status'], 'refused')
        self.assertEqual(plan[0]['reason'], 'Recorded grade differs')
        self.assertEqual(plan[0]['properties'], {})
        same_grade = parse_grades(raw.replace(b'7.5', b'0'))
        self.assertEqual(plan_grades(same_grade, [STUDENT], [RESULT], ASSIGNMENT)[0]['status'], 'skipped')

    def test_grade_import_creates_missing_and_fills_legacy_blank_grade(self):
        raw = 'Eesnimi,Perekonnanimi,Olek,"Hinne/10,00"\nExample,Student,Lõpetatud,"7,5"\n'
        rows = parse_grades(raw.encode())
        created = plan_grades(rows, [STUDENT], [], ASSIGNMENT)[0]
        self.assertEqual(created['status'], 'create')
        self.assertEqual(created['properties']['Õpilane'], relation(['s-1']))
        self.assertEqual(created['properties']['Protsent'], {'number': 75})
        blank = page('r-1', Õpilane=relation(['s-1']), Ülesanne=relation(['a-1']),
                     Maksimum={'number': 10}, Olek={'select': {'name': 'In progress'}})
        filled = plan_grades(rows, [STUDENT], [blank], ASSIGNMENT)[0]
        self.assertEqual(filled['status'], 'fill')
        self.assertEqual(filled['properties']['Punktid'], {'number': 7.5})
        self.assertNotIn('Maksimum', filled['properties'])

    def test_duplicate_attempt_grades_require_teacher_review(self):
        raw = b'First name,Surname,Grade/10\nExample,Student,7\nExample,Student,8\n'
        result = plan_grades(parse_grades(raw), [STUDENT], [], ASSIGNMENT)
        self.assertEqual([r['status'] for r in result], ['refused'])

    def test_stats_csv_cannot_be_used_as_grades(self):
        with self.assertRaisesRegex(ValueError, 'Results → Grades'):
            parse_grades(b'Quiz name,Course name\nExample,Course\n')

    def test_refresh_preserves_previous_snapshot_key(self):
        previous = {'assessment_key': 'auto:original', 'quiz': 'Moodle quiz', 'stats_hash': 'old'}
        self.assertEqual(assignment_matches('F01-03. Scientific method', [ASSIGNMENT]), [ASSIGNMENT])
        self.assertEqual(select_snapshot([previous], ASSIGNMENT, 'old'), ('auto:original', previous))
        self.assertEqual(select_snapshot([previous], ASSIGNMENT, 'new', 'auto:original'),
                         ('auto:original', previous))

    def test_question_uses_bank_id_and_preserves_editorial_workflow(self):
        row = {'name': 'Hypothesis', 'question_type': 'Multiple choice', 'signature': 'hypothesis|multiple choice',
               'attempts': 25, 'facility': 90, 'discrimination': -10}
        bank = [{'moodle_id': '123', 'name': 'Hypothesis', 'question_type': 'multichoice'}]
        planned = plan_questions([row], [], bank, ASSIGNMENT, {}, [])[0]
        self.assertEqual(planned['status'], 'create')
        self.assertEqual(planned['properties']['Moodle ID'], text_property('123'))
        self.assertEqual(planned['properties']['Ülesanded'], relation(['a-1']))
        existing = page('q-1', Name=text_property('Hypothesis', 'title'), Moodle_ID={})
        existing['properties'].update({'Moodle ID': text_property('123'), 'Workflow': {'select': {'name': 'Ready'}},
                                       'Ülesanded': relation(['a-0']), 'Category': relation(['topic-0'])})
        planned = plan_questions([row], [existing], bank, ASSIGNMENT, {}, [])[0]
        self.assertEqual(planned['properties']['Ülesanded'], relation(['a-0', 'a-1']))
        self.assertNotIn('Workflow', planned['properties'])
        self.assertEqual(plan_questions([row, row], [], bank, ASSIGNMENT, {}, [])[0]['status'], 'refused')

    def test_question_without_bank_is_scoped_and_reimported_in_place(self):
        row = {'name': 'Hypothesis', 'question_type': 'Multiple choice', 'signature': 'hypothesis|multiple choice',
               'attempts': 25, 'facility': 80, 'discrimination': 30}
        created = plan_questions([row], [], [], ASSIGNMENT, {}, [])[0]
        self.assertEqual(created['status'], 'create')
        self.assertNotIn('Moodle ID', created['properties'])
        current = page('stat-1', **created['properties'])
        current['properties']['Name'] = text_property('Renamed in Notion', 'title')
        existing = plan_questions([row], [current], [], ASSIGNMENT, {}, [])[0]
        self.assertEqual(existing['page_id'], 'stat-1')
        other_assignment = page('a-2', Teema=relation(['topic-2']))
        current['properties']['Name'] = text_property('Hypothesis', 'title')
        self.assertEqual(plan_questions([row], [current], [], other_assignment, {}, [])[0]['status'], 'refused')
