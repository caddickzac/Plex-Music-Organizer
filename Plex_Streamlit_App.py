"""
Plex Library Organizer — Streamlit App (scaffold v9)

New in v9:
- Sidebar pre-fills from ./config.txt if present (keys: "Plex URL:", "Plex Token:"; values may be quoted or plain).
- Expected schema/values now render as a single 2-column table (no index).
- Keeps prior features: cache-busted script discovery, hide export in Update tab, UTF-8 subprocesses, friendly success messages.
"""

from __future__ import annotations
import os
import io
import re
import json
import hashlib
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List
from glob import glob

import streamlit as st
st.set_page_config(page_title="Plex Music Library — Organizer", page_icon="🎵", layout="wide")

import pandas as pd
from plexapi.server import PlexServer  # type: ignore

APP_TITLE = "Plex Music Library — Organizer"
APP_DIR = os.getcwd()
SCRIPTS_DIR = os.path.join(APP_DIR, "Scripts")
CONFIG_TXT = os.path.join(APP_DIR, "config.txt")

# ---------------------------
# Config dataclass
# ---------------------------
@dataclass
class AppConfig:
    plex_baseurl: str = ""
    plex_token: str = ""

# ---------------------------
# Utilities: config.txt loader
# ---------------------------
def _strip_wrapping_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1].strip()
    return s

def load_config_txt() -> AppConfig:
    """
    Reads ./config.txt for:
      Plex URL: <value>
      Plex Token: <value>
    Values may be quoted with ' or ".
    Missing file or keys -> empty strings.
    """
    baseurl = ""
    token = ""
    try:
        with open(CONFIG_TXT, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or ":" not in line:
                    continue
                key, val = line.split(":", 1)
                key = key.strip().lower()
                val = _strip_wrapping_quotes(val)
                if key == "plex url":
                    baseurl = val
                elif key == "plex token":
                    token = val
    except FileNotFoundError:
        pass
    except Exception:
        # Be permissive; just fall back to blanks on parse issues
        pass
    return AppConfig(plex_baseurl=baseurl, plex_token=token)

# ---------------------------
# Dynamic script discovery (folder-based)
# ---------------------------
def prettify_action_label(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return base.replace("_", " ")

@dataclass
class ScriptInfo:
    action: str
    cmd: List[str]
    schema: List[str]
    path: str
    expected_values: List[str] = field(default_factory=list)

def scripts_signature() -> str:
    """
    Build a signature of all .py/.json files in ./Scripts so Streamlit cache
    invalidates whenever you add/edit/rename scripts or sidecars.
    """
    h = hashlib.sha1()
    paths = sorted(
        glob(os.path.join(SCRIPTS_DIR, "*.py")) +
        glob(os.path.join(SCRIPTS_DIR, "*.json"))
    )
    for p in paths:
        try:
            stt = os.stat(p)
            h.update(p.encode("utf-8"))
            h.update(str(stt.st_mtime_ns).encode("utf-8"))
            h.update(str(stt.st_size).encode("utf-8"))
        except FileNotFoundError:
            pass
    return h.hexdigest()

@st.cache_data(show_spinner=False)
def discover_scripts(include_exports: bool = True, _sig: str = "") -> Dict[str, ScriptInfo]:
    """
    Discover scripts in SCRIPTS_DIR. Optional sidecar JSON per script:
      {"action": "relabel: track title",
       "expected_columns": [...],
       "expected_values": [...]}

    If include_exports is False, hide export-oriented scripts (e.g., export_library_metadata.py,
    or anything whose sidecar/label contains 'export').
    `_sig` is unused except to bust the cache when the folder changes.
    """
    reg: Dict[str, ScriptInfo] = {}
    if not os.path.isdir(SCRIPTS_DIR):
        return reg

    for py in sorted(glob(os.path.join(SCRIPTS_DIR, "*.py"))):
        base = os.path.basename(py).lower()
        meta_path = os.path.splitext(py)[0] + ".json"
        action = prettify_action_label(py)
        schema: List[str] = []
        expected_values: List[str] = []

        try:
            if os.path.isfile(meta_path):
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                    action = meta.get("action", action)
                    schema = list(meta.get("expected_columns", []))
                    expected_values = list(meta.get("expected_values", []))
        except Exception:
            pass

        # Hide export scripts from Update tab
        is_export = (
            "export" in base or
            "export" in action.lower() or
            base == "export_library_metadata.py"
        )
        if not include_exports and is_export:
            continue

        # Ensure unique action names
        key = action
        suffix = 2
        while key in reg:
            key = f"{action} ({suffix})"
            suffix += 1
        reg[key] = ScriptInfo(
            action=key,
            cmd=["python", py],
            schema=schema,
            path=py,
            expected_values=expected_values,
        )

    return reg

# ---------------------------
# Export via external script (Scripts/export_library_metadata.py)
# ---------------------------
def export_library_metadata_via_script(cfg: AppConfig) -> pd.DataFrame:
    """Run external export script and load the resulting CSV."""
    if not (cfg.plex_baseurl and cfg.plex_token):
        raise RuntimeError("Missing Plex URL or Token.")

    script_path = os.path.join(SCRIPTS_DIR, "export_library_metadata.py")
    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"Export script not found: {script_path}")

    out_default = os.path.join(APP_DIR, "plex_music_exported_details.csv")
    out_path = out_default

    env = os.environ.copy()
    env.update({
        "PLEX_BASEURL": cfg.plex_baseurl,
        "PLEX_TOKEN": cfg.plex_token,
        "OUTPUT_CSV": out_path,
        # Force UTF-8 for child process output on Windows
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })

    log_box = st.empty()
    log_lines: List[str] = []

    st.write("Running external export script…")
    try:
        proc = subprocess.Popen(
            ["python", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        if proc.stdout is not None:
            for line in proc.stdout:
                log_lines.append(line.rstrip("\r\n"))
                tail = "\n".join(log_lines[-200:])
                log_box.code(tail or "…", language="bash")
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(f"export_library_metadata.py exited with code {ret}")
    except Exception as e:
        raise RuntimeError(f"Failed running export script: {e}")

    csv_file = out_path if os.path.isfile(out_path) else out_default
    if not os.path.isfile(csv_file):
        raise FileNotFoundError("Export finished but CSV not found. Ensure the script writes the file or honors OUTPUT_CSV.")

    try:
        df = pd.read_csv(csv_file)
    except Exception as e:
        raise RuntimeError(f"Could not read exported CSV: {e}")

    return df

# ---------------------------
# Helpers for nicer success messages
# ---------------------------
def parse_edited_count(stdout: str) -> int | None:
    m = re.search(r"Edited\s*=\s*(\d+)", stdout, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None

def pluralize_last_word(phrase: str) -> str:
    parts = phrase.split()
    if not parts:
        return phrase
    last = parts[-1]
    if last.endswith("s"):
        return phrase
    if last.endswith("y") and len(last) > 1 and last[-2].lower() not in "aeiou":
        last_pl = last[:-1] + "ies"
    else:
        last_pl = last + "s"
    parts[-1] = last_pl
    return " ".join(parts)

def success_message_for_action(action_label: str, edited: int | None) -> str:
    target = action_label
    if ":" in action_label:
        target = action_label.split(":", 1)[1].strip()
    target_pl = pluralize_last_word(target.lower())
    if edited is not None:
        if "relabel" in action_label.lower():
            verb = "relabeled"
        elif "add" in action_label.lower():
            verb = "added"
        else:
            verb = "updated"
        return f"{edited} {target_pl} {verb} successfully."
    return f"{action_label}: completed successfully."

# ---------------------------
# UI pieces
# ---------------------------
def ui_sidebar_config() -> AppConfig:
    # Pre-fill from config.txt on first load
    file_cfg = load_config_txt()
    if "baseurl" not in st.session_state and file_cfg.plex_baseurl:
        st.session_state["baseurl"] = file_cfg.plex_baseurl
    if "token" not in st.session_state and file_cfg.plex_token:
        st.session_state["token"] = file_cfg.plex_token

    st.sidebar.header("Configuration")
    baseurl = st.sidebar.text_input("Plex URL", placeholder="http://127.0.0.1:32400", key="baseurl")
    token = st.sidebar.text_input("Plex Token", type="password", placeholder="••••••••", key="token")

    # Hint if we actually loaded defaults from file
    if (file_cfg.plex_baseurl or file_cfg.plex_token):
        st.sidebar.caption("Defaults loaded from config.txt")

    return AppConfig(plex_baseurl=baseurl.strip(), plex_token=token.strip())

def ui_export_tab(cfg: AppConfig):
    st.subheader("Export current metadata → CSV")
    if not (cfg.plex_baseurl and cfg.plex_token):
        st.info("Enter URL and Token in the left panel to enable export.")
        return
    if st.button("Export all track details", type="primary"):
        try:
            df = export_library_metadata_via_script(cfg)
            st.success(f"Exported {len(df):,} tracks.")
            st.dataframe(df.head(50))
            out = io.BytesIO()
            df.to_csv(out, index=False)
            st.download_button(
                "Download plex_music_exported_details.csv",
                data=out.getvalue(),
                file_name="plex_music_exported_details.csv",
                mime="text/csv",
            )
        except Exception as e:
            st.error(f"Export failed: {e}")

def ui_update_tab(cfg: AppConfig):
    st.subheader("Submit changes from CSV → run your scripts")
    # Hide export scripts here; pass folder signature to bust cache on changes
    registry = discover_scripts(include_exports=False, _sig=scripts_signature())
    if not registry:
        st.warning("No scripts found. Create a `Scripts/` folder with .py files. Optional: add a matching .json sidecar for schema & action name.")
        return

    action_labels = list(registry.keys())
    action = st.selectbox("Choose an action", action_labels)

    with st.expander("Expected CSV schema & values"):
        info = registry[action]
        cols = list(info.schema or [])
        vals = list(info.expected_values or [])
        n = max(len(cols), len(vals), 1)
        cols += [""] * (n - len(cols))
        vals += [""] * (n - len(vals))
        spec_df = pd.DataFrame({"expected_columns": cols, "expected_values": vals})
        try:
            st.dataframe(spec_df, use_container_width=True, hide_index=True)
        except TypeError:
            st.dataframe(spec_df.reset_index(drop=True), use_container_width=True)
        if not info.schema and not info.expected_values:
            st.caption("No schema provided for this action. Add a sidecar JSON with `expected_columns` (and optionally `expected_values`).")

    uploaded = st.file_uploader("Upload CSV", type=["csv"], accept_multiple_files=False)

    if uploaded is not None:
        try:
            df = pd.read_csv(uploaded)
            st.write(f"CSV loaded with {len(df):,} rows.")
            st.dataframe(df.head(25))
        except Exception as e:
            st.error(f"Could not read CSV: {e}")
            return

        st.divider()
        st.warning("Writes to Plex are potentially destructive. Make a backup/export first.")

        confirm_phrase = st.text_input("Type CONFIRM to enable execution")
        ok = (confirm_phrase.strip().upper() == "CONFIRM")
        run_btn = st.button("Run script", type="primary", disabled=not ok)

        if run_btn and ok:
            tmp_path = os.path.join(APP_DIR, "uploaded.csv")
            with open(tmp_path, "wb") as f:
                f.write(uploaded.getvalue())

            try:
                spec = registry[action]
                env = os.environ.copy()
                env.update({
                    "PLEX_BASEURL": cfg.plex_baseurl,
                    "PLEX_TOKEN": cfg.plex_token,
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                })
                payload = json.dumps({
                    "action": spec.action,
                    "csv_path": tmp_path,
                })
                proc = subprocess.run(
                    spec.cmd,
                    input=payload,            # pass STR with text=True
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
                code = proc.returncode

                st.code(stdout or "<no stdout>", language="bash")
                if stderr:
                    st.error(stderr)

                if code == 0:
                    edited = parse_edited_count(stdout)
                    st.success(success_message_for_action(spec.action, edited))
                else:
                    st.error(f"{spec.action}: script failed with exit code {code}")
            except FileNotFoundError:
                st.error("Script not found. Check the `Scripts/` folder paths.")
            except Exception as e:
                st.error(f"Execution error: {e}")

def ui_registry_tab():
    st.subheader("Discovered actions → scripts")
    # Show everything in the registry (including export); pass signature to auto-refresh
    registry = discover_scripts(include_exports=True, _sig=scripts_signature())
    refresh = st.button("↻ Refresh list")
    if refresh:
        discover_scripts.clear()
        st.rerun()

    if registry:
        reg_df = pd.DataFrame([
            {
                "action": info.action,
                "command": " ".join(info.cmd),
                "path": info.path,
                "expected_columns": ", ".join(info.schema),
                "expected_values": ", ".join(info.expected_values),
            }
            for info in registry.values()
        ])
        st.dataframe(reg_df)
    else:
        st.caption("No scripts discovered yet. Add .py files to the `Scripts/` folder.")

    st.subheader("Sidecar metadata format (.json next to .py)")
    st.code(
        """{
  "action": "add: artist genres",
  "expected_columns": ["Artist_ID", "Artist_Genres"],
  "expected_values": ["12345", "Rock; Indie; Alt"]
}""",
        language="json",
    )

# ---------------------------
# Main
# ---------------------------
def main():
    st.title(APP_TITLE)
    st.caption("Configure Plex, export current metadata, and call your existing update scripts safely.")
    cfg = ui_sidebar_config()
    tab1, tab2, tab3 = st.tabs(["Export", "Update from CSV", "Script Registry"])
    with tab1:
        ui_export_tab(cfg)
    with tab2:
        ui_update_tab(cfg)
    with tab3:
        ui_registry_tab()

if __name__ == "__main__":
    main()
