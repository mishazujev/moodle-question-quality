# Moodle Question Quality — quick guide

App: https://moodle-question-quality.streamlit.app/

Purpose: track Moodle question statistics over time, review quality in Notion `Küsimused`, and manually publish approved edits back to Moodle. Supabase stores history and private XML files. Do not upload student-level responses.

## Regular workflow

1. **Import an assessment (section 1).** Upload the quiz's `completestats.csv` or `questionstats.csv`. To import grades, also upload the CSV from **Results → Grades** for the same quiz. Review the assignment match, choose the prior snapshot if prompted, then preview and approve the import. The receipt lists saved statistics, grade and question changes, and skipped or refused rows. The grade importer fills blank results and creates results for matched students. A recorded grade stays in place. **Statistics only** is available for imports without a Notion assignment.
2. **Review history (section 2).** Use **Historical question quality** to inspect attempt counts, facility, discrimination and quality suggestions. The review CSV is downloadable.
3. **Register and link question IDs (sections 3–4; first time or for new questions).** Export the Moodle **question bank as XML** and register it. Preview question-ID matches with Notion and confirm only correct ones. Use manual linking for renamed questions. HTML can help identify IDs but cannot provide XML editing templates. A quiz statistics `Q#` is **not** a stable bank ID.
4. **Review quality (section 5).** Use this section to sync quality independently of a new import. The combined import prefers verified Moodle IDs. With a unique title and type, it can create or update a question for the verified assignment without a bank export. These pages show that assignment's statistics. Register the bank to track the same question across assignments or prepare XML revisions. The assignment carries its `Meetod` link.
5. **Edit in Notion.** Open `Küsimused`, review problematic questions, make editorial fixes, and set `Workflow = Ready` when finished.
6. **Export and publish (section 6).** Click **Prepare XML from Ready questions**; examine any skipped items or errors; download the approved XML and mark the batch **Awaiting import**. Import the XML into Moodle **manually**, verify the questions and grading there, then confirm **Published** in the app. A Moodle XML import can create new questions/versions, so inspect the result.

## What the statuses mean

**Quality** (data-based screening, not an automatic editorial decision):
- `More data`: fewer than 10 attempts or incomplete data; don't auto-fix.
- `Keep candidate`: no strong issue detected by the current rules.
- `Review`: teacher should inspect unusual difficulty or weak discrimination.
- `At risk`: stronger warning, such as negative discrimination; check question wording and answer key.

**Workflow** (teacher's editorial state): `Active` → `Fix` / `Update` when necessary → `Ready` → `Awaiting import` → `Published`. `Retired` is no longer in use. Quality and Workflow are separate properties.

## Important reminders

- Statistics CSVs analyze **performance**; question-bank XML provides **stable IDs and export templates**. Both are needed for the complete workflow, but at different stages.
- Review Notion matches before confirming. Name-only matches can be wrong, especially after renaming.
- Supported XML revisions: numerical, short answer, true/false and limited multiple choice. Complex or media-rich questions may be skipped, with a reason shown.
- Never mark `Published` until the XML has actually been imported and checked in Moodle.
- An old local `quality.sqlite` can be migrated once using the **One-time migration** section; do not reimport it repeatedly.
- Share **Ülesanded Moodle**, **Tunniteemad**, **Meetodid**, **Küsimused**, **Õpilased**, and **Õpitulemused** with the app's Notion integration as needed for the combined import and its relations.
- Never commit keys, student data, quiz CSVs or source XML to GitHub. Credentials belong only in Streamlit's server-side **App settings → Secrets**. Use the Grades export for scores. Individual question-response exports should stay in Moodle.

## Where things live

- App: https://moodle-question-quality.streamlit.app/
- Code: https://github.com/mishazujev/moodle-question-quality
- Supabase: https://supabase.com/dashboard/project/fkiqdxfdmxjqcmrbmqbz
- Notion: `Küsimused` (existing integration)

The app uses the separate `moodle_quality` schema in the existing Supabase project. Under **Project Settings → Data API → Exposed schemas**, `moodle_quality` must be enabled. Streamlit secret names: `APP_PASSWORD`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `NOTION_TOKEN`.

Changes pushed to GitHub `main` normally trigger a Streamlit redeploy; imported data persists in Supabase.
