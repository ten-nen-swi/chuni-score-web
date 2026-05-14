import os
import requests
import time
from flask import Flask, render_template, redirect, Response, stream_with_context, request
from pyairtable import Table

app = Flask(__name__)

# --- 設定（セキュリティ対策：直接書かずにサーバーの設定から読み取る） ---
# ローカルでテストする時は、一時的に直接書いてもOKですが、GitHubに上げる時はこの形にします
CHUNI_TOKEN = os.environ.get("CHUNI_TOKEN")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
BASE_ID = os.environ.get("BASE_ID")
TABLE_NAME = os.environ.get("TABLE_NAME")

UPDATE_BATCH_SIZE = 50 # 1回の更新でAirtableに書き込む最大件数
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
        sss_1009_count = 0
        max_score = 0
        for name in player_names:
            val = f.get(f"{name}_Score", 0)
            is_sss = f.get(name, False)
            scores[name] = {"val": val, "is_sss": is_sss}
            if is_sss: sss_count += 1
            if val >= 1009000: sss_1009_count += 1
            if val > max_score: max_score = val

        # アタッチメント画像取得
        jacket_attachments = f.get('ジャケット', [])
        jacket_url = jacket_attachments[0]['url'] if jacket_attachments else "https://via.placeholder.com/70"

        song_info = {
            "title": f.get('タイトル', 'Unknown'),
            "diff": f.get('難易度', ''),
            "jacket": jacket_url,
            "scores": scores,
            "sss_count": sss_count,
            "sss_1009_count": sss_1009_count,
            "max_score": max_score
        }
        
        if const not in grouped_data:
            grouped_data[const] = []
        grouped_data[const].append(song_info)

    # 定数順に並べ替え
    sorted_keys = sorted(grouped_data.keys(), key=lambda x: float(x), reverse=True)

    # 指定された優先順位で並べ替え
    # 1. SSS達成人数 (sss_count)
    # 2. 1009000以上達成人数 (sss_1009_count)
    # 3. 5人の中の最高スコア (max_score)
    # 全て同じならタイトル順
    for const in sorted_keys:
        grouped_data[const].sort(
            key=lambda x: (-x['sss_count'], -x['sss_1009_count'], -x['max_score'], x['title'])
        )

    return render_template("index.html", 
                           grouped_data=grouped_data, 
                           sorted_keys=sorted_keys, 
                           player_names=player_names,
                           player_config_for_html=PLAYER_CONFIG)

@app.route("/update/<name>/<user_id>")
def update_player_score(name, user_id):
    # ブラウザでアクセスした時、最初は画面(HTML)を返す
    # JavaScript(EventSource)からのリクエストは Accept ヘッダーがこれになる
    if request.headers.get('Accept') != 'text/event-stream':
        return render_template("update.html")

    def generate():
        yield f"data: === {name.upper()} (User: {user_id}) 同期開始 ===\n\n"
        
        if not CHUNI_TOKEN or not AIRTABLE_API_KEY:
            yield "data: [!] APIキーまたはトークンが設定されていません。\n\n"
            return

        # Airtableから現在のデータを取得
        yield "data: Airtableから全楽曲データを読み込み中...\n\n"
        try:
            a_records = table.all()
            a_map = {f"{str(r['fields'].get('ID'))}_{r['fields'].get('難易度')}": r for r in a_records}
        except Exception as e:
            yield f"data: [!] Airtable読み込み失敗: {e}\n\n"
            return

        # chunirec APIから取得
        url = "https://api.chunirec.net/2.0/records/showall.json"
        params = {"token": CHUNI_TOKEN, "user_name": user_id, "region": "jp2"}
        
        try:
            res = requests.get(url, params=params)
            c_data = res.json().get('records', [])
            if not c_data:
                yield "data: [!] chunirecからデータが取得できませんでした。\n\n"
                return
        except Exception as e:
            yield f"data: [!] chunirec通信エラー: {e}\n\n"
            return

        count_upd = 0
        count_checked = 0
        score_col = f"{name}_Score"
        check_col = name

        for c in c_data:
            # 定数13.9以下はスキップ（元のスクリプト通り）
            if float(c.get('const', 0)) <= 13.9:
                continue

            count_checked += 1
            # 10件ごとに進捗を出してタイムアウトを防ぐ
            if count_checked % 10 == 0:
                yield f"data: ... {count_checked}件チェック中\n\n"

            key = f"{str(c.get('id'))}_{c.get('diff')}"

            if key in a_map:
                a_row = a_map[key]
                c_score = int(c.get('score', 0))
                current_score = a_row['fields'].get(score_col, 0)

                # スコアが上がっている場合のみ更新
                if c_score > current_score:
                    try:
                        table.update(a_row['id'], {
                            score_col: c_score,
                            check_col: True if c_score >= 1007500 else False
                        })
                        yield f"data: {c.get('title')} ({c.get('diff')}) : {current_score:,} -> {c_score:,} 更新完了\n\n"
                        count_upd += 1
                        time.sleep(0.2) # Airtableのレートリミット対策
                        if count_upd >= UPDATE_BATCH_SIZE:
                            yield f"data: --- {name.upper()} 50件更新に達したため一時停止します ---\n\n"
                            yield "data: BATCH_FINISHED\n\n"
                            return
                    except Exception as e:
                        yield f"data: [!] {c.get('title')} 更新失敗: {e}\n\n"

        yield f"data: --- {name.upper()} 完了！ 更新: {count_upd}件 ---\n\n"
        yield "data: FINISHED\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={'X-Accel-Buffering': 'no'})

if __name__ == "__main__":
    app.run()