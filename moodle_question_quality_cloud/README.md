# Moodle Question Quality — hosted edition

A **single-teacher** hosted Streamlit workbench. Moodle statistics are stored in Supabase Postgres; original question-bank XML and generated exports are in a private Supabase Storage bucket; existing questions in Notion `Küsimused` are updated only after a preview and approval. **No student-level data is needed.**

## Deployment (once)

1. Create a **private** GitHub repository (e.g. `moodle-question-quality`) and push the contents of this folder. Do **not** upload the old `data/` directory, SQLite databases, CSVs, question-bank XMLs, or Notion tokens. The `.gitignore` excludes these locally.
2. Create a Supabase project. Open **SQL Editor**, paste `sql/001_schema.sql`, and run it. Verify tables and the **private** `moodle-question-xml` Storage bucket exist.
3. In **Project Settings → API**, copy the **Project URL** and **Secret** API key (`sb_secret_...`). This is a powerful server-side credential; never put it in GitHub, JS, or a client browser. The code expects the new server-only `sb_secret_...` key for direct REST calls.
4. In Notion, create or reuse the internal integration, grant access to `Küsimused` with Read content + Update content, and copy its secret. The Notion data source ID is configured in `notion_bridge.py`. Required properties are listed there. (The prior v3 release already added them to your `Küsimused` database.)
5. Go to [share.streamlit.io](https://share.streamlit.io), sign in with GitHub, select the private repository, `main` branch and `app.py` as the main file. Deploy.
6. In **Streamlit Cloud → App → Settings → Secrets**, add the four TOML values shown in `.streamlit/secrets.toml.example` with your real keys. Reboot the app if needed. The `APP_PASSWORD` is your own login to the interface, not your GitHub, Supabase or Notion password.

## Routine use

- Open the Streamlit Cloud URL and sign in with the application password.
- Paste/upload `questionstats.csv` or `completestats.csv` and import. Identical statistical content is deduplicated.
- Register Moodle XML category exports. They supply stable candidate question-bank IDs (from export comments) and safe XML templates. Review identity matches with Notion, then confirm.
- Preview and sync Quality; sufficiently evidenced negative discrimination can mark an Active question `Fix`. Less than 10 attempts -> `More data`.
- Rewrite in Notion, set `Workflow=Ready`, prepare/export XML and manually import it to Moodle. After checking the import, confirm `Published`.

## Security and boundaries

- Private repository recommended, secrets only in Streamlit Cloud secret settings. Server-side the server secret bypasses Supabase row-level security. A single `APP_PASSWORD` provides basic access control, **not** per-teacher accounts or audit trails.
- Supabase tables have RLS enabled and no public policies. Use separate projects for schools or users until full authentication and tenant isolation are built.
- Old SQLite data from your computer is **not automatically imported**. Use the **One-time migration from your previous Windows app** upload in the web interface; it accepts the latest `data/quality.sqlite` with no local command or credentials needed.
- A raw Moodle `Q#` from quiz statistics is **not** a bank ID. Verified Moodle bank IDs come from XML/HTML export comments; ID stability across Moodle instances and question versions needs checking.
- Revised questions receive a new publication batch; standard Moodle XML import may create **new** questions. Publishing requires manual verification. Complex/media questions are deliberately excluded from automatic edits.
- If your Notion question `Name` differs from Moodle and no ID is established, use the manual mapping screen. Historical rows under renamed signatures are **not** silently combined.
- The deployment is not live until you create the Supabase project, run SQL, provide credentials and deploy the repository.

## Local optional testing

Install Python 3.10+ then `python -m pip install -r requirements.txt` and `python -m streamlit run app.py`. Add the same credentials into a local `.streamlit/secrets.toml` (ignored by Git). Use `python -m unittest discover -s tests` for offline tests.
