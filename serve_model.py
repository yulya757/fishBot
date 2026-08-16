# serve_model.py — локальный HTTP-сервер инференса для fishBot.
# Держит lora_v2 в VRAM, main.py (через ai_backends.py) ходит сюда за ответами,
# когда в .env выставлено INFERENCE_BACKEND=local.
#
# Запуск (только на GPU-хосте): source <venv>/bin/activate && python serve_model.py
#
# POST /reply {"messages": [{"role": "user", "content": "го в зал"}, ...],
#              "system_extra": "...", "temperature": 0.6, "top_p": 0.8, "max_new_tokens": 150}
#          -> {"reply": "не могу\nу меня вождение"}
#
# Загрузка модели вынесена в load_model() и вызывается только из __main__, чтобы
# файл оставался импортируемым и проверяемым (py_compile, тесты Handler-логики)
# на машине без unsloth/torch/GPU.

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

from reply_utils import clean_reply, merge_roles

load_dotenv()

HOST = os.getenv("LOCAL_MODEL_HOST", "127.0.0.1")
PORT = int(os.getenv("LOCAL_MODEL_PORT", "8008"))
MODEL_DIR = os.getenv("LOCAL_MODEL_DIR", "lora_v2")
MAX_SEQ_LEN = int(os.getenv("LOCAL_MAX_SEQ_LEN", "1024"))
LOAD_IN_4BIT = os.getenv("LOCAL_MODEL_4BIT", "true").strip().lower() != "false"
HISTORY_MAX = 20  # реплик контекста (обучались на окнах до 10)

# Загрузка системного промпта — тот же файл, что использует main.py
try:
    with open("system_promt.txt", "r", encoding="utf-8") as f:
        SYSTEM_PROMPT = f.read().strip()
except FileNotFoundError:
    print("Ошибка: Файл system_promt.txt не найден.")
    SYSTEM_PROMPT = ""
except Exception as e:
    print(f"Ошибка при чтении system_promt.txt: {e}")
    SYSTEM_PROMPT = ""

model = None
tokenizer = None
tok = None
IM_END = None
GEN_LOCK = threading.Lock()  # GPU одна — генерации строго по очереди


def load_model():
    """Грузит базу + LoRA-адаптер в VRAM. Требует unsloth/torch — вызывать только на GPU-хосте."""
    global model, tokenizer, tok, IM_END
    from unsloth import FastModel

    print("Загружаю модель...")
    model, tokenizer = FastModel.from_pretrained(
        model_name=MODEL_DIR, max_seq_length=MAX_SEQ_LEN, load_in_4bit=LOAD_IN_4BIT,
    )
    FastModel.for_inference(model)
    tok = getattr(tokenizer, "tokenizer", tokenizer)
    IM_END = tok.convert_tokens_to_ids("<|im_end|>")
    if IM_END is None or IM_END == tok.unk_token_id:
        IM_END = tok.eos_token_id


def generate_reply(messages, system_extra="", temperature=0.6, top_p=0.8, max_new_tokens=150):
    system = SYSTEM_PROMPT
    if system_extra:
        system += "\n\n" + system_extra
    full_messages = [{"role": "system", "content": system}]
    full_messages += merge_roles(messages)[-HISTORY_MAX:]

    prompt = tokenizer.apply_chat_template(
        full_messages, tokenize=False, add_generation_prompt=True,
        enable_thinking=False,  # пустой <think></think>, как в обучающих данных
    )
    if not isinstance(prompt, str):  # редкий сбой обёртки процессора
        prompt = prompt[0] if isinstance(prompt, (list, tuple)) and prompt else str(prompt)
    inputs = tok(prompt, return_tensors="pt", add_special_tokens=False).to(model.device)
    with GEN_LOCK:
        out = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=1.05,
            do_sample=True,
            eos_token_id=IM_END,
            pad_token_id=IM_END,
        )
    reply = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    return clean_reply(reply)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/reply":
            self.send_error(404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = generate_reply(
                body["messages"],
                body.get("system_extra", ""),
                float(body.get("temperature", 0.6)),
                float(body.get("top_p", 0.8)),
                int(body.get("max_new_tokens", 150)),
            )
            data = json.dumps({"reply": reply}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:  # один плохой запрос не должен ронять сервер
            print("ошибка запроса:", e)
            self.send_error(500, str(e))

    def log_message(self, fmt, *args):
        pass  # не спамим access-логом


if __name__ == "__main__":
    load_model()
    print(f"Сервер готов: http://{HOST}:{PORT}/reply")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
