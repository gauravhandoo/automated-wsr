# Automated WSR Generator

A Streamlit application that reads Jira Stories/Bugs, applies AI summarization, and generates a PowerPoint status report while preserving your base template.

## Features

- Connects to Jira and reads Stories, Bugs, and their subtasks.
- Computes reporting buckets per module:
  - Carried over from previous period (excluding: On Hold, Discarded, Done/Sign-Off, Deployed to Production)
  - Created in reporting period
  - Moved to Deployed to UAT in reporting period
  - Moved to Deployed to Production in reporting period
  - Moved to Ready for QA in reporting period
- AI summaries include key progress, decisions, and dependencies in <=30 words per Story/Bug.
- Splits updates into AMS vs SLCM tracks:
  - AMS: labels not containing `SLCM`
  - SLCM: labels containing `SLCM`
- Lets users review/edit generated summaries before creating the PPT.
- Writes output back into slides in your template by matching slide titles with module/track names.

## Prerequisites

- Python 3.11+
- Jira Cloud API token (or compatible Jira auth)
- OpenAI-compatible API key for model summarization (optional, fallback summarizer is available)

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Create `.env` from `.env.example` and fill values:

```bash
copy .env.example .env
```

3. Run app:

```bash
streamlit run app.py
```

## UX Workflow

1. Enter Jira connection details in sidebar.
2. Select cut-off date and reporting duration in weeks.
3. Select AI model.
4. Upload your status report PPT template.
5. Click **Fetch Jira + Build Draft**.
6. Review and edit generated summaries.
7. Click **Generate PowerPoint** and download output.

## Notes on Template Handling

- Existing template slide design/layout is retained.
- The app attempts to match slides by title text:
  - Module slides: title contains module name
  - Track slides: title contains `AMS` or `SLCM` and `status`
- If no matching slide is found, a new slide is appended.

## Suggested Enhancements

- Add explicit placeholder tags (for example `{{MODULE_BODY}}`) and direct text replacement.
- Add historical status-at-date reconstruction for strict as-of cut-off calculations.
- Add Jira field mapping UI for module extraction and status aliases.
- Persist approved summary edits for audit history.
