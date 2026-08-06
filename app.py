import os
import json
import requests
import time
from threading import Lock
from flask import Flask, render_template, redirect, Response, stream_with_context, request
from pyairtable import Table

try:
    from redis import Redis
except ImportError:
    Redis = None

app = Flask(__name__)

# --- 設定（セキュリティ対策：直接書かずにサーバーの設定から読み取る） ---
# ローカルでテストする時は、一時的に直接書いてもOKですが、GitHubに上げる時はこの形にします
CHUNI_TOKEN = os.environ.get("CHUNI_TOKEN")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
BASE_ID = os.environ.get("BASE_ID")
TABLE_NAME = os.environ.get("TABLE_NAME")
REDIS_URL = os.environ.get("REDIS_URL")

UPDATE_LIMIT = 50 # 1回の操作で更新する最大件数
AIRTABLE_BATCH_SIZE = 10 # Airtable APIが1リクエストで処理できる最大件数
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "86400"))
CACHE_KEY = "chuni-score:airtable-records:v1"
# プレイヤーリスト
PLAYER_CONFIG = [
    {"name": "mea", "user_id": "tennenswi"},
    {"name": "e", "user_id": "alanioala"},
    {"name": "tute", "user_id": "tutenero"},
    {"name": "rise", "user_id": "risechuni"},
    {"name": "Sakon", "user_id": "souther64"},
]

table = Table(AIRTABLE_API_KEY, BASE_ID, TABLE_NAME)

_memory_cache = {"records": None, "expires_at": 0}
_cache_lock = Lock()
_redis_client = None


def get_redis_client():
    """Render Key Valueへ接続する。未設定・障害時はNoneを返す。"""
    global _redis_client
    if not REDIS_URL or Redis is None:
        return None
    if _redis_client is None:
        try:
            client = Redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            client.ping()
            _redis_client = client
        except Exception as e:
            app.logger.warning("Key Value connection failed: %s", e)
            return None
    return _redis_client


def get_cached_records():
    """Key Value、次にプロセスメモリからAirtableレコードを取得する。"""
    client = get_redis_client()
    if client:
        try:
            cached = client.get(CACHE_KEY)
            if cached:
                records = json.loads(cached)
                with _cache_lock:
                    _memory_cache["records"] = records
                    _memory_cache["expires_at"] = time.time() + CACHE_TTL_SECONDS
                return records
        except Exception as e:
            app.logger.warning("Key Value read failed: %s", e)

    with _cache_lock:
        if (_memory_cache["records"] is not None
                and _memory_cache["expires_at"] > time.time()):
            return _memory_cache["records"]
    return None


def set_cached_records(records):
    """最新レコードをKey Valueとプロセスメモリの両方へ保存する。"""
    with _cache_lock:
        _memory_cache["records"] = records
        _memory_cache["expires_at"] = time.time() + CACHE_TTL_SECONDS

    client = get_redis_client()
    if client:
        try:
            client.setex(
                CACHE_KEY,
                CACHE_TTL_SECONDS,
                json.dumps(records, ensure_ascii=False),
            )
        except Exception as e:
            app.logger.warning("Key Value write failed: %s", e)


def get_airtable_records():
    """キャッシュを優先し、なければAirtableから取得して保存する。"""
    records = get_cached_records()
    if records is not None:
        return records
    records = table.all()
    set_cached_records(records)
    return records


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]

@app.route("/")
def index():
    # 鍵がセットされていない場合のエラー回避
    if not AIRTABLE_API_KEY:
        return "サーバーにAPIキーが設定されていません。"

    records = get_airtable_records()
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
        yield "data: キャッシュまたはAirtableから全楽曲データを読み込み中...\n\n"
        try:
            a_records = get_airtable_records()
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

        pending_updates = []
        count_checked = 0
        score_col = f"{name}_Score"
        check_col = name

        for c in c_data:
            # 定数13.9以下はスキップ（元のスクリプト通り）
            if float(c.get('const', 0)) <= 13.9:
                continue

            count_checked += 1
            # 100件ごとに進捗を出してタイムアウトを防ぐ
            if count_checked % 100 == 0:
                yield f"data: ... {count_checked}件チェック中\n\n"

            key = f"{str(c.get('id'))}_{c.get('diff')}"

            if key in a_map:
                a_row = a_map[key]
                c_score = int(c.get('score', 0))
                current_score = a_row['fields'].get(score_col, 0)

                # スコアが上がっている場合のみ、最大50件まで更新候補に追加
                if c_score > current_score:
                    pending_updates.append({
                        "id": a_row['id'],
                        "fields": {
                            score_col: c_score,
                            check_col: True if c_score >= 1007500 else False
                        },
                        "log": f"{c.get('title')} ({c.get('diff')}) : {current_score:,} -> {c_score:,}",
                    })
                    if len(pending_updates) >= UPDATE_LIMIT:
                        break

        count_upd = 0
        records_by_id = {r.get("id"): r for r in a_records}
        for batch in chunks(pending_updates, AIRTABLE_BATCH_SIZE):
            payload = [{"id": item["id"], "fields": item["fields"]} for item in batch]
            try:
                table.batch_update(payload)
                for item in batch:
                    cached_record = records_by_id.get(item["id"])
                    if cached_record:
                        cached_record.setdefault("fields", {}).update(item["fields"])
                    yield f"data: {item['log']} 更新完了\n\n"
                    count_upd += 1
                set_cached_records(a_records)
            except Exception as e:
                yield f"data: [!] {len(batch)}件の一括更新に失敗: {e}\n\n"

        if len(pending_updates) >= UPDATE_LIMIT:
            yield f"data: --- {name.upper()} 50件更新に達したため一時停止します ---\n\n"
            yield "data: BATCH_FINISHED\n\n"
            return

        yield f"data: --- {name.upper()} 完了！ 更新: {count_upd}件 ---\n\n"
        yield "data: FINISHED\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={'X-Accel-Buffering': 'no'})

@app.route("/update_master")
def update_master():
    if request.headers.get('Accept') != 'text/event-stream':
        return render_template("update.html")

    def generate():
        yield "data: === 楽曲データ全件登録処理開始 ===\n\n"
        
        yield "data: chunirecから楽曲データを取得中...\n\n"
        try:
            res = requests.get("https://api.chunirec.net/2.0/music/showall.json", 
                               params={"token": CHUNI_TOKEN, "region": "jp2"})
            music_data = res.json()
        except Exception as e:
            yield f"data: [!] chunirec APIエラー: {e}\n\n"
            return

        yield "data: キャッシュまたはAirtableから既存データを確認中...\n\n"
        try:
            existing_records = get_airtable_records()
            existing_keys = {f"{r['fields'].get('タイトル')}_{r['fields'].get('難易度')}"
                             for r in existing_records if r['fields'].get('タイトル')}
        except Exception as e:
            yield f"data: [!] Airtable読み込み失敗: {e}\n\n"
            return

        pending_creates = []
        target_diffs = ["EXP", "MAS", "ULT"]

        for song in music_data:
            title = song.get('meta', {}).get('title')
            song_id = song.get('meta', {}).get('id')
            data_block = song.get('data', {})

            for diff in target_diffs:
                diff_info = data_block.get(diff)
                if not diff_info: continue
                
                const_val = float(diff_info.get('const') or diff_info.get('level') or 0.0)
                const_str = "{:.1f}".format(const_val)

                # 14.0以上 かつ 未登録なら作成
                if const_val >= 14.0 and f"{title}_{diff}" not in existing_keys:
                    fields = {
                        "ID": str(song_id),
                        "タイトル": str(title),
                        "難易度": str(diff),
                        "定数": const_str,
                    }
                    pending_creates.append({"fields": fields, "title": title, "diff": diff})
                    existing_keys.add(f"{title}_{diff}")
                    if len(pending_creates) >= UPDATE_LIMIT:
                        break
            if len(pending_creates) >= UPDATE_LIMIT:
                break

        count = 0
        for batch in chunks(pending_creates, AIRTABLE_BATCH_SIZE):
            try:
                created = table.batch_create([item["fields"] for item in batch])
                existing_records.extend(created)
                set_cached_records(existing_records)
                count += len(created)
                for item in batch:
                    yield f"data: 【登録】{item['title']} ({item['diff']}) / 定数: {item['fields']['定数']}\n\n"
            except Exception as e:
                yield f"data: [!] {len(batch)}件の一括登録に失敗: {e}\n\n"

        if len(pending_creates) >= UPDATE_LIMIT:
            yield "data: --- 50件に達したため一時停止します ---\n\n"
            yield "data: BATCH_FINISHED\n\n"
            return

        yield f"data: --- 完了！ 新たに {count} 件の楽曲を登録しました ---\n\n"
        yield "data: FINISHED\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream', headers={'X-Accel-Buffering': 'no'})

if __name__ == "__main__":
    app.run()
