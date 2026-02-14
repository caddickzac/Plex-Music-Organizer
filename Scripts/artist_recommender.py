import pandas as pd
import numpy as np
import re

def get_recommendations(df):
    """
    Python implementation of the R fuzzy recommender logic.
    Identifies missing artists based on library similarity and play counts.
    """
    # 1. Clean Library Artists for matching
    def clean_key(text):
        if not isinstance(text, str): return ""
        # Lowercase, remove punctuation, trim
        return re.sub(r'[^\w\s]', '', text.lower()).strip()

    # Create Artist-Level dataframe and ensure numeric types
    artist_df = df[['Artist', 'Similar_Artists', 'Total_Plays']].drop_duplicates('Artist').copy()
    
    # FIX: Convert Total_Plays from string to numeric, turning errors into 0
    artist_df['Total_Plays'] = pd.to_numeric(artist_df['Total_Plays'], errors='coerce').fillna(0)
    
    artist_df = artist_df[artist_df['Artist'].notna()]

    # Create canonical list of library keys
    library_clean = set(artist_df['Artist'].apply(clean_key).unique())

    # 2. Expand Similar Artists rows
    recs = artist_df.assign(Similar_Artists=artist_df['Similar_Artists'].str.split(r',\s*')).explode('Similar_Artists')
    recs = recs[recs['Similar_Artists'].notna() & (recs['Similar_Artists'] != "")]

    # Create match keys for suggested artists
    recs['suggested_key'] = recs['Similar_Artists'].apply(clean_key)

    # THE FUZZY FILTER: Only keep if the artist is NOT in your library
    recs = recs[~recs['suggested_key'].isin(library_clean)]

    # 3. Summarize and Score
    recommendations = recs.groupby('Similar_Artists').agg(
        Related_Library_Artists=('Artist', lambda x: ", ".join(x.unique())),
        Recommendation_Count=('Artist', 'count'),
        Related_Library_Artists_Total_Play_Count=('Total_Plays', 'sum')
    ).reset_index()

    # Calculate Hybrid Score: Count * log10(Total Plays + 1)
    # The math will now work because 'Related_Library_Artists_Total_Play_Count' is numeric
    recommendations['Priority_Score'] = (
        recommendations['Recommendation_Count'] * np.log10(recommendations['Related_Library_Artists_Total_Play_Count'] + 1)
    )

    return recommendations.rename(columns={'Similar_Artists': 'Missing_Artist'}).sort_values('Priority_Score', ascending=False)