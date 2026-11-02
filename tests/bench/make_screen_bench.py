"""Builds the Mini-side benchmark script: 3 cropped screenshots (browser area only) embedded as base64, 12
questions with known answers. The Mini runs each local vision model on them; scoring happens here."""
import base64, io, json
from pathlib import Path
from PIL import Image

RUNS = Path(r"C:\Projects\AgentComputerUse\data\runs")
CROP = (0, 0, 950, 1010)                     # the Edge/Notepad area, not the editor or chat
shots = {"popup": RUNS / "20261005-180101" / "step-03.png",
         "results": RUNS / "20261005-182126" / "step-03.png",
         "save": RUNS / "20261005-182126" / "step-09.png"}
images = {}
for k, p in shots.items():
    img = Image.open(p).convert("RGB").crop(CROP)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    images[k] = base64.b64encode(buf.getvalue()).decode()

POPUP_LIST = ("[1] Button 'Back'\n[2] Edit 'Address and search bar'\n[3] Hyperlink 'Microsoft Privacy Statement'\n"
              "[4] Button 'Got it'\n[5] Button 'Customize'")
RESULTS_LIST = ("[1] Button 'See more'\n[2] Hyperlink 'Springfield, IL Hourly Weather | AccuWeather'\n[3] Button 'IMAGES'\n"
                "[4] Edit 'weather in Springfield Illinois'")
Q = [
    ("popup", "Is there a pop-up or dialog box on top of the page asking the user something? Answer only yes or no."),
    ("popup", "List the exact labels of the buttons and links inside the pop-up, separated by commas. Nothing else."),
    ("popup", "These are the items on screen:\n" + POPUP_LIST + "\nWhich number is the button that dismisses the pop-up "
              "without changing anything? Answer with the number only."),
    ("results", "Is there a pop-up or dialog box on top of the page asking the user something? Answer only yes or no."),
    ("results", "What is the current temperature shown for Springfield, Illinois? Answer with the number and unit only."),
    ("results", "What are the forecast high and low temperatures for Wed 7? Answer as: high X, low Y."),
    ("results", "These are the items on screen:\n" + RESULTS_LIST + "\nWhich number opens the AccuWeather result? "
                "Answer with the number only."),
    ("results", "What phone number is shown on this page? If there is none, answer exactly: none."),
    ("save", "Is there a dialog box open on top of the window? Answer only yes or no."),
    ("save", "What text is currently in the File name box? Quote it exactly."),
    ("save", "Which button saves the file? Answer with its exact label only."),
    ("save", "What does the red error message in the dialog say? If there is none, answer exactly: none."),
]
script = '''import json, time, urllib.request
IMAGES = %s
QUESTIONS = %s
MODELS = %s
out = []
for model in MODELS:
    for i, (img, q) in enumerate(QUESTIONS, 1):
        body = json.dumps({"model": model, "prompt": q, "images": [IMAGES[img]], "stream": False, "think": False,
                           "keep_alive": "5m", "options": {"temperature": 0, "num_predict": 80}}).encode()
        t = time.time()
        try:
            req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
            ans = json.loads(urllib.request.urlopen(req, timeout=300).read()).get("response", "").strip()
        except Exception as e:
            ans = "ERROR " + str(e)[:100]
        out.append({"model": model, "q": i, "answer": ans, "seconds": round(time.time() - t, 1)})
        print(json.dumps(out[-1], ensure_ascii=False), flush=True)
''' % (json.dumps(images), json.dumps(Q), json.dumps(["gemma3:4b", "qwen3-vl:2b"]))
Path(__file__).with_name("screen_bench_mini.py").write_text(script, encoding="utf-8")
print("questions:", len(Q), "| script KB:", len(script) // 1024)
