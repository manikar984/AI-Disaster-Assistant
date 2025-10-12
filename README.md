# AI-Disaster-Assistant
AI-powered system to detect, classify, summarize, and visualize disaster-related posts in real-time from social media and static datasets.
Features

Disaster Classification: Detects whether a post is related to a disaster using keyword-based analysis.

Severity Detection: Assesses the severity of disasters as High, Medium, or Low based on textual cues.

Summarization: Generates short summaries of disaster-related posts for quick understanding.

Real-Time Data Integration: Fetches live disaster-related posts from X (Twitter) while handling rate limits and caching.

Interactive Map: Visualizes disaster events with severity indicators on an interactive map.

Streamlit Dashboard: Provides an easy-to-use web interface to explore and analyze disaster data.

Tech Stack

Python 3.x

Streamlit

Pandas

Folium

Tweepy (Twitter API)

How It Works

Load a static dataset of disaster-related posts.

Fetch live tweets from X (Twitter) using relevant disaster keywords.

Merge static and live data into a unified dataset.

Apply AI-based classification, severity detection, and summarization.

Display the results in an interactive Streamlit dashboard with a map visualization.

Installation
git clone https://github.com/your-username/AI-Disaster-Assistant.git
cd AI-Disaster-Assistant
pip install -r requirements.txt
streamlit run prototype.py

Usage

View the disaster table with classification, severity, and summaries.

Filter by location to explore specific regions.

Enter custom disaster posts for real-time analysis.

Visualize disasters on the interactive map with color-coded severity markers.
