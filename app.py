import os
import requests
from flask import Flask, render_template, redirect
from pyairtable import Table

app = Flask(__name__)

# --- 設定（セキュリティ対策：直接書かずにサーバーの設定から読み取る） ---
# ローカルでテストする時は、一時的に直接書いてもOKですが、GitHubに上げる時はこの形にします
CHUNI_TOKEN = os.environ.get("CHUNI_TOKEN")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
BASE_ID = os.environ.get("BASE_ID")
TABLE_NAME = os.environ.get("TABLE_NAME")

# プレイヤーリスト
PLAYER_CONFIG = [
    {"name": "mea", "user_id": "tennenswi"},
    {"name": "e", "user_id": "alanioala"},
    {"name": "tute", "user_id": "tutenero"},
    {"name": "rise", "user_id": "risechuni"},
    {"name": "Sakon", "user_id": "souther64"},
]

table = Table(AIRTABLE_API_KEY, BASE_ID, TABLE_NAME)

@app.route("/")
def index():
    # 鍵がセットされていない場合のエラー回避
    if not AIRTABLE_API_KEY:
        return "サーバーにAPIキーが設定されていません。"

    records = table.all()
    player_names = [p["name"] for p in PLAYER_CONFIG]
    
    grouped_data = {}
    for r in records:
        f = r.get('fields', {})
        const = f.get('定数', '0.0')
        
        scores = {}
        sss_count = 0
        for name in player_names:
            val = f.get(f"{name}_Score", 0)
            is_sss = f.get(name, False)
            scores[name] = {"val": val, "is_sss": is_sss}
            if is_sss: sss_count += 1

        # アタッチメント画像取得
        jacket_attachments = f.get('ジャケット', [])
        jacket_url = jacket_attachments[0]['url'] if jacket_attachments else "https://via.placeholder.com/70"

        song_info = {
            "title": f.get('タイトル', 'Unknown'),
            "diff": f.get('難易度', ''),
            "jacket": jacket_url,
            "scores": scores,
            "sss_count": sss_count
        }
        
        if const not in grouped_data:
            grouped_data[const] = []
        grouped_data[const].append(song_info)

    # 定数順に並べ替え
    sorted_keys = sorted(grouped_data.keys(), key=lambda x: float(x), reverse=True)

    # 達成人数が多い順に並べ替え
    for const in sorted_keys:
        grouped_data[const].sort(key=lambda x: (-x['sss_count'], x['title']))

    return render_template("index.html", 
                           grouped_data=grouped_data, 
                           sorted_keys=sorted_keys, 
                           player_names=player_names,
                           player_config_for_html=PLAYER_CONFIG)

if __name__ == "__main__":
    app.run(debug=True, port=5000)