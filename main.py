import discord
import json
import logging
from google import genai
from google.genai.types import GenerateContentConfig, HarmCategory, HarmBlockThreshold, SafetySetting
from google.oauth2 import service_account
from datetime import datetime, timedelta
import os
from pytz import timezone
import pytz
import io
import db
import traceback
import re


def _parse_utc_datetime(value: str) -> datetime:
    """AI出力の日時文字列をUTCのaware datetimeに変換する。"""
    if not isinstance(value, str):
        raise TypeError("start_time/end_time は文字列である必要があります")

    s = value.strip()
    if not s:
        raise ValueError("start_time/end_time が空です")

    # 末尾ZはUTCとして扱う（datetime.fromisoformat はZ非対応）
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"

    # +0000 / -0900 のようなオフセットを +00:00 形式へ正規化
    if re.search(r"[+-]\d{4}$", s):
        s = s[:-2] + ":" + s[-2:]

    # 秒が省略されがちな入力を吸収する
    m_no_tz = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2}))?", s)
    if m_no_tz:
        date_part, hm_part, sec_part = m_no_tz.group(1), m_no_tz.group(2), m_no_tz.group(3)
        sec = sec_part if sec_part is not None else "00"
        s = f"{date_part}T{hm_part}:{sec}+00:00"
    else:
        m_with_tz = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::(\d{2}))?([+-]\d{2}:\d{2})",
            s,
        )
        if m_with_tz and m_with_tz.group(3) is None:
            date_part, hm_part, tz_part = m_with_tz.group(1), m_with_tz.group(2), m_with_tz.group(4)
            s = f"{date_part}T{hm_part}:00{tz_part}"

    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pytz.UTC)
    return dt.astimezone(pytz.UTC)

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('bot_errors.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# サービスアカウント認証情報の読み込み
SERVICE_ACCOUNT_FILE = 'google-credentials.json'
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=['https://www.googleapis.com/auth/cloud-platform']
)

# Vertex AIプロジェクト設定
with open(SERVICE_ACCOUNT_FILE, 'r') as f:
    service_account_info = json.load(f)
    VERTEX_PROJECT_ID = service_account_info.get('project_id')

VERTEX_PROJECT_REGION = os.getenv('VERTEX_PROJECT_REGION', 'us-central1')
TOKEN = os.getenv('DISCORD_TOKEN')

# Gemini APIクライアントの設定
safety_settings = [
    SafetySetting(
        category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=HarmBlockThreshold.BLOCK_NONE,
    ),
    SafetySetting(
        category=HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=HarmBlockThreshold.BLOCK_NONE,
    ),
    SafetySetting(
        category=HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=HarmBlockThreshold.BLOCK_LOW_AND_ABOVE,
    ),
    SafetySetting(
        category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=HarmBlockThreshold.BLOCK_NONE,
    ),
]

# Vertex AI用のGenAIクライアント初期化
genai_client = genai.Client(
    vertexai=True,
    project=VERTEX_PROJECT_ID,
    location=VERTEX_PROJECT_REGION,
    credentials=credentials
)

client = discord.Client(intents=discord.Intents.all())

@client.event
async def on_ready():
    print('Bot is ready')
    try:
        db.init_db()
        logging.info("DB初期化完了")
    except Exception as e:
        logging.error(f"DB初期化失敗: {e}")
        # DB初期化に失敗してもbotは起動を続行

@client.event
async def on_message(message):
    # print(f'{message.channel}: {message.author}: {message.author.name}: {message.content}')
    if message.author == client.user:
        return
    if message.author.bot:
        return
    dm = (type(message.channel) == discord.DMChannel) and (client.user == message.channel.me)

    if dm or message.content.startswith('!ev'):
        # 以降はGeminiを用いるので、制限を確認
        user_id = str(message.author.id)
        try:
            allowed, error_message = db.check_limits(user_id)
            if not allowed:
                await message.channel.send(error_message)
                return
        except Exception as e:
            logging.error(f"DB制限チェックエラー: {e}")
            await message.channel.send("データベースエラーが発生しました。Bot管理者に連絡してください。")
            return
        # メッセージに画像が添付されている場合は初めの一枚を取得
        image = None
        if message.attachments != None:
            for attachment in message.attachments:
                # print("attachment")
                # print(attachment.content_type)
                if attachment.content_type.startswith('image/'):
                    image = await attachment.read()
                    break

        # ユーザーへの前提通り、日本標準時を明示してモデルに渡す
        d = datetime.now(timezone('Asia/Tokyo'))
        
        # システムプロンプト(固定の指示部分)
        system_instruction = [
            '# 役割',
            'あなたはイベント情報抽出の専門家です。与えられたメッセージからイベントの詳細を抽出し、JSON形式で出力します。',
            '',
            '# 出力形式',
            '出力はJSON文のみとし、トップレベルに"events"キー（配列）を持たせ、その配列にイベントを1つずつJSONオブジェクトとして格納してください。各イベントは"start_time"、"end_time"、"title"、"description"、"external"、"location"を必ず含めてください。',
            'イベントが1つだけでも要素1の配列にし、イベントが存在しない場合は空の配列にしてください。',
            'また、出力はjsonのプレーンテキストとし、コードブロックで囲んだりしないでください。',
            '',
            '# フィールドの詳細',
            '- start_time, end_time: "YYYY-MM-DDTHH:MM:SSZ"（UTC、末尾Z）で記述',
            '- title: イベントのタイトル',
            '- description: 箇条書きで簡潔にまとめた説明文(配列ではなく改行コードを含めた文字列)',
            '- external: 入力テキスト内に "https://discord.com/channels/" で始まる具体的なURLが明記されている場合のみ false。それ以外はすべて true',
            '- location: externalがfalseの場合はそのチャンネルURL。trueの場合は場所の名前やURL(不明なら「不明」)。決して入力にないURLを捏造しないこと',
            '',
            '# 日時の扱い',
            '- ユーザーの入力で与えられる日時は、タイムゾーンが明示されている場合を除き、日本標準時(UTC+9)であるとする',
            '  - UTCに変換してstart_time, end_timeを設定する',
            '  - 例えば「2026年5月1日15時」とあれば、start_timeは「2026-05-01T06:00:00Z」となる',
            '- end_timeが不明な場合はstart_timeから1時間後の日時を設定する',
            '- 開催日時が明示的に過去である場合を除き、start_timeは現在時刻よりも後で、条件に合う最も近い日時を入力しているものとする',
            '  - 例えば「5月1日15時」とあれば、現在の日時が2026年4月30日であっても「2026-05-01T06:00:00Z」となる',
            '  - 同じ「5月1日15時」でも、現在の日時が2026年5月1日16時であれば「2027-05-01T06:00:00Z」となる',
            '  - 現在の日時が2026年4月30日なら、「明日の10時」とあれば「2026-05-01T01:00:00Z」となる',
            '  - 現在の日時が2026年4月30日なら、「10日の15時」とあれば「2026-05-10T06:00:00Z」となる',
            '',
            '# 注意事項',
            '- URLの捏造禁止: 入力テキストに含まれていないURLは生成しないこと。',
            '- Discordチャンネルの扱い: 「ボイスチャンネル」「VC」等があっても、具体的なURL（https://discord.com/channels/...）がない限り external=true とし、locationには名称（例：「ボイスチャンネル」）を入れること。',
            '- 予備日の扱い: 予備の日程と明示されている場合、予備の日程もイベントとして出力すること。ただし、予備の日程であってもstart_time, end_timeは必ず設定すること。また、予備日のみタイトルの末尾に「（予備日）」と追加すること'
        ]
        
        # ユーザープロンプト(メッセージ内容)
        user_prompt = f"現在の日本標準時での日時は{d.strftime('%Y/%m/%d %H:%M:%S')}です。\n\n"
        
        if message.reference != None:
            reference = await message.channel.fetch_message(message.reference.message_id)
            user_prompt += f"返信先のメッセージ送信者:{reference.author.name}\n返信先のメッセージ:「{reference.content}」\n\n次がメッセージ本文です。返信先に対する指示がある場合、それに従ってください。\n\n"
            # 画像がまだ設定されておらず返信先のメッセージに画像が添付されている場合は初めの一枚を取得
            if image == None and reference.attachments:
                for attachment in reference.attachments:
                    if attachment.content_type and attachment.content_type.startswith('image/'):
                        image = await attachment.read()
                        break
        
        user_prompt += f"メッセージの送信者:{message.author.name}\n"
        user_prompt += f"イベントについて記述したメッセージ:「{message.content.replace('!ev','').strip()}」"
        
        # Gemini APIリクエスト
        try:
            response_obj = genai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_prompt,
                config=GenerateContentConfig(
                    system_instruction=system_instruction,
                    safety_settings=safety_settings
                ),
            )
            
            if response_obj.text is None:
                logging.error(f"AI応答がNone: user={message.author.name}, message={message.content[:100]}")
                await message.channel.send("AIからの応答が取得できませんでした。")
                return
            
            response = str.strip(response_obj.text)
            logging.info(f"AI応答取得成功: user={message.author.name}, response_length={len(response)}")
        
        except Exception as e:
            logging.error(f"Gemini APIエラー: user={message.author.name}, error={str(e)}\n{traceback.format_exc()}")
            await message.channel.send("AIとの通信中にエラーが発生しました。しばらく時間をおいて再度お試しください。")
            return

        # responseを解釈して、日付、タイトル、説明文を取り出す
        if response.startswith("```"):
            response = str.strip(response[3:-3])
        if response.startswith("json"):
            response = str.strip(response[4:])

        try:
            parsed = json.loads(response)
            logging.info(f"JSONパース成功: events_count={len(parsed.get('events', []))}")
        except json.JSONDecodeError as e:
            logging.error(f"JSONパースエラー: user={message.author.name}, error={str(e)}, response={response[:500]}")
            await message.channel.send("AIの応答形式が不正です。もう一度お試しください。")
            return

        # イベントがないまたはサイズ0の場合は警告を出す
        if 'events' not in parsed or len(parsed['events']) == 0:
            await message.channel.send("イベントが見つかりませんでした。")
            return

        responseMessage = "以下のイベントを登録しました。\n"
        ical_text = ""

        try:
            external_lock = False # external=trueのイベントが出現した後はすべてexternal=trueとみなすロック
            # イベントを1つずつ取り出してdiscordのイベントとして登録
            for event in parsed['events']:
                # UTCでの日時
                start_time = _parse_utc_datetime(event['start_time'])
                end_time = _parse_utc_datetime(event['end_time'])
                if end_time <= start_time:
                    end_time = start_time + timedelta(hours=1)
                title = event['title']
                description = event['description']
                external = event['external'] or external_lock

                # channelの存在確認
                channel = None
                if not external and not dm: # external=falseかつDMでない場合のみチャンネル取得を試みる
                    try:
                        # idを抽出
                        _loc = str.strip(event['location'])
                        if _loc[-1]=="/":
                            _loc = _loc[:-1]
                        _loc = _loc.split('/')[-1]
                        channel = message.guild.get_channel(int(_loc))
                    except Exception as e:
                        logging.error(f"channel取得エラー: user={message.author.name}, error={str(e)}")
                        channel = None

                if channel is None:
                    external = True
                    external_lock = True

                if external:
                    entity_type = discord.EntityType.external
                    location = event['location']  # 任意の場所
                    channel = None
                    if not dm: # DMの場合はイベントを作成出来ないので登録を無視
                        if image != None:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, location=location, privacy_level=discord.PrivacyLevel.guild_only, image=image)
                        else:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, location=location, privacy_level=discord.PrivacyLevel.guild_only)
                else:
                    if isinstance(channel, discord.VoiceChannel):
                        entity_type = discord.EntityType.voice
                    elif isinstance(channel, discord.StageChannel):
                        entity_type = discord.EntityType.stage_instance
                    else:
                        # デフォルトでvoiceに設定
                        entity_type = discord.EntityType.voice
                    location = None
                    if not dm: # DMの場合はイベントを作成出来ないので登録を無視
                        if image != None:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, channel=channel, privacy_level=discord.PrivacyLevel.guild_only, image=image)
                        else:
                            await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, channel=channel, privacy_level=discord.PrivacyLevel.guild_only)

                # icalendar形式で出力
                ical_text += "BEGIN:VEVENT\n"
                ical_text += f"SUMMARY:{title}\n"
                description_replaced = description.replace('\r', '').replace('\n', '\\n')
                ical_text += f"DESCRIPTION:{description_replaced}\n"
                ical_text += f"DTSTART:{start_time.strftime('%Y%m%dT%H%M%SZ')}\n"
                ical_text += f"DTEND:{end_time.strftime('%Y%m%dT%H%M%SZ')}\n"
                if external:
                    ical_text += f"LOCATION:{location}\n"
                else:
                    ical_text += f"LOCATION:Discord Voice Channel\n"
                ical_text += "END:VEVENT\n"

        except Exception as e:
            logging.error(
                f"イベント作成エラー: user={message.author.name}, "
                f"error={str(e)}, parsed_data={json.dumps(parsed, ensure_ascii=False)[:500]}\n"
                f"{traceback.format_exc()}"
            )
            await message.channel.send("イベントの作成中にエラーが発生しました。Botの管理者に連絡してください。")
            return

        if dm: # DMの場合はイベントを作成出来ないので登録を無視
            responseMessage = "以下の内容のスケジュールファイルを作成しました。\n"
        else:
            responseMessage = "以下のイベントを登録しました。\n"
        for event in parsed['events']:
            start_time = _parse_utc_datetime(event['start_time'])
            end_time = _parse_utc_datetime(event['end_time'])
            # 日本時間に変換してログに追加
            responseMessage += f"```タイトル：{event['title']}\n説明：{event['description']}\n開始（日本時間）：{start_time.astimezone(timezone('Asia/Tokyo')).strftime('%Y/%m/%d %H:%M')}\n終了（日本時間）：{end_time.astimezone(timezone('Asia/Tokyo')).strftime('%Y/%m/%d %H:%M')}\n場所：{event['location']}```\n\n"

        # ical_textをメモリ上のファイルに一時保存してアップロード
        with io.BytesIO() as f:
            f.write("BEGIN:VCALENDAR\nVERSION:2.0\n".encode('utf-8'))
            f.write(ical_text.encode('utf-8'))
            f.write("END:VCALENDAR\n".encode('utf-8'))
            f.seek(0)  # ファイルポインタを先頭に戻す
            await message.channel.send(responseMessage, file=discord.File(fp=f, filename="event.ics"))

if TOKEN is None:
    raise ValueError("DISCORD_TOKEN環境変数が設定されていません")

client.run(TOKEN)