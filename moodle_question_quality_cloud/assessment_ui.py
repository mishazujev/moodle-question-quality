"""Single preview and approval for Moodle statistics and grades."""

import hashlib
from collections import Counter
from threading import Lock

import streamlit as st

from assessment_import import (
    ASSIGNMENTS, apply_entries, assignment_matches, page_name, parse_grades,
    preview_import, quiz_title, relation_ids, save_snapshot, select_snapshot,
)
from cloud_backend import parse_csv


@st.cache_resource
def import_lock():
    return Lock()


def preview_rows(plans):
    return [
        {
            'type': row['kind'],
            'name': row['name'],
            'action': row['status'],
            'reason': row['reason'],
            'fields': ', '.join(row['properties']),
            'quality': row['properties'].get('Quality', {}).get('select', {}).get('name', ''),
            'workflow': row['properties'].get('Workflow', {}).get('select', {}).get('name', ''),
            'points': row['properties'].get('Punktid', {}).get('number'),
            'maximum': row['properties'].get('Maksimum', {}).get('number'),
        }
        for row in plans
    ]


def assignment_state(page):
    """Compare meaningful values without depending on relation order or UUID formatting."""
    return (
        page_name(page, 'Title'),
        tuple(sorted(value.replace('-', '').lower() for value in relation_ids(page, 'Meetod'))),
        tuple(sorted(value.replace('-', '').lower() for value in relation_ids(page, 'Teema'))),
        bool(page.get('in_trash') or page.get('archived')),
    )


def show_receipt(snapshot, receipt):
    counts = Counter((row['kind'], row['status']) for row in receipt)
    refused = sum(row['status'] == 'refused' for row in receipt)
    failed = sum(row['status'] == 'failed' for row in receipt)
    st.write(snapshot)
    st.write(f"{counts['Grade', 'create']} results created · {counts['Grade', 'fill']} grades filled · "
             f"{counts['Grade', 'skipped']} grades already recorded")
    st.write(f"{counts['Question', 'create']} questions created · {counts['Question', 'update']} questions updated · "
             f"{counts['Question', 'skipped']} questions already current")
    st.write(f'{refused} refused · {failed} failed')
    if receipt:
        st.dataframe(receipt, hide_index=True, use_container_width=True)


def render_assessment_import(cloud, notion, clear_reports):
    statistics = st.file_uploader('Moodle Statistics CSV', type=['csv'], key='assessment_statistics',
                                  help='Upload completestats.csv or questionstats.csv for the quiz.')
    grades = st.file_uploader('Moodle Grades CSV (optional)', type=['csv'], key='assessment_grades',
                              help='Export from Results → Grades for the same quiz. The statistics report has no individual grades.')
    if not statistics:
        return
    raw = statistics.getvalue()
    grades_raw = grades.getvalue() if grades else None
    inputs = (hashlib.sha256(raw).hexdigest(), hashlib.sha256(grades_raw).hexdigest() if grades_raw else None)
    state = st.session_state
    if state.get('assessment_inputs') != inputs:
        state['assessment_inputs'] = inputs
        for name in ('assignment_candidates', 'assessment_assignment', 'assessment_existing',
                     'assessment_preview', 'assessment_receipt'):
            state.pop(name, None)
    try:
        rows, stats_hash, _ = parse_csv(raw)
        grade_rows = parse_grades(grades_raw) if grades_raw is not None else None
        name = quiz_title(raw)
    except ValueError as error:
        st.error(str(error))
        return
    if not name:
        st.error('The statistics CSV has no quiz title. Export a report with the quiz overview, or use Statistics only.')
        return
    st.caption(f'{name} · {len(rows)} question statistics rows')
    if grades:
        st.caption(f'Grades file: {grades.name}. Confirm that it belongs to the quiz above.')
    if state.get('assessment_receipt'):
        show_receipt(*state['assessment_receipt'])

    if 'assignment_candidates' not in state or st.button('Refresh assignment match'):
        state.pop('assessment_preview', None)
        try:
            with st.spinner('Matching the quiz to Ülesanded Moodle…'):
                state['assignment_candidates'] = assignment_matches(name, notion.all_pages(ASSIGNMENTS))
        except Exception as error:
            state.pop('assignment_candidates', None)
            st.error(f'Could not read Ülesanded Moodle. Check the Notion integration access. {error}')
    candidates = state.get('assignment_candidates')
    if candidates is None:
        return
    if not candidates:
        st.warning('No matching assignment title or quiz code was found in Ülesanded Moodle.')
        return
    matched = st.selectbox('Notion assignment', candidates, format_func=lambda p: page_name(p, 'Title'),
                           index=0 if len(candidates) == 1 else None, key='assessment_assignment')
    if not matched:
        return
    try:
        method = relation_ids(matched, 'Meetod')
        if not method:
            st.warning('The app cannot see a Meetod link. If it is populated in Notion, connect the Meetodid database to Moodle Question Quality and refresh the assignment match.')
        assessments = cloud.assessments()
        _, prior = select_snapshot(assessments, matched, stats_hash)
    except Exception as error:
        st.error(str(error))
        return
    legacy_key = None
    if not prior and assessments:
        options = {row['assessment_key']: row for row in assessments if not row['assessment_key'].startswith('notion:')}
        if options:
            choice = st.selectbox(
                'Supabase history for this assessment', ['create', *options], index=None,
                placeholder='Choose a new assessment or the previous snapshot',
                format_func=lambda key: 'This is a new assessment' if key == 'create' else
                f"{options[key]['quiz']} · {options[key]['cohort']} · {key}",
                key='assessment_existing',
            )
            if choice is None:
                return
            if choice != 'create':
                legacy_key = choice
    preview_inputs = (inputs, matched['id'], legacy_key)
    if st.button('Preview Notion import'):
        state.pop('assessment_preview', None)
        try:
            with st.spinner('Comparing Moodle exports with Notion and assessment history…'):
                fresh = notion.request('GET', '/pages/' + matched['id'])
                if fresh.get('in_trash') or fresh.get('archived'):
                    raise ValueError('The selected assignment is archived or deleted.')
                preview = preview_import(notion, cloud, fresh, rows, stats_hash, grade_rows, legacy_key)
            state['assessment_preview'] = (preview_inputs, preview, assignment_state(fresh))
        except Exception as error:
            st.error(f'Preview failed. Check that the relevant Notion databases are shared with the app. {error}')

    saved = state.get('assessment_preview')
    if not saved or saved[0] != preview_inputs:
        return
    key, prior, plans = saved[1]
    if prior and prior['stats_hash'] == stats_hash:
        st.info(f'Equivalent statistics already stored under {key}. No additional statistics will be counted.')
    elif prior:
        st.info(f'Replace the previous statistics snapshot under {key}.')
    else:
        st.info(f'Save a new statistics snapshot under {key}.')
    if prior and prior['quiz'] != page_name(matched, 'Title'):
        st.caption('The saved snapshot will use the selected Notion assignment title for future uploads.')
    if plans:
        st.dataframe(preview_rows(plans), hide_index=True, use_container_width=True)
    if st.button('Approve and import', type='primary'):
        try:
            with import_lock(), st.spinner('Rechecking and applying the approved import…'):
                fresh = notion.request('GET', '/pages/' + matched['id'])
                if assignment_state(fresh) != saved[2]:
                    raise ValueError('The assignment title, topic, method, or access changed after preview. Build a new preview.')
                current = preview_import(notion, cloud, fresh, rows, stats_hash, grade_rows, legacy_key)
                if current != saved[1]:
                    raise ValueError('The preview is out of date. Build a new preview before importing.')
                snapshot = save_snapshot(cloud, fresh, rows, stats_hash, key, prior)
                clear_reports()
                receipt = apply_entries(notion, cloud, plans)
            state['assessment_receipt'] = (snapshot, receipt)
            for name in ('assessment_preview', 'notion_pages', 'notion_changes', 'links'):
                state.pop(name, None)
            st.rerun()
        except Exception as error:
            state.pop('assessment_preview', None)
            st.error(str(error))
