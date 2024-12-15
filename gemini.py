from google import genai
import os

def hoge():
    return 'hoge'

def getResponse(message):

    client = genai.Client(
        api_key=os.environ('GEMINI_API_KEY')
    )

    response = client.models.generate_content(
        model=os.environ('GEMINI_MODEL'),
        contents=message
    )
    
    return response.text