import discord
import json
import gemini
from datetime import datetime
from keep_alive import keep_alive
import os

TOKEN = os.getenv('DISCORD_TOKEN')

client = discord.Client(intents=discord.Intents.all())

@client.event
async def on_ready():
    print('Bot is ready')
    print(gemini.hoge())

@client.event
async def on_message(message):
    # print(f'{message.channel}: {message.author}: {message.author.name}: {message.content}')
    if message.author == client.user:
        return
    # if message.reference != None:
    #     reference = await message.channel.fetch_message(message.reference.message_id)
    #     print(f'→{reference.channel}: {reference.author}: {reference.author.name}: {reference.content}')

    if message.content.startswith('!ev'):
        # メッセージに画像が添付されている場合は初めの一枚を取得
        image = None
        if message.attachments != None:
            for attachment in message.attachments:
                print("attachment")
                print(attachment.content_type)
                if attachment.content_type.startswith('image/'):
                    image = await attachment.read()
                    break

        d = datetime.now()
        input = f"""次のイベントの内容を解釈し、日時、タイトル、説明文、開催場所等の情報を自動的に生成し、JSON形式で返してください。
出力はJSON文のみとし、1日ごとにイベントを区切り、"events"キーの配列に1つずつ"start_time"、"end_time"、"title"、"description"、"external"、"location"を含んだJSONオブジェクトを格納する形にしてください。
イベントが1つだけでも要素1の配列にし、イベントが存在しない場合は空の配列にすること。
descriptionは箇条書きで簡潔にまとめてください。ただし配列にせず、改行コードを含めた文字列で記述してください。
ただし、プロンプトで与えられた日時は日本標準時ですが、start_timeとend_timeは「%Y-%m-%dT%H:%M:%SZ」形式のUTCで書いてください。
end_timeが不明な場合はstart_timeから1時間後の日時を入れてください。
また開催日時が明示的に過去である場合を除いて、start_timeは現在時刻よりも後の日時です。したがって、start_time、end_timeは過去の日付にせずに、1年後など現在時刻よりも後の日時を設定してください。
なお、現在の日本標準時での日時は{d.strftime('%Y/%m/%d %H:%M:%S')}です。
また、開催場所は、明示的にdiscordのボイスチャンネルが貼られた場合は"external"をfalseにして"location"にチャンネルURLを文字列で格納、それ以外の場合は"external"にtrueを入れて"location"にもっともらしい場所の名前やURLの文字列（完全に不明なら「不明」）を格納してください。
メッセージの送信者：{message.author.name}
イベントについて記述したメッセージ：「{str.strip(message.content[3:])}」"""
        if message.reference != None:
            reference = await message.channel.fetch_message(message.reference.message_id)
            print(f'{reference.channel}: {reference.author}: {reference.author.name}: {reference.content}')
            input += f"\n返信先のメッセージ送信者：{reference.author.name}\n返信先のメッセージ：「{reference.content}」"
            # 画像がまだ設定されておらず返信先のメッセージに画像が添付されている場合は初めの一枚を取得
            if image == None and reference.attachments:
                for attachment in reference.attachments:
                    if attachment.content_type.startswith('image/'):
                        image = await attachment.read()
                        break
        response = str.strip(gemini.getResponse(input))

        # responseを解釈して、日付、タイトル、説明文を取り出す
        if response.startswith("```"):
            response = str.strip(response[3:-3])
        if response.startswith("json"):
            response = str.strip(response[4:])

        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            await message.channel.send("返答のパースに失敗しました：\n" + response)
            return

        # イベントがないまたはサイズ0の場合は警告を出す
        if 'events' not in parsed or len(parsed['events']) == 0:
            await message.channel.send("イベントが見つかりませんでした。")
            return

        try:
            # イベントを1つずつ取り出してdiscordのイベントとして登録
            for event in parsed['events']:
                start_time = datetime.strptime(event['start_time'], "%Y-%m-%dT%H:%M:%S%z")
                end_time = datetime.strptime(event['end_time'], "%Y-%m-%dT%H:%M:%S%z")
                title = event['title']
                description = event['description']
                external = event['external']
                if external:
                    entity_type = discord.EntityType.external
                    location = event['location']  # 任意の場所
                    channel = None
                    if image != None:
                        await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, location=location, privacy_level=discord.PrivacyLevel.guild_only, image=image)
                    else:
                        await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, location=location, privacy_level=discord.PrivacyLevel.guild_only)
                else:
                    entity_type = discord.EntityType.voice
                    location = None
                    event['location'] = str.strip(event['location'])
                    if event['location'][-1]=="/":
                        event['location'] = event['location'][:-1]
                    event['location'] = event['location'].split('/')[-1]
                    print(event['location'])
                    channel = message.guild.get_channel(int(event['location']))
                    if image != None:
                        await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, channel=channel, privacy_level=discord.PrivacyLevel.guild_only, image=image)
                    else:
                        await message.guild.create_scheduled_event(name=title, description=description, start_time=start_time, end_time=end_time, entity_type=entity_type, channel=channel, privacy_level=discord.PrivacyLevel.guild_only)
        except Exception as e:
            await message.channel.send("エラーが発生しました。Botの管理者に連絡してください。")
            # await message.channel.send("エラーが発生しました。Botの管理者に連絡してください。\n" + response + "\n" + str(e))
            return

        responseMessage = "以下のイベントを登録しました。\n"
        for event in parsed['events']:
            responseMessage += f"```タイトル：{event['title']}\n説明：{event['description']}\n開始（日本時間）：{start_time.astimezone().strftime('%Y/%m/%d %H:%M')}\n終了（日本時間）：{end_time.astimezone().strftime('%Y/%m/%d %H:%M')}\n場所：{event['location']}```\n\n"
        await message.channel.send(responseMessage)

client.run(TOKEN)