import streamlit as st
import pandas as pd
import networkx as nx
import plotly.graph_objects as go
import community.community_louvain as community_louvain
import numpy as np
import os
import io

# --- Config & Helpers ---
APP_DIR = os.getcwd()
EXPORTS_DIR = os.path.join(APP_DIR, "Exports")

def read_csv_forgiving(source):
    """Robust CSV reader handling file paths or uploaded objects."""
    if isinstance(source, str):
        with open(source, 'rb') as f:
            raw = f.read()
    elif hasattr(source, 'getvalue'):
        raw = source.getvalue()
    else:
        return pd.read_csv(source)

    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False, encoding=enc)
        except UnicodeDecodeError:
            continue
    
    text = raw.decode("utf-8", errors="replace")
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)

@st.cache_data(show_spinner=False)
def load_and_process_data(csv_file):
    """Reads CSV, builds graph, detects communities, calculates 3D layout."""
    try:
        df = pd.read_csv(csv_file)
    except:
        df = read_csv_forgiving(csv_file)
    
    if 'Artist' not in df.columns or 'Similar_Artists' not in df.columns:
        return None, "CSV missing required columns: 'Artist' or 'Similar_Artists'"

    df['Similar_Artists'] = df['Similar_Artists'].fillna('')
    
    G = nx.Graph()
    library_artists = set(df['Artist'].unique())
    
    for artist in library_artists:
        G.add_node(artist, type='Library')
        
    for _, row in df.iterrows():
        source = row['Artist']
        similars = [s.strip() for s in row['Similar_Artists'].split(',') if s.strip()]
        for target in similars:
            if target not in library_artists:
                G.add_node(target, type='Missing')
            G.add_edge(source, target)

    try:
        partition = community_louvain.best_partition(G, resolution=1.2)
    except:
        partition = {n: 0 for n in G.nodes()}
    
    pos = nx.spring_layout(G, dim=3, k=0.15, iterations=40, seed=42)
    
    node_data = []
    for node in G.nodes():
        x, y, z = pos[node]
        node_data.append({
            "Artist": node, "x": x, "y": y, "z": z,
            "Cluster": str(partition[node]),
            "Type": G.nodes[node]['type']
        })
        
    return pd.DataFrame(node_data), G

def run():
    if __name__ == "__main__":
        st.set_page_config(layout="wide", page_title="Plex Music Galaxy")

    st.subheader("Plex Music Galaxy")
    
    try:
        import scipy
    except ImportError:
        st.error("Missing dependency: `scipy`. Please run `pip install scipy`.")
        return

    # 1. File Input
    export_files = []
    if os.path.exists(EXPORTS_DIR):
        export_files = [f for f in os.listdir(EXPORTS_DIR) if "Artist_Level_Info" in f and f.endswith(".csv")]
    
    col1, col2 = st.columns([1, 1])
    with col1:
        input_method = st.radio("Input Source", ["Select from Exports", "Upload File"], horizontal=True)
    
    csv_source = None
    if input_method == "Select from Exports":
        if export_files:
            sel = st.selectbox("Select File", sorted(export_files, reverse=True))
            csv_source = os.path.join(EXPORTS_DIR, sel)
        else:
            st.info("No export files found.")
    else:
        csv_source = st.file_uploader("Upload 'Artist_Level_Info.csv'", type=["csv"])

    if not csv_source:
        return

    # 2. Process (Automatic once file is present)
    with st.spinner("Computing galaxy layout..."):
        try:
            plot_df, G = load_and_process_data(csv_source)
            if plot_df is None:
                st.error(G)
                return
        except Exception as e:
            st.error(f"Processing Error: {e}")
            return

    st.divider()

    # 3. Controls
    c1, c2 = st.columns([1, 2])
    show_missing = c1.toggle("Show Missing Artists (Red)", value=False)
    
    filtered_df = plot_df.copy() if show_missing else plot_df[plot_df['Type'] == 'Library'].copy()
    available = sorted(filtered_df['Artist'].unique())
    target_artist = c2.selectbox("Focus on Artist", options=available, index=0 if available else None)

    # 4. Plot
    if target_artist and not filtered_df.empty:
        if target_artist in G:
            raw_neighbors = list(G.neighbors(target_artist))
            visible_neighbors = set(raw_neighbors).intersection(set(filtered_df['Artist']))
        else:
            visible_neighbors = set()

        def get_style(row):
            if row['Artist'] == target_artist: return "Focus"
            if row['Artist'] in visible_neighbors: return "Neighbor"
            return "Background"

        filtered_df['display_type'] = filtered_df.apply(get_style, axis=1)

        fig = go.Figure()

        # Background (Library)
        bg_lib = filtered_df[(filtered_df['display_type'] == 'Background') & (filtered_df['Type'] == 'Library')]
        for c_id in sorted(bg_lib['Cluster'].unique()):
            d = bg_lib[bg_lib['Cluster'] == c_id]
            fig.add_trace(go.Scatter3d(
                x=d['x'], y=d['y'], z=d['z'], mode='markers',
                marker=dict(size=4, opacity=0.3),
                hovertext=d['Artist'], hovertemplate='%{hovertext}<extra></extra>',
                name=f'Cluster {c_id}', legendgroup='Clusters'
            ))

        # Background (Missing)
        bg_miss = filtered_df[(filtered_df['display_type'] == 'Background') & (filtered_df['Type'] == 'Missing')]
        if not bg_miss.empty:
            fig.add_trace(go.Scatter3d(
                x=bg_miss['x'], y=bg_miss['y'], z=bg_miss['z'], mode='markers',
                marker=dict(size=3, color='#ff4b4b', opacity=0.2),
                hovertext=bg_miss['Artist'], hovertemplate='%{hovertext}<extra></extra>',
                name='Missing', legendgroup='Missing'
            ))

        # Neighbors
        n_lib = filtered_df[(filtered_df['display_type'] == 'Neighbor') & (filtered_df['Type'] == 'Library')]
        if not n_lib.empty:
            fig.add_trace(go.Scatter3d(
                x=n_lib['x'], y=n_lib['y'], z=n_lib['z'], mode='markers+text',
                marker=dict(size=8, color='#00d2ff', opacity=0.9),
                text=n_lib['Artist'], textposition="top center", textfont=dict(size=10, color="white"),
                hovertemplate='%{text}<extra></extra>',
                name='Related (Library)'
            ))

        n_miss = filtered_df[(filtered_df['display_type'] == 'Neighbor') & (filtered_df['Type'] == 'Missing')]
        if not n_miss.empty:
            fig.add_trace(go.Scatter3d(
                x=n_miss['x'], y=n_miss['y'], z=n_miss['z'], mode='markers+text',
                marker=dict(size=8, color='#ff4b4b', opacity=0.9, symbol='diamond'),
                text=n_miss['Artist'], textposition="top center", textfont=dict(size=10, color="#ffcbcb"),
                hovertemplate='%{text}<extra></extra>',
                name='Related (Missing)'
            ))

        # Focus
        foc = filtered_df[filtered_df['display_type'] == 'Focus']
        if not foc.empty:
            fig.add_trace(go.Scatter3d(
                x=foc['x'], y=foc['y'], z=foc['z'], mode='markers+text',
                marker=dict(size=20, color='white', line=dict(width=2, color='black')),
                text=foc['Artist'], textposition="top center",
                textfont=dict(size=14, color="yellow", family="Arial Black"), 
                hovertemplate='%{text}<extra></extra>',
                name='Selected'
            ))

        fig.update_layout(
            height=800, showlegend=False,
            scene=dict(xaxis=dict(visible=False), yaxis=dict(visible=False), zaxis=dict(visible=False), bgcolor='#0e1117'),
            paper_bgcolor='#0e1117', margin=dict(l=0, r=0, b=0, t=0)
        )
        st.plotly_chart(fig, use_container_width=True)

if __name__ == "__main__":
    run()