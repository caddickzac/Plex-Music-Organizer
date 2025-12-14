"""
Plex Library Organizer — Streamlit App (scaffold v10)

New in v10:
- Adds tab: "Compare Exported Metadata"
  - Upload "old" and "new" exported CSVs
  - Compare: Artist_Collections, Album_Collections, Track_Collections, Playlists, User_Rating
  - Match key (Option A): Album_Artist + Album + Disc # + Track #
  - Outputs a CSV based on "new" with *_Match columns: yes/no/not found
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

    # NOTE: your export script now defaults to YYYY_MM_DD plex_music_exported_details.csv
    # but we still pass OUTPUT_CSV here for predictability.
    out_path = os.path.join(APP_DIR, "plex_music_exported_details.csv")

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

    if not os.path.isfile(out_path):
        raise FileNotFoundError("Export finished but CSV not found. Ensure the script writes the file or honors OUTPUT_CSV.")

    try:
        df = pd.read_csv(out_path)
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
# Compare helpers (new tab)
# ---------------------------
COMPARE_VARS = ["Artist_Collections", "Album_Collections", "Track_Collections", "Playlists", "User_Rating"]
KEY_COLS_POSITION = ["Album_Artist", "Album", "Disc #", "Track #"]

def _norm_str(x) -> str:
    if x is None:
        return ""
    try:
        # pandas may give NaN floats if dtype inference happens
        if isinstance(x, float) and pd.isna(x):
            return ""
    except Exception:
        pass
    return str(x).strip()

def _parse_set(cell: str) -> set[str]:
    s = _norm_str(cell)
    if not s:
        return set()
    parts = [p.strip() for p in s.split(",")]
    parts = [p for p in parts if p]
    parts = [" ".join(p.split()) for p in parts]
    return set(parts)

def _rating_to_float(x):
    s = _norm_str(x)
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None

def _build_position_key(df: pd.DataFrame) -> pd.Series:
    missing = [c for c in KEY_COLS_POSITION if c not in df.columns]
    if missing:
        raise ValueError(f"Missing key columns in CSV: {missing}. Needed for matching.")
    parts = [df[c].astype(str).fillna("").str.strip() for c in KEY_COLS_POSITION]
    return parts[0].str.cat(parts[1:], sep=" | ")

def compare_exports_add_match_cols(
    old_df: pd.DataFrame,
    new_df: pd.DataFrame,
    include_details: bool = False
) -> tuple[pd.DataFrame, dict]:
    """
    Returns:
      - result_df: copy of new_df with *_Match columns appended (yes/no/not found)
      - summary: dict of counts

    Match key: Album_Artist + Album + Disc # + Track #

    If include_details=True:
      For list vars (collections/playlists) also add:
        <Var>_Old, <Var>_Lost, <Var>_Gained
      For User_Rating also add:
        User_Rating_Old
    """
    old = old_df.copy()
    new = new_df.copy()

    old["_key"] = _build_position_key(old)
    new["_key"] = _build_position_key(new)

    old_dupes = int(old["_key"].duplicated().sum())
    new_dupes = int(new["_key"].duplicated().sum())

    old1 = old.drop_duplicates("_key", keep="first")
    new1 = new.drop_duplicates("_key", keep="first")

    old_cols = ["_key"] + [c for c in COMPARE_VARS if c in old1.columns]
    old_sub = old1[old_cols].copy()
    old_sub = old_sub.rename(columns={c: f"{c}__old" for c in COMPARE_VARS if c in old_sub.columns})

    merged = new1.merge(old_sub, on="_key", how="left", indicator=True)
    found_mask = (merged["_merge"] == "both")

    # Prepare columns we may add to the output mapping (per key)
    per_key_cols = {}  # key -> dict of computed outputs

    for i in range(len(merged)):
        k = merged.loc[i, "_key"]
        row_out = {}

        is_found = bool(found_mask.iloc[i])

        for c in COMPARE_VARS:
            match_col = f"{c}_Match"

            if not is_found:
                row_out[match_col] = "not found"
                if include_details:
                    if c == "User_Rating":
                        row_out["User_Rating_Old"] = "not found"
                    else:
                        row_out[f"{c}_Old"] = "not found"
                        row_out[f"{c}_Lost"] = "not found"
                        row_out[f"{c}_Gained"] = "not found"
                continue

            # FOUND: compare values
            if c == "User_Rating":
                new_val = merged.loc[i, c] if c in merged.columns else ""
                old_val = merged.loc[i, f"{c}__old"] if f"{c}__old" in merged.columns else ""

                n = _rating_to_float(new_val)
                o = _rating_to_float(old_val)

                if n is None and o is None:
                    row_out[match_col] = "yes"
                elif (n is not None) and (o is not None) and (n == o):
                    row_out[match_col] = "yes"
                else:
                    row_out[match_col] = "no"

                if include_details:
                    # keep old rating as a readable string (or blank)
                    row_out["User_Rating_Old"] = _norm_str(old_val)

            else:
                new_val = merged.loc[i, c] if c in merged.columns else ""
                old_val = merged.loc[i, f"{c}__old"] if f"{c}__old" in merged.columns else ""

                nset = _parse_set(new_val)
                oset = _parse_set(old_val)

                row_out[match_col] = "yes" if nset == oset else "no"

                if include_details:
                    lost = oset - nset
                    gained = nset - oset
                    row_out[f"{c}_Old"] = ", ".join(sorted(oset, key=lambda x: x.lower()))
                    row_out[f"{c}_Lost"] = ", ".join(sorted(lost, key=lambda x: x.lower()))
                    row_out[f"{c}_Gained"] = ", ".join(sorted(gained, key=lambda x: x.lower()))

        per_key_cols[k] = row_out

    # Build result_df on ORIGINAL new_df (not de-duped) by mapping per-key outputs back
    result = new_df.copy()
    result["_key"] = _build_position_key(result)

    # Always add match columns
    match_cols = [f"{c}_Match" for c in COMPARE_VARS]
    for mc in match_cols:
        result[mc] = result["_key"].map(lambda k: per_key_cols.get(k, {}).get(mc, "not found"))

    # Optional details columns
    if include_details:
        for c in COMPARE_VARS:
            if c == "User_Rating":
                col = "User_Rating_Old"
                result[col] = result["_key"].map(lambda k: per_key_cols.get(k, {}).get(col, "not found"))
            else:
                for suffix in ["Old", "Lost", "Gained"]:
                    col = f"{c}_{suffix}"
                    result[col] = result["_key"].map(lambda k: per_key_cols.get(k, {}).get(col, "not found"))

    result = result.drop(columns=["_key"])

    summary = {
        "old_rows": int(len(old_df)),
        "new_rows": int(len(new_df)),
        "old_duplicate_keys": old_dupes,
        "new_duplicate_keys": new_dupes,
        "unique_old_keys": int(old1["_key"].nunique()),
        "unique_new_keys": int(new1["_key"].nunique()),
        "matched_keys": int(found_mask.sum()),
        "unmatched_new_keys": int((~found_mask).sum()),
        "include_details": bool(include_details),
    }

    # Match counts from result (includes duplicates too, which is fine for user-facing tallies)
    for c in COMPARE_VARS:
        mc = f"{c}_Match"
        vc = result[mc].value_counts(dropna=False).to_dict()
        summary[f"{mc}_counts"] = {str(k): int(v) for k, v in vc.items()}

    return result, summary

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

def ui_compare_tab():
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Zurich")
    except Exception:
        tz = None  # fallback to local server time if zoneinfo unavailable

    st.subheader("Compare Exported Metadata")
    st.caption(
        "Upload an **old** and **new** export CSV. Tracks are matched by: "
        "**Album_Artist + Album + Disc # + Track #**. "
        "Outputs a CSV based on **new** with *_Match columns (yes/no/not found). "
        "Optionally include Old/Lost/Gained detail columns."
    )

    col1, col2 = st.columns(2)
    with col1:
        old_up = st.file_uploader("Upload OLD export CSV", type=["csv"], key="compare_old")
    with col2:
        new_up = st.file_uploader("Upload NEW export CSV", type=["csv"], key="compare_new")

    if old_up is None or new_up is None:
        st.info("Upload both files to enable comparison.")
        return

    try:
        old_df = pd.read_csv(old_up, dtype=str, keep_default_na=False)
        new_df = pd.read_csv(new_up, dtype=str, keep_default_na=False)
    except Exception as e:
        st.error(f"Could not read one of the CSVs: {e}")
        return

    with st.expander("Preview (first 25 rows)"):
        st.markdown("**OLD**")
        st.dataframe(old_df.head(25), use_container_width=True)
        st.markdown("**NEW**")
        st.dataframe(new_df.head(25), use_container_width=True)

    st.divider()

    # sanity check for key columns
    missing_old = [c for c in KEY_COLS_POSITION if c not in old_df.columns]
    missing_new = [c for c in KEY_COLS_POSITION if c not in new_df.columns]
    if missing_old or missing_new:
        st.error(
            f"Missing key columns needed for matching.\n\n"
            f"- Missing in OLD: {missing_old}\n"
            f"- Missing in NEW: {missing_new}\n\n"
            f"Expected columns: {KEY_COLS_POSITION}"
        )
        return

    st.write("Comparison variables:")
    st.code("\n".join(COMPARE_VARS), language="text")

    include_details = st.checkbox(
        "Include details (Old/Lost/Gained columns for collections & playlists)",
        value=False
    )

    run = st.button("Run comparison", type="primary")
    if not run:
        return

    try:
        result_df, summary = compare_exports_add_match_cols(
            old_df,
            new_df,
            include_details=include_details
        )

        st.success("Comparison complete.")

        # Summary metrics
        st.markdown("### Summary")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("OLD rows", summary["old_rows"])
        m2.metric("NEW rows", summary["new_rows"])
        m3.metric("Matched keys", summary["matched_keys"])
        m4.metric("Unmatched NEW keys", summary["unmatched_new_keys"])

        if summary["old_duplicate_keys"] or summary["new_duplicate_keys"]:
            st.warning(
                f"Duplicate match keys detected (OLD={summary['old_duplicate_keys']}, NEW={summary['new_duplicate_keys']}). "
                "For duplicates, the comparison uses the *first* occurrence per key."
            )

        st.markdown("### Match breakdown")
        for c in COMPARE_VARS:
            mc = f"{c}_Match"
            counts = summary.get(f"{mc}_counts", {})
            yes_n = counts.get("yes", 0)
            no_n = counts.get("no", 0)
            nf_n = counts.get("not found", 0)
            st.write(f"**{mc}** — yes: {yes_n:,} | no: {no_n:,} | not found: {nf_n:,}")

        st.markdown("### Output preview (first 50 rows)")
        st.dataframe(result_df.head(50), use_container_width=True)

        # ---- NEW: Save to APP_DIR with datestamped filename ----
        now = datetime.now(tz) if tz else datetime.now()
        date_prefix = now.strftime("%Y_%m_%d")
        out_filename = f"{date_prefix} metadata_comparison_output.csv"
        out_path = os.path.join(APP_DIR, out_filename)

        try:
            result_df.to_csv(out_path, index=False, encoding="utf-8")
            st.info(f"Saved to app folder: `{out_path}`")
        except Exception as e:
            st.error(f"Could not save file to app folder: {e}")

        # ---- Download button (manual click required by browser) ----
        out_buf = io.BytesIO()
        result_df.to_csv(out_buf, index=False)
        st.download_button(
            f"Download {out_filename}",
            data=out_buf.getvalue(),
            file_name=out_filename,
            mime="text/csv",
        )

    except Exception as e:
        st.error(f"Comparison failed: {e}")

# ---------------------------
# Main
# ---------------------------
def main():
    st.title(APP_TITLE)
    st.caption("Export music metadata, update metadata, and add to playlists and collections.")
    cfg = ui_sidebar_config()

    tab1, tab2, tab3 = st.tabs(["Export", "Update from CSV", "Compare Exported Metadata"])
    with tab1:
        ui_export_tab(cfg)
    with tab2:
        ui_update_tab(cfg)
    with tab3:
        ui_compare_tab()

if __name__ == "__main__":
    main()
