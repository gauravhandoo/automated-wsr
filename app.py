from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st

from src.analysis import build_module_snapshots, build_track_snapshots, derive_dependencies, summarize_ticket_with_ai
from src.config import calculate_window, load_settings
from src.jira_client import JiraService
from src.models import StatusConfig
from src.ppt_builder import PptStatusReportBuilder, build_output_filename

st.set_page_config(page_title="Automated WSR Generator", layout="wide")
st.title("Automated WSR Generator")
st.caption("Jira-driven Weekly/Fortnightly status report with AI summarization and PPT output.")

settings = load_settings()

# -- AI provider detection --
_PROVIDER_RULES = [
    (("claude",),                                 "Anthropic", "https://api.anthropic.com/v1"),
    (("gemini", "google"),                        "Google",    "https://generativelanguage.googleapis.com/v1beta/openai"),
    (("gpt", "o1-", "o3", "o4"),                 "OpenAI",    "https://api.openai.com/v1"),
    (("llama", "mistral", "mixtral", "deepseek"), "Custom",    "https://api.openai.com/v1"),
]

_DEFAULT_MODEL_CATALOG = (
    "# -- OpenAI --\n"
    "gpt-4.1-mini\n"
    "gpt-4.1\n"
    "gpt-4o\n"
    "gpt-4o-mini\n"
    "o4-mini\n"
    "o3\n"
    "# -- Anthropic (Claude) --\n"
    "claude-opus-4-5\n"
    "claude-sonnet-4-5\n"
    "claude-haiku-3-5\n"
    "# -- Google (Gemini) --\n"
    "gemini-2.5-pro\n"
    "gemini-2.0-flash\n"
    "gemini-1.5-flash"
)

_DEFAULT_LABEL_MAPPING = (
    "AMP=Admissions Module\n"
    "Cohort=Admissions Module\n"
    "Scholarship=Admissions Module\n"
    "AMS_Support=AMS Non-Admissions Module\n"
    "Allotments=AMS Non-Admissions Module\n"
    "Allotment=AMS Non-Admissions Module\n"
    "Donations=AMS Non-Admissions Module\n"
    "FA=AMS Non-Admissions Module\n"
    "FacultyContract=AMS Non-Admissions Module\n"
    "LMS=AMS Non-Admissions Module\n"
    "Umbraco=AMS Non-Admissions Module\n"
    "alumni=AMS Non-Admissions Module\n"
    "leads=AMS Non-Admissions Module\n"
    "security_review=AMS Non-Admissions Module\n"
    "digitallearning=Executive Education\n"
    "oppo=Executive Education\n"
    "SFMC=SFMC\n"
    "marketing=SFMC"
)

_DEFAULT_STATUSES = [
    "Backlog", "To Do", "In Progress", "In Review",
    "Ready for QA", "In QA",
    "Deployed to UAT", "Deployed to Production",
    "On Hold", "Discarded", "Done", "Done/Sign-Off",
]


def _detect_provider(model):
    m = model.lower()
    for keywords, provider, url in _PROVIDER_RULES:
        if any(kw in m for kw in keywords):
            return provider, url
    return "OpenAI", "https://api.openai.com/v1"


if "label_map_raw" not in st.session_state:
    st.session_state["label_map_raw"] = _DEFAULT_LABEL_MAPPING
if "_prev_provider" not in st.session_state:
    st.session_state["_prev_provider"] = ""
if "_provider_base_url" not in st.session_state:
    st.session_state["_provider_base_url"] = settings.openai_base_url

# SIDEBAR
with st.sidebar:
    st.header("1) Inputs")
    jira_server  = st.text_input("Jira Server URL",  value=settings.jira_server)
    jira_email   = st.text_input("Jira Email",        value=settings.jira_email)
    jira_token   = st.text_input("Jira API Token",    value=settings.jira_api_token, type="password")
    jira_project = st.text_input("Jira Project Key",  value=settings.jira_project_key)

    module_source = st.selectbox(
        "Module Source",
        ["labels", "customfield", "components"],
        index=0,
        help="Select where module should be identified from.",
    )
    jira_module_field = st.text_input(
        "Jira Module Field (customfield id)",
        value=settings.jira_module_field,
        help="Used only when Module Source is customfield, e.g. customfield_12345.",
    )
    module_label_prefix = st.text_input(
        "Module Label Prefix (optional)",
        value="",
        help="If set (e.g. module:), only labels starting with this prefix are used.",
    )

    st.markdown("**Label to Module Name Mapping** (one per line, format: label=Slide Module Name)")
    label_map_raw = st.text_area(
        "Label Mapping",
        value=st.session_state["label_map_raw"],
        height=130,
        key="label_map_textarea",
        help="Maps Jira label values to the module name used to match slide titles in the PPT template.",
    )
    st.session_state["label_map_raw"] = label_map_raw

    _creds_ready = bool(jira_server and jira_email and jira_token and jira_project)

    if st.button(
        "Load Labels from Jira",
        disabled=not _creds_ready,
        help="Fetches unique labels from recent Story/Bug tickets. Fill all credentials first.",
    ):
        with st.spinner("Fetching labels..."):
            try:
                _svc = JiraService(jira_server, jira_email, jira_token)
                _fetched = _svc.fetch_project_labels(jira_project)
                st.session_state["jira_labels"] = _fetched
                st.success(f"Loaded {len(_fetched)} labels.")
            except Exception as _e:
                st.error(f"Could not load labels: {_e}")

    if st.session_state.get("jira_labels"):
        with st.expander(f"Available labels ({len(st.session_state['jira_labels'])})", expanded=False):
            _existing_keys = {
                line.partition("=")[0].strip().lower()
                for line in label_map_raw.splitlines()
                if "=" in line
            }
            _unmapped = [l for l in st.session_state["jira_labels"] if l.lower() not in _existing_keys]
            st.write(", ".join(f"`{l}`" for l in st.session_state["jira_labels"]))
            if _unmapped:
                st.caption(f"**{len(_unmapped)} not yet mapped:** {', '.join(_unmapped)}")
                if st.button("Append unmapped labels (fill in module name after =)"):
                    _additions = "\n".join(f"{l}=" for l in _unmapped)
                    st.session_state["label_map_raw"] = label_map_raw.rstrip() + "\n" + _additions
                    st.rerun()

    st.header("2) Reporting Window")
    cutoff_date = st.date_input("Cut-off Date", value=date.today())
    weeks = st.selectbox("Duration (weeks)", [1, 2, 3, 4], index=0)
    start_date, end_date = calculate_window(cutoff_date, weeks)
    st.info(f"Window: {start_date} to {end_date}")
    active_sprints_only = st.checkbox(
        "Active Sprints only",
        value=True,
        help="Adds sprint in openSprints() to JQL. Uncheck to include all sprints.",
    )

    st.header("3) Status Grouping")
    st.caption("Map your project Jira statuses to each PPT report bucket.")

    if st.button(
        "Load Statuses from Jira",
        disabled=not _creds_ready,
        help="Fetches all statuses for Story/Bug issue types. Fill credentials first.",
    ):
        with st.spinner("Fetching statuses..."):
            try:
                _svc2 = JiraService(jira_server, jira_email, jira_token)
                _fetched_s = _svc2.fetch_project_statuses(jira_project)
                st.session_state["jira_statuses"] = _fetched_s
                st.success(f"Loaded {len(_fetched_s)} statuses.")
            except Exception as _e:
                st.error(f"Could not load statuses: {_e}")

    _all_statuses = st.session_state.get("jira_statuses", _DEFAULT_STATUSES)

    def _safe_defaults(candidates):
        return [s for s in candidates if s in _all_statuses]

    excl_statuses = st.multiselect(
        "Exclude from carry-over",
        options=_all_statuses,
        default=_safe_defaults(["On Hold", "Discarded", "Done/Sign-Off", "Deployed to Production", "Done"]),
        help="Tickets in these statuses won't appear in the carried over bucket.",
    )
    uat_statuses_sel = st.multiselect(
        "Deployed to UAT statuses",
        options=_all_statuses,
        default=_safe_defaults(["Deployed to UAT"]),
        help="Tickets transitioning to any of these count as moved to UAT.",
    )
    prod_statuses_sel = st.multiselect(
        "Deployed to Production statuses",
        options=_all_statuses,
        default=_safe_defaults(["Deployed to Production"]),
        help="Tickets transitioning to any of these count as moved to Production.",
    )
    ready_qa_statuses_sel = st.multiselect(
        "Ready for QA statuses",
        options=_all_statuses,
        default=_safe_defaults(["Ready for QA", "In QA"]),
        help="Tickets transitioning to any of these count as ready for QA.",
    )

    st.header("4) AI Model")
    model_catalog_text = st.text_area(
        "Model Catalog (lines starting with # are comments)",
        value=_DEFAULT_MODEL_CATALOG,
        help="Pre-filled with common OpenAI, Anthropic, and Google models. Add your own on new lines.",
        height=215,
    )
    model_options = [
        m.strip()
        for m in model_catalog_text.replace(",", "\n").splitlines()
        if m.strip() and not m.strip().startswith("#")
    ]
    if not model_options:
        model_options = [settings.default_model]
    model_options = list(dict.fromkeys(model_options))

    _default_idx = model_options.index(settings.default_model) if settings.default_model in model_options else 0
    model = st.selectbox("Model", model_options, index=_default_idx)

    _provider, _provider_default_url = _detect_provider(model)
    if st.session_state["_prev_provider"] != _provider:
        st.session_state["_provider_base_url"] = _provider_default_url
        st.session_state["_prev_provider"] = _provider

    _key_labels = {
        "OpenAI":    "OpenAI API Key",
        "Anthropic": "Anthropic API Key",
        "Google":    "Google AI API Key",
        "Custom":    "API Key",
    }
    _key_help = {
        "OpenAI":    "Your OpenAI API key from platform.openai.com",
        "Anthropic": "Your Anthropic API key from console.anthropic.com. Tip: use OpenRouter for OpenAI-compatible access.",
        "Google":    "Your Google AI Studio key from aistudio.google.com",
        "Custom":    "API key for your custom or local model endpoint",
    }
    _url_help = {
        "OpenAI":    "Default for OpenAI. Change only if using Azure OpenAI or a compatible proxy.",
        "Anthropic": "Anthropic OpenAI-compatible endpoint. Use https://openrouter.ai/api/v1 for OpenRouter.",
        "Google":    "Google OpenAI-compatible Gemini endpoint.",
        "Custom":    "Base URL for your custom model endpoint.",
    }

    openai_api_key = st.text_input(
        _key_labels.get(_provider, "API Key"),
        value=settings.openai_api_key,
        type="password",
        help=_key_help.get(_provider, "API key for the selected model provider"),
    )
    openai_base_url = st.text_input(
        f"{_provider} Base URL",
        value=st.session_state["_provider_base_url"],
        help=_url_help.get(_provider, "Base URL for the model provider API"),
        key="_base_url_input",
    )
    st.session_state["_provider_base_url"] = openai_base_url

# MAIN CONTENT
uploaded_template = st.file_uploader("Upload PowerPoint Template (.pptx)", type=["pptx"])

fetch_col, gen_col = st.columns([1, 1])
with fetch_col:
    run_fetch = st.button("Fetch Jira + Build Draft", type="primary", use_container_width=True)
with gen_col:
    run_generate = st.button("Generate PowerPoint", use_container_width=True)

if run_fetch:
    if not all([jira_server, jira_email, jira_token, jira_project]):
        st.error("Jira connectivity inputs are required.")
    else:
        with st.status("Running fetch pipeline...", expanded=True) as status:
            try:
                if not jira_server.startswith(("http://", "https://")):
                    raise ValueError(f"Invalid Jira server URL. Must start with http:// or https://. Got: {jira_server}")

                label_module_map = {}
                for line in label_map_raw.splitlines():
                    line = line.strip()
                    if "=" in line:
                        k, _, v = line.partition("=")
                        if k.strip():
                            label_module_map[k.strip()] = v.strip()

                st.write(f"**Window:** {start_date} to {end_date}")
                st.write(f"**Project:** {jira_project}  |  **Module source:** {module_source}")
                st.write(f"**Jira Server:** {jira_server}")

                log_area = st.empty()
                log_lines = []

                def on_progress(msg):
                    log_lines.append(msg)
                    log_area.code("\n".join(log_lines[-12:]), language=None)

                on_progress("Connecting to Jira...")
                service = JiraService(
                    jira_server,
                    jira_email,
                    jira_token,
                    module_source=module_source,
                    module_field=jira_module_field,
                    module_label_prefix=module_label_prefix,
                    label_module_map=label_module_map,
                )

                tickets = service.fetch_story_bug_tickets(
                    jira_project, cutoff_date, start_date=start_date,
                    on_progress=on_progress,
                    active_sprints_only=active_sprints_only,
                )

                if not tickets:
                    status.update(label="No tickets found", state="error")
                    st.warning(
                        f"No Story/Bug tickets found for project '{jira_project}' in window "
                        f"{start_date} to {end_date}. Verify: Jira server URL, project key, credentials, and active sprint status."
                    )
                else:
                    st.write(f"**AI summarisation** - processing {len(tickets)} tickets...")
                    ai_bar = st.progress(0, text="Starting AI summaries...")
                    for i, ticket in enumerate(tickets, 1):
                        ai_bar.progress(i / len(tickets), text=f"AI summary: {ticket.key} ({i}/{len(tickets)})")
                        ticket.dependencies = derive_dependencies(ticket)
                        ticket.ai_summary = summarize_ticket_with_ai(
                            ticket=ticket,
                            model=model,
                            api_key=openai_api_key,
                            base_url=openai_base_url,
                        )
                    ai_bar.progress(1.0, text=f"AI summaries complete - {len(tickets)} tickets done.")

                    _status_cfg = StatusConfig(
                        excluded_carry_over=frozenset(excl_statuses),
                        uat_statuses=frozenset(uat_statuses_sel),
                        prod_statuses=frozenset(prod_statuses_sel),
                        ready_qa_statuses=frozenset(ready_qa_statuses_sel),
                    )
                    st.session_state["tickets"] = tickets
                    st.session_state["status_config"] = _status_cfg
                    st.session_state["module_snapshots"] = build_module_snapshots(
                        tickets, start_date, end_date, status_config=_status_cfg
                    )
                    st.session_state["track_snapshots"] = build_track_snapshots(tickets, start_date, end_date)
                    status.update(label=f"Done - {len(tickets)} tickets fetched and summarised.", state="complete")

            except ConnectionResetError as e:
                status.update(label="Connection lost", state="error")
                st.error(
                    f"**Connection Reset by Jira Server.** This usually means:\n"
                    f"- Jira server URL is incorrect or unreachable\n"
                    f"- Network/firewall is blocking the connection\n"
                    f"- Jira API credentials are invalid (check email & token)\n\n"
                    f"**Details:** {e}"
                )
            except ValueError as e:
                status.update(label="Invalid input", state="error")
                st.error(f"**Invalid configuration:** {e}")
            except Exception as exc:
                status.update(label="Fetch failed", state="error")
                st.error(
                    f"**Fetch failed:** {type(exc).__name__}\n\n"
                    f"{exc}\n\n"
                    "Check Jira connectivity, API token validity, and network access."
                )

if "tickets" in st.session_state:
    st.subheader("5) Review and Edit Generated Content")

    rows = []
    for ticket in st.session_state["tickets"]:
        rows.append({
            "Module": ticket.module,
            "Track": "SLCM" if ticket.is_slcm else "AMS",
            "Key": ticket.key,
            "Type": ticket.issue_type,
            "Status": ticket.status,
            "Summary (editable)": ticket.ai_summary,
            "Dependencies": " | ".join(ticket.dependencies),
        })

    df = pd.DataFrame(rows)
    edited_df = st.data_editor(
        df,
        use_container_width=True,
        num_rows="fixed",
        hide_index=True,
        column_config={"Summary (editable)": st.column_config.TextColumn(width="large")},
    )

    if st.button("Apply Review Edits"):
        edited_map = {row["Key"]: row["Summary (editable)"] for _, row in edited_df.iterrows()}
        for ticket in st.session_state["tickets"]:
            if ticket.key in edited_map:
                ticket.ai_summary = str(edited_map[ticket.key]).strip()

        _saved_cfg = st.session_state.get("status_config", StatusConfig())
        st.session_state["module_snapshots"] = build_module_snapshots(
            st.session_state["tickets"], start_date, end_date, status_config=_saved_cfg
        )
        st.session_state["track_snapshots"] = build_track_snapshots(
            st.session_state["tickets"], start_date, end_date
        )
        st.success("Edits applied.")

    st.markdown("### AMS / SLCM Combined Counts")
    tracks = st.session_state["track_snapshots"]
    track_rows = [
        {"Track": t.track, "Stories": t.stories_count, "Bugs": t.bugs_count,
         "Total": t.stories_count + t.bugs_count}
        for t in tracks.values()
    ]
    st.dataframe(pd.DataFrame(track_rows), hide_index=True)

if run_generate:
    if uploaded_template is None:
        st.error("Please upload the PowerPoint template first.")
    elif "module_snapshots" not in st.session_state or "track_snapshots" not in st.session_state:
        st.error("Please fetch Jira data and review summaries before generating PPT.")
    else:
        with st.spinner("Generating PowerPoint report from template..."):
            builder = PptStatusReportBuilder(uploaded_template.getvalue())
            builder.fill_all_slides(
                st.session_state["module_snapshots"],
                st.session_state["track_snapshots"],
            )

            ppt_bytes = builder.to_bytes()
            output_filename = build_output_filename(start_date, end_date)

            parent_2026 = Path(__file__).parent.parent
            save_path = parent_2026 / output_filename
            save_path.write_bytes(ppt_bytes)

            st.success(f"PowerPoint generated and saved to: {save_path}")
            st.download_button(
                "Download Generated WSR PPT",
                data=BytesIO(ppt_bytes).getvalue(),
                file_name=output_filename,
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
