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
PLAYLIST_CREATOR_SCRIPT = os.path.join(SCRIPTS_DIR, "playlist_creator.py")

# NEW: where playlist presets will live
PRESETS_DIR = os.path.join(APP_DIR, "Playlist_Presets")

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

        # Skip Playlist Creator from the CSV update actions
        if base == "playlist_creator.py":
            continue

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
# Export via external script
# ---------------------------
def export_library_metadata_via_script(cfg: AppConfig) -> pd.DataFrame:
    """Run external export script and load the resulting CSV."""
    if not (cfg.plex_baseurl and cfg.plex_token):
        raise RuntimeError("Missing Plex URL or Token.")

    script_path = os.path.join(SCRIPTS_DIR, "export_library_metadata.py")
    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"Export script not found: {script_path}")

    out_path = os.path.join(APP_DIR, "Track_Level_Info.csv")

    env = os.environ.copy()
    env.update({
        "PLEX_BASEURL": cfg.plex_baseurl,
        "PLEX_TOKEN": cfg.plex_token,
        "OUTPUT_CSV": out_path,
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
# Forgiving CSV reader
# ---------------------------
def read_csv_forgiving(uploaded_file) -> pd.DataFrame:
    """
    Read a user-uploaded CSV with encoding fallbacks.
    Tries: utf-8, utf-8-sig, cp1252, latin-1
    """
    raw = uploaded_file.getvalue()

    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False, encoding=enc)
        except UnicodeDecodeError:
            continue
        except Exception:
            pass

    text = raw.decode("utf-8", errors="replace")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)

# ---------------------------
# Compare helpers
# ---------------------------
COMPARE_VARS = ["Artist_Collections", "Album_Collections", "Track_Collections", "Playlists", "User_Rating"]
KEY_COLS_POSITION = ["Album_Artist", "Album", "Disc #", "Track #"]

def _norm_str(x) -> str:
    if x is None:
        return ""
    try:
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

    per_key_cols = {}

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

    result = new_df.copy()
    result["_key"] = _build_position_key(result)

    match_cols = [f"{c}_Match" for c in COMPARE_VARS]
    for mc in match_cols:
        result[mc] = result["_key"].map(lambda k: per_key_cols.get(k, {}).get(mc, "not found"))

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

    for c in COMPARE_VARS:
        mc = f"{c}_Match"
        vc = result[mc].value_counts(dropna=False).to_dict()
        summary[f"{mc}_counts"] = {str(k): int(v) for k, v in vc.items()}

    return result, summary

# ---------------------------
# Sidebar config
# ---------------------------
def ui_sidebar_config() -> AppConfig:
    file_cfg = load_config_txt()
    if "baseurl" not in st.session_state and file_cfg.plex_baseurl:
        st.session_state["baseurl"] = file_cfg.plex_baseurl
    if "token" not in st.session_state and file_cfg.plex_token:
        st.session_state["token"] = file_cfg.plex_token

    st.sidebar.header("Configuration")
    baseurl = st.sidebar.text_input(
        "Plex URL",
        placeholder="http://127.0.0.1:32400",
        key="baseurl"
    )
    token = st.sidebar.text_input(
        "Plex Token",
        type="password",
        placeholder="••••••••",
        key="token"
    )

    if (file_cfg.plex_baseurl or file_cfg.plex_token):
        st.sidebar.caption("Defaults loaded from config.txt")

    return AppConfig(plex_baseurl=baseurl.strip(), plex_token=token.strip())

# ---------------------------
# Export tab
# ---------------------------
def ui_export_tab(cfg: AppConfig):
    st.subheader("Export current metadata → CSV")
    if not (cfg.plex_baseurl and cfg.plex_token):
        st.info("Enter URL and Token in the left panel to enable export.")
        return
    if st.button("Export all track details", type="primary"):
        try:
            df = export_library_metadata_via_script(cfg)
            st.success(f"Exported {len(df):,} tracks.")
            st.dataframe(df.head(50), use_container_width=True)
            out = io.BytesIO()
            df.to_csv(out, index=False)
            st.download_button(
                "Download Track_Level_Info.csv",
                data=out.getvalue(),
                file_name="Track_Level_Info.csv",
                mime="text/csv",
            )
        except Exception as e:
            st.error(f"Export failed: {e}")

# ---------------------------
# Update single-script tab
# ---------------------------
def ui_update_tab(cfg: AppConfig):
    st.subheader("Submit changes from CSV → run a single script")
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

    uploaded = st.file_uploader("Upload CSV", type=["csv"], accept_multiple_files=False, key="single_csv")

    if uploaded is not None:
        try:
            df = read_csv_forgiving(uploaded)
            st.write(f"CSV loaded with {len(df):,} rows.")
            st.dataframe(df.head(25), use_container_width=True)
        except Exception as e:
            st.error(f"Could not read CSV: {e}")
            return

        st.divider()
        st.warning("Writes to Plex are potentially destructive. Make a backup/export first.")

        confirm_phrase = st.text_input("Type CONFIRM to enable execution", key="single_confirm")
        ok = (confirm_phrase.strip().upper() == "CONFIRM")
        run_btn = st.button("Run script", type="primary", disabled=not ok, key="single_run")

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
                    input=payload,
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

# ---------------------------
# Update multi-script tab
# ---------------------------
def ui_update_multi_tab(cfg: AppConfig):
    st.subheader("Submit changes from CSV → run multiple scripts in sequence")

    registry = discover_scripts(include_exports=False, _sig=scripts_signature())
    if not registry:
        st.warning(
            "No scripts found. Create a `Scripts/` folder with .py files. "
            "Optional: add a matching .json sidecar for schema & action name."
        )
        return

    action_labels = list(registry.keys())

    st.caption("Select one or more actions to run (they will execute top-to-bottom).")

    if "multi_selected_actions" not in st.session_state:
        st.session_state["multi_selected_actions"] = []

    selected = []
    for label in action_labels:
        checked = st.checkbox(
            label,
            value=(label in st.session_state["multi_selected_actions"]),
            key=f"multi_{label}"
        )
        if checked:
            selected.append(label)

    st.session_state["multi_selected_actions"] = selected

    if not selected:
        st.info("Select at least one action to proceed.")
        return

    with st.expander("Expected CSV schema & values (selected actions)", expanded=True):
        rows = []
        for label in selected:
            info = registry[label]
            cols = list(info.schema or [])
            vals = list(info.expected_values or [])
            n = max(len(cols), len(vals), 1)
            cols += [""] * (n - len(cols))
            vals += [""] * (n - len(vals))

            if not info.schema and not info.expected_values:
                rows.append({"Action": info.action, "expected_columns": "", "expected_values": ""})
            else:
                for c, v in zip(cols, vals):
                    rows.append({"Action": info.action, "expected_columns": c, "expected_values": v})

        spec_df = pd.DataFrame(rows)
        try:
            st.dataframe(spec_df, use_container_width=True, hide_index=True)
        except TypeError:
            st.dataframe(spec_df.reset_index(drop=True), use_container_width=True)

        st.caption("Actions run in the order listed above.")

    uploaded = st.file_uploader(
        "Upload CSV (used for all selected scripts)",
        type=["csv"],
        accept_multiple_files=False,
        key="multi_csv"
    )
    if uploaded is None:
        return

    try:
        df = read_csv_forgiving(uploaded)
        st.write(f"CSV loaded with {len(df):,} rows.")
        st.dataframe(df.head(25), use_container_width=True)
    except Exception as e:
        st.error(f"Could not read CSV: {e}")
        return

    st.divider()
    st.warning("Writes to Plex are potentially destructive. Make a backup/export first.")

    confirm_phrase = st.text_input("Type CONFIRM to enable execution", key="multi_confirm")
    ok = (confirm_phrase.strip().upper() == "CONFIRM")

    run_btn = st.button(
        f"Run {len(selected)} script(s) in order",
        type="primary",
        disabled=not ok,
        key="multi_run"
    )

    if not (run_btn and ok):
        return

    tmp_path = os.path.join(APP_DIR, "uploaded.csv")
    try:
        with open(tmp_path, "wb") as f:
            f.write(uploaded.getvalue())
    except Exception as e:
        st.error(f"Could not write temporary CSV to disk: {e}")
        return

    st.divider()
    st.markdown("### Execution log")

    env = os.environ.copy()
    env.update({
        "PLEX_BASEURL": cfg.plex_baseurl,
        "PLEX_TOKEN": cfg.plex_token,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })

    any_fail = False

    for idx, label in enumerate(selected, start=1):
        spec = registry[label]
        st.markdown(f"#### {idx}. {spec.action}")

        try:
            payload = json.dumps({"action": spec.action, "csv_path": tmp_path})

            proc = subprocess.run(
                spec.cmd,
                input=payload,
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
                any_fail = True
                st.error(f"{spec.action}: script failed with exit code {code}")

        except FileNotFoundError:
            any_fail = True
            st.error("Script not found. Check the `Scripts/` folder paths.")
        except Exception as e:
            any_fail = True
            st.error(f"Execution error: {e}")

    st.divider()
    if any_fail:
        st.warning("Finished running selected scripts — at least one failed. Review logs above.")
    else:
        st.success("Finished running all selected scripts successfully.")

# ---------------------------
# Preset helpers for Playlist Creator
# ---------------------------
def ensure_presets_dir() -> None:
    try:
        os.makedirs(PRESETS_DIR, exist_ok=True)
    except Exception:
        pass

def list_presets() -> List[str]:
    ensure_presets_dir()
    try:
        names = []
        for fn in os.listdir(PRESETS_DIR):
            if fn.lower().endswith(".json"):
                names.append(os.path.splitext(fn)[0])
        names.sort()
        return names
    except Exception:
        return []

def load_preset_dict(name: str) -> dict:
    ensure_presets_dir()
    path = os.path.join(PRESETS_DIR, f"{name}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_preset_dict(name: str, data: dict) -> None:
    ensure_presets_dir()
    path = os.path.join(PRESETS_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def apply_preset_to_session(preset: dict) -> None:
    """
    Copy preset values into st.session_state for all known Playlist Creator keys.
    """
    keys = [
        "pc_lib",
        "pc_custom_title",
        "pc_preset_name", 
        "pc_exclude_days",
        "pc_lookback_days",
        "pc_max_tracks",
        "pc_sonic_limit",
        "pc_hist_ratio",
        "pc_explore_exploit",
        "pc_use_periods",

        "pc_min_track",
        "pc_min_album",
        "pc_min_artist",
        "pc_allow_unrated",

        "pc_min_play_count",
        "pc_max_play_count",

        "pc_min_year",
        "pc_max_year",
        "pc_min_duration",
        "pc_max_duration",

        "pc_recent_days",
        "pc_recent_weight",

        "pc_max_artist",
        "pc_max_album",

        "pc_hist_min_rating",
        "pc_hist_max_play_count",

        "pc_seed_mode_label",
        "pc_seed_fallback_mode",

        "pc_seed_tracks",
        "pc_seed_artists",
        "pc_seed_playlists",
        "pc_seed_collections",
        "pc_seed_genres",

        "pc_genre_strict",
        "pc_allow_off_genre",
        "pc_exclude_genres",
        "pc_include_collections",
        "pc_exclude_collections",
    ]
    for k in keys:
        if k in preset:
            st.session_state[k] = preset[k]

# ---------------------------
# Playlist Creator tab (with presets)
# ---------------------------
def ui_playlist_creator_tab(cfg: AppConfig):
    # 1. HANDLE LOADING FIRST (Before any widgets are instantiated)
    if "pc_preset_select" in st.session_state:
        # Check if the load button was pressed
        if st.get_option("server.runOnSave") or st.session_state.get("pc_btn_load"):
             selected_preset = st.session_state.get("pc_preset_select")
             if selected_preset and selected_preset != "<none>":
                 preset = load_preset_dict(selected_preset)
                 if preset:
                     # This will now work because the widgets haven't been "drawn" yet
                     apply_preset_to_session(preset)
                     
    st.subheader("Playlist Creator (sonic similarity + custom seeds)")

    st.caption(
        "Use Plex listening history, sonic similarity, and custom seeds "
        "to auto-generate a new playlist. This calls `playlist_creator.py` "
        "in the Scripts/ folder."
    )

    # ---------------------------
    # Presets UI (TOP)
    # ---------------------------
    ensure_presets_dir()
    st.markdown("### Presets")

    existing_presets = list_presets()
    preset_options = ["<none>"] + existing_presets

    # CALLBACK FUNCTION: This runs BEFORE the widgets are drawn
    def handle_load_preset():
        sel = st.session_state.get("pc_preset_select")
        if sel and sel != "<none>":
            preset = load_preset_dict(sel)
            if preset:
                apply_preset_to_session(preset)
                # Note: st.success and st.experimental_rerun aren't 
                # strictly needed inside a callback, but you can keep them.

    col_p1, col_p2 = st.columns([2, 2])
    with col_p1:
        selected_preset = st.selectbox(
            "Load existing preset",
            options=preset_options,
            index=0,
            key="pc_preset_select",
        )
    with col_p2:
        # This widget is now safe because handle_load_preset runs BEFORE this line
        preset_name = st.text_input(
            "Preset name (for saving)",
            value=st.session_state.get("pc_preset_name", ""),
            key="pc_preset_name",
            placeholder="e.g., Classic Rock 1960–79",
        )

    col_pb1, col_pb2 = st.columns(2)
    with col_pb1:
        # Use on_click to trigger the callback
        st.button("Load preset", key="pc_btn_load", on_click=handle_load_preset)

    with col_pb2:
        if st.button("Save current settings as preset", key="pc_btn_save"):
            name = (preset_name or "").strip()
            if not name:
                st.error("Please enter a preset name before saving.")
            else:
                # What we store as a preset: all relevant pc_* keys
                preset_keys = [
                    "pc_lib",
                    "pc_custom_title",
                    "pc_preset_name",
                    "pc_exclude_days",
                    "pc_lookback_days",
                    "pc_max_tracks",
                    "pc_sonic_limit",
                    "pc_hist_ratio",
                    "pc_explore_exploit",
                    "pc_use_periods",

                    "pc_min_track",
                    "pc_min_album",
                    "pc_min_artist",
                    "pc_allow_unrated",

                    "pc_min_play_count",
                    "pc_max_play_count",

                    "pc_min_year",
                    "pc_max_year",
                    "pc_min_duration",
                    "pc_max_duration",

                    "pc_recent_days",
                    "pc_recent_weight",

                    "pc_max_artist",
                    "pc_max_album",

                    "pc_hist_min_rating",
                    "pc_hist_max_play_count",

                    # 👇 make sure this is here
                    "pc_seed_mode_label",
                    "pc_seed_fallback_mode",

                    "pc_seed_tracks",
                    "pc_seed_artists",
                    "pc_seed_playlists",
                    "pc_seed_collections",
                    "pc_seed_genres",

                    "pc_genre_strict",
                    "pc_allow_off_genre",
                    "pc_exclude_genres",
                    "pc_include_collections",
                    "pc_exclude_collections",
                ]
                data = {k: st.session_state.get(k) for k in preset_keys}
                save_preset_dict(name, data)

                st.success(f"Saved preset: {name}")

    st.divider()

    # ---------------------------
    # Core playlist UI
    # ---------------------------

    # Music library name
    music_lib = st.text_input("Music library name", value="Music", key="pc_lib")

    # Playlist naming (custom title)
    st.markdown("### Playlist naming")
    custom_title = st.text_input(
        "Custom playlist title (optional)",
        value=st.session_state.get("pc_custom_title", ""),
        key="pc_custom_title",
        placeholder="e.g., Sunday Psych, Classic Rock 1960–79, Morning Ambient",
        help=(
            "If set, this will be used as the playlist title and printed onto the "
            "generated black cover art image."
        ),
    )

    st.divider()
    st.markdown("### Playlist parameters")

    col_a, col_b, col_c = st.columns(3)

    with col_a:
        exclude_days = st.number_input(
            "Exclude played (days)",
            min_value=0,
            max_value=365,
            value=st.session_state.get("pc_exclude_days", 3),
            step=1,
            key="pc_exclude_days",
        )
        lookback_days = st.number_input(
            "History lookback (days)",
            min_value=1,
            max_value=365,
            value=st.session_state.get("pc_lookback_days", 30),
            step=1,
            key="pc_lookback_days",
        )
        max_tracks = st.number_input(
            "Max tracks",
            min_value=5,
            max_value=300,
            value=st.session_state.get("pc_max_tracks", 50),
            step=1,
            key="pc_max_tracks",
        )

    with col_b:
        sonic_similar_limit = st.number_input(
            "Sonically similar per seed",
            min_value=5,
            max_value=100,
            value=st.session_state.get("pc_sonic_limit", 20),
            step=1,
            key="pc_sonic_limit",
        )
        historical_ratio = st.slider(
            "Historical ratio (fraction of tracks from history)",
            0.0,
            1.0,
            st.session_state.get("pc_hist_ratio", 0.3),
            0.05,
            key="pc_hist_ratio",
        )
        use_time_periods = st.checkbox(
            "Use time-of-day periods",
            value=st.session_state.get("pc_use_periods", False),
            key="pc_use_periods",
        )

    with col_c:
        explore_exploit = st.slider(
            "Explore vs. exploit (popularity)",
            min_value=0.0,
            max_value=1.0,
            value=st.session_state.get("pc_explore_exploit", 0.7),
            step=0.05,
            key="pc_explore_exploit",
            help=(
                "0 = pure exploration (low popularity bias), "
                "1 = pure exploitation (strong bias toward most popular tracks)."
            ),
        )
        seed_fallback_mode = st.radio(
            "When explicit seeds are too few, fill from:",
            options=["history", "genre"],
            index=0 if st.session_state.get("pc_seed_fallback_mode", "history") == "history" else 1,
            key="pc_seed_fallback_mode",
        )

    exploit_weight = float(explore_exploit)

    st.markdown("### Rating filters")
    st.caption("Minimum ratings (0–10, Plex's internal scale). Set to 0 to ignore a filter.")

    r1, r2, r3, r4 = st.columns([1, 1, 1, 1])

    with r1:
        min_track = st.number_input(
            "Min track rating",
            min_value=0,
            max_value=10,
            value=st.session_state.get("pc_min_track", 7),
            step=1,
            key="pc_min_track",
        )
    with r2:
        min_album = st.number_input(
            "Min album rating",
            min_value=0,
            max_value=10,
            value=st.session_state.get("pc_min_album", 0),
            step=1,
            key="pc_min_album",
        )
    with r3:
        min_artist = st.number_input(
            "Min artist rating",
            min_value=0,
            max_value=10,
            value=st.session_state.get("pc_min_artist", 0),
            step=1,
            key="pc_min_artist",
        )
    with r4:
        allow_unrated = st.checkbox(
            "Allow tracks with no rating",
            value=st.session_state.get("pc_allow_unrated", True),
            key="pc_allow_unrated",
            help="If checked, tracks/albums/artists with userRating = None are allowed even when minimums are set.",
        )

    st.markdown("### Play count filters")
    st.caption(
        "Based on Plex viewCount (number of plays). "
        "Use –1 to ignore a bound. Example: min=0, max=0 → only never-played tracks."
    )

    pc1, pc2 = st.columns(2)
    with pc1:
        min_play_count = st.number_input(
            "Min play count (–1 = no minimum)",
            min_value=-1,
            max_value=10000,
            value=st.session_state.get("pc_min_play_count", -1),
            step=1,
            key="pc_min_play_count",
        )
    with pc2:
        max_play_count = st.number_input(
            "Max play count (–1 = no maximum)",
            min_value=-1,
            max_value=10000,
            value=st.session_state.get("pc_max_play_count", -1),
            step=1,
            key="pc_max_play_count",
        )

    st.markdown("### Year & duration filters (album-based year)")

    y1, y2, d1, d2 = st.columns(4)
    with y1:
        min_year = st.number_input(
            "Min album year (0 = none)",
            min_value=0,
            max_value=3000,
            value=st.session_state.get("pc_min_year", 0),
            step=1,
            key="pc_min_year",
        )
    with y2:
        max_year = st.number_input(
            "Max album year (0 = none)",
            min_value=0,
            max_value=3000,
            value=st.session_state.get("pc_max_year", 0),
            step=1,
            key="pc_max_year",
        )
    with d1:
        min_duration_sec = st.number_input(
            "Min track duration (sec, 0 = none)",
            min_value=0,
            max_value=60 * 60 * 4,
            value=st.session_state.get("pc_min_duration", 0),
            step=5,
            key="pc_min_duration",
        )
    with d2:
        max_duration_sec = st.number_input(
            "Max track duration (sec, 0 = none)",
            min_value=0,
            max_value=60 * 60 * 4,
            value=st.session_state.get("pc_max_duration", 0),
            step=5,
            key="pc_max_duration",
        )

    st.markdown("### Recency bias (Recently added)")

    rba, rbb = st.columns(2)
    with rba:
        recently_added_days = st.number_input(
            "Recently added window (days, 0 = ignore)",
            min_value=0,
            max_value=365,
            value=st.session_state.get("pc_recent_days", 0),
            step=1,
            key="pc_recent_days",
        )
    with rbb:
        recently_added_weight = st.slider(
            "Recently added weight",
            min_value=0.0,
            max_value=2.0,
            value=st.session_state.get("pc_recent_weight", 1.0),
            step=0.05,
            key="pc_recent_weight",
            help=">1 favors newly added tracks more strongly; <1 downweights them.",
        )

    st.markdown("### Artist / album caps")
    aa1, aa2 = st.columns(2)
    with aa1:
        max_tracks_per_artist = st.number_input(
            "Max tracks per artist (0 = no cap)",
            min_value=0,
            max_value=1000,
            value=st.session_state.get("pc_max_artist", 6),
            step=1,
            key="pc_max_artist",
        )
    with aa2:
        max_tracks_per_album = st.number_input(
            "Max tracks per album (0 = no cap)",
            min_value=0,
            max_value=1000,
            value=st.session_state.get("pc_max_album", 0),
            step=1,
            key="pc_max_album",
        )

    st.markdown("### History filters (for history-based seeds & fallbacks)")

    h1, h2 = st.columns(2)
    with h1:
        history_min_rating = st.number_input(
            "History: min track rating (0–10)",
            min_value=0,
            max_value=10,
            value=st.session_state.get("pc_hist_min_rating", 0),
            step=1,
            key="pc_hist_min_rating",
        )
    with h2:
        history_max_play_count = st.number_input(
            "History: max play count (–1 = no max)",
            min_value=-1,
            max_value=10000,
            value=st.session_state.get("pc_hist_max_play_count", -1),
            step=1,
            key="pc_hist_max_play_count",
        )

    st.markdown("### Genre & collections filters (album-level)")

    gcol1, gcol2 = st.columns(2)
    with gcol1:
        genre_seeds_raw = st.text_input(
            "Genre seeds (comma-separated, album-level)",
            value=st.session_state.get("pc_seed_genres", ""),
            placeholder="e.g., Rock, Psychedelic Rock, Jazz",
            key="pc_seed_genres",
        )
        include_collections_raw = st.text_input(
            "Include only collections (comma-separated, album-level)",
            value=st.session_state.get("pc_include_collections", ""),
            placeholder="e.g., Classic Rock, Sunday Psych",
            key="pc_include_collections",
        )
    with gcol2:
        exclude_collections_raw = st.text_input(
            "Exclude collections (comma-separated, album-level)",
            value=st.session_state.get("pc_exclude_collections", ""),
            placeholder="e.g., Christmas, Kids Music",
            key="pc_exclude_collections",
        )
        exclude_genres_raw = st.text_input(
            "Exclude genres (comma-separated, album-level)",
            value=st.session_state.get("pc_exclude_genres", ""),
            placeholder="e.g., Holiday, Comedy",
            key="pc_exclude_genres",
        )

    genre_strict = st.checkbox(
        "Genre strict (enforce album genres against genre_seeds)",
        value=st.session_state.get("pc_genre_strict", False),
        key="pc_genre_strict",
        help=(
            "If checked, album genres must intersect with genre_seeds. "
            "Off-genre tracks can still appear up to the allowed fraction."
        ),
    )

    off_genre_fraction = st.slider(
        "Allow off-genre fraction",
        min_value=0.0,
        max_value=1.0,
        value=st.session_state.get("pc_allow_off_genre", 0.2),
        step=0.05,
        key="pc_allow_off_genre",
        help="Maximum fraction of tracks allowed that don't match the album-level genre seeds.",
    )

    st.markdown("### Seed strategy")


    seed_options = [
        "Auto (infer from seeds/history)",
        "History only",
        "Genre seeds",
        "Sonic Album Mix",
        "Sonic Artist Mix",
        "Sonic Combo (Albums + Artists)",
        "Album Echoes (seed albums only)",
        "Sonic Tracks (track-level similarity)",
        "Strict Collection"
    ]

    seed_mode_label = st.selectbox(
        "Seed mode",
        seed_options,
        index=0,
        key="pc_seed_mode_label",   # 👈 critical
        help=(
            "How to build the core candidate set:\n"
            "- Auto: let the script infer based on provided seeds and history\n"
            "- History only: build from listening history\n"
            "- Genre seeds: use genre_seeds only\n"
            "- Sonic Album/Artist: seed + sonically similar albums/artists\n"
            "- Sonic Combo: both sonic albums and sonic artists\n"
            "- Album Echoes: one track per seed album, no extra sonic expansion\n"
            "- Sonic Tracks: expand directly from seed tracks via sonicallySimilar()"
        ),
    )

    seed_mode_map = {
        "Auto (infer from seeds/history)": "",
        "History only": "history",
        "Genre seeds": "genre",
        "Sonic Album Mix": "sonic_album_mix",
        "Sonic Artist Mix": "sonic_artist_mix",
        "Sonic Combo (Albums + Artists)": "sonic_combo",
        "Album Echoes (seed albums only)": "album_echoes",
        "Sonic Tracks (track-level similarity)": "track_sonic",
        "Strict Collection": "strict_collection",
    }
    seed_mode = seed_mode_map[st.session_state["pc_seed_mode_label"]]


    # Slider initialization
    new_vs_legacy_slider = 0.5

    if seed_mode == "strict_collection":
        st.divider()
        st.markdown("### 🎚️ Curator Controls")
        new_vs_legacy_slider = st.slider(
            "Playlist Balance: Legacy vs. New",
            min_value=0.0,
            max_value=1.0,
            value=0.5,
            step=0.05,
            help="1.0 (New) forces tracks added in the last 180 days to the top. 0.0 (Legacy) favors high play counts and ratings."
        )
        col1, col2 = st.columns(2)
        col1.caption("⬅️ Legacy Staples")
        col2.markdown("<p style='text-align: right; color: gray; font-size: small;'>Fresh Blood ➡️</p>", unsafe_allow_html=True)
        st.divider()

    st.markdown("### Seed sources")

    st.caption(
        "Any combination of seeds can be used. Playlist Creator will deduplicate "
        "and then expand via sonic similarity and/or history."
    )

    def _parse_list(text: str) -> List[str]:
        return [p.strip() for p in (text or "").split(",") if p.strip()]

    col1, col2 = st.columns(2)

    with col1:
        seed_track_keys_raw = st.text_input(
            "Seed track ratingKeys (comma-separated)",
            value=st.session_state.get("pc_seed_tracks", ""),
            placeholder="e.g., 12345, 67890",
            key="pc_seed_tracks",
        )
        seed_artist_names_raw = st.text_input(
            "Seed artist names (comma-separated)",
            value=st.session_state.get("pc_seed_artists", ""),
            placeholder="e.g., Bill Evans, Miles Davis",
            key="pc_seed_artists",
        )
        seed_playlist_names_raw = st.text_input(
            "Seed playlist names (comma-separated)",
            value=st.session_state.get("pc_seed_playlists", ""),
            placeholder="e.g., Dinner Party",
            key="pc_seed_playlists",
        )

    with col2:
        seed_collection_names_raw = st.text_input(
            "Seed collection names (comma-separated)",
            value=st.session_state.get("pc_seed_collections", ""),
            placeholder="e.g., All That Jazz",
            key="pc_seed_collections",
        )

    seed_track_keys = _parse_list(seed_track_keys_raw)
    seed_artist_names = _parse_list(seed_artist_names_raw)
    seed_playlist_names = _parse_list(seed_playlist_names_raw)
    seed_collection_names = _parse_list(seed_collection_names_raw)
    genre_seeds = _parse_list(genre_seeds_raw)
    include_collections = _parse_list(include_collections_raw)
    exclude_collections = _parse_list(exclude_collections_raw)
    exclude_genres = _parse_list(exclude_genres_raw)

    st.divider()
    st.warning(
        "Playlist Creator will create a **new playlist** in Plex but will not edit "
        "metadata or existing playlists."
    )
    payload = {
        "plex": {
            "url": cfg.plex_baseurl,
            "token": cfg.plex_token,
            "music_library": music_lib,
        },
        "playlist": {
            "exclude_played_days": int(exclude_days),
            "history_lookback_days": int(lookback_days),
            "max_tracks": int(max_tracks),
            "sonic_similar_limit": int(sonic_similar_limit),
            "historical_ratio": float(historical_ratio),
            "exploit_weight": float(exploit_weight),

            # rating filters
            "min_rating": {
                "track": int(min_track),
                "album": int(min_album),
                "artist": int(min_artist),
            },
            "allow_unrated": 1 if allow_unrated else 0,

            # time periods / fallback
            "use_time_periods": 1 if use_time_periods else 0,
            "seed_fallback_mode": seed_fallback_mode.lower(),
            "seed_mode": seed_mode,

            "new_vs_legacy_slider": new_vs_legacy_slider,

            # play count filters
            "min_play_count": int(min_play_count),
            "max_play_count": int(max_play_count),

            # 💿 Year & duration filters (album-level year, track-level duration)
            "min_year": int(min_year),
            "max_year": int(max_year),
            "min_duration_sec": int(min_duration_sec),
            "max_duration_sec": int(max_duration_sec),

            # 🕒 Recency bias (recently added)
            "recently_added_days": int(recently_added_days),
            "recently_added_weight": float(recently_added_weight),

            # 👥 Artist / album caps
            "max_tracks_per_artist": int(max_tracks_per_artist),
            "max_tracks_per_album": int(max_tracks_per_album),

            # 📜 History filters
            "history_min_rating": int(history_min_rating),
            "history_max_play_count": int(history_max_play_count),

            # 🎯 Genre strictness & collections
            "genre_strict": 1 if genre_strict else 0,
            "allow_off_genre_fraction": float(off_genre_fraction),
            "include_collections": include_collections,
            "exclude_collections": exclude_collections,
            "exclude_genres": exclude_genres,

            # 🔥 Custom playlist title → naming + cover art
            "custom_title": custom_title.strip() or None,

            # Seed lists
            "seed_track_keys": seed_track_keys,
            "seed_artist_names": seed_artist_names,
            "seed_playlist_names": seed_playlist_names,
            "seed_collection_names": seed_collection_names,
            "genre_seeds": genre_seeds,
        },
    }

    st.divider()

    # --- 3. SHOW RUN BUTTON ---
    run_btn = st.button("Generate Playlist (Run on Laptop)", type="primary", key="pc_run")
    
    # Stop here if the user hasn't clicked Run
    if not run_btn:
        return

    # --- Execution Logic (Below this remains mostly the same) ---
    st.markdown("### Playlist Creator log")

    log_box = st.empty()
    log_lines = []

    env = os.environ.copy()
    env.update({
        "PLEX_BASEURL": cfg.plex_baseurl,
        "PLEX_URL": cfg.plex_baseurl,
        "PLEX_TOKEN": cfg.plex_token,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })


    try:
        proc = subprocess.Popen(
            ["python", PLAYLIST_CREATOR_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        assert proc.stdout is not None
        assert proc.stdin is not None

        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()

        for line in proc.stdout:
            log_lines.append(line.rstrip("\r\n"))
            tail = "\n".join(log_lines[-200:])
            log_box.code(tail or "…", language="bash")

        ret = proc.wait()
    except FileNotFoundError:
        st.error("Could not execute playlist_creator.py. Check that it's in Scripts/ and Python is on PATH.")
        return
    except Exception as e:
        st.error(f"Error while running Playlist Creator: {e}")
        return

    if ret == 0:
        st.success("Playlist Creator finished successfully. Check Plex for the new playlist.")
    else:
        st.error(f"Playlist Creator exited with code {ret}. Review the log above.")



# ---------------------------
# Compare tab
# ---------------------------
def ui_compare_tab():
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Zurich")
    except Exception:
        tz = None

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
        old_df = read_csv_forgiving(old_up)
        new_df = read_csv_forgiving(new_up)
    except Exception as e:
        st.error(f"Could not read one of the CSVs: {e}")
        return

    with st.expander("Preview (first 25 rows)"):
        st.markdown("**OLD**")
        st.dataframe(old_df.head(25), use_container_width=True)
        st.markdown("**NEW**")
        st.dataframe(new_df.head(25), use_container_width=True)

    st.divider()

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
        value=False,
        key="compare_details"
    )

    run = st.button("Run comparison", type="primary", key="compare_run")
    if not run:
        return

    try:
        result_df, summary = compare_exports_add_match_cols(old_df, new_df, include_details=include_details)
        st.success("Comparison complete.")

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
            st.write(
                f"**{mc}** — yes: {counts.get('yes', 0):,} | "
                f"no: {counts.get('no', 0):,} | "
                f"not found: {counts.get('not found', 0):,}"
            )

        st.markdown("### Output preview (first 50 rows)")
        st.dataframe(result_df.head(50), use_container_width=True)

        now = datetime.now(tz) if tz else datetime.now()
        date_prefix = now.strftime("%Y_%m_%d")
        out_filename = f"{date_prefix} metadata_comparison_output.csv"
        out_path = os.path.join(APP_DIR, out_filename)

        try:
            result_df.to_csv(out_path, index=False, encoding="utf-8")
            st.info(f"Saved to app folder: `{out_path}`")
        except Exception as e:
            st.error(f"Could not save file to app folder: {e}")

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
    st.caption("Export music metadata, update metadata, and build intelligent playlists.")
    cfg = ui_sidebar_config()

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Export",
        "Update from CSV (Single Script)",
        "Update from CSV (Multiple Scripts)",
        "Compare Exported Metadata",
        "Playlist Creator",
    ])
    with tab1:
        ui_export_tab(cfg)
    with tab2:
        ui_update_tab(cfg)
    with tab3:
        ui_update_multi_tab(cfg)
    with tab4:
        ui_compare_tab()
    with tab5:
        ui_playlist_creator_tab(cfg)


if __name__ == "__main__":
    main()
