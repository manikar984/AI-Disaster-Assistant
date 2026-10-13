# -------------------------
# twitter_api.py — Real-time Disaster Tweet Fetcher (Improved)
# -------------------------

import tweepy
import pandas as pd
import os
import json
from datetime import datetime, timedelta

# -------------------------
# 1. Authenticate with X (Twitter) API
# -------------------------
BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")  # Replace with your actual token
client = tweepy.Client(bearer_token=BEARER_TOKEN)

# -------------------------
# 2. Fetch disaster tweets with caching & rate-limit handling
# -------------------------
def fetch_disaster_tweets(
    query="flood OR earthquake OR fire OR cyclone OR storm -is:retweet lang:en",
    max_results=10
):
    """
    Fetches latest disaster-related tweets.
    Returns a DataFrame with columns: text, location, time
    """
    if not BEARER_TOKEN:
        return pd.DataFrame([])
    try:
        tweets = client.search_recent_tweets(
            query=query,
            tweet_fields=["created_at", "geo", "lang"],
            max_results=max_results
        )

        tweet_data = []
        if tweets.data:
            for tweet in tweets.data:
                tweet_data.append({
                    "text": tweet.text,
                    "location": "Unknown",  # Can later map with tweet.geo if available
                    "time": tweet.created_at
                })

        return pd.DataFrame(tweet_data)

    except tweepy.TooManyRequests:
        print("⚠️ Rate limit reached! Returning empty DataFrame for now.")
        return pd.DataFrame([])

    except tweepy.Unauthorized:
        print("❌ Unauthorized: Check your Twitter API Bearer Token.")
        return pd.DataFrame([])

    except Exception as e:
        print(f"❌ Unexpected error fetching tweets: {e}")
        return pd.DataFrame([])

# -------------------------
# 3. Merge with static dataset safely
# -------------------------
def merge_with_static_data(static_file="disaster_data.json"):
    """
    Merge static JSON dataset with latest tweets.
    Returns a combined pandas DataFrame.
    """
    # Load static data
    if os.path.exists(static_file):
        with open(static_file, "r") as f:
            static_data = json.load(f)
    else:
        static_data = []

    static_df = pd.DataFrame(static_data)

    # Fetch live tweets
    live_df = fetch_disaster_tweets(max_results=20)

    # Merge both
    combined_df = pd.concat([static_df, live_df], ignore_index=True)

    # Optionally, remove duplicate tweets based on text
    combined_df = combined_df.drop_duplicates(subset="text", keep="last").reset_index(drop=True)

    return combined_df

# -------------------------
# 4. Example Run
# -------------------------
if __name__ == "__main__":
    df = merge_with_static_data()
    print("✅ Combined Dataset Preview:")
    print(df.head())
