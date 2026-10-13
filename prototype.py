# -------------------------
# Final BuildAthon Version: 🌍 AI-Powered Disaster Assistant (with Real-Time X Integration)
# -------------------------

import pandas as pd
import json
import folium
from html import escape
import streamlit as st
from streamlit_folium import st_folium
import tweepy  # ✅ For fetching real-time X (Twitter) data
from twitter_api import merge_with_static_data


# -------------------------
# Load static dataset first
# -------------------------
with open("disaster_data.json", "r") as f:
    data = json.load(f)  # <-- now 'data' exists
# -------------------------
# -------------------------
# Step 0: Fetch Real-Time Tweets from X (Twitter) with caching
# -------------------------
import streamlit as st
import tweepy

import os
BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")
client = tweepy.Client(bearer_token=BEARER_TOKEN)

@st.cache_data(ttl=300)
def fetch_disaster_tweets():
    if not BEARER_TOKEN:
        return []
    query = "flood OR earthquake OR fire OR cyclone OR storm -is:retweet lang:en"
    try:
        tweets = client.search_recent_tweets(
            query=query,
            tweet_fields=["created_at"],
            max_results=10
        )
        tweet_data = []
        if tweets.data:
            for tweet in tweets.data:
                tweet_data.append({
                    "text": tweet.text,
                    "location": "Unknown"
                })
        return tweet_data

    except tweepy.TooManyRequests:
        st.warning("⚠️ Rate limit reached, using cached/static data instead.")
        return []

    except Exception as e:
        st.warning(f"⚠️ Could not fetch live tweets: {e}")
        return []

# -------------------------
# Fetch tweets (cached)
realtime_data = fetch_disaster_tweets()
data.extend([{**r, "data_mode": "live"} for r in realtime_data])
df = pd.DataFrame(data)

# -------------------------
# Step 1: Load sample disaster data
# -------------------------
with open("disaster_data.json", "r") as f:
    data = json.load(f)

# Merge static + live tweet data
realtime_data = fetch_disaster_tweets()
data.extend([{**r, "data_mode": "live"} for r in realtime_data])
df = pd.DataFrame(data)

# Merge static + live tweet data
realtime_data = fetch_disaster_tweets()
data.extend([{**r, "data_mode": "live"} for r in realtime_data])

# -------------------------
# Step 1b: Update Static Dataset
# -------------------------
# Load static dataset
with open("disaster_data.json", "r") as f:
    static_data = json.load(f)

# Fetch live tweets (cached)
live_data = fetch_disaster_tweets()

# Never overwrite the historical fixture with live records.
combined_data = [{**r, "data_mode": "sample"} for r in static_data] + [{**r, "data_mode": "live"} for r in live_data]
st.warning("Legacy prototype: static records are historical samples, not current alerts.")

# Create dataframe
df = pd.DataFrame(combined_data)

# -------------------------
# Step 2: Keyword-based classifier
# -------------------------
def classify_post(text):
    disaster_keywords = ["flood", "earthquake", "fire", "cyclone", "storm"]
    if any(word.lower() in text.lower() for word in disaster_keywords):
        return "Disaster"
    else:
        return "Not Disaster"

# -------------------------
# Step 3: Severity Detection
# -------------------------
def detect_severity(text):
    text = text.lower()
    if any(word in text for word in ["massive", "major", "severe", "deadly", "destroyed"]):
        return "High"
    elif any(word in text for word in ["moderate", "damaged", "affected"]):
        return "Medium"
    elif any(word in text for word in ["minor", "small", "slight"]):
        return "Low"
    else:
        return "Medium"

# -------------------------
# Step 4: CPU-friendly summarization
# -------------------------
def summarize_post(text, label):
    if label == "Disaster":
        return ' '.join(text.split()[:7]) + ('...' if len(text.split()) > 7 else '')
    else:
        return ""

# Apply functions
df['label'] = df['text'].apply(classify_post)
df['severity'] = df['text'].apply(detect_severity)
df['summary'] = df.apply(lambda row: summarize_post(row['text'], row['label']), axis=1)

# -------------------------
# Step 5: Streamlit Dashboard
# -------------------------
st.title("🌍 AI-Powered Disaster Assistant")
st.markdown("""
This AI system **detects, classifies, and visualizes disasters** using intelligent keyword detection,
severity analysis, and **real-time Twitter (X) data integration**.
""")

# -------------------------
# Step 6: Filter by Location
# -------------------------
locations = ["All"] + df['location'].unique().tolist()
selected_location = st.selectbox("📍 Filter by Location", locations)

if selected_location != "All":
    filtered_df = df[df['location'] == selected_location]
else:
    filtered_df = df

st.write("### 🧾 Disaster Posts")
st.dataframe(filtered_df[['text', 'label', 'severity', 'summary']])

# -------------------------
# Step 7: Real-Time Input (Live Prediction)
# -------------------------
st.subheader("🧠 Real-Time Disaster Report Analyzer")

user_input = st.text_area("Enter a new disaster-related post or message:")

if st.button("Analyze Post"):
    label = classify_post(user_input)
    severity = detect_severity(user_input)
    summary = summarize_post(user_input, label)

    if label == "Disaster":
        st.success(f"🚨 **Prediction:** {label}")
    else:
        st.info(f"✅ **Prediction:** {label}")

    sev_colors = {"High": "🔴", "Medium": "🟠", "Low": "🟢"}
    st.markdown(f"**Severity:** {sev_colors.get(severity, '')} {severity}")
    st.markdown(f"**Summary:** {summary}")

    if label == "Disaster":
        new_entry = pd.DataFrame([{
            "text": user_input,
            "location": "Unknown",
            "label": label,
            "severity": severity,
            "summary": summary
        }])
        df = pd.concat([df, new_entry], ignore_index=True)
        st.success("✅ Post added to dataset!")

# -------------------------
# Step 8: Map Visualization with Severity Colors
# -------------------------
m = folium.Map(location=[20.5937, 78.9629], zoom_start=5)

location_coords = {
    "Kerala": [10.8505, 76.2711],
    "Japan": [36.2048, 138.2529],
    "New York": [40.7128, -74.0060]
}

def get_color_by_severity(sev):
    if sev == "High":
        return "darkred"
    elif sev == "Medium":
        return "orange"
    else:
        return "green"

for index, row in df.iterrows():
    if row['label'] == "Disaster":
        coords = location_coords.get(row['location'])
        if coords is None:
            continue
        folium.CircleMarker(
            location=coords,
            radius=10 if row['severity'] == "High" else 7 if row['severity'] == "Medium" else 5,
            color=get_color_by_severity(row['severity']),
            fill=True,
            fill_opacity=0.7,
            popup=f"<b>{escape(str(row['location']))}</b><br>{escape(str(row['summary']))}<br><b>Severity:</b> {escape(str(row['severity']))}",
        ).add_to(m)

st.write("### 🗺️ Disaster Map (with Live Updates)")
st_folium(m, width=700, height=500)

# -------------------------
# Step 9: Analytics Summary
# -------------------------
st.markdown("## 📊 Disaster Analytics Overview")

col1, col2, col3 = st.columns(3)
col1.metric("📝 Total Posts", len(df))
col2.metric("🚨 Disasters Detected", len(df[df['label'] == "Disaster"]))
col3.metric("🔴 High Severity", len(df[df['severity'] == "High"]))

# -------------------------
# Step 10: Footer
# -------------------------
st.markdown("---")
st.caption("💡 Developed by Ajay — BuildAthon 2025 | Rule-based prototype | Streamlit & optional X API")
